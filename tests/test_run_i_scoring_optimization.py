import copy

from planning.content_engineering import build_technical_scope_gate
from planning.scoring_evidence import artifact_evidence_index, judge_scoring_row, TECHNICAL_SCORING_GATE_MIN
from planning.scoring_source_scope import scoring_source_scope
from planning.scoring_taxonomy import evidence_contract_for, pre_generation_scoring_mapping_gate
from planning.scoring_retrieval import select_evidence
from planning.scoring_global_acceptance import judge_global_service_plan
from planning.scoring_response_points import bind_source_obligations, attach_response_points
from planning.scoring_responsiveness import classify_responsiveness, WHOLE_DOCUMENT_RUBRIC, record_requires_technical_acceptance, record_requires_body_response
from planning.scoring_final_evidence import align_final_evidence


def unit(text, uid='CU-1', substantive=True):
    return {'text': text, 'section_id': 'S1', 'assembled_content_unit_id': uid,
            'archetype': 'NARRATIVE', 'chars': len(text), 'substantive': substantive,
            'artifact_location': uid, 'structured_text': ''}


def test_scope_comes_from_source_volume_even_for_misleading_technical_words():
    item = {'scoring_item_id': 'SCR-X', 'label': '快速应急响应', 'scoring_leaf_id': 'L-X'}
    graph = {'leaves': [{'leaf_id': 'L-X', 'volume': '商务部分', 'topic': '快速应急响应',
                         'weight': 11, 'locator': {'table_id': 'T', 'row_index': 3}}]}
    evidence = scoring_source_scope(item, graph)
    gate = build_technical_scope_gate({'rows': []}, scoring_items=[item], scoring_graph=graph)
    row = gate['scoring_register'][0]
    assert row['disposition'] == 'EXCLUDED'
    assert row['source_scoring_scope'] == evidence
    assert evidence['weight'] == 11 and evidence['locator']['row_index'] == 3


def test_exit_rejects_first_contractor_hit_and_selects_project_evidence():
    wrong = unit('临时承包施工单位撤场，资料移交并签收。', 'CU-WRONG')
    right = unit('合同期满项目退出移交：资料移交、设备移交、系统权限交回，双方确认。', 'CU-RIGHT')
    hits, missing, candidates = select_evidence({'资料': ('资料移交',)}, [wrong, right], 'EXIT_HANDOVER')
    assert not missing and hits[0]['content_unit_id'] == 'CU-RIGHT'
    assert any('WRONG_BUSINESS_EVENT' in c['rejected_reasons'] for c in candidates)


def test_only_wrong_business_event_stays_nearby():
    row = {'label': '项目退出移交方案', 'section_ids': ['S1']}
    idx, structured, counts = artifact_evidence_index({'S1': {'body_blocks': [{'content': '供方施工单位人员撤场设备移交。' * 35}]}})
    verdict = judge_scoring_row(row, idx, structured, counts)
    assert verdict['status'] == 'NEARBY_ONLY'
    assert verdict['rejected_evidence']


def test_structural_backing_cannot_be_borrowed_from_another_unit():
    narrative = unit('本项目组织架构中项目负责人负责指挥，人员值守，交底培训。' * 20)
    narrative['structured_text'] = ''
    hits, _, _ = select_evidence({'岗位组织': ('项目负责人',)}, [narrative], 'MANAGEMENT_TEAM')
    assert not hits[0]['structured_backed']


def test_team_contract_does_not_request_protected_identity_evidence():
    contract = evidence_contract_for('项目管理团队人员构成、人员结构及相关经验', responsiveness_class='TECHNICAL_BODY_ANONYMOUS_STRUCTURE')
    assert contract['rubric_id'] == 'MANAGEMENT_TEAM' and contract['min_concepts'] == 4
    assert contract['require_structured'] and not contract['identity_material_required']
    assert not any(t in str(contract['concepts']) for t in ['姓名', '证书', '相关经验'])


def test_advantage_contract_accepts_mechanism_not_evaluator_wording():
    contract = evidence_contract_for('针对本项目描述自身物业管理优势，横向比较，优得2分')
    assert contract['rubric_id'] == 'MANAGEMENT_ADVANTAGE'
    assert not any(t in str(contract['concepts']) for t in ['横向比较', '优得'])
    assert contract['min_concepts'] == len(contract['concepts'])


def test_global_rubric_remains_technical_acceptance_without_single_cu():
    cls, _, _ = classify_responsiveness('项目服务方案根据是否覆盖所有物业需求，内容结构是否清晰详实合理')
    record = {'responsiveness_class': cls}
    assert cls == WHOLE_DOCUMENT_RUBRIC
    assert record_requires_technical_acceptance(record) and not record_requires_body_response(record)


def test_global_domain_and_structure_gaps_cannot_be_hidden_by_one_good_section():
    plan = {'service_domains': [{'domain': 'engineering', 'terms': ['维修'], 'requirements': [], 'planned_sections': ['S1']},
                                {'domain': 'catering', 'terms': ['食堂'], 'requirements': [], 'planned_sections': ['S2']}],
            'required_sections': ['S1', 'S2'], 'source_project_terms': ['办公楼']}
    verdict = judge_global_service_plan(plan, {'S1': [unit('办公楼维修由工程岗位负责，执行检查，记录归档。' * 25)]})
    assert verdict['status'] == 'PARTIAL'
    assert 'SERVICE_DOMAIN:catering' in verdict['missing_dimensions']
    assert 'EMPTY_SECTION:S2' in verdict['missing_dimensions']


