import json,threading,time
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_zcode_account_connection import ctl,request
from test_ppt_bounded_delivery import factory,payload
from providers.base import ProviderError,write_json
from providers.capability import resolve_profile,recommended_parallelism,apply_to_request
from providers.execution_reliability import classify_provider_failure
from providers.zcode_account import ZCodeAccountProvider
from providers.openai_compatible import CURRENT_RUN_ID
from zcode_account import connection as mod
from zcode_account.policy import prepare_ppt_policy,generation_call_cap,partial_execution_issues

def test_native_profile_is_serial_and_passes_real_reasoning():
    for speed,effort in [('fast','low'),('balanced','low'),('deep','high')]:
        profile=resolve_profile('zcode_cn',{'model':'account:bigmodel-individual-coding-plan/GLM-5.3-Flash'},speed_profile=speed)
        effective=profile['effective_settings']
        assert effective['capability_assumption']!='offline mock profile'
        assert effective['supports_parallel'] is False and recommended_parallelism('zcode_cn',3)==1
        assert apply_to_request({},profile)['reasoning_effort']==effort

def test_new_plan_budget_is_persisted_and_old_run_is_not_enlarged(tmp_path):
    jobs=tmp_path/'new';plan=prepare_ppt_policy(jobs,'new','Flash',29,35)
    assert generation_call_cap(jobs)==35
    assert prepare_ppt_policy(jobs,'new','Flash',29,35)==plan
    with pytest.raises(ProviderError):prepare_ppt_policy(jobs,'new','Flash',30,36)
    old=tmp_path/'old';old.mkdir();write_json(old/'marker.json',{'task_id':'old-ppt-SL-001','status':'unknown_or_failed'})
    before=(old/'marker.json').read_bytes()
    resumed=prepare_ppt_policy(old,'old','Flash',29,35)
    assert resumed['legacy'] is False and resumed['prior_generation_calls']==1 and resumed['generation_call_cap']==36
    assert generation_call_cap(old)==36 and (old/'.ppt_policy').is_file()
    assert (old/'marker.json').read_bytes()==before
    with pytest.raises(ProviderError):prepare_ppt_policy(old,'old','Flash',30,36)

def test_29_page_factory_and_real_adapter_share_one_bounded_contract(ctl,tmp_path,monkeypatch):
    c,calls=ctl;root=tmp_path/'ppt-run';f=factory(root,29,2);f.run_root=root;f.provider_name='zcode_cn'
    provider=ZCodeAccountProvider({'provider_name':'zcode_cn','provider_type':'zcode_account','model':c.selection['model']})
    f.provider=provider;current={};seen=[]
    def call(self,method,params,**kw):
        calls.append(params);return {'text':json.dumps(payload(current['sid']),ensure_ascii=False),'selection':c.selection['ref']}
    monkeypatch.setattr(mod.Client,'call',call)
    def invoke(req,unit):
        current['sid']=req['slide_id'];seen.append(req.copy())
        token=CURRENT_RUN_ID.set(root.name)
        try:out=provider.invoke_structured(apply_to_request(req,resolve_profile('zcode_cn',provider.config)),unit)
        finally:CURRENT_RUN_ID.reset(token)
        return out,{'status':'SUCCESS','invoked':True}
    f._invoke=invoke
    assert f.generate_ppt()['completed_slides']==29 and len(calls)==29
    assert all(r['response_budget_seconds']==180 and r['generation_deadline_epoch']>time.time() for r in seen)
    obs=json.loads((f.ppt_root/'execution_observation.json').read_text())
    assert obs['workers']==1 and len(obs['outcomes'])==29
    assert f.generate_ppt()['completed_slides']==29 and len(calls)==29
    assert all(r['reasoningEffort']=='low' for r in calls)

def test_exhausted_contract_stops_deck_before_more_attempts(tmp_path):
    seen=[]
    def invoke(req,unit):seen.append(req);return None,{'status':'DEFERRED_CALL_BUDGET','invoked':False}
    f=factory(tmp_path,29,2,invoke);f.run_root=tmp_path;f.provider_name='zcode_cn'
    f.provider=SimpleNamespace(prepare_ppt_run=lambda *a:{'page_call_cap':35})
    assert f.generate_ppt()['completed_slides']==0 and len(seen)==1

