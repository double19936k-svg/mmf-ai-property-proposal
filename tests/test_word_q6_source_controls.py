import copy
from governance.tender_controls import extract_controls,final_control_findings
from planning.planner import build_global_state
from longform.factory import _fact_drift,ContextPackBuilder,_repair_customer_text
from governance.final_editorial import SourceFacts
from governance.artifact_qa import apply_artifact_repairs
from governance.commitment_provenance import apply_local_repairs
from governance.table_fidelity import source_contract,require_source_tables,KEY,TableFidelityError
import pytest


def extraction():
 return {'paragraphs':[{'file_id':'F','para_id':'P1','text':'招标代理机构地址：香港中路20号','heading_path':['投标人须知']},{'file_id':'F','para_id':'P2','text':'生食物和熟食物的盛用容器、刀具严格区分，不得混用。','heading_path':['技术需求书']},{'file_id':'F','para_id':'P3','text':'厨房工作人员必须着工作服，不留长指甲，接触脏物后必须洗手，持健康证上岗。','heading_path':['技术需求书']}], 'tables':[{'file_id':'F','table_id':'T1','table_index':0,'heading_path':['技术需求书'],'columns':['岗位','人员配置（人）'],'rows':[['项目主管','1'],['工程','5'],['前台','2'],['合计','8']]}]}


def test_located_staff_table_enters_context_and_does_not_compare_each_role_to_total():
 c=extract_controls(extraction());p={'requirements':[],'project_facts':{},'source_controls':c}
 state=build_global_state(p,{},[],{'coverage_summary':{}},{'contracts':[],'topic_ownership':[]},{'dependencies':[]})
 assert state['staffing']['minimum_staffing']==8
 assert state['staffing']['roles']=={'项目主管':1,'工程':5,'前台':2}
 assert not _fact_drift('项目主管1人，工程5人，前台2人，合计8人',state)
 assert _fact_drift('工程6人，合计9人',state)
 ctx=ContextPackBuilder(state,{}, {},{}).word({'section_id':'arbitrary','source_requirements':[]},{})
 assert ctx['source_controls']['staffing']['source']['table_id']=='T1'
 assert ctx['confirmed_governance']['staffing']['minimum_staffing']==8


def test_empty_or_inconsistent_forms_cannot_supply_staffing():
 e=extraction();e['tables'][0]['rows'][-1][1]='99'
 assert extract_controls(e)['staffing']['status']=='unknown'
 e=extraction();e['tables'][0]['heading_path']=['投标文件格式']
 assert extract_controls(e)['staffing']['status']=='unknown'


def test_known_staffing_and_food_requirements_must_be_in_actual_text():
 controls=extract_controls(extraction())
 rows=[{'text':'人员最低配置未给出确定取值。坐落于香港中路20号','path':'paragraph[1]'}]
 f=final_control_findings(rows,controls);rules={x['rule_id'] for x in f}
 assert {'SOURCE_STAFFING_OMITTED','KNOWN_STAFFING_MARKED_UNKNOWN','SOURCE_SERVICE_REQUIREMENT_OMITTED','CONTACT_ADDRESS_AS_PROPERTY'}<=rules
 good=[{'text':'项目主管 | 1\n工程 | 5\n前台 | 2\n合计 | 8\n持健康证上岗，生熟分区，刀具区分；着工作服，不留长指甲，工作前洗手。'}]
 assert final_control_findings(good,controls)==[]
 # A deletion of one value cannot be hidden by complete heading counts.
 good[0]['text']=good[0]['text'].replace('工程 | 5','工程 | ')
 assert any(x['rule_id']=='SOURCE_STAFFING_OMITTED' for x in final_control_findings(good,controls))


def test_source_verification_failure_is_a_block():
 assert final_control_findings([{'text':'完整正文'}],{'source_verification_failed':True})[0]['severity']=='BLOCK'


@pytest.mark.parametrize('cell',['如采用外委方式检测','外委检测','人员最低配置、每日服务时长与响应时限'])
def test_table_cleanup_uses_identical_display_normalization_without_reanchoring(cell):
 t={'columns':['事项','方法'],'rows':[['核验',cell]]};t[KEY]=source_contract(t,'CU-ARBITRARY');original=copy.deepcopy(t[KEY])
 fixed=_repair_customer_text(apply_artifact_repairs(apply_local_repairs(t)),SourceFacts())
 assert fixed[KEY]==original
 require_source_tables(fixed)


def test_nonempty_cell_is_not_silently_deleted_by_narration_cleanup():
 text='整改完成信息与原判定标准要点'
 t={'columns':['事项','方法'],'rows':[['整改',text]]};t[KEY]=source_contract(t,'CU-ORIGINAL')
 repaired=apply_artifact_repairs(t)
 assert repaired['rows'][0][1].strip()
 assert repaired[KEY]==t[KEY]


def test_final_gate_uses_verified_tender_even_when_caller_omits_fact_context(tmp_path):
 from pathlib import Path
 import json,hashlib
 from docx import Document
 from governance.final_artifact_qa import evaluate_docx_artifact
 originals=tmp_path/'tender/originals';originals.mkdir(parents=True)
 raw=b'original frozen tender';(originals/'tender.txt').write_bytes(raw)
 e={'files':[{'file_id':'F','original_filename':'tender.txt','sha256':hashlib.sha256(raw).hexdigest()}],'paragraphs':[{'file_id':'F','para_id':'P1','text':'本项目配置前台2人。'}],'tables':[]}
 (tmp_path/'tender/extraction.json').write_text(json.dumps(e,ensure_ascii=False),encoding='utf-8')
 d=Document();d.add_heading('人员配置',level=1);d.add_paragraph('本项目配置前台2人，负责接待与受理登记。');p=tmp_path/'candidate.docx';d.save(p)
 result=evaluate_docx_artifact(p,brief={'medium':'WORD'},run_dir=tmp_path)
 assert not any(x.get('rule_id')=='C1' for x in result['findings'])
 (originals/'tender.txt').write_bytes(b'tampered')
 result=evaluate_docx_artifact(p,brief={'medium':'WORD'},run_dir=tmp_path)
 assert any(x.get('rule_id')=='SOURCE_CONTROL_BINDING_INVALID' for x in result['findings'])


def test_future_record_instruction_does_not_certify_present_completion(tmp_path):
 from docx import Document
 from governance.final_artifact_qa import evaluate_docx_artifact
 d=Document();d.add_heading('实施记录',level=1);d.add_paragraph('启动阶段结束后做一次小结，写明已完成项、待确认项与转入日常机制的事项。');p=tmp_path/'future.docx';d.save(p)
 r=evaluate_docx_artifact(p)
 assert not any(x.get('rule_id')=='UNSUPPORTED_EXISTING_STATE_CLAIM' and x.get('severity')=='BLOCK' for x in r['findings'])
 d.paragraphs[1].text='本项目已经完成全部启动工作。';d.save(p)
 r=evaluate_docx_artifact(p)
 assert any(x.get('rule_id') in ['UNSUPPORTED_EXISTING_STATE_CLAIM','UNSUPPORTED_COMPLETION_CLAIM','C5'] and x.get('severity')=='BLOCK' for x in r['findings'])
