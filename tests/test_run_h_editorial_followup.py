"""Fresh Run H witnesses: preservation, canonical scopes and honest page locators."""
import json
import re

import pytest
from docx import Document

from governance.commitment_provenance import evaluate_commitments, apply_local_repairs
from governance.final_editorial import SourceFacts, edit_text, run_final_editorial, bind_human_input_pages
from governance.client_facing_prose import client_facing_prose, internal_narration_findings
from governance.final_artifact_qa import evaluate_docx_artifact


@pytest.mark.parametrize('text', [
    '缺少复验的条目即使现场已完成也不得销项。',
    '物业服务团队只反馈现场状态与已完成的动作。',
    '关闭条件为处置动作已完成、结果已反馈、回访已记录三项同时满足。',
    '秩序负责人每周对全部已完成清单逐条复核。',
    '不因文字已经填写就认定现场已经完成整改。',
    '关闭只认两种形态，一是缺项物已交付并复验合格，二是经甲方书面确认的替代安排。',
    '字段设计对应三种关闭形态：缺项物已交付并复验合格、经甲方书面确认的替代安排、限制放行。',
])
def test_process_roles_are_not_current_existing_facts(text):
    result = evaluate_commitments({}, [], {'artifact': {'text': text}})
    assert result['status'] == 'PASS', result['violations']
    assert apply_local_repairs({'artifact': {'text': text}})['artifact']['text'] == text
    for row in result['claims']:
        record = row['canonical_claim_record']
        if record['modality'] == 'CONDITIONAL':
            lo, hi = record['modality_governed_span']
            assert lo <= record['claim_span'][0] <= record['claim_span'][1] <= hi


@pytest.mark.parametrize('text', [
    '关闭条件为处置动作已完成；此外地下机房已安装自动灭火装置。',
    '每周复核已完成清单，地下机房已完成改造。',
    '不因文字已经填写就认定整改完成，地下机房已安装自动灭火装置。',
    '甲方现有制度规定甲方负责审批作业范围与时段。',
    '动火作业按现行制度必须事前上报审批。',
    '缺少复验的条目即使已完成也不得销项，另有配电间已完成改造。',
    '字段设计对应关闭形态：地下机房已安装自动灭火装置。',
    '关闭条件为处置动作已完成；地下机房已完成改造。',
])
def test_process_governor_cannot_exempt_independent_or_facility_facts(text):
    assert evaluate_commitments({}, [], {'artifact': {'text': text}})['status'] == 'BLOCK'


@pytest.mark.parametrize('text', [
    '动火、临时用电等事项必须事前上报 | 审批留痕为协同单与甲方书面确认文件。',
    '甲方负责审批作业范围与时段、见证重要设备的复验结论。',
])
def test_external_approval_method_requires_review_not_unsupported_fact_hard(text):
    result = evaluate_commitments({}, [], {'artifact': {'text': text}})
    assert result['status'] == 'NEEDS_REVIEW'
    assert not result['violations'] and result['review_items']


def test_source_conflict_comparisons_are_verbatim():
    facts = SourceFacts({'paragraphs': [
        {'para_id': 'TECH', 'heading_path': ['技术需求书'], 'text': '建筑面积9163平方米'},
        {'para_id': 'CONTRACT', 'heading_path': ['合同主要条款'], 'text': '建筑面积9163.8平方米'},
    ]})
    for text in [
        '一处给出9163平方米，另一处给出9163.8平方米，二者并列保留，不相互替换。',
        '9163.8平方米与9163平方米并列保留。',
        '面积记为9163.8平方米，与前节的9163平方米并列保留。',
        '另一处给出9163.8平方米。',
    ]:
        assert edit_text(text, facts)[0] == text


@pytest.mark.parametrize('text', [
    '第一层是前置条件，指上一环节形成的资料。',
    '第二层是完成判据，指可以当场验证的成果状态。',
    '第一类为资料缺项，指缺少设备档案；第二类为现场条件缺项，指核验尚未完成。',
    '本章按作业区划分巡检范围。',
    '时序的判定口径有三层。第一层是前置条件，指上一环节形成的资料。',
    '未确认的比例门槛不作为对外承诺口径。',
])
def test_business_definitions_and_boundaries_survive_sanitizer_and_qa(text, tmp_path):
    assert client_facing_prose(text)[0] == text
    assert edit_text(text)[0] == text
    doc = Document(); doc.add_paragraph(text); path = tmp_path / 'method.docx'; doc.save(path)
    qa = evaluate_docx_artifact(path)
    assert not any(x['severity'] == 'BLOCK' and x['rule_id'].startswith('CLIENT_ARTIFACT_INTERNAL') for x in qa['findings'])


@pytest.mark.parametrize('text', [
    '第二步把分值高低只用于内部编制与复核的优先级排序。',
    '评分因素在项目内部只用于校核服务事项和排列交底顺序。',
])
def test_scoring_authoring_is_detected_and_removed(text):
    assert internal_narration_findings(text)
    assert client_facing_prose(text)[0] == ''


