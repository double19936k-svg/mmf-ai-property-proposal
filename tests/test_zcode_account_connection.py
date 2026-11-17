"""Offline protocol and identity checks; never read credentials or invoke a model."""
import io
import json
import queue
import threading
import pytest
from providers.base import ProviderError
from zcode_account import connection as mod
from zcode_account.connection import Connection,trusted_login_url
from zcode_account.protocol import Client,ProtocolError,environment
from providers.zcode_account import ZCodeAccountProvider,zcode_provider_rows

REF={'providerId':'account:bigmodel-individual-coding-plan','modelId':'GLM-test'}
MID=REF['providerId']+'/'+REF['modelId']


@pytest.fixture
def ctl(tmp_path,monkeypatch):
    monkeypatch.setenv('MMF_ZCODE_ACCOUNT_ENABLED','1')
    monkeypatch.setattr(mod,'verified_installation',lambda:{'cli_version':'synthetic'})
    monkeypatch.setattr(mod,'set_configured_selection',lambda *args:None)
    c=Connection(tmp_path/'ctl');c.state.update(status='ready',models=[{'id':MID,'ref':REF}]);c.select_model(MID)
    calls=[]
    class FakeClient:
        workspace={'workspacePath':'synthetic','workspaceKey':'synthetic'}
        def __init__(self,*args):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def call(self,method,params,**kwargs):
            calls.append((method,params,kwargs))
            return {'text':'{"ok":true}','selection':REF,'usage':None,'finishReason':'stop'}
    monkeypatch.setattr(mod,'Client',FakeClient)
    monkeypatch.setattr(mod,'connection',lambda:c)
    return c,calls


def request(**kw):return {'run_id':'synthetic-run','task_id':'t1','request_id':'request-1','prompt':'synthetic','required_keys':['ok'],**kw}


def test_disabled_route_has_no_provider_and_no_available_state(ctl,monkeypatch):
    c,calls=ctl;monkeypatch.delenv('MMF_ZCODE_ACCOUNT_ENABLED');assert zcode_provider_rows()==[]
    assert not c.health()['available'];assert not calls


@pytest.mark.parametrize('url,expected',[
    ('https://bigmodel.cn/login?appId=zcode',True),('https://zcode.z.ai/api/v1/oauth/cli/callback/bigmodel',True),
    ('https://bigmodel.cn.evil.example/login',False),('http://bigmodel.cn/login',False),
    ('https://evil@bigmodel.cn/login',False),('https://bigmodel.cn:444/login',False)])
def test_official_url_allowlist(url,expected):assert trusted_login_url(url)==expected


def test_environment_isolates_vendor_home_and_removes_inherited_secrets(tmp_path,monkeypatch):
    cli=tmp_path/'vendor/glm/zcode.cjs';cli.parent.mkdir(parents=True);cli.write_text('')
    built=tmp_path/'vendor/config/provider/zcode-builtin.json';built.parent.mkdir(parents=True);built.write_text('{}')
    node=tmp_path/'node.exe';node.write_text('')
    monkeypatch.setenv('MMF_ZCODE_CLI',str(cli));monkeypatch.setenv('MMF_ZCODE_NODE',str(node))
    for name in ('ZCODE_BASE_URL','ZAI_API_KEY','BIGMODEL_API_KEY','QODERCN_CONFIG_DIR','NODE_OPTIONS'):
        monkeypatch.setenv(name,'must-not-inherit')
    account=tmp_path/'own';env=environment(account)
    assert env['USERPROFILE']==str(account/'home') and env['HOME']==str(account/'home')
    assert env['ZCODE_DATA_BASE_DIR']==str(account/'data')
    assert env['ZCODE_BUILTIN_PROVIDER_CONFIG_FILE']==str(built)
    assert env['ZCODE_CREDENTIAL_SECRET_FILE']==str(account/'secure'/'credential-secret')
    assert 32<=(account/'secure'/'credential-secret').stat().st_size<=4096
    assert 'must-not-inherit' not in (account/'secure'/'credential-secret').read_text(encoding='utf-8')
    assert all(env.get(k)!='must-not-inherit' for k in ('ZCODE_BASE_URL','ZAI_API_KEY','BIGMODEL_API_KEY','QODERCN_CONFIG_DIR','NODE_OPTIONS'))
    conf=json.loads((account/'home/.zcode/cli/config.json').read_text())
    assert not any(conf['features'].values()) and conf['hooks']['enabled'] is False


