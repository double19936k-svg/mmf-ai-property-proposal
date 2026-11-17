"""Qoder 内部模型队列的取单/回单工具（MMF-007C 阶段7）。

队列另一端是 Qoder 内部 Qwen3.8-Flash 执行者（Agent 会话），不是任何外部付费 API。
本工具只做文件搬运与账本统计，自身不产生模型调用。

用法（--queue 缺省时读环境变量 MMF_QODER_INTERNAL_QUEUE_DIR）：
    python tools/qoder_bridge.py stats
    python tools/qoder_bridge.py pending --limit 6
    python tools/qoder_bridge.py claim <job_id> --worker qoder-a
    python tools/qoder_bridge.py show <job_id> --part prompt
    python tools/qoder_bridge.py submit <job_id> --worker qoder-a --file answer.txt
    python tools/qoder_bridge.py stop / resume
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
DIRS = ("out", "in", "claims", "done", "abandoned", "ledger")
MODEL_LABEL = "qwen3.8-flash (Qoder internal free quota)"


def _queue_root(args: argparse.Namespace) -> Path:
    raw = str(args.queue or os.environ.get("MMF_QODER_INTERNAL_QUEUE_DIR") or "").strip()
    if not raw:
        raise SystemExit("缺少队列目录：传 --queue 或设置 MMF_QODER_INTERNAL_QUEUE_DIR")
    root = Path(raw)
    if args.create:
        for name in DIRS:
            (root / name).mkdir(parents=True, exist_ok=True)
    return root


def _atomic_write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def _job_file(root: Path, job_id: str) -> Path | None:
    for folder in ("out", "claims", "done", "abandoned"):
        for path in (root / folder).glob(f"{job_id}*.request.json"):
            return path
    return None


def _brief(row: dict) -> dict:
    return {
        "job_id": row.get("job_id"),
        "task_id": row.get("task_id"),
        "attempt_mode": row.get("attempt_mode"),
        "generation_mode": row.get("generation_mode"),
        "batch_id": row.get("batch_id"),
        "section_ids": row.get("section_ids"),
        "content_unit_ids": row.get("content_unit_ids"),
        "response_protocol": row.get("response_protocol"),
        "required_keys": row.get("required_keys"),
        "prompt_chars": len(str(row.get("prompt") or "")),
        "system_chars": len(str(row.get("system_prompt") or "")),
        "age_seconds": round(time.time() - float(row.get("created_at") or time.time()), 1),
    }


def cmd_pending(root: Path, args: argparse.Namespace) -> int:
    rows = []
    for path in sorted((root / "out").glob("*.request.json")):
        row = _load(path)
        if row:
            rows.append(_brief(row))
    for row in rows[: args.limit] if args.limit else rows:
        print(json.dumps(row, ensure_ascii=False))
    print(f"pending={len(rows)}", file=sys.stderr)
    return 0


def cmd_claim(root: Path, args: argparse.Namespace) -> int:
    path = root / "out" / f"{args.job_id}.request.json"
    if not path.is_file():
        raise SystemExit(f"未找到待取请求：{args.job_id}")
    target = root / "claims" / f"{args.job_id}.request.json"
    os.replace(path, target)
    _atomic_write(root / "claims" / f"{args.job_id}.claim.json", {"job_id": args.job_id, "worker": args.worker, "claimed_at": time.time()})
    print(json.dumps({"claimed": args.job_id, "worker": args.worker}, ensure_ascii=False))
    return 0


def cmd_show(root: Path, args: argparse.Namespace) -> int:
    path = _job_file(root, args.job_id)
    if path is None:
        raise SystemExit(f"未找到请求：{args.job_id}")
    row = _load(path)
    text = str(row.get("system_prompt") or "") if args.part == "system" else str(row.get("prompt") or "")
    if args.part == "all":
        text = f"[SYSTEM]\n{row.get('system_prompt') or ''}\n\n[USER]\n{row.get('prompt') or ''}"
    if args.max_chars and len(text) > args.max_chars:
        text = text[: args.max_chars] + f"\n…[truncated {len(text) - args.max_chars} chars]"
    print(text)
    return 0


def cmd_submit(root: Path, args: argparse.Namespace) -> int:
    raw = Path(args.file).read_text(encoding="utf-8-sig") if args.file else sys.stdin.read()
    body = raw
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        pass
    path = _job_file(root, args.job_id)
    if path is None:
        # 请求已被判超时/取消；迟到响应必须留痕，不得静默丢弃。
        _atomic_write(root / "late" / f"{args.job_id}.response.json", {"job_id": args.job_id, "worker": args.worker, "status": "LATE"})
        print(json.dumps({"submitted": False, "reason": "request_no_longer_pending", "job_id": args.job_id}, ensure_ascii=False))
        return 2
    request = _load(path)
    _atomic_write(root / "in" / f"{args.job_id}.response.json", {
        "job_id": args.job_id,
        "task_id": request.get("task_id"),
        "worker": args.worker,
        "model": args.model or MODEL_LABEL,
        "status": "OK",
        "body": body,
        "submitted_at": time.time(),
    })
    _atomic_write(root / "ledger" / f"{args.job_id}.submit.json", {"job_id": args.job_id, "worker": args.worker, "response_chars": len(raw)})
    os.replace(path, root / "done" / path.name)
    print(json.dumps({"submitted": True, "job_id": args.job_id, "response_chars": len(raw)}, ensure_ascii=False))
    return 0


def cmd_stats(root: Path, args: argparse.Namespace) -> int:
    ledger = root / "ledger" / "calls.jsonl"
    events: dict[str, int] = {}
    workers: dict[str, int] = {}
    waits = []
    if ledger.is_file():
        for line in ledger.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            events[str(row.get("event"))] = events.get(str(row.get("event")), 0) + 1
            if row.get("event") == "RESPONSE_RECEIVED":
                worker = str(row.get("worker") or "unknown")
                workers[worker] = workers.get(worker, 0) + 1
                if isinstance(row.get("wait_seconds"), (int, float)):
                    waits.append(float(row["wait_seconds"]))
    pending = len(list((root / "out").glob("*.request.json"))) if (root / "out").is_dir() else 0
    claimed = len(list((root / "claims").glob("*.request.json"))) if (root / "claims").is_dir() else 0
    payload = {
        "queue_root": str(root),
        "pending": pending,
        "claimed_unanswered": claimed,
        "answered": len(list((root / "done").glob("*.request.json"))) if (root / "done").is_dir() else 0,
        "abandoned": len(list((root / "abandoned").glob("*.request.json"))) if (root / "abandoned").is_dir() else 0,
        "late_responses": len(list((root / "late").glob("*.json"))) if (root / "late").is_dir() else 0,
        "qcode_internal_qwen_calls": events.get("RESPONSE_RECEIVED", 0),
        "enqueued": events.get("REQUEST_ENQUEUED", 0),
        "timeouts": events.get("REQUEST_TIMED_OUT", 0),
        "cancelled": events.get("REQUEST_CANCELLED", 0),
        "worker_errors": events.get("WORKER_ERROR", 0),
        "events": events,
        "workers": workers,
        "mean_wait_seconds": round(sum(waits) / len(waits), 2) if waits else None,
        "stop_sentinel": (root / "STOP").is_file(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_stop(root: Path, args: argparse.Namespace) -> int:
    _atomic_write(root / "STOP", {"stopped_at": time.time()})
    print(json.dumps({"stop": True}, ensure_ascii=False))
    return 0


def cmd_resume(root: Path, args: argparse.Namespace) -> int:
    try:
        (root / "STOP").unlink()
    except OSError:
        pass
    print(json.dumps({"stop": False}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Qoder 内部模型文件队列工具")
    parser.add_argument("--queue", default="")
    parser.add_argument("--create", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pending"); p.add_argument("--limit", type=int, default=0)
    p = sub.add_parser("claim"); p.add_argument("job_id"); p.add_argument("--worker", required=True)
    p = sub.add_parser("show"); p.add_argument("job_id"); p.add_argument("--part", choices=("prompt", "system", "all"), default="all"); p.add_argument("--max-chars", type=int, default=0)
    p = sub.add_parser("submit"); p.add_argument("job_id"); p.add_argument("--worker", required=True); p.add_argument("--file", default=""); p.add_argument("--model", default="")
    sub.add_parser("stats")
    sub.add_parser("stop")
    sub.add_parser("resume")
    args = parser.parse_args(argv)
    root = _queue_root(args)
    handler = {
        "pending": cmd_pending,
        "claim": cmd_claim,
        "show": cmd_show,
        "submit": cmd_submit,
        "stats": cmd_stats,
        "stop": cmd_stop,
        "resume": cmd_resume,
    }[args.command]
    return handler(root, args)


if __name__ == "__main__":
    sys.exit(main())
