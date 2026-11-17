"""Offline evidence for lossless table splitting and resumable partial responses."""
import json
from collections import defaultdict
import pytest

from providers.base import ProviderError
from tender_intake import understanding as u
from tender_intake.models import TenderError
from tender_intake.recognition_segments import ARRAY_KEYS, execute_segments, plan_segments


def extraction(rows=80):
    return {'files':[{'file_id':'F1','original_filename':'synthetic.docx'}],
        'paragraphs':[], 'tables':[{'file_id':'F1','table_id':'T1','table_index':1,
        'columns':['岗位','服务要求'], 'rows':[[str(i),f'每天巡检{i}次，必须记录。'] for i in range(rows)]}]}


def context(rows=80):
    e=extraction(rows);chunk=u.build_chunks(e)[0]
    return e,chunk,plan_segments(chunk,e)


def request(chunk):
    return {'run_id':'synthetic','task_id':'synthetic-tender-'+chunk['chunk_id'],
        'job_id':'job','request_id':'request','chunk_id':chunk['chunk_id'],
        'prompt':u._prompt(chunk),'system_prompt':'synthetic',
        'json_schema':u.PROVIDER_SCHEMA,'required_keys':sorted(u.PROVIDER_REQUIRED_KEYS),
        'generation_mode':'tender_requirement_recognition','timeout_seconds':600}


class FakeProvider:
    config={'provider_type':'qoder_account','provider_name':'qoder_account','model':'synthetic'}
    def __init__(self, fail_at=None, invalid=None):
        self.calls=[];self.fail_at=fail_at;self.invalid=invalid
    def invoke_structured(self,r,d):
        self.calls.append(r)
        if len(self.calls)==self.fail_at:
            raise ProviderError('请求结果未知，不重新派发',error_code='RESPONSE_UNKNOWN')
        answer={'chunk_id':r['chunk_id'],**{k:[{'segment':r['chunk_id']}] for k in ARRAY_KEYS}}
        if self.invalid=='identity':answer['chunk_id']='OTHER'
        if self.invalid=='channel':answer['proposed_conflicts']=None
        return answer


def run(provider,req,e,chunk,segments,path,progress=None):
    return execute_segments(provider,req,chunk,e,path,segments,u._prompt,u._mock_payload,progress)


@pytest.mark.parametrize('variant',['ordinary','long_row','newlines','stale_metadata','empty'])
def test_every_literal_character_and_original_locator_survives(variant):
    e=extraction()
    if variant=='long_row':e['tables'][0]['rows'][3][1]='合同要求甲🙂'*1900
    if variant=='newlines':e['tables'][0]['rows'][5][1]='第一行\n第二行\r\n第三行'
    if variant=='empty':e['tables'][0].update(columns=[],rows=[])
    chunk=u.build_chunks(e)[0]
    if variant=='stale_metadata':e['tables'][0]['columns']=['stale']
    segments=plan_segments(chunk,e);reconstructed=defaultdict(str)
    for segment in segments:
        assert segment['chunk_id'].startswith(chunk['chunk_id'])
        for part in segment['blocks']:
            original=next(b for b in chunk['blocks'] if b['source_id']==part['source_id'])
            for key in ('source_id','file_id','table_id','table_index','source_file','source_paragraph_or_table'):
                assert part[key]==original[key]
            reconstructed[part['source_id']]+=part['text']
        if len(segments)>1:
            assert segment['char_count']<=3500
            assert sum(b.get('segment_rows',0) for b in segment['blocks'])<=32
            assert len(segment['blocks'])<=3
    assert reconstructed=={b['source_id']:b['text'] for b in chunk['blocks']}


def test_many_small_tables_are_bounded_and_paragraph_route_is_unchanged():
    e=extraction(1);base=e['tables'][0]
    e['tables']=[{**base,'table_id':f'T{i}','table_index':i} for i in range(10)]
    c=u.build_chunks(e)[0];parts=plan_segments(c,e)
    assert [len(p['blocks']) for p in parts]==[3,3,3,1]
    e={'files':e['files'],'tables':[], 'paragraphs':[{'file_id':'F1','para_id':'P1','paragraph_index':1,'text':'原来正常的段落'*1000}]}
    c=u.build_chunks(e)[0]
    assert plan_segments(c,e)[0] is c


def test_all_proposal_channels_merge_and_reuse_without_new_calls(tmp_path):
    e,c,parts=context();p=FakeProvider();r=request(c);updates=[]
    merged=run(p,r,e,c,parts,tmp_path,updates.append)
    assert merged['chunk_id']==c['chunk_id']
    for key in ARRAY_KEYS:assert [row['segment'] for row in merged[key]]==[s['chunk_id'] for s in parts]
    assert len({call['request_id'] for call in p.calls})==len(parts)
    assert len({call['job_id'] for call in p.calls})==len(parts)
    assert all(call['max_tokens']==32000 for call in p.calls)
    assert updates[-1]['completed']==len(parts)
    again=run(p,r,e,c,parts,tmp_path)
    for key in ('chunk_id',*ARRAY_KEYS):assert again[key]==merged[key]
    # Metadata contains fresh cache provenance; the candidate channels stay exact.
    assert len(p.calls)==len(parts)