def test_empty_session_inspection_never_generates_and_filters_other_regions():
    c=object.__new__(Client);c.workspace={'workspacePath':'synthetic','workspaceKey':'synthetic'};calls=[]
    def call(method,params,**kw):
        calls.append((method,params))
        return {'session':{'sessionId':'synthetic'},'settings':{'model':{'available':[
            {'ref':REF,'label':'domestic'},
            {'ref':{'providerId':'account:zai-individual-coding-plan','modelId':'x'}},
            {'ref':{'providerId':'bigmodel-api','modelId':'x'}}]}}}
    c.call=call;result=c.inspect()
    assert len(result['models'])==1 and result['models'][0]['ref']==REF
    assert [m for m,p in calls]==['session/create','session/close']
    assert calls[0][1]['titleGenerationEnabled'] is False and calls[0][1]['toolAllowlist']==[]
    assert result['model_requests']==0


def test_no_model_selection_until_directory_ready(ctl):
    c,calls=ctl
    with pytest.raises(ProviderError):c.select_model('invented')
    c.state['status']='not_connected'
    with pytest.raises(ProviderError):c.select_model(MID)
    assert not calls


def test_explicit_request_roundtrip_and_no_duplicate_send(ctl,tmp_path):
    c,calls=ctl;r=c.execute(request(),tmp_path/'task')
    assert c.execute(request(),tmp_path/'repeat')==r and len(calls)==1
    method,params,kw=calls[0];assert method=='workspace/generateText' and params['tools']==[]
    assert r['usage'] is None and r['cost_availability']=='NOT_AVAILABLE'
    assert c.snapshot()['run_progress']['synthetic-run']=={'admitted':1,'returned':1}


def test_changed_content_same_identity_not_resent(ctl,tmp_path):
    c,calls=ctl;c.execute(request(),tmp_path/'one')
    with pytest.raises(ProviderError,match='身份'):c.execute(request(prompt='changed'),tmp_path/'two')
    assert len(calls)==1


def test_unknown_outcome_survives_restart_without_resend(ctl,tmp_path,monkeypatch):
    c,calls=ctl
    def fail(*args,**kw):calls.append('failed');raise ProtocolError('timeout')
    monkeypatch.setattr(mod.Client,'call',fail)
    with pytest.raises(ProviderError):c.execute(request(),tmp_path/'one')
    restarted=Connection(c.root);restarted.state.update(c.state)
    with pytest.raises(ProviderError,match='自动重发'):restarted.execute(request(),tmp_path/'two')
    assert calls==['failed']


def test_bounded_admissions_and_preexisting_answers_preserved(ctl,tmp_path):
    c,calls=ctl
    for i in range(8):c.execute(request(request_id=str(i)),tmp_path/str(i))
    with pytest.raises(ProviderError,match='8次'):c.execute(request(request_id='9'),tmp_path/'9')
    assert len(calls)==8 and c.execute(request(request_id='0'),tmp_path/'again')['body']=='{"ok":true}'


def test_recognition_budget_and_low_effort_reach_transport(ctl,tmp_path):
    c,calls=ctl
    for i in range(48):c.execute(request(request_id=str(i),generation_mode='tender_requirement_recognition',reasoning_effort='low'),tmp_path/str(i))
    with pytest.raises(ProviderError,match='48次'):c.execute(request(request_id='49',generation_mode='tender_requirement_recognition'),tmp_path/'49')
    assert len(calls)==48 and calls[0][1]['reasoningEffort']=='low'
    c.execute(request(request_id='first-generation',generation_mode='solution'),tmp_path/'generation')
    assert len(calls)==49  # Recognition does not consume the separate 8-call writing budget.


