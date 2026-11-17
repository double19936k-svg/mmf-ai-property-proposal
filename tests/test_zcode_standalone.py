"""Standalone-auth regression: a desktop app-server is not a CLI login checker."""
import io,json
import pytest
from zcode_account import standalone as s
from zcode_account.protocol import ProtocolError


@pytest.fixture
def profile(tmp_path,monkeypatch):
    account=tmp_path/'own';p=account/'data/.zcode/v2/provider_config.json';p.parent.mkdir(parents=True)
    ref={'providerId':s.DOMESTIC_PROVIDER,'modelId':'GLM-test'}
    p.write_text(json.dumps({'config':{'defaultModelSelection':ref}}))
    builtin=tmp_path/'builtin.json'
    builtin.write_text(json.dumps({'config':{'providerConfigRules':{'providerRules':[
        {'providerId':s.DOMESTIC_PROVIDER,'config':{'builtinModelIds':['GLM-test']}}]}}}))
    (account/'mmf_official_login_receipt.json').write_text(json.dumps({'provider':'bigmodel','completed_at':123,'cli_sha256':s.APPROVED_CLI_SHA}))
    monkeypatch.setattr(s,'identity',lambda:{'cli_sha256':s.APPROVED_CLI_SHA})
    monkeypatch.setattr(s,'installation',lambda:('node','cli',builtin))
    return account,ref,p


def test_real_domestic_login_can_be_reused_without_desktop_host_overlay(profile,monkeypatch):
    account,ref,_=profile;calls=[]
    class Process:
        returncode=0
        def communicate(self,timeout):return json.dumps({'sessionId':'fake','traceId':'fake','response':'No goal is set.'}),''
        def poll(self):return 0
    monkeypatch.setattr(s,'spawn',lambda account,args:(calls.append(args) or Process()))
    with s.Client(account) as client:row=client.inspect()
    assert row['models'][0]['ref']==ref and row['model_requests']==0
    assert calls==[['--prompt','/goal','--mode','plan','--json','--workflow-mode','disabled']]
    assert row['model_catalog_availability']=='NOT_AVAILABLE'
    assert row['official_login_completed_at']==123


def test_no_receipt_never_mistakes_public_configuration_for_login(profile,monkeypatch):
    account,ref,_=profile;(account/'mmf_official_login_receipt.json').unlink()
    monkeypatch.setattr(s,'spawn',lambda *a:pytest.fail('no receipt should not start CLI'))
    assert not s.Client(account).inspect()['models']


@pytest.mark.parametrize('provider',['account:zai-individual-coding-plan','bigmodel-api'])
def test_foreign_or_api_selection_not_reused(profile,monkeypatch,provider):
    account,ref,p=profile;p.write_text(json.dumps({'config':{'defaultModelSelection':{**ref,'providerId':provider}}}))
    monkeypatch.setattr(s,'spawn',lambda *a:pytest.fail('wrong route should not start CLI'))
    assert not s.Client(account).inspect()['models']


def test_vendor_upgrade_requires_control_branch_reverification(profile,monkeypatch):
    account,ref,_=profile;monkeypatch.setattr(s,'identity',lambda:{'cli_sha256':'changed'})
    with pytest.raises(ProtocolError,match='版本'):s.Client(account).inspect()


@pytest.mark.parametrize('current_sha',list(s.REVIEWED_CLI_VERSIONS))
def test_reviewed_upgrade_reuses_original_login_receipt_without_reauthorization(profile,monkeypatch,current_sha):
    account,ref,_=profile;calls=[]
    monkeypatch.setattr(s,'identity',lambda:{'cli_sha256':current_sha})
    class Process:
        returncode=0
        def communicate(self,timeout):return json.dumps({'response':'No goal is set.'}),''
        def poll(self):return 0
    monkeypatch.setattr(s,'spawn',lambda a,args:(calls.append(args) or Process()))
    row=s.Client(account).inspect()
    assert row['models'][0]['ref']==ref and row['official_login_completed_at']==123
    assert len(calls)==1 and calls[0][1]=='/goal' and 'login' not in calls[0]
    assert s.verified_installation()['cli_version']==s.REVIEWED_CLI_VERSIONS[current_sha]


def test_unknown_receipt_cannot_be_promoted_by_reviewed_installation(profile,monkeypatch):
    account,ref,_=profile
    (account/'mmf_official_login_receipt.json').write_text(json.dumps({'provider':'bigmodel','cli_sha256':'unknown'}))
    monkeypatch.setattr(s,'spawn',lambda *a:pytest.fail('unverified receipt must not be used'))
    assert s.Client(account).inspect()['models']==[]


