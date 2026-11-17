from __future__ import annotations
import json
import threading
import time
from pathlib import Path
import pytest
from test_ppt_bounded_delivery import factory, payload
from providers.base import write_json, ProviderError
from providers.qoder_internal import QoderInternalProvider, response_wait_seconds
from providers.qoder_ppt_bridge import PPTBridge
from planning.ppt_delivery import completion_state, execution_budget


def test_single_timeout_does_not_cancel_remaining_pages(tmp_path):
    calls = []
    def invoke(req, unit):
        sid = req['slide_id']; calls.append(sid)
        return (None, {'status': 'DEFERRED_NETWORK', 'invoked': True}) if sid == 'SL-001' else (payload(sid), {'status': 'SUCCESS'})
    f = factory(tmp_path, 12, 1, invoke)
    result = f.generate_ppt()
    assert len(calls) == 12 and result['completed_slides'] == 11
    statuses = json.loads((f.ppt_root/'execution_observation.json').read_text(encoding='utf-8'))['outcomes']
    assert statuses[0]['status'] == 'DEFERRED_NETWORK'
    assert all(s['status'] in {'COMPLETED', 'COMPLETED_WITH_WARNING'} for s in statuses[1:])


@pytest.mark.parametrize('reason', ['DEFERRED_PROVIDER_AUTH', 'DEFERRED_PLATFORM_CONFIRMATION'])
def test_actual_systemic_block_stops_new_calls_with_true_reason(tmp_path, reason):
    calls = []
    def invoke(req, unit):
        calls.append(req['slide_id']); return None, {'status': reason}
    f = factory(tmp_path, 8, 1, invoke)
    assert f.generate_ppt()['status'] == 'DEFERRED_EXTERNAL'
    assert len(calls) == 1
    obs = json.loads((f.ppt_root/'execution_observation.json').read_text(encoding='utf-8'))
    assert {r['status'] for r in obs['outcomes']} == {reason}


def test_missing_source_blocks_only_its_page(tmp_path):
    f = factory(tmp_path, 4, 1)
    f.inputs['ppt_plan']['slide_storyboard'][0]['evidence_requirement'] = 'SOURCE_REQUIRED'
    assert f.generate_ppt()['completed_slides'] == 3


def unknown(f, sid='SL-001'):
    f.run_root = f.ppt_root.parent.parent
    row = {'request_id': 'req1', 'request_fingerprint': 'sig1', 'job_id': 'job1', 'slide_id': sid, 'status': 'RESPONSE_UNKNOWN'}
    (f.run_root/'provider_unknown_requests.jsonl').write_text(json.dumps(row)+'\n', encoding='utf-8')
    return row


def test_late_reply_recovered_even_after_two_attempts_and_full_call_budget(tmp_path):
    f = factory(tmp_path, 1); unknown(f)
    unit = f.ppt_root/'slides/SL-001'; unit.mkdir(parents=True)
    write_json(unit/'audit.json', {'attempts': [{'attempt_id':1,'invoked':True}, {'attempt_id':2,'invoked':False,'status':'UNKNOWN_REQUEST_REVIEW'}]})
    b = execution_budget(f.ppt_root); b['page_requests_started'] = 7; write_json(f.ppt_root/'execution_budget.json', b)
    f._collect_late_response = lambda origin, probe, path: (payload(probe['slide_id']), {'status':'SUCCESS', 'invoked':False,'late_response_recovered':True})
    f._invoke = lambda *a: pytest.fail('late response must not resend or consume a call')
    result=f.generate_ppt()
    assert result['completed_slides'] == 1 and result['repair_count'] == 0
    assert execution_budget(f.ppt_root)['page_requests_started'] == 7


def test_late_reply_arrives_after_other_pages_without_parent_resume(tmp_path, monkeypatch):
    import planning.ppt_delivery as policy
    monkeypatch.setattr(policy, 'LATE_RECONCILE_SECONDS', .1)
    f = factory(tmp_path, 3, 1); unknown(f)
    recovered = []
    def collect(origin, req, path):
        if (f.ppt_root/'slides/SL-003/payload.json').exists():
            recovered.append(req['slide_id'])
            return payload(req['slide_id']), {'status':'SUCCESS','invoked':False,'late_response_recovered':True}
        return None
    f._collect_late_response = collect
    assert f.generate_ppt()['completed_slides'] == 3
    assert recovered == ['SL-001'] and execution_budget(f.ppt_root)['page_requests_started'] == 2


