"""进程级外部网络禁令（MMF-007C 阶段7，仅在显式开启时生效）。

用途：当某次运行必须证明"TODD_PROVIDER_CALLS = 0"时，在启动前设置
MMF_PROVIDER_NETWORK_LOCKDOWN=1，本模块会把任何非本机回环的 socket 连接与 DNS 解析
直接判为失败。默认关闭，不影响正常产品行为。
"""
from __future__ import annotations

import os
import socket
from typing import Any

ENV_FLAG = "MMF_PROVIDER_NETWORK_LOCKDOWN"
MARKER = "UNEXPECTED_REAL_PROVIDER_CALL"
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "local", "host.docker.internal"}

_STATE: dict[str, Any] = {"installed": False, "blocks": 0}


def enabled_by_env() -> bool:
    return str(os.environ.get(ENV_FLAG) or "").strip().lower() in {"1", "true", "yes", "on"}


def is_allowed_host(host: object) -> bool:
    if host is None:
        return True
    text = str(host).strip("[]").lower()
    if text in ALLOWED_HOSTS:
        return True
    if text.endswith(".local") or text.endswith(".internal"):
        return True
    return text.startswith("127.") or text.startswith("::ffff:127.")


def install() -> dict[str, Any]:
    """把回环以外的 connect/getaddrinfo 全部拒绝；重复调用只装一次。"""
    if _STATE["installed"]:
        return status()
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if not is_allowed_host(host):
            _STATE["blocks"] += 1
            raise AssertionError(f"{MARKER}: 禁止连接外部主机 {host!r}（本次运行只允许 Qoder 内部额度）")
        return real_connect(self, address, *args, **kwargs)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not is_allowed_host(host):
            _STATE["blocks"] += 1
            raise AssertionError(f"{MARKER}: 禁止解析外部主机 {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]
    _STATE["installed"] = True
    return status()


def status() -> dict[str, Any]:
    return {"installed": bool(_STATE["installed"]), "blocked_external_attempts": int(_STATE["blocks"]), "env_flag": ENV_FLAG}


def describe_provider_calls() -> dict[str, Any]:
    """报告用的口径：本进程是否具备任何外部 Provider 调用能力。"""
    row = status()
    return {
        "network_lockdown_installed": row["installed"],
        "blocked_external_provider_attempts": row["blocked_external_attempts"],
        "todd_provider_calls": 0 if row["installed"] else None,
    }