def test_unexpected_generation_summary_not_accepted_as_connection_check(profile,monkeypatch):
    account,ref,_=profile
    class Process:
        returncode=0
        def communicate(self,timeout):return json.dumps({'response':'unexpected','usage':{'tokens':1}}),''
        def poll(self):return 0
    monkeypatch.setattr(s,'spawn',lambda *a:Process())
    with pytest.raises(ProtocolError,match='意外响应'):s.Client(account).inspect()


def test_executor_uses_official_standalone_and_never_invents_observed_model(profile,monkeypatch):
    account,ref,_=profile;commands=[]
    class Process:
        returncode=0;stdin=io.StringIO();stderr=io.StringIO('')
        stdout=io.StringIO(json.dumps({'type':'event','thinking':'not logged'})+'\n'+json.dumps({
            'type':'result','sessionId':'fake-session','response':'{"ok":true}','projection':{'status':'idle'}})+'\n')
        def poll(self):return 0
        def wait(self,timeout):return 0
    monkeypatch.setattr(s,'spawn_prompt',lambda account,args:(commands.append(args) or Process()))
    with s.Client(account) as client:row=client.call('workspace/generateText',{'selection':ref,'prompt':'synthetic'})
    assert row['text']=='{"ok":true}' and row['selection'] is None and row['usage'] is None
    assert row['observed_model_availability']=='NOT_AVAILABLE'
    assert commands[0][:2]==['--prompt','synthetic'] and '--mode' in commands[0] and 'plan' in commands[0]
    assert 'Bash' in commands[0] and '--workflow-mode' in commands[0]
    assert all(tool in commands[0] for tool in ('EnterPlanMode','ExitPlanMode','TaskOutput','TaskStop','ReadSessionContext'))


def test_unexpected_native_tool_is_counted_without_logging_input(profile,monkeypatch):
    account,ref,_=profile;updates=[]
    class Process:
        returncode=0;stderr=io.StringIO('')
        stdout=io.StringIO(json.dumps({'type':'model.streaming','payload':{'kind':'tool_call','toolName':'ExitPlanMode','input':{'plan':'PRIVATE_TOOL_INPUT'}}})+'\n'+json.dumps({'type':'result','response':'','projection':{'status':'idle'}})+'\n')
        def poll(self):return 0
        def wait(self,timeout):return 0
    monkeypatch.setattr(s,'spawn_prompt',lambda *a:Process())
    row=s.Client(account).call('workspace/generateText',{'selection':ref,'prompt':'synthetic'},progress=updates.append)
    assert row['tool_event_count']==1 and row['tool_names']==['ExitPlanMode']
    assert updates[-1]['tool_events']==1 and 'PRIVATE_TOOL_INPUT' not in json.dumps(updates)


def test_long_input_is_forwarded_whole_to_stdin_transport(profile,monkeypatch):
    account,ref,_=profile;seen=[]
    class Process:
        returncode=0;stderr=io.StringIO('')
        stdout=io.StringIO(json.dumps({'type':'result','response':'retained','projection':{'status':'idle'}})+'\n')
        def poll(self):return 0
        def wait(self,timeout):return 0
    monkeypatch.setattr(s,'spawn_prompt',lambda account,args:(seen.append(args) or Process()))
    prompt='字'*50000
    assert s.Client(account).call('workspace/generateText',{'selection':ref,'prompt':prompt})['text']=='retained'
    assert seen[0][1]==prompt


def test_changed_public_default_never_silently_switches_execution_model(profile,monkeypatch):
    account,ref,p=profile;p.write_text(json.dumps({'config':{'defaultModelSelection':{**ref,'modelId':'other'}}}))
    monkeypatch.setattr(s,'spawn',lambda *a:pytest.fail('changed config must not spawn'))
    with pytest.raises(ProtocolError,match='尚未发送') as error:s.Client(account).call('workspace/generateText',{'selection':ref,'prompt':'synthetic'})
    assert error.value.code=='ZCODE_NOT_DISPATCHED'


def test_public_model_switch_is_applied_and_other_settings_survive(profile):
    account,ref,p=profile
    b=s.installation()[2];data=json.loads(b.read_text());data['config']['providerConfigRules']['providerRules'][0]['config']['builtinModelIds'].append('GLM-5.3-Flash');b.write_text(json.dumps(data))
    config=json.loads(p.read_text());config['unrelated']={'keep':'intact'};p.write_text(json.dumps(config))
    target={'providerId':s.DOMESTIC_PROVIDER,'modelId':'GLM-5.3-Flash'}
    s.set_configured_selection(account,target)
    assert s.configured_selection(account)==target
    assert json.loads(p.read_text())['unrelated']=={'keep':'intact'}
    before=p.read_bytes()
    with pytest.raises(ProtocolError):s.set_configured_selection(account,{**target,'providerId':'foreign'})
    assert p.read_bytes()==before


