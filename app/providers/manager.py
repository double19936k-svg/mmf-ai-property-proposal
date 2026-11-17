from __future__ import annotations

import json
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from .account_image import ACCOUNT_IMAGE_NAMES, AccountImageProvider, account_image_rows
from .base import AIProvider, ProviderUnavailableError
from .credential_store import CredentialStore
from .cursor_account import CursorAccountProvider, cursor_provider_rows
from .grok_build import GrokBuildProvider
from .image_generation import ImageGenerationProvider, normalize_image_base_url
from .local_cli import LocalCLIProvider
from .mock import MockProvider
from .openai_compatible import OpenAICompatibleProvider, normalize_openai_base_url
from .qoder_internal import QoderInternalProvider
from .qoder_account import QoderAccountProvider, account_provider_rows
from .zcode_account import ZCodeAccountProvider, zcode_provider_rows

PROVIDER_TYPES = {
    "grok_build": GrokBuildProvider,
    "openai_compatible": OpenAICompatibleProvider,
    "local_cli": LocalCLIProvider,
    "mock": MockProvider,
    "image_generation": ImageGenerationProvider,
    "qoder_internal": QoderInternalProvider,
    "qoder_account": QoderAccountProvider,
    "zcode_account": ZCodeAccountProvider,
    "cursor_account": CursorAccountProvider,
}
PUBLIC_FIELDS = {"display_name", "enabled", "base_url", "model", "reasoning_mode", "temperature", "timeout", "health_timeout", "extra_options", "billing_note"}
DEVELOPER_FIELDS = {"provider_type", "endpoint_alias", "provider_version", "command", "command_template", "api_key_env", "proxy_env", "model_alias"}

# MMF-007C 阶段7：只在设置了队列目录的进程里出现的任务作用域执行通道。
# 不写入 providers.local.json，也不读取凭据库；取消环境变量后彻底消失。
TASK_SCOPED_PROVIDER_ENV = "MMF_QODER_INTERNAL_QUEUE_DIR"


def task_scoped_provider_rows() -> list[dict[str, Any]]:
    queue_dir = str(os.environ.get(TASK_SCOPED_PROVIDER_ENV) or "").strip()
    if not queue_dir:
        return []
    try:
        wait_timeout = max(30, int(os.environ.get("MMF_QODER_INTERNAL_WAIT_TIMEOUT") or 1800))
    except (TypeError, ValueError):
        wait_timeout = 1800
    return [{
        "provider_name": "qoder_internal",
        "display_name": "Qoder 内部 Qwen3.8-Flash（文件队列）",
        "provider_type": "qoder_internal",
        "enabled": True,
        "test_mode": True,
        "qualification_status": "task_scoped",
        "model": "qwen3.8-flash",
        "endpoint_alias": "qoder_internal_queue",
        "billing_note": "仅消耗 Qoder 内部免费额度；不读取任何外部 Provider Key 与凭据库。",
        "queue_dir": queue_dir,
        "wait_timeout": wait_timeout,
    }]

