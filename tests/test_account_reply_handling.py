import hashlib,json,time
from pathlib import Path
import pytest
import account_tasks as a
from account_issue_policy import classify
from providers.account_recognition import parse_recognition
from providers.base import ProviderError,write_json

REQUEST={'run_id':'synthetic','task_id':'synthetic-tender-CHK-1','chunk_id':'CHK-1',
         'generation_mode':'tender_requirement_recognition','required_keys':['chunk_id','proposed_items'],'prompt':'synthetic'}

def payload():
    return {'chunk_id':'CHK-1','proposed_items':[{'source_id':'PAR-1','source_excerpt':'保持区域"五无五净"（"五无"：无垃圾）。','normalized_requirement':'保洁要求'}],
            'proposed_boilerplate':[{'reason':'待核实'}],'proposed_conflicts':[],
            'proposed_scoring_items':[{'description':'测试评分'}],'chunk_missing_facts':['未提供面积']}

def malformed():
    return json.dumps(payload(),ensure_ascii=False).replace('\\"','"')

@pytest.fixture
def ctl(tmp_path,monkeypatch):
    monkeypatch.setattr(a,'root',lambda:tmp_path/'agents')
    monkeypatch.setattr(a,'run_visible',lambda run_id:True)
    class C:
        root=tmp_path/'ctl';selection={'model':'test-flash'};calls=[]
        brand={'id':'cn','credential_source':'synthetic','transport':'synthetic'}
        responses=[malformed()]
        def execute(self,request,d,cancelled):
            self.calls.append(request)
            value=self.responses[min(len(self.calls)-1,len(self.responses)-1)]
            if isinstance(value,Exception):raise value
            return {'body':value,'session_id':'session-synthetic','digest':'synthetic','requested_model':'test-flash'}
        def event(self,*a,**kw):pass
    return C()

def test_qoder_quote_repair_preserves_whole_reply_and_every_channel():
    result,audit=parse_recognition('qoder_account',REQUEST,malformed())
    assert result==payload() and len(audit['syntax_edits'])==4
    assert audit['type']=='qoder_recognition_serialization'
    assert not audit['business_text_changed'] and not audit['partial_items_salvaged']

@pytest.mark.parametrize('provider',['qoder_account','zcode_cn'])
def test_both_account_coordinators_accept_complete_quote_repair_without_more_calls(ctl,tmp_path,provider):
    result=a.execute(provider,REQUEST,tmp_path/'task',ctl)
    assert result['body']==malformed() and len(ctl.calls)==1
    assert a.list_tasks()[0]['state']=='content_ready'

def test_qoder_provider_uses_same_parser_as_coordinator(ctl,tmp_path,monkeypatch):
    from qoder_account import connection as mod
    from providers.qoder_account import QoderAccountProvider
    monkeypatch.setattr(mod,'connection',lambda:ctl)
    p=QoderAccountProvider({'provider_name':'qoder_account','provider_type':'qoder_account'})
    result=p.invoke_structured(REQUEST,tmp_path/'task')
    assert result['proposed_items']==payload()['proposed_items']
    assert result['provider_metadata']['serialization_repair']['applied']
    assert len(ctl.calls)==1

def test_old_failed_receipt_is_repaired_locally_once_with_no_dispatch(ctl,tmp_path):
    key=a.task_key('qoder_account',REQUEST,ctl.root);p=a.folder(key)
    row={'key':key,'provider':'qoder_account','run_id':'synthetic','task_id':REQUEST['task_id'],
         'state':'failed','turn':1,'session_id':'session-synthetic','error_code':'OUTPUT_CONTRACT_ERROR'}
    write_json(p/'request.json',REQUEST);write_json(p/'receipt-0.json',{'body':malformed()});a.put(key,row)
    assert a.list_tasks()[0]['state']=='content_ready'
    assert a.list_tasks()[0]['error_code'] is None and not ctl.calls
    assert (p/'final.json').exists() and read(p/'receipt-0.json')['body']==malformed()

def read(p):return json.loads(p.read_text(encoding='utf-8'))

def test_unusable_returned_reply_waits_for_explicit_format_choice(ctl,tmp_path,monkeypatch):
    ctl.responses=['{"unrecognized":true}',json.dumps(payload(),ensure_ascii=False)]
    with pytest.raises(ProviderError):a.execute('qoder_account',REQUEST,tmp_path/'task',ctl)
    row=a.list_tasks()[0]
    assert row['state']=='needs_attention' and row['question']['kind']=='response_format' and len(ctl.calls)==1
    from qoder_account import connection as mod
    monkeypatch.setattr(mod,'connection',lambda:ctl)
    callbacks=[]
    a.answer(row['key'],row['nonce'],'继续整理已返回内容（1次请求）',lambda *args:callbacks.append(args))
    end=time.monotonic()+3
    while time.monotonic()<end and a.state(row['key'])['state']!='content_ready':time.sleep(.02)
    assert a.state(row['key'])['state']=='content_ready' and len(ctl.calls)==2
    assert ctl.calls[-1]['native_session_id']=='session-synthetic'
    assert '只整理上一轮完整原答' in ctl.calls[-1]['prompt']
    assert a.state(row['key'])['format_retries']==1
    assert a.state(row['key'])['error_code'] is None and a.state(row['key'])['handling_detail'] is None
    assert not callbacks  # One repair approval must not dispatch remaining customer chunks.
    with pytest.raises(ProviderError):a.answer(row['key'],row['nonce'],'继续整理已返回内容（1次请求）')

