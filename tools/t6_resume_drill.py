"""MMF-007 恢复演练：长文生成中途杀掉服务进程，重启后 Resume 续跑。

--phase prepare : 上传→识别→确认→启动生成→等待 N 秒→快照 checkpoint→杀服务→退出
--phase resume  : （服务已重启）再次 /api/generate→轮询到终态→输出连续性核对 JSON
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"


def post(port: int, path: str, payload: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode("utf-8"), method="POST", headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))


def get(port: int, path: str, timeout: int = 60) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wait_tender(port: int, run_id: str, timeout: int = 900) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(8)
        status = get(port, f"/api/tender/runs/{run_id}/status").get("status") or {}
        if status.get("pack_status") in {"awaiting_todd_confirmation", "ready_for_plan", "blocked_clarification", "understanding_failed"}:
            return status
        log(f"tender: {status.get('stage')} {status.get('pack_status')}")
    raise RuntimeError("tender timeout")


def kill_server() -> None:
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    pids = set()
    for line in out.splitlines():
        if ":3055" in line and "LISTENING" in line:
            pids.add(line.split()[-1])
    for pid in pids:
        log(f"killing server pid={pid}")
        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, text=True)


def checkpoint_snapshot(run_id: str) -> dict:
    path = RUNS / run_id / "checkpoint" / "state.json"
    ck = json.loads(path.read_text(encoding="utf-8"))
    return {
        "status": ck.get("status"),
        "completed_sections": sorted(ck.get("completed_sections") or []),
        "failed_batches": ck.get("failed_batches") or [],
        "provider_calls_total": ck.get("provider_calls_total"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=3055)
    parser.add_argument("--file", required=True)
    parser.add_argument("--provider", default="kimi_moonshot")
    parser.add_argument("--phase", choices=["prepare", "resume"], required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--kill-after", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()

    if args.phase == "prepare":
        sys.path.insert(0, str(ROOT / "tools"))
        from t6_run_driver import upload_tender

        up = upload_tender(args.port, args.file, args.provider)
        run_id = up["run_id"]
        log(f"run_id={run_id}")
        post(args.port, "/api/tender/process", {"run_id": run_id, "provider_name": args.provider})
        status = wait_tender(args.port, run_id)
        log(f"tender ready: {status.get('pack_status')}")
        post(args.port, "/api/tender/confirm", {"run_id": run_id, "decisions": {"accept_remaining": True}, "brief_options": {"scenario": "完整物业服务方案", "medium": "WORD", "provider_name": args.provider}})
        gen = post(args.port, "/api/generate", {"run_id": run_id, "selected_positive_ids": [], "clarification_answers": {}, "speed_profile": "fast"})
        log(f"generate: {gen.get('status')}; 等待 {args.kill_after}s 后杀服务")
        time.sleep(args.kill_after)
        before = checkpoint_snapshot(run_id)
        log(f"before-kill: sections={len(before['completed_sections'])} calls={before['provider_calls_total']}")
        kill_server()
        print("DRILL_JSON=" + json.dumps({"run_id": run_id, "before": before}, ensure_ascii=False))
        return 0

    run_id = args.run_id
    gen = post(args.port, "/api/generate", {"run_id": run_id, "selected_positive_ids": [], "clarification_answers": {}, "speed_profile": "fast"})
    log(f"resume generate: {gen.get('status')}")
    deadline = time.monotonic() + args.timeout
    result = {}
    while time.monotonic() < deadline:
        time.sleep(20)
        try:
            result = get(args.port, f"/api/runs/{run_id}/status")
        except Exception as exc:
            log(f"poll error: {exc}")
            continue
        status = result.get("run_status") or result.get("status")
        log(f"gen: {status}")
        if status in {"generation_completed", "failed", "cancelled", "generation_failed"}:
            break
    after = checkpoint_snapshot(run_id)
    audit = {}
    audit_path = RUNS / run_id / "run_audit.json"
    if audit_path.is_file():
        a = json.loads(audit_path.read_text(encoding="utf-8"))
        audit = {k: a.get(k) for k in ("delivery_status", "final_artifact_qa_status", "total_processing_seconds", "provider_calls_total", "this_resume_calls", "review_count")}
    print("DRILL_JSON=" + json.dumps({"run_id": run_id, "after": after, "audit": audit, "sections_after_minus_before": sorted(set(after["completed_sections"]) - set(json.loads(sys.argv[2])["completed_sections"])) if len(sys.argv) > 2 else None}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
