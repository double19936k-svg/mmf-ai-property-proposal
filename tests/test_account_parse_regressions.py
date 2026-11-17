"""Regression cases from the two actual failed tender-recognition routes."""
import asyncio,hashlib,importlib.util,json,os,shutil,sys
from pathlib import Path
from types import SimpleNamespace
import pytest


@pytest.mark.parametrize('mode,limit',[('generic',8000),('tender_requirement_recognition',32000)])
def test_system_init_activity_does_not_abort_qoder_result_stream(monkeypatch,capsys,mode,limit):
    from qoder_account import worker as w
    class SystemMessage:
        subtype='init'
    class TextBlock:
        text='{"ok":true}'
    class AssistantMessage:
        content=[TextBlock()]
    class ResultMessage:
        result='{"ok":true}';is_error=False;subtype='success';session_id='synthetic';num_turns=1
        stop_reason='end_turn';usage=None;model_usage=None;total_credits=None;total_cost_usd=None
    class StreamEvent:pass
    class Options(SimpleNamespace):pass
    queried=[]
    class Client:
        session_id='synthetic'
        def __init__(self,options):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def query(self,prompt):queried.append(prompt)
        async def receive_response(self):
            for row in (SystemMessage(),AssistantMessage(),ResultMessage()):yield row
    sdk=SimpleNamespace(QoderAgentOptions=Options,QoderSDKClient=Client,qodercli_auth=lambda:None,
        PermissionResultDeny=lambda **kw:kw,AssistantMessage=AssistantMessage,ResultMessage=ResultMessage,
        StreamEvent=StreamEvent,TextBlock=TextBlock,SystemMessage=SystemMessage)
    monkeypatch.setattr(w.importlib,'import_module',lambda name:sdk)
    monkeypatch.setattr(w,'runtime_info',lambda:{'cli_path':'synthetic','sdk_version':'fake'})
    monkeypatch.setattr(w,'auth_status',lambda cli:{'logged_in':True})
    monkeypatch.setenv('MMF_QODER_ACCOUNT_WORKSPACE','synthetic')
    monkeypatch.setenv('MMF_QODER_ACCOUNT_EDITION','cn');monkeypatch.setenv('QODERCN_CONFIG_DIR','synthetic')
    asyncio.run(w.run('execute',{'model':'test','prompt':'synthetic','generation_mode':mode}))
    rows=[json.loads(s) for s in capsys.readouterr().out.splitlines()]
    assert any(r.get('kind')=='init' and r['event']=='activity' for r in rows)
    assert rows[-1]['event']=='result' and rows[-1]['body']=='{"ok":true}' and queried==['synthetic']
    assert rows[-1]['max_output_tokens']==limit


def test_windows_long_prompt_roundtrip_via_stdin_without_truncation(tmp_path,monkeypatch):
    from zcode_account import protocol as p
    node=os.environ.get('RUN_J_NODE') or shutil.which('node')
    assert node,'Node runtime is required by the existing product build'
    cli=tmp_path/'official-cli.cjs'
    cli.write_text("const crypto=require('node:crypto'); const p=process.argv[process.argv.indexOf('--prompt')+1]; console.log(JSON.stringify({length:p.length,sha256:crypto.createHash('sha256').update(p).digest('hex'),entry:process.argv[1]}));",encoding='utf-8')
    monkeypatch.setattr(p,'installation',lambda:(node,cli,tmp_path/'unused'))
    monkeypatch.setattr(p,'environment',lambda account:os.environ.copy())
    account=tmp_path/'own';(account/'workspace').mkdir(parents=True)
    prompt=('长请求含引号"、换行\n和路径 C:\\tmp\\file。'*5000)+'END'
    process=p.spawn_prompt(account,['--prompt',prompt])
    output=process.stdout.read();assert process.wait(timeout=10)==0
    result=json.loads(output)
    assert result['length']==len(prompt) and result['sha256']==hashlib.sha256(prompt.encode()).hexdigest()
    assert result['entry']==str(cli)


def test_transport_limit_rejects_before_admission_to_process(tmp_path,monkeypatch):
    from zcode_account import protocol as p
    monkeypatch.setattr(p,'installation',lambda:('node','cli','builtin'))
    monkeypatch.setattr(p,'environment',lambda account:{})
    monkeypatch.setattr(p.subprocess,'Popen',lambda *a,**kw:pytest.fail('must not start a process'))
    with pytest.raises(p.ProtocolError,match='传输上限'):p.spawn_prompt(tmp_path,['--prompt','字'*3000000])
