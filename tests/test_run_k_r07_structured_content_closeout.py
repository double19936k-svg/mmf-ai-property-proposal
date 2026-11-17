import pytest
from governance.artifact_qa import apply_artifact_repairs
from governance.client_facing_prose import client_facing_prose
from longform.local_normalize import normalize_block, normalize_fragment
from longform.cu_assembly import apply_content_units


@pytest.mark.parametrize('kind', ['bullet_group', 'numbered_steps'])
@pytest.mark.parametrize('field', ['content', 'items'])
def test_explicit_item_boundaries_survive_semicolons(kind, field):
    values = ['登记缺陷；专业决策由甲方组织。', '核对结果\n再签认。']
    block, _ = normalize_block({'type': kind, field: values})
    assert block['content'] == values


def test_unstructured_string_keeps_legacy_split():
    block, _ = normalize_block({'type':'bullet_group', 'content':'登记缺陷；复核结果'})
    assert block['content'] == ['登记缺陷', '复核结果']


@pytest.mark.parametrize('value', [
    '受理与分类：在门区受理位置判定对象类别并取得登记所需的身份信息，类别与登记同时完成',
    '证件核验分类：现场核验证件和通行资格，按核验结果登记。',
    '每台设备有主责标注：台账卡上写明自维、外部维保或甲方管理三种方式之一。',
])
def test_operational_label_and_action_remain_together(value):
    assert client_facing_prose(value)[0] == value
    assert apply_artifact_repairs({'steps':[value]})['steps'] == [value]


@pytest.mark.parametrize('value', [
    '分类规则：正文按三种语气分档，未覆盖项不进入正文。',
    '受理分类：本节把门区身份信息改写成正文句子，统一措辞。',
])
def test_explicit_authoring_still_removed(value):
    projected = client_facing_prose(value)[0]
    assert '正文' not in projected
    assert projected != value


def test_source_process_does_not_lose_first_step():
    steps=['受理与分类：在门区受理位置判定对象类别并取得登记所需的身份信息，类别与登记同时完成',
           '形式核对：核对证件并登记结果', '申请许可：通知甲方确认',
           '通行交接：取得确认后引导通行', '回写改进：登记需完善的接口']
    cid='CU-S05-03-05'
    fragment=apply_content_units([{'content_unit_id':cid, 'parent_section_id':'S05-03',
        'content':'五步流程如下。', 'archetype':'PROCESS','payload':{'steps':steps}}],
        base_fragment={},section_id='S05-03',planned_ids=[cid])
    normalized,_=normalize_fragment(apply_artifact_repairs(fragment),section_id='S05-03')
    assert normalized['processes'][0]['steps'] == steps
