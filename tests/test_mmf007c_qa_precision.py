"""Round 3 P0-3（§28–§33 / §49）：QA 精度 —— 空标题判据、四类严重性、否定句识别。

这里既有 §49 点名的 QA-EMPTY-01/02，也把 §31/§32/§33 的三条红线钉成测试：
① 真空标题必须仍然是 TRUE_BLOCK；② FALSE_POSITIVE 必须带可复核理由，不能只贴标签；
③ "不/未/被当成/条件句"里的命中不得当成主张。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from docx import Document

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from governance.commitment_provenance import _repair_text, evaluate_commitments  # noqa: E402
from governance.consistency import check_cross_section_consistency  # noqa: E402
from governance.final_artifact_qa import (  # noqa: E402
    _body_kind,
    _docx_blocks,
    assess_headings,
    evaluate_docx_artifact,
    repair_docx_artifact,
)
from planning.content_engineering import evaluate_bidder_evidence  # noqa: E402
from qa_severity import classify_finding, classify_findings  # noqa: E402
import delivery_state  # noqa: E402


def _save(build) -> Path:
    folder = tempfile.TemporaryDirectory()
    path = Path(folder.name) / "artifact.docx"
    doc = Document()
    build(doc)
    doc.save(path)
    _save.cleanup.append(folder)
    return path


_save.cleanup = []


class EmptyHeadingJudgementTests(unittest.TestCase):
    """§29 的"空"＝标题到下一个同级或更高级标题之间没有正文/表格/清单/流程。"""

    def setUp(self) -> None:
        self.addCleanup(lambda: [f.cleanup() for f in _save.cleanup])
        _save.cleanup.clear()

    def test_qa_empty_01_heading_followed_by_table_is_not_empty(self) -> None:
        def build(doc):
            doc.add_heading("保洁作业标准", level=1)
            table = doc.add_table(rows=2, cols=2)
            table.cell(0, 0).text = "区域"
            table.cell(0, 1).text = "频次"
            table.cell(1, 0).text = "大堂"
            table.cell(1, 1).text = "每日两次"
            doc.add_heading("秩序维护", level=1)
            doc.add_paragraph("按分区明确值守责任与交接记录要求，形成可核查的台账。")
        path = _save(build)
        blocks = _docx_blocks(path)
        table_block = next(row for row in blocks if row.get("kind") == "table")
        self.assertEqual("TABLE", _body_kind(table_block))
        report = evaluate_docx_artifact(path)
        rules = {row["rule_id"] for row in report["findings"]}
        self.assertNotIn("EMPTY_HEADING_BLOCK", rules)
        self.assertNotIn("HEADING_WITHOUT_SUBSTANTIVE_BODY", rules)
        self.assertNotIn("THIN_HEADING_BODY", rules)

    def test_qa_empty_02_heading_followed_by_list_is_not_empty(self) -> None:
        list_block = {"path": "paragraph[1]", "kind": "paragraph", "text": "1. 核对图纸与点位",
                      "style": "List Number", "heading_candidate": False, "paragraph_index": 1}
        self.assertEqual("LIST", _body_kind(list_block))
        blocks = [
            {"path": "paragraph[0]", "kind": "paragraph", "text": "进场准备", "style": "Heading 1", "heading_level": 1, "heading_candidate": True, "paragraph_index": 0},
            list_block,
            {"path": "paragraph[2]", "kind": "paragraph", "text": "2. 建立临时交接点", "style": "List Number", "heading_candidate": False, "paragraph_index": 2},
            {"path": "paragraph[3]", "kind": "paragraph", "text": "秩序维护", "style": "Heading 1", "heading_level": 1, "heading_candidate": True, "paragraph_index": 3},
            {"path": "paragraph[4]", "kind": "paragraph", "text": "按门岗与巡更两条线明确职责，交接记录随台账留存备查。", "style": "Normal", "heading_candidate": False, "paragraph_index": 4},
        ]
        audit = assess_headings(blocks)
        self.assertEqual([], [row["rule_id"] for row in audit["findings"]])

    def test_qa_empty_03_true_empty_heading_still_blocks(self) -> None:
        def build(doc):
            doc.add_heading("关键岗位职责边界", level=1)
            doc.add_heading("跨专业协同与升级机制", level=1)
            doc.add_paragraph("建立跨专业事项发起、流转与关闭的机制，并保留可核查记录。")
        path = _save(build)
        report = evaluate_docx_artifact(path)
        rows = [row for row in report["findings"] if row["rule_id"] == "EMPTY_HEADING_BLOCK"]
        self.assertTrue(rows)
        self.assertEqual({"TRUE_BLOCK"}, {row["qa_class"] for row in rows})
        self.assertEqual("BLOCK", report["status"])
        self.assertEqual("REMOVE_EMPTY_HEADING", rows[0]["repair_action"])
        repair_docx_artifact(path)
        titles = [p.text for p in Document(str(path)).paragraphs]
        self.assertNotIn("关键岗位职责边界", titles)
        self.assertIn("跨专业协同与升级机制", titles)

    def test_qa_empty_04_volume_divider_is_false_positive_with_reason(self) -> None:
        blocks = [
            {"path": "paragraph[0]", "kind": "paragraph", "text": "第一章 项目理解", "style": "Heading 1", "heading_level": 1, "heading_candidate": True, "paragraph_index": 0},
            {"path": "paragraph[1]", "kind": "paragraph", "text": "1.1 服务范围", "style": "Heading 2", "heading_level": 2, "heading_candidate": True, "paragraph_index": 1},
            {"path": "paragraph[2]", "kind": "paragraph", "text": "本项目服务范围覆盖全部楼栋及地下空间的日常物业管理工作。", "style": "Normal", "heading_candidate": False, "paragraph_index": 2},
        ]
        audit = assess_headings(blocks)
        self.assertEqual([], audit["findings"])
        row = next(x for x in audit["rejected_candidates"] if x["text"].startswith("第一章"))
        self.assertEqual("FALSE_POSITIVE", row["qa_class"])
        self.assertIn("VOLUME_DIVIDER", row["classification_reason"])

    def test_qa_empty_05_front_matter_and_toc_are_not_headings(self) -> None:
        blocks = [
            {"path": "paragraph[0]", "kind": "paragraph", "text": "目 录", "style": "Heading 1", "heading_level": 1, "heading_candidate": True, "paragraph_index": 0},
            {"path": "paragraph[1]", "kind": "paragraph", "text": "第一章 项目理解\t3", "style": "TOC 1", "heading_candidate": True, "paragraph_index": 1},
            {"path": "paragraph[2]", "kind": "paragraph", "text": "物业服务方案", "style": "Title", "heading_candidate": True, "paragraph_index": 2},
            {"path": "paragraph[3]", "kind": "paragraph", "text": "1 项目理解", "style": "Heading 1", "heading_level": 1, "heading_candidate": True, "paragraph_index": 3},
            {"path": "paragraph[4]", "kind": "paragraph", "text": "以下为本章正文内容，说明服务范围与管理目标的确定过程及依据。", "style": "Normal", "heading_candidate": False, "paragraph_index": 4},
        ]
        audit = assess_headings(blocks)
        self.assertEqual([], audit["findings"])
        reasons = {row["text"]: row["classification_reason"] for row in audit["rejected_candidates"]}
        toc_row = next(key for key in reasons if key.startswith("第一章 项目理解"))
        self.assertIn("TOC_ENTRY", reasons[toc_row])

    def test_qa_empty_06_thin_body_is_needs_review_not_empty(self) -> None:
        blocks = [
            {"path": "paragraph[0]", "kind": "paragraph", "text": "风险识别", "style": "Heading 1", "heading_level": 1, "heading_candidate": True, "paragraph_index": 0},
            {"path": "paragraph[1]", "kind": "paragraph", "text": "识别主要风险。", "style": "Normal", "heading_candidate": False, "paragraph_index": 1},
            {"path": "paragraph[2]", "kind": "paragraph", "text": "应急管理", "style": "Heading 1", "heading_level": 1, "heading_candidate": True, "paragraph_index": 2},
            {"path": "paragraph[3]", "kind": "paragraph", "text": "明确响应流程与记录要求，确保过程可核查、结果可复核。", "style": "Normal", "heading_candidate": False, "paragraph_index": 3},
        ]
        audit = assess_headings(blocks)
        rules = {row["rule_id"] for row in audit["findings"]}
        self.assertEqual({"THIN_HEADING_BODY"}, rules)
        row = audit["findings"][0]
        self.assertEqual("NEEDS_REVIEW", row["qa_class"])
        self.assertLess(row["body_chars"], 20)


class FourClassClassificationTests(unittest.TestCase):
    """§32：先分对类，再决定 Delivery；FALSE_POSITIVE 必须自带理由。"""

    def test_qa_class_01_false_positive_without_reason_falls_back_to_review(self) -> None:
        row = classify_finding({"rule_id": "EMPTY_HEADING_BLOCK", "severity": "BLOCK", "qa_class": "FALSE_POSITIVE"})
        self.assertEqual("NEEDS_REVIEW", row["qa_class"])
        self.assertEqual("BLOCK", row["severity"])
        self.assertTrue(row["classification_reason"])

    def test_qa_class_02_conflict_resolves_to_stricter_class(self) -> None:
        # 把 BLOCK 改标 WARNING 来制造通过 —— 取更严的那一档挡掉。
        row = classify_finding({
            "rule_id": "INTERNAL_SECTION_ID", "severity": "BLOCK",
            "qa_class": "WARNING", "classification_reason": "只是风格提示",
        })
        self.assertEqual("TRUE_BLOCK", row["qa_class"])
        self.assertTrue(row["delivery_blocking"])
        # 但规则用可复核证据自证"这不是缺陷"时，允许推翻自己先前声明的 BLOCK，并留痕。
        proven = classify_finding({
            "rule_id": "EMPTY_HEADING_BLOCK", "severity": "BLOCK",
            "qa_class": "FALSE_POSITIVE", "classification_reason": "VOLUME_DIVIDER：一级章节页",
        })
        self.assertEqual("FALSE_POSITIVE", proven["qa_class"])
        self.assertEqual("BLOCK", proven["severity_overridden_by_evidence"])
        self.assertFalse(proven["delivery_blocking"])

    def test_qa_class_03_counts_and_false_positive_rate(self) -> None:
        report = classify_findings([
            {"rule_id": "EMPTY_HEADING_BLOCK", "severity": "BLOCK", "qa_class": "TRUE_BLOCK"},
            {"rule_id": "C2", "severity": "BLOCK", "qa_class": "FALSE_POSITIVE", "classification_reason": "negation_aware:DENIED"},
            {"rule_id": "BROKEN_QUOTE_SERIALIZATION", "severity": "WARNING", "qa_class": "WARNING"},
        ])
        self.assertEqual(2, len(report["findings"]))
        self.assertEqual(1, len(report["false_positives"]))
        self.assertEqual(1, report["summary"]["counts"]["FALSE_POSITIVE"])
        self.assertEqual(1, report["summary"]["false_positive_overriding_block"])
        self.assertAlmostEqual(1 / 3, report["summary"]["false_positive_rate"], places=3)
        self.assertEqual("TRUE_BLOCK", report["summary"]["status"])

    def test_qa_class_04_delivery_reads_class_not_rule_name(self) -> None:
        rows = [
            {"rule_id": "EMPTY_HEADING_BLOCK", "severity": "BLOCK", "qa_class": "FALSE_POSITIVE",
             "classification_reason": "VOLUME_DIVIDER", "text": "第一章"},
            {"rule_id": "BROKEN_QUOTE_SERIALIZATION", "severity": "WARNING", "qa_class": "WARNING", "text": "弯引号"},
        ]
        issues = delivery_state.remaining_issues({"findings": rows})
        self.assertEqual(["BROKEN_QUOTE_SERIALIZATION"], [row["rule_id"] for row in issues])

        def build(doc):
            doc.add_heading("第一章 项目理解", level=1)
            doc.add_paragraph("说明服务范围与管理目标的确定过程及依据。")
        result = delivery_state.classify_fail_soft(findings=issues, artifact_path=_save(build), repaired=True)
        self.assertEqual("COMPLETED_WITH_WARNINGS", result["delivery_status"])
        self.assertEqual(0, result["true_block_count"])

    def test_qa_class_05_true_block_still_delivers_but_as_review(self) -> None:
        def build(doc):
            doc.add_heading("标题", level=1)
            doc.add_paragraph("正文内容说明。")
        issues = [{"rule_id": "EMPTY_HEADING_BLOCK", "severity": "BLOCK", "qa_class": "TRUE_BLOCK", "text": "空标题"}]
        result = delivery_state.classify_fail_soft(findings=issues, artifact_path=_save(build))
        self.assertEqual("REVIEW_REQUIRED", result["qa_status"])
        self.assertEqual("NEEDS_REVIEW", result["delivery_status"])
        self.assertTrue(result["artifact_available"])
        self.assertTrue(result["downloadable"])
        self.assertEqual(1, result["true_block_count"])

    def test_qa_class_06_auto_repair_severity_label_survives_classification(self) -> None:
        # AUTO_REPAIR 与 NEEDS_REVIEW 同档但是两种口径：表格门读的是"文件可交付、要自动修补"。
        row = classify_finding({"rule_id": "TABLE_EMPTY_SCAFFOLD", "severity": "AUTO_REPAIR"})
        self.assertEqual("NEEDS_REVIEW", row["qa_class"])
        self.assertEqual("AUTO_REPAIR", row["severity"])
        self.assertFalse(row["delivery_blocking"])

    def test_qa_class_07_report_counts_every_false_positive_channel(self) -> None:
        def build(doc):
            doc.add_heading("第一章 项目理解", level=1)
            doc.add_paragraph("本方案不因程序设计而新增固定岗位，人员编制按现场台账口径执行。")
        report = evaluate_docx_artifact(_save(build))
        classification = report["qa_classification"]
        self.assertGreater(classification["false_positive_channels"]["commitment_suppressed"], 0)
        self.assertEqual(report["false_positive_count"], classification["counts"]["FALSE_POSITIVE"])
        self.assertEqual(
            report["false_positive_count"], sum(classification["false_positive_channels"].values()))
        self.assertGreater(classification["false_positive_rate"], 0.0)
        self.assertEqual(
            len(report["findings"]) + report["false_positive_count"], classification["adjudicated_total"])
        self.assertEqual("PASS", report["status"])


    """§33：否定、引述、条件句里的命中不是主张。"""

    def test_qa_neg_01_denied_commitment_is_suppressed_with_reason(self) -> None:
        report = evaluate_commitments({}, [], {"artifact": {"paragraphs": [
            "本方案不因程序设计而新增固定岗位，人员编制按现场台账口径执行。",
        ]}})
        self.assertEqual([], [row for row in report["violations"] if row.get("claim_type") == "C2"])
        self.assertTrue(report["suppressed"])
        self.assertIn("DENIED", report["suppressed"][0]["classification_reason"])
        self.assertTrue(report["negation_aware"])

    def test_qa_neg_02_asserted_commitment_still_blocks(self) -> None:
        report = evaluate_commitments({}, [], {"artifact": {"paragraphs": [
            "我司在各城区均设立分公司，并配备30人的自有平台呼叫中心团队。",
        ]}})
        self.assertTrue(report["violations"])
        self.assertEqual("BLOCK", report["status"])
        self.assertTrue(all(row["qa_class"] == "TRUE_BLOCK" for row in report["violations"]))

    def test_qa_neg_03_mention_and_conditional_are_not_claims(self) -> None:
        for text in (
            "该表述容易被当成已经完成，需要与招标人确认后闭环。",
            "无论准备是否已经完成，均应按现行制度上报审批权限内的事项。",
        ):
            report = evaluate_commitments({}, [], {"artifact": {"paragraphs": [text]}})
            self.assertFalse([row for row in report["violations"]
                              if row.get("claim_type") == "UNSUPPORTED_COMPLETION_CLAIM"], text)

    def test_qa_neg_04_defined_object_is_needs_review_not_block(self) -> None:
        report = evaluate_commitments({}, [], {"artifact": {"paragraphs": [
            "必须上报的范围、审批的事项清单见本节表格。",
        ]}})
        self.assertEqual([], [row for row in report["violations"] if row.get("claim_type") == "C6"])
        self.assertTrue([row for row in report["review_items"] if row.get("claim_type") == "C6"])
        self.assertEqual("NEEDS_REVIEW", report["status"])

    def test_qa_neg_05_bidder_claim_disclaimer_is_suppressed(self) -> None:
        artifact = {"sections": [
            {"body": "本方案不代表投标人具备同类项目业绩，具体材料以正式提交文件为准。"},
            {"body": "投标人已具备类似项目业绩，可直接采信。"},
        ]}
        report = evaluate_bidder_evidence(artifact, {"rows": []})
        self.assertEqual(1, report["unsupported_claim_count"])
        self.assertIn("negation_aware:DENIED", [row["classification_reason"] for row in report["suppressed"]])
        self.assertEqual("NEEDS_REVIEW", report["findings"][0]["qa_class"])

    def test_qa_neg_06_consistency_keeps_quantifier_and_drops_denial(self) -> None:
        fragments = {"S1": {"body_blocks": [
            {"type": "paragraph", "text": "人员配置不少于30人，满足峰值需要。"},
            {"type": "paragraph", "text": "本项目不会人员配置60人。"},
        ]}}
        global_state = {"project_facts": {"team_headcount": {"value": "52"}}}
        report = check_cross_section_consistency(fragments, global_state)
        self.assertEqual(["30"], [row["observed"] for row in report["issues"]])
        self.assertEqual(["60"], [row["observed"] for row in report["suppressed"]])


class RepairTermPreservationTests(unittest.TestCase):
    """修复端与门槛同口径：中性技术词不能被无条件擦掉。"""

    def test_qa_term_01_scoring_vocabulary_survives_without_claim(self) -> None:
        text = "各类报修的响应时限与到场时限在岗位说明书中分条列明。"
        self.assertEqual(text, _repair_text(text))

    def test_qa_term_02_scoped_when_the_class_actually_fires(self) -> None:
        repaired = _repair_text("报修后 30 分钟内到场，响应时限按此执行。")
        self.assertNotIn("响应时限", repaired)
        self.assertIn("响应安排", repaired)


if __name__ == "__main__":
    unittest.main(verbosity=2)
