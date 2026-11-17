"""Real DOCX differential checks for typed carriers and local prose roles."""
import copy
from docx import Document
from governance.client_facing_prose import client_facing_prose, CLIENT_FACING_PROSE_RULE_ID
from governance.final_artifact_qa import evaluate_docx_artifact, repair_docx_artifact
from governance.r06_claim_integrity import scan_promised_counts, RULE_MISMATCH, RULE_UNVERIFIABLE

def paragraph(text):
    return {"kind": "paragraph", "path": "paragraph[0]", "text": text}

def counts(text, later=()):
    return scan_promised_counts([paragraph(text), *later])

def table_block(n):
    return {"kind": "table", "path": "table[0]",
            "table_rows": [["序号", "内容"], *[[str(i), "事项"+str(i)] for i in range(1,n+1)]],
            "table_header_rows": [True], "text": "table"}

def qa(doc, tmp_path):
    path = tmp_path / "case.docx"
    doc.save(path)
    return path, evaluate_docx_artifact(path)

def narration(report):
    return [r for r in report["findings"] if r["rule_id"] == CLIENT_FACING_PROSE_RULE_ID]

def test_nominal_table_headers_do_not_form_a_cross_cell_sentence(tmp_path):
    doc = Document()
    t = doc.add_table(rows=2, cols=4)
    for i, value in enumerate(["难点对象与成因", "执行标准与判定口径", "责任岗位与所需条件", "验证记录与偏差处理"]):
        t.cell(0,i).text = value
        t.cell(1,i).text = ["日常巡检", "按甲方书面确认标准执行", "当班岗位", "记录实际处置结果"][i]
    _, report = qa(doc, tmp_path)
    assert narration(report) == []

def test_true_cell_narration_is_local_and_never_advertises_an_unperformed_repair(tmp_path):
    doc = Document()
    t = doc.add_table(rows=2, cols=2)
    t.cell(0,0).text = "核对项"
    t.cell(0,1).text = "实施说明"
    t.cell(1,0).text = "责任岗位"
    t.cell(1,1).text = "作业做法由现场负责人确认，本节只固定难点判定与责任核对。"
    path, report = qa(doc, tmp_path)
    found = narration(report)
    assert len(found) == 1
    assert found[0]["path"] == "table[0].rows[1].cells[1]"
    assert found[0]["severity"] == "NEEDS_REVIEW"
    assert found[0]["auto_repair_applied"] is False
    assert found[0]["repair_execution_state"] == "NOT_APPLIED"
    assert found[0]["automatic_docx_repair_supported"] is False
    before = copy.deepcopy(t._tbl.xml)
    repair_docx_artifact(path)
    assert Document(path).tables[0]._tbl.xml == before
    assert narration(evaluate_docx_artifact(path))[0]["path"] == found[0]["path"]

def test_cell_boundaries_cannot_supply_a_missing_authoring_role(tmp_path):
    doc = Document()
    t = doc.add_table(rows=1, cols=2)
    t.cell(0,0).text = "本节"
    t.cell(0,1).text = "只记录现场巡查发现，由当班岗位确认。"
    _, report = qa(doc, tmp_path)
    assert narration(report) == []

def test_paragraph_narration_has_one_native_locator_even_after_an_empty_paragraph(tmp_path):
    doc = Document()
    doc.add_paragraph("")
    doc.add_paragraph("本章先把可以写进正文的数与只能写成条件的做法分开。")
    _, report = qa(doc, tmp_path)
    found = narration(report)
    assert len(found) == 1
    assert found[0]["path"] == "paragraph[1]"
    assert found[0]["repair_execution_state"] == "NOT_APPLIED"
    assert found[0]["automatic_docx_repair_supported"] is True

def test_document_portion_navigation_requires_a_source_backed_edit_without_deleting_business_content():
    text = ("逐项记录现场观察与处理结果。至于这些观察如何转为保养安排与观察重点的调整，"
            "本节后半部分按同一事项编号给出归集、比对、定性与回写四个步骤，"
            "此处只规定记录中必须留下能够支撑那四步的内容。")
    result, actions = client_facing_prose(text)
    assert actions
    assert result == text
    assert actions[0]["shape_id"] == "DOCUMENT_PORTION_NAVIGATION"
    assert all(not a["applied"] for a in actions)

def test_operational_timing_and_data_boundaries_survive():
    samples = [
        "本班后半时段按同一事项编号给出处理结果，异常记录由下一班跟进。",
        "保养安排按同一事项编号归集、比对、定性与回写，现场负责人确认后执行。",
        "统计不引入未经确认的历史数字，也不把个案表述为整体结论，样本量不足时如实标注为观察值。",
        "能耗仅作观察比对，不设定节能指标与降幅，用能方式调整、设备停用与费用安排由甲方决定。",
    ]
    for text in samples:
        result, actions = client_facing_prose(text)
        assert result == text
        assert not any(r["applied"] for r in actions)

