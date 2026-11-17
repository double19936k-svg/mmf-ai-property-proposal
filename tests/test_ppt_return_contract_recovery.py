"""Bounded PPT rejection recovery: keep native text, identity guards and budgets."""
import json
import time
import pytest
from test_ppt_timeout_recovery import bridge, reply, unknown
from test_ppt_bounded_delivery import factory, payload
from providers.base import ProviderError, write_json
from providers.qoder_internal import QoderInternalProvider
from planning.ppt_delivery import execution_budget
from longform import request_ledger


def answer(path, value, model='native-test'):
    text=json.dumps(value,ensure_ascii=False)
    with path.open('a',encoding='utf8') as handle:
        handle.write(json.dumps({'type':'assistant','uuid':'answer-1','message':{
            'model':model,'content':[{'type':'text','text':text}]}},ensure_ascii=False)+'\n')
    return text


@pytest.mark.parametrize('sid',[None,'',False])
def test_missing_identity_returns_definitive_error_and_releases_slot(tmp_path,sid):
    b,t=bridge(tmp_path);b.claim('w1')
    value={'headline':'保留完整业务原文'}
    if sid is not None:value['slide_id']=sid
    original=answer(t,value)
    assert b.sweep()==['job1'] and b.sweep()==[]
    envelope=json.loads((b.q/'in/job1.response.json').read_text(encoding='utf8'))
    assert envelope['status']=='ERROR' and envelope['error_code']=='OUTPUT_CONTRACT_ERROR'
    assert envelope['body']==original
    assert (b.q/'answers/job1.answer').read_text(encoding='utf8')==original
    assert b.claim('w1')['job_id']=='job2' and b.sweep()==[]


def test_wrong_page_stays_unbound_and_wrong_route_still_stops(tmp_path):
    b,t=bridge(tmp_path);b.claim('w1');reply(t,slide='SL-999')
    assert b.sweep()==[] and len(b.active())==1 and not (b.q/'in/job1.response.json').exists()
    answer(t,{'headline':'缺页ID的答案'},model='other-model')
    assert b.sweep()==[] and (b.q/'STOP').exists()
    assert not (b.q/'in/job1.response.json').exists()


def test_worker_control_message_is_not_a_page_failure(tmp_path):
    b,t=bridge(tmp_path);b.claim('w1');answer(t,{'status':'NO_JOB_WITHIN_WAIT','worker':'w1'})
    assert b.sweep()==[] and len(b.active())==1


def test_explicit_request_identity_wins_over_an_example_in_prompt(tmp_path):
    b,t=bridge(tmp_path)
    request=b.q/'out/job1.request.json';job=json.loads(request.read_text(encoding='utf8'))
    job.update(slide_id='SL-001',prompt='示例 {"slide_id":"SL-999"}')
    write_json(request,job);b.claim('w1');reply(t)
    assert b.sweep()==['job1']


def rejected_provider(root,*,generation_mode='ppt_slide',foreign=None):
    provider=QoderInternalProvider({'queue_dir':str(root/'q')})
    request={'job_id':'job1','task_id':'task1','request_id':'req1','generation_mode':generation_mode,'slide_id':'SL-001'}
    write_json(root/'q/abandoned/job1.TIMEOUT.request.json',request)
    envelope={'status':'ERROR','error_code':'OUTPUT_CONTRACT_ERROR','error':'structured output missing slide_id',
              'job_id':'job1','task_id':'task1','body':'{"headline":"保留原回答"}'}
    if foreign:envelope.update(foreign)
    write_json(root/'q/in/job1.response.json',envelope)
    return provider,request


def test_late_rejection_is_explicit_and_preserves_the_native_error(tmp_path):
    provider,request=rejected_provider(tmp_path)
    with pytest.raises(ProviderError) as error:provider.collect_late_response(request,tmp_path/'attempt')
    assert error.value.error_code=='OUTPUT_CONTRACT_ERROR'
    assert (tmp_path/'q/late/job1.response.json').is_file()
    assert provider.collect_late_response(request,tmp_path/'attempt') is None


@pytest.mark.parametrize('foreign',[{'job_id':'other-job'},{'task_id':'other-task'}])
def test_foreign_late_error_cannot_settle_this_request(tmp_path,foreign):
    provider,request=rejected_provider(tmp_path,foreign=foreign)
    assert provider.collect_late_response(request,tmp_path/'attempt') is None


def test_word_late_error_keeps_legacy_behavior(tmp_path):
    provider,request=rejected_provider(tmp_path,generation_mode='word')
    assert provider.collect_late_response(request,tmp_path/'attempt') is None