def test_unknown_probe_does_not_exhaust_retry_or_call_budget(tmp_path, monkeypatch):
    import planning.ppt_delivery as policy
    monkeypatch.setattr(policy,'LATE_RECONCILE_SECONDS',0)
    f = factory(tmp_path, 2, 1); unknown(f)
    f._collect_late_response = lambda *a: None
    assert f.generate_ppt()['completed_slides'] == 1
    assert f.generate_ppt()['completed_slides'] == 1
    assert execution_budget(f.ppt_root)['page_requests_started'] == 1
    audit = json.loads((f.ppt_root/'slides/SL-001/audit.json').read_text(encoding='utf-8'))
    assert audit['attempts'] == []


def test_partial_artifacts_in_all_locations_do_not_mean_completion(tmp_path):
    f = factory(tmp_path,3); f.generate_ppt()
    root = f.ppt_root.parent.parent
    write_json(root/'06_ppt_presentation_plan.json', f.inputs['ppt_plan'])
    assert completion_state(root)['complete']
    for folder in ['', 'artifact', 'last_valid_artifact']:
        directory=root/folder; directory.mkdir(exist_ok=True)
        (directory/'short.pptx').write_bytes(b'unchanged partial artifact')
    (f.ppt_root/'slides/SL-002/payload.json').unlink()
    state = completion_state(root)
    assert not state['complete'] and state['pending_slide_ids'] == ['SL-002'] and state['resume_available']
    assert f.generate_ppt()['completed_slides'] == 3
    assert completion_state(root)['complete']
    assert all((root/folder/'short.pptx').read_bytes() == b'unchanged partial artifact' for folder in ['', 'artifact', 'last_valid_artifact'])


def test_word_and_legacy_ppt_keep_completion_contract(tmp_path):
    assert completion_state(tmp_path) is None
    write_json(tmp_path/'06_ppt_presentation_plan.json', {'slide_storyboard':[{'slide_id':'SL-001'}]})
    assert completion_state(tmp_path) is None


def test_claimed_request_timeout_can_be_recovered(tmp_path):
    q=tmp_path/'q'
    provider=QoderInternalProvider({'queue_dir':str(q),'wait_timeout':.03,'poll_interval':.001})
    request={'task_id':'claim-timeout','job_id':'claim-timeout','required_keys':['slide_id'],'generation_mode':'ppt_slide'}
    def worker():
        path=q/'out/claim-timeout.request.json'
        for _ in range(1000):
            if path.exists():break
            time.sleep(.001)
        (q/'claims').mkdir(exist_ok=True)
        path.replace(q/'claims'/path.name)
    thread=threading.Thread(target=worker);thread.start()
    with pytest.raises(ProviderError):provider.invoke_structured(request,tmp_path/'attempt')
    thread.join(timeout=2)
    assert provider._request_location('claim-timeout') == 'abandoned'
    write_json(q/'in/claim-timeout.response.json', {'status':'OK','body':{'slide_id':'SL-001'}})
    result=provider.collect_late_response(request,tmp_path/'recovered')
    assert result is not None and result[0]['slide_id']=='SL-001' and result[1]['late_response_recovered']


def test_ppt_recommend_wait_is_bounded_but_word_unaffected():
    assert response_wait_seconds({'generation_mode':'ppt_recommend','response_deadline_epoch':220},1800,now=100)==120
    assert response_wait_seconds({'generation_mode':'word','response_deadline_epoch':220},1800,now=100)==1800


def bridge(tmp_path):
    now=time.time()
    write_json(tmp_path/'RUN_CONTRACT.json',{'instance_created_epoch':now,'deadline_epoch':now+5400,'generation_stop_epoch':now+4200,
               'caps':{'executions':4,'enqueues':70,'concurrency':2,'external_paid_api':0},'model':{'native_model_key':'native-test'}})
    transcript=tmp_path/'native.jsonl'; transcript.write_text('',encoding='utf-8')
    write_json(tmp_path/'runtime/worker_sessions.json',{'w1':{'session_id':'native-w1','transcript':str(transcript)},'w2':{'session_id':'native-w2','transcript':str(tmp_path/'native2.jsonl')}})
    (tmp_path/'native2.jsonl').write_text('',encoding='utf-8')
    b=PPTBridge(tmp_path)
    for i in range(1,4):
        write_json(b.q/f'out/job{i}.request.json',{'job_id':f'job{i}','task_id':f'task{i}', 'required_keys':['slide_id','headline'], 'prompt':json.dumps({'slide_id':f'SL-00{i}'})})
    return b,transcript


def reply(path, slide='SL-001', model='native-test', **extra):
    with path.open('a',encoding='utf-8') as f:
        f.write(json.dumps({'type':'assistant','uuid':'native-message','message':{'model':model,'content':[{'type':'text','text':json.dumps({'slide_id':slide,'headline':'业务安排'},ensure_ascii=False)}]},**extra},ensure_ascii=False)+'\n')


