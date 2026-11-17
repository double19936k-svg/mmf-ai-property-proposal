"""R05 QA 上下文更正差分测试。

每一类**可放行语境**（表单使用条件、反事实风险、台账类别、信念转述、编号口径、
避免性描述、甲方规定委托、文档自指、FAQ 引导句）都配一个**方向相反的真缺陷负例**：
真正无来源的既存事实/场地构型/完成主张、真实截断、同句另一分句的现实主张，
在这些负例上 ArtifactQA 必须照常 BLOCK。断言全部落在
`governance.final_artifact_qa.evaluate_docx_artifact`（终审权威入口），
不借用 hygiene 等其他规则集的结果，也不按块编号或新旧 SHA 特判。
"""
import tempfile
import unittest
from pathlib import Path

from docx import Document

from governance.final_artifact_qa import evaluate_docx_artifact


def _write_doc(tmp: Path, blocks: list[tuple]) -> Path:
    """blocks: ('h', text, level) 标题 | ('p', text) 段落 | ('t', rows) 表格。"""
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


def _true_hits(qa) -> set[tuple[str, str]]:
    return {(f.get("rule_id"), (f.get("text") or f.get("issue") or "")[:24])
            for f in qa.get("findings") or [] if f.get("qa_class") == "TRUE_BLOCK"}


def _has_true(qa, rule: str, fragment: str) -> bool:
    return any(f.get("rule_id") == rule and fragment in (f.get("text") or f.get("issue") or "")
               for f in qa.get("findings") or [] if f.get("qa_class") == "TRUE_BLOCK")


class FormUsageCondition(unittest.TestCase):
    def test_form_usage_condition_is_not_completion_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "作业线之间需要下达任务、顶替岗位或移交已完成事项时当场开始使用本单，"
                      "单据随当班记录归档。"),
            ]))
            self.assertFalse(_has_true(qa, "UNSUPPORTED_EXISTING_STATE_CLAIM", "顶替岗位或移交"))

    def test_form_usage_frame_does_not_shield_later_real_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "需要增派人员时使用本单，本项目现有人员已完成全部培训并到位。"),
            ]))
            self.assertTrue(_has_true(qa, "UNSUPPORTED_EXISTING_STATE_CLAIM", "已完成"))

    def test_applicable_scenario_label_is_not_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "字段说明：适用场景：项目统筹、专业执行与客户协同三线之间下达任务"
                      "或移交已完成事项时使用；保存位置：归入项目管理档案并按年度报甲方备案。"),
            ]))
            self.assertFalse(_has_true(qa, "UNSUPPORTED_EXISTING_STATE_CLAIM", "移交已完成"))


class CounterfactualRisk(unittest.TestCase):
    def test_counterfactual_otherwise_is_not_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "否则每期汇总看到的都是已完成数量，而实际停留在中间状态的事项无人说明。"),
            ]))
            self.assertFalse(_has_true(qa, "UNSUPPORTED_EXISTING_STATE_CLAIM", "已完成数量"))

    def test_counterfactual_does_not_shield_new_sentence_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "否则流程空转。本项目保洁服务已完成验收并交付甲方。"),
            ]))
            self.assertTrue(_has_true(qa, "UNSUPPORTED_EXISTING_STATE_CLAIM", "已完成"))

    def test_awaited_checkpoint_is_not_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "若分派时只写交给某一个人，剩下的部分就在各线之间悬空，"
                      "等到检查时每条线都能指出自己那一部分已完成，"
                      "而提出方得到的是没有闭合的答复。"),
            ]))
            self.assertFalse(_has_true(qa, "UNSUPPORTED_EXISTING_STATE_CLAIM", "那一部分已完成"))

    def test_failure_condition_timing_is_not_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "两处不同步时，清单上写着已完成而汇总中同类条目照旧出现，"
                      "检查时就分不清是改进无效还是分类已变。"),
            ]))
            self.assertFalse(_has_true(qa, "UNSUPPORTED_EXISTING_STATE_CLAIM", "清单上写着已完成"))


