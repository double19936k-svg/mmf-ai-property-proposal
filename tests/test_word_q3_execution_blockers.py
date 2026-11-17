import copy
import json
import threading
import time

import pytest

from governance.final_editorial import SourceFacts
from governance.table_fidelity import KEY, TableFidelityError, require_source_tables, source_contract
from longform.orchestrator import _local_text_pipeline
from longform.envelope import parse_cu_envelope
from planning.budget_control import judge_content_unit
from providers.qoder_internal import QoderInternalProvider


def facts():
    return SourceFacts({'tables': [{'table_id': 'T-STAFF', 'file_id': 'F',
        'columns': ['岗位', '人数'], 'rows': [['前台', '2'], ['室内保洁', '4'],
            ['厨师', '3'], ['面点师', '2'], ['打荷', '2'], ['秩序维护', '7'], ['合计', '20']]}]})


@pytest.mark.parametrize('text', ['前台岗位配置2人', '室内保洁岗位配置4人', '厨师岗位配置3人',
    '面点师岗位配置2人', '打荷岗位配置2人', '秩序维护岗位配置7人', '前台人数为2人',
    '面点师岗位2人当班制作', '厨师岗位1人负责热菜烹制', '厨师岗位3人同时在岗负责制作'])
def test_verified_staff_role_grammar_survives_assembly(text):
    t = {'columns': ['安排'], 'rows': [[text]]}
    t[KEY] = source_contract(t, 'CU-STAFF')
    result = _local_text_pipeline(t, facts())
    assert result['rows'] == [[text]]
    require_source_tables(result)
    result['rows'][0][0] += '新增人员'
    with pytest.raises(TableFidelityError):
        require_source_tables(result)


@pytest.mark.parametrize('text', ['前台岗位配置3人', '厨师岗位配置3.5人', '保安岗位配置2人', '前台演练参与2人',
    '厨师岗位4人负责热菜烹制', '厨师岗位1.5人负责热菜烹制', '现有厨师岗位1人负责热菜烹制',
    '现有厨师岗位1人负责热菜烹制，随后登记事项并复核'])
def test_staffing_source_does_not_support_wrong_role_quantity_or_activity(text):
    t = {'columns': ['安排'], 'rows': [[text]]}; t[KEY] = source_contract(t, 'CU-STAFF')
    cleaned = _local_text_pipeline(t, facts())
    assert cleaned['rows'] != [[text]]
    with pytest.raises(TableFidelityError):
        require_source_tables(cleaned)


def test_envelope_and_json_charge_identical_prose_and_structure_once():
    payload = {'content': '核查设备运行并登记异常', 'columns': ['检查内容'], 'rows': [['核查并记录']]} 
    envelope = '<<<CU_START:CU-1|arch=STANDARD_TABLE>>>\n' + json.dumps(payload, ensure_ascii=False) + '\n<<<CU_END>>>'
    parsed = parse_cu_envelope(envelope, requested_ids=['CU-1'], archetypes={'CU-1': 'STANDARD_TABLE'})['blocks']['CU-1']
    contract = {'target_budget': 30}
    original = copy.deepcopy(parsed)
    strict = {'content': payload['content'], 'payload': {k: v for k, v in payload.items() if k != 'content'}}
    a = judge_content_unit(contract, content=parsed); b = judge_content_unit(contract, content=strict)
    assert a['actual_chars'] == b['actual_chars']
    assert a['structure_chars'] == b['structure_chars']
    assert a['prose_chars'] == len(payload['content'])
    assert parsed == original


def test_distinct_summary_is_not_silently_dropped_from_budget():
    row = {'content': '巡检说明', 'payload': {'summary': '不同的结构说明', 'items': ['记录']}}
    assert judge_content_unit({'target_budget': 30}, content=row)['actual_chars'] == 13


@pytest.mark.parametrize('mode,protocol,keys', [('longform_batch','batch_json',['batch_id','sections']),
    ('longform_section','strict_json',['section_id']), ('recommend','strict_json',['recommended_positive'])])
def test_queue_exports_a_machine_readable_response_contract(tmp_path, mode, protocol, keys):
    q = tmp_path/'q'; (q/'out').mkdir(parents=True); (q/'in').mkdir()
    p = QoderInternalProvider({'provider_name':'qoder_internal','queue_dir':str(q),'wait_timeout':5,'poll_interval':0.01,'test_mode':True})
    observed=[]; errors=[]
    def worker():
        try:
            deadline=time.monotonic()+5
            while not list((q/'out').glob('*.json')):
                assert time.monotonic()<deadline; time.sleep(0.01)
            f=next((q/'out').glob('*.json')); job=json.loads(f.read_text(encoding='utf-8')); observed.append(job)
            (q/'in'/(job['job_id']+'.response.json')).write_text(json.dumps({'job_id':job['job_id'],'status':'OK','body':{k:[] for k in keys}}),encoding='utf-8')
        except Exception as e: errors.append(e)
    t=threading.Thread(target=worker,daemon=True); t.start()
    p.invoke_structured({'task_id':'offline-contract','generation_mode':mode,'required_keys':keys,'json_schema':{'type':'object'},'prompt':'fixture'},tmp_path/'task')
    t.join(5); assert not errors
    assert observed[0]['response_protocol']==protocol
    assert observed[0]['required_keys']==sorted(keys)
    assert observed[0]['response_schema']=={'type':'object'}
