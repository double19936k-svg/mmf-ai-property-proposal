from __future__ import annotations

from typing import Any


STATUS_LABELS = {
    "connected": "已连接",
    "installed": "已安装",
    "not_installed": "未安装",
    "not_configured": "未配置",
    "authentication_required": "需要认证",
    "unavailable": "不可用",
}


def public_status(row: dict[str, Any], settings: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = settings or {}
    name = str(row.get("provider_name") or "")
    ptype = str(row.get("provider_type") or "")
    raw = str(row.get("connection_status") or row.get("status") or "")
    message = str(row.get("message") or "")
    qualification = str(row.get("qualification_status") or "")
    display = str(row.get("display_name") or name)
    user_status = "not_configured"
    user_message = message

    if name == "kimi_moonshot":
        display = "Kimi / Moonshot"
        if row.get("available"):
            user_status = "connected"
            user_message = "Kimi已连接"
        elif not row.get("configured"):
            user_status = "not_configured"
            user_message = "请填写Kimi API Key后点测试连接。密钥在 platform.kimi.com 创建。"
        elif any(token in f"{raw} {message}".lower() for token in ("auth", "401", "unauthorized")):
            user_status = "authentication_required"
            user_message = "Kimi密钥无效或需要重新填写"
        else:
            user_status = "connected"
            user_message = "Kimi已配置，可直接用于生成"
    elif name == "mock":
        user_status = "connected" if row.get("available") else "not_configured"
        display = "Mock（流程测试）"
        user_message = "仅用于验证流程，不能生成正式方案。"
    elif ptype == "grok_build" or name == "grok_build":
        display = "Local Grok Bridge"
        if row.get("available"):
            user_status = "connected"
            user_message = "本机Grok已登录且测试通过，可用于生成"
        elif any(token in f"{raw} {message}".lower() for token in ("auth", "login", "401", "unauthenticated")):
            user_status = "authentication_required"
            user_message = "本机Grok需要登录。可先使用千问。"
        elif raw in {"installed", "not_tested"} or row.get("installed") or (row.get("configured") and not row.get("available")):
            user_status = "installed"
            user_message = "本机Grok CLI已安装，请点测试连接确认登录后使用"
        elif raw in {"not_configured", "not_installed"} or not settings.get("enable_grok_bridge"):
            user_status = "not_configured"
            user_message = "未检测到本机Grok CLI。"
        else:
            user_status = "unavailable"
            user_message = "当前Grok Bridge不可用。可改用千问等API引擎。"
    elif ptype == "image_generation" or name in {"qwen_image", "grok_image"}:
        display = str(row.get("display_name") or "AI图片服务")
        if not settings.get("enable_image_provider", True) and name != (settings.get("image_provider_name") or "qwen_image"):
            user_status = "not_configured"
            user_message = "未在首次配置中启用"
        elif row.get("available"):
            user_status = "connected"
            user_message = "图片服务已连接"
        elif (not row.get("configured")) or raw in {"not_tested", "pending_test", "not_configured"}:
            user_status = "not_configured"
            user_message = "请填写图片服务API Key，或到设置页点击测试连接。千问万相可与文字千问共用同一把Key。"
        elif any(token in f"{raw} {message}".lower() for token in ("auth", "401", "unauthorized")):
            user_status = "authentication_required"
            user_message = "图片服务密钥无效或需要重新填写"
        else:
            user_status = "unavailable"
            user_message = "图片服务当前不可用"
    elif name == "qwen_modelstudio":
        display = "千问 Model Studio"
        if row.get("available"):
            user_status = "connected"
            user_message = "千问已连接"
        elif not row.get("configured"):
            user_status = "not_configured"
            user_message = "Provider尚未配置"
        elif any(token in f"{raw} {message}".lower() for token in ("auth", "401", "unauthorized")):
            user_status = "authentication_required"
            user_message = "千问密钥无效或需要重新填写"
        else:
            user_status = "connected"
            user_message = "千问已配置，可直接用于生成"
    else:
        if row.get("available"):
            user_status = "connected"
        elif not row.get("configured"):
            user_status = "not_configured"
        else:
            user_status = "connected"

    selectable = (
        ptype != "image_generation"
        and bool(row.get("configured") or row.get("available"))
        and user_status not in {"authentication_required", "unavailable"}
    )
    if name == "grok_build":
        selectable = user_status == "connected"

    return {
        **row,
        "display_name": display,
        "user_status": user_status,
        "user_status_label": STATUS_LABELS.get(user_status, user_status),
        "user_message": user_message,
        "selectable": selectable,
        "optional": ptype in {"grok_build", "image_generation"} or name in {"grok_build", "qwen_image", "grok_image"},
        "capability": "image" if ptype == "image_generation" else "text",
    }
