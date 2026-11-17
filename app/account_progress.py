"""One workflow progress projection for account connectors, with receipt-based usage."""
import hashlib, math, re
from collections import Counter
from account_delivery import read
from providers.cursor_account import bridge_problem, confirmed_not_sent, dispatch_scope, is_cursor_provider, pending_bridge_progress
import paths

_usage_cache={}


def recognition_work(run, status):
    """Count validated recognition units without treating receipts or retries as progress."""
    parent_total = status.get('chunks_total') or 0
    parent_done = status.get('chunks_completed') or 0
    if not (isinstance(parent_total, int) and isinstance(parent_done, int)
            and 0 <= parent_done <= parent_total and parent_total):
        return None

    part = status.get('recognition_segments') or {}
    part_done, part_total = part.get('completed'), part.get('total')
    if (part.get('parent_chunk_id') != f'CHK-{parent_done + 1:04d}'
            or not isinstance(part_done, int) or not isinstance(part_total, int)
            or not 0 <= part_done <= part_total or not part_total):
        part_done = part_total = 0

    provider = status.get('provider_name')
    key = 'cursor' if is_cursor_provider(provider) else 'zcode' if provider == 'zcode_cn' else None
    if key:
        plan = status.get(f'{key}_request_plan') or read(run / f'tender/{key}_recognition_plan.json')
        if plan.get('provider') == provider:
            sizes = {row.get('chunk_id'): row.get('requests') for row in plan.get('chunks') or []
                     if isinstance(row, dict) and isinstance(row.get('chunk_id'), str)}
            for index in range(1, parent_total + 1):
                chunk_id = f'CHK-{index:04d}'
                if chunk_id not in sizes:
                    saved = read(run / 'tender/chunks' / chunk_id / 'normalized.json')
                    metadata = saved.get('provider_metadata') or {}
                    if saved.get('chunk_id') == chunk_id and metadata.get('provider_name') == provider:
                        segments = metadata.get('recognition_segments')
                        sizes[chunk_id] = segments.get('segments_total') if segments else 1
            ids = [f'CHK-{i:04d}' for i in range(1, parent_total + 1)]
            if all(isinstance(sizes.get(chunk_id), int) and sizes[chunk_id] > 0 for chunk_id in ids):
                done = sum(sizes[chunk_id] for chunk_id in ids[:parent_done])
                if part_total and sizes.get(part['parent_chunk_id']) == part_total:
                    done += part_done
                return {'done':done, 'total':sum(sizes[chunk_id] for chunk_id in ids), 'basis':'plan'}
    if part_total:
        return {'done':part_done, 'total':part_total, 'basis':'current_chunk'}
    return None


def _reported_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _qoder_tokens(usage):
    # Official Qoder usage reports uncached input, output and two separate cache counts.
    required = [usage.get(field) for field in ("input_tokens", "output_tokens")]
    if not all(_reported_number(n) for n in required):
        return None
    cache = [usage.get(field, 0) for field in ("cache_creation_input_tokens", "cache_read_input_tokens")]
    return sum(required + cache) if all(_reported_number(n) for n in cache) else None


def usage(run_id,provider):
    cursor = is_cursor_provider(provider)
    run_hash = hashlib.sha256(run_id.encode()).hexdigest()
    runtime = paths.current().runtime_dir
    if cursor:
        files = sorted((runtime / 'cursor_account' / run_hash[:24]).glob('*/reply.json'))
    else:
        name='zcode_cn_account' if provider=='zcode_cn' else 'qoder_account_cn'
        width=24 if provider=='zcode_cn' else 16
        files=sorted((runtime/name/'jobs'/run_hash[:width]).glob('*.answer.json'))
    stamp=(run_id,provider,tuple((str(p),p.stat().st_mtime_ns,p.stat().st_size) for p in files))
    if provider != "qoder_account" and stamp in _usage_cache:
        return _usage_cache[stamp]
    returned=[];scopes=Counter();tokens=0;measured=0
    qoder = provider == "qoder_account"
    charges, credits = [], []
    for p in files:
        answer=read(p)
        if cursor:
            ledger = read(p.with_name('dispatch.json'))
            model = ledger.get('model')
            if not model or answer.get('state') != 'finished' or answer.get('observed_model') != model:continue
            scope = dispatch_scope(ledger, p.parent.name, run_id)
        else:
            ledger=read(p.with_name(p.name.replace('.answer.json','.json')))
            if ledger.get('status')!='returned' or not answer.get('digest') or ledger.get('digest')!=answer.get('digest'):continue
            scope=ledger.get('budget_scope')
        if not scope and provider=='qoder_account':
            from qoder_account.connection import is_recognition_job
            scope='recognition' if is_recognition_job(ledger) else 'generation'
        returned.append(answer);scopes[scope or 'generation']+=1
        u=answer.get('usage') or {}
        n=u.get('totalTokens',u.get('total_tokens')) if isinstance(u,dict) else None
        if qoder:
            if n is None and isinstance(u, dict):
                n=_qoder_tokens(u)
            if _reported_number(answer.get("total_cost_usd")):
                charges.append(answer["total_cost_usd"])
            if _reported_number(answer.get("total_credits")):
                credits.append(answer["total_credits"])
        if _reported_number(n):tokens+=int(n);measured+=1
    result={'returned_requests':len(returned),'recognition_requests':scopes['recognition'],'generation_requests':scopes['generation'],
            'total_tokens':tokens if measured else None,'measured_requests':measured,'usage_complete':measured==len(returned) and bool(returned),
            'source':'official_receipts','transport':sorted({str(a.get('transport') or 'NOT_AVAILABLE') for a in returned}),
            'cost':sum(charges) if charges else 'NOT_AVAILABLE',
            'cost_complete':bool(returned) and len(charges)==len(returned),
            'total_credits':sum(credits) if credits else None,
            'credits_complete':bool(returned) and len(credits)==len(returned)}
    if qoder:
        from qoder_account.connection import connection
        ctl=connection()
        with ctl.lock:
            quota=ctl.state.get("usage") or {}
            percent=quota.get("totalUsagePercentage")
            result["account_usage_percent"]=percent if _reported_number(percent) else None
            result["account_quota_exceeded"]=quota.get("isQuotaExceeded") if type(quota.get("isQuotaExceeded")) is bool else None
    result['recommendation_requests'] = scopes['recommendation']
    if len(_usage_cache)>100:_usage_cache.clear()
    _usage_cache[stamp]=result
    return result

