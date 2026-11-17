"""R41 §30：测试环境的网络禁令与产品必须是同一把尺子。

R4 的裁决指出：进程级 lockdown 只在 `server.py` 启动时装，pytest 与直接 import 都不继承它。
本轮按要求只做极小改动 —— 把测试侧那份**逐字相同**的主机判定删掉，改为引用产品模块的
`providers.network_lockdown.is_allowed_host`；"直接 import 的脚本也继承 lockdown"需要
site-packages 级挂钩（.pth / 每个入口点显式 install），不是极小改动，已登记 Run E。

这里验的是改完之后：判定只有一处、测试进程确实被同一处挡住了、本机回环照常可用。
全部离线，不发起任何 Provider 调用。
"""
from __future__ import annotations

import ast
import socket
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
APP = APP_ROOT / "app"
for _path in (str(APP), str(APP_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from providers import network_lockdown  # noqa: E402

CONTEST = APP_ROOT / "tests" / "conftest.py"
MARKER = network_lockdown.MARKER


class HostPolicyHasOneOwner(unittest.TestCase):
    """哪些地址算"本机"，答案只能有一个来源。"""

    def test_conftest_does_not_carry_its_own_host_vocabulary(self) -> None:
        # 扫代码不扫散文：conftest 的文档字符串里提到"曾经自带 ALLOWED_HOSTS"是这轮的说明，
        # 把它当违规等于逼人把证据从注释里删掉。
        tree = ast.parse(CONTEST.read_text(encoding="utf-8"))
        documented = set()
        body = tree.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            documented.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = node.body
                if inner and isinstance(inner[0], ast.Expr) and isinstance(inner[0].value, ast.Constant):
                    documented.add(id(inner[0].value))
        assigned = set()
        for node in body:
            if isinstance(node, ast.Assign):
                assigned |= {str(target.id) for target in node.targets if isinstance(target, ast.Name)}
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                assigned.add(str(node.target.id))
        self.assertNotIn("ALLOWED_HOSTS", assigned,
                         "测试侧又养了一份主机白名单 —— 与产品那份相同不代表不会分叉")
        imported = {(str(node.module), alias.name) for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) for alias in node.names}
        self.assertIn(("providers.network_lockdown", "is_allowed_host"), imported,
                      "主机判定必须引用产品那一份，才是同一把尺子")
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in documented:
                self.assertNotIn(node.value, {".local", ".internal", "127.", "::ffff:127."},
                                 f"conftest 里重新写回了主机判定：{node.value!r}")

    def test_product_module_is_still_the_single_definition(self) -> None:
        self.assertTrue(hasattr(network_lockdown, "ALLOWED_HOSTS"))
        self.assertTrue(network_lockdown.is_allowed_host("127.0.0.1"))
        self.assertTrue(network_lockdown.is_allowed_host("::ffff:127.0.0.1"))
        self.assertFalse(network_lockdown.is_allowed_host("dashscope.aliyuncs.com"))
        self.assertFalse(network_lockdown.is_allowed_host("api.openai.com"))


class TestProcessIsActuallyLocked(unittest.TestCase):
    """autouse fixture 真的挡在这个进程上，而不是只写在文档里。"""

    def test_external_dns_lookup_is_refused_inside_the_test_session(self) -> None:
        import conftest

        before = dict(conftest.BLOCK_COUNTS)
        with self.assertRaises(AssertionError) as caught:
            socket.getaddrinfo("dashscope.aliyuncs.com", 443)
        self.assertIn(MARKER, str(caught.exception))
        self.assertEqual(before["getaddrinfo"] + 1, conftest.BLOCK_COUNTS["getaddrinfo"],
                         "拦截要留下可审计的计数，否则'零真实调用'只能靠嘴说")

    def test_external_connect_is_refused_inside_the_test_session(self) -> None:
        import conftest

        before = dict(conftest.BLOCK_COUNTS)
        with self.assertRaises(AssertionError) as caught:
            socket.socket().connect(("api.moonshot.cn", 443))
        self.assertIn(MARKER, str(caught.exception))
        self.assertEqual(before["connect"] + 1, conftest.BLOCK_COUNTS["connect"])

    def test_loopback_still_works_for_mock_server_tests(self) -> None:
        # 挡外部不能挡到自己：Mock Server / 本地 HTTP 测试都要能用回环。
        socket.getaddrinfo("127.0.0.1", 8123)
        socket.getaddrinfo("localhost", 8123)

    def test_lockdown_can_be_turned_off_by_env_without_importing_a_second_policy(self) -> None:
        import conftest

        self.assertEqual(conftest.LOCKDOWN_ENV, "MMF_TEST_PROVIDER_LOCKDOWN")
        # 关掉开关时测试侧也不得换一把尺子：判定函数还是产品那一个。
        self.assertIs(conftest.is_allowed_host, network_lockdown.is_allowed_host)


if __name__ == "__main__":
    unittest.main()
