"""防误触机制自检：外部连接必须失败，localhost 必须放行。"""
import socket
import pytest


def test_external_connect_blocked():
    with pytest.raises(AssertionError, match="UNEXPECTED_REAL_PROVIDER_CALL"):
        socket.create_connection(("dashscope.aliyuncs.com", 443), timeout=1)


def test_external_dns_blocked():
    with pytest.raises(AssertionError, match="UNEXPECTED_REAL_PROVIDER_CALL"):
        socket.getaddrinfo("api.moonshot.cn", 443)


def test_localhost_allowed():
    # 不真正监听，仅验证守卫放行 localhost（连接失败也是 ConnectionRefused 而非断言错误）
    try:
        socket.create_connection(("127.0.0.1", 1), timeout=0.2)
    except AssertionError:
        raise
    except OSError:
        pass
