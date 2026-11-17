"""ZCode-only bounded recognition. Qoder's established segmentation stays unchanged."""
from .recognition_segments import plan_segments,digest
from .models import TenderError

MAX_SOURCES=48
MAX_CHARS=3500
MAX_PROMPT_CHARS=8000
MAX_CALLS=48

def plan_zcode_segments(chunk,extraction,prompt_builder):
    pieces=[]
    for existing in plan_segments(chunk,extraction):
        for block in existing['blocks']:
            if len(block['text'])<=MAX_CHARS:pieces.append(block);continue
            for start in range(0,len(block['text']),MAX_CHARS):
                pieces.append({**block,'text':block['text'][start:start+MAX_CHARS],
                    'source_text_span':[start,min(start+MAX_CHARS,len(block['text']))]})
    groups=[];current=[];chars=0;ids=set()
    for block in pieces:
        trial={'chunk_id':chunk['chunk_id']+'-Z048','blocks':current+[block]}
        if current and (len(current)>=MAX_SOURCES or chars+len(block['text'])>MAX_CHARS
                or block['source_id'] in ids or len(prompt_builder(trial))>MAX_PROMPT_CHARS):
            groups.append(current);current=[];chars=0;ids=set()
        if len(prompt_builder({'chunk_id':chunk['chunk_id']+'-Z048','blocks':[block]}))>MAX_PROMPT_CHARS:
            raise TenderError('ZCODE_SEGMENT_TOO_LARGE','单个来源的标识或表格上下文过长，未发送模型请求；原文保留。')
        current.append(block);chars+=len(block['text']);ids.add(block['source_id'])
    if current:groups.append(current)
    if len(groups)==1 and groups[0]==chunk['blocks']:return [chunk]
    return [{'chunk_id':f"{chunk['chunk_id']}-Z{i:03d}",'parent_chunk_id':chunk['chunk_id'],'blocks':b,
        'char_count':sum(len(x['text']) for x in b)} for i,b in enumerate(groups,1)]

def pending_request_plan(chunks,extraction,prompt_builder,chunks_dir):
    rows=[]
    for chunk in chunks:
        if (chunks_dir/chunk['chunk_id']/'normalized.json').is_file():continue
        segments=plan_zcode_segments(chunk,extraction,prompt_builder)
        rows.append({'chunk_id':chunk['chunk_id'],'requests':len(segments),
            'max_sources':max(len(s['blocks']) for s in segments),
            'max_prompt_characters':max(len(prompt_builder(s)) for s in segments)})
    return {'version':1,'provider':'zcode_cn','max_requests':MAX_CALLS,
        'planned_requests':sum(r['requests'] for r in rows),'chunks':rows,
        'reasoning_effort':'low','max_output_tokens':16384,'customer_content_omitted':True}