def test_new_review_roles_cannot_override_existing_blocking_semantics(tmp_path):
    text = "本节只给出正文写法，具体条目一律不进入技术标正文。"
    _, actions = client_facing_prose(text)
    assert any(r["applied"] for r in actions)
    doc = Document();doc.add_paragraph(text)
    _, report = qa(doc, tmp_path)
    found = narration(report)
    assert len(found) == 1
    assert found[0]["severity"] == "BLOCK" and found[0]["qa_class"] == "TRUE_BLOCK"

def test_complete_inline_enumeration_is_an_actual_count_carrier():
    assert counts("包括三项服务内容：秩序维护、环境保洁、设备巡检。") == []

def test_inline_mismatch_blocks_even_without_a_following_table():
    result = counts("包括四项服务内容：秩序维护、环境保洁、设备巡检。")
    assert len(result) == 1
    assert result[0]["rule_id"] == RULE_MISMATCH
    assert result[0]["carrier_count"] == 3
    assert result[0]["carrier_evidence"][0]["proof"] == "COMPLETE_INLINE_ENUMERATION"

def test_inline_count_cannot_launder_a_missing_table_reference():
    result = counts("本表列出三项服务内容：秩序维护、环境保洁、设备巡检。")
    assert len(result) == 1
    assert result[0]["rule_id"] == "R07_PROMISED_CARRIER_REFERENCE_UNVERIFIABLE"
    assert result[0]["quantity_verified"] is True
    assert result[0]["qa_class"] == "NEEDS_REVIEW"

def test_inline_list_does_not_override_a_proven_contradictory_table():
    result = counts("本表列出三项服务内容：秩序维护、环境保洁、设备巡检。", [table_block(2)])
    assert len(result) == 1
    assert result[0]["rule_id"] == RULE_MISMATCH
    assert result[0]["carrier_count"] == 2
    assert result[0]["inline_count"] == 3

def test_matching_table_cannot_erase_a_contradictory_inline_enumeration():
    found = counts("本表列出三项服务内容：秩序维护、环境保洁。", [table_block(3)])
    assert len(found) == 1
    assert found[0]["rule_id"] == RULE_MISMATCH
    assert found[0]["carrier_count"] == 2

def test_inline_count_and_missing_reference_are_measured_from_actual_docx(tmp_path):
    doc = Document()
    doc.add_paragraph("包括三项服务内容：秩序维护、环境保洁、设备巡检。")
    _, report = qa(doc, tmp_path)
    assert not any(f["rule_id"].startswith(("R06_PROMISED_COUNT", "R07_PROMISED_CARRIER")) for f in report["findings"])
    doc.paragraphs[0].text = "本表列出三项服务内容：秩序维护、环境保洁、设备巡检。"
    path, report = qa(doc, tmp_path)
    found = [f for f in report["findings"] if f["rule_id"] == "R07_PROMISED_CARRIER_REFERENCE_UNVERIFIABLE"]
    assert len(found) == 1 and found[0]["quantity_verified"] is True
    assert Document(path).paragraphs[0].text == doc.paragraphs[0].text

def test_incomplete_or_nested_inline_examples_are_unverifiable():
    for text in [
        "包括三项服务内容：秩序维护、环境保洁等。",
        "包括三项服务内容：秩序维护、环境保洁、设备巡检及其他。",
        "包括三项服务内容：秩序维护（含巡查、值守）、环境保洁。",
        "包括三项服务内容：秩序维护、环境保洁或者设备巡检。",
    ]:
        found = counts(text)
        assert len(found) == 1 and found[0]["rule_id"] == RULE_UNVERIFIABLE

def test_sibling_sentence_or_unrelated_repeated_number_is_not_inline_evidence():
    for text in [
        "包括三项服务内容。其他情形：巡查、值守、维护。",
        "包括三项服务内容。三项设备条件各自指向一个层面：甲指向型号，乙指向电源，丙指向状态。",
    ]:
        assert counts(text)[0]["rule_id"] == RULE_UNVERIFIABLE

def test_real_inline_categories_preserve_table_locator_review():
    text = ("本表列出四类高频情形：凭证无法确认的出入、影响使用的秩序异常、"
            "人员受困与求助、以及超出秩序职责的设施与环境异常，写明到场顺序。")
    found = counts(text)
    assert found[0]["quantity_verified"] is True
    assert found[0]["inline_count"] == 4
    assert found[0]["rule_id"] == "R07_PROMISED_CARRIER_REFERENCE_UNVERIFIABLE"

def test_multiple_promises_still_require_unique_mapping():
    found = counts("包括三项服务内容：巡查、保洁、维护；承诺两个关键步骤：登记、回写。")
    assert len(found) == 2
    assert all(r["rule_id"] == RULE_UNVERIFIABLE for r in found)

def test_next_step_noun_is_not_a_cardinality_but_explicit_one_step_is():
    assert counts("下一步工作安排如下。", [table_block(2)]) == []
    found = counts("以下一个步骤执行核对。", [table_block(2)])
    assert found[0]["rule_id"] == RULE_MISMATCH