def test_unknown_parent_cannot_be_resent_under_a_segment_identity(ctl,tmp_path,monkeypatch):
    c,calls=ctl
    def fail(*args,**kw):raise ProtocolError('synthetic timeout')
    monkeypatch.setattr(mod.Client,'call',fail)
    with pytest.raises(ProviderError):c.execute(request(task_id='parent',generation_mode='tender_requirement_recognition'),tmp_path/'parent')
    with pytest.raises(ProviderError,match='父块'):
        c.execute(request(task_id='parent-segment-child',request_id='child',generation_mode='tender_requirement_recognition'),tmp_path/'child')


def test_expired_deadline_never_sends(ctl,tmp_path):
    c,calls=ctl
    with pytest.raises(ProviderError):c.execute(request(response_deadline_epoch=1),tmp_path/'expired')
    assert not calls


def test_model_switch_same_run_not_silently_used(ctl,tmp_path):
    c,calls=ctl;c.execute(request(),tmp_path/'one')
    c.state['models'].append({'id':'different','ref':{**REF,'modelId':'second'}});c.select_model('different')
    with pytest.raises(ProviderError,match='绑定'):c.execute(request(request_id='2'),tmp_path/'two')
    assert len(calls)==1


def test_wrong_observed_model_retains_raw_answer_and_does_not_resend(ctl,tmp_path,monkeypatch):
    c,calls=ctl
    monkeypatch.setattr(mod.Client,'call',lambda *a,**kw:{'text':'retained','selection':{'providerId':'overseas','modelId':'wrong'}})
    with pytest.raises(ProviderError,match='不一致'):c.execute(request(),tmp_path/'one')
    assert json.loads((tmp_path/'one/zcode_raw_response.json').read_text())['body']=='retained'
    with pytest.raises(ProviderError,match='自动重发'):c.execute(request(),tmp_path/'two')


def test_health_and_capability_checks_do_not_generate(ctl,tmp_path):
    c,calls=ctl;p=ZCodeAccountProvider(zcode_provider_rows()[0])
    assert p.health_check()['available']
    with pytest.raises(ProviderError):p.invoke_structured(request(purpose='capability_probe'),tmp_path/'probe')
    assert not calls


def test_ppt_identity_mismatch_keeps_raw_envelope(ctl,tmp_path):
    c,calls=ctl;p=ZCodeAccountProvider(zcode_provider_rows()[0])
    with pytest.raises(ProviderError,match='页号'):p.invoke_structured(request(generation_mode='ppt_slide',slide_id='s1'),tmp_path/'slide')
    assert (tmp_path/'slide/zcode_raw_response.json').exists()


def test_protocol_denies_reverse_interactions_and_matches_request():
    c=object.__new__(Client);c.sequence=0;c.failed=False;c.queue=queue.Queue();sent=[];c.send=sent.append
    class Process:
        def poll(self):return None
    c.process=Process()
    c.queue.put({'id':'permission-1','method':'interaction/requestPermission','params':{}})
    c.queue.put({'id':'unrelated','result':'ignore'})
    c.queue.put({'id':'mmf-1','result':{'ok':True}})
    assert c.call('readonly',{})=={'ok':True}
    assert sent[1]['error']['code']==-32601


@pytest.mark.parametrize('payload',['not-json\n','[]\n','{"id":"x"}'])
def test_malformed_or_truncated_stdout_fails_closed(payload):
    c=object.__new__(Client);c.failed=False;c.queue=queue.Queue();c.process=type('P',(),{'stdout':io.StringIO(payload)})()
    c._read();assert c.failed


def test_disabled_manager_and_public_ui_do_not_change_old_selection(ctl,monkeypatch):
    from provider_view import public_status
    c,calls=ctl
    result=public_status({'provider_name':'zcode_cn','provider_type':'zcode_account',**c.health()})
    assert result['selectable'] and result['display_name']=='ZCode 国内账户（实验）'
    c.suspended=True;assert not c.health()['available']


