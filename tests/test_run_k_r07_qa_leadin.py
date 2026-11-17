"""R07 lead-in differential. Expectations were fixed before this file existed.

The frozen v03 carrier is not rewritten into a pass. A same-section subtitle can
expose the body underneath it; release still requires every named anchor to
appear literally in that body.
"""
import os
import tempfile
import unittest
from pathlib import Path

from docx import Document

from governance.final_artifact_qa import evaluate_docx_artifact

V03_SHA = "10c1187929da34a792d13c6f7ae6541d2a22b699f05294f6b73a8bdada9eb6ce"
LEAD = "投诉的处置按受理、首次响应、过程跟踪、结果反馈四步执行。具体步骤如下："
REAL_STEPS = [
    "1. 按渠道接收投诉并逐条登记提出方、对象、位置与事由，注明所属作业线",
    "2. 首次响应：复述要求与提出方核对事实，需现场查看的由责任岗位按及时到场要求查看并控制影响范围",
    "3. 过程跟踪：分派与处置动作逐条写入同一编号，跨线的由专业负责人牵头会商并记录调配与原因",
    "4. 结果反馈：回告处置内容与依据标准并记录是否认可，未认可的继续办理，条目随月度节点汇总报送",
]
LITERAL_STEPS = [
    "1. 受理：按渠道接收投诉并逐条登记提出方、对象、位置与事由，注明所属作业线",
    *REAL_STEPS[1:],
]
REFERENCE = (
    "它包含四件可核对的东西：判定结论及其依据来源、启用所需的前提、"
    "拟委托内容将如何界定、以及未启用时各项作业由谁承担。"
    "这四件在方案中都已落成表格与条目。"
)
ROWS = [
    ["核对项", "实施说明"],
    ["判定结论及其依据来源", "依据为甲方书面确认文件"],
    ["启用所需的前提", "甲方书面确认后启用"],
    ["拟委托内容将如何界定", "以区域编号与作业类别界定"],
    ["未启用时各项作业由谁承担", "由本团队承担各项作业"],
]


def _write(parts, table=None):
    doc = Document()
    for kind, text in parts:
        if kind == "h2":
            doc.add_heading(text, level=2)
        elif kind == "h3":
            doc.add_heading(text, level=3)
        elif kind == "b":
            doc.add_paragraph(text, style="List Bullet")
        else:
            doc.add_paragraph(text)
    if table:
        grid = doc.add_table(rows=len(table), cols=len(table[0]))
        for i, row in enumerate(table):
            for j, cell in enumerate(row):
                grid.cell(i, j).text = cell
    handle = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    handle.close()
    path = Path(handle.name)
    doc.save(path)
    return path


def _qa(parts, table=None):
    path = _write(parts, table)
    try:
        return evaluate_docx_artifact(path)
    finally:
        path.unlink(missing_ok=True)


def _blocked(qa, rule):
    return [row for row in qa["findings"] if row.get("rule_id") == rule and row.get("qa_class") == "TRUE_BLOCK"]


