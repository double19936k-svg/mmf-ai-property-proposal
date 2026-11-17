import json
import pytest
from providers.base import ProviderError, parse_provider_payload
from providers.zcode_recognition import parse_recognition, ARRAY_KEYS

REQUEST = {'chunk_id': 'CHK-0001-Z004', 'generation_mode': 'tender_requirement_recognition'}

def payload():
    return {'chunk_id': REQUEST['chunk_id'], **{key: [] for key in ARRAY_KEYS},
            'proposed_items': [{'source_id': 'PAR-1', 'source_excerpt': '每月复核水、电、气费用。',
                                'normalized_requirement': '每月复核水、电、气费用。'}]}

def test_interior_quote_in_excerpt_keeps_complete_reply_and_all_candidate_channels():
    row=payload();row['proposed_conflicts']=[{'description':'测试冲突','candidates':[]}]
    row['proposed_boilerplate']=[{'reason':'测试模板'}]
    row['proposed_scoring_items']=[{'description':'测试评分'}];row['chunk_missing_facts']=['待核实']
    original='说明" 的证据不在本条；本条原文为：每月复核水、电、气费用。'
    row['proposed_items'][0]['source_excerpt']=original
    text=json.dumps(row,ensure_ascii=False,indent=2).replace('说明\\" 的','说明" 的')
    result,audit=parse_recognition(REQUEST,'```json\n'+text+'\n```')
    assert result==row and result['proposed_items'][0]['source_excerpt']==original
    assert audit['syntax_edits']==[{'type':'escape_quote','offset':text.index('" 的')}]
    assert not audit['business_text_changed'] and not audit['partial_items_salvaged']
    assert all(audit['channel_counts'][k]==1 for k in ARRAY_KEYS)

@pytest.mark.parametrize('kind',['trailing_comma','newline','tab'])
def test_small_serialization_errors_preserve_business_values(kind):
    row=payload()
    if kind=='trailing_comma':text=json.dumps(row,ensure_ascii=False)[:-1]+',}'
    else:
        ch='\n' if kind=='newline' else '\t';row['proposed_items'][0]['source_excerpt']+='甲'+ch+'乙'
        text=json.dumps(row,ensure_ascii=False).replace('\\n','\n').replace('\\t','\t')
    result,audit=parse_recognition(REQUEST,text)
    assert result==row and audit['applied']

def test_wrapped_and_aliased_reply_preserves_all_channels():
    row=payload();row['chunkId']=row.pop('chunk_id');row['items']=row.pop('proposed_items')
    result,audit=parse_recognition(REQUEST,{'data':row})
    assert result['chunk_id']==REQUEST['chunk_id'] and result['proposed_items']==row['items']
    assert audit['wrapper']=='/data' and audit['aliases']['chunk_id']=='chunkId'

def test_only_missing_request_identity_is_filled_without_changing_business_data():
    row=payload();del row['chunk_id'];result,audit=parse_recognition(REQUEST,row)
    assert result==payload() and audit['filled_keys']==['chunk_id']

@pytest.mark.parametrize('value',[
    '{"chunk_id":"CHK-0001-Z004","proposed_items":[{"source_id":"PAR-1"}',
    '{"chunk_id":"CHK-0001-Z004","proposed_items":[{"source_id":"PAR-1"}],',
    '{"chunk_id":"CHK-0001-Z004","proposed_items":[{"source_id":"PAR-1"}]',
    '{"chunk_id":"CHK-0001-Z004","proposed_items":[],"proposed_items":[{}]}',
    '{"chunk_id":"CHK-0001-Z004","proposed_items":[{"source_id":"PAR-1","text":"unfinished}',
])
def test_truncated_or_ambiguous_reply_never_promotes_inner_objects(value):
    with pytest.raises(ProviderError) as err:parse_recognition(REQUEST,value)
    assert err.value.error_code=='ZCODE_RECOGNITION_FORMAT'
    assert 'chunk_id' not in str(err.value)

@pytest.mark.parametrize('row',[
    {'source_id':'PAR-1','normalized_requirement':'这是内部条目'},
    {'chunk_id':'CHK-0001-Z004'},
    {'proposed_items':None},
    {'proposed_items':[], 'proposed_conflicts':'lost channel'},
    {'proposed_items':[], 'items':[{'source_id':'different'}]},
    {'data':{'proposed_items':[]},'result':{'proposed_items':[]}},
])
def test_unusable_business_shape_is_not_repaired_to_empty_success(row):
    with pytest.raises(ProviderError) as err:parse_recognition(REQUEST,row)
    assert err.value.error_code=='ZCODE_RECOGNITION_SCHEMA'

