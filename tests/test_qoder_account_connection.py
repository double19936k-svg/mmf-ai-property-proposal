"""Offline boundary tests: no Qoder credentials, network or model calls."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

from providers.base import ProviderError
from providers.qoder_account import QoderAccountProvider, account_provider_rows
from qoder_account.connection import Connection


@pytest.fixture
def ctl(tmp_path, monkeypatch):
    monkeypatch.setenv('MMF_QODER_ACCOUNT_ENABLED','1')
    root=tmp_path/'ctl'
    controller=Connection(root)
    controller.environment()
    controller.state.update(status='ready',models=[{'value':'model-a','isEnabled':True}],message='ready')
    controller.select_model('model-a')
    fake=tmp_path/'fake_sdk.py'
    fake.write_text('''import sys,json,os,time
from pathlib import Path
p=json.load(sys.stdin)
counter=Path(os.environ['FAKE_QODER_COUNTER'])
with counter.open('a') as f:f.write('called\\n')
print(json.dumps({'event':'activity','phase':'generating','text_chars':5}),flush=True)
if os.environ.get('FAKE_QODER_WAIT'):time.sleep(60)
body=json.loads(p['prompt'])
print(json.dumps({'event':'result','body':json.dumps(body),'is_error':False,'subtype':'success','session_id':'synthetic-session','num_turns':1,'usage':None,'model_usage':None,'total_credits':None,'total_cost_usd':None}),flush=True)
''',encoding='utf-8')
    counter=tmp_path/'invocations.txt'
    monkeypatch.setenv('FAKE_QODER_COUNTER',str(counter))
    monkeypatch.setattr(controller,'command',lambda mode:[sys.executable,'-B',str(fake)])
    monkeypatch.setattr('qoder_account.connection.connection',lambda:controller)
    return controller,counter


def req(**changes):
    return {'run_id':'r1','task_id':'t1','job_id':'j1','request_id':'rq1','prompt':'{"ok":true}',
        'system_prompt':'synthetic','required_keys':['ok'],**changes}


def test_optional_off_has_no_provider_or_sdk_dependency(monkeypatch):
    monkeypatch.delenv('MMF_QODER_ACCOUNT_ENABLED',raising=False)
    assert account_provider_rows()==[]
    assert 'qoder_agent_sdk' not in sys.modules


def test_connect_does_not_execute_model_or_collect_credentials(ctl,monkeypatch):
    controller,counter=ctl
    modes=[]
    def one(mode,timeout=45):
        modes.append(mode)
        return {'event':'runtime','cli_path':'not-used'} if mode=='inspect' else {'event':'connection','status':'ready','models':[{'value':'model-a','isEnabled':True}],'model_requests':0}
    monkeypatch.setattr(controller,'_one',one)
    controller._connect(False)
    assert modes==['inspect','connect']
    assert not counter.exists()
    assert controller.snapshot()['connection_check_model_requests']==0


def test_missing_login_never_selectable(ctl,monkeypatch):
    controller,counter=ctl
    monkeypatch.setattr(controller,'_one',lambda mode,timeout=45:{'cli_path':'unused'} if mode=='inspect' else {'status':'authentication_required','models':[]})
    controller._connect(False)
    assert not controller.health()['available']
    from provider_view import public_status
    view=public_status({'provider_name':'qoder_account','provider_type':'qoder_account',**controller.health()})
    assert not view['selectable']
    assert not counter.exists()


def test_model_selection_must_be_account_enabled(ctl):
    controller,_=ctl
    controller.state['models'].append({'value':'locked','isEnabled':False})
    with pytest.raises(ProviderError):controller.select_model('locked')
    with pytest.raises(ProviderError):controller.select_model('invented')
    assert controller.selection['model']=='model-a'


def test_sdk_roundtrip_reuses_returned_result_without_resend(ctl,tmp_path):
    controller,counter=ctl
    first=controller.execute(req(),tmp_path/'t1',lambda:False)
    second=controller.execute(req(),tmp_path/'t2',lambda:False)
    assert first==second
    assert counter.read_text().splitlines()==['called']
    assert first['requested_model']=='model-a'
    assert first['total_credits'] is None
    assert any(e['phase']=='returned' for e in controller.snapshot()['events'])


def test_duplicate_identity_changed_prompt_is_blocked(ctl,tmp_path):
    controller,counter=ctl
    controller.execute(req(),tmp_path/'t1',lambda:False)
    with pytest.raises(ProviderError,match='身份'):
        controller.execute(req(prompt='{"ok":false}'),tmp_path/'t2',lambda:False)
    assert len(counter.read_text().splitlines())==1


def test_unresolved_admission_survives_restart_without_resend(ctl,tmp_path):
    controller,counter=ctl
    import hashlib
    run_key=hashlib.sha256(b'r1').hexdigest()[:16]
    key=hashlib.sha256(b'r1|rq1').hexdigest()[:24]
    directory=controller.root/'jobs'/run_key;directory.mkdir(parents=True)
    payload=req();digest=hashlib.sha256(json.dumps({'request':payload,'model':'model-a'},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    (directory/(key+'.json')).write_text(json.dumps({'digest':digest,'status':'admitted'}))
    with pytest.raises(ProviderError) as error:controller.execute(payload,tmp_path/'t',lambda:False)
    assert error.value.error_code=='RESPONSE_UNKNOWN'
    assert not counter.exists()


def test_budget_counts_admissions_not_answers_and_is_per_run(ctl,tmp_path,monkeypatch):
    controller,counter=ctl
    monkeypatch.setenv('MMF_QODER_ACCOUNT_MAX_JOBS','2')
    controller.execute(req(),tmp_path/'t1',lambda:False)
    controller.execute(req(task_id='t2',job_id='j2',request_id='rq2'),tmp_path/'t2',lambda:False)
    with pytest.raises(ProviderError) as error:
        controller.execute(req(task_id='t3',job_id='j3',request_id='rq3'),tmp_path/'t3',lambda:False)
    assert error.value.error_code=='QODER_BUDGET_EXHAUSTED'
    assert len(counter.read_text().splitlines())==2
    controller.execute(req(run_id='r2'),tmp_path/'another-run',lambda:False)


def test_stopped_or_expired_page_never_admitted(ctl,tmp_path):
    controller,counter=ctl
    from providers.openai_compatible import GenerationCancelled
    with pytest.raises(GenerationCancelled):controller.execute(req(),tmp_path/'t',lambda:True)
    with pytest.raises(ProviderError) as error:
        controller.execute(req(response_deadline_epoch=1),tmp_path/'t',lambda:False)
    assert error.value.error_code=='PPT_TIME_BUDGET'
    assert not counter.exists()


def test_recognition_budget_is_separate_and_legacy_jobs_still_count(ctl,tmp_path,monkeypatch):
    controller,counter=ctl
    monkeypatch.setenv('MMF_QODER_ACCOUNT_MAX_JOBS','1')
    monkeypatch.setenv('MMF_QODER_ACCOUNT_RECOGNITION_MAX_JOBS','2')
    controller.execute(req(task_id='run-tender-CHK-0001'),tmp_path/'legacy',lambda:False)
    controller.execute(req(task_id='new',request_id='new',generation_mode='tender_requirement_recognition'),tmp_path/'recognition',lambda:False)
    with pytest.raises(ProviderError) as error:
        controller.execute(req(task_id='third',request_id='third',generation_mode='tender_requirement_recognition'),tmp_path/'third',lambda:False)
    assert error.value.error_code=='QODER_BUDGET_EXHAUSTED'
    controller.execute(req(task_id='generation',request_id='generation'),tmp_path/'generation',lambda:False)
    with pytest.raises(ProviderError):controller.execute(req(task_id='gen2',request_id='gen2'),tmp_path/'gen2',lambda:False)
    assert len(counter.read_text().splitlines())==3


def test_recognition_limit_is_bounded_and_generation_default_stays_eight(ctl,tmp_path,monkeypatch):
    from qoder_account.connection import recognition_job_limit
    controller,counter=ctl
    monkeypatch.delenv('MMF_QODER_ACCOUNT_MAX_JOBS',raising=False)
    monkeypatch.delenv('MMF_QODER_ACCOUNT_RECOGNITION_MAX_JOBS',raising=False)
    assert recognition_job_limit()==24
    monkeypatch.setenv('MMF_QODER_ACCOUNT_RECOGNITION_MAX_JOBS','1000')
    assert recognition_job_limit()==24
    monkeypatch.setenv('MMF_QODER_ACCOUNT_RECOGNITION_MAX_JOBS','0')
    assert recognition_job_limit()==1
    for i in range(8):controller.execute(req(task_id=str(i),request_id=str(i)),tmp_path/str(i),lambda:False)
    with pytest.raises(ProviderError):controller.execute(req(request_id='ninth'),tmp_path/'ninth',lambda:False)
    assert len(counter.read_text().splitlines())==8


def test_provider_preserves_contract_and_unknown_cost(ctl,tmp_path):
    provider=QoderAccountProvider({'provider_name':'qoder_account','provider_type':'qoder_account'})
    result=provider.invoke_structured(req(),tmp_path/'task')
    assert result['ok'] is True
    metadata=result['provider_metadata']
    assert metadata['cost_availability']=='NOT_AVAILABLE'
    assert metadata['billable'] is None
    assert metadata['observed_model']=='NOT_AVAILABLE'
    assert metadata['transport']=='qoder_official_sdk'
    assert 'free quota' not in json.dumps(metadata)


def test_new_qoder_large_word_run_can_pass_eight_writing_calls(ctl, tmp_path, monkeypatch):
    from dataclasses import replace
    import paths
    from providers.base import write_json

    controller, counter = ctl
    monkeypatch.delenv('MMF_QODER_ACCOUNT_MAX_JOBS', raising=False)
    roots = replace(paths.current(), runs_dir=tmp_path / 'runs')
    monkeypatch.setattr(paths, 'current', lambda: roots)
    write_json(roots.runs_dir / 'large/brief.json', {
        'provider_name': 'qoder_account', 'medium': 'WORD', 'document_scale': 'large_technical_tender',
    })
    for i in range(9):
        controller.execute(req(run_id='large', request_id=f'word-{i}', generation_mode='longform_section'),
                           tmp_path / f'word-{i}', lambda: False)
    assert len(counter.read_text().splitlines()) == 9
    assert controller.snapshot()['word_call_caps']['large_technical_tender'] == 128


def test_ppt_identity_mismatch_keeps_raw_answer(ctl,tmp_path):
    provider=QoderAccountProvider({'provider_name':'qoder_account','provider_type':'qoder_account'})
    payload=req(generation_mode='ppt_slide',slide_id='S1',prompt='{"slide_id":"S2","body":[]}',required_keys=['slide_id','body'])
    with pytest.raises(ProviderError) as error:provider.invoke_structured(payload,tmp_path/'page')
    assert error.value.error_code=='PPT_SLIDE_ID_MISMATCH'
    assert (tmp_path/'page/qoder_raw_answer.json').is_file()


def test_connection_check_cannot_trigger_synthetic_ping(ctl,tmp_path):
    _,counter=ctl
    provider=QoderAccountProvider({'provider_name':'qoder_account','provider_type':'qoder_account'})
    with pytest.raises(ProviderError) as error:provider.invoke_structured(req(purpose='capability_probe'),tmp_path/'t')
    assert error.value.error_code=='QODER_EXPLICIT_GENERATION_REQUIRED'
    assert not counter.exists()


def test_worker_environment_never_inherits_other_api_keys(ctl,monkeypatch):
    controller,_=ctl
    monkeypatch.setenv('QODER_PERSONAL_ACCESS_TOKEN','not-a-real-token')
    monkeypatch.setenv('OPENAI_API_KEY','not-a-real-key')
    monkeypatch.setenv('QODER_BASE_URL','https://unwanted.example')
    env=controller.environment()
    assert not {'QODER_PERSONAL_ACCESS_TOKEN','OPENAI_API_KEY','QODER_BASE_URL'} & set(env)
    assert Path(env['MMF_QODER_ACCOUNT_WORKSPACE']).is_relative_to(controller.root)


def test_frontend_reconnect_reads_only_and_disconnect_is_product_local(ctl):
    controller,counter=ctl
    controller.event('admitted',run_id='r1',task_id='t1')
    before=controller.snapshot()
    assert controller.snapshot(before['event_cursor'])['events']==[]
    assert not counter.exists()
    controller.disconnect()
    assert not controller.health()['available']
    assert controller.snapshot()['status']=='not_connected'


def test_account_profile_is_not_mock_or_unverified_reasoning(ctl):
    from providers.capability import resolve_profile,recommended_parallelism
    profile=resolve_profile('qoder_account',{'model':'model-a'},speed_profile='deep')
    assert profile['effective_settings']['ui_reasoning_label']=='沿用Qoder模型设置'
    assert recommended_parallelism('qoder_account')==1


def test_model_and_progress_survive_old_stream_event_eviction(ctl,tmp_path):
    controller,_=ctl
    controller.execute(req(),tmp_path/'t1',lambda:False)
    controller.events=[]
    assert controller.snapshot()['run_progress']['r1']=={'admitted':1,'returned':1}
    controller.state['models'].append({'value':'model-b','isEnabled':True})
    controller.select_model('model-b')
    with pytest.raises(ProviderError) as error:
        controller.execute(req(task_id='t2',request_id='rq2'),tmp_path/'t2',lambda:False)
    assert error.value.error_code=='QODER_MODEL_LOCKED'


def test_inflight_cancel_keeps_admission_and_blocks_blind_resend(ctl,tmp_path,monkeypatch):
    controller,counter=ctl
    monkeypatch.setenv('FAKE_QODER_WAIT','1')
    from providers.openai_compatible import GenerationCancelled
    with pytest.raises(GenerationCancelled):
        controller.execute(req(),tmp_path/'cancelled',lambda:counter.exists())
    monkeypatch.delenv('FAKE_QODER_WAIT')
    with pytest.raises(ProviderError) as error:
        controller.execute(req(),tmp_path/'resume',lambda:False)
    assert error.value.error_code=='RESPONSE_UNKNOWN'
    assert len(counter.read_text().splitlines())==1


def test_sdk_connection_handshake_never_calls_query(ctl,monkeypatch,capsys):
    import asyncio
    import types
    from qoder_account import worker
    controller,_=ctl
    calls=[]
    class FakeClient:
        def __init__(self,options):
            assert options.tools==[] and options.strict_mcp_config is True
            assert options.setting_sources==[]
        async def __aenter__(self):calls.append('handshake');return self
        async def __aexit__(self,*args):pass
        async def get_available_models(self):calls.append('models');return [{'value':'model-a','displayName':'A','isEnabled':True}]
        async def get_usage_info(self):calls.append('usage');return None
        async def query(self,*args):raise AssertionError('Connection must never generate')
    fake=types.SimpleNamespace(QoderAgentOptions=lambda **args:types.SimpleNamespace(**args),QoderSDKClient=FakeClient,qodercli_auth=lambda:'local-login',PermissionResultDeny=object,AssistantMessage=object,ResultMessage=object,StreamEvent=object,TextBlock=object,SystemMessage=object)
    monkeypatch.setitem(sys.modules,'qoder_agent_sdk',fake)
    monkeypatch.setattr(worker,'runtime_info',lambda:{'cli_path':'synthetic','sdk_version':'synthetic'})
    monkeypatch.setattr(worker,'auth_status',lambda cli:{'logged_in':True,'cli_version':'synthetic'})
    monkeypatch.setenv('MMF_QODER_ACCOUNT_WORKSPACE',str(controller.root/'workspace'))
    monkeypatch.setenv('QODER_CONFIG_DIR',str(controller.root/'account'))
    asyncio.run(worker.run('connect',{}))
    row=json.loads(capsys.readouterr().out)
    assert row['status']=='ready' and row['model_requests']==0
    assert row['usage_availability']=='NOT_AVAILABLE'
    assert calls==['handshake','models','usage']


def test_launcher_records_actual_service_and_reuses_instance(tmp_path):
    import subprocess
    import urllib.request
    root=Path(__file__).resolve().parents[1]
    profile=tmp_path/'live'
    command=[sys.executable,'-B',str(root/'tools/launch_qoder_account.py'),'--profile',str(profile),'--no-browser']
    env=os.environ.copy()
    env['PYTHONUTF8']='1';env['PYTHONIOENCODING']='utf-8'
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    first=json.loads(subprocess.check_output(command,env=env,encoding='utf-8',timeout=30))
    try:
        health=json.load(opener.open(first['url']+'api/health',timeout=10))
        assert health['process_id']==first['pid']
        second=json.loads(subprocess.check_output(command,env=env,encoding='utf-8',timeout=15))
        assert second['reused_existing_instance'] is True
        assert (first['pid'],first['url'])==(second['pid'],second['url'])
        request=urllib.request.Request(first['url']+'api/qoder-account/connect',b'{}',headers={'Content-Type':'text/plain','Origin':'https://unrelated.invalid'})
        import urllib.error
        with pytest.raises(urllib.error.HTTPError) as error:opener.open(request,timeout=10)
        assert error.value.code==403
        status=json.load(opener.open(first['url']+'api/qoder-account/status',timeout=10))
        assert status['status']=='not_connected'
        assert status['connection_check_model_requests']==0
    finally:
        body=json.dumps({'process_id':first['pid'],'build_id':health['build_id'],'runs_root':health['runs_root']}).encode()
        request=urllib.request.Request(first['url']+'api/service/shutdown',body,headers={'Content-Type':'application/json'})
        opener.open(request,timeout=10).close()


def test_login_browser_fallback_accepts_only_official_pkce_url(ctl,monkeypatch):
    import io,time,subprocess
    controller,_=ctl
    legitimate='https://qoder.com/device/selectAccounts?challenge=synthetic&nonce=synthetic&machine_token=synthetic'
    opened=[]
    monkeypatch.setattr('webbrowser.open',lambda url:opened.append(url))
    class FakeLogin:
        stdout=io.StringIO('https://unrelated.invalid/device/selectAccounts?challenge=bad\n'+legitimate+'\n')
        def wait(self,timeout):
            for _ in range(100):
                if controller.state.get('official_login_url'):
                    assert controller.state['official_login_url']==legitimate
                    return 0
                time.sleep(.005)
            raise AssertionError('Login URL was not exposed')
    def popen(*args,**kwargs):
        assert kwargs['stderr']==subprocess.STDOUT
        assert kwargs['env']['CI']=='1'
        return FakeLogin()
    monkeypatch.setattr('qoder_account.connection.subprocess.Popen',popen)
    monkeypatch.setattr(controller,'_one',lambda mode,timeout=45:{'cli_path':'synthetic'} if mode=='inspect' else {'status':'authentication_required','models':[]})
    controller._connect(True)
    assert controller.state['status']=='authentication_required'
    assert 'official_login_url' not in controller.state
    assert opened==[legitimate]


@pytest.mark.parametrize('edition,expected_module,config_key,other_key',[
    ('global','qoder_agent_sdk','QODER_CONFIG_DIR','QODERCN_CONFIG_DIR'),
    ('cn','qodercn_agent_sdk','QODERCN_CONFIG_DIR','QODER_CONFIG_DIR')])
def test_editions_keep_authentication_and_runtime_separate(tmp_path,monkeypatch,edition,expected_module,config_key,other_key):
    monkeypatch.setenv('MMF_QODER_ACCOUNT_EDITION',edition)
    monkeypatch.setenv('QODER_CONFIG_DIR','do-not-reuse-global')
    monkeypatch.setenv('QODERCN_CONFIG_DIR','do-not-reuse-cn')
    controller=Connection(tmp_path/edition)
    env=controller.environment()
    assert controller.brand['module']==expected_module
    assert env['MMF_QODER_ACCOUNT_EDITION']==edition
    assert env[config_key]==str(controller.root/'account')
    assert other_key not in env


def test_wrong_edition_is_not_silently_redirected(monkeypatch):
    from qoder_account.edition import edition_settings
    monkeypatch.setenv('MMF_QODER_ACCOUNT_EDITION','unknown-region')
    with pytest.raises(ValueError):edition_settings()


def test_cn_sdk_handshake_uses_only_cn_public_api(tmp_path,monkeypatch,capsys):
    import asyncio,types
    from qoder_account import worker
    monkeypatch.setenv('MMF_QODER_ACCOUNT_EDITION','cn')
    monkeypatch.setenv('QODERCN_CONFIG_DIR',str(tmp_path/'cn-account'))
    monkeypatch.setenv('MMF_QODER_ACCOUNT_WORKSPACE',str(tmp_path))
    calls=[]
    class CNClient:
        def __init__(self,options):
            assert options.auth=='cn-local-login'
            assert options.env=={'QODERCN_CONFIG_DIR':str(tmp_path/'cn-account')}
        async def __aenter__(self):calls.append('cn-handshake');return self
        async def __aexit__(self,*args):pass
        async def get_available_models(self):calls.append('cn-models');return []
        async def get_usage_info(self):calls.append('cn-usage');return None
        async def query(self,*args):raise AssertionError('CN connection cannot generate')
    fake=types.SimpleNamespace(QoderAgentOptions=lambda **args:types.SimpleNamespace(**args),QoderSDKClient=CNClient,qodercli_auth=lambda:'cn-local-login',PermissionResultDeny=object,AssistantMessage=object,ResultMessage=object,StreamEvent=object,TextBlock=object,SystemMessage=object)
    monkeypatch.setitem(sys.modules,'qodercn_agent_sdk',fake)
    monkeypatch.setattr(worker,'runtime_info',lambda:{'cli_path':'cn-synthetic','sdk_version':'synthetic'})
    monkeypatch.setattr(worker,'auth_status',lambda cli:{'logged_in':True,'cli_version':'synthetic'})
    asyncio.run(worker.run('connect',{}))
    result=json.loads(capsys.readouterr().out)
    assert result['edition']=='cn' and result['model_requests']==0
    assert calls==['cn-handshake','cn-models','cn-usage']


def test_cn_login_rejects_global_domain_and_opens_cn_domain(tmp_path,monkeypatch):
    import io,time
    monkeypatch.setenv('MMF_QODER_ACCOUNT_EDITION','cn')
    controller=Connection(tmp_path/'cn')
    legitimate='https://qoder.cn/device/selectAccounts?challenge=synthetic&nonce=synthetic&machine_token=synthetic'
    opened=[]
    monkeypatch.setattr('webbrowser.open',lambda url:opened.append(url))
    class FakeLogin:
        stdout=io.StringIO('https://qoder.com/device/selectAccounts?challenge=wrong-edition\n'+legitimate+'\n')
        def wait(self,timeout):
            for _ in range(100):
                if controller.state.get('official_login_url'):
                    assert controller.state['official_login_url']==legitimate
                    return 0
                time.sleep(.005)
            raise AssertionError('CN login URL was not exposed')
    def popen(*args,**kwargs):
        assert 'QODERCN_CONFIG_DIR' in kwargs['env'] and 'QODER_CONFIG_DIR' not in kwargs['env']
        return FakeLogin()
    monkeypatch.setattr('qoder_account.connection.subprocess.Popen',popen)
    monkeypatch.setattr(controller,'_one',lambda mode,timeout=45:{'cli_path':'synthetic-cn','edition':'cn'} if mode=='inspect' else {'status':'authentication_required','models':[]})
    controller._connect(True)
    assert opened==[legitimate]
    assert controller.snapshot()['product_label']=='Qoder CN'


def test_recognition_grace_is_one_window_only_while_text_is_recent():
    from qoder_account.connection import recognition_grace_seconds
    assert recognition_grace_seconds(recognition=True, constrained=False, last_text=10, now=30) == 120
    assert recognition_grace_seconds(recognition=True, constrained=False, last_text=10, now=30.1) == 0
    assert recognition_grace_seconds(recognition=True, constrained=True, last_text=29, now=30) == 0
    assert recognition_grace_seconds(recognition=False, constrained=False, last_text=29, now=30) == 0
    assert recognition_grace_seconds(recognition=True, constrained=False, last_text=None, now=30) == 0


def _streaming_fake(path, text_chars, pause):
    path.write_text(
        "import json,os,sys,time\n"
        "json.load(sys.stdin)\n"
        f"print(json.dumps({{'event':'activity','phase':'generating','text_chars':{text_chars}}}),flush=True)\n"
        f"time.sleep({pause})\n"
        "print(json.dumps({'event':'result','body':'{}','is_error':False,'subtype':'success',"
        "'session_id':'synthetic-session','num_turns':1,'usage':None,'model_usage':None,"
        "'total_credits':None,'total_cost_usd':None}),flush=True)\n",
        encoding="utf-8")


def test_active_recognition_gets_one_finishing_window(ctl, tmp_path, monkeypatch):
    controller, _ = ctl
    fake = tmp_path / "stream.py"
    _streaming_fake(fake, 12, 1.5)
    monkeypatch.setattr(controller, "command", lambda mode: [sys.executable, "-B", str(fake)])
    terminal = controller.execute(req(
        generation_mode="tender_requirement_recognition", timeout_seconds=1,
        task_id="run-tender-CHK-0001", request_id="grace-1"), tmp_path / "task", lambda: False)
    assert terminal["subtype"] == "success"
    assert any(row.get("activity") == "recognition_finishing_window" for row in controller.events)


def test_idle_or_constrained_recognition_does_not_get_extra_time(ctl, tmp_path, monkeypatch):
    controller, _ = ctl
    idle = tmp_path / "idle.py"
    _streaming_fake(idle, 0, 1.5)
    monkeypatch.setattr(controller, "command", lambda mode: [sys.executable, "-B", str(idle)])
    with pytest.raises(ProviderError) as idle_error:
        controller.execute(req(
            generation_mode="tender_requirement_recognition", timeout_seconds=1,
            task_id="run-tender-CHK-0002", request_id="grace-idle"), tmp_path / "idle", lambda: False)
    assert idle_error.value.error_code == "RESPONSE_UNKNOWN"
    assert not any(row.get("activity") == "recognition_finishing_window" for row in controller.events)
    streaming = tmp_path / "held.py"
    _streaming_fake(streaming, 12, 1.5)
    monkeypatch.setattr(controller, "command", lambda mode: [sys.executable, "-B", str(streaming)])
    with pytest.raises(ProviderError) as held:
        controller.execute(req(
            generation_mode="tender_requirement_recognition", timeout_seconds=1,
            response_deadline_epoch=time.time() + 30,
            task_id="run-tender-CHK-0003", request_id="grace-held"), tmp_path / "held", lambda: False)
    assert held.value.error_code == "RESPONSE_UNKNOWN"
    assert not any(row.get("activity") == "recognition_finishing_window" for row in controller.events)


def test_launcher_does_not_reuse_wrong_edition_instance(tmp_path):
    import subprocess,urllib.request
    root=Path(__file__).resolve().parents[1];profile=tmp_path/'cn-live'
    base=[sys.executable,'-B',str(root/'tools/launch_qoder_account.py'),'--profile',str(profile),'--no-browser']
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    first=json.loads(subprocess.check_output([*base,'--edition','cn'],encoding='utf-8',timeout=30))
    health=json.load(opener.open(first['url']+'api/health',timeout=10))
    try:
        assert first['edition']=='cn'
        state=json.load(opener.open(first['url']+'api/qoder-account/status',timeout=10))
        assert state['edition']=='cn' and state['product_label']=='Qoder CN'
        mismatch=subprocess.run([*base,'--edition','global'],capture_output=True,encoding='utf-8',errors='replace',timeout=15)
        assert mismatch.returncode!=0
        assert json.loads((profile/'LAUNCH.json').read_text(encoding='utf-8'))['pid']==first['pid']
    finally:
        body=json.dumps({'process_id':first['pid'],'build_id':health['build_id'],'runs_root':health['runs_root']}).encode()
        opener.open(urllib.request.Request(first['url']+'api/service/shutdown',body,headers={'Content-Type':'application/json'}),timeout=10).close()