class LeadInStructure(unittest.TestCase):
    def test_v03_actual_docx_stays_blocked_on_missing_literal_anchor(self):
        path = Path(os.environ["MMF_R07_V03_DOCX"])
        qa = evaluate_docx_artifact(path)
        self.assertEqual(qa["sha256"], V03_SHA)
        rows = _blocked(qa, "FAQ-009")
        self.assertEqual([row["path"] for row in rows], ["artifact.paragraphs[395]"])
        self.assertIn("受理", rows[0]["classification_reason"])
        self.assertIn("缺失", rows[0]["classification_reason"])
        self.assertEqual(qa["qa_classification"]["false_positive_channels"]["lead_in_continuation"], 0)

    def test_v03_shape_without_literal_first_anchor_stays_blocked(self):
        qa = _qa([
            ("h2", "投诉响应、过程跟踪与闭环反馈"),
            ("p", LEAD),
            ("h3", "投诉响应、过程跟踪与闭环反馈：步骤 1"),
            *[("p", text) for text in REAL_STEPS],
            ("h3", "投诉响应、过程跟踪与闭环反馈：步骤 2"),
            ("p", "1. 受理：这一步属于下一流程，不能借来补上一条引导句。"),
        ])
        rows = _blocked(qa, "FAQ-009")
        self.assertEqual(len(rows), 1)
        self.assertIn("受理", rows[0]["classification_reason"])

    def test_v03_shape_with_literal_anchors_is_continuation(self):
        qa = _qa([
            ("h2", "投诉响应、过程跟踪与闭环反馈"),
            ("p", LEAD),
            ("h3", "投诉响应、过程跟踪与闭环反馈：步骤 1"),
            *[("p", text) for text in LITERAL_STEPS],
        ])
        self.assertFalse(any(row.get("rule_id") == "FAQ-009" for row in qa["findings"]))
        self.assertGreater(qa["qa_classification"]["false_positive_channels"]["lead_in_continuation"], 0)

    def test_deleted_carrier_stays_blocked(self):
        qa = _qa([
            ("h2", "投诉响应、过程跟踪与闭环反馈"),
            ("p", LEAD),
            ("h3", "投诉响应、过程跟踪与闭环反馈：步骤 1"),
        ])
        self.assertTrue(_blocked(qa, "FAQ-009"))

    def test_missing_one_item_stays_blocked(self):
        qa = _qa([
            ("h2", "投诉响应、过程跟踪与闭环反馈"),
            ("p", LEAD),
            ("h3", "投诉响应、过程跟踪与闭环反馈：步骤 1"),
            *[("p", text) for text in LITERAL_STEPS[:3]],
        ])
        rows = _blocked(qa, "FAQ-009")
        self.assertEqual(len(rows), 1)
        self.assertIn("结果反馈", rows[0]["classification_reason"])

    def test_empty_title_does_not_prove_completeness(self):
        qa = _qa([
            ("h2", "投诉响应、过程跟踪与闭环反馈"),
            ("p", LEAD),
            ("h3", "受理、首次响应、过程跟踪、结果反馈"),
            ("h2", "下一事项"),
        ])
        self.assertTrue(_blocked(qa, "FAQ-009"))

    def test_steps_under_a_new_heading2_are_not_borrowed(self):
        qa = _qa([
            ("h2", "投诉响应、过程跟踪与闭环反馈"),
            ("p", LEAD),
            ("h2", "另一节"),
            *[("p", text) for text in LITERAL_STEPS],
        ])
        self.assertTrue(_blocked(qa, "FAQ-009"))

    def test_other_topic_list_does_not_pass(self):
        qa = _qa([
            ("h2", "投诉响应、过程跟踪与闭环反馈"),
            ("p", LEAD),
            ("h3", "投诉响应、过程跟踪与闭环反馈：步骤 1"),
            ("p", "1. 绿化苗木按品种登记并注明种植位置。"),
            ("p", "2. 浇灌频次按甲方确认的季节表执行。"),
            ("p", "3. 修剪残枝当日清运并记录去向。"),
            ("p", "4. 月度复核成活情况后归档。"),
        ])
        self.assertTrue(_blocked(qa, "FAQ-009"))

    def test_same_title_with_wrong_body_does_not_pass(self):
        qa = _qa([
            ("h2", "投诉响应、过程跟踪与闭环反馈"),
            ("p", LEAD),
            ("h3", "受理、首次响应、过程跟踪、结果反馈"),
            ("p", "绿化苗木的品种登记由专业负责人组织实施并按甲方要求记录归档。"),
        ])
        self.assertTrue(_blocked(qa, "FAQ-009"))

    def test_same_count_with_different_meaning_does_not_pass(self):
        qa = _qa([
            ("h2", "投诉响应、过程跟踪与闭环反馈"),
            ("p", LEAD),
            ("h3", "步骤"),
            ("p", "1. 开门前检查门厅照明并记录完好情况。"),
            ("p", "2. 清扫大堂地面并清运当日垃圾。"),
            ("p", "3. 核对钥匙柜数量后交下一班。"),
            ("p", "4. 关闭非值守区域的照明与门窗。"),
        ])
        self.assertTrue(_blocked(qa, "FAQ-009"))

    def test_adjacent_forged_four_items_do_not_pass(self):
        qa = _qa([
            ("h2", "投诉响应、过程跟踪与闭环反馈"),
            ("p", LEAD),
            ("p", "1. 今日天气转凉，户外作业改到午前。"),
            ("p", "2. 会议室桌椅按场次复位。"),
            ("p", "3. 车库道闸保持常开时段不变。"),
            ("p", "4. 快递柜清点后通知收件人。"),
        ])
        self.assertTrue(_blocked(qa, "FAQ-009"))

    def test_sibling_subtitle_cannot_supply_the_missing_anchor(self):
        qa = _qa([
            ("h2", "投诉响应、过程跟踪与闭环反馈"),
            ("p", LEAD),
            ("h3", "投诉响应、过程跟踪与闭环反馈：步骤 1"),
            *[("p", text) for text in REAL_STEPS],
            ("h3", "投诉响应、过程跟踪与闭环反馈：步骤 2"),
            ("p", "1. 受理：补写在下一流程里，不能回填上一条引导句。"),
        ])
        self.assertTrue(_blocked(qa, "FAQ-009"))

    def test_subtitle_does_not_hide_a_complete_bullet_list(self):
        qa = _qa([
            ("h2", "投诉响应、过程跟踪与闭环反馈"),
            ("p", LEAD),
            ("h3", "步骤"),
            ("b", "受理：登记来源渠道与事项编号，由当班岗留存。"),
            ("b", "首次响应：由责任岗查看并记录实际情况。"),
            ("b", "过程跟踪：按事项编号回写处置记录。"),
            ("b", "结果反馈：负责人报告处理过程与结论。"),
        ])
        self.assertFalse(any(row.get("rule_id") == "FAQ-009" for row in qa["findings"]))


class UnchangedNegativeCases(unittest.TestCase):
    def test_unsourced_geographic_premise_stays_blocked(self):
        self.assertTrue(_blocked(_qa([("p", "多个作业区共用同一区域编号门禁通行权限。")]), "UNSUPPORTED_SITE_PREMISE"))

    def test_completed_claim_stays_blocked(self):
        self.assertTrue(_blocked(_qa([("p", "否则台账失真，本项目人员培训已经完成。")]), "UNSUPPORTED_COMPLETION_CLAIM"))

    def test_missing_table_item_stays_blocked(self):
        self.assertTrue(_blocked(_qa([("p", REFERENCE)], table=ROWS[:-1]), "UNSUPPORTED_EXISTING_STATE_CLAIM"))

    def test_real_truncation_stays_blocked(self):
        self.assertTrue(_blocked(_qa([("p", "日常清洁作业按")]), "FAQ-009"))


if __name__ == "__main__":
    unittest.main()
