"""MMF-007C Round 4 · Part G/H：并发写撞车不得带走已经付费的正文。

守的是一条很具体的链级不变量：**Provider 返回之后，Content Unit 必须落盘，请求账必须结清，
而这两件事都不能依赖后面任何一次台账写盘成功。**

来源（`R4_offline_observations.md` 第 53 条）：300 类合成链里 5 个 CU（CU-S05-01-06..10）
由 Mock 正常返回、`provider_structured_output.json` 也在盘上，但请求账永久停在
`RESPONSE_RECEIVED`。`r4_run_batch_exception_probe.py` 在 4 轮合成链里复现 3 次，栈顶全部
是 `write_json` 的 `os.replace`：Windows 上目标文件正被同一 Section 的另一个并发批次读着，
改名抛 `PermissionError [WinError 5]`，异常从 `_run_batch` 中段冒出 —— 而当时的顺序是
"先写 attempt 台账，再落盘 CU"，于是整批已付费内容一起没落盘，清扫又把这一节判成
`partially_persisted` 跳过，正文再也回不来。同一场撞车还会把并发批次刚写的那条 attempt
记录整份覆盖掉（读—改—写不串行），使现场只剩一行 `RESPONSE_RECEIVED`，看起来像凭空丢字。

离线轮：只用 Synthetic Fixture + MockProvider，零真实 Provider 调用，零 Todd 付费调用。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
for item in (APP, ROOT / "tests"):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

import providers.base as provider_base  # noqa: E402
from longform.factory import LongformGenerationFactory, write_json  # noqa: E402
from providers.mock import MockProvider  # noqa: E402
from test_mmf007_final_closure import MOCK_CONFIG, _cu_factory  # noqa: E402


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


class AtomicReplaceTest(unittest.TestCase):
    """`os.replace` 的一次撞车只能重试，不能吞掉，也不能伪装成写成功。"""

    def test_transient_permission_error_is_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "governance.json"
            temp = Path(tmp) / "governance.json.tmp"
            temp.write_text('{"status": "BLOCK"}', encoding="utf-8")
            real = os.replace
            calls = {"n": 0}

            def flaky(src, dst):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise PermissionError(5, "Access is denied")
                return real(src, dst)

            with patch.object(provider_base.os, "replace", side_effect=flaky):
                provider_base.atomic_replace(temp, target)
            self.assertEqual(calls["n"], 3, "必须真的重试过，而不是第一次异常就当成功")
            self.assertEqual(_read(target)["status"], "BLOCK")

    def test_permanent_permission_error_still_raises_and_leaves_no_fake_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "qa.json"
            temp = Path(tmp) / "qa.json.tmp"
            temp.write_text('{"status": "BLOCK"}', encoding="utf-8")
            with patch.object(provider_base.os, "replace",
                              side_effect=PermissionError(5, "Access is denied")):
                with self.assertRaises(PermissionError):
                    provider_base.atomic_replace(temp, target)
            self.assertFalse(target.exists(), "改名没成功就不能留下'看起来已写'的目标")

    def test_write_json_reaches_the_same_code_path_for_both_sides(self):
        # 一把尺：长文侧的 write_json 必须走 providers.base 的原子写，否则退避只修了一半。
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "deep" / "fragment.json"
            write_json(target, {"body_blocks": [{"content": "秩序维护"}]})
            self.assertEqual(_read(target)["body_blocks"][0]["content"], "秩序维护")
            with patch.object(provider_base, "atomic_replace") as replaced:
                write_json(target, {"body_blocks": []})
            self.assertTrue(replaced.called, "长文侧写盘没有走同一条原子替换路径")


class MergeSectionAttemptsTest(unittest.TestCase):
    """同一逻辑 Section 的并发批次：attempt 台账按记录身份合并，不按"谁最后写"。"""

    def _bare_factory(self, run: Path) -> LongformGenerationFactory:
        factory = object.__new__(LongformGenerationFactory)
        factory.checkpoint_lock = threading.Lock()
        factory.word_root = run / "longform" / "word"
        return factory

    def test_concurrent_rows_are_both_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            factory = self._bare_factory(run)
            path = run / "longform" / "word" / "sections" / "S05-01" / "audit.json"
            path.parent.mkdir(parents=True)
            row_a = {"attempt_id": 1, "mode": "generate", "batch_id": "GB-01",
                     "request_id": "REQ-A", "status": "SUCCESS"}
            write_json(path, {"attempts": [row_a]})
            # worker B 在 A 落盘之前就读了旧账，所以它手里只有"空旧账 + 自己那条"。
            row_b = {"attempt_id": 2, "mode": "generate", "batch_id": "GB-02",
                     "request_id": "REQ-B", "status": "SUCCESS"}
            merged = factory._merge_section_attempts("S05-01", [row_b])
            self.assertEqual([row["request_id"] for row in _read(path)["attempts"]],
                             ["REQ-A", "REQ-B"], "后写的批次把先写的 attempt 记录覆盖掉了")
            self.assertEqual([row["request_id"] for row in merged], ["REQ-A", "REQ-B"])

    def test_replaying_the_same_rows_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            factory = self._bare_factory(run)
            rows = [{"attempt_id": 1, "mode": "generate", "batch_id": "GB-01",
                     "request_id": "REQ-A", "status": "SUCCESS"}]
            factory._merge_section_attempts("S01-01", rows)
            again = factory._merge_section_attempts("S01-01", rows + [rows[0]])
            self.assertEqual(len(_read(run / "longform/word/sections/S01-01/audit.json")["attempts"]), 1)
            self.assertEqual(len(again), 1)


class PaidContentSurvivesLedgerWriteFailureTest(unittest.TestCase):
    """台账写盘的失败不能连带把已返回的 CU 留在盘外、把请求账留在 `RESPONSE_RECEIVED`。"""

    def _ledger_rows(self, run: Path) -> list[dict]:
        path = run / "provider_unknown_requests.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_cu_content_and_request_closure_survive_a_failing_attempt_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            provider = MockProvider(MOCK_CONFIG)
            factory, sid, cu_ids = _cu_factory(run, provider)
            boom = {"raised": False}

            def explode(_self, _sid, rows):
                boom["raised"] = True
                raise PermissionError(5, "Access is denied (injected attempt-ledger collision)")

            with patch.object(LongformGenerationFactory, "_merge_section_attempts", explode):
                result = factory.generate_word([sid])
            self.assertTrue(boom["raised"], "注入点没被走到，这条测试就是空跑")
            cus_dir = run / "longform" / "word" / "sections" / sid / "cus"
            landed = sorted(p.stem for p in cus_dir.glob("CU-*.json")) if cus_dir.is_dir() else []
            self.assertEqual(landed, sorted(cu_ids),
                             "Provider 已返回的 CU 因后续台账写盘失败而没落盘 —— 这就是丢正文本体")
            last_status: dict[str, str] = {}
            for row in self._ledger_rows(run):
                last_status[str(row.get("request_id"))] = str(row.get("status"))
            self.assertTrue(last_status, "请求账必须存在，否则这条断言是空跑")
            open_rows = {rid: status for rid, status in last_status.items()
                         if status not in {"RESOLVED", "RESPONSE_REJECTED", "SUPERSEDED"}}
            self.assertEqual(open_rows, {},
                             "内容已落盘却还没结账：请求会永久停在 RESPONSE_RECEIVED，"
                             "清扫把它判成 partially_persisted 跳过后正文再也回不来")
            summary = result["content_unit_generation"]
            self.assertEqual(summary["completed"], len(cu_ids))


class BatchDirScopingTest(unittest.TestCase):
    """`batch_id` 只在本卷批次计划里唯一，落点却是全 Run 共享的 `word/batches/`。"""

    def _bare_factory(self, run: Path) -> LongformGenerationFactory:
        factory = object.__new__(LongformGenerationFactory)
        factory.word_root = run / "longform" / "word"
        return factory

    def test_same_batch_id_from_two_volumes_does_not_share_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            factory = self._bare_factory(Path(tmp))
            v05 = factory._batch_dir("GB-02", ["S05-01"])
            v07 = factory._batch_dir("GB-02", ["S09-01", "S09-02"])
            self.assertNotEqual(v05, v07,
                                "同名批次的 provider_raw.json / envelope_recovery.json 会互相覆盖")
            self.assertEqual(v05.name, "S05-01-GB-02")

    def test_reconcile_batch_without_sections_still_has_a_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            factory = self._bare_factory(Path(tmp))
            path = factory._batch_dir("GB-09", [])
            self.assertTrue(str(path).endswith("NOSECTION-GB-09"))


if __name__ == "__main__":
    unittest.main()
