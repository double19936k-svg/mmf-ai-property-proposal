"""Bounded table recognition segments; original source IDs and every character survive."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from .models import TenderError, write_json

PLAN_VERSION = 1
MAX_TABLE_CHARS = 3500
MAX_TABLE_ROWS = 32
MAX_TABLES = 3
ARRAY_KEYS = ('proposed_items','proposed_conflicts','proposed_boilerplate','proposed_scoring_items','chunk_missing_facts')


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),default=str).encode()).hexdigest()


def _table_pieces(block, table):
    text = block['text']
    strings = [' | '.join(str(c) for c in row) for row in [table.get('columns',[]),*(table.get('rows') or [])]]
    matched_rows='\n'.join(strings)==text
    if not matched_rows:
        # A stale extraction must not be assigned invented row positions.
        rendered_lines=text.splitlines(keepends=True)
    else:
        rendered_lines=[value+ ('\n' if index<len(strings)-1 else '') for index,value in enumerate(strings)]
    units=[];offset=0
    for index,line in enumerate(rendered_lines):
        # Exceptionally long rows are split at literal line/character boundaries.
        for start in range(0,len(line),MAX_TABLE_CHARS):
            part=line[start:start+MAX_TABLE_CHARS]
            units.append((offset+start,offset+start+len(part),index))
        offset+=len(line)
    assert offset==len(text)
    spans=[];current=[];chars=0;row_ids=set()
    for unit in units:
        length=unit[1]-unit[0]
        if current and (chars+length>MAX_TABLE_CHARS or len(row_ids|{unit[2]})>MAX_TABLE_ROWS):
            spans.append(current);current=[];chars=0;row_ids=set()
        current.append(unit);chars+=length;row_ids.add(unit[2])
    if current:spans.append(current)
    if not spans:return [{**block,'segment_rows':0}]
    pieces=[]
    for span in spans:
        begin,end=span[0][0],span[-1][1]
        pieces.append({**block,'text':text[begin:end],'segment_rows':len({r[2] for r in span}),
            'source_text_span':[begin,end],'source_row_range':[span[0][2],span[-1][2]],
            'table_context':{'columns':table.get('columns') or [],'row_range':[span[0][2],span[-1][2]],
                'row_units':'table_rows' if matched_rows else 'source_text_lines',
                'note':'列名只辅助理解；证据摘录须来自本段text。'}})
    assert ''.join(p['text'] for p in pieces)==text
    return pieces


def plan_segments(chunk, extraction):
    tables={str(t.get('table_id')):t for t in extraction.get('tables',[])}
    pieces=[]
    for block in chunk['blocks']:
        if block.get('kind')=='table':pieces.extend(_table_pieces(block,tables.get(str(block.get('table_id')),{})))
        else:pieces.append(block)
    groups=[];current=[];chars=0;rows=0;table_count=0;ids=set()
    for block in pieces:
        is_table=block.get('kind')=='table';size=len(block['text']);nrows=int(block.get('segment_rows') or 0)
        # Keep a previously valid paragraph group intact. Paragraphs and tables
        # never share a new segment, so a dense table cannot consume its reply.
        boundary=current and ((current[-1].get('kind')=='table') != is_table)
        overflow=is_table and current and (chars+size>MAX_TABLE_CHARS or rows+nrows>MAX_TABLE_ROWS
            or table_count+1>MAX_TABLES or block['source_id'] in ids)
        if boundary or overflow:
            groups.append(current);current=[];chars=0;rows=0;table_count=0;ids=set()
        current.append(block);chars+=size;rows+=nrows;table_count+=int(is_table);ids.add(block['source_id'])
    if current:groups.append(current)
    if len(groups)<=1:return [chunk]
    return [{'chunk_id':f"{chunk['chunk_id']}-S{index:03d}",'parent_chunk_id':chunk['chunk_id'],
        'blocks':blocks,'char_count':sum(len(b['text']) for b in blocks)} for index,blocks in enumerate(groups,1)]


def execute_segments(provider, request, chunk, extraction, call_dir: Path, segments, prompt_builder, mock_builder, progress=None, *, max_tokens=32000):
    identity=dict(getattr(provider,'config',{}) or {})
    provider_identity={k:identity.get(k) for k in ('provider_name','provider_type','model')}
    plan_digest=digest({'version':PLAN_VERSION,'chunk':chunk,'segments':segments,'provider':provider_identity})
    call_dir.mkdir(parents=True,exist_ok=True)
    evidence=call_dir/'recognition_segments';evidence.mkdir(exist_ok=True)
    plan={'version':PLAN_VERSION,'plan_digest':plan_digest,'parent_chunk_id':chunk['chunk_id'],
        'segments_total':len(segments),'segments_completed':0,'max_table_chars':MAX_TABLE_CHARS,
        'max_table_rows':MAX_TABLE_ROWS,'max_tables_per_segment':MAX_TABLES,'status':'running','segments':[]}
    merged={'chunk_id':chunk['chunk_id'],**{key:[] for key in ARRAY_KEYS}}
    for segment in segments:
        if progress:progress({'parent_chunk_id':chunk['chunk_id'],'current':plan['segments_completed']+1,
            'completed':plan['segments_completed'],'total':len(segments)})
        # Original IDs stay intact even when one table spans several requests.
        segment_key=digest({'blocks':segment['blocks'],'provider':provider_identity})[:20]
        task_id=str(request['task_id'])+'-segment-'+segment_key
        mock=mock_builder(extraction,segment)
        allowed={b['source_id']:b['text'] for b in segment['blocks']}
        mock['proposed_items']=[r for r in mock.get('proposed_items',[]) if r.get('source_excerpt') in allowed.get(r.get('source_id'),'')]
        child={**request,'task_id':task_id,'chunk_id':segment['chunk_id'],'prompt':prompt_builder(segment),
            'mock_response':mock,'max_tokens':max_tokens}
        for identity_key in ('request_id', 'job_id'):
            if request.get(identity_key):child[identity_key]=str(request[identity_key])+'-segment-'+segment_key
        child_digest=digest(child);folder=evidence/segment_key;folder.mkdir(exist_ok=True)
        cached=folder/'validated_payload.json'
        item={'segment_id':segment['chunk_id'],'task_id':task_id,'request_digest':child_digest,
            'source_ids':[b['source_id'] for b in segment['blocks']],'characters':segment['char_count'],
            'tables':sum(b.get('kind')=='table' for b in segment['blocks']),'status':'running'}
        try:
            if cached.is_file():
                prior=json.loads(cached.read_text(encoding='utf-8'))
                if prior.get('request_digest')!=child_digest:
                    raise TenderError('SEGMENT_IDENTITY_MISMATCH','子块与原请求内容不一致，未重新派发。')
                payload=prior['payload'];item['cache_reused']=True
            else:
                payload=provider.invoke_structured(child,folder/'provider_call');item['cache_reused']=False
            if payload.get('chunk_id')!=segment['chunk_id'] or any(not isinstance(payload.get(k),list) for k in ARRAY_KEYS):
                raise TenderError('UNDERSTAND_SEGMENT_SCHEMA_INVALID','子块结果结构不完整，已保留原答。')
            if not cached.exists():write_json(cached,{'request_digest':child_digest,'payload':payload})
            for key in ARRAY_KEYS:merged[key].extend(payload[key])
            item['status']='PASS';plan['segments_completed']+=1
            if progress:progress({'parent_chunk_id':chunk['chunk_id'],'current':plan['segments_completed'],
                'completed':plan['segments_completed'],'total':len(segments)})
        except Exception as exc:
            item.update(status='FAIL',error_code=getattr(exc,'error_code',None),error_type=type(exc).__name__)
            plan['segments'].append(item);plan['status']='incomplete'
            write_json(evidence/'plan.json',plan)
            raise
        plan['segments'].append(item);write_json(evidence/'plan.json',plan)
    plan['status']='complete';write_json(evidence/'plan.json',plan)
    merged['provider_metadata']={'provider_name':provider_identity.get('provider_name'),
        'model':provider_identity.get('model'),'recognition_segments':plan,'raw_responses_preserved':True}
    return merged
