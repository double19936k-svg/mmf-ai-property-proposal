from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import paths
from user_errors import USER_MESSAGES

AUTO_IMAGE_PROVIDER = "auto"

DEFAULTS = {
    "schema_version": "mmf-desktop-user-settings-v0.1",
    "first_run_completed": False,
    "default_provider": "qwen_modelstudio",
    "default_medium": "WORD",
    "enable_grok_bridge": False,
    "enable_image_provider": True,
    "image_provider_name": AUTO_IMAGE_PROVIDER,
    "image_provider_status": "not_configured",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def current_settings() -> dict[str, Any]:
    roots = paths.current()
    stored = paths.load_user_settings(roots.package_root)
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in stored.items() if not str(k).startswith("_")})
    merged.update({
        "data_root": str(roots.data_root),
        "output_root": str(roots.output_root),
        "logs_dir": str(roots.logs_dir),
        "config_root": str(roots.config_root),
        "temp_root": str(roots.temp_root),
        "runs_dir": str(roots.runs_dir),
        "settings_file": str(roots.user_settings_file),
        "secure_config_dir": str(roots.secure_config_dir),
        "app_version": paths.APP_VERSION,
        "app_name": paths.APP_NAME,
        "app_status": paths.APP_STATUS,
        "image_provider_name": merged.get("image_provider_name") or AUTO_IMAGE_PROVIDER,
        "image_provider_status": merged.get("image_provider_status") or "not_configured",
        "image_provider_message": "使用 Qoder CN 写 PPT 时，自动模式会调用已连接的 Qoder 生成场景配图。也可以手动选择其他图片服务。",
    })
    return merged


def resolve_image_provider(settings: dict[str, Any], writing_provider: str) -> str:
    selection = settings.get("image_provider_name") or AUTO_IMAGE_PROVIDER
    if selection == AUTO_IMAGE_PROVIDER:
        return "qoder_account_image" if writing_provider == "qoder_account" else "qwen_image"
    return str(selection)


def _validate_dir(label: str, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.drive and not path.is_absolute():
        path = (paths.current().package_root / path).resolve()
    else:
        path = path.resolve()
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".mmf_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise ValueError(f"{label}不可写") from exc
    forbidden = (paths.current().package_root / "app").resolve()
    if path == forbidden:
        raise ValueError(f"{label}不能使用程序内部目录")
    return path


def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
    roots = paths.current()
    current = current_settings()
    images_enabled = bool(payload.get("enable_image_provider", current.get("enable_image_provider", True)))
    updates = {
        "data_root": str(_validate_dir("工作目录", payload.get("data_root") or current["data_root"])),
        "output_root": str(_validate_dir("输出目录", payload.get("output_root") or current["output_root"])),
        "logs_dir": str(_validate_dir("日志目录", payload.get("logs_dir") or current["logs_dir"])),
        "config_root": str(_validate_dir("配置目录", payload.get("config_root") or current["config_root"])),
        "temp_root": str(_validate_dir("临时目录", payload.get("temp_root") or current["temp_root"])),
        "runs_dir": str(_validate_dir("Run目录", payload.get("runs_dir") or current["runs_dir"])),
        "default_provider": str(payload.get("default_provider") or current.get("default_provider") or "qwen_modelstudio"),
        "default_medium": str(payload.get("default_medium") or current.get("default_medium") or "WORD").upper(),
        "enable_grok_bridge": bool(payload.get("enable_grok_bridge", current.get("enable_grok_bridge", False))),
        "enable_image_provider": images_enabled,
        "image_provider_name": str(payload.get("image_provider_name") or current.get("image_provider_name") or AUTO_IMAGE_PROVIDER),
        "image_provider_status": "enabled" if images_enabled else "disabled",
        "first_run_completed": True,
        "updated_at": now_iso(),
        "schema_version": DEFAULTS["schema_version"],
        "package_root": str(roots.package_root),
    }
    if updates["default_medium"] not in {"WORD", "PPT"}:
        raise ValueError("默认输出只能是WORD或PPT")
    configured_target = os.environ.get("MMF_USER_SETTINGS")
    target = Path(configured_target).expanduser().resolve() if configured_target else roots.secure_config_dir / "user_settings.json"
    portable = roots.config_root / "user_settings.json"
    _write_json(target, updates)
    try:
        if portable != target:
            _write_json(portable, {k: v for k, v in updates.items() if "key" not in k.lower()})
    except OSError:
        pass
    paths.reload(roots.package_root)
    paths.ensure_directories()
    try:
        from app_core import provider_manager
        manager = provider_manager()
        static_image = resolve_image_provider(updates, "")
        for item in manager.config.get("available_providers", []):
            if item.get("provider_type") == "image_generation":
                item["enabled"] = images_enabled and item.get("provider_name") == static_image
        manager._write_config(manager.config)
        manager.reload()
    except Exception:
        pass
    return current_settings()


def setup_payload() -> dict[str, Any]:
    settings = current_settings()
    return {
        "required": not bool(settings.get("first_run_completed")),
        "settings": settings,
        "fields": [
            {"id": "output_root", "label": "输出目录", "value": settings["output_root"]},
            {"id": "default_provider", "label": "默认AI引擎", "value": settings["default_provider"]},
            {"id": "default_medium", "label": "默认输出", "value": settings["default_medium"]},
            {"id": "enable_grok_bridge", "label": "启用本机Grok Bridge（可选）", "value": settings["enable_grok_bridge"]},
            {"id": "enable_image_provider", "label": "启用AI图片服务", "value": settings.get("enable_image_provider", True)},
            {"id": "image_provider_name", "label": "图片引擎", "value": settings["image_provider_name"]},
        ],
    }
