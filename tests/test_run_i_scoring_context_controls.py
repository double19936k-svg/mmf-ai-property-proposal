"""Business identity and source-obligation controls from the independent gate recheck."""
import pytest

from planning.scoring_evidence import artifact_evidence_index, judge_scoring_row
from planning.scoring_retrieval import select_evidence
from planning.scoring_response_points import bind_source_obligations
from planning.scoring_taxonomy import evidence_contract_for


def carrier(text, uid='CU-1'):
    return {'text': text, 'section_id': 'S1', 'assembled_content_unit_id': uid,
            'archetype': 'STANDARD_TABLE', 'chars': len(text), 'substantive': True,
            'artifact_location': 'CONTROL.docx#table[0]', 'structured_text': text}


def verdict(text, label, contract=None):
    fragments = {'S1': {'content_unit_fragments': [
        {'content_unit_id': 'CU-1', 'content': text, 'payload': {},
         'final_structured_text': text, 'final_artifact_locations': ['CONTROL.docx#table[0]']}]}}
    index, structured, counts = artifact_evidence_index(fragments)
    return judge_scoring_row({'label': label, 'section_ids': ['S1'], 'entity_contract': contract or {}},
                             index, structured, counts)


HQ = ('公司总部组织架构由综合管理部、人力资源部、财务部和市场部组成。总部项目主管仅管理全国项目统计报表，'
      '总部部门职责通过部门经理汇报，总部人员配置齐备，培训考核按公司总部管理制度开展。'
      '这里描述公司总部管理团队，不涉及现场客户服务安排。') * 5
PROJECT = ('本项目现场组织架构由项目主管、专业负责人、现场人员组成，项目主管常驻现场接受甲方指令。'
           '岗位职责和汇报归口到专业负责人，人员配置以现场排班与值守岗位核对，培训交底和技能核验由项目负责人考核。') * 5


def test_headquarters_alone_cannot_fulfill_project_team():
    result = verdict(HQ, '项目管理团队')
    assert result['status'] != 'FULFILLED'
    assert not result['selected_evidence']


def test_headquarters_does_not_win_when_real_project_team_exists():
    contract = evidence_contract_for('项目管理团队')
    hits, missing, candidates = select_evidence(contract['concept_synonyms'],
                                               [carrier(HQ, 'HQ'), carrier(PROJECT, 'PROJECT')],
                                               'MANAGEMENT_TEAM', require_structured=True)
    assert not missing and {h['content_unit_id'] for h in hits} == {'PROJECT'}
    assert all(not c['business_context_valid'] for c in candidates if c['content_unit_id'] == 'HQ')


def test_actual_site_team_with_headquarters_support_remains_fulfilled():
    text = PROJECT + '总部为本项目提供培训资源，现场项目主管负责培训交底与技能考核。'
    assert verdict(text, '项目管理团队')['status'] == 'FULFILLED'


def test_headquarters_project_support_does_not_become_site_organization():
    text = ('公司总部为本项目提供组织架构，部门职责与汇报关系按总部管理。'
            '总部人员配置、培训交底和技能考核面向全国项目。') * 12
    result = verdict(text, '项目管理团队')
    assert result['status'] != 'FULFILLED' and not result['selected_evidence']


ENTRY = '进场接管：我方接收物业管理权移交，查验设备与档案资料移交清单，与原物业方签收确认，双方确认进场。'


def test_entry_takeover_is_rejected_without_a_better_exit_candidate():
    hits, _, candidates = select_evidence({'资料移交': ('资料移交', '移交清单'),
                                           '双方确认': ('双方确认', '签收')},
                                          [carrier(ENTRY * 8)], 'EXIT_HANDOVER')
    assert not hits
    assert candidates and all(not c['business_context_valid'] for c in candidates)


def test_entry_and_exit_in_the_same_cu_keep_directions_separate():
    text = ENTRY + '合同终止后本项目退出移交，由甲方接收资料移交，双方确认退出交接。'
    hits, _, candidates = select_evidence({'资料移交': ('资料移交',), '双方确认': ('双方确认',)},
                                          [carrier(text)], 'EXIT_HANDOVER')
    assert len(hits) == 2 and all(h['offset'] >= len(ENTRY) for h in hits)
    assert all(not c['business_context_valid'] for c in candidates if c['offset'] < len(ENTRY))


def test_exit_carrier_cannot_lend_event_identity_to_contractor_handover():
    text = '合同终止后本项目退出移交按退出流程办理。维保单位退场时办理资料移交并签收。'
    hits, _, candidates = select_evidence({'资料移交': ('资料移交',)}, [carrier(text)], 'EXIT_HANDOVER')
    assert not hits and candidates


def test_contractor_event_switch_blocks_neutral_following_confirmation():
    text = '合同终止后本项目退出移交按退出流程办理。维保单位退场办理交接。双方确认签收。'
    hits, _, _ = select_evidence({'双方确认': ('双方确认', '签收')}, [carrier(text)], 'EXIT_HANDOVER')
    assert not hits


def test_exit_transfer_to_successor_does_not_become_entry_takeover():
    text = '合同终止后本项目向甲方及接管方办理资料移交，双方确认退出交接。'
    hits, missing, _ = select_evidence({'资料移交': ('资料移交',), '双方确认': ('双方确认',)},
                                     [carrier(text)], 'EXIT_HANDOVER')
    assert not missing and len(hits) == 2


