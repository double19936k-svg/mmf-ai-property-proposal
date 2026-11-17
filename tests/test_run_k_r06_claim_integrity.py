# -*- coding: utf-8 -*-
"""R06 完成主张与数量一致性差分测试。

覆盖三类断言，全部落在终审权威入口 ``evaluate_docx_artifact``（缺口1）
与独立模块 ``governance.r06_claim_integrity.scan_promised_counts``（缺口2，Codex
负责接入 final_artifact_qa）：

1. 旧候选漏拦 → 修复候选正确拒绝（三句 Qoder 用例 + 同族扩展）；
2. 合法语义保留：未来态（将/拟/履约时）、条件、否定、引用、失败类别定义、
   R05 已正确的条件框架/编号约定/语义争议不回退；
3. 数量一致性：数量不符 BLOCK；数量相等且载体完整不报；范围不明 NEEDS_REVIEW；
   邻格/独立后句不受上文条件掩护；表头/汇总行/编号取值/班次时限百分比不当行数。
"""
import json
import tempfile
import unittest
from pathlib import Path

from docx import Document

from governance.final_artifact_qa import evaluate_docx_artifact
from governance.r06_claim_integrity import scan_promised_counts



def _write_doc(tmp: Path, blocks: list[tuple]) -> Path:
    doc = Document()
    for block in blocks:
        kind = block[0]
        if kind == "h":
            doc.add_heading(block[1], level=block[2] if len(block) > 2 else 1)
        elif kind == "p":
            doc.add_paragraph(block[1])
        elif kind == "t":
            rows = block[1]
            table = doc.add_table(rows=len(rows), cols=len(rows[0]))
            for i, row in enumerate(rows):
                for j, cell in enumerate(row):
                    table.rows[i].cells[j].text = cell
    path = tmp / "fixture.docx"
    doc.save(str(path))
    return path


def _qa(path: Path):
    return evaluate_docx_artifact(path)


def _has_true(qa, rule: str, fragment: str) -> bool:
    return any(f.get("rule_id") == rule and fragment in (f.get("text") or f.get("issue") or "")
               for f in qa.get("findings") or [] if f.get("qa_class") == "TRUE_BLOCK")


def _qa_with_run_context(path: Path) -> dict:
    """Portable synthetic source context; real frozen-run replay is separate evidence."""
    brief = {"scenario": "完整物业服务方案"}
    extraction = {"paragraphs": [{"para_id": "TEST-SOURCE-1", "text": "技术方案应描述物业服务的拟议履约安排。"}], "tables": []}
    from governance.final_editorial import SourceFacts
    facts = SourceFacts(extraction)
    brief = {**brief, "verified_tender_facts": facts.rows}
    contracts = []
    state = {}
    canonical_facts = {**state, **facts.claim_source()}
    return evaluate_docx_artifact(path, canonical_facts=canonical_facts,
                                  brief=brief, contracts=contracts)


def _counts(blocks: list[tuple]) -> list[dict]:
    from governance.final_artifact_qa import _docx_blocks
    doc = Document()
    for block in blocks:
        kind = block[0]
        if kind == "h":
            doc.add_heading(block[1], level=block[2] if len(block) > 2 else 1)
        elif kind == "p":
            doc.add_paragraph(block[1])
        elif kind == "t":
            rows = block[1]
            table = doc.add_table(rows=len(rows), cols=len(rows[0]))
            for i, row in enumerate(rows):
                for j, cell in enumerate(row):
                    table.rows[i].cells[j].text = cell
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "d.docx"
        doc.save(str(path))
        return scan_promised_counts(_docx_blocks(path))


class CompletedClaimNowBlocked(unittest.TestCase):
    """缺口1：旧候选漏拦 → 修复候选正确拒绝。"""

    def _inject_and_assert(self, sentence: str, verb_fragment: str):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa_with_run_context(_write_doc(Path(tmp), [
                ("h", "退出移交", 2),
                ("p", sentence),
            ]))
            self.assertTrue(_has_true(qa, "UNSUPPORTED_COMPLETION_CLAIM", verb_fragment),
                            list(_qa(qa) for _ in [0]) and None)

    def test_handover_and_signature_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa_with_run_context(_write_doc(Path(tmp), [
                ("p", "本项目全部办公用房、服务用房与物业档案资料均已完整移交甲方并完成签收。"),
            ]))
            self.assertTrue(_has_true(qa, "UNSUPPORTED_COMPLETION_CLAIM", "移交"))

    def test_payment_report_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa_with_run_context(_write_doc(Path(tmp), [
                ("p", "本月已全部完成缴纳并向社会保险机构报送完毕。"),
            ]))
            self.assertTrue(_has_true(qa, "UNSUPPORTED_COMPLETION_CLAIM", "全部完成"))

    def test_return_and_check_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa_with_run_context(_write_doc(Path(tmp), [
                ("p", "钥匙、账号与门禁权限已全部交回并核对完毕。"),
            ]))
            self.assertTrue(_has_true(qa, "UNSUPPORTED_COMPLETION_CLAIM", "交回"))

    def test_ledger_established_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa_with_run_context(_write_doc(Path(tmp), [
                ("p", "我方已建立完整的社会保险缴纳台账并按月归档备查。"),
            ]))
            self.assertTrue(_has_true(qa, "UNSUPPORTED_EXISTING_STATE_CLAIM", "建立"))

    def test_neighbor_cell_and_followup_not_shielded(self):
        """邻格/独立后句不受上文条件掩护。"""
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("t", [["管理事项", "执行方式"],
                       ["作业检查", "需要增派人员时使用本单，本项目现有人员已完成全部培训并到位"]]),
            ]))
            self.assertTrue(_has_true(qa, "UNSUPPORTED_EXISTING_STATE_CLAIM", "已完成"))


