from __future__ import annotations

import importlib.util
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .base import (
    AIProvider,
    ProviderError,
    ProviderUnavailableError,
    normalize_generation,
    normalize_recommendation,
    parse_json_object,
    write_json,
)
from .execution_reliability import classify_provider_failure


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class GrokBuildProvider(AIProvider):
    provider_version = "0.2-bridge"

    def _bridge_paths(self) -> tuple[Path, Path]:
        from paths import PACKAGE_ROOT
        default_dir = PACKAGE_ROOT / "providers" / "grok_bridge"
        bridge_file = Path(os.path.expandvars(str(self.config.get("bridge_path", default_dir / "grok_bridge.py")))).expanduser()
        config_file = Path(os.path.expandvars(str(self.config.get("bridge_config", bridge_file.parent / "bridge_config.json")))).expanduser()
        if not config_file.is_file():
            example = bridge_file.parent / "bridge_config.example.json"
            if example.is_file():
                config_file = example
        return bridge_file.resolve(), config_file.resolve()

    def _bridge(self):
        bridge_file, config_file = self._bridge_paths()
        if not bridge_file.is_file() or not config_file.is_file():
            raise ProviderUnavailableError("Grok Build仅作为Developer/Local Advanced Provider；当前机器未安装正式Bridge。")
        spec = importlib.util.spec_from_file_location("property_ai_grok_bridge", bridge_file)
        if spec is None or spec.loader is None:
            raise ProviderUnavailableError("无法加载Grok Bridge。")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.GrokBridge(config_file)

    def health_check(self) -> dict[str, Any]:
        base = {"configured": bool(self.config.get("enabled", False)), "metadata": self.get_metadata()}
        if not base["configured"]:
            return {**base, "available": False, "status": "not_configured", "message": "未启用"}
        grok_exe = Path.home() / ".grok" / "bin" / "grok.exe"
        if not shutil.which(str(self.config.get("command") or "grok")) and not grok_exe.is_file():
            return {**base, "available": False, "status": "not_installed", "installed": False, "message": "未检测到本机Grok CLI"}
        if not bool(self.config.get("live_health")):
            return {**base, "available": False, "status": "installed", "installed": True, "message": "本机Grok CLI已安装，请点测试连接确认登录"}
        try:
            status = self._bridge().health_check()
        except Exception as exc:
            code = str(getattr(exc, "code", "unavailable"))
            return {**base, "available": False, "status": code.lower(), "message": f"{code}: {exc}"}
        return {
            **base,
            "available": bool(status.get("available")),
            "status": "available" if status.get("available") else str(status.get("status", "unavailable")).lower(),
            "message": "可用" if status.get("available") else str(status.get("message", "Grok Bridge不可用")),
            "bridge_health": status,
        }

    def preflight_auth_probe(self) -> dict[str, Any]:
        """Read-only probe. It never sends business content or changes auth state."""
        health = self.health_check()
        bridge = health.get("bridge_health", {})
        if health.get("available") and bridge.get("authenticated") is True:
            status = "AUTHENTICATED"
        elif bridge.get("authenticated") is False:
            status = "PROVIDER_AUTH_REQUIRED"
        elif health.get("status") in {"network_error", "proxy_error"}:
            status = "NETWORK_ERROR"
        elif health.get("status") in {"cli_not_found"}:
            status = "CLI_NOT_FOUND"
        elif health.get("status") in {"model_unavailable"}:
            status = "MODEL_UNAVAILABLE"
        else:
            status = "AUTH_STATE_UNKNOWN"
        return {"status": status, "authenticated": bridge.get("authenticated"), "health": health}

    def _invoke(self, request: dict[str, Any], task_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        task_id = request["task_id"]
        schema = request.get("json_schema")
        try:
            bridge_result = self._bridge().invoke(
                task_id=task_id,
                prompt=request["prompt"],
                system_prompt=request["system_prompt"],
                working_directory=task_dir.parent,
                run_dir=task_dir,
                timeout_seconds=int(self.config.get("timeout", 1800)),
                max_network_retries=2,
                agent_max_turns=int(request.get("agent_max_turns", 1)),
                tools=request.get("tools"),
                json_schema=schema,
                reasoning_effort=request.get("reasoning_effort"),
            )
            result = bridge_result["structured_output"]
            if not isinstance(result, dict) or not result:
                raise ProviderError("Grok返回空结果，正在按无约束JSON重试。")
        except Exception as first_exc:
            if schema is None:
                exc = first_exc
                code = str(getattr(exc, "code", ""))
                probe = self.preflight_auth_probe() if code in {"AUTH_REQUIRED", "PROVIDER_AUTH_REQUIRED", "AUTH_STATE_UNKNOWN"} else None
                classified = classify_provider_failure(code, str(exc), auth_probe=(probe or {}).get("health", {}).get("bridge_health", probe))
                raise ProviderUnavailableError(f"Grok Bridge调用失败[{classified}]：{exc}", error_code=classified) from exc
            try:
                bridge_result = self._bridge().invoke(
                    task_id=f"{task_id}-plain",
                    prompt=request["prompt"] + "\n\n只输出一个完整JSON对象，根字段必须包含所需业务字段，不要输出空对象，不要附加<|eos|>或其他标记。",
                    system_prompt=request["system_prompt"],
                    working_directory=task_dir.parent,
                    run_dir=task_dir,
                    timeout_seconds=int(self.config.get("timeout", 1800)),
                    max_network_retries=2,
                    agent_max_turns=int(request.get("agent_max_turns", 1)),
                    tools=request.get("tools"),
                    json_schema=None,
                    reasoning_effort=request.get("reasoning_effort"),
                )
                result = bridge_result["structured_output"]
            except Exception as exc:
                code = str(getattr(exc, "code", ""))
                probe = self.preflight_auth_probe() if code in {"AUTH_REQUIRED", "PROVIDER_AUTH_REQUIRED", "AUTH_STATE_UNKNOWN"} else None
                classified = classify_provider_failure(code, str(exc), auth_probe=(probe or {}).get("health", {}).get("bridge_health", probe))
                raise ProviderUnavailableError(f"Grok Bridge调用失败[{classified}]：{exc}", error_code=classified) from exc
        audit = bridge_result["audit"]
        metadata = self._task_metadata(
            task_id,
            session_id=audit.get("session_id"),
            request_id=audit.get("request_id"),
            serialization_repair=audit.get("serialization_repair", {"applied": False}),
            started_at=audit.get("started_at"),
            finished_at=audit.get("finished_at"),
            bridge_used=True,
            bridge_attempts=audit.get("attempts", []),
        )
        write_json(task_dir / "provider_audit.json", metadata)
        return result, metadata

    def recommend_knowledge(self, request: dict[str, Any], task_dir: Path) -> dict[str, Any]:
        result, metadata = self._invoke(request, task_dir)
        return normalize_recommendation(result, metadata)

    def generate_solution(self, request: dict[str, Any], task_dir: Path) -> dict[str, Any]:
        result, metadata = self._invoke(request, task_dir)
        return normalize_generation(result, metadata)

    def invoke_structured(self, request: dict[str, Any], task_dir: Path) -> dict[str, Any]:
        result, metadata = self._invoke(request, task_dir)
        return {**result, "provider_metadata": metadata}
