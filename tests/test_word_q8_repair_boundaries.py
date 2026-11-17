import copy
import json
from pathlib import Path
import pytest

from governance.customer_hygiene import apply_hygiene_tree
from governance.table_fidelity import source_contract, check_source_table, TableFidelityError
from governance.final_editorial import SourceFacts
from longform.factory import _repair_word_artifact
from longform.orchestrator import fragment_to_section
from longform.cu_audit import rebuild_content_unit_generation_audit
from planning.content_engineering import classify_technical_scope
from tender_intake.fact_sources import canonical_facts


def staff_facts():
    return SourceFacts({'tables':[{'table_id':'T1','columns':['岗位','人员配置'], 'rows':[['秩序维护',9],['室内保洁',4],['合计',13]]}]})


def boundary_table():
    value={'columns':['信息类型','不得升级为'], 'rows':[['来源事实','改写为更高的自设指标或额外的免费承诺'],['待补充事实','写成已经具备的资源、已经达成的结论或历史项目业绩']]}
    value['table_source_contract']=source_contract(value,'CU-BOUNDARY')
    return value


def test_prohibitions_keep_column_meaning_across_shared_repairs():
    table=boundary_table()
    repaired=_repair_word_artifact({'tables':[table]},staff_facts())['tables'][0]
    assert repaired['rows']==table['rows']
    assert repaired['table_source_contract']==table['table_source_contract']
    assert check_source_table(repaired)==[]
    assert apply_hygiene_tree('写成已经具备的资源、已经达成的结论或历史项目业绩')==''


def test_old_erased_negative_cells_recover_without_touching_input():
    table=boundary_table();table['rows'][0][1]=''
    original=copy.deepcopy(table)
    section=fragment_to_section({'tables':[table], 'body_blocks':[{'type':'paragraph','content':'按岗位执行工作。'}]},'边界')
    assert section['tables'][0]['rows'][0][1]==table['table_source_contract']['source_values'][1][1]
    assert section['source_table_repairs'][0]['content_unit_id']=='CU-BOUNDARY'
    assert table==original


def test_assembly_recovery_still_blocks_number_drift():
    table=boundary_table();table['rows'][0][0]='来源事实13人'
    table['table_source_contract']=source_contract(table,'CU-NUMERIC')
    table['rows'][0][0]='来源事实12人'
    with pytest.raises(TableFidelityError):fragment_to_section({'tables':[table]},'边界')


@pytest.mark.parametrize('text',[
    '本方在13人配置内设置替岗安排。',
    '秩序维护模式的核心是把有限的9人力量按区域排布。',
    '秩序维护按固定岗与流动岗组织，9人按门区与楼层分配。',
])
def test_source_staffing_survives_all_repair_stages(text):
    repaired=_repair_word_artifact({'text':text},staff_facts())['text']
    for number in ['13人','9人']:
        if number in text:assert number in repaired
    assert '按现场条件配置的人员' not in repaired


def test_source_capacity_does_not_authorize_unrelated_attendance():
    repaired=_repair_word_artifact({'text':'演练人数为13人，外援9人。'},staff_facts())['text']
    assert '13人' not in repaired and '9人' not in repaired


@pytest.mark.parametrize('text',[
    '大厨须年龄48岁以下，有两年以上学校餐厅工作经验。',
    '大厨须持有上岗证、健康证和厨师等级证书。',
    '工程人员须持电工上岗证并有三年以上工作经验。',
])
def test_personnel_service_conditions_are_technical(text):
    assert classify_technical_scope({'normalized_requirement':text})=='TECHNICAL_INCLUDED'


@pytest.mark.parametrize('text',[
    '投标人须提供营业执照复印件。',
    '投标人应提供项目经理履历与业绩证明。',
])
def test_bidder_evidence_remains_excluded(text):
    assert classify_technical_scope({'normalized_requirement':text}).endswith('_EXCLUDED')


def test_price_header_cannot_conflict_with_located_project_name():
    reqs=[{'requirement_id':'R1','sources':[{'file_id':'F','table_id':'T','source_excerpt':'项目名称 | 报价方式 | 投标总价'}]},
          {'requirement_id':'R2','sources':[{'file_id':'F','para_id':'P','source_excerpt':'项目名称：示例办公楼物业服务项目'}]}]
    extraction={'paragraphs':[{'file_id':'F','para_id':'P','text':'项目名称：示例办公楼物业服务项目'}],
                'tables':[{'file_id':'F','table_id':'T','columns':['字段','值'],'rows':[['项目名称','报价方式','投标总价']]}]}
    facts=canonical_facts(reqs,extraction)
    assert facts['project_name']['value']=='示例办公楼物业服务项目'
    assert len(facts['project_name']['candidates'])==1


def test_cu_audit_reconciles_envelope_and_nested_batch_json(tmp_path):
    def put(rel,value):
        p=tmp_path/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
    ids=['CU-A','CU-B','CU-C']
    put('08_document_hierarchy.json',{'content_units':[{'content_unit_id':v} for v in ids]})
    put('longform/word/batches/a/provider_structured_output.json',{'cu_envelope_text':'<<<CU_START:CU-A>>>\n岗位执行检查。\n<<<CU_END>>>'})
    put('longform/word/batches/b/provider_structured_output.json',{'sections':[{'content_unit_fragments':[{'content_unit_id':'CU-B'}]}]})
    put('longform/word/batches/c/provider_structured_output.json',{'content_unit_fragments':[{'content_unit_id':'CU-C'}]})
    for cu in ids:put(f'longform/word/sections/S01/cus/{cu}.json',{'content_unit_id':cu,'generation_origin':'provider_content_unit'})
    result=rebuild_content_unit_generation_audit(tmp_path)
    assert result['returned']==result['provider_received']==result['persisted']==3