def test_unknown_old_ppt_page_cannot_be_resent_under_new_attempt_id(ctl,tmp_path,monkeypatch):
    c,calls=ctl
    def fail(*a,**kw):raise mod.ProtocolError('synthetic unknown',code='ZCODE_RESPONSE_UNKNOWN')
    monkeypatch.setattr(mod.Client,'call',fail)
    with pytest.raises(ProviderError):c.execute(request(task_id='run-ppt-SL-001-A1',generation_mode='ppt_slide',slide_id='SL-001'),tmp_path/'one')
    with pytest.raises(ProviderError,match='未作为新尝试重发'):
        c.execute(request(task_id='run-ppt-SL-001-A2',request_id='new-id',generation_mode='ppt_slide',slide_id='SL-001'),tmp_path/'two')
    assert not calls

def test_queue_wait_is_not_subtracted_from_response_budget(ctl,tmp_path,monkeypatch):
    c,calls=ctl;c.execution_lock.acquire();result=[]
    req=request(generation_mode='ppt_slide',response_deadline_epoch=time.time()+.05,response_budget_seconds=180,generation_deadline_epoch=time.time()+500)
    def work():
        try:result.append(c.execute(req,tmp_path/'task'))
        except Exception as exc:result.append(exc)
    worker=threading.Thread(target=work);worker.start();time.sleep(.25)
    assert any(e['phase']=='queued' for e in c.events)
    c.execution_lock.release();worker.join(timeout=3)
    assert len(result)==1 and isinstance(result[0],dict)
    assert 179<calls[0][2]['timeout']<=180
    assert any(e['phase']=='dispatch_ready' for e in c.events)

@pytest.mark.parametrize('native,expected',[('ZCODE_RESPONSE_UNKNOWN','RESPONSE_UNKNOWN'),('ZCODE_RUN_BUDGET','CALL_BUDGET_EXHAUSTED'),('ZCODE_NOT_DISPATCHED','NOT_DISPATCHED')])
def test_distinct_native_states_remain_distinct(native,expected):assert classify_provider_failure(native)==expected

def test_partial_view_uses_execution_evidence_without_editing_qa(tmp_path):
    audit=tmp_path/'longform/ppt/slides/SL-001/audit.json'
    write_json(audit,{'attempts':[{'error':'本轮ZCode已达到8次请求上限'}]})
    qa=tmp_path/'artifact_qa_report.json';write_json(qa,{'status':'BLOCK','findings':[{'issue':'实际物理页数未达到约定下限'}]})
    original=qa.read_bytes();issues=partial_execution_issues(tmp_path)
    assert '调用次数已用尽' in issues[0]['label'] and '人工' not in issues[0]['label']
    assert qa.read_bytes()==original

@pytest.mark.parametrize('code,sent',[('ZCODE_RESPONSE_UNKNOWN',True),('ZCODE_NOT_DISPATCHED',False),('ZCODE_RUN_BUDGET',False)])
def test_orchestrator_ledger_keeps_unknown_and_not_sent_distinct(tmp_path,code,sent):
    from test_mmf006e_r21_q01_q04 import _minimal_factory
    from longform import request_ledger
    calls=[]
    def fail(req,path):calls.append(req);raise ProviderError('synthetic ZCode failure',error_code=code)
    p=SimpleNamespace(config={'provider_name':'zcode_cn','provider_type':'zcode_account','model':'Flash'},invoke_structured=fail)
    f=_minimal_factory(tmp_path,p);f.provider_name='zcode_cn'
    req={'task_id':'synthetic-ppt-SL-001-A1','generation_mode':'ppt_slide','slide_id':'SL-001','prompt':'synthetic','required_keys':['slide_id']}
    result,audit=f._invoke(req,tmp_path/'attempt')
    assert result is None and audit['invoked'] is sent and audit['native_error_code']==code
    rows=request_ledger.read_events(tmp_path/request_ledger.LEDGER_FILENAME)
    if sent:
        assert rows[-1]['status']==request_ledger.RESPONSE_UNKNOWN
        assert request_ledger.open_requests(rows)
        assert f._invoke(req,tmp_path/'again')[1]['invoked'] is False and len(calls)==1
    else:assert rows[-1]['resolution']=='not_dispatched' and not request_ledger.open_requests(rows)