def test_login_is_explicitly_domestic_and_no_model_request(tmp_path,monkeypatch):
    monkeypatch.setenv('MMF_ZCODE_ACCOUNT_ENABLED','1');c=Connection(tmp_path/'ctl');checks=[];commands=[]
    class FakeClient:
        def __init__(self,*a):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def inspect(self):
            checks.append('inspect')
            return {'models':[] if len(checks)==1 else [{'ref':REF,'label':'test','disabled_reason':''}]}
    class Login:
        stdout=io.StringIO('https://attacker.example/login\nhttps://bigmodel.cn/login?appId=zcode\n')
        stderr=io.StringIO('')
        def wait(self,timeout):return 0
    monkeypatch.setattr(mod,'Client',FakeClient);monkeypatch.setattr(mod,'identity',lambda:{'cli_sha256':'synthetic'})
    monkeypatch.setattr(mod,'open_login_page',lambda url:None)
    monkeypatch.setattr(mod,'spawn',lambda account,args:(commands.append(args) or Login()))
    c._connect(True)
    assert commands==[['login','bigmodel','--no-browser']]
    assert c.state['status']=='ready' and c.state['models'][0]['ref']==REF
    assert c.state['connection_check_model_requests']==0 and not c.state['login_url']
    assert not c.health()['available']  # A model still requires explicit selection.


def test_check_does_not_launch_login_and_late_check_cannot_reconnect_after_disconnect(tmp_path,monkeypatch):
    c=Connection(tmp_path/'ctl');c.suspended=True
    class FakeClient:
        def __init__(self,*a):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def inspect(self):return {'models':[{'ref':REF,'disabled_reason':''}]}
    monkeypatch.setattr(mod,'Client',FakeClient)
    monkeypatch.setattr(mod,'spawn',lambda *args:pytest.fail('read-only check launched login'))
    c._connect(False)
    assert c.state['status']=='not_connected'


def test_browser_reauthorization_is_not_skipped_when_cached_account_is_ready(ctl,monkeypatch):
    c,calls=ctl;commands=[]
    monkeypatch.setattr(c,'_check',lambda:c.state.update(status='ready',login_url='',login_stage='connected'))
    monkeypatch.setattr(mod,'identity',lambda:{'cli_sha256':'reviewed-test'})
    class Login:
        stdout=io.StringIO('');stderr=io.StringIO('')
        def wait(self,timeout):return 0
    monkeypatch.setattr(mod,'spawn',lambda a,args:(commands.append(args) or Login()))
    c._connect(True)
    assert commands==[['login','bigmodel','--no-browser']]
    assert c.state['status']=='ready' and c.selection['model']==MID
    assert not calls and not c.state['login_url']


def test_browser_can_cancel_login_without_overwriting_cancellation_with_error(ctl,monkeypatch):
    c,calls=ctl
    monkeypatch.setattr(c,'_check',lambda:c.state.update(status='not_connected'))
    class Login:
        stdout=io.StringIO('');stderr=io.StringIO('');stopped=False
        def poll(self):return 1 if self.stopped else None
        def terminate(self):self.stopped=True
        def wait(self,timeout):c.disconnect();return 1
    monkeypatch.setattr(mod,'spawn',lambda *a:Login())
    c._connect(True)
    assert c.state['status']=='not_connected' and c.state['login_stage']=='not_connected'
    assert c.state['next_action']=='check' and c.selection['model']==MID and not calls


def test_login_timeout_returns_browser_action_and_clears_stale_link(ctl,monkeypatch):
    import subprocess
    c,calls=ctl
    monkeypatch.setattr(c,'_check',lambda:c.state.update(status='not_connected'))
    class Login:
        stdout=io.StringIO('');stderr=io.StringIO('');killed=False
        def wait(self,timeout=None):
            if not self.killed:raise subprocess.TimeoutExpired('synthetic',timeout)
            return 1
        def kill(self):self.killed=True
    monkeypatch.setattr(mod,'spawn',lambda *a:Login())
    c._connect(True)
    assert c.state['reason_code']=='ZCODE_LOGIN_TIMEOUT' and c.state['next_action']=='retry'
    assert not c.state['login_url'] and c.state['login_expires_at'] is None and not c.control_busy
    assert not calls


