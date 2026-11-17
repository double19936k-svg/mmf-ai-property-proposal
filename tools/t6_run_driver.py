"""MMF-007 长任务驱动脚本：从需求文件到成品的全流程 HTTP 驱动。

用法（示例）：
  runtime/dev_python/Scripts/python.exe tools/t6_run_driver.py \
      --port 3055 --provider qwen_modelstudio --speed fast --medium WORD \
      --label T6-L1-hisense --file "C:/path/tender.docx" [--timeout 5400]

输出：进度写 stdout，结束时打印一行 JSON 摘要（run_id / status / 耗时 / 审计关键字段）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
import urllib.request


def http_json(port: int, method: str, path: str, payload: dict | None = None, timeout: int = 120) -> dict:
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def upload_tender(port: int, file_path: str, provider: str) -> dict:
    boundary = f"----mmf{uuid.uuid4().hex}"
    filename = file_path.replace("\\", "/").rsplit("/", 1)[-1]
    body = bytearray()
    body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"provider_name\"\r\n\r\n{provider}\r\n".encode("utf-8"))
    with open(file_path, "rb") as fh:
        content = fh.read()
    body.extend(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{filename}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
    )
    body.extend(content)
    body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/tender/upload", data=bytes(body), method="POST"
    )
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8"))


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--provider", default="qwen_modelstudio")
    parser.add_argument("--speed", default="fast")
    parser.add_argument("--medium", default="WORD")
    parser.add_argument("--scenario", default="完整物业服务方案")
    parser.add_argument("--label", default="run")
    parser.add_argument("--timeout", type=int, default=5400)
    args = parser.parse_args()

    started = time.monotonic()
    upload = upload_tender(args.port, args.file, args.provider)
    run_id = upload["run_id"]
    log(f"{args.label}: run_id={run_id}")

    kicked = http_json(args.port, "POST", "/api/tender/process", {"run_id": run_id, "provider_name": args.provider})
    log(f"process: {kicked.get('status')}")

    tender = {}
    while time.monotonic() - started < args.timeout:
        time.sleep(10)
        tender = http_json(args.port, "GET", f"/api/tender/runs/{run_id}/status").get("status") or {}
        stage = tender.get("stage")
        pack_status = tender.get("pack_status")
        if pack_status in {"ready_for_plan", "awaiting_todd_confirmation", "blocked_clarification"}:
            break
        log(f"tender: stage={stage} pack={pack_status}")
    else:
        print(json.dumps({"label": args.label, "run_id": run_id, "status": "TENDER_TIMEOUT", "tender": tender}, ensure_ascii=False))
        return 2
    log(f"tender done: pack={tender.get('pack_status')} stage={tender.get('stage')}")

    confirmed = http_json(args.port, "POST", "/api/tender/confirm", {
        "run_id": run_id,
        "decisions": {"accept_remaining": True},
        "brief_options": {"scenario": args.scenario, "medium": args.medium, "provider_name": args.provider},
    })
    if confirmed.get("status") == "blocked_clarification":
        print(json.dumps({"label": args.label, "run_id": run_id, "status": "BLOCKED_CLARIFICATION"}, ensure_ascii=False))
        return 3
    log(f"confirmed: knowledge={confirmed.get('status', 'ok')}")

    gen = http_json(args.port, "POST", "/api/generate", {
        "run_id": run_id,
        "selected_positive_ids": [],
        "clarification_answers": {},
        "speed_profile": args.speed,
    })
    log(f"generate: {gen.get('status')}")

    result = {}
    while time.monotonic() - started < args.timeout:
        time.sleep(20)
        try:
            result = http_json(args.port, "GET", f"/api/runs/{run_id}/status")
        except Exception as exc:  # transient
            log(f"status poll error: {exc}")
            continue
        status = result.get("run_status") or result.get("status")
        stage = (result.get("result") or {}).get("current_active_stage") or result.get("stage")
        log(f"gen: status={status} stage={stage}")
        if status in {"generation_completed", "completed", "failed", "generation_failed", "cancelled"}:
            break
    else:
        print(json.dumps({"label": args.label, "run_id": run_id, "status": "GEN_TIMEOUT"}, ensure_ascii=False))
        return 4

    audit = {}
    for candidate in ("run_audit.json",):
        pass
    elapsed = round(time.monotonic() - started, 1)
    summary = {
        "label": args.label,
        "run_id": run_id,
        "status": result.get("status"),
        "wall_seconds": elapsed,
        "result_status": (result.get("result") or {}).get("status"),
        "delivery_status": (result.get("result") or {}).get("delivery_status"),
    }
    print("SUMMARY_JSON=" + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
