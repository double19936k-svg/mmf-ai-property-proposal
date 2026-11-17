"""MMF-007 周末任务全局测试隔离（V1.1 任务书 0.3 节）。

EXTERNAL_PROVIDER_CALLS_DISABLED 机制：
- 所有测试默认禁止访问真实商业 Provider Endpoint（DashScope/Moonshot/Zhipu/XAI/DeepSeek/Wanx 等）；
- 任何非 localhost 的 socket 连接 / DNS 解析尝试 → 立即失败并标记 UNEXPECTED_REAL_PROVIDER_CALL；
- localhost / 127.0.0.1 / ::1 允许（Mock Server、本地 HTTP 测试）；
- 仅影响测试进程，不触碰 production 配置与 Todd 的真实凭据。

R41 §30：哪些主机算"本机回环"由**产品那一份** `providers.network_lockdown.is_allowed_host`
说了算。这里曾经自带一份 ALLOWED_HOSTS 与一份判定函数，两份逐字相同 —— 相同不等于不会分叉：
测试侧放宽一格，"测试里没有真实 Provider 调用"这句证言就悄悄换了一把尺子。现在测试只保留
monkeypatch 的生命周期管理（每条测试后自动还原），判定与拦截计数都归产品模块。
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from providers.network_lockdown import MARKER, is_allowed_host  # noqa: E402

LOCKDOWN_ENV = "MMF_TEST_PROVIDER_LOCKDOWN"
# 拦截次数：报告"本次测试跑有没有试图出去"时读它，不读测试自己数的遍数。
BLOCK_COUNTS: dict[str, int] = {"connect": 0, "getaddrinfo": 0}


@pytest.fixture(autouse=True)
def external_provider_call_lockdown(monkeypatch, request):
    """默认开启；单独豁免：给测试标 @pytest.mark.allow_real_network（不建议）。"""
    if os.environ.get(LOCKDOWN_ENV, "1") != "1":
        yield
        return
    if request.node.get_closest_marker("allow_real_network"):
        yield
        return

    def guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if not is_allowed_host(host):
            BLOCK_COUNTS["connect"] += 1
            raise AssertionError(f"{MARKER}: 测试试图连接外部主机 {host!r}（测试环境禁止真实 Provider 调用）")
        return real_connect(self, address, *args, **kwargs)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not is_allowed_host(host):
            BLOCK_COUNTS["getaddrinfo"] += 1
            raise AssertionError(f"{MARKER}: 测试试图解析外部主机 {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield
