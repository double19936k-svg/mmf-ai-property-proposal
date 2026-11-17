import json
from collections import defaultdict
import pytest
from tender_intake import understanding as u
from tender_intake.zcode_segments import plan_zcode_segments,pending_request_plan,MAX_SOURCES,MAX_CHARS,MAX_PROMPT_CHARS
from providers.base import ProviderError
from tender_intake.recognition_segments import ARRAY_KEYS

def extraction(n=402):
    return {'files':[{'file_id':'F1','original_filename':'synthetic.docx'}],'tables':[],
        'paragraphs':[{'file_id':'F1','para_id':f'P{i}','paragraph_index':i,'text':f'测试中心第{i}区必须每日巡检2次并记录。'} for i in range(n)]}

def test_dense_paragraphs_and_long_unicode_sources_survive_in_bounded_requests():
    e=extraction();e['paragraphs'].append({'file_id':'F1','para_id':'LONG','paragraph_index':403,'text':'甲🙂\n'*2500})
    for chunk in u.build_chunks(e):
        parts=plan_zcode_segments(chunk,e,u._prompt);restored=defaultdict(str)
        for s in parts:
            assert len(s['blocks'])<=MAX_SOURCES and s['char_count']<=MAX_CHARS and len(u._prompt(s))<=MAX_PROMPT_CHARS
            for b in s['blocks']:restored[b['source_id']]+=b['text']
        assert restored=={b['source_id']:b['text'] for b in chunk['blocks']}

def test_zcode_segments_merge_all_channels_and_reuse_successful_caches(tmp_path):
    e=extraction(100);c=u.build_chunks(e)[0];calls=[]
    class P:
        config={'provider_type':'zcode_account','provider_name':'zcode_cn','model':'test'}
        def invoke_structured(self,r,d):
            calls.append(r);return {'chunk_id':r['chunk_id'],**{k:[{'segment':r['chunk_id']}] for k in ARRAY_KEYS}}
    req={'task_id':'synthetic-tender-CHK-0001','generation_mode':'tender_requirement_recognition','reasoning_effort':'low'}
    p=P();result=u._invoke_recognition(p,req,tmp_path,chunk=c,extraction=e)
    assert len(calls)==3 and all(r['max_tokens']==16384 and r['reasoning_effort']=='low' for r in calls)
    for k in ARRAY_KEYS:assert len(result[k])==3
    saved={p:p.read_bytes() for p in tmp_path.rglob('validated_payload.json')}
    u._invoke_recognition(p,req,tmp_path,chunk=c,extraction=e)
    assert len(calls)==3 and all(p.read_bytes()==v for p,v in saved.items())

def test_existing_unknown_parent_is_not_replaced_by_children(tmp_path):
    e=extraction();c=u.build_chunks(e)[0];calls=[]
    (tmp_path/'zcode_stream_progress.json').write_text('{}')
    class P:
        config={'provider_type':'zcode_account'}
        def invoke_structured(self,r,d):calls.append(r);raise ProviderError('unknown',error_code='ZCODE_NO_AUTOMATIC_RESEND')
    with pytest.raises(ProviderError):u._invoke_recognition(P(),{'task_id':'original'},tmp_path,chunk=c,extraction=e)
    assert calls==[{'task_id':'original'}] and not (tmp_path/'recognition_segments').exists()

def test_request_plan_counts_all_remaining_chunks_before_dispatch(tmp_path):
    e=extraction(100);chunks=u.build_chunks(e);plan=pending_request_plan(chunks,e,u._prompt,tmp_path)
    assert plan['planned_requests']==3 and plan['max_requests']==48
    c=chunks[0];folder=tmp_path/c['chunk_id'];folder.mkdir();(folder/'normalized.json').write_text('{}')
    assert pending_request_plan(chunks,e,u._prompt,tmp_path)['planned_requests']==0