def test_autonomous_return_preserves_native_reply_and_is_idempotent(tmp_path):
    b,t=bridge(tmp_path); assert b.claim('w1')['status']=='CLAIMED'; reply(t)
    assert b.sweep()==['job1'] and b.sweep()==[]
    envelope=json.loads((b.q/'in/job1.response.json').read_text(encoding='utf-8'))
    assert envelope['source_provenance']['native_message_uuid']=='native-message'
    assert json.loads(envelope['body'])['headline']=='业务安排'
    assert b.claim('w1')['job_id']=='job2'
    assert b.sweep()==[] # Prior page's transcript text cannot answer a new page.


def test_bridge_rejects_wrong_model_and_keeps_unknown_jobs_unresent(tmp_path):
    b,t=bridge(tmp_path); b.claim('w1'); reply(t,model='other-model')
    assert b.sweep()==[] and (b.q/'STOP').exists() and not (b.q/'in/job1.response.json').exists()
    assert b.claim('w2')['status']=='HALT'


def test_bridge_claims_real_two_slots_and_obeys_original_deadline(tmp_path):
    b,t=bridge(tmp_path)
    assert b.claim('w1')['status']=='CLAIMED' and b.claim('w2')['status']=='CLAIMED'
    assert len(b.active())==2 and b.claim('w1')['status']=='BUSY'
    assert b.claim('w1',now=b.contract['generation_stop_epoch'])['status']=='HALT'
    assert b.health(now=b.contract['deadline_epoch'])['status']=='DEADLINE'


def test_bridge_idle_alert_does_not_need_parent_turn(tmp_path):
    b,t=bridge(tmp_path)
    assert b.health(now=b.contract['instance_created_epoch']+121)['status']=='STALLED'
    assert b.health()['model_invocation_capability'] is False


def test_bridge_does_not_claim_abandoned_jobs_again(tmp_path):
    b,t=bridge(tmp_path)
    for path in (b.q/'out').glob('*.json'):path.replace(b.q/'abandoned'/path.name)
    assert b.claim('w1')['status']=='IDLE'


def test_guard_counts_native_turns_including_an_active_unreturned_job(tmp_path):
    b,t=bridge(tmp_path)
    b.contract['caps']['model_turns']=2
    assert b.claim('w1')['status']=='CLAIMED'
    reply(t); assert b.sweep()==['job1']
    assert b.claim('w1')['job_id']=='job2'
    reply(t,slide='SL-002')
    assert b.health()['model_turns']==2 and (b.q/'STOP').read_text(encoding='utf-8').strip()=='MODEL_TURN_LIMIT'


def test_partial_resume_enters_production_worker_without_moving_artifacts(tmp_path, monkeypatch):
    import server, paths
    from dataclasses import replace
    from types import SimpleNamespace
    original_roots=paths.current()
    rid='ppt_resume'; root=tmp_path/rid;root.mkdir()
    write_json(root/'06_ppt_presentation_plan.json',{'execution_policy':{'profile':'bounded_90min_v1'},'slide_storyboard':[{'slide_id':'SL-001'}]})
    monkeypatch.setattr(paths,'current',lambda:replace(original_roots,runs_dir=tmp_path))
    monkeypatch.setattr(server,'load_run_status',lambda _: {'run_status':'generation_completed','provider_name':'mock'})
    monkeypatch.setattr(server,'_enrich_run',lambda x:x)
    monkeypatch.setattr(server,'SERVICE_STOPPING',False)
    monkeypatch.setattr(server,'GENERATION_JOBS',{})
    started=[]
    monkeypatch.setattr(server.threading,'Thread',lambda **kw:SimpleNamespace(start=lambda:started.append(kw['args'][0])))
    assert server._start_generation_job(rid,[],{})['status']=='generation_started'
    assert started==[rid]


def test_partial_terminal_cannot_be_reopened_by_heartbeat(tmp_path):
    from generation_lifecycle import write_progress
    path=tmp_path/'generation_status.json';write_json(path,{'status':'generation_partial'})
    assert write_progress(path,{'run_id':'partial','status':'running'},write_json) is False


