"""Persistent coordination for account Agents only; API providers never enter here."""
from __future__ import annotations
import hashlib,json,re,threading,time,uuid
from pathlib import Path
from providers.base import ProviderError,write_json,parse_provider_payload
from account_issue_policy import FORMAT_CODES,classify

PROVIDERS={'qoder_account','zcode_cn'}
FILE_GENERATION_MODES={'longform_section','longform_batch','ppt_slide'}
FORMAT_REPAIR_OPTION='继续整理已返回内容（1次请求）'
FORMAT_QUESTION='程序未能安全整理这份原答。可以让原会话仅整理一次格式（占用原预算1次请求），或保留内容暂不继续；无需你修改JSON。'
INSTANCE=uuid.uuid4().hex
LOCK=threading.RLock()
ACTIVE=set()
LABELS={'running':'运行中','waiting_for_user':'等待用户确认','resuming':'恢复执行',
        'content_ready':'当前内容已接收，等待检查与装配','needs_attention':'内容已保留，待处理','failed':'执行失败','interrupted':'执行中断，待核对','cancelled':'已停止'}
CONTRACT='''\n账号Agent交互规则：普通写作、排版及既定范围内的技术步骤自行完成，不逐页询问。需求识别缺少事实时使用原chunk_missing_facts，写作缺少事实时按原任务保留待确认项，不逐块打断用户。不得编造缺失事实或自行扩大范围和预算。只有执行规则冲突、无法依据既有要求继续的重要决策才返回一个独立JSON：{"mmf_interaction":{"kind":"facts|scope|budget|structure_precedence","question":"具体问题","choices":[{"value":"稳定选项ID","label":"选项文字"}]}}。不得把该对象放进正文。没有必要问题时直接返回原任务要求的完整结果JSON。Word/PPT结构差异默认沿用Word目录映射，不改项目事实；除非任务已有明确相反规则。'''

def decision_path(row):
    key=hashlib.sha256(json.dumps([row['provider'],row['run_id'],row.get('account_model')]).encode()).hexdigest()
    return root()/'decisions'/(key+'.json')

