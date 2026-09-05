from __future__ import annotations

import re
from typing import Any


SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|authorization|bearer|dashscope|sk-[A-Za-z0-9]{8,}|eyJ[A-Za-z0-9_-]{20,})"
)
PATH_NOISE_RE = re.compile(r"(?i)(traceback|winerror|permissionerror|filenotfounderror|jsondecodeerror|errno)")


USER_MESSAGES = {
    "provider_not_configured": "Provider尚未配置",
    "authentication_required": "当前AI引擎需要登录或重新填写密钥",
    "network_failed": "网络连接失败",
    "output_not_writable": "输出目录不可写",
    "grok_bridge_unavailable": "当前Grok Bridge不可用",
    "generation_failed": "生成失败，请查看运行日志",
    "python_missing": "未检测到可用的Python运行环境",
    "node_missing": "未检测到Node，PPT生成暂不可用",
    "port_in_use": "启动端口已被占用，请关闭已运行的MMF后重试",
    "config_missing": "首次安装尚未完成，请先运行“首次安装.cmd”",
    "permission_denied": "当前目录没有写入权限",
    "run_not_found": "找不到这次生成任务",
    "kimi_unavailable": "Kimi当前不可用，请检查API Key和网络",
    "image_provider_unavailable": "当前版本尚未启用AI图片服务",
}


def sanitize(text: str, limit: int = 400) -> str:
    raw = str(text or "")
    raw = SECRET_RE.sub("[已隐藏]", raw)
    raw = raw.replace("\\", "/")
    return raw[-limit:]


def classify(exc: BaseException | str, *, extra: str = "") -> dict[str, Any]:
    message = f"{exc} {extra}".lower()
    text = str(exc)
    if "not_configured" in message or "未配置" in message or "api key" in message and "empty" in message:
        code = "provider_not_configured"
    elif any(token in message for token in ("auth_required", "authentication", "unauthorized", "401", "login")):
        code = "authentication_required"
    elif any(token in message for token in ("grok", "bridge")) and any(token in message for token in ("unavailable", "not found", "未安装", "cli_not_found")):
        code = "grok_bridge_unavailable"
    elif any(token in message for token in ("network", "timed out", "timeout", "connection", "urlerror", "unreachable")):
        code = "network_failed"
    elif any(token in message for token in ("not writable", "不可写", "access is denied", "permission denied", "errno 13")):
        if "output" in message or "输出" in message:
            code = "output_not_writable"
        else:
            code = "permission_denied"
    elif "kimi" in message and any(token in message for token in ("官方认证", "尚未完成", "不可用")):
        code = "kimi_unavailable"
    elif "run" in message and ("不存在" in message or "not found" in message):
        code = "run_not_found"
    elif PATH_NOISE_RE.search(text) or "traceback" in message:
        code = "generation_failed"
    else:
        code = "generation_failed"
    user = USER_MESSAGES.get(code, USER_MESSAGES["generation_failed"])
    if code == "generation_failed" and text and not PATH_NOISE_RE.search(text) and any("\u4e00" <= ch <= "\u9fff" for ch in text):
        user = sanitize(text, 180)
    return {
        "error_code": code,
        "error": user,
        "detail": sanitize(text),
    }
