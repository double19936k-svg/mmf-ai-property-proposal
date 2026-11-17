from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from docx import Document


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
FIXTURE = ROOT / "tests" / "fixtures" / "qwen_fail_soft_20260910"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from delivery_state import (  # noqa: E402
    DELIVERY_CLEAN,
    DELIVERY_REVIEW,
    DELIVERY_TECHNICAL,
    DELIVERY_WARNINGS,
    artifact_physically_invalid,
    classify_fail_soft,
    contains_internal_code,
    public_message,
    record_last_valid_artifact,
    restore_last_valid_artifact,
)
from recovery_orchestrator import recover_governance  # noqa: E402


def _docx(path: Path, paragraphs: list[str] | None = None) -> Path:
    doc = Document()
    for text in paragraphs or ["青岛海信国际中心物业顾问服务项目主体内容。", "安全、工程与客服安排已按招标范围编写。"]:
        doc.add_paragraph(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


class FailSoftDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_soft_01_repaired_commitment_is_completed_clean(self) -> None:
        generated = {"artifact": {"paragraphs": ["所有人员资历证明已整理成册。"]}}
        result = recover_governance(generated, brief={"requirements": "物业服务"}, positives=[], guardrails=[], contracts=[])
        path = _docx(self.folder / "clean.docx")
        decision = classify_fail_soft(
            artifact_path=path,
            findings=[],
            artifact_qa=result["artifact_qa"],
            commitment=result["commitment"],
            compliance=result["compliance"],
            repaired=True,
            repair_attempts=result["repair_count"],
        )
        self.assertEqual(decision["delivery_status"], DELIVERY_CLEAN)
        self.assertTrue(decision["downloadable"])

    def test_soft_02_unrepaired_commitment_is_needs_review(self) -> None:
        path = _docx(self.folder / "review.docx")
        decision = classify_fail_soft(
            artifact_path=path,
            findings=[{"rule_id": "UNSUPPORTED_COMPLETION_CLAIM", "severity": "BLOCK", "text": "作为本方案附件提交", "scope": "sentence"}],
            repair_attempts=2,
        )
        self.assertEqual(decision["delivery_status"], DELIVERY_REVIEW)
        self.assertTrue(decision["artifact_available"])
        self.assertTrue(decision["downloadable"])
        self.assertNotEqual(decision["delivery_status"], DELIVERY_TECHNICAL)
        self.assertFalse(contains_internal_code(decision["public_message"]))

    def test_soft_03_five_mild_warnings_are_completed_with_warnings(self) -> None:
        path = _docx(self.folder / "warn.docx")
        findings = [{"rule_id": "INTERNAL_GOVERNANCE_TONE", "severity": "WARNING", "text": f"措辞{i}", "scope": "sentence"} for i in range(5)]
        decision = classify_fail_soft(artifact_path=path, findings=findings)
        self.assertEqual(decision["delivery_status"], DELIVERY_WARNINGS)
        self.assertEqual(decision["review_count"], 5)
        self.assertTrue(decision["downloadable"])

    def test_soft_04_broken_cross_reference_is_needs_review_not_technical(self) -> None:
        path = _docx(self.folder / "xref.docx")
        decision = classify_fail_soft(
            artifact_path=path,
            findings=[{"rule_id": "BROKEN_CROSS_REFERENCE", "severity": "BLOCK", "text": "相关章节", "scope": "paragraph"}],
            repair_attempts=2,
        )
        self.assertEqual(decision["delivery_status"], DELIVERY_REVIEW)
        self.assertNotEqual(decision["delivery_status"], DELIVERY_TECHNICAL)

    def test_soft_05_corrupt_docx_is_technical_failed(self) -> None:
        path = self.folder / "broken.docx"
        path.write_bytes(b"not-a-docx")
        decision = classify_fail_soft(artifact_path=path, findings=[], renderer_failed=False)
        self.assertEqual(decision["delivery_status"], DELIVERY_TECHNICAL)
        self.assertFalse(decision["downloadable"])

    def test_soft_06_five_of_thirty_sections_is_technical_failed(self) -> None:
        decision = classify_fail_soft(
            artifact_qa={"longform_depth": {"metrics": {"planned_sections": 30, "generated_sections": 5, "missing_sections": [f"S{i:02d}-01" for i in range(6, 31)]}}},
            findings=[{"rule_id": "LF-002", "severity": "BLOCK"}],
        )
        self.assertEqual(decision["delivery_status"], DELIVERY_TECHNICAL)

    def test_soft_07_last_valid_artifact_is_retained_after_qa_fail(self) -> None:
        run_dir = self.folder / "run"
        source = _docx(run_dir / "keep.docx")
        meta = record_last_valid_artifact(run_dir, source)
        self.assertTrue(Path(meta["last_valid_artifact_path"]).is_file())
        decision = classify_fail_soft(
            artifact_path=source,
            findings=[{"rule_id": "EMPTY_HEADING_BLOCK", "severity": "BLOCK", "text": "空标题"}],
            repair_attempts=2,
        )
        self.assertEqual(decision["delivery_status"], DELIVERY_REVIEW)
        self.assertTrue(Path(meta["last_valid_artifact_path"]).is_file())
        self.assertTrue(source.is_file())

    def test_soft_08_repair_failure_rolls_back_last_valid(self) -> None:
        run_dir = self.folder / "run8"
        source = _docx(run_dir / "live.docx", ["保留这份可打开正文。"])
        record_last_valid_artifact(run_dir, source)
        source.write_bytes(b"corrupt-after-failed-repair")
        self.assertTrue(artifact_physically_invalid(source))
        self.assertTrue(restore_last_valid_artifact(run_dir, source))
        self.assertFalse(artifact_physically_invalid(source))

    def test_soft_09_needs_review_history_keeps_download_and_folder(self) -> None:
        path = _docx(self.folder / "hist.docx")
        decision = classify_fail_soft(
            artifact_path=path,
            findings=[{"rule_id": "UNSUPPORTED_COMPLETION_CLAIM", "severity": "BLOCK", "text": "附件已提交"}],
        )
        self.assertEqual(decision["history_label"], "待人工复核")
        self.assertTrue(decision["downloadable"])
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("打开文件夹", html)
        self.assertIn("下载待复核版本", html)

    def test_soft_10_frontend_hides_internal_codes(self) -> None:
        message = public_message(DELIVERY_REVIEW, 3)
        self.assertFalse(contains_internal_code(message))
        self.assertIn("人工复核", message)
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("KNOWLEDGE_COMMITMENT_GOVERNANCE_FAILED", html)
        self.assertNotIn("FINAL_ARTIFACT_QA_FAILED", html)
        self.assertIn("public_label", html)
        self.assertIn("notice error", html)

    def test_qwen_real_run_fail_soft_replay(self) -> None:
        snapshot = json.loads((FIXTURE / "final_qa_findings.json").read_text(encoding="utf-8"))
        source = FIXTURE / "blocked_artifact.docx"
        self.assertTrue(source.is_file())
        self.assertFalse(artifact_physically_invalid(source))
        copy = self.folder / "replay.docx"
        shutil.copy2(source, copy)
        Document(str(copy))
        decision = classify_fail_soft(
            artifact_path=copy,
            findings=snapshot["findings"],
            artifact_qa={"longform_depth": {"metrics": {
                "planned_sections": snapshot["planned_sections"],
                "generated_sections": snapshot["generated_sections"],
                "missing_sections": [],
            }}},
            repair_attempts=2,
        )
        self.assertNotEqual(decision["delivery_status"], DELIVERY_TECHNICAL)
        self.assertIn(decision["delivery_status"], {DELIVERY_REVIEW, DELIVERY_WARNINGS})
        self.assertTrue(decision["artifact_available"])
        self.assertTrue(decision["downloadable"])
        self.assertGreaterEqual(decision["review_count"], 1)
        self.assertFalse(contains_internal_code(decision["public_message"]))
        self.assertTrue(copy.is_file())
        frozen = json.loads((FIXTURE / "frozen_run_audit.json").read_text(encoding="utf-8"))
        self.assertEqual(frozen.get("status"), "final_artifact_blocked")


if __name__ == "__main__":
    unittest.main()