def test_wrong_chunk_is_not_relabelled_as_current():
    row=payload();row['chunk_id']='CHK-OTHER'
    with pytest.raises(ProviderError) as err:parse_recognition(REQUEST,row)
    assert err.value.error_code=='ZCODE_RECOGNITION_ID_MISMATCH'

def test_valid_reply_unchanged_and_generic_parser_behavior_not_modified():
    row=payload();result,audit=parse_recognition(REQUEST,json.dumps(row))
    assert result==row and not audit['applied']
    assert parse_provider_payload({'generation_mode':'other'}, {'ok':True}, {'ok'})==( {'ok':True},None)

def test_provider_reparses_saved_terminal_without_new_dispatch(tmp_path,monkeypatch):
    from zcode_account import connection as mod
    from providers.zcode_account import ZCodeAccountProvider
    ref={'providerId':'account:bigmodel-individual-coding-plan','modelId':'GLM-test'}
    monkeypatch.setenv('MMF_ZCODE_ACCOUNT_ENABLED','1')
    monkeypatch.setattr(mod,'verified_installation',lambda:{'cli_version':'synthetic'})
    monkeypatch.setattr(mod,'set_configured_selection',lambda *a:None)
    c=mod.Connection(tmp_path/'connection');model=ref['providerId']+'/'+ref['modelId']
    c.state.update(status='ready',models=[{'id':model,'ref':ref}]);c.select_model(model)
    monkeypatch.setattr(mod,'connection',lambda:c)
    calls=[];row=payload();row['proposed_items'][0]['source_excerpt']='引文" 说明'
    bad=json.dumps(row,ensure_ascii=False).replace('引文\\"','引文"')
    class Client:
        workspace={}
        def __init__(self,*a):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def call(self,*a,**kw):calls.append(kw);return {'text':bad,'selection':ref,'finishReason':'stop'}
    monkeypatch.setattr(mod,'Client',Client)
    request={**REQUEST,'task_id':'synthetic-tender-child','run_id':'synthetic','prompt':'synthetic'}
    original=c.execute(request,tmp_path/'original');assert len(calls)==1
    monkeypatch.setattr(mod,'Client',lambda *a:pytest.fail('must use returned ledger response'))
    provider=ZCodeAccountProvider({'provider_name':'zcode_cn','provider_type':'zcode_account'})
    result=provider.invoke_structured(request,tmp_path/'recovered')
    assert result['proposed_items']==row['proposed_items'] and len(calls)==1
    assert result['provider_metadata']['serialization_repair']['applied']
    assert json.loads((tmp_path/'recovered/provider_raw_envelope.json').read_text(encoding='utf-8'))==original

def test_bad_cached_response_reports_parse_failure_without_masking_connection(tmp_path,monkeypatch):
    from zcode_account import connection as mod
    from providers.zcode_account import ZCodeAccountProvider
    events=[]
    row={'body':'{"chunk_id":"CHK-0001-Z004","proposed_items":[{"source_id":"PAR-1"}',
         'digest':'saved-response','requested_model':'GLM-test'}
    class Saved:
        root=tmp_path/'saved-controller'
        def execute(self,*a):return row
        def event(self,phase,**kw):events.append((phase,kw))
    monkeypatch.setattr(mod,'connection',lambda:Saved())
    p=ZCodeAccountProvider({'provider_name':'zcode_cn','provider_type':'zcode_account'})
    with pytest.raises(ProviderError) as error:p.invoke_structured(REQUEST,tmp_path)
    assert error.value.error_code=='ZCODE_RECOGNITION_FORMAT'
    assert json.loads((tmp_path/'provider_raw_envelope.json').read_text())==row
    detail=json.loads((tmp_path/'provider_parse_error.json').read_text(encoding='utf-8'))
    assert detail['response_received'] and not detail['automatic_resend']
    assert events[0][0]=='response_format_failed'

def test_optional_channels_can_be_omitted_but_are_audited():
    result,audit=parse_recognition(REQUEST,{'proposed_items':[]})
    assert result['chunk_id']==REQUEST['chunk_id']
    assert set(audit['filled_keys'])==set(ARRAY_KEYS[1:])|{'chunk_id'}