def snapshot(run_id,tasks,generation=None,recognition=None,worker_alive=False):
    if not tasks and not generation and not recognition:return None
    provider=(generation or {}).get('provider_name') or (recognition or {}).get('provider_name') or (tasks[0]['provider'] if tasks else '')
    cursor = is_cursor_provider(provider)
    if provider not in {'qoder_account','zcode_cn'} and not cursor:return None
    run=paths.current().runs_dir/run_id
    if not run.is_dir() or (run/'history_hidden.json').exists():return None
    is_gen=bool((run/'generation_status.json').exists())
    bridge = pending_bridge_progress(run_id, 'generation' if is_gen else 'recognition') if cursor else []
    activity = bridge[-1].get('activity') if bridge and worker_alive else None
    cursor_live = {'assistant':'正在接收 Cursor 的模型输出。', 'thinking':'Cursor 正在处理当前请求。'}.get(activity)
    relevant=[t for t in tasks if ('-tender-' in str(t.get('task_id','')))!=is_gen]
    controls={}
    actionable=[t for t in relevant if t.get('nonce') and t.get('question')]
    current=actionable[0] if actionable else next((t for t in relevant if t.get('state') in {'interrupted','needs_attention','failed'}),None)
    u=usage(run_id,provider)
    work = None
    if is_gen:
        g=generation or {};checkpoint=read(run/'checkpoint/state.json');pc=g.get('ppt_completion') or {}
        progress=checkpoint.get('progress') or {};done=pc.get('completed_body_pages',progress.get('done',0));total=pc.get('planned_body_pages',progress.get('total',0))
        phase_record=read(run/'generation_phase.json')
        unit='页正文' if pc else '节正文';phase=phase_record.get('phase') or checkpoint.get('public_phase')
        state='running' if worker_alive else 'completed' if g.get('generated') else 'needs_attention'
        label=g.get('public_message') or '正在同步生成状态'
        if cursor:
            live = read(run/'generation_status.json')
            phase = live.get('phase') or phase
            label = (g.get('progress_message') or live.get('message') or '正在准备生成方案。') if worker_alive else (
                '方案文件已生成。' if state=='completed' else g.get('error') or g.get('status_label') or '生成未完成。')
            if not worker_alive and state != 'completed' and g.get('execution_issues'):
                label = g['execution_issues'][0]['label']
            if cursor_live:
                label = cursor_live
        if worker_alive and phase=='LOCAL_REVIEW_REPAIR':
            label=phase_record['message']
        if worker_alive:
            repairing=next((task for task in relevant if task.get('automatically_repairing_format')),None)
            if repairing:
                label=repairing['message']
        stage='内容生成' if phase in {'GENERATING','TECHNICAL_RETRYING',None} else '检查与装配'
        if state!='running':stage='文件就绪' if state=='completed' else '生成未完成'
        budget=next((t for t in relevant if t.get('error_code') in {'ZCODE_RUN_BUDGET','QODER_BUDGET_EXHAUSTED'}),None)
        if budget:label=f"写稿已收到{u['generation_requests']}次回复，原调用预算已用尽；"+('正在保存已完成内容。' if worker_alive else '已完成内容保留，未自动增加请求。')
    else:
        r=recognition or {};done=r.get('chunks_completed',0);total=r.get('chunks_total',0);unit='个文件分块';stage='需求识别'
        work = recognition_work(run, r)
        from tender_intake.cursor_recognition_recovery import WAITING, pending_choice
        choice = pending_choice(run, r) if cursor or provider == 'zcode_cn' else None
        waiting = bool(choice or (cursor and r.get('recognition_outcome') == WAITING))
        success=not waiting and (r.get('recognition_outcome')=='AI_RECOGNITION_SUCCESS' or r.get('pack_status') in {'ready_for_plan','confirmed'})
        worker_alive=worker_alive or any(t['state'] in {'running','resuming'} for t in relevant)
        fallback=waiting or r.get('recognition_outcome')=='AI_RECOGNITION_FAILED_WITH_LOCAL_FALLBACK'
        state='running' if worker_alive else 'completed' if success else 'needs_attention' if fallback or relevant or r.get('error') else 'idle'
        label='需求识别已完成，等待确认需求。' if success else '正在识别需求。' if worker_alive else r.get('provider_error') or r.get('error') or '识别待处理，已有内容保留。'
        part=r.get('recognition_segments') or {}
        if worker_alive and part.get('total'):label+=f"当前分块第{part.get('current',0)}/{part['total']}个子块。"
        failed=r.get('failed_chunk')
        if cursor and not success:
            if worker_alive and cursor_live:
                label += cursor_live
            elif fallback and not worker_alive and isinstance(failed, str) and re.fullmatch(r'CHK-[0-9]{4}', failed):
                retry = read(run/'tender/chunks'/failed/'recognition_retry_audit.json')
                first = next((row for row in retry.get('attempts', []) if row.get('error_code') == 'CURSOR_RESPONSE_UNKNOWN'), {})
                if first.get('error'):
                    label = first['error']
                if confirmed_not_sent(bridge):
                    label = bridge_problem(bridge)
                    controls['retry_unsent'] = not r.get('local_fallback_confirmed')
                if part.get('total'):
                    label += f" 当前分块已校验{part.get('completed',0)}/{part['total']}个子块。"
                controls['recheck'] = any(row.get('run_id') for row in bridge)
        controls['accept_local']=bool(fallback and not r.get('local_fallback_confirmed') and not worker_alive)
        if fallback and r.get('local_fallback_confirmed') and not worker_alive:
            label='已确认使用本地解析结果，可继续确认需求。'
        controls['retry']=bool(fallback and not relevant and not worker_alive and not cursor)
        if provider=='qoder_account' and fallback and not worker_alive and isinstance(failed,str) and re.fullmatch(r'CHK-[0-9]{4}', failed):
            from tender_intake.qoder_recognition_recovery import approved_reply_split
            call=run/'tender/chunks'/failed/'provider_call'
            controls['split']=approved_reply_split(call,f'{run_id}-tender-{failed}')
        if waiting and not worker_alive:
            state = 'waiting_for_user'
            action = choice['action'] if choice else None
            if choice and choice.get('zcode_identity_mismatch'):
                label = 'ZCode 返回的子块编号与原请求不符，原答已隔离。'
                label += '可重试当前子块。' if action else '可继续使用本地解析。'
            elif choice and choice['terminal_code']:
                from providers.cursor_account import RECOGNITION_TERMINAL_ERRORS
                _, message = RECOGNITION_TERMINAL_ERRORS[choice['terminal_code']]
                label = message + ('请先选择处理方式。' if action else '可选择继续使用本地解析。')
            elif action == 'repair':
                label = '当前子块的回复已收到，末尾有多余标点。请先选择处理方式。'
            elif action == 'retry':
                label = '当前子块的回复未通过校验。请先选择处理方式。'
            else:
                label = '当前子块仍未通过校验，可选择继续使用本地解析。'
            if part.get('total'):
                label += f" 当前分块已校验{part.get('completed',0)}/{part['total']}个子块。"
            controls.pop('recheck', None)
            controls.pop('retry_unsent', None)
            if action:
                controls['choice'] = {'action': action, 'nonce': choice['nonce']}
    if actionable and (is_gen or not worker_alive or any(t['state']=='waiting_for_user' for t in actionable)):
        state='waiting_for_user';label='等待你的重要决定，回复后继续原会话。'
    elif not worker_alive and current and state not in {'completed','waiting_for_user'} and not (is_gen and budget):label=current.get('message') or label
    if not worker_alive and state!='completed' and current is None:
        current=next((t for t in relevant if t.get('state')=='content_ready'),None)
        if current:label='已有完整回复已保存，可接续原任务；已完成内容直接复用。'
    if controls.get('split') and not actionable and current and current.get('error_code')=='QODER_RECOGNITION_FORMAT':
        label=f'第{int(failed.split("-")[1])}块的回复没有返回完整。已完成的分块已保留，可以拆小后接续识别。'
    else:
        controls.pop('split',None)
    # A successful child reply is only a record, never a separate completion card.
    return {'run_id':run_id,'provider':provider,'stage':stage,'state':state,'message':label,
            'done':max(0,int(done or 0)),'total':max(0,int(total or 0)),'unit':unit,
            'recognition_work':work,
            'indeterminate':state=='running' and (not total or (is_gen and stage=='检查与装配')),
            'pending_count':len(actionable),'task':current if state!='completed' and (is_gen or state not in {'running','idle'}) else None,
            'recognition_controls':controls,
            'records':[{k:t.get(k) for k in ['key','task_id','state','label','error_code','turn','session_id']} for t in relevant[:10]],
            'record_count':len(relevant),'usage':u}
