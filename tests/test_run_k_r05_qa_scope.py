"""Actual DOCX tests for mixed clauses, table cells and document carrier scope."""
import tempfile
import unittest
from pathlib import Path
from docx import Document
from governance.final_artifact_qa import evaluate_docx_artifact

REFERENCE = ('它包含四件可核对的东西：判定结论及其依据来源、启用所需的前提、'
             '拟委托内容将如何界定、以及未启用时各项作业由谁承担。'
             '这四件在方案中都已落成表格与条目。')
ROWS = [['核对项', '实施说明'],
        ['判定结论及其依据来源', '依据为甲方书面确认文件'],
        ['启用所需的前提', '甲方书面确认后启用'],
        ['拟委托内容将如何界定', '以区域编号与作业类别界定'],
        ['未启用时各项作业由谁承担', '由本团队承担各项作业']]


class QaScope(unittest.TestCase):
    def qa(self, paragraphs=(), *, table=None, other_heading=False, reference=False, bullets=()):
        with tempfile.TemporaryDirectory() as tmp:
            doc = Document()
            doc.add_heading('作业组织', 2)
            if reference:
                doc.add_paragraph(reference if isinstance(reference, str) else REFERENCE)
            if other_heading:
                doc.add_heading('其他事项', 2)
            for text in paragraphs:
                doc.add_paragraph(text)
            for text in bullets:
                doc.add_paragraph(text, style='List Bullet')
            if table:
                t = doc.add_table(rows=len(table), cols=len(table[0]))
                for i, row in enumerate(table):
                    for j, text in enumerate(row):
                        t.cell(i, j).text = text
            p = Path(tmp) / 'actual.docx'
            doc.save(p)
            return evaluate_docx_artifact(p)

    def assert_block(self, qa, rule):
        self.assertTrue(any(r.get('rule_id') == rule and r.get('qa_class') == 'TRUE_BLOCK'
                            for r in qa['findings']), qa['findings'])

    def test_avoidance_cannot_cover_sibling_facility_assertion(self):
        self.assert_block(self.qa(['应避免记录遗漏，现有空调设施已经更换并完成验收。']), 'UNSUPPORTED_EXISTING_STATE_CLAIM')

    def test_counterfactual_cannot_cover_sibling_completion(self):
        self.assert_block(self.qa(['否则台账失真，本项目人员培训已经完成。']), 'UNSUPPORTED_COMPLETION_CLAIM')

    def test_belief_cannot_cover_sibling_completion(self):
        self.assert_block(self.qa(['各岗以为操作结束，本项目培训已经完成。']), 'UNSUPPORTED_COMPLETION_CLAIM')

    def test_contrast_cannot_cover_sibling_completion(self):
        self.assert_block(self.qa(['文本上写着某类设施的处置路径，而该设施已经更换型号，本项目保洁人员已经完成培训。']), 'UNSUPPORTED_COMPLETION_CLAIM')

    def test_contrast_cannot_cover_earlier_fact(self):
        self.assert_block(self.qa(['现有空调设施已经更换并完成验收，文本写着旧型号，而该设施已经更换型号。']), 'UNSUPPORTED_EXISTING_STATE_CLAIM')

    def test_owner_regulation_cannot_cover_sibling_site_fact(self):
        self.assert_block(self.qa(['通行取值按甲方规定，本项目园区实行封闭管理。']), 'C4')

    def test_owner_regulation_in_another_cell_cannot_cover_fact(self):
        self.assert_block(self.qa(table=[['说明', '实际状态'], ['通行取值按甲方规定', '本项目园区实行封闭管理']]), 'C4')

    def test_avoidance_in_another_cell_cannot_cover_fact(self):
        self.assert_block(self.qa(table=[['防范安排', '设施现状'], ['应避免记录遗漏', '现有空调设施已经更换']]), 'UNSUPPORTED_EXISTING_STATE_CLAIM')

    def test_counterfactual_in_another_cell_cannot_cover_fact(self):
        self.assert_block(self.qa(table=[['可能影响', '实际状态'], ['否则台账失真', '本项目人员培训已经完成']]), 'UNSUPPORTED_COMPLETION_CLAIM')

    def test_numbering_prefix_does_not_shield_geographic_permission(self):
        self.assert_block(self.qa(['多个作业区共用同一区域编号门禁通行权限。']), 'UNSUPPORTED_SITE_PREMISE')

    def test_unrelated_following_paragraph_is_not_continuation(self):
        self.assert_block(self.qa(['具体步骤如下：', '本项目绿化苗木的品种登记由专业负责人组织实施并按甲方要求记录归档。']), 'FAQ-009')

    def test_named_steps_missing_from_following_prose_remain_blocked(self):
        self.assert_block(self.qa(['投诉处置按受理、首次响应、过程跟踪、结果反馈四步执行。具体步骤如下：',
                                  '绿化苗木的品种登记由专业负责人组织实施并按甲方要求记录归档。']), 'FAQ-009')

    def test_only_some_promised_steps_remain_blocked(self):
        self.assert_block(self.qa(['投诉处置按受理、首次响应、过程跟踪、结果反馈四步执行。具体步骤如下：',
                                  '受理由当班岗位登记，首次响应按甲方要求进行。']), 'FAQ-009')

    def test_complete_contiguous_list_is_real_continuation(self):
        q = self.qa(['投诉处置按受理、首次响应、过程跟踪、结果反馈四步执行。具体步骤如下：'],
                    bullets=['受理：登记来源渠道与事项编号，由当班岗留存。',
                             '首次响应：由责任岗查看并记录实际情况。',
                             '过程跟踪：按事项编号回写处置记录。',
                             '结果反馈：负责人报告处理过程与结论。'])
        self.assertFalse(any(r['rule_id'] == 'FAQ-009' for r in q['findings']))

    def test_incomplete_list_does_not_satisfy_promised_steps(self):
        self.assert_block(self.qa(['投诉处置按受理、首次响应、过程跟踪、结果反馈四步执行。具体步骤如下：'],
                                  bullets=['受理：登记来源渠道与事项编号，由当班岗留存。',
                                           '首次响应：由责任岗查看并记录实际情况。']), 'FAQ-009')

    def test_continuation_audit_is_retained(self):
        q = self.qa(['投诉处置按受理、首次响应、过程跟踪、结果反馈四步执行。具体步骤如下：',
                     '受理由当班岗登记，首次响应查明情况，过程跟踪回写台账，结果反馈由负责人执行。'])
        self.assertFalse(any(r['rule_id'] == 'FAQ-009' for r in q['findings']))
        self.assertTrue(q['qa_classification']['false_positive_channels']['lead_in_continuation'])

    def test_scattered_keyword_prose_is_not_structured_carrier(self):
        self.assert_block(self.qa(['判定结论待澄清，依据来源未知，启用之前须审批，拟委托内容暂不确定，未启用时作业应另行通知。'], reference=True), 'UNSUPPORTED_EXISTING_STATE_CLAIM')

    def test_carrier_in_another_section_is_not_local_support(self):
        self.assert_block(self.qa(table=ROWS, reference=True, other_heading=True), 'UNSUPPORTED_EXISTING_STATE_CLAIM')

    def test_one_missing_item_remains_blocked(self):
        self.assert_block(self.qa(table=ROWS[:-1], reference=True), 'UNSUPPORTED_EXISTING_STATE_CLAIM')

    def test_only_half_of_conjunctive_item_is_not_enough(self):
        rows = [r[:] for r in ROWS]
        rows[1] = ['判定结论', '由甲方确认']
        self.assert_block(self.qa(table=rows, reference=True), 'UNSUPPORTED_EXISTING_STATE_CLAIM')

    def test_number_of_items_must_match(self):
        self.assert_block(self.qa(table=ROWS, reference=REFERENCE.replace('包含四件', '包含五件')), 'UNSUPPORTED_EXISTING_STATE_CLAIM')

    def test_unsupported_sibling_fact_is_not_self_reference(self):
        self.assert_block(self.qa(table=ROWS, reference=REFERENCE[:-1] + '，现有空调设施已经更换。'), 'UNSUPPORTED_EXISTING_STATE_CLAIM')

    def test_verified_carrier_has_consistent_canonical_gate(self):
        q = self.qa(table=ROWS, reference=True)
        rows = [r for r in q['false_positives'] if '文档自指已逐件定位' in r.get('classification_reason', '')]
        self.assertEqual(len(rows), 1)
        record = rows[0]['canonical_claim_record']
        self.assertEqual(record['decision']['gate'], 'SUPPRESS')
        self.assertEqual(len(record['document_carrier_support']), 4)

    def test_positive_usage_evidence_cannot_include_neighboring_clause(self):
        q = self.qa(['通行取值按甲方规定，涉及权限与封闭管理的取值按甲方规定。'])
        rows = [r for r in q['false_positives'] if r.get('claim_type') == 'C4']
        self.assertEqual(len(rows), 1)
        usage = rows[0]['canonical_claim_record']['usage_role']
        self.assertEqual(usage['text'], '涉及权限与封闭管理的取值按甲方规定')


if __name__ == '__main__':
    unittest.main()
