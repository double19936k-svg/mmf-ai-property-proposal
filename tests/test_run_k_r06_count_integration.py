from pathlib import Path
from docx import Document
import pytest
from governance.final_artifact_qa import evaluate_docx_artifact, _docx_blocks
from governance.r06_claim_integrity import scan_promised_counts, RULE_MISMATCH, RULE_UNVERIFIABLE

def rows(doc, values):
    table=doc.add_table(rows=len(values),cols=len(values[0]))
    for ri,row in enumerate(values):
        for ci,value in enumerate(row):table.cell(ri,ci).text=value
    return table

def check(doc,tmp_path):
    target=tmp_path/'count.docx';doc.save(target)
    report=evaluate_docx_artifact(target)
    return [v for v in report['findings'] if v['rule_id'] in (RULE_MISMATCH,RULE_UNVERIFIABLE)]

@pytest.mark.parametrize('count,expected',[(2,RULE_MISMATCH),(3,None)])
def test_real_final_entry_compares_unique_complete_table(tmp_path,count,expected):
    doc=Document();doc.add_paragraph('本表列出三项关键服务内容。')
    rows(doc,[['序号','内容']]+[[str(i),'作业'+str(i)] for i in range(1,count+1)])
    findings=check(doc,tmp_path)
    assert [v['rule_id'] for v in findings]==([] if expected is None else [expected])
    if expected:assert findings[0]['qa_class']=='TRUE_BLOCK' and findings[0]['carrier_count']==2

@pytest.mark.parametrize('kind',['mixed','unknown_header','duplicate_numbers','window_cut','next_section','merged','two_lists'])
def test_ambiguous_carriers_never_turn_into_proven_mismatch_or_silent_pass(tmp_path,kind):
    doc=Document();doc.add_paragraph('承诺三个规定步骤。')
    if kind=='mixed':
        rows(doc,[['序号','内容'],['1','甲'],['2','乙']])
        for n in range(1,4):doc.add_paragraph(str(n)+'、行动')
    elif kind=='unknown_header':
        rows(doc,[['1','甲'],['2','乙'],['3','丙']])
    elif kind=='duplicate_numbers':
        for n in (1,2,2):doc.add_paragraph(str(n)+'、行动')
    elif kind=='window_cut':
        for n in range(1,16):doc.add_paragraph(str(n)+'、行动')
    elif kind=='next_section':
        doc.add_paragraph('下面先交代核对场景与资料来源。')
        doc.add_heading('其他项目的列表',level=2)
        for n in range(1,4):doc.add_paragraph(str(n)+'、行动')
    elif kind=='merged':
        t=rows(doc,[['序号','内容'],['1','甲'],['2','乙']]);t.cell(1,0).merge(t.cell(1,1))
    else:
        for n in range(1,4):doc.add_paragraph(str(n)+'、行动')
        doc.add_paragraph('另一套不同的处置路径。')
        for n in range(1,3):doc.add_paragraph(str(n)+'、其他行动')
    findings=check(doc,tmp_path)
    assert len(findings)==1 and findings[0]['rule_id']==RULE_UNVERIFIABLE
    assert findings[0]['qa_class']=='NEEDS_REVIEW' and findings[0]['carrier_count'] is None

@pytest.mark.parametrize('size,expected',[(2,RULE_MISMATCH),(3,None)])
def test_native_word_numbering_is_a_real_carrier(tmp_path,size,expected):
    doc=Document();doc.add_paragraph('承诺三个规定步骤。')
    for n in range(size):doc.add_paragraph('行动内容'+str(n),style='List Number')
    findings=check(doc,tmp_path)
    assert [v['rule_id'] for v in findings]==([] if expected is None else [expected])

def test_cell_newlines_and_separator_chars_do_not_invent_rows(tmp_path):
    doc=Document();doc.add_paragraph('本表列出一项关键服务内容。')
    rows(doc,[['序号','内容'],['1','第一行 | 内容\n第二行 | 内容']])
    assert check(doc,tmp_path)==[]