def test_received_contract_error_retries_only_its_page(tmp_path):
    calls=[]
    def invoke(req,unit):
        sid=req['slide_id'];calls.append(sid)
        if sid=='SL-001' and calls.count(sid)==1:
            return None,{'status':'DEFERRED_OUTPUT_CONTRACT','error_code':'OUTPUT_CONTRACT_ERROR','invoked':True}
        return payload(sid),{'status':'SUCCESS','invoked':True}
    f=factory(tmp_path,3,1,invoke)
    result=f.generate_ppt()
    assert result['completed_slides']==3 and result['repair_count']==1
    assert calls==['SL-001','SL-001','SL-002','SL-003']
    assert execution_budget(f.ppt_root)['page_requests_started']==4


def test_persistent_contract_error_stops_after_two_attempts(tmp_path):
    calls=[]
    def invoke(req,unit):
        calls.append(req['slide_id']);return None,{'status':'DEFERRED_OUTPUT_CONTRACT','error_code':'OUTPUT_CONTRACT_ERROR','invoked':True}
    f=factory(tmp_path,1,1,invoke)
    assert f.generate_ppt()['completed_slides']==0 and len(calls)==2
    assert f.generate_ppt()['completed_slides']==0 and len(calls)==2
    assert not (f.ppt_root/'slides/SL-001/payload.json').exists()


@pytest.mark.parametrize('limit',['clock','calls'])
def test_contract_retry_still_obeys_existing_budgets(tmp_path,limit,monkeypatch):
    import planning.ppt_delivery as policy
    calls=[]
    f=factory(tmp_path,1,1)
    budget=execution_budget(f.ppt_root)
    original_seconds=policy.generation_seconds_left
    monkeypatch.setattr(policy,'execution_budget',lambda *args,**kwargs:budget)
    monkeypatch.setattr(policy,'generation_seconds_left',lambda b:0 if calls and limit=='clock' else original_seconds(b))
    def invoke(req,unit):
        calls.append(req['slide_id'])
        if limit=='calls':budget['page_requests_started']=7
        return None,{'status':'DEFERRED_OUTPUT_CONTRACT','error_code':'OUTPUT_CONTRACT_ERROR','invoked':True}
    f._invoke=invoke
    assert f.generate_ppt()['completed_slides']==0 and len(calls)==1


def late_factory(tmp_path,expired=False,foreign=None):
    f=factory(tmp_path,1,1)
    row=unknown(f);row['task_id']='task1'
    (f.run_root/'provider_unknown_requests.jsonl').write_text(json.dumps(row)+'\n',encoding='utf8')
    f.provider,_=rejected_provider(tmp_path,foreign=foreign)
    unit=f.ppt_root/'slides/SL-001';unit.mkdir(parents=True)
    write_json(unit/'audit.json',{'attempts':[{'attempt_id':1,'invoked':True,'status':'DEFERRED_NETWORK'}]})
    execution_budget(f.ppt_root,started_epoch=time.time()-4300 if expired else time.time())
    return f


def test_unknown_becomes_known_rejection_then_one_local_repair(tmp_path):
    f=late_factory(tmp_path);calls=[]
    f._invoke=lambda req,unit:(calls.append(req['slide_id']) or payload(req['slide_id']),{'status':'SUCCESS','invoked':True})
    assert f.generate_ppt()['completed_slides']==1 and calls==['SL-001']
    events=request_ledger.read_events(f.run_root/'provider_unknown_requests.jsonl')
    assert any(e.get('status')=='RESPONSE_REJECTED' and e.get('resolution')=='invalid_native_response' for e in events)
    assert not request_ledger.open_requests(events)


def test_late_rejection_does_not_extend_the_original_clock(tmp_path):
    f=late_factory(tmp_path,expired=True)
    f._invoke=lambda *args:pytest.fail('no calls after generation cutoff')
    assert f.generate_ppt()['completed_slides']==0
    assert not request_ledger.open_requests(request_ledger.read_events(f.run_root/'provider_unknown_requests.jsonl'))


def test_unidentified_response_stays_unknown_without_resend(tmp_path,monkeypatch):
    import planning.ppt_delivery as policy
    monkeypatch.setattr(policy,'LATE_RECONCILE_SECONDS',0)
    f=late_factory(tmp_path,foreign={'job_id':'other-job'})
    f._invoke=lambda *args:pytest.fail('never blindly resend a genuinely unknown request')
    assert f.generate_ppt()['completed_slides']==0
    assert request_ledger.open_requests(request_ledger.read_events(f.run_root/'provider_unknown_requests.jsonl'))