def test_user_can_retain_unusable_reply_without_any_new_request(ctl,tmp_path):
    ctl.responses=['{"unrecognized":true}']
    with pytest.raises(ProviderError):a.execute('qoder_account',REQUEST,tmp_path/'task',ctl)
    row=a.list_tasks()[0];result=a.answer(row['key'],row['nonce'],'保留内容，暂不继续')
    assert result['state']=='needs_attention' and result['nonce'] is None and len(ctl.calls)==1


@pytest.mark.parametrize(('provider','mode','protocol'),[
    ('qoder_account','longform_section',None),
    ('zcode_cn','longform_batch','cu_envelope'),
    ('qoder_account','ppt_slide',None)])
def test_file_generation_automatically_organizes_the_original_session_once(ctl,tmp_path,provider,mode,protocol):
    request={'run_id':'synthetic','task_id':'synthetic-file','generation_mode':mode,
             'required_keys':['slide_id','content'],'slide_id':'SL-1','prompt':'synthetic'}
    if protocol:
        request['response_protocol']=protocol
    malformed='{"content":'
    valid='<<<CU_START id="CU-1">>>\n原文\n<<<CU_END>>>' if protocol else '{"slide_id":"SL-1","content":"原文"}'
    ctl.responses=[malformed,valid]
    result=a.execute(provider,request,tmp_path/'task',ctl)
    key=a.task_key(provider,request,ctl.root)
    row=a.state(key)
    assert result['body']==valid
    assert row['state']=='content_ready' and row['format_retries']==1 and not row.get('nonce')
    assert row['answers'][-1]['source']=='automatic_generation_policy'
    assert len(ctl.calls)==2 and ctl.calls[-1]['native_session_id']=='session-synthetic'
    assert ctl.calls[-1]['task_id']=='synthetic-file-decision-1'
    assert ('CU封装标记' if protocol else 'JSON序列化') in ctl.calls[-1]['prompt']
    assert read(a.folder(key)/'receipt-0.json')['body']==malformed
    assert read(a.folder(key)/'receipt-1.json')['body']==valid
    assert a.execute(provider,request,tmp_path/'task',ctl)==result and len(ctl.calls)==2


@pytest.mark.parametrize(('replies','count','code'),[
    (['{"content":','{"content":'],2,'OUTPUT_CONTRACT_ERROR'),
    (['{"slide_id":"OTHER","content":"原文"}'],1,'PPT_SLIDE_ID_MISMATCH'),
    (['{"content":',ProviderError('budget',error_code='QODER_BUDGET_EXHAUSTED')],2,'QODER_BUDGET_EXHAUSTED'),
])
def test_automatic_format_repair_never_loops_or_accepts_identity_mismatch(ctl,tmp_path,replies,count,code):
    request={'run_id':'synthetic','task_id':'synthetic-ppt-slide','generation_mode':'ppt_slide',
             'required_keys':['slide_id','content'],'slide_id':'SL-1','prompt':'synthetic'}
    ctl.responses=replies
    with pytest.raises(ProviderError) as exc:
        a.execute('qoder_account',request,tmp_path/'task',ctl)
    row=a.list_tasks()[0]
    assert exc.value.error_code==code and row['state']=='needs_attention' and row['nonce'] is None
    assert len(ctl.calls)==count and not (a.folder(row['key'])/'final.json').exists()

@pytest.mark.parametrize('code,state',[
    ('OUTPUT_CONTRACT_ERROR','needs_attention'),('ZCODE_RECOGNITION_SCHEMA','needs_attention'),
    ('RESPONSE_UNKNOWN','interrupted'),('ZCODE_REQUEST_INCOMPLETE','interrupted'),
    ('PPT_SLIDE_ID_MISMATCH','needs_attention'),('ACCOUNT_PERMISSION_UNSUPPORTED','needs_attention'),
    ('ZCODE_NOT_READY','needs_attention'),('QODER_BUDGET_EXHAUSTED','needs_attention'),
    ('QODER_REQUEST_ERROR','needs_attention'),('QODER_OUTPUT_TRUNCATED','needs_attention'),
    ('ZCODE_MODEL_UNAVAILABLE','needs_attention'),
    ('ACCOUNT_TASK_FAILED','failed')])
def test_execution_issues_are_not_all_fatal_and_are_not_all_user_business_decisions(code,state):
    assert classify(code)[0]==state

def test_wrong_chunk_is_isolated_and_never_offered_as_business_approval(ctl,tmp_path):
    ctl.responses=[json.dumps({**payload(),'chunk_id':'OTHER'})]
    with pytest.raises(ProviderError):a.execute('qoder_account',REQUEST,tmp_path/'task',ctl)
    row=a.list_tasks()[0]
    assert row['state']=='needs_attention' and not row.get('question')
    assert not (a.folder(row['key'])/'final.json').exists()

def test_partial_recognition_projection_preserves_canonical_guard(ctl,tmp_path):
    a.execute('qoder_account',REQUEST,tmp_path/'task',ctl)
    status={'provider_name':'qoder_account','recognition_outcome':'AI_RECOGNITION_FAILED_WITH_LOCAL_FALLBACK','chunks_completed':1,'chunks_total':6}
    original=dict(status);view=a.recognition_handling('synthetic',status)
    assert view['state']=='pending_resume' and view['can_continue'] and status==original
    assert a.recognition_handling('synthetic',{**status,'provider_name':'glm_zhipu'}) is None
