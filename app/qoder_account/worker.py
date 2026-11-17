"""SDK subprocess boundary: only public SDK/CLI APIs; JSON events on stdout.

The host may run this with a separate Python environment. Never print credentials,
raw SDK stderr or reasoning blocks. No model request is made by inspect/connect.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    from .edition import edition_settings
except ImportError:
    from edition import edition_settings


def emit(event_name: str, **fields) -> None:
    print(json.dumps({"event": event_name, **fields}, ensure_ascii=False), flush=True)


def runtime_info() -> dict:
    brand = edition_settings()
    sdk = importlib.import_module(brand["module"])
    root = Path(sdk.__file__).resolve().parent
    cli = root / "_bundled" / (brand["cli"] + (".exe" if os.name == "nt" else ""))
    if not cli.is_file():
        raise RuntimeError("QODER_RUNTIME_MISSING")
    return {"sdk_version": importlib.metadata.version(brand["distribution"]), "cli_path": str(cli),
        "edition": brand["id"], "product_label": brand["label"], "sdk_distribution": brand["distribution"]}


def auth_status(cli: str) -> dict:
    completed = subprocess.run([cli, "status", "--output", "json"], capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        row = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError):
        raise RuntimeError("QODER_STATUS_UNAVAILABLE") from None
    return {"logged_in": row.get("logged_in") is True, "cli_version": str(row.get("version") or "unknown")}


def quota_view(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    # Account identifiers and URLs are deliberately excluded from the product UI.
    return {key: value[key] for key in ("userType", "totalUsagePercentage", "isQuotaExceeded",
        "userQuota", "addOnQuota", "session") if key in value}


async def run(mode: str, payload: dict) -> None:
    brand = edition_settings()
    sdk = importlib.import_module(brand["module"])
    (QoderAgentOptions, QoderSDKClient, qodercli_auth, PermissionResultDeny, AssistantMessage,
        ResultMessage, StreamEvent, TextBlock, SystemMessage) = (getattr(sdk, name) for name in (
        "QoderAgentOptions", "QoderSDKClient", "qodercli_auth", "PermissionResultDeny", "AssistantMessage",
        "ResultMessage", "StreamEvent", "TextBlock", "SystemMessage"))
    info = runtime_info()
    status = auth_status(info["cli_path"])
    if not status["logged_in"]:
        emit("connection", status="authentication_required", **status,
            sdk_version=info["sdk_version"], edition=brand["id"], model_requests=0)
        return

    async def deny_tool(name, tool_input, context):
        emit('permission_denied',tool_name=str(name)[:100],message='当前内容通道没有授予此工具权限，未执行该操作。')
        return PermissionResultDeny(message="本通道只生成文字，不执行工具。")

    options = QoderAgentOptions(auth=qodercli_auth(), cli_path=info["cli_path"],
        cwd=os.environ["MMF_QODER_ACCOUNT_WORKSPACE"], tools=[], allowed_tools=[],
        can_use_tool=deny_tool, permission_mode="default", setting_sources=[], skills=[],
        strict_mcp_config=True, mcp_servers={}, include_partial_messages=True,
        max_turns=1, control_request_timeout_ms=20000,
        stderr=lambda _: None, on_auth_expired=lambda: emit("auth_expired"),
        env={brand["config_env"]: os.environ[brand["config_env"]]},
        extra_args={"max-model-request-retries": "0"})
    if mode == "execute":
        if payload.get('native_session_id'):
            options.resume=str(payload['native_session_id'])
        options.model = str(payload.get("model") or "")
        if not options.model:
            raise RuntimeError("QODER_MODEL_REQUIRED")
        options.system_prompt = str(payload.get("system_prompt") or "")
        default_output = 32000 if payload.get("generation_mode") == "tender_requirement_recognition" else 8000
        options.extra_args["max-output-tokens"] = str(min(32000, max(256, int(payload.get("max_tokens") or default_output))))
    async with QoderSDKClient(options) as client:
        if mode == "connect":
            models = await client.get_available_models()
            models = [{k: row[k] for k in ("value", "displayName", "isEnabled", "isFree", "priceFactor") if k in row}
                for row in models if isinstance(row, dict) and row.get("value")]
            try:
                quota = quota_view(await client.get_usage_info())
            except Exception:
                quota = None
            emit("connection", status="ready", logged_in=True, sdk_version=info["sdk_version"],
                cli_version=status["cli_version"], models=models, usage=quota,
                usage_availability="available" if quota is not None else "NOT_AVAILABLE", edition=brand["id"], model_requests=0)
            return
        emit("session_started", session_id=client.session_id)
        await client.query(str(payload["prompt"]))
        final = None
        parts: list[str] = []
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                texts = [block.text for block in message.content if isinstance(block, TextBlock)]
                parts.extend(texts)
                emit("activity", phase="generating", text_chars=sum(len(t) for t in texts))
            elif isinstance(message, StreamEvent):
                # Only event kind/character count; no raw thinking or full context in frontend.
                delta = message.event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    emit("activity", phase="generating", text_chars=len(str(delta.get("text") or "")))
            elif isinstance(message, SystemMessage):
                sid=(getattr(message,'data',{}) or {}).get('session_id')
                if isinstance(sid,str) and len(sid)<=160:emit('session_started',session_id=sid)
                emit("activity", phase="session", kind=message.subtype)
            elif isinstance(message, ResultMessage):
                final = message
                break
        if final is None:
            raise RuntimeError("QODER_RESPONSE_UNKNOWN")
        body = final.result or "\n".join(parts)
        emit("result", body=body, is_error=final.is_error, subtype=final.subtype,
            max_output_tokens=int(options.extra_args["max-output-tokens"]),
            session_id=final.session_id, num_turns=final.num_turns, stop_reason=final.stop_reason,
            usage=final.usage, model_usage=final.model_usage, total_credits=final.total_credits,
            total_cost_usd=final.total_cost_usd)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    mode = sys.argv[1]
    try:
        if mode == "inspect":
            emit("runtime", **runtime_info())
            return
        payload = json.load(sys.stdin) if mode == "execute" else {}
        asyncio.run(run(mode, payload))
    except Exception as exc:
        code = str(exc) if str(exc).startswith("QODER_") else type(exc).__name__
        emit("error", code=code, message="Qoder连接或执行未完成，请查看连接状态后处理。")
        sys.exit(1)


if __name__ == "__main__":
    main()