class LedgerCategory(unittest.TestCase):
    def test_ledger_category_description_is_not_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "两次尝试之后仍无回应的，在月度材料中单列并说明处置已经完成但确认未取得。"),
            ]))
            self.assertFalse(_has_true(qa, "UNSUPPORTED_COMPLETION_CLAIM", "已经完成"))

    def test_real_completion_still_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "本项目人员培训已经完成。"),
            ]))
            self.assertTrue(_has_true(qa, "UNSUPPORTED_COMPLETION_CLAIM", "已经完成"))


class BeliefReport(unittest.TestCase):
    def test_belief_report_is_not_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "实际避免的是事后各岗位都以为自己已完成的那类记录。"),
            ]))
            self.assertFalse(_has_true(qa, "UNSUPPORTED_EXISTING_STATE_CLAIM", "自己已完成"))

    def test_real_completion_without_belief_frame_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "各岗位均已完成培训。"),
            ]))
            self.assertTrue(_has_true(qa, "UNSUPPORTED_COMPLETION_CLAIM", "已完成"))


class SitePremise(unittest.TestCase):
    def test_numbering_convention_alignment_is_not_geography(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "对对方实施部分核对是否按约定内容完成。"),
                ("t", [["管理事项", "依据与口径", "执行方式与时点", "未启用供方时的处理"],
                       ["作业检查：对对方实施部分核对是否按约定内容完成",
                        "以约定的作业内容、完成条件与本方现场观察为判定依据，不以其自述为凭",
                        "与本方检查共用同一区域编号口径，作业后由本方复看现场状态并记时点",
                        "该条不启用，本团队自行承担的检查仍按本节的环节标准执行并留同一记录"]]),
            ]))
            self.assertFalse(_has_true(qa, "UNSUPPORTED_SITE_PREMISE", "同一区域"))

    def test_unsourced_geographic_colocation_still_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "本项目各作业区与甲方办公区在同一区域内集中布置，可共用出入口。"),
            ]))
            self.assertTrue(_has_true(qa, "UNSUPPORTED_SITE_PREMISE", "同一区域"))

    def test_same_region_substring_not_globally_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "本项目的多个作业区域共用同一区域通行权限与门禁资源。"),
            ]))
            self.assertTrue(_has_true(qa, "UNSUPPORTED_SITE_PREMISE", "同一区域"))

    def test_r04_692_semantic_dispute_still_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "多人协同在同一区域内按两件事处理：一是通道与用水点位的共用顺序，"
                      "事前先定谁先用；二是作业面交界处的责任，交界处由后完成的一方做末次整理"
                      "并在记录中注明。岗位之间的互相提醒限于当班范围内，涉及别条线的配合"
                      "按事项编号转办，不在口头答应之后各自认为已完成。"),
            ]))
            self.assertTrue(_has_true(qa, "UNSUPPORTED_SITE_PREMISE", "同一区域"))

    def test_clearly_conditionalized_wording_not_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "如现场存在共用通道与用水点位，则按共用布局安排协同作业并登记顺序。"),
            ]))
            self.assertFalse(_has_true(qa, "UNSUPPORTED_SITE_PREMISE", "共用"))


class AvoidedFailure(unittest.TestCase):
    def test_avoided_failure_description_is_not_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "这一约束防止一种常见的脱节—文本写着某类设施的处置路径，"
                      "而该设施已经更换或改造，按旧文本行动反而会扩大影响。"),
            ]))
            self.assertFalse(_has_true(qa, "UNSUPPORTED_EXISTING_STATE_CLAIM", "该设施已经更换"))

    def test_contrastive_hypothetical_is_not_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "文本上写着某类设施的处置路径，而该设施已经更换型号或改造。"),
            ]))
            self.assertFalse(_has_true(qa, "UNSUPPORTED_EXISTING_STATE_CLAIM", "该设施已经更换"))

    def test_real_facility_state_still_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "该项目空调设施已经更换为新型号并完成验收。"),
            ]))
            self.assertTrue(_has_true(qa, "UNSUPPORTED_EXISTING_STATE_CLAIM", "已经更换"))


