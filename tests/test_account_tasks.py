import hashlib,json,threading,time,zipfile
from types import SimpleNamespace
import pytest
import account_tasks as a
from providers.base import ProviderError,write_json

Q={'kind':'structure_precedence','question':'是否以Word目录为准建立映射？','choices':[{'value':'word','label':'以Word目录为准'}]}
REQ={'run_id':'synthetic','task_id':'task','request_id':'req','prompt':'synthetic','required_keys':['ok']}

@pytest.fixture
def env(tmp_path,monkeypatch):
    monkeypatch.setattr(a,'root',lambda:tmp_path/'agents')
    monkeypatch.setattr(a,'run_visible',lambda run_id:True)
    class C:
        root=tmp_path/'controller';selection={'model':'synthetic'}
        calls=[]
        def event(self,*args,**kw):pass
        def execute(self,r,d,cancelled):
            self.calls.append(r)
            return {'session_id':'sess_synthetic','body':json.dumps({'mmf_interaction':Q}) if len(self.calls)==1 else '{"ok":true}'}
    return C()

def wait_for(test):
    end=time.monotonic()+4
    while time.monotonic()<end:
        value=test()
        if value:return value
        time.sleep(.02)
    pytest.fail('state did not arrive')

@pytest.mark.parametrize('provider',['qoder_account','zcode_cn'])
def test_question_reply_resumes_same_session_once_and_caches(env,tmp_path,provider):
    result=[]
    thread=threading.Thread(target=lambda:result.append(a.execute(provider,REQ,tmp_path/'task',env)));thread.start()
    row=wait_for(lambda:next((r for r in a.list_tasks() if r['state']=='waiting_for_user'),None))
    assert row['session_id']=='sess_synthetic' and not row['artifact_verified']
    a.answer(row['key'],row['nonce'],'以Word目录为准')
    with pytest.raises(ProviderError):a.answer(row['key'],row['nonce'],'重复回复')
    thread.join(timeout=4);assert not thread.is_alive() and len(result)==1
    assert len(env.calls)==2 and env.calls[1]['native_session_id']=='sess_synthetic'
    assert env.calls[1]['request_id']!=env.calls[0]['request_id']
    assert a.state(row['key'])['state']=='content_ready'
    assert a.execute(provider,REQ,tmp_path/'retry',env)==result[0] and len(env.calls)==2

