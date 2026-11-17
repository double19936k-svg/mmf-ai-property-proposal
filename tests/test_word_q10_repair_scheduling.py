import copy
import pytest
from longform.document_repair import build_repair_plan


@pytest.mark.parametrize('text',[
    '厨房每餐后台面地面及时擦扫干净，器皿热水洗净后保存',
    '厨房排水保持畅通，不积存污水',
    '菜肴口味好、咸淡适中并保证饭菜热度',
    '房屋外观整洁并保持雨水污水管道畅通',
])
def test_food_and_building_service_standards_are_not_reference_only(text):
    from planning.content_engineering import classify_technical_scope,classify_requirement
    row={'normalized_requirement':text,'domain':'service_standard','mandatory_level':'MUST'}
    assert classify_requirement(row)=='TECHNICAL'
    assert classify_technical_scope(row)=='TECHNICAL_INCLUDED'


def test_food_vocabulary_cannot_promote_commercial_or_bidder_proof():
    from planning.content_engineering import classify_technical_scope
    assert classify_technical_scope({'text':'厨房设备采购报价按价格表填写'})=='PRICING_EXCLUDED'
    assert classify_technical_scope({'text':'投标人须提供餐饮类似项目业绩证明'}).endswith('_EXCLUDED')


def test_service_gaps_are_prioritized_and_same_section_is_one_call(tmp_path):
    texts=[('R01','评分横向比较优得8分','scoring','S01-01'),
           ('R02','费用预算不超过人民币100万元','contract_or_duration','S01-01'),
           ('R03','接待人员须满足年龄与学历条件','staffing','S02-01'),
           ('R04','刀具和器具用后热水清洗保存','catering','S03-01'),
           ('R05','厨房排水保持畅通并清除污物','catering','S03-01')]
    matrix={'matrix':[{'requirement_id':rid,'requirement_text':text,'requirement_domain':domain,
        'primary_section_id':sid,'mandatory_level':'MUST','scope_class':'TECHNICAL_INCLUDED'} for rid,text,domain,sid in texts]}
    qa={'coverage_gate':{'status':'FAIL','missing':[x[0] for x in texts]},'artifact':{'sections':[]}}
    before=copy.deepcopy(qa)
    states={sid:'COMPLETED' for _,_,_,sid in texts}
    plan=build_repair_plan(qa=qa,word={'section_states':states},contracts=[{'section_id':sid} for sid in states],
        requirement_matrix=matrix,run_dir=tmp_path)
    requirements=[r for r in plan['supplement'] if r['failure_class']=='REQUIREMENT_GAP']
    assert [r['requirement_gap_class'] for r in requirements[:2]]==['SERVICE_OBLIGATION','SERVICE_OBLIGATION']
    kitchen=next(r for r in requirements if r['section_id']=='S03-01')
    assert kitchen['grouped_requirement_ids']==['R04','R05']
    assert {r['requirement_id'] for r in kitchen['focus']}=={'R04','R05'}
    assert {rid for r in requirements for rid in r['grouped_requirement_ids']}==set(qa['coverage_gate']['missing'])
    assert qa==before and qa['coverage_gate']['status']=='FAIL'