class OwnerRegulationDelegation(unittest.TestCase):
    def test_owner_regulation_delegation_is_not_unsourced_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("t", [["预案类别", "适用场景与边界", "文本要素与维护责任", "核对与修订方式"],
                       ["秩序与人员安全类：拥堵、异常人员与车辆、可疑物品与通行受阻",
                        "适用于可能影响人员安全或正常通行的情形",
                        "涉及权限与封闭管理的取值按甲方规定",
                        "门区与通道条件变化、区域调整后触发核对，修订前后对照入档并注明生效时点"]]),
            ]))
            self.assertFalse(_has_true(qa, "C4", "封闭管理"))

    def test_unsourced_closed_management_still_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "本项目园区实行封闭管理，外来人员一律禁止进入。"),
            ]))
            self.assertTrue(_has_true(qa, "C4", "封闭管理"))


class SelfReference(unittest.TestCase):
    PARAGRAPH = ("本节在当前阶段的输出是一份条件说明。条件说明本身有实在内容，不是占位。"
                 "它包含四件可核对的东西：判定结论及其依据来源、启用所需的前提、"
                 "拟委托内容将如何界定、以及未启用时各项作业由谁承担。"
                 "这四件在方案中都已落成表格与条目，甲方若决定启用，"
                 "可直接在已登记的框架上确认范围与接口；若决定不启用，"
                 "本节同样有结论可归档，不会留下一段无人说明的空白。")
    TABLE = [["核对项", "说明"],
             ["判定结论及其依据来源", "由甲方书面确认模式决定"],
             ["启用所需的前提", "甲方书面确认后启用"],
             ["拟委托内容将如何界定", "以区域编号与作业类别逐项列出"],
             ["未启用时各项作业由谁承担", "本团队与专业线承担"]]

    def test_verified_self_reference_is_not_unsourced(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", self.PARAGRAPH),
                ("t", self.TABLE),
            ]))
            self.assertFalse(_has_true(qa, "UNSUPPORTED_EXISTING_STATE_CLAIM", "这四件在方案中"))

    def test_missing_carrier_still_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", self.PARAGRAPH),
                ("p", "与本任务无关的其他段落内容，不含所称载体。"),
            ]))
            self.assertTrue(_has_true(qa, "UNSUPPORTED_EXISTING_STATE_CLAIM", "这四件在方案中"))


class Faq009LeadIn(unittest.TestCase):
    def test_lead_in_with_real_continuation_not_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("h", "投诉处理", 2),
                ("p", "投诉的处置按受理、首次响应、过程跟踪、结果反馈四步执行，"
                      "四步共用一本投诉台账。具体步骤如下："),
                ("p", "受理阶段由当班岗位登记编号与位置，首次响应在三十分钟内到场控制影响范围，"
                      "过程跟踪按事项编号回写台账，结果反馈由项目负责人向甲方通报结论。"),
            ]))
            self.assertFalse(_has_true(qa, "FAQ-009", "如下"))

    def test_lead_in_before_new_heading_still_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("h", "投诉处理", 2),
                ("p", "投诉的处置按受理、首次响应、过程跟踪、结果反馈四步执行。具体步骤如下："),
                ("h", "下一章 其他事项", 2),
            ]))
            self.assertTrue(any(f.get("rule_id") == "FAQ-009" for f in qa.get("findings") or []))

    def test_real_mid_sentence_break_still_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "日常清洁作业按"),
            ]))
            self.assertTrue(any(f.get("rule_id") == "FAQ-009" for f in qa.get("findings") or []))

    def test_including_without_continuation_still_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = _qa(_write_doc(Path(tmp), [
                ("p", "服务内容包括"),
            ]))
            self.assertTrue(any(f.get("rule_id") == "FAQ-009" for f in qa.get("findings") or []))


if __name__ == "__main__":
    unittest.main()