def test_restart_keeps_question_and_recovers_only_matching_session(env,tmp_path,monkeypatch):
    key=a.task_key('zcode_cn',REQ,env.root);path=a.folder(key)
    write_json(path/'request.json',REQ)
    row={'key':key,'provider':'zcode_cn','run_id':'synthetic','task_id':'task','account_model':'synthetic','digest':hashlib.sha256(json.dumps(REQ,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest(),'state':'waiting_for_user','session_id':'sess_synthetic','turn':1,'answers':[],'question':Q,'nonce':'nonce','owner':'old-server'}
    a.put(key,row);env.calls.append({'previous_turn':True})
    from zcode_account import connection as mod
    monkeypatch.setattr(mod,'connection',lambda:env)
    recovered=[]
    assert a.list_tasks()[0]['state']=='waiting_for_user'
    a.answer(key,'nonce','以Word目录为准',lambda row,request:recovered.append(request))
    wait_for(lambda:recovered)
    assert len(env.calls)==2 and env.calls[-1]['native_session_id']=='sess_synthetic'
    assert a.cached_request('zcode_cn',REQ)==REQ

def test_interrupted_unknown_is_not_automatically_resubmitted(env,tmp_path):
    key=a.task_key('zcode_cn',REQ,env.root)
    a.put(key,{'key':key,'provider':'zcode_cn','run_id':'synthetic','task_id':'task','account_model':'synthetic','digest':hashlib.sha256(json.dumps(REQ,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),'state':'running','session_id':'sess_synthetic','turn':0,'owner':'previous-server'})
    assert a.list_tasks()[0]['state']=='interrupted'
    with pytest.raises(ProviderError):a.execute('zcode_cn',REQ,tmp_path/'again',env)
    assert not env.calls


def test_human_wait_does_not_expire_next_turn_but_keeps_whole_plan_deadline(env,tmp_path):
    now=time.time()
    request={**REQ,'response_deadline_epoch':now-600,'generation_deadline_epoch':now+80}
    key=a.task_key('qoder_account',request,env.root)
    row={'key':key,'provider':'qoder_account','run_id':'synthetic','task_id':'task','account_model':'synthetic',
         'digest':hashlib.sha256(json.dumps(request,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
         'state':'resuming','session_id':'sess_synthetic','turn':1,'response_window_seconds':180,
         'answers':[{'answer':'以Word目录为准'}],'owner':'old-server'}
    a.put(key,row);env.calls.append({'previous_turn':True})
    a.execute('qoder_account',request,tmp_path,env)
    assert now<env.calls[-1]['response_deadline_epoch']==request['generation_deadline_epoch']
    assert request['response_deadline_epoch']==now-600

def test_regular_text_and_nested_questions_are_not_control_events():
    assert a.interaction('是否以Word目录为准？') is None
    assert a.interaction(json.dumps({'body':{'mmf_interaction':Q}})) is None
    assert a.interaction('```json\n'+json.dumps({'mmf_interaction':Q})+'\n```')==Q

def test_api_provider_cannot_enter_account_runtime(env,tmp_path):
    with pytest.raises(ProviderError):a.execute('glm_zhipu',REQ,tmp_path,env)
    assert not env.calls

@pytest.mark.parametrize('provider',['qoder_account','zcode_cn'])
def test_old_dispatch_uses_unchanged_request_identity(env,tmp_path,provider):
    rid=REQ['run_id'];slot=REQ['request_id']
    jobs=env.root/'jobs'/hashlib.sha256(rid.encode()).hexdigest()[:16 if provider=='qoder_account' else 24]
    key=hashlib.sha256(((rid+'|'+slot) if provider=='qoder_account' else slot).encode()).hexdigest()[:24]
    write_json(jobs/(key+'.json'),{'status':'returned'})
    a.execute(provider,REQ,tmp_path,env)
    assert env.calls==[REQ] and a.list_tasks()==[]

def test_same_structure_decision_is_reused_without_asking_again(env,tmp_path,monkeypatch):
    seen=[]
    def execute(r,d,cancelled):
        seen.append(r);return {'session_id':'sess_shared','body':'{"ok":true}' if r.get('native_session_id') else json.dumps({'mmf_interaction':Q})}
    monkeypatch.setattr(env,'execute',execute)
    done=[];thread=threading.Thread(target=lambda:done.append(a.execute('zcode_cn',REQ,tmp_path,env)));thread.start()
    row=wait_for(lambda:next((r for r in a.list_tasks() if r['state']=='waiting_for_user'),None));a.answer(row['key'],row['nonce'],'以Word目录为准');thread.join(timeout=4)
    assert done
    a.execute('zcode_cn',{**REQ,'task_id':'second','request_id':'second'},tmp_path/'second',env)
    assert len(seen)==4 and not any(r['state']=='waiting_for_user' for r in a.list_tasks())

def test_permission_denial_is_visible_and_never_content_ready(env,tmp_path,monkeypatch):
    monkeypatch.setattr(env,'execute',lambda *args:{'body':'{"ok":true}','session_id':'sess_synthetic','permission_denied_tools':['Write']})
    with pytest.raises(ProviderError):a.execute('qoder_account',REQ,tmp_path,env)
    assert a.list_tasks()[0]['error_code']=='ACCOUNT_PERMISSION_UNSUPPORTED'
    assert not (a.folder(a.list_tasks()[0]['key'])/'final.json').exists()

def test_cancellation_while_waiting_preserves_receipt(env,tmp_path):
    errors=[]
    def work():
        try:a.execute('zcode_cn',REQ,tmp_path,env)
        except ProviderError as exc:errors.append(exc)
    thread=threading.Thread(target=work);thread.start()
    row=wait_for(lambda:next((r for r in a.list_tasks() if r['state']=='waiting_for_user'),None));a.cancel(row['key'])
    thread.join(timeout=3)
    assert errors and a.state(row['key'])['state']=='cancelled'
    assert (a.folder(row['key'])/'receipt-0.json').exists() and len(env.calls)==1

def test_artifact_done_requires_hash_and_original_final_state(tmp_path,monkeypatch):
    import paths
    roots=SimpleNamespace(runs_dir=tmp_path/'runs',output_root=tmp_path/'out');monkeypatch.setattr(paths,'current',lambda:roots)
    run=roots.runs_dir/'r1';run.mkdir(parents=True)
    file=run/'final.docx'
    with zipfile.ZipFile(file,'w') as z:z.writestr('[Content_Types].xml','x');z.writestr('word/document.xml','x')
    write_json(run/'generation_status.json',{'status':'generation_completed','result':{'provider_metadata':{'provider_name':'zcode_cn'},'delivery_status':'COMPLETED_CLEAN','artifact_name':file.name,'artifact_sha256':hashlib.sha256(file.read_bytes()).hexdigest()}})
    assert a.workflow_view('r1')['artifact_verified']
    file.write_bytes(b'corrupt');assert a.workflow_view('r1')['state']=='failed'
    data=json.loads((run/'generation_status.json').read_text());data['status']='generation_partial';data['result']['ppt_completion']={'complete':False,'completed_body_pages':1,'planned_body_pages':29};write_json(run/'generation_status.json',data)
    assert not a.workflow_view('r1')['artifact_verified']