def test_saved_account_rejection_still_opens_official_login(tmp_path,monkeypatch):
    monkeypatch.setenv('MMF_ZCODE_ACCOUNT_ENABLED','1')
    c=Connection(tmp_path/'ctl');commands=[];opened=[]
    def reject():raise ProtocolError('需要重新授权',code='ZCODE_LOGIN_REQUIRED')
    class Login:
        stdout=io.StringIO('https://bigmodel.cn/login?appId=zcode\n');stderr=io.StringIO('')
        def wait(self,timeout):return 0
    monkeypatch.setattr(c,'_check',reject)
    monkeypatch.setattr(mod,'identity',lambda:{'cli_sha256':'synthetic'})
    monkeypatch.setattr(mod,'open_login_page',opened.append)
    monkeypatch.setattr(mod,'spawn',lambda account,args:(commands.append(args) or Login()))
    c._connect(True)
    assert commands==[['login','bigmodel','--no-browser']]
    assert opened==['https://bigmodel.cn/login?appId=zcode']


def test_unsupported_version_has_clear_browser_action_and_does_not_start_login(ctl,monkeypatch):
    c,calls=ctl
    def reject():raise ProtocolError('当前版本尚未适配。',code='ZCODE_VERSION_UNREVIEWED')
    monkeypatch.setattr(c,'_check',reject)
    monkeypatch.setattr(mod,'spawn',lambda *a:pytest.fail('unsupported installation cannot log in'))
    c._connect(True)
    assert c.state['reason_code']=='ZCODE_VERSION_UNREVIEWED' and c.state['next_action']=='update'
    assert not c.state['login_url'] and not calls


@pytest.mark.parametrize('mode,budget',[('tender_requirement_recognition',600),('ppt_slide',240)])
def test_recognition_uses_its_existing_budget_and_other_tasks_stay_bounded(ctl,tmp_path,mode,budget):
    c,calls=ctl
    c.execute(request(generation_mode=mode,timeout_seconds=600),tmp_path/'call')
    assert budget-1<=calls[0][2]['timeout']<=budget


def test_recognition_still_obeys_original_deadline(ctl,tmp_path):
    import time
    c,calls=ctl
    c.execute(request(generation_mode='tender_requirement_recognition',timeout_seconds=600,response_deadline_epoch=time.time()+15),tmp_path/'call')
    assert 0<calls[0][2]['timeout']<=15


@pytest.mark.parametrize('code',['ZCODE_RESPONSE_UNKNOWN','ZCODE_REQUEST_INCOMPLETE','ZCODE_NO_AUTOMATIC_RESEND'])
def test_known_zcode_unknown_outcome_does_not_enter_generic_retry(code):
    from tender_intake.understanding import _is_transient_provider_error
    assert not _is_transient_provider_error(ProviderError('网络超时 timeout',error_code=code))


def test_disabled_full_model_preference_persists_without_dispatch(ctl,monkeypatch):
    c,calls=ctl;monkeypatch.setattr(c,'start_connect',lambda login=False:c.snapshot())
    c.set_model_preferences(False)
    assert c.snapshot()['glm53_enabled'] is False
    assert Connection(c.root).model_preferences['glm53_enabled'] is False
    c.set_model_preferences(True)
    assert c.selection['model']==MID and not calls


def test_disabled_full_model_cannot_be_selected_and_in_use_model_cannot_be_disabled(ctl,monkeypatch):
    c,calls=ctl
    c.selection={'model':REF['providerId']+'/GLM-5.3','ref':{**REF,'modelId':'GLM-5.3'}}
    with pytest.raises(ProviderError,match='先选择'):c.set_model_preferences(False)
    assert not calls


