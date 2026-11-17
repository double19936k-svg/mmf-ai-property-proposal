from __future__ import annotations

from typing import Any


STATUS_LABELS = {
    "connected": "已连接",
    "installed": "已安装",
    "not_installed": "未安装",
    "not_configured": "未配置",
    "authentication_required": "需要认证",
    "unavailable": "不可用",
    "not_tested": "待测试",
}


def public_status(row: dict[str, Any], settings: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = settings or {}
    name = str(row.get("provider_name") or "")
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    ptype = str(row.get("provider_type") or metadata.get("provider_type") or "")
    raw = str(row.get("connection_status") or row.get("status") or "")
    message = str(row.get("message") or "")
    qualification = str(row.get("qualification_status") or "")
    display = str(row.get("display_name") or name)
    user_status = "not_configured"
    user_message = message

    if name == "zcode_cn":
        display = "ZCode 国内账户（实验）"
        user_status = "connected" if row.get("available") else "not_tested"
        user_message = message or "请在ZCode国内账户卡片中登录并选择模型。"
    elif name == "qoder_account":
        display = str(row.get("display_name") or "Qoder账户（实验）")
        user_status = "connected" if row.get("available") else "authentication_required" if raw == "authentication_required" else "not_tested"
        user_message = message or "请在连接卡片中连接Qoder并选择模型。"
    elif name == "kimi_moonshot":
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
        elif raw in {"not_tested", "pending_test"}:
            user_status = "not_tested"
            user_message = "Kimi已配置，请先测试连接"
        else:
            user_status = "unavailable"
            user_message = message or "Kimi连接测试未通过"
    elif ptype == "cursor_account":
        user_status = ("connected" if row.get("available") else
                       "not_configured" if raw == "not_connected" else "authentication_required")
        user_message = message or "请重新检查 Cursor 官方授权。"
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
    elif name == "glm_zhipu":
        display = "智谱 GLM"
        if row.get("available"):
            user_status = "connected"
            user_message = "智谱GLM已连接"
        elif not row.get("configured"):
            user_status = "not_configured"
            user_message = "请填写智谱 API Key后点测试连接。密钥在 open.bigmodel.cn 创建。"
        elif any(token in f"{raw} {message}".lower() for token in ("auth", "401", "unauthorized")):
            user_status = "authentication_required"
            user_message = "智谱密钥无效或需要重新填写"
        elif raw in {"not_tested", "pending_test"}:
            user_status = "not_tested"
            user_message = "智谱GLM已配置，请先测试连接"
        else:
            user_status = "unavailable"
            user_message = message or "智谱GLM连接测试未通过"
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
        elif raw in {"not_tested", "pending_test"}:
            user_status = "not_tested"
            user_message = "千问已配置，请先测试连接"
        else:
            user_status = "unavailable"
            user_message = message or "千问连接测试未通过"
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
        and user_status not in {"authentication_required", "unavailable", "not_tested"}
    )
    if name == "grok_build" or ptype == "cursor_account":
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