def test_guard_closes_out_without_a_parent_chat_turn(tmp_path):
    import subprocess,sys
    now=time.time()-4231
    write_json(tmp_path/'RUN_CONTRACT.json',{'instance_created_epoch':now,'deadline_epoch':now+5400,'generation_stop_epoch':now+4200,
      'caps':{'executions':4,'enqueues':70,'concurrency':2,'external_paid_api':0},'model':{'native_model_key':'native-test'}})
    script=tmp_path/'closeout.py';script.write_text("from pathlib import Path\nPath('CLOSEOUT_DONE').write_text('closed',encoding='utf-8')\n",encoding='utf-8')
    tool=Path(__file__).resolve().parents[1]/'tools/ppt_queue_bridge.py'
    result=subprocess.run([sys.executable,'-B',str(tool),'serve','--instance',str(tmp_path),'--closeout-script',str(script)],capture_output=True,timeout=8)
    assert result.returncode==0,result.stderr.decode(errors='replace')
    assert (tmp_path/'CLOSEOUT_DONE').read_text(encoding='utf-8')=='closed'
    assert (tmp_path/'q/STOP').exists()


def test_history_keeps_partial_download_and_offers_resume(tmp_path,monkeypatch):
    import app_core,paths
    from dataclasses import replace
    roots=paths.current()
    monkeypatch.setattr(paths,'current',lambda:replace(roots,runs_dir=tmp_path))
    root=tmp_path/'history_ppt';root.mkdir()
    write_json(root/'brief.json',{'medium':'PPT','project_name':'partial','provider_name':'mock'})
    write_json(root/'knowledge_selection.json',{})
    write_json(root/'generation_input.json',{})
    write_json(root/'06_ppt_presentation_plan.json',{'execution_policy':{'profile':'bounded_90min_v1'},'slide_storyboard':[{'slide_id':'SL-001'}]})
    artifact=root/'artifact/short.pptx';artifact.parent.mkdir();artifact.write_bytes(b'kept')
    monkeypatch.setattr(app_core,'RUNS_DIR',tmp_path)
    monkeypatch.setattr(app_core,'resolve_run_artifact',lambda *args:artifact)
    row=app_core.list_runs()[0]
    assert row['run_status']=='generation_partial' and row['can_resume_recommendation'] and row['artifact_available']


def test_browser_keeps_polling_while_a_prior_partial_file_exists(tmp_path):
    import subprocess,shutil
    node=shutil.which('node');assert node
    html=(Path(__file__).resolve().parents[1]/'static/index.html').read_text(encoding='utf-8')
    poll='async function pollGenerationStatus(){'+html.split('async function pollGenerationStatus(){',1)[1].split('\nasync function stopGeneration()',1)[0]
    script="""const assert=require('assert');
let state={runId:'partial'}, resultCalls=0, pendingTimers=0;
const data={generated:true,run_status:'generation_in_progress',ppt_completion:{complete:false}};
const api=async()=>data,renderGenerationResult=()=>{resultCalls++},updateGenerationClock=()=>{},qs=()=>null;
const setTimeout=()=>{pendingTimers++};
"""+poll+"""
(async()=>{
await pollGenerationStatus();assert.equal(resultCalls,0);assert.equal(pendingTimers,1);
data.run_status='generation_partial';await pollGenerationStatus();assert.equal(resultCalls,1);
})();
"""
    result=subprocess.run([node,'-e',script],capture_output=True,timeout=8)
    assert result.returncode==0,result.stderr.decode(errors='replace')


def test_closeout_keeps_product_bytes_if_export_unavailable(tmp_path):
    import subprocess,sys,zipfile
    now=time.time();root=tmp_path/'instance';root.mkdir()
    write_json(root/'RUN_CONTRACT.json',{'instance_created_epoch':now,'deadline_epoch':now+5400})
    write_json(root/'runtime/current_run.json',{'run_id':'product'})
    run=root/'runs/product';run.mkdir(parents=True)
    write_json(run/'06_ppt_presentation_plan.json',{'execution_policy':{'profile':'bounded_90min_v1'},'slide_storyboard':[{'slide_id':'SL-001'}]})
    source=run/'product.pptx'
    with zipfile.ZipFile(source,'w') as z:z.writestr('ppt/slides/slide1.xml','<slide/>')
    tool=Path(__file__).resolve().parents[1]/'tools/ppt_closeout.py'
    result=subprocess.run([sys.executable,'-B',str(tool),'--instance',str(root),'--soffice',str(root/'missing.exe')],capture_output=True,timeout=8)
    assert result.returncode==1,result.stderr.decode(errors='replace')
    report=json.loads((root/'deliverables/CLOSEOUT_RESULT.json').read_text(encoding='utf-8'))
    assert report['status']=='PPT_SAVED_EXPORT_FAILED' and report['ppt_completion']['pending_slide_ids']==['SL-001']
    assert Path(report['pptx']).read_bytes()==source.read_bytes() and report['whole_delivery_90min_accepted'] is False
