import pytest
from governance.artifact_qa import apply_artifact_repairs, evaluate_artifact
from governance.client_facing_prose import client_facing_prose
from governance.final_editorial import edit_text, SourceFacts
from longform.factory import _repair_customer_text

OPERATING = [
    '范围或条件变化时的联动改写在承诺边界与待澄清登记中按条目处理并注明生效时点',
    '项目层级复核：项目负责人对整体履职与考核意见复核',
    '口头需求未登记：需求已被处理但没有取得事项编号',
    '能否在情景复述中说明确认、上报与引导的先后顺序以及所在楼层的疏散方向',
    '结论与验收归属：谁对专业结论负责、谁确认恢复使用',
    '发酵与蒸制时间控制：把时间写成可回查的记录而不是经验',
    '事项产生与登记：把需要多方配合的事固定为一条可跟踪条目',
    '协调发起与登记：把需要配合或决定的事项固定为条目',
    '修订与版本维护：变化按生效时点进入文本而不改写历史',
    '把一处规定扩展成另一些义务或改写其时限与对象',
    '执行标准与判定口径', '本方案的边界表述',
    '培训人员复述疏散方向及上报顺序',
    '合同义务不得改写其时限与对象',
]

@pytest.mark.parametrize('text', OPERATING)
def test_operating_carriers_survive_all_shared_cleaners(text):
    assert client_facing_prose(text)[0] == text
    assert _repair_customer_text(text) == text
    assert edit_text(text, SourceFacts())[0] == text
    value = apply_artifact_repairs({'cell': text})['cell']
    assert value and ('生效时点' not in text or '生效时点' in value)

@pytest.mark.parametrize('text', [
    '本节不展开巡视内容，而按三种语气登记正文句子。',
    '写法说明：把正文句子分为已确认事实、条件化方法与待澄清事项三档。',
    '正文不再另行说明这些内容。',
])
def test_authoring_roles_are_still_removed(text):
    assert client_facing_prose(text)[0] != text

def test_positional_arrays_keep_repeated_long_cells_and_headers():
    same = '值班负责人记录事项编号并交接尚未关闭事项'
    raw = {'tables': [{'columns': [same, same], 'rows': [[same, same], [same, same]]}]}
    result = apply_artifact_repairs(raw)['tables'][0]
    assert result['columns'] == [same, same]
    assert result['rows'] == [[same, same], [same, same]]

def test_source_email_identity_survives_unknown_english_cleanup():
    raw = '联系：biwenlu@sinochem.com；muzehua@sinochem.com。untranslated'
    result = apply_artifact_repairs({'text': raw})['text']
    assert 'biwenlu@sinochem.com' in result and 'muzehua@sinochem.com' in result
    assert 'untranslated' not in result

@pytest.mark.parametrize('email', ['biwenlu@.com', '@.com', 'owner@-example.com'])
def test_malformed_email_is_a_block(email):
    assert any(f['rule_id'] == 'EMAIL_IDENTITY_CORRUPTION' and f['severity'] == 'BLOCK'
               for f in evaluate_artifact({'text': email})['findings'])
