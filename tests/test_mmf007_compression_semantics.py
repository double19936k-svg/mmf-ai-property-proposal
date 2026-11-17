"""MMF-007 Round 2 Blocker 2：Compression 正式入口语义验收。

验证链：Synthetic Longform → 正式入口 run_compression_pipeline →
compression_plan.json / compression_trace.json / compression_loss_report.json /
condensed DOCX。断言 JSON root 为 dict（非路径串）、语义内容、输出位置、
DOCX 可打开、trace 引用有效、loss 字段有效。
可由 Codex 单独运行：
  runtime/dev_python/Scripts/python.exe -m pytest tests/test_mmf007_compression_semantics.py -q
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(__file__).parent))

import test_mmf006e_r21_q01_q04 as r21  # noqa: E402
from providers.mock import MockProvider  # noqa: E402
from longform.orchestrator import generate_longform  # noqa: E402
from compression.planner import run_compression_pipeline  # noqa: E402

MOCK_CONFIG = {"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock-semantics"}


class CompressionSemanticsTest(unittest.TestCase):
    out_dir = None
    outcome = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        brief = {
            **r21._brief(),
            "project_type": "产业园综合体",
            "scenario": "完整物业服务方案",
            "document_scale": "regular_solution",
        }
        run = r21._write_run(Path(cls.tmp.name), r21._complex_pack(), brief, r21._selection())
        cls.run_dir = run
        # 使用正式常规方案档位验证压缩入口；超长档位的预算/CU扩展由
        # document-scale规划测试独立覆盖，避免在压缩语义测试中重复造长文。
        generate_longform(
            run_dir=run, provider=MockProvider(MOCK_CONFIG), provider_name="mock", brief=brief,
            selection=r21._selection(), selected_ids=["KU-0001"], task_mode="full_longform",
            require_section_min=True, speed_profile="fast")
        cls.outcome = run_compression_pipeline(run, target_pages=(30, 60))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_01_json_files_are_content_dicts_not_paths(self):
        """Codex 复现项：JSON 正文必须是内容对象，不得是路径字符串。"""
        out_dir = Path(self.outcome["out_dir"])
        for name in ("compression_plan.json", "compression_trace.json", "compression_loss_report.json"):
            parsed = json.loads((out_dir / name).read_text(encoding="utf-8-sig"))
            self.assertIsInstance(parsed, (dict, list), f"{name} root 必须是 dict/list")
            raw = (out_dir / name).read_text(encoding="utf-8-sig").strip()
            self.assertFalse(raw.endswith(".json\"") and len(raw) < 300, f"{name} 疑似只含路径串")

    def test_02_plan_has_section_plan_semantics(self):
        plan = self.outcome["plan"]
        self.assertEqual(plan["schema_version"], "compression-plan-v0.1")
        rows = plan["plan_rows"]
        self.assertGreater(len(rows), 0)
        for row in rows:
            self.assertIn(row["compression_mode"], {"KEEP_FULL", "SUMMARY_ONLY"})
            self.assertIsInstance(row["target_chars"], int)
            self.assertTrue(row["source_section_ids"])
        self.assertIn("estimated_pages", plan["summary"])

    def test_03_trace_references_valid(self):
        trace = self.outcome["trace"]
        self.assertEqual(trace["schema_version"], "compression-trace-v0.1")
        plan_ids = {row["section_id"] for row in self.outcome["plan"]["plan_rows"]}
        for row in trace["rows"]:
            self.assertIn(row["section_id"], plan_ids, "trace 引用必须存在于 plan")
            for vid in row["source_volume_ids"]:
                self.assertTrue(vid.startswith("V"), vid)

    def test_04_loss_report_fields_valid(self):
        loss = self.outcome["loss_check"]
        self.assertEqual(loss["schema_version"], "compression-loss-check-v0.1")
        self.assertIn(loss["status"], {"PASS", "CORE_LOSS_DETECTED"})
        self.assertIsInstance(loss["CORE_SCORING_LOSS"], int)
        self.assertIsInstance(loss["CORE_MUST_LOSS"], int)
        self.assertEqual(loss["status"], "PASS", f"核心内容不得丢失: {loss}")

    def test_05_outputs_confined_to_run_compression_dir(self):
        """输出必须进入 run/compression/，禁止污染 cwd/项目根/temp 未知路径。"""
        out_dir = Path(self.outcome["out_dir"])
        self.assertTrue(out_dir.is_absolute())
        self.assertIn("compression", out_dir.parts)
        for meta in self.outcome["paths"].values():
            self.assertTrue(str(out_dir) in meta["output_path"])
            self.assertGreater(meta["bytes"], 2, "不得是空文件或纯路径串")

    def test_06_condensed_docx_opens_and_within_target(self):
        docx_path = self.outcome["condensed_docx"]
        self.assertIsNotNone(docx_path)
        from docx import Document
        doc = Document(docx_path)
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        self.assertGreater(len(paragraphs), 0)
        estimated = self.outcome["plan"]["summary"]["estimated_pages"]
        self.assertGreaterEqual(estimated, 10)
        self.assertLessEqual(estimated, 60)

    def test_07_json_readback_flag(self):
        self.assertTrue(self.outcome.get("json_readback_valid"))


if __name__ == "__main__":
    unittest.main()
