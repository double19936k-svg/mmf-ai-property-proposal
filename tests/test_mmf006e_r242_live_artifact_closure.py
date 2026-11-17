from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from docx import Document


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import desktop_actions  # noqa: E402
import workflow_timing  # noqa: E402
from governance.final_artifact_qa import (  # noqa: E402
    evaluate_docx_artifact,
    quote_serialization_status,
    repair_docx_artifact,
)


class LiveWorkflowTimingTests(unittest.TestCase):
    def test_time_live_01_active_stage_is_in_shared_total(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            run_dir = Path(folder) / "run"
            run_dir.mkdir()
            workflow_timing.record_stage_duration(run_dir, "completed", 105)
            with patch.object(workflow_timing.time, "monotonic", return_value=1000.0):
                workflow_timing.set_active_stage(run_dir, "longform_generation")
            with patch.object(workflow_timing.time, "monotonic", return_value=1110.0):
                live = workflow_timing.load_workflow_timing(run_dir)
                history = workflow_timing.history_duration(run_dir)
            self.assertEqual(live["current_total_processing_seconds"], 215.0)
            self.assertEqual(history["total_processing_seconds"], 215)
            self.assertEqual(live["current_active_stage_elapsed_seconds"], 110.0)
            workflow_timing.clear_active_stage(run_dir)

    def test_time_live_02_idle_wall_clock_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            run_dir = Path(folder) / "run"
            run_dir.mkdir()
            workflow_timing.record_stage_duration(run_dir, "completed", 105)
            with patch.object(workflow_timing.time, "monotonic", return_value=1600.0):
                live = workflow_timing.load_workflow_timing(run_dir)
            self.assertEqual(live["current_total_processing_seconds"], 105.0)
            self.assertEqual(live["current_active_stage_elapsed_seconds"], 0.0)

    def test_generation_page_uses_server_processing_total(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("current_total_processing_seconds", html)
        self.assertIn("genProcessingSeconds", html)


class OpenFolderTests(unittest.TestCase):
    def _roots(self, root: Path) -> SimpleNamespace:
        return SimpleNamespace(runs_dir=root / "runs", output_root=root / "output")

    def test_folder_01_known_run_output_opens_explorer_without_shell(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            roots = self._roots(root)
            (roots.runs_dir / "run_01").mkdir(parents=True)
            (roots.output_root / "run_01").mkdir(parents=True)
            with patch.object(desktop_actions.paths, "current", return_value=roots), patch.object(desktop_actions.subprocess, "Popen") as popen:
                result = desktop_actions.open_run_folder("run_01", "output")
            self.assertTrue(result["ok"])
            self.assertEqual(popen.call_args.kwargs["shell"], False)
            self.assertEqual(popen.call_args.args[0][0], "explorer.exe")

    def test_folder_02_missing_output_has_explicit_error(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            roots = self._roots(root)
            (roots.runs_dir / "run_01").mkdir(parents=True)
            roots.output_root.mkdir(parents=True)
            with patch.object(desktop_actions.paths, "current", return_value=roots):
                with self.assertRaisesRegex(FileNotFoundError, "不存在或已被移动"):
                    desktop_actions.open_run_folder("run_01", "output")

    def test_folder_03_path_traversal_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            roots = self._roots(Path(folder))
            roots.runs_dir.mkdir(parents=True)
            roots.output_root.mkdir(parents=True)
            with patch.object(desktop_actions.paths, "current", return_value=roots):
                with self.assertRaises(PermissionError):
                    desktop_actions.open_run_folder("../../../Windows", "output")


class ProductionArtifactQATests(unittest.TestCase):
    def test_quote_pipeline_status_is_auditable_before_render(self) -> None:
        report = quote_serialization_status({"artifact": {"paragraphs": ["分区分“ ”类"]}})
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["empty_or_adjacent_count"], 1)

    def _save(self, build) -> Path:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "artifact.docx"
        doc = Document()
        build(doc)
        doc.save(path)
        return path

    def test_art_prod_01_empty_quote_fails(self) -> None:
        path = self._save(lambda doc: doc.add_paragraph("分区分类“ ”差异化管控。"))
        report = evaluate_docx_artifact(path)
        self.assertEqual(report["status"], "BLOCK")
        self.assertTrue(any(row["rule_id"] == "BROKEN_QUOTE_SERIALIZATION" for row in report["findings"]))

    def test_art_prod_02_adjacent_headings_fail(self) -> None:
        """§29 收口（Round 3 P0-3）：原标题下的 18 字短正文是"不完整"，不是"空"。

        判成 EMPTY_HEADING_BLOCK 会让 REMOVE_EMPTY_HEADING 删掉标题、把正文孤儿化，
        所以这里必须落在 THIN_HEADING_BODY / NEEDS_REVIEW，且修复后标题仍在。
        """
        def build(doc):
            doc.add_heading("关键岗位职责边界", level=1)
            doc.add_heading("跨专业协同与升级机制", level=2)
            doc.add_paragraph("建立跨专业事项发起、流转与关闭机制。")
        path = self._save(build)
        report = evaluate_docx_artifact(path)
        rules = {row["rule_id"] for row in report["findings"]}
        self.assertNotIn("EMPTY_HEADING_BLOCK", rules)
        thin = [row for row in report["findings"] if row["rule_id"] == "THIN_HEADING_BODY"]
        self.assertTrue(thin)
        self.assertEqual({row["qa_class"] for row in thin}, {"NEEDS_REVIEW"})
        self.assertEqual(report["status"], "NEEDS_REVIEW")
        repair_docx_artifact(path)
        titles = [p.text for p in Document(str(path)).paragraphs]
        self.assertIn("关键岗位职责边界", titles)
        self.assertIn("跨专业协同与升级机制", titles)

    def test_art_prod_03_unsupported_attachment_claim_fails(self) -> None:
        path = self._save(lambda doc: doc.add_paragraph("其简历及资历评价详见附表。"))
        report = evaluate_docx_artifact(path)
        self.assertTrue(any(row["rule_id"] == "UNSUPPORTED_COMPLETION_CLAIM" for row in report["findings"]))

    def test_art_prod_04_internal_section_id_fails(self) -> None:
        path = self._save(lambda doc: doc.add_paragraph("具体安排参见S03-02。"))
        report = evaluate_docx_artifact(path)
        self.assertTrue(any(row["rule_id"] == "INTERNAL_SECTION_ID" for row in report["findings"]))

    def test_art_prod_05_repaired_docx_is_reopened_and_passes(self) -> None:
        def build(doc):
            doc.add_heading("关键岗位职责边界", level=1)
            doc.add_heading("跨专业协同与升级机制", level=2)
            doc.add_paragraph("其简历及资历评价详见附表，并参见S03-02。“ ”")
        path = self._save(build)
        self.assertEqual(evaluate_docx_artifact(path)["status"], "BLOCK")
        repair_docx_artifact(path)
        report = evaluate_docx_artifact(path)
        self.assertEqual(report["status"], "PASS", report["findings"])
        self.assertTrue(report["actual_docx_inspected"])


if __name__ == "__main__":
    unittest.main()
