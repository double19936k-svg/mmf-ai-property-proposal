"""Local PPT queue bridge. Reads native transcripts; never invokes a model itself."""
from __future__ import annotations
import hashlib
import json
import os
import re
import time
from pathlib import Path
from .base import write_json


def read(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def rows(path):
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return []
    result = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def object_from_text(text):
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try:
        value = json.loads(cleaned)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


class PPTBridge:
    def __init__(self, instance: Path):
        self.root = instance.resolve()
        self.contract = read(self.root / "RUN_CONTRACT.json")
        if not self.contract.get("deadline_epoch") or not self.contract.get("generation_stop_epoch"):
            raise ValueError("A current immutable run contract is required")
        if self.contract["generation_stop_epoch"] >= self.contract["deadline_epoch"]:
            raise ValueError("Export/review reserve is required")
        if "instance_created_epoch" not in self.contract or self.contract["deadline_epoch"] != self.contract["instance_created_epoch"] + 5400 or self.contract["generation_stop_epoch"] != self.contract["instance_created_epoch"] + 4200:
            raise ValueError("The original 90-minute instance clock must include the 20-minute closeout reserve")
        if self.contract["caps"]["external_paid_api"] != 0 or self.contract["caps"]["concurrency"] not in (1, 2):
            raise ValueError("PPT bridge requires external API=0 and concurrency<=2")
        self.q = self.root / "q"
        self.state = self.root / "runtime/ppt_bridge"
        self.state.mkdir(parents=True, exist_ok=True)
        for folder in ("out", "in", "claims", "done", "abandoned", "answers", "ledger"):
            (self.q / folder).mkdir(parents=True, exist_ok=True)

    def event(self, name, **values):
        with (self.state / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": name, "at": time.time(), **values}, ensure_ascii=False) + "\n")

    def active(self):
        return [p for p in self.state.glob("active_*.json") if read(p).get("job_id")]

    def claim(self, worker, *, now=None):
        if worker not in {"w1", "w2"}:
            raise ValueError("worker must be w1/w2")
        # Cross-process reservation: two native workers cannot claim a third slot
        # or the same request. The lock is released even on a malformed request.
        lock = self.state / "claim.lock"
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return {"status": "BUSY"}
        os.close(descriptor)
        try:
            current = time.time() if now is None else now
            if current >= self.contract["generation_stop_epoch"] or (self.q / "STOP").exists():
                return {"status": "HALT"}
            active_path = self.state / f"active_{worker}.json"
            if active_path.exists() or len(self.active()) >= self.contract["caps"]["concurrency"]:
                return {"status": "BUSY"}
            reservations = sum(r.get("event") == "NATIVE_EXECUTION_RESERVED" for r in rows(self.state / "events.jsonl"))
            if reservations >= self.contract["caps"]["executions"]:
                return {"status": "HALT_EXECUTION_LIMIT"}
            sessions = read(self.root / "runtime/worker_sessions.json")
            session = sessions.get(worker) or {}
            if not session.get("session_id") or not Path(session.get("transcript", "")).is_file():
                return {"status": "HALT_NO_NATIVE_SESSION"}
            # Unknown timed-out jobs are never sent to the model a second time.
            for path in sorted((self.q / "out").glob("*.request.json")):
                job = read(path)
                jid = str(job.get("job_id") or "")
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", jid):
                    continue
                target = self.q / "claims" / path.name
                try:
                    os.replace(path, target)
                except FileNotFoundError:
                    continue
                match = re.search(r'"slide_id"\s*:\s*"([^"]+)"', job.get("prompt", ""))
                sid = job.get("slide_id") or (match.group(1) if match else None)
                prompt = ("只返回一个完整JSON对象，勿使用Markdown；必须包含顶层键：" + "、".join(job.get("required_keys", []))
                          + ("；slide_id必须是" + sid if sid else "") + "。\n" + job.get("system_prompt", "") + "\n" + job.get("prompt", ""))
                prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
                write_json(active_path, {"job_id": jid, "slide_id": sid, "task_id": job.get("task_id"), "worker": worker,
                           "required_keys": job.get("required_keys", []), "claimed_at": current, "session": session,
                           "transcript_rows_at_claim": len(rows(session["transcript"])), "prompt_sha256": prompt_hash})
                write_json(self.q / "claims" / f"{jid}.claim.json", {"job_id": jid, "worker": worker, "claimed_at": current})
                self.event("NATIVE_EXECUTION_RESERVED", job_id=jid, slide_id=sid, worker=worker, prompt_sha256=prompt_hash)
                return {"status": "CLAIMED", "job_id": jid, "prompt": prompt}
            return {"status": "IDLE"}
        finally:
            lock.unlink(missing_ok=True)

    def sweep(self):
        recovered = []
        for active_path in self.active():
            active = read(active_path)
            session = active["session"]
            transcript = rows(session["transcript"])[int(active["transcript_rows_at_claim"]):]
            for row in reversed(transcript):
                message = row.get("message") or {}
                if row.get("type") != "assistant" or row.get("isSidechain"):
                    continue
                text = "\n".join(c.get("text", "") for c in message.get("content", []) if c.get("type") == "text").strip()
                payload = object_from_text(text)
                if payload is None and any(signal in text.lower() for signal in ("daily limit", "usage limit", "quota exhausted")):
                    (self.q / "STOP").write_text("NATIVE_QUOTA_EXHAUSTED\n", encoding="utf-8")
                    write_json(self.state / "BLOCKED_QUOTA.json", {"job_id": active["job_id"], "at": time.time()})
                    break
                if payload is None:
                    continue
                if active.get("slide_id") and payload.get("slide_id") != active["slide_id"]:
                    # An explicit different page may be a late answer; never bind it
                    # to this job. A recognizable answer missing its ID is instead a
                    # definite contract failure: report it so the slot can be reused.
                    if payload.get("slide_id") or not any(
                        key in payload for key in active["required_keys"] if key != "slide_id"
                    ):
                        continue
                jid = active["job_id"]
                model = message.get("model")
                expected = self.contract["model"]["native_model_key"]
                if model != expected:
                    write_json(self.state / "BLOCKED_MODEL.json", {"job_id": jid, "observed": model, "expected": expected})
                    (self.q / "STOP").write_text("MODEL_ROUTE_MISMATCH\n", encoding="utf-8")
                    break
                missing = [k for k in active["required_keys"] if k not in payload]
                if active.get("slide_id") and not payload.get("slide_id") and "slide_id" not in missing:
                    missing.append("slide_id")
                answered = time.time()
                provenance = {"native_chat_id": session["session_id"], "native_message_uuid": row.get("uuid"),
                              "native_reply_at": row.get("timestamp"), "model": model, "usage": message.get("usage"),
                              "worker": active["worker"], "prompt_sha256": active["prompt_sha256"],
                              "transcript_sha256": hashlib.sha256(Path(session["transcript"]).read_bytes()).hexdigest(),
                              "source": "native assistant JSON, unchanged; autonomous collector"}
                envelope = {"job_id": jid, "task_id": active.get("task_id"), "worker": active["worker"], "model": model,
                            "status": "ERROR" if missing else "OK", "body": text, "submitted_at": answered,
                            "answered_at": answered, "source_provenance": provenance}
                if missing:
                    envelope.update(error_code="OUTPUT_CONTRACT_ERROR", error="structured output missing keys: " + ",".join(missing))
                write_json(self.q / "answers" / f"{jid}.provenance.json", provenance)
                (self.q / "answers" / f"{jid}.answer").write_text(text, encoding="utf-8")
                write_json(self.q / "in" / f"{jid}.response.json", envelope)
                claimed = self.q / "claims" / f"{jid}.request.json"
                if claimed.exists():
                    os.replace(claimed, self.q / "done" / claimed.name)
                self.event("NATIVE_REPLY_RETURNED", job_id=jid, worker=active["worker"], model=model, invalid_keys=missing,
                           latency_seconds=round(answered - active["claimed_at"], 3), provenance=provenance,
                           model_turns=sum(r.get("type") == "assistant" and bool((r.get("message") or {}).get("model")) and not r.get("isSidechain") for r in transcript))
                active_path.unlink(missing_ok=True)
                recovered.append(jid)
                break
        write_json(self.state / "heartbeat.json", {"at": time.time(), "active": len(self.active()), "returned": recovered})
        return recovered

    def health(self, *, now=None):
        current = time.time() if now is None else now
        events = rows(self.state / "events.jsonl")
        last = max((r["at"] for r in events), default=float(self.contract.get("instance_created_epoch", self.contract["deadline_epoch"] - 5400)))
        waiting = any((self.q / "out").glob("*.request.json"))
        enqueues = sum(r.get("event") == "REQUEST_ENQUEUED" for r in rows(self.q / "ledger/calls.jsonl"))
        model_turns = sum(int(r.get("model_turns") or 0) for r in events if r.get("event") == "NATIVE_REPLY_RETURNED")
        for path in self.active():
            active = read(path)
            transcript = rows(active["session"]["transcript"])[int(active["transcript_rows_at_claim"]):]
            model_turns += sum(r.get("type") == "assistant" and bool((r.get("message") or {}).get("model")) and not r.get("isSidechain") for r in transcript)
        if model_turns >= self.contract["caps"].get("model_turns", 220):
            (self.q / "STOP").write_text("MODEL_TURN_LIMIT\n", encoding="utf-8")
        if enqueues > self.contract["caps"]["enqueues"]:
            (self.q / "STOP").write_text("ENQUEUE_LIMIT\n", encoding="utf-8")
        state = "DEADLINE" if current >= self.contract["deadline_epoch"] else "GENERATION_CUTOFF" if current >= self.contract["generation_stop_epoch"] else "STALLED" if (waiting or self.active()) and current - last >= 120 else "RUNNING"
        report = {"status": state, "at": current, "idle_seconds": round(current - last, 1), "active": len(self.active()),
                  "pending_dispatch": waiting, "enqueues": enqueues, "model_turns": model_turns, "model_invocation_capability": False}
        write_json(self.state / "health.json", report)
        return report
