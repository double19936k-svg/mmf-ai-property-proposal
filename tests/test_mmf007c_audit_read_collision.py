"""MMF-007C Round 3：run_audit.json 这类"一边写一边读"的审计文件不能撞一次就判交付失败。

现场是 dist 侧全量跑里的 test_flow_b（挂在 006E-R2 一直记为"偶发"的那条）：状态接口返回
`{'error_code': 'permission_denied', 'detail': "[Errno 13] Permission denied: '<run_dir>/run_audit.json'"}`。
r3_run_audit_readwrite_race_probe.py 把姿态复现出来了：一个线程原子替换、三个线程普通读，
25 秒内 4155 次读撞出 7 次，签名逐字一致（写侧同时撞出 7 次 [WinError 5] '<tmp>' -> '<dst>'）。
这里钉三件事：瞬时共享冲突要重试、不是共享冲突的故障不得被重试掩盖、重试用尽仍然照原样抛。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import app_core  # noqa: E402


class AuditReadCollisionTests(unittest.TestCase):
    """read_json / read_jsonl 的读盘重试预算。"""

    def setUp(self) -> None:
        self.folder = Path(app_core.RUNTIME_DIR) / f"t_audit_read_collision_{os.getpid()}"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.target = self.folder / "run_audit.json"
        app_core.write_json(self.target, {"status": "generation_completed", "seq": 7})

    def tearDown(self) -> None:
        shutil.rmtree(self.folder, ignore_errors=True)

    def _flaky_read_text(self, failures: int, error: type[OSError]):
        calls: list[str] = []
        original = Path.read_text

        def fake(self, *args, **kwargs):
            calls.append("call")
            if len(calls) <= failures:
                raise error(13, "Permission denied", str(self))
            return original(self, *args, **kwargs)

        return fake, calls

    def test_transient_permission_error_on_read_is_retried(self) -> None:
        fake, calls = self._flaky_read_text(2, PermissionError)
        with patch.object(Path, "read_text", fake):
            row = app_core.read_json(self.target, "test_read_collision")
        self.assertEqual(row["seq"], 7, "重试之后必须把原来那份内容读回来，不能返回半成品")
        self.assertEqual(len(calls), 3)

    def test_permission_error_beyond_budget_still_raises(self) -> None:
        fake, calls = self._flaky_read_text(10_000, PermissionError)
        with patch.object(Path, "read_text", fake):
            with self.assertRaises(PermissionError):
                app_core.read_json(self.target, "test_read_collision")
        # 预算必须有限：目录真的不可读时，故障要原样抛给上层，不能被咽成"稍后再说"。
        self.assertEqual(len(calls), app_core._TRANSIENT_READ_ATTEMPTS)

    def test_non_sharing_violation_is_not_retried(self) -> None:
        fake, calls = self._flaky_read_text(10_000, FileNotFoundError)
        with patch.object(Path, "read_text", fake):
            with self.assertRaises(FileNotFoundError):
                app_core.read_json(self.target, "test_read_collision")
        self.assertEqual(len(calls), 1, "缺文件不是共享冲突：多等一轮只会把可诊断的故障糊成一团")

    def test_jsonl_shares_the_same_read_path(self) -> None:
        lines = self.folder / "access_probe.jsonl"
        lines.write_text(json.dumps({"a": 1}) + "\n" + json.dumps({"a": 2}) + "\n", encoding="utf-8")
        fake, calls = self._flaky_read_text(1, PermissionError)
        with patch.object(Path, "read_text", fake):
            rows = app_core.read_jsonl(lines, "test_read_collision")
        self.assertEqual([row["a"] for row in rows], [1, 2])
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