def question_key(q):return hashlib.sha256(json.dumps(q,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def root():
    from paths import current
    return current().runtime_dir/'account_agents'

def task_key(provider,request,scope=''):
    identity=request.get('request_id') or request.get('job_id') or request.get('task_id')
    return hashlib.sha256(json.dumps([provider,str(scope),request.get('run_id','standalone'),identity],ensure_ascii=False).encode()).hexdigest()[:32]

def folder(key):
    if not re.fullmatch('[a-f0-9]{32}',key):raise ProviderError('任务编号无效。',error_code='ACCOUNT_TASK_ID')
    return root()/key

def read(path):return json.loads(path.read_text(encoding='utf-8'))


def run_visible(run_id):
    from paths import current
    if not isinstance(run_id,str) or not re.fullmatch('[A-Za-z0-9_-]{1,100}',run_id):return False
    run=current().runs_dir/run_id
    return run.is_dir() and not (run/'history_hidden.json').exists()


def require_visible(row):
    if not run_visible(row.get('run_id')):
        raise ProviderError('该记录已从历史移除，不再提供任务操作；保留的原始记录不会被重建。',error_code='ACCOUNT_RUN_HIDDEN')

def state(key):
    path=folder(key)/'state.json'
    if not path.exists():raise ProviderError('未找到该账号任务。',error_code='ACCOUNT_TASK_MISSING')
    return read(path)

def put(key,row,**updates):
    with LOCK:
        row={**row,**updates,'updated_at':time.time()};write_json(folder(key)/'state.json',row)
        with (folder(key)/'events.jsonl').open('a',encoding='utf-8') as out:
            out.write(json.dumps({k:row.get(k) for k in ('updated_at','state','turn','session_id','error_code')},ensure_ascii=False)+'\n')
    return row

def public(row):
    out={k:row.get(k) for k in ('key','provider','run_id','task_id','state','session_id','turn','question','nonce','message','error_code','handling_detail','updated_at')}
    answers=row.get('answers') or []
    if row['state']=='resuming' and answers and answers[-1].get('source')=='automatic_generation_policy':
        out['automatically_repairing_format']=True
    out['label']=LABELS.get(row['state'],row['state']);out['artifact_verified']=False
    out['retry_available']=row['state']=='needs_attention' and row.get('error_code')=='ZCODE_NOT_DISPATCHED'
    out['purpose_label']='需求识别' if '-tender-' in str(row.get('task_id','')) else 'PPT内容' if '-ppt-' in str(row.get('task_id','')) else '账号内容任务'
    # A provider reply is not a completed Word/PPT artifact.
    return out

def list_tasks(run_id=''):
    result=[]
    for p in root().glob('*/state.json'):
        row=read(p)
        if not run_visible(row.get('run_id')):continue
        if run_id and row.get('run_id')!=run_id:continue
        if row['state'] in ('failed','needs_attention') and row.get('error_code') in FORMAT_CODES and not row.get('offline_repair_checked'):
            row=repair_saved(row['key'])
        if row['state'] in ('running','resuming') and row.get('owner')!=INSTANCE:
            row=put(row['key'],row,state='interrupted',message='服务在执行期间中断，原请求结果需核对；未自动重发。')
        result.append(public(row))
    return sorted(result,key=lambda x:x.get('updated_at',0),reverse=True)[:100]

def legacy(provider,request,ctl):
    rid=str(request.get('run_id') or 'standalone');slot=str(request.get('request_id') or request.get('job_id') or request.get('task_id') or '')
    jobs=ctl.root/'jobs'/hashlib.sha256(rid.encode()).hexdigest()[:16 if provider=='qoder_account' else 24]
    key=hashlib.sha256(((rid+'|'+slot) if provider=='qoder_account' else slot).encode()).hexdigest()[:24]
    return (jobs/(key+'.json')).exists()

def interaction(body):
    if isinstance(body,str):
        fenced=re.fullmatch(r'\s*```(?:json)?\s*([\s\S]*?)\s*```\s*',body)
        if fenced:body=fenced.group(1)
    try:v=json.loads(body)
    except (ValueError,TypeError):return None
    if not isinstance(v,dict) or set(v)!={'mmf_interaction'}:return None
    q=v['mmf_interaction']
    if not isinstance(q,dict) or q.get('kind') not in {'facts','scope','budget','structure_precedence'}:raise ProviderError('Agent问题类型无效，原答已保留。',error_code='ACCOUNT_QUESTION_INVALID')
    if not isinstance(q.get('question'),str) or not 1<=len(q['question'])<=1500:raise ProviderError('Agent问题格式无效。',error_code='ACCOUNT_QUESTION_INVALID')
    choices=q.get('choices',[])
    if not isinstance(choices,list) or len(choices)>6:raise ProviderError('Agent问题选项无效。',error_code='ACCOUNT_QUESTION_INVALID')
    if any(not isinstance(c,dict) or not isinstance(c.get('value'),str) or not isinstance(c.get('label'),str) or len(c['value'])>80 or len(c['label'])>300 for c in choices):raise ProviderError('Agent问题选项无效。',error_code='ACCOUNT_QUESTION_INVALID')
    if len({c['value'] for c in choices})!=len(choices):raise ProviderError('Agent选项重复。',error_code='ACCOUNT_QUESTION_INVALID')
    return {'kind':q['kind'],'question':q['question'],'choices':choices}

def validate(provider,request,envelope):
    if request.get('generation_mode')=='tender_requirement_recognition':
        from providers.account_recognition import parse_recognition
        result,_=parse_recognition(provider,request,envelope.get('body'))
    else:result,_=parse_provider_payload(request,envelope.get('body'),set(request.get('required_keys') or []))
    identity='slide_id' if request.get('generation_mode')=='ppt_slide' else 'chunk_id' if request.get('generation_mode')=='tender_requirement_recognition' else None
    if identity and result.get(identity)!=request.get(identity):raise ProviderError('Agent返回页号与请求不一致，原答保留。' if identity=='slide_id' else 'Agent结果编号不符，原答保留。',error_code='PPT_SLIDE_ID_MISMATCH' if identity=='slide_id' else 'ACCOUNT_RESULT_IDENTITY')


def repair_saved(key):
    """Re-parse a known returned receipt; never start or continue a model call."""
    with LOCK:
        row=state(key);path=folder(key);request=read(path/'request.json')
        receipt=path/f'receipt-{max(0,row["turn"]-1)}.json'
        # An explicit local recovery may recheck an identity refusal after a
        # bounded parser update. A genuine mismatch still fails validation and
        # remains isolated; this never sends or resumes a model request.
        local_recheck=FORMAT_CODES|{'QODER_RECOGNITION_ID_MISMATCH','ZCODE_RECOGNITION_ID_MISMATCH'}
        if row.get('error_code') not in local_recheck or not receipt.exists():return row
        envelope=read(receipt)
        try:validate(row['provider'],request,envelope)
        except ProviderError as exc:
            return put(key,row,**issue_updates(key,row,exc.error_code,str(exc)),offline_repair_checked=True)
        write_json(path/'final.json',envelope)
        return put(key,row,state='content_ready',error_code=None,question=None,nonce=None,offline_repair_checked=True,
                   previous_error_code=row.get('error_code'),handling_detail='程序已整理完整原答，未新增模型请求。',
                   message='当前内容已从原答自动恢复；后续未完成块仍需接续，整份文件尚未完成。')


def issue_updates(key,row,code,message):
    phase,detail=classify(code)
    updates={'state':phase,'error_code':code,'message':message[:600],'handling_detail':detail}
    if code in FORMAT_CODES:
        updates.update(message=detail)
        receipt=folder(key)/f'receipt-{max(0,row["turn"]-1)}.json'
        if row.get('session_id') and row['turn']<3 and not row.get('format_retries') and receipt.exists():
            updates.update(question={'kind':'response_format','question':FORMAT_QUESTION,'choices':[{'value':'repair','label':FORMAT_REPAIR_OPTION},{'value':'retain','label':'保留内容，暂不继续'}]},nonce=uuid.uuid4().hex)
    return updates

def execute(provider,request,task_dir,ctl,cancelled=lambda:False):
    if provider not in PROVIDERS:raise ProviderError('此任务管理器仅支持账号Agent。',error_code='ACCOUNT_PROVIDER_SCOPE')
    key=task_key(provider,request,ctl.root);path=folder(key);path.mkdir(parents=True,exist_ok=True)
    digest=hashlib.sha256(json.dumps(request,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
    with LOCK:
        if (path/'state.json').exists():
            row=state(key)
            unsent = ((row.get('turn') or 0) == 0 and not row.get('session_id')
                      and not (path/'receipt-0.json').exists() and not (path/'final.json').exists()
                      and row.get('state') in {'needs_attention','failed','interrupted'}
                      and row.get('error_code') in {'QODER_MODEL_LOCKED','ZCODE_MODEL_LOCKED','ZCODE_NOT_DISPATCHED','ACCOUNT_MODEL_CHANGED'})
            if row['digest']!=digest:
                # A request that never reached the model can take the current payload.
                # A sent or answered task keeps its original identity.
                if unsent:
                    write_json(path/'request.json', request)
                    row = put(key, row, digest=digest, state='resuming', error_code=None, question=None, nonce=None, message='原请求尚未派发，已按当前内容重新准备。')
                else:
                    raise ProviderError('账号任务内容与原请求不符，未派发。',error_code='ACCOUNT_TASK_IDENTITY')
            elif unsent:
                row = put(key, row, state='resuming', error_code=None, question=None, nonce=None, message='原请求尚未派发，准备重新派发。')
            if (path/'final.json').exists():return read(path/'final.json')
            receipt=path/f'receipt-{max(0,row["turn"]-1)}.json'
            if row['state'] in ('failed','needs_attention') and row.get('error_code') in FORMAT_CODES and receipt.exists():
                envelope=read(receipt);validate(provider,request,envelope);write_json(path/'final.json',envelope)
                put(key,row,state='content_ready',error_code=None,question=None,nonce=None,handling_detail=None,message='已从完整原答恢复，不新增模型调用。');return envelope
            if key in ACTIVE:raise ProviderError('该账号任务已在执行，请查看原任务。',error_code='ACCOUNT_TASK_BUSY')
            if row['state']=='needs_attention' and row.get('error_code')=='ZCODE_NOT_DISPATCHED':
                row=put(key,row,state='resuming',owner=INSTANCE)
            if row['state'] not in ('waiting_for_user','resuming'):raise ProviderError(row.get('message') or '原账号任务状态需核对，未自动重发。',error_code='ACCOUNT_TASK_INTERRUPTED')
        else:
            # Preserve all already-dispatched request identities and cached replies.
            if legacy(provider,request,ctl):return ctl.execute(request,Path(task_dir),cancelled)
            write_json(path/'request.json',request)
            row={'key':key,'provider':provider,'account_model':getattr(ctl,'selection',{}).get('model'),'run_id':request.get('run_id','standalone'),'task_id':request.get('task_id'),
                 'digest':digest,'turn':0,'session_id':None,'owner':INSTANCE,'state':'running','answers':[],'question':None,'message':'正在执行账号Agent内容任务。'}
            if request.get('response_deadline_epoch'):
                row['response_window_seconds']=max(1,min(240,float(request['response_deadline_epoch'])-time.time()))
            row=put(key,row)
        ACTIVE.add(key)
    try:
        if row.get('account_model')!=getattr(ctl,'selection',{}).get('model'):
            raise ProviderError('当前模型与原账号任务不同，未恢复其他模型。',error_code='ACCOUNT_MODEL_CHANGED')
        while True:
            row=state(key)
            if cancelled() or row['state']=='cancelled':raise ProviderError('账号任务已停止，已有结果保留。',error_code='ACCOUNT_CANCELLED')
            if row['state']=='waiting_for_user':
                time.sleep(.2);continue
            if row['turn']>=3:raise ProviderError('当前内容已到3轮交互上限，已有结果保留。',error_code='ACCOUNT_TURN_BUDGET')
            payload=dict(request)
            if row['turn']==0:payload['system_prompt']=str(payload.get('system_prompt') or '')+CONTRACT
            else:
                if not row['session_id']:raise ProviderError('未取得可恢复的原会话，未新建替代会话。',error_code='ACCOUNT_SESSION_MISSING')
                payload['native_session_id']=row['session_id']
                # A human reply is a new native turn, not an already-expired
                # transport wait. Keep the original whole-plan wall deadline.
                if request.get('response_deadline_epoch'):
                    payload['response_deadline_epoch']=min(time.time()+row.get('response_window_seconds',180),float(request.get('generation_deadline_epoch') or float('inf')))
                if row['answers'][-1].get('kind')=='response_format':
                    if request.get('response_protocol')=='cu_envelope':
                        payload['prompt']='只整理上一轮完整原答的格式，按原任务的CU封装标记返回完整内容。保留正文、引文和原编号，不补写、不删改事实、不调用工具。'
                    else:
                        payload['prompt']='上一轮业务内容已经生成，但返回格式未通过。只整理上一轮完整原答的JSON序列化，保留全部候选、引文和原编号，不重新识别、不删改事实、不调用工具。返回原任务约定的完整结果。原结果字段：'+json.dumps(request.get('required_keys',[]),ensure_ascii=False)
                else:
                    payload['prompt']='对上一轮问题的用户决定：'+json.dumps(row['answers'][-1],ensure_ascii=False)+'\n请依据此决定继续原任务，返回原来约定的完整结果。原结果字段：'+json.dumps(request.get('required_keys',[]),ensure_ascii=False)
                for k in ('request_id','job_id','task_id'):
                    if payload.get(k):payload[k]=str(payload[k])+f'-decision-{row["turn"]}'
            row=put(key,row,state='resuming' if row['turn'] else 'running',owner=INSTANCE,question=None,nonce=None,error_code=None,handling_detail=None)
            envelope=ctl.execute(payload,Path(task_dir) if row['turn']==0 else path/f'turn-{row["turn"]}',lambda:cancelled() or state(key)['state']=='cancelled')
            write_json(path/f'receipt-{row["turn"]}.json',envelope)
            session=envelope.get('session_id') or row['session_id']
            if row['session_id'] and session!=row['session_id']:raise ProviderError('恢复返回了不同会话，未将它当成原任务继续。',error_code='ACCOUNT_SESSION_CHANGED')
            with LOCK:
                row=state(key)
                if cancelled() or row['state']=='cancelled':
                    raise ProviderError('账号任务已停止，已有结果保留。',error_code='ACCOUNT_CANCELLED')
                row=put(key,row,session_id=session,turn=row['turn']+1)
            if envelope.get('permission_denied_tools'):
                raise ProviderError('Agent提出了当前内容通道未授权的工具操作，已拒绝并保留会话；请在网页查看原因。',error_code='ACCOUNT_PERMISSION_UNSUPPORTED')
            q=interaction(envelope.get('body'))
            if q:
                if row['turn']>=3:raise ProviderError('当前内容已到3轮交互上限，原答已保留；未追加调用。',error_code='ACCOUNT_TURN_BUDGET')
                if not session:raise ProviderError('Agent提出问题但未提供会话编号，无法安全恢复。',error_code='ACCOUNT_SESSION_MISSING')
                decisions=read(decision_path(row)) if decision_path(row).exists() else {}
                approved=decisions.get(question_key(q)) if q['kind']=='structure_precedence' else None
                if approved:
                    row=put(key,row,state='resuming',answers=[*row['answers'],approved],message='复用本任务此前同一结构问题的明确决定。')
                    continue
                row=put(key,row,state='waiting_for_user',question=q,nonce=uuid.uuid4().hex,message='需要一个重要决定；回复后继续同一会话，使用原有调用预算。')
                continue
            write_json(Path(task_dir)/'provider_raw_envelope.json',envelope)
            try:validate(provider,request,envelope)
            except ProviderError as exc:
                write_json(Path(task_dir)/'provider_parse_error.json',{'error_code':exc.error_code,'message':str(exc),'request_digest':envelope.get('digest'),'response_received':True,'automatic_resend':False})
                ctl.event('response_format_failed',run_id=row['run_id'],task_id=request.get('task_id'),error_code=exc.error_code)
                if request.get('generation_mode') in FILE_GENERATION_MODES and exc.error_code in FORMAT_CODES:
                    with LOCK:
                        row=state(key)
                        if (row['state']!='cancelled' and not cancelled() and row.get('session_id')
                                and row['turn']<3 and not row.get('format_retries')):
                            decision={'kind':'response_format','question':FORMAT_QUESTION,
                                      'answer':FORMAT_REPAIR_OPTION,'source':'automatic_generation_policy'}
                            row=put(key,row,state='resuming',question=None,nonce=None,format_retries=1,
                                    answers=[*row['answers'],decision],
                                    message='正在原会话自动整理已返回的内容，占用原预算1次请求。')
                            continue
                raise
            write_json(path/'final.json',envelope)
            put(key,row,state='content_ready',error_code=None,handling_detail=None,message='当前内容格式与编号检查通过；整份Word/PPT仍由原生成流程检查。')
            return envelope
    except Exception as exc:
        row=state(key)
        if row['state']!='cancelled':
            code=getattr(exc,'error_code','ACCOUNT_TASK_FAILED')
            put(key,row,**issue_updates(key,row,code,str(exc)),offline_repair_checked=True)
        raise
    finally:
        with LOCK:ACTIVE.discard(key)

def answer(key,nonce,text,on_recovered=None):
    with LOCK:
        row=state(key)
        require_visible(row)
        if row['state'] not in ('waiting_for_user','needs_attention') or not row.get('question') or row.get('nonce')!=nonce:raise ProviderError('问题已变化或已经回复，请刷新任务。',error_code='ACCOUNT_STALE_REPLY')
        if not isinstance(text,str) or not text.strip() or len(text)>3000:raise ProviderError('请填写不超过3000字的回复。',error_code='ACCOUNT_REPLY_INVALID')
        if row['turn']>=3:raise ProviderError('已到交互轮数上限，未继续调用。',error_code='ACCOUNT_TURN_BUDGET')
        if row['question']['kind']=='response_format':
            if text.strip()=='保留内容，暂不继续':return public(put(key,row,nonce=None,question=None,message='内容和原会话已保留；没有继续调用模型。'))
            if text.strip()!=FORMAT_REPAIR_OPTION:raise ProviderError('请明确选择是否继续整理；不会自动发送新请求。',error_code='ACCOUNT_REPLY_INVALID')
            row['format_retries']=int(row.get('format_retries',0))+1
        decision={'kind':row['question']['kind'],'question':row['question']['question'],'answer':text.strip()}
        if row['question']['kind']=='structure_precedence':
            path=decision_path(row);saved=read(path) if path.exists() else {};saved[question_key(row['question'])]=decision;write_json(path,saved)
        row=put(key,row,state='resuming',answers=[*row['answers'],decision],nonce=None,message='决定已保存，正在恢复原会话。')
        active=key in ACTIVE
    if not active:
        callback=None if decision['kind']=='response_format' else on_recovered
        threading.Thread(target=_recover,args=(key,callback),daemon=True).start()
    return public(row)

def _recover(key,on_recovered=None):
    row=state(key);request=read(folder(key)/'request.json')
    try:
        if not run_visible(row.get('run_id')):return
        if row['provider']=='qoder_account':from qoder_account.connection import connection
        else:from zcode_account.connection import connection
        execute(row['provider'],request,folder(key),connection())
        if on_recovered:on_recovered(row,request)
        # The result is cached. Re-entering the original workflow consumes no new
        # call for this content and still uses its normal QA/artifact checks.
    except Exception as exc:
        current=state(key)
        if current['state']=='content_ready':put(key,current,message='答案已保存，但原流程接续未完成；可点击“接续原任务”。',workflow_resume_error=type(exc).__name__)

def cached_request(provider,request):
    for p in root().glob('*/request.json'):
        original=read(p)
        row=read(p.with_name('state.json'))
        if row['provider']!=provider or original.get('run_id')!=request.get('run_id'):continue
        if request.get('request_id') and original.get('request_id')!=request['request_id']:continue
        if original.get('task_id')!=request.get('task_id'):continue
        if request.get('slide_id') and original.get('slide_id')!=request['slide_id']:continue
        if (p.parent/'final.json').exists():return original
    return None

def cancel(key):
    with LOCK:
        row=state(key)
        require_visible(row)
        if row['state']!='content_ready':row=put(key,row,state='cancelled',nonce=None,message='已停止账号任务；已返回内容和会话记录保留。')
    return public(row)

def continue_workflow(key,callback):
    row=state(key)
    require_visible(row)
    if row['state']=='needs_attention' and row.get('error_code')=='ZCODE_NOT_DISPATCHED':
        threading.Thread(target=callback,args=(row,read(folder(key)/'request.json')),daemon=True).start()
        return public(row)
    if row['state']!='content_ready' or not (folder(key)/'final.json').exists():raise ProviderError('当前没有可复用的完整内容。',error_code='ACCOUNT_CONTENT_NOT_READY')
    threading.Thread(target=_recover,args=(key,callback),daemon=True).start()
    return public(row)


def recognition_handling(run_id,status):
    """UI projection only; never changes canonical incomplete/fallback guards."""
    if status.get('provider_name') not in PROVIDERS or status.get('recognition_outcome')=='AI_RECOGNITION_SUCCESS':return None
    tasks=[t for t in list_tasks(run_id) if '-tender-' in str(t.get('task_id',''))]
    if not tasks:return None
    if any(t['state']=='needs_attention' for t in tasks) and all(t.get('retry_available') or t['state']=='content_ready' for t in tasks):
        return {'state':'pending_resume','title':'AI识别待继续','message':f"中断步骤已核实当时尚未发送，连接处理后可接续；已缓存{status.get('chunks_completed',0)}/{status.get('chunks_total',0)}块直接复用。",'can_continue':True}
    if any(t['state']=='needs_attention' for t in tasks):
        return {'state':'needs_attention','title':'AI识别待处理','message':'已有回复或执行条件需要处理，内容已保留。请在本页下方“账号任务进度与处理”区域选择处理方式；无需你修改程序格式。','can_continue':False}
    if any(t['state']=='interrupted' for t in tasks):
        return {'state':'interrupted','title':'AI识别等待核对','message':'原请求结果尚未确认，需要先核对原会话；不能直接重复发送。','can_continue':False}
    if any(t['state']=='content_ready' for t in tasks):
        return {'state':'pending_resume','title':'AI识别待继续','message':f"已有回复已恢复并保留，整份识别尚未完成（已缓存{status.get('chunks_completed',0)}/{status.get('chunks_total',0)}块）。接续时复用已完成内容。",'can_continue':True}
    return None

ARTIFACT_CACHE={}
def workflow_view(run_id):
    """Project final completion from existing authority plus matching file bytes."""
    from paths import current
    import zipfile
    if not run_visible(run_id):return None
    run=current().runs_dir/run_id;path=run/'generation_status.json'
    if not path.exists():return None
    data=read(path);result=data.get('result') or {};provider=result.get('provider_metadata',{}).get('provider_name')
    if not provider and (run/'brief.json').exists():provider=read(run/'brief.json').get('provider_name')
    if provider not in PROVIDERS:return None
    if data.get('status')=='running':return {'state':'running','label':'原生成流程正在执行','artifact_verified':False}
    pc=result.get('ppt_completion') or {}
    if pc.get('complete') is False:return {'state':'failed','label':f"生成尚未完成：正文{pc.get('completed_body_pages',0)}/{pc.get('planned_body_pages',0)}页，部分稿已保留",'artifact_verified':False}
    if data.get('status') not in ('generation_completed','completed'):return {'state':'failed','label':'原生成流程尚未完成，请查看任务记录。','artifact_verified':False}
    delivery=result.get('delivery_status')
    if delivery not in ('COMPLETED_CLEAN','COMPLETED_WITH_WARNINGS'):return {'state':'needs_review','label':'文件已生成，最终检查仍有待处理事项。','artifact_verified':False}
    name=result.get('artifact_name') or ''
    if Path(name).name!=name or Path(name).suffix.lower() not in ('.docx','.pptx'):return {'state':'checking_artifacts','label':'生成已结束，产物信息待核对。','artifact_verified':False}
    file=next((p for p in [run/name,run/'artifact'/name,current().output_root/run_id/name] if p.is_file()),None)
    expected=result.get('artifact_sha256')
    for q in [run/'final_artifact_qa.json',run/'artifact_qa_report.json']:
        if not expected and q.exists():
            qa=read(q);expected=qa.get('sha256') or (qa.get('final_pptx') or {}).get('artifact_sha256')
    if not file or not expected:return {'state':'checking_artifacts','label':'产物尚缺少可核对的文件或摘要。','artifact_verified':False}
    stamp=(str(file),file.stat().st_mtime_ns,file.stat().st_size,expected)
    if stamp not in ARTIFACT_CACHE:
        try:
            with zipfile.ZipFile(file) as z:valid=z.testzip() is None and {'[Content_Types].xml','word/document.xml' if file.suffix.lower()=='.docx' else 'ppt/presentation.xml'}<=set(z.namelist())
            ARTIFACT_CACHE[stamp]=valid and hashlib.sha256(file.read_bytes()).hexdigest()==expected
        except (OSError,zipfile.BadZipFile):ARTIFACT_CACHE[stamp]=False
    ok=ARTIFACT_CACHE[stamp]
    return {'state':'completed' if ok else 'failed','label':'已完成，产物检查通过' if ok else '产物与检查记录不一致，请核对文件。','artifact_verified':ok}