def exit_contract(days=7):
    contract = evidence_contract_for('项目退出移交方案')
    bind_source_obligations(contract, {'requirements': [{'requirement_id': 'SOURCE-EXIT', 'sources': [
        {'para_id': 'SOURCE-P', 'source_excerpt': f'乙方应在合同终止或者解除后{days}日内将人员和自有资产撤出物业服务区域'}]}]})
    return contract


OTHER_EXIT_DIMENSIONS = ('项目退出移交由项目负责人牵头，按退出流程启动准备。资料移交按档案清单办理，'
                         '设备移交按权属清点，权限交回以甲方实际授权对象为准。问题清单登记未完事项，'
                         '人员撤场按接续岗位交底，过渡安排保持值守服务，双方确认验收签字并保存记录。')


def exit_verdict(clock, days=7):
    return verdict((OTHER_EXIT_DIMENSIONS + clock) * 4, '项目退出移交方案', exit_contract(days))


@pytest.mark.parametrize('clock', [
    '合同终止后7日内完成资料移交；人员和自有资产在合同终止后30日内撤出物业服务区域。',
    '合同终止后7日内人员撤场，自有资产在30日内撤出物业服务区域。',
    '合同终止后7日内将人员撤出物业服务区域，自有资产另行处理。',
    '合同终止后7日内核对人员和自有资产的资料，撤出安排待确认。',
    '合同终止后7日内制定人员和自有资产的撤出计划。',
    '禁止在合同终止后7日内将人员和自有资产撤出物业服务区域。',
])
def test_withdrawal_clock_needs_correct_action_and_both_source_objects(clock):
    result = exit_verdict(clock)
    assert result['status'] != 'FULFILLED'
    assert '合同退出期限' in result['missing_dimensions']


@pytest.mark.parametrize('clock', [
    '合同终止或者解除后7日内将人员和自有资产撤出物业服务区域。',
    '人员及自有资产应于合同终止后7日内撤出物业服务区域。',
    '合同解除后7日内，将人员和自有资产撤出物业服务区域。',
    '合同终止后7天内将人员和自有资产撤出物业服务区域。',
    '合同终止后七日内将人员和自有资产撤出物业服务区域。',
    '合同终止后7日内将人员撤出物业服务区域；自有资产在合同终止后7日内撤出物业服务区域。',
])
def test_correct_source_withdrawal_clock_remains_fulfilled(clock):
    assert exit_verdict(clock)['status'] == 'FULFILLED'


def test_other_record_clock_does_not_conflict_with_correct_withdrawal_clock():
    text = ('合同终止后7日内将人员和自有资产撤出物业服务区域。'
            '合同终止后30日内完成档案复制核对。')
    assert exit_verdict(text)['status'] == 'FULFILLED'


def test_contradictory_withdrawal_clock_cannot_hide_behind_correct_first_hit():
    text = ('合同终止后7日内将人员和自有资产撤出物业服务区域。'
            '合同终止后30日内将人员和自有资产撤出物业服务区域。')
    result = exit_verdict(text)
    assert result['status'] != 'FULFILLED'
    assert '合同退出期限' in result['missing_dimensions']


def test_contradiction_in_another_cu_blocks_the_correct_clock():
    contract = exit_contract()
    units = [carrier('合同终止后7日内将人员和自有资产撤出物业服务区域。', 'CORRECT'),
             carrier('合同终止后30日内将人员和自有资产撤出物业服务区域。', 'CONFLICT')]
    hits, _, candidates = select_evidence({'合同退出期限': ('7日内',)}, units, 'EXIT_HANDOVER',
                                          source_obligations=contract['source_obligations'])
    assert not hits
    assert all('CONTRADICTORY_WITHDRAWAL_CLOCK' in c['rejected_reasons'] for c in candidates)


def test_table_cells_cannot_lend_objects_to_a_records_deadline():
    text = '项目退出移交记录矩阵|合同终止后7日内完成档案移交|人员和自有资产另列于撤出区域表'
    hits, _, candidates = select_evidence({'合同退出期限': ('7日内',)}, [carrier(text)], 'EXIT_HANDOVER',
                                          source_obligations=exit_contract()['source_obligations'])
    assert not hits and candidates


def test_clock_value_is_source_derived_instead_of_seven_hardcoded():
    assert exit_verdict('合同终止后12日内将人员和自有资产撤出物业服务区域。', 12)['status'] == 'FULFILLED'


def test_unrelated_source_clock_does_not_create_withdrawal_obligation():
    contract = evidence_contract_for('项目退出移交方案')
    bind_source_obligations(contract, {'requirements': [{'sources': [
        {'para_id': 'P', 'source_excerpt': '合同终止后7日内完成资料移交，人员和自有资产的撤出期限由双方另行约定。'}]}]})
    assert not contract.get('source_obligations')


def test_recorded_source_contract_retains_both_objects_and_action():
    binding = exit_contract(12)['source_obligations'][0]
    assert binding['required_objects'] == ['PERSONNEL', 'OWN_ASSETS']
    assert binding['required_action'] == 'WITHDRAWAL'
    assert binding['source']['para_id'] == 'SOURCE-P'