class ProviderManager:
    def __init__(self, app_root: Path, cache_seconds: int = 30, config_dir: Path | None = None):
        self.app_root = app_root.resolve()
        self.cache_seconds = cache_seconds
        self._lock = threading.RLock()
        self._health_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._live_pass: dict[str, dict[str, Any]] = {}
        self.credential_store = CredentialStore(os.environ.get("MMF_CREDENTIAL_NAMESPACE") or "property_ai")
        from paths import CONFIG_DIR
        cfg = Path(config_dir or CONFIG_DIR).resolve()
        self.local_path = cfg / "providers.local.json"
        self.example_path = cfg / "providers.example.json"
        if not self.local_path.exists():
            self._write_config(json.loads(self.example_path.read_text(encoding="utf-8-sig")))
        else:
            self._merge_example_providers()
        self.reload()
        self._load_live_pass()

    def _merge_example_providers(self) -> None:
        if not self.example_path.is_file() or not self.local_path.is_file():
            return
        local = json.loads(self.local_path.read_text(encoding="utf-8-sig"))
        example = json.loads(self.example_path.read_text(encoding="utf-8-sig"))
        have = {row.get("provider_name") for row in local.get("available_providers", [])}
        changed = False
        for item in example.get("available_providers", []):
            if item.get("provider_name") not in have:
                local.setdefault("available_providers", []).append(item)
                changed = True
        if changed:
            self._write_config(local)

    def _write_config(self, value: dict[str, Any]) -> None:
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.local_path.with_suffix(".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(self.local_path)

    def reload(self) -> None:
        with self._lock:
            self.config = json.loads(self.local_path.read_text(encoding="utf-8-sig"))
            self.default_provider = self.config.get("default_provider", "grok_build")
            self.providers: dict[str, AIProvider] = {}
            rows = [row for row in self.config.get("available_providers", []) if isinstance(row, dict)]
            have = {str(row.get("provider_name") or "") for row in rows}
            rows.extend(row for row in task_scoped_provider_rows() if str(row.get("provider_name")) not in have)
            rows.extend(row for row in account_provider_rows() if str(row.get("provider_name")) not in have)
            rows.extend(row for row in zcode_provider_rows() if str(row.get("provider_name")) not in have)
            rows.extend(row for row in cursor_provider_rows() if str(row.get("provider_name")) not in have)
            rows.extend(account_image_rows())
            configured_rows = {
                str(row.get("provider_name") or ""): row
                for row in rows
            }
            for item in rows:
                cls = (AccountImageProvider if item.get("provider_name") in ACCOUNT_IMAGE_NAMES
                       else PROVIDER_TYPES.get(item.get("provider_type")))
                if cls:
                    runtime_item = dict(item)
                    if item.get("provider_type") == "image_generation":
                        extra = dict(item.get("extra_options") or {})
                        reuse_name = str(extra.get("reuse_text_provider") or "")
                        reused = configured_rows.get(reuse_name) or {}
                        reused_url = str(reused.get("base_url") or "").strip()
                        if reused_url:
                            runtime_item["base_url"] = normalize_image_base_url(
                                reused_url,
                                str(extra.get("backend") or "dashscope_wanx"),
                            )
                            runtime_item["endpoint_inherited_from"] = reuse_name
                    self.providers[item["provider_name"]] = cls(runtime_item, self.credential_store)
            self._health_cache.clear()

    def _live_file(self) -> Path:
        try:
            import paths as mmf_paths
            return mmf_paths.current().runtime_dir / "provider_live.json"
        except Exception:
            return self.app_root.parent / "runtime" / "provider_live.json"

    def _load_live_pass(self) -> None:
        path = self._live_file()
        if not path.is_file():
            return
        try:
            stored = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, TypeError):
            return
        if isinstance(stored, dict):
            self._live_pass = {key: value for key, value in stored.items() if isinstance(value, dict)}

    def _save_live_pass(self) -> None:
        path = self._live_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._live_pass, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _record_live(self, provider_name: str, result: dict[str, Any]) -> None:
        with self._lock:
            if result.get("available"):
                self._live_pass[provider_name] = {
                    "configured": True,
                    "available": True,
                    "status": "available",
                    "installed": True,
                    "authenticated": True,
                    "message": "最近测试已通过",
                    "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "credential_source": result.get("credential_source", "not_applicable"),
                }
                self._save_live_pass()
                return
            if result.get("status") in {"installed", "not_installed", "not_configured", "pending_test"}:
                return
            if provider_name in self._live_pass:
                self._live_pass.pop(provider_name, None)
                self._save_live_pass()

    def _remembered_live(self, provider_name: str) -> dict[str, Any] | None:
        remembered = self._live_pass.get(provider_name)
        if not remembered or not remembered.get("available"):
            return None
        return dict(remembered)

    def _cli_present(self, provider: AIProvider) -> bool:
        if not bool(provider.config.get("enabled", False)):
            return False
        if provider.config.get("provider_type") != "grok_build":
            return True
        command = str(provider.config.get("command") or "grok")
        typical = Path.home() / ".grok" / "bin" / "grok.exe"
        return bool(shutil.which(command) or typical.is_file())

    def health(self, provider_name: str, refresh: bool = False) -> dict[str, Any]:
        provider = self.providers.get(provider_name)
        if not provider:
            return {"configured": False, "available": False, "status": "unknown_provider", "message": "Provider不存在", "metadata": {"provider_name": provider_name}}
        if isinstance(provider, AccountImageProvider):
            return provider.health_check()
        if provider.config.get("provider_type") == "cursor_account":
            from .cursor_settings import provider_health
            return provider_health(provider)
        if provider.config.get("provider_type") == "zcode_account":
            return provider.health_check()
        if provider.config.get("provider_type") == "qoder_account":
            # Explicit connection is checked by the SDK controller. Never issue the
            # manager's synthetic paid ping, or resurrect a cached login as ready.
            return provider.health_check()
        cached = self._health_cache.get(provider_name)
        if not refresh and cached and time.monotonic() - cached[0] < self.cache_seconds:
            return cached[1]
        live = bool(refresh or provider.config.get("live_health"))
        if not live and self._cli_present(provider):
            remembered = self._remembered_live(provider_name)
            if remembered:
                result = {**remembered, "configured": True, "metadata": provider.get_metadata()}
                self._health_cache[provider_name] = (time.monotonic(), result)
                return result
        result = provider.health_check()
        try:
            from .capability_probe import observe_from_health
            result["capability_observation"] = observe_from_health(provider, result, force=bool(refresh), run_synthetic=bool(refresh))
            observation = result["capability_observation"]
            if refresh and observation.get("structured_response") == "rejected":
                message = str(observation.get("reason") or "连接测试未通过最小生成验证，请检查模型名称或稍后重试。")
                candidates = [str(item) for item in (result.get("models") or []) if item][:8]
                if provider_name == "qwen_modelstudio" and candidates:
                    message += " 可选择的模型包括：" + "、".join(candidates)
                result = {
                    **result,
                    "available": False,
                    "status": str(observation.get("status") or "unavailable").lower(),
                    "message": message,
                }
        except Exception:
            result["capability_observation"] = {
                "status": "UNAVAILABLE",
                "reason": "probe_record_failed",
                "unverified_fields": ["max_context", "tpm", "rpm", "native_max_level"],
            }
        if live:
            self._record_live(provider_name, result)
        elif not result.get("available") and result.get("status") in {"installed", "pending_test"}:
            remembered = self._remembered_live(provider_name)
            if remembered:
                result = {**remembered, "configured": True, "metadata": provider.get_metadata()}
        self._health_cache[provider_name] = (time.monotonic(), result)
        return result

    @staticmethod
    def _connection_status(provider: AIProvider, health: dict[str, Any]) -> str:
        if provider.config.get("test_mode"):
            return "test_mode"
        if health.get("available"):
            return "connected"
        if health.get("status") == "installed":
            return "installed"
        if health.get("status") == "pending_test":
            return "not_tested"
        if health.get("status") == "not_configured":
            return "not_configured"
        if health.get("status") == "not_installed" or (provider.config.get("provider_type") == "grok_build" and "命令不可用" in health.get("message", "")):
            return "not_installed"
        return "connection_failed"

    def _passive_health(self, provider_name: str, provider: AIProvider) -> dict[str, Any]:
        if isinstance(provider, AccountImageProvider):
            return provider.health_check()
        if provider.config.get("provider_type") == "cursor_account":
            from .cursor_settings import provider_health
            return provider_health(provider)
        if provider.config.get("provider_type") == "zcode_account":
            return provider.health_check()
        if provider.config.get("provider_type") == "qoder_account":
            return provider.health_check()
        cached = self._health_cache.get(provider_name)
        if cached and time.monotonic() - cached[0] < self.cache_seconds:
            return cached[1]
        if provider.config.get("test_mode"):
            return provider.health_check()
        enabled = bool(provider.config.get("enabled", False))
        provider_type = provider.config.get("provider_type")
        configured = enabled
        if provider_type in {"openai_compatible", "image_generation"}:
            secret, source = provider._api_key() if hasattr(provider, "_api_key") else self.credential_store.get(provider_name, provider.config.get("api_key_env", ""))
            if provider_type == "image_generation":
                configured = enabled and bool(provider.config.get("model")) and bool(secret)
            else:
                configured = enabled and bool(provider.config.get("base_url")) and bool(provider.config.get("model")) and bool(secret)
            credential_source = source
        else:
            credential_source = "not_applicable"
            grok_ready = self._cli_present(provider) if provider_type == "grok_build" else False
            configured = enabled and grok_ready
            if provider_type == "grok_build" and grok_ready:
                remembered = self._remembered_live(provider_name)
                if remembered:
                    return {**remembered, "configured": True, "credential_source": credential_source, "metadata": provider.get_metadata()}
                return {
                    "configured": True,
                    "available": False,
                    "status": "installed",
                    "installed": True,
                    "message": "本机Grok CLI已安装，请点测试连接确认登录",
                    "metadata": provider.get_metadata(),
                    "credential_source": credential_source,
                }
        return {
            "configured": configured,
            "available": False,
            "status": "pending_test" if configured else "not_configured",
            "message": "已配置，正在后台检查连接" if configured else "未配置",
            "metadata": provider.get_metadata(),
            "credential_source": credential_source,
        }

    def list_status(self, refresh: bool = False) -> list[dict[str, Any]]:
        refreshed: dict[str, dict[str, Any]] = {}
        if refresh and self.providers:
            with ThreadPoolExecutor(max_workers=min(5, len(self.providers))) as pool:
                futures = {name: pool.submit(self.health, name, True) for name in self.providers}
                refreshed = {name: future.result() for name, future in futures.items()}
        rows = []
        for name, provider in self.providers.items():
            status = refreshed.get(name) if refresh else self._passive_health(name, provider)
            rows.append({"provider_name": name, "display_name": provider.config.get("display_name", name), "provider_type": provider.config.get("provider_type", ""), "model": provider.config.get("model", ""), "configured": status["configured"], "available": status["available"], "status": status["status"], "connection_status": self._connection_status(provider, status), "qualification_status": provider.config.get("qualification_status", "experimental"), "message": status["message"], "credential_source": status.get("credential_source", "not_applicable"), "test_mode": bool(provider.config.get("test_mode", False)), "is_default": name == self.default_provider, "billing_note": provider.config.get("billing_note", "")})
        return rows

    def public_config(self, provider_name: str, developer: bool = False) -> dict[str, Any]:
        provider = self.providers.get(provider_name)
        if not provider:
            raise ProviderUnavailableError(f"Provider不存在：{provider_name}")
        cfg = provider.config
        fields = PUBLIC_FIELDS | ({"provider_name"} | DEVELOPER_FIELDS if developer else {"provider_name"})
        value = {key: cfg.get(key) for key in fields if key in cfg}
        if cfg.get("provider_type") == "zcode_account":
            return {**value,"provider_name":provider_name,"credential_source":"official_zcode_bigmodel_login",
                "api_key_masked":"","qualification_status":"experimental"}
        if cfg.get("provider_type") == "cursor_account":
            return {**value, "provider_name": provider_name, "credential_source": "official_cursor_sdk_login",
                    "api_key_masked": "", "qualification_status": "experimental"}
        if cfg.get("provider_type") == "qoder_account":
            return {**value, "provider_name": provider_name, "credential_source": provider.get_metadata()["credential_source"],
                    "api_key_masked": "", "qualification_status": "experimental"}
        provider = self.providers.get(provider_name)
        if cfg.get("provider_type") == "image_generation" and provider is not None and hasattr(provider, "_api_key"):
            secret, credential_source = provider._api_key()
        else:
            secret, credential_source = self.credential_store.get(provider_name, cfg.get("api_key_env", ""))
        value.update({"provider_name": provider_name, "credential_ref": cfg.get("credential_ref", self.credential_store.credential_ref(provider_name)), "api_key_masked": "********" if secret else "", "credential_source": credential_source, "credential_capability": self.credential_store.capability(), "qualification_status": cfg.get("qualification_status", "experimental")})
        return value

    def save_config(self, provider_name: str, changes: dict[str, Any], api_key: str = "") -> dict[str, Any]:
        with self._lock:
            item = next((row for row in self.config.get("available_providers", []) if row.get("provider_name") == provider_name), None)
            if not item:
                raise ProviderUnavailableError(f"Provider不存在：{provider_name}")
            for key in PUBLIC_FIELDS:
                if key in changes:
                    value = changes[key]
                    if key == "base_url":
                        if item.get("provider_type") == "image_generation":
                            backend = str((item.get("extra_options") or {}).get("backend") or "dashscope_wanx")
                            value = normalize_image_base_url(str(value or ""), backend)
                        else:
                            value = normalize_openai_base_url(str(value or ""))
                    item[key] = value
            credential_result = None
            if str(api_key or "").strip():
                credential_result = self.credential_store.set(provider_name, api_key)
                item["credential_ref"] = credential_result["credential_ref"]
            self._write_config(self.config)
            self.reload()
        return {"saved": True, "provider": self.public_config(provider_name), "credential": credential_result, "health": self.health(provider_name, refresh=True)}

    def delete_credential(self, provider_name: str) -> dict[str, Any]:
        result = self.credential_store.delete(provider_name)
        self._health_cache.pop(provider_name, None)
        if provider_name in self._live_pass:
            self._live_pass.pop(provider_name, None)
            self._save_live_pass()
        return result

    def get(self, provider_name: str | None, require_available: bool = True) -> AIProvider:
        name = provider_name or self.default_provider
        provider = self.providers.get(name)
        if not provider:
            raise ProviderUnavailableError(f"Provider不存在：{name}")
        if require_available:
            status = self.health(name)
            if not status["available"]:
                raise ProviderUnavailableError(f"{provider.config.get('display_name', name)}不可用：{status['message']}")
        return provider