@pytest.mark.parametrize('text', [
    '企业业绩、认证与人员资历类事实按【待投标人确认：所需证明】登记，不以叙述替代凭证。',
    '到岗名册与职责分工签发【待投标人确认：所需证明】、分区域核验记录与影像编号对应。',
    '人员须持有效健康证明，证明材料由投标人据实提供【待投标人确认：所需证明】。',
    '联系人【待投标人确认：真实姓名与联系电话】',
    '若以自有系统承载台账，【待投标人确认：所需证明】，在核验前仅作检索副本。',
    '核对服务资料。【待投标人确认：所需证明】 响应组织由逐条台账维持。',
    '可比经验所需证明材料以一处行内提示登记：【待投标人确认：所需证明】，可比性不改变面积基数。',
    '涉及证明材料的提供，只登记为一处行内提示【待投标人确认：所需证明】，由投标人按招标要求提供。',
])
def test_confirmation_anchors_never_become_prose_blanks(text, tmp_path):
    doc = Document(); doc.add_paragraph(text); path = tmp_path / 'confirm.docx'; doc.save(path)
    result = run_final_editorial(path, facts=SourceFacts(), output_dir=tmp_path)
    out = Document(path).paragraphs[0].text
    assert not re.search(r'_{4,}|【|按登记|，。|，，|行内提示|待核实\s+响应', out)
    assert '待核实' in out or '待确认' in out
    assert '将配备' not in out
    assert result['human_input_index']['entry_count'] == 1
    assert result['unresolved_inline_placeholders'] == 0


def test_page_locator_requires_actual_measured_boundary(tmp_path):
    entries = [{'section_id': f'S{i:02d}-01', 'section_title': str(i), 'page_locator': None} for i in range(1, 4)]
    path = tmp_path / 'HUMAN_INPUT_INDEX.json'
    path.write_text(json.dumps({'entries': entries}), encoding='utf-8')
    report = {'physical_pages': 90, 'displayed_pages': ['3', '30', '1'], 'status': 'PARTIAL',
              'toc_entry_pages': [{'text': '一', 'rendered_page': 3}, {'text': '二', 'rendered_page': 30}, {'text': '三', 'rendered_page': 0}]}
    bind_human_input_pages(tmp_path, report)
    out = json.loads(path.read_text(encoding='utf-8'))['entries']
    assert out[0]['page_locator']['from'] == 3 and out[0]['page_locator']['to'] == 29
    assert out[1]['page_locator'] is None and out[2]['page_locator'] is None
    assert out[1]['page_locator_status'] == 'NOT_MEASURED'
    # Old aggregate counters and cached display values cannot prove individual entries.
    bind_human_input_pages(tmp_path, {'physical_pages': 90, 'displayed_pages': ['3', '30', '60'], 'toc_pages_verified': 3})
    assert all(x['page_locator'] is None for x in json.loads(path.read_text(encoding='utf-8'))['entries'])


def test_native_chapter_identity_survives_editorial_title_tidy(tmp_path):
    doc = Document()
    title = '实施计划、履约成果与服务责任与实施条件'
    doc.add_heading(title, 1)
    doc.add_heading('登记与处理与关闭', 2)
    doc.add_paragraph('项目负责人核对处理记录。')
    path = tmp_path / 'chapter.docx'; doc.save(path)
    run_final_editorial(path, facts=SourceFacts(), output_dir=tmp_path)
    after = Document(path)
    assert after.paragraphs[0].text == title
    assert after.paragraphs[1].text == '登记、处理与关闭'


def test_final_qa_checks_blanks_and_process_context_without_exempting_facilities(tmp_path):
    doc = Document()
    doc.add_heading('投诉处理核验流程', 2)
    doc.add_paragraph('首次响应已完成：已告知受理结果并留下记录。', style='List Bullet')
    doc.add_paragraph('地下机房已安装自动灭火装置。', style='List Bullet')
    doc.add_paragraph('地下机房已完成系统建设。', style='List Bullet')
    doc.add_paragraph('资历证明________________。')
    path = tmp_path / 'qa.docx'; doc.save(path)
    qa = evaluate_docx_artifact(path)
    assert any(x['rule_id'] == 'UNRESOLVED_INLINE_PLACEHOLDER' and x['severity'] == 'BLOCK' for x in qa['findings'])
    hard = [x for x in qa['findings'] if x['severity'] == 'BLOCK' and x['rule_id'] == 'UNSUPPORTED_EXISTING_STATE_CLAIM']
    assert len(hard) == 2 and any('安装' in x['text'] for x in hard)
    assert any(x['severity'] == 'NEEDS_REVIEW' and x.get('canonical_claim_record', {}).get('usage_role', {}).get('role') == 'PROCESS_CHECKLIST_REVIEW' for x in qa['findings'])
