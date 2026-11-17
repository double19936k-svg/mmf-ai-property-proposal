"""Cursor account preferences and official login controls for the local UI."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading

import paths
from . import cursor_account
from .base import write_json

_lock = threading.RLock()
_action = ""
_message = ""


def _preference_file():
    return paths.current().config_root / "cursor_account_selection.json"


def _preferences():
    try:
        row = json.loads(_preference_file().read_text(encoding="utf-8"))
        return row if isinstance(row, dict) else {}
    except (OSError, ValueError):
        return {}


def disconnected():
    return _preferences().get("disconnected") is True


def provider_health(provider):
    health = provider.health_check()
    if disconnected():
        return {**health, "available": False, "status": "not_connected",
                "message": "本产品已断开 Cursor 连接，请在 AI 引擎设置中重新连接。"}
    return health


def status(default_provider=None):
    with _lock:
        pref = _preferences()
        models = [
            {"provider_name": row["provider_name"], "label": row["display_name"].split(" · ", 1)[1].removesuffix("（试用）"),
             "available": cursor_account.bridge_health(row["model"])[0]}
            for row in cursor_account.cursor_provider_rows()
        ]
        keys = {row["provider_name"] for row in models}
        selected = pref.get("provider_name")
        if not selected:
            selected = next(
                (name for name in (default_provider, cursor_account.NAME) if name in keys),
                models[0]["provider_name"] if models else "",
            )
        ready = not pref.get("disconnected") and any(
            row["available"] and row["provider_name"] == selected for row in models
        )
        return {"models": models, "provider_name": selected, "connected": ready,
                "disconnected": bool(pref.get("disconnected")), "busy": _action,
                "message": _message}


def _refresh_catalog(action, root, node):
    global _action, _message
    try:
        completed = subprocess.run(
            [node, str(root / "cursor_bridge.mjs"), "login" if action == "connect" else "models"],
            cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="replace", timeout=315 if action == "connect" else 25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        row = json.loads(completed.stdout.splitlines()[-1])
        if completed.returncode or row.get("state") not in {"ready", "login_required"}:
            raise ValueError("Cursor catalog request failed")
        models = [
            {"id": item["id"], "displayName": str(item.get("displayName") or item["id"])}
            for item in row.get("models", []) if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        with _lock:
            previous = {item["provider_name"] for item in cursor_account.cursor_provider_rows()}
            if row["state"] == "login_required":
                models = cursor_account.read_catalog(root).get("models", [])
            write_json(root / "catalog.json", {"state": row["state"], "models": models,
                                               "expires_at_ms": row.get("expires_at_ms")})
            if previous != {item["provider_name"] for item in cursor_account.cursor_provider_rows()}:
                from app_core import provider_manager
                provider_manager().reload()
            if row["state"] == "ready":
                if action == "connect":
                    write_json(_preference_file(), {**_preferences(), "disconnected": False})
                _message = ("测试通过。本产品仍处于断开状态，点击「连接引擎」即可恢复。"
                            if disconnected() else "Cursor 授权检查通过，模型列表已更新。")
            else:
                _message = "请点击「连接引擎」，在 Cursor 官方页面完成授权。"
    except (OSError, ValueError, IndexError, AttributeError, subprocess.TimeoutExpired):
        with _lock:
            _message = "Cursor 连接操作未完成，请重试。"
    finally:
        with _lock:
            _action = ""


def update(action, data, default_provider=None):
    global _action, _message
    with _lock:
        if _action:
            raise ValueError("Cursor 连接操作正在进行，请稍候。")
        pref = _preferences()
        if action == "model":
            name = data.get("provider_name")
            if name not in {row["provider_name"] for row in cursor_account.cursor_provider_rows()}:
                raise ValueError("请选择列表中的 Cursor 模型。")
            write_json(_preference_file(), {**pref, "provider_name": name})
            _message = "模型已保存，之后新提交的任务将使用此模型。"
        elif action == "disconnect":
            write_json(_preference_file(), {**pref, "disconnected": True})
            _message = "本产品已断开 Cursor 连接。"
        elif action in {"connect", "check"}:
            root = cursor_account.runtime()
            node = shutil.which("node")
            if not root or not node or not (root / "cursor_bridge.mjs").is_file():
                raise ValueError("未找到 Cursor 桥接运行环境。")
            _action = action
            _message = ("如需授权，请在 Cursor 官方浏览器窗口完成登录。"
                        if action == "connect" else "正在检查 Cursor 授权和模型列表。")
            try:
                threading.Thread(target=_refresh_catalog, args=(action, root, node), daemon=True).start()
            except RuntimeError:
                _action = ""
                raise
        else:
            raise ValueError("未找到该 Cursor 连接操作。")
        return status(default_provider)