def test_check_filters_explicitly_disabled_full_model(ctl,monkeypatch):
    c,calls=ctl
    class CatalogClient:
        def __init__(self,*a):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def inspect(self):return {'models':[{'ref':{**REF,'modelId':m},'label':m,'disabled_reason':''} for m in ('GLM-5.3','GLM-5.3-Flash')]}
    monkeypatch.setattr(mod,'Client',CatalogClient);monkeypatch.setattr(mod,'identity',lambda:{'cli_sha256':'synthetic'})
    c.model_preferences={'glm53_enabled':False};c._check()
    assert [m['ref']['modelId'] for m in c.state['models']]==['GLM-5.3-Flash']
    with pytest.raises(ProviderError):c.select_model(REF['providerId']+'/GLM-5.3')
    assert not calls


def test_relogin_check_restores_saved_flash_without_enabling_full_or_generating(ctl,monkeypatch):
    c,calls=ctl;writes=[]
    flash={**REF,'modelId':'GLM-5.3-Flash'}
    c.selection={'model':REF['providerId']+'/GLM-5.3-Flash','ref':flash}
    c.model_preferences={'glm53_enabled':False}
    class CatalogClient:
        def __init__(self,*a):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def inspect(self):return {'models':[{'ref':{**REF,'modelId':m},'label':m,'disabled_reason':''} for m in ('GLM-5.3','GLM-5.3-Flash')],
            'current':{**REF,'modelId':'GLM-5.3'}}
    monkeypatch.setattr(mod,'Client',CatalogClient);monkeypatch.setattr(mod,'identity',lambda:{'cli_sha256':'synthetic'})
    monkeypatch.setattr(mod,'configured_selection',lambda a:{**REF,'modelId':'GLM-5.3'})
    monkeypatch.setattr(mod,'set_configured_selection',lambda a,r:writes.append(r))
    c._check()
    assert writes==[flash] and not calls
    assert c.state['status']=='ready' and not c.model_preferences['glm53_enabled']
    assert [m['ref']['modelId'] for m in c.state['models']]==['GLM-5.3-Flash']


def test_execution_restores_choice_before_admission_or_client_call(ctl,tmp_path,monkeypatch):
    c,calls=ctl;restored=[]
    monkeypatch.setattr(mod,'configured_selection',lambda a:{**REF,'modelId':'changed'})
    def restore(a,r):
        assert not list((c.root/'jobs').glob('*/*.json'))
        assert not calls
        restored.append(r)
    monkeypatch.setattr(mod,'set_configured_selection',restore)
    c.execute(request(),tmp_path/'call')
    assert restored==[REF] and len(calls)==1


def test_failed_preflight_has_no_admission_and_does_not_consume_budget(ctl,tmp_path,monkeypatch):
    c,calls=ctl
    monkeypatch.setattr(mod,'configured_selection',lambda a:None)
    def fail(*a):raise OSError('public config is not writable')
    monkeypatch.setattr(mod,'set_configured_selection',fail)
    with pytest.raises(ProviderError) as error:c.execute(request(),tmp_path/'call')
    assert error.value.error_code=='ZCODE_NOT_DISPATCHED'
    assert not list((c.root/'jobs').glob('*/*.json')) and not calls


def test_proven_not_sent_can_be_retried_explicitly_and_then_cached(ctl,tmp_path,monkeypatch):
    c,calls=ctl;attempts=[]
    def call(self,*a,**kw):
        attempts.append(1)
        if len(attempts)==1:raise ProtocolError('startup mismatch; not sent',code='ZCODE_NOT_DISPATCHED')
        return {'text':'{"ok":true}','selection':REF,'usage':None,'finishReason':'stop'}
    monkeypatch.setattr(mod.Client,'call',call)
    with pytest.raises(ProviderError) as error:c.execute(request(),tmp_path/'first')
    assert error.value.error_code=='ZCODE_NOT_DISPATCHED'
    marker=next(p for p in (c.root/'jobs').glob('*/*.json') if not p.name.endswith('.answer.json'))
    assert json.loads(marker.read_text())['status']=='not_dispatched' and len(attempts)==1
    first=c.execute(request(),tmp_path/'explicit_retry')
    assert c.execute(request(),tmp_path/'cached')==first and len(attempts)==2
    assert json.loads(marker.read_text())['pre_send_history'][0]['status']=='not_dispatched'