def test_exit_time_limit_is_source_bound_and_absence_does_not_invent_it():
    contract = evidence_contract_for('项目退出移交方案')
    empty = copy.deepcopy(contract)
    bind_source_obligations(empty, {'requirements': []})
    assert '合同退出期限' not in empty['concepts']
    bind_source_obligations(contract, {'requirements': [{'requirement_id': 'R', 'sources': [
        {'para_id': 'P', 'source_excerpt': '乙方应在合同终止或者解除后12日内将人员和自有资产撤出'}]}]})
    assert contract['min_concepts'] == 9
    assert contract['source_obligations'][0]['value'] == '12'
    assert contract['source_obligations'][0]['source']['para_id'] == 'P'


def test_response_point_missing_blocks_real_plan_and_threshold_unchanged():
    record = {'canonical_scoring_id': 'C', 'topic': '项目退出移交', 'source_ids': [],
              'planned_response_section': 'S1', 'planned_response_sections': ['S1'],
              'planned_response_cu': ['CU-1'], 'evidence_contract': evidence_contract_for('项目退出移交')}
    matrix = {'canonical_scoring_records': [record], 'rows': []}
    attach_response_points(matrix, {'content_units': [{'content_unit_id': 'CU-1', 'section_id': 'S1', 'must_cover': []}]})
    gate = pre_generation_scoring_mapping_gate(matrix, planned_unit_ids=['CU-1'], planned_section_ids=['S1'])
    assert gate['may_enter_generation']
    record['planning_response_points'] = []
    gate = pre_generation_scoring_mapping_gate(matrix, planned_unit_ids=['CU-1'], planned_section_ids=['S1'])
    assert not gate['may_enter_generation']
    assert 'EXPLICIT_RESPONSE_POINT' in gate['unmapped_records'][0]['missing']
    assert TECHNICAL_SCORING_GATE_MIN == .8


def test_payload_only_content_does_not_become_final_docx_evidence():
    from governance.final_editorial import SourceFacts
    fragments = {'S1': {'title': '项目退出移交方案', 'content_unit_fragments': [
        {'content_unit_id': 'C1', 'content': '退出计划的准备与交接。',
         'payload': {'steps': ['系统权限交回并设备移交', '人员撤场与资料移交']}}]}}
    blocks = [{'text': '项目退出移交方案', 'heading_level': 2, 'kind': 'paragraph', 'path': 'p[0]'},
              {'text': '退出计划的准备与交接。', 'heading_level': None, 'kind': 'paragraph', 'path': 'p[1]'}]
    final, audit = align_final_evidence(blocks, fragments, SourceFacts(), 'final.docx')
    assert not final['S1']['content_unit_fragments'][0]['final_structured_text']
    assert '系统权限交回' not in str(final)


def test_evaluator_only_fallback_cannot_create_business_evidence_contract():
    from planning.scoring_evidence import label_concepts
    assert not label_concepts('横向比较，优得2分，一般得1分，较差得0分')


def test_missing_global_acceptance_plan_blocks_pre_generation():
    record = {'canonical_scoring_id': 'G', 'responsiveness_class': WHOLE_DOCUMENT_RUBRIC,
              'counts_toward_technical_fulfillment': False, 'must_respond': True}
    matrix = {'canonical_scoring_records': [record], 'rows': [],
              'response_point_contract_version': 'explicit-scoring-response-point-v1'}
    gate = pre_generation_scoring_mapping_gate(matrix, planned_unit_ids=[], planned_section_ids=[])
    assert not gate['may_enter_generation']
    assert gate['unmapped_records'][0]['missing'] == ['GLOBAL_ACCEPTANCE_PLAN']


def test_response_point_cannot_borrow_a_different_units_section():
    record = {'canonical_scoring_id': 'C', 'planned_response_section': 'S1',
              'planned_response_sections': ['S1', 'S2'], 'planned_response_cu': ['CU-1'],
              'evidence_contract': evidence_contract_for('项目退出移交')}
    matrix = {'canonical_scoring_records': [record], 'rows': []}
    attach_response_points(matrix, {'content_units': [{'content_unit_id': 'CU-1', 'section_id': 'S1'}]})
    record['planning_response_points'][0]['section_id'] = 'S2'
    gate = pre_generation_scoring_mapping_gate(matrix, planned_unit_ids=['CU-1'],
                planned_section_ids=['S1', 'S2'], planned_unit_sections={'CU-1': 'S1'})
    assert not gate['may_enter_generation']


def test_conflicting_source_exit_limits_are_not_silently_selected():
    contract = evidence_contract_for('项目退出移交方案')
    bind_source_obligations(contract, {'requirements': [{'sources': [
        {'para_id': 'P1', 'source_excerpt': '合同终止后7日内将人员和自有资产撤出'},
        {'para_id': 'P2', 'source_excerpt': '合同终止后12日内将人员和自有资产撤出'}]}]})
    assert contract['source_obligation_conflicts']
    record = {'canonical_scoring_id': 'C', 'planned_response_section': 'S1',
              'planned_response_cu': ['CU-1'], 'evidence_contract': contract}
    gate = pre_generation_scoring_mapping_gate({'canonical_scoring_records': [record], 'rows': []},
                planned_unit_ids=['CU-1'], planned_section_ids=['S1'])
    assert not gate['may_enter_generation']
    assert 'SOURCE_OBLIGATION_CONFLICT' in gate['unmapped_records'][0]['missing']