def test_progress_contains_counts_without_thinking_or_tool_payload(profile,monkeypatch):
    account,ref,_=profile;updates=[]
    class Process:
        returncode=0;stderr=io.StringIO('')
        stdout=io.StringIO(json.dumps({'type':'event','thinking':'PRIVATE_THOUGHT'})+'\n'+json.dumps({'type':'result','response':'{"ok":true}','projection':{'status':'idle'}})+'\n')
        def poll(self):return 0
        def wait(self,timeout):return 0
    monkeypatch.setattr(s,'spawn_prompt',lambda *a:Process())
    s.Client(account).call('workspace/generateText',{'selection':ref,'prompt':'synthetic'},progress=updates.append)
    assert updates[-1]['terminal_received'] and updates[-1]['events_received']==2
    assert 'PRIVATE_THOUGHT' not in json.dumps(updates)


@pytest.mark.parametrize('fail',[False,True])
@pytest.mark.parametrize('effort',['low','high','max'])
def test_request_limits_reach_native_config_and_restore_after_vendor_reformat(profile,monkeypatch,fail,effort):
    account,ref,path=profile
    target={**ref,'modelId':'GLM-5.3-Flash'}
    data=json.loads(path.read_text());data['config']['defaultModelSelection']=target;data['keep']={'unchanged':True};path.write_text(json.dumps(data))
    builtin=s.installation()[2];b=json.loads(builtin.read_text());b['config']['providerConfigRules']['providerRules'][0]['config']['builtinModelIds'].append(target['modelId']);builtin.write_text(json.dumps(b))
    original=path.read_bytes();seen=[]
    def generate(self,params,*args):
        active=json.loads(path.read_text());seen.append(active)
        path.write_text(json.dumps(active,sort_keys=True,indent=2))
        if fail:raise ProtocolError('synthetic interrupted')
        return {'text':'{"ok":true}'}
    monkeypatch.setattr(s.Client,'_generate',generate)
    if fail:
        with pytest.raises(ProtocolError,match='interrupted'):s.Client(account).call('workspace/generateText',{'selection':target,'maxOutputTokens':8192,'reasoningEffort':effort})
    else:assert s.Client(account).call('workspace/generateText',{'selection':target,'maxOutputTokens':8192,'reasoningEffort':effort})['text']=='{"ok":true}'
    assert path.read_bytes()==original
    assert seen[0]['config']['defaultModelSelection']['options']=={'reasoningLevel':effort}
    rule=seen[0]['config']['modelConfigRules']['providerModelRules'][0]
    assert rule['providerId']==target['providerId'] and rule['config']['optionSpecs']['maxOutputTokens']['max']==8192


def test_real_external_config_edit_is_preserved_instead_of_overwritten(profile):
    account,ref,path=profile
    with pytest.raises(ProtocolError,match='外部修改'):
        with s.request_options(account,{'selection':ref,'maxOutputTokens':8192}):
            data=json.loads(path.read_text());data['external']={'must_keep':True};path.write_text(json.dumps(data))
    assert json.loads(path.read_text())['external']=={'must_keep':True}

def test_resume_uses_explicit_session_id_and_never_latest(profile,monkeypatch):
    account,ref,_=profile;seen=[]
    class Process:
        returncode=0;stderr=io.StringIO('')
        stdout=io.StringIO(json.dumps({'type':'result','sessionId':'sess_test','response':'{"ok":true}','projection':{'status':'idle'}})+'\n')
        def poll(self):return 0
        def wait(self,timeout):return 0
    monkeypatch.setattr(s,'spawn_prompt',lambda account,args:(seen.append(args) or Process()))
    s.Client(account).call('workspace/generateText',{'selection':ref,'prompt':'synthetic','nativeSessionId':'sess_test'})
    assert seen[0][-2:]==['--resume','sess_test'] and '--continue' not in seen[0]


def test_cli_without_a_selected_model_is_login_required_and_unsent(profile,monkeypatch):
    account,ref,_=profile
    class Process:
        returncode=1
        stderr=io.StringIO('Error: Select a model before sending a message\n')
        stdout=io.StringIO('')
        def poll(self):return 1
        def wait(self,timeout):return 1
    monkeypatch.setattr(s,'spawn_prompt',lambda *a:Process())
    with pytest.raises(ProtocolError,match='重新授权') as error:
        s.Client(account).call('workspace/generateText',{'selection':ref,'prompt':'synthetic'})
    assert error.value.code=='ZCODE_LOGIN_REQUIRED'
