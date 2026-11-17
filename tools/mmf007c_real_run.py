"""MMF-007C 阶段8：真实招标文件 300-Class 试炼驱动器（只走 Qoder 内部额度队列）。

与 t6_run_driver.py 的区别：
- 全程 provider_name=qoder_internal，进程外没有任何网络出口（配合 MMF_PROVIDER_NETWORK_LOCKDOWN）；
- 心跳按 public_phase/进度/修复预算打印，不再出现"静止几小时"的观感；
- 结束时把 §44-46 审计口径（分阶段调用数与耗时、终态、页数、成品路径）汇总成 JSON。

用法：
  runtime/dev_python/Scripts/python.exe tools/mmf007c_real_run.py \
      --port 3042 --provider qoder_internal --speed fast --parallelism 2 \
      --file "<Run 目录内的招标文件测试副本>.docx" --label MMF007C-REAL-300 --timeout 21600
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def http_json(port: int, method: str, path: str, payload: dict | None = None, timeout: int = 120) -> dict:
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def upload(port: int, file_path: str, provider: str) -> dict:
    boundary = f"----mmf{uuid.uuid4().hex}"
    filename = file_path.replace("\\", "/").rsplit("/", 1)[-1]
    body = bytearray()
    body.extend(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"provider_name\"\r\n\r\n{provider}\r\n".encode("utf-8")
    )
    body.extend(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{filename}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
    )
    body.extend(Path(file_path).read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/tender/upload", data=bytes(body), method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read().decode("utf-8"))


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def provider_posture(port: int) -> dict:
    """只记录"是否配置"，绝不记录任何 Key 内容。"""
    rows = http_json(port, "GET", "/api/providers").get("providers") or []
    return {
        str(row.get("provider_name")): {
            "configured": bool(row.get("configured")),
            "available": bool(row.get("available")),
            "type": row.get("provider_type"),
            "test_mode": bool(row.get("test_mode")),
        }
        for row in rows
    }


def read_run_file(runs_dir: Path, run_id: str, name: str) -> dict:
    path = runs_dir / run_id / name
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=3042)
    parser.add_argument("--file", required=True)
    parser.add_argument("--provider", default="qoder_internal")
    parser.add_argument("--speed", default="fast")
    parser.add_argument("--parallelism", type=int, default=2)
    parser.add_argument("--medium", default="WORD")
    parser.add_argument("--scenario", default="完整物业服务方案")
    parser.add_argument(
        "--document-scale",
        default="regular_solution",
        choices=["regular_solution", "detailed_technical_solution", "large_technical_tender"],
        help="与 UI 的“文档规模/方案深度”同义；300-Class 试炼必须显式传 large_technical_tender",
    )
    parser.add_argument("--label", default="MMF007C-REAL")
    parser.add_argument("--timeout", type=int, default=21600)
    parser.add_argument("--runs-dir", default="", help="用于读取审计文件；缺省按 dist/源侧 runs 目录猜测")
    args = parser.parse_args()

    runs_dir = Path(args.runs_dir) if args.runs_dir else Path.cwd() / "runs"
    started = time.monotonic()
    health = http_json(args.port, "GET", "/api/health", timeout=20)
    log(f"health: {health.get('status')} version={health.get('version')}")
    log(f"providers: {json.dumps(provider_posture(args.port), ensure_ascii=False)}")

    uploaded = upload(args.port, args.file, args.provider)
    run_id = uploaded["run_id"]
    log(f"uploaded run_id={run_id}")

    http_json(args.port, "POST", "/api/tender/process", {"run_id": run_id, "provider_name": args.provider})
    pack: dict = {}
    while time.monotonic() - started < args.timeout:
        time.sleep(15)
        pack = (http_json(args.port, "GET", f"/api/tender/runs/{run_id}/status").get("status") or {})
        state = pack.get("pack_status")
        log(f"tender: stage={pack.get('stage')} pack={state} reqs={pack.get('requirement_count')}")
        if state in {
            "ready_for_plan", "awaiting_todd_confirmation", "blocked_clarification",
            "partial_degraded", "understanding_failed", "cancelled", "failed",
        }:
            break
    if pack.get("pack_status") not in {"ready_for_plan", "awaiting_todd_confirmation", "partial_degraded"}:
        print("SUMMARY_JSON=" + json.dumps({"label": args.label, "run_id": run_id, "status": "TENDER_NOT_READY", "pack": pack}, ensure_ascii=False))
        return 2

    confirmed: dict = {}
    fallback_acknowledged = False
    for attempt in range(2):
        try:
            confirmed = http_json(args.port, "POST", "/api/tender/confirm", {
                "run_id": run_id,
                "decisions": {"accept_remaining": True},
                "brief_options": {
                    "scenario": args.scenario,
                    "medium": args.medium,
                    "provider_name": args.provider,
                    "speed_profile": args.speed,
                    "document_scale": args.document_scale,
                },
            }, timeout=3600)
            break
        except urllib.error.HTTPError as exc:
            # 内部额度识别降级为本地解析时，确认前必须显式承认；这里承认一次并重试，
            # 并把降级事实留在 SUMMARY_JSON 里，不做静默绕过。
            detail = exc.read().decode("utf-8", "replace") if attempt == 0 else ""
            if attempt == 0 and "本地解析" in detail:
                fallback_acknowledged = True
                log("AI 需求识别降级为本地解析：显式确认继续，然后重试确认（已在报告中记录）")
                http_json(args.port, "POST", "/api/tender/accept-local-fallback", {"run_id": run_id})
                continue
            print("SUMMARY_JSON=" + json.dumps({
                "label": args.label, "run_id": run_id, "status": "CONFIRM_FAILED", "detail": detail[:600],
            }, ensure_ascii=False))
            return 4
    log(f"confirmed: {confirmed.get('status')}")
    if confirmed.get("status") == "blocked_clarification":
        # accept_remaining 仍无法放行：必须人工回答，不得静默继续生成。
        print("SUMMARY_JSON=" + json.dumps({
            "label": args.label, "run_id": run_id, "status": "CONFIRMATION_BLOCKED",
            "message": confirmed.get("message"),
        }, ensure_ascii=False))
        return 3

    gen = http_json(args.port, "POST", "/api/generate", {
        "run_id": run_id,
        "selected_positive_ids": [],
        "clarification_answers": {},
        "speed_profile": args.speed,
        "max_parallelism": args.parallelism,
        "trust_submitted_selection": True,
    })
    log(f"generate kicked: {gen.get('status')} parallelism={args.parallelism} speed={args.speed} scale={args.document_scale}")

    last_line = ""
    final: dict = {}
    while time.monotonic() - started < args.timeout:
        time.sleep(20)
        try:
            final = http_json(args.port, "GET", f"/api/runs/{run_id}/status", timeout=60)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log(f"status poll error: {type(exc).__name__}")
            continue
        record = read_run_file(runs_dir, run_id, "generation_status.json")
        heartbeat = record.get("message") or record.get("public_phase") or ""
        line = f"status={final.get('status') or final.get('run_status')} phase={record.get('public_phase')} {heartbeat}"
        if line != last_line:
            log(line)
            last_line = line
        state = str(final.get("status") or final.get("run_status") or "")
        if state in {"completed", "generation_completed", "failed", "cancelled", "generation_failed", "needs_review", "technical_failed"}:
            break

    audit = read_run_file(runs_dir, run_id, "run_audit.json")
    summary = {
        "label": args.label,
        "run_id": run_id,
        "document_scale": args.document_scale,
        "tender_local_fallback_acknowledged": fallback_acknowledged,
        "wall_seconds": round(time.monotonic() - started, 1),
        "final_status": final.get("status") or final.get("run_status"),
        "public_phase": read_run_file(runs_dir, run_id, "generation_status.json").get("public_phase"),
        "result_status": (final.get("result") or {}).get("status"),
        "terminal_state": (final.get("result") or {}).get("terminal_state") or audit.get("terminal_state"),
        "delivery_status": (final.get("result") or {}).get("delivery_status"),
        "audit_keys": {key: value for key, value in audit.items() if "call" in key or "seconds" in key or "duration" in key or "state" in key},
        "artifact": (final.get("result") or {}).get("artifact_path") or audit.get("artifact_path"),
    }
    print("SUMMARY_JSON=" + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