def test_partial_failure_keeps_subresults_and_resume_skips_them(tmp_path):
    e,c,parts=context();r=request(c);p=FakeProvider(fail_at=2)
    with pytest.raises(ProviderError):run(p,r,e,c,parts,tmp_path)
    plan=json.loads((tmp_path/'recognition_segments/plan.json').read_text())
    assert plan['status']=='incomplete' and plan['segments_completed']==1
    assert len(list(tmp_path.rglob('validated_payload.json')))==1
    saved={f:f.read_bytes() for f in tmp_path.rglob('validated_payload.json')}
    resumed=FakeProvider();merged=run(resumed,r,e,c,parts,tmp_path)
    assert len(resumed.calls)==len(parts)-1
    assert all(f.read_bytes()==body for f,body in saved.items())
    assert len(merged['proposed_items'])==len(parts)


@pytest.mark.parametrize('invalid',['identity','channel'])
def test_invalid_child_does_not_complete_parent_or_cache_payload(tmp_path,invalid):
    e,c,parts=context();p=FakeProvider(invalid=invalid)
    with pytest.raises(TenderError):run(p,request(c),e,c,parts,tmp_path)
    assert not list(tmp_path.rglob('validated_payload.json'))
    assert json.loads((tmp_path/'recognition_segments/plan.json').read_text())['status']=='incomplete'
    assert len(p.calls)==1


def test_changed_instructions_cannot_reuse_or_resend_cached_identity(tmp_path):
    e,c,parts=context();p=FakeProvider();r=request(c)
    run(p,r,e,c,parts,tmp_path);prior=len(p.calls)
    with pytest.raises(TenderError,match='不一致'):
        run(p,{**r,'system_prompt':'changed'},e,c,parts,tmp_path)
    assert len(p.calls)==prior


def test_segmentation_is_scoped_to_qoder_and_does_not_retry_unknown(tmp_path):
    e,c,parts=context();p=FakeProvider(fail_at=1)
    with pytest.raises(ProviderError):u._invoke_recognition(p,request(c),tmp_path/'qoder',chunk=c,extraction=e)
    assert len(p.calls)==1
    for provider_type in ('openai_compatible',):
        p=FakeProvider();p.config={'provider_type':provider_type}
        u._invoke_recognition(p,request(c),tmp_path/provider_type,chunk=c,extraction=e)
        assert len(p.calls)==1 and p.calls[0]['task_id']==request(c)['task_id']


def test_parent_pass_caches_are_byte_identical_after_segmented_resume(tmp_path,monkeypatch):
    from tender_intake.models import write_json
    e,c,parts=context()
    blocks=[{'chunk_id':f'CHK-{i:04d}','blocks':[],'char_count':0} for i in range(1,5)]
    c={**c,'chunk_id':'CHK-0005'}
    monkeypatch.setattr(u,'build_chunks',lambda extraction:blocks+[c])
    monkeypatch.setattr(u,'build_requirement_pack',lambda *a,**kw:{'status':'confirmation_required'})
    from tender_intake.proposal_channels import CHANNELS
    monkeypatch.setattr('tender_intake.proposal_channels.normalize_channels',lambda *a:{key:[] for key in CHANNELS})
    for old in blocks:
        folder=tmp_path/'tender/chunks'/old['chunk_id'];folder.mkdir(parents=True)
        write_json(folder/'normalized.json',{'proposal_channels_version':1,'proposed_items':[], 'proposed_scoring_items':[]})
        (folder/'proof.txt').write_text('already valid',encoding='utf-8')
    before={f:f.read_bytes() for f in (tmp_path/'tender/chunks').rglob('*') if f.is_file()}
    class MockProvider(FakeProvider):
        def invoke_structured(self,r,d):
            self.calls.append(r)
            return r['mock_response']
    p=MockProvider()
    result=u.understand_run(tmp_path,e,p,resume=True)
    assert result['status']['recognition_outcome']=='AI_RECOGNITION_SUCCESS'
    assert result['status']['chunks_completed']==5
    assert len(p.calls)==len(parts)
    assert all(f.read_bytes()==body for f,body in before.items())


@pytest.mark.parametrize('legacy_dir',['.','output_budget_retry'])
def test_upgrade_cannot_bypass_an_unknown_legacy_parent(tmp_path,legacy_dir):
    e,c,parts=context();folder=tmp_path/legacy_dir;folder.mkdir(exist_ok=True)
    (folder/'qoder_request_identity.json').write_text('{}')
    p=FakeProvider(fail_at=1)
    with pytest.raises(ProviderError):u._invoke_recognition(p,request(c),tmp_path,chunk=c,extraction=e)
    assert p.calls[0]['task_id']==request(c)['task_id']
    assert not (tmp_path/'recognition_segments').exists()