class LegalSemanticsPreserved(unittest.TestCase):
    """未来态/条件/否定/引用/失败类别定义保留；R05 修复不回退。"""

    def test_future_register_not_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "退出移交时我方将完整移交全部档案资料，并配合甲方完成签收。"),
                ("p", "拟在服务期届满前完成缴纳清理，履约时向社保机构报送。"),
            ]))
            self.assertFalse(_has_true(qa, "UNSUPPORTED_COMPLETION_CLAIM", "移交"))
            self.assertFalse(_has_true(qa, "UNSUPPORTED_COMPLETION_CLAIM", "缴纳"))

    def test_conditional_frame_not_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "如项目已配置专用作业车辆，则按区域编号安排巡回路线。"),
            ]))
            self.assertFalse(_has_true(qa, "UNSUPPORTED_EXISTING_STATE_CLAIM", "已配置"))

    def test_negation_not_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "相关费用并未完成缴纳的责任转移，仍按合同约定执行。"),
            ]))
            self.assertFalse(_has_true(qa, "UNSUPPORTED_COMPLETION_CLAIM", "完成缴纳"))

    def test_failure_category_definition_not_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "两次尝试之后仍无回应的，在月度材料中单列并说明处置已经完成但确认未取得。"),
            ]))
            self.assertFalse(_has_true(qa, "UNSUPPORTED_COMPLETION_CLAIM", "已经完成"))

    def test_r05_numbering_convention_not_regressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("t", [["管理事项", "执行方式与时点", "未启用供方时的处理"],
                       ["作业检查：对对方实施部分核对是否按约定内容完成",
                        "与本方检查共用同一区域编号口径，作业后由本方复看现场状态并记时点",
                        "该条不启用，本团队自行承担的检查仍按本节的环节标准执行并留同一记录"]]),
            ]))
            self.assertFalse(_has_true(qa, "UNSUPPORTED_SITE_PREMISE", "同一区域"))

    def test_r05_semantic_dispute_still_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "多人协同在同一区域内按两件事处理：一是通道与用水点位的共用顺序，"
                      "事前先定谁先用；二是作业面交界处的责任。"),
            ]))
            self.assertTrue(_has_true(qa, "UNSUPPORTED_SITE_PREMISE", "同一区域"))


class PromisedCountIntegrity(unittest.TestCase):
    """缺口2：明确承诺数量与真实载体。"""

    def test_promise_three_table_two_blocked(self):
        findings = _counts([
            ("p", "本表列出三项关键服务内容。"),
            ("t", [["序号", "内容"], ["1", "保洁"], ["2", "秩序"]]),
        ])
        self.assertTrue(any(f["rule_id"] == "R06_PROMISED_COUNT_MISMATCH" and f["promised_count"] == 3
                            and f["carrier_count"] == 2 for f in findings))

    def test_promise_four_steps_list_three_blocked(self):
        findings = _counts([
            ("p", "承诺四个规定步骤确保交接闭环。"),
            ("p", "1、移交清单编制。"),
            ("p", "2、现场核对。"),
            ("p", "3、双方签认。"),
        ])
        self.assertTrue(any(f["rule_id"] == "R06_PROMISED_COUNT_MISMATCH"
                            and f["promised_count"] == 4 and f["carrier_count"] == 3
                            for f in findings))

    def test_equal_count_with_full_carrier_no_finding(self):
        findings = _counts([
            ("p", "本表列出三项关键服务内容。"),
            ("t", [["序号", "内容"], ["1", "保洁"], ["2", "秩序"], ["3", "绿化"]]),
        ])
        self.assertEqual(findings, [])

    def test_summary_and_header_rows_not_counted(self):
        findings = _counts([
            ("p", "本表列出三项应急处置措施。"),
            ("t", [["措施", "说明"],
                   ["先期处置", "控制影响范围"],
                   ["专业承接", "由实施方作出结论"],
                   ["报告甲方", "按事项编号报送"],
                   ["合计", "—"]]),
        ])
        # 3 data rows == promised 3 → no finding（汇总行不计）
        self.assertEqual(findings, [])

    def test_ambiguous_carrier_needs_review(self):
        findings = _counts([
            ("p", "承诺五项服务内容。"),
        ])
        self.assertTrue(any(f["rule_id"] == "R06_PROMISED_COUNT_UNVERIFIABLE"
                            and f["severity"] == "NEEDS_REVIEW" for f in findings))

    def test_no_promised_count_no_fabricated_result(self):
        findings = _counts([
            ("p", "本表列出若干项服务内容，按区域编号逐项核对。"),
            ("t", [["序号", "内容"], ["1", "保洁"]]),
        ])
        self.assertEqual(findings, [])

    def test_schedule_numbers_not_treated_as_rows(self):
        findings = _counts([
            ("p", "值守按24小时三班安排，每班8小时。"),
            ("t", [["班次", "时段"], ["早班", "08:00-16:00"], ["中班", "16:00-24:00"],
                   ["夜班", "00:00-08:00"]]),
        ])
        self.assertEqual(findings, [])

    def test_percentage_not_treated_as_rows(self):
        findings = _counts([
            ("p", "绿化存活率承诺达到95%。"),
            ("t", [["项目", "标准"], ["修剪", "每月两次"]]),
        ])
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
