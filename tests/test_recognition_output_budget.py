import hashlib,json,sys
from pathlib import Path
import pytest
from providers.base import ProviderError
from qoder_account.connection import Connection,terminal_failure
from tender_intake import understanding as u


def req(**kw):
    return {'task_id':'run-tender-CHK-0005','chunk_id':'CHK-0005','prompt':'unchanged source text',
        'system_prompt':'unchanged instruction','json_schema':{'type':'object'},
        'generation_mode':'tender_requirement_recognition','timeout_seconds':600,**kw}


def test_cached_token_limit_result_classified_without_resend(tmp_path,monkeypatch):
    monkeypatch.setenv('MMF_QODER_ACCOUNT_ENABLED','1')
    c=Connection(tmp_path/'ctl');c.environment();c.state.update(status='ready',models=[{'value':'test','isEnabled':True}]);c.select_model('test')
    worker=tmp_path/'worker.py';counter=tmp_path/'counter.txt'
    worker.write_text('import sys,json\nfrom pathlib import Path\njson.load(sys.stdin)\np=Path('+repr(str(counter))+')\np.write_text(p.read_text()+"call\\n" if p.exists() else "call\\n")\nprint(json.dumps({"event":"result","body":"{\\\"partial\\\":","is_error":True,"subtype":"error_max_turns","stop_reason":"max_tokens","max_output_tokens":8000}))\n',encoding='utf-8')
    monkeypatch.setattr(c,'command',lambda mode:[sys.executable,'-B',str(worker)])
    for path in ('original','cached'):
        with pytest.raises(ProviderError) as error:c.execute({**req(),'run_id':'run'},tmp_path/path,lambda:False)
        assert error.value.error_code=='QODER_OUTPUT_TRUNCATED'
    assert counter.read_text().splitlines()==['call']
    assert (tmp_path/'original/qoder_raw_answer.json').exists()


def test_proven_truncation_gets_one_larger_budget_with_new_identity(tmp_path):
    calls=[]
    class Provider:
        def invoke_structured(self,r,d):
            calls.append((r,d))
            if len(calls)==1:raise terminal_failure({'is_error':True,'stop_reason':'max_tokens'})
            return {'chunk_id':'CHK-0005','provider_metadata':{'request_digest':'new'}}
    original=req();row=u._invoke_recognition(Provider(),original,tmp_path/'call')
    assert len(calls)==2 and calls[1][0]['max_tokens']==32000
    assert calls[1][0]['task_id']!=original['task_id']
    for key in ('prompt','system_prompt','chunk_id','json_schema'):assert calls[1][0][key]==original[key]
    assert 'max_tokens' not in original
    assert row['provider_metadata']['output_budget_recovery']['status']=='returned'
    assert json.loads((tmp_path/'call/output_budget_recovery.json').read_text())['status']=='returned'


@pytest.mark.parametrize('code',['RESPONSE_UNKNOWN','QODER_REQUEST_ERROR','REQUEST_IDENTITY_MISMATCH','QODER_BUDGET_EXHAUSTED'])
def test_unknown_auth_and_budget_failures_never_trigger_output_retry(tmp_path,code):
    calls=[]
    class Provider:
        def invoke_structured(self,r,d):calls.append(r);raise ProviderError('failed',error_code=code)
    with pytest.raises(ProviderError):u._invoke_recognition(Provider(),req(),tmp_path/'call')
    assert len(calls)==1


def test_already_large_output_limit_never_loops(tmp_path):
    calls=[]
    class Provider:
        def invoke_structured(self,r,d):calls.append(r);raise terminal_failure({'is_error':True,'stop_reason':'max_tokens','max_output_tokens':32000})
    with pytest.raises(ProviderError):u._invoke_recognition(Provider(),req(),tmp_path/'call')
    assert len(calls)==1


def test_recovery_itself_fails_once_and_preserves_audit(tmp_path):
    calls=[]
    class Provider:
        def invoke_structured(self,r,d):calls.append(r);raise terminal_failure({'is_error':True,'stop_reason':'max_tokens'})
    with pytest.raises(ProviderError):u._invoke_recognition(Provider(),req(),tmp_path/'call')
    assert len(calls)==2
    assert json.loads((tmp_path/'call/output_budget_recovery.json').read_text())['status']=='failed'


def test_four_completed_chunks_reused_and_resume_only_failed_chunk(tmp_path,monkeypatch):
    extraction={'run_id':'run','files':[{'file_id':'F1','original_filename':'synthetic.txt'}],
        'pages':[],'paragraphs':[{'file_id':'F1','para_id':f'P{i}','paragraph_index':i,
        'text':f'本项目必须完成第{i}项巡查服务。','heading_path':[]} for i in range(1,7)],'tables':[],'warnings':[]}
    blocks=u._blocks(extraction)
    chunks=[{'chunk_id':f'CHK-{i:04d}','blocks':[b]} for i,b in enumerate(blocks,1)]
    monkeypatch.setattr(u,'build_chunks',lambda ex:chunks)
    class Provider:
        config={'provider_name':'qoder_account','display_name':'Qoder CN账户'}
        phase='old';calls=[]
        def invoke_structured(self,r,d):
            self.calls.append((r['chunk_id'],r.get('max_tokens')))
            if r['chunk_id']=='CHK-0005' and not r.get('max_tokens'):
                if self.phase=='old':raise ProviderError('old terminal',error_code='QODER_REQUEST_ERROR')
                raise terminal_failure({'is_error':True,'stop_reason':'max_tokens'})
            return {**r['mock_response'],'provider_metadata':{'synthetic':True}}
    provider=Provider();run=tmp_path/'run'
    first=u.understand_run(run,extraction,provider)
    assert first['status']['chunks_completed']==4
    paths=[run/f'tender/chunks/CHK-{i:04d}/normalized.json' for i in range(1,5)]
    before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    provider.phase='fixed';provider.calls=[]
    result=u.understand_run(run,extraction,provider)
    assert provider.calls==[('CHK-0005',None),('CHK-0005',32000),('CHK-0006',None)]
    assert result['status']['chunks_completed']==6 and result['status']['recognition_outcome']=='AI_RECOGNITION_SUCCESS'
    assert all(hashlib.sha256(p.read_bytes()).hexdigest()==digest for p,digest in before.items())


@pytest.mark.parametrize('name',['zcode_cn','mock'])
def test_recognition_retains_actual_engine_for_history_resume(tmp_path,name):
    ex={'run_id':'run','files':[{'file_id':'F1','original_filename':'synthetic.txt'}], 'pages':[],
        'paragraphs':[{'file_id':'F1','para_id':'P1','paragraph_index':1,'text':'本项目必须每日完成巡查服务。','heading_path':[]}],
        'tables':[],'warnings':[]}
    class Provider:
        config={'provider_name':name}
        def invoke_structured(self,r,d):return r['mock_response']
    result=u.understand_run(tmp_path/'run',ex,Provider())
    assert result['status']['provider_name']==name
    assert json.loads((tmp_path/'run/tender/status.json').read_text())['provider_name']==name