def test_summary_row_is_not_a_promised_item(tmp_path):
    doc=Document();doc.add_paragraph('本表列出三项应急措施。')
    rows(doc,[['措施','说明'],['控制','甲'],['核对','乙'],['报送','丙'],['合计','—']])
    assert check(doc,tmp_path)==[]

@pytest.mark.parametrize('text',[
    '值守按24小时三班安排，每班8小时。','本表列出三个工作人员。','绿化存活率达到95%。',
    '不承诺三项服务内容。','文件引用“本表列出三项服务内容”，此处只说明引用用法。'])
def test_noncarrier_quantities_and_quoted_or_negated_counts_are_not_claims(tmp_path,text):
    doc=Document();doc.add_paragraph(text)
    rows(doc,[['序号','内容'],['1','甲']])
    assert check(doc,tmp_path)==[]

def test_carrier_in_other_table_cell_does_not_cover_paragraph_claim(tmp_path):
    doc=Document();doc.add_paragraph('承诺三个规定步骤。')
    rows(doc,[['步骤','内容'],['第一步','1、行动\n2、行动\n3、行动']])
    assert check(doc,tmp_path)[0]['rule_id']==RULE_MISMATCH

def test_insufficient_input_for_next_step_is_not_an_enumeration(tmp_path):
    doc=Document();doc.add_paragraph('上一步的产出不足以下一步就无法继续，流程因此暴露断点。')
    for n in range(3):doc.add_paragraph('核对条目'+str(n),style='List Bullet')
    assert check(doc,tmp_path)==[]

def test_five_steps_cannot_be_compared_with_seven_different_exit_phases(tmp_path):
    doc=Document();doc.add_paragraph('以下五个步骤按月核对。')
    rows(doc,[['阶段','说明']]+[[str(n),'阶段说明'] for n in range(1,8)])
    assert check(doc,tmp_path)[0]['rule_id']==RULE_UNVERIFIABLE

def test_bullet_checklist_is_not_a_promised_numbered_step_sequence(tmp_path):
    doc=Document();doc.add_paragraph('承诺三个规定步骤。')
    for n in range(3):doc.add_paragraph('核对条目'+str(n),style='List Bullet')
    assert check(doc,tmp_path)[0]['rule_id']==RULE_UNVERIFIABLE

def test_this_table_cannot_be_replaced_by_a_list(tmp_path):
    doc=Document();doc.add_paragraph('本表列出三项关键服务内容。')
    for n in range(1,4):doc.add_paragraph(str(n)+'、核对条目')
    assert check(doc,tmp_path)[0]['rule_id']==RULE_UNVERIFIABLE

def test_state_labels_are_not_claims_but_named_subject_actions_still_are(tmp_path):
    doc=Document();doc.add_paragraph('状态：已开始、已完成与受阻三类。')
    target=tmp_path/'labels.docx';doc.save(target)
    report=evaluate_docx_artifact(target)
    assert not any(v.get('qa_class')=='TRUE_BLOCK' and v['rule_id'].startswith('UNSUPPORTED_') for v in report['findings'])
    doc=Document();doc.add_paragraph('状态：本项目培训已完成与验收。')
    target=tmp_path/'assertion.docx';doc.save(target)
    report=evaluate_docx_artifact(target)
    assert any(v.get('qa_class')=='TRUE_BLOCK' and v['rule_id']=='UNSUPPORTED_EXISTING_STATE_CLAIM' for v in report['findings'])

def test_explicit_false_header_flag_cannot_remove_first_data_row(tmp_path):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    doc=Document();doc.add_paragraph('本表列出三项服务内容。')
    table=rows(doc,[['1','甲'],['2','乙'],['3','丙']])
    header=OxmlElement('w:tblHeader');header.set(qn('w:val'),'0')
    table.rows[0]._tr.get_or_add_trPr().append(header)
    assert check(doc,tmp_path)[0]['rule_id']==RULE_UNVERIFIABLE
