"""Allow one explicit retry for a returned ZCode recognition identity mismatch."""
import hashlib
import json
import re

import paths
from account_delivery import read
from account_tasks import task_key
from providers.base import ProviderError
from .zcode_segments import MAX_CALLS

PROVIDER = 'zcode_cn'
MISMATCH = 'ZCODE_RECOGNITION_ID_MISMATCH'
MODE = 'tender_requirement_recognition'
RETRY_SUFFIX = '-identity-retry'


def _locations(run_id, slot):
    runtime = paths.current().runtime_dir
    root = runtime / 'zcode_cn_account'
    jobs = root / 'jobs' / hashlib.sha256(run_id.encode()).hexdigest()[:24]
    key = task_key(PROVIDER, {'run_id': run_id, 'task_id': slot}, root)
    return root, jobs, runtime / 'account_agents' / key


def _receipt_proof(run_id, slot, chunk_id):
    root, jobs, job = _locations(run_id, slot)
    state = read(job / 'state.json')
    original = read(job / 'request.json')
    receipt = read(job / 'receipt-0.json')
    key = hashlib.sha256(slot.encode()).hexdigest()[:24]
    dispatch = read(jobs / f'{key}.json')
    native_receipt = read(jobs / f'{key}.answer.json')
    if not (state.get('error_code') == MISMATCH and state.get('state') == 'needs_attention'
            and state.get('turn') == 1 and state.get('provider') == PROVIDER
            and (original.get('run_id'), original.get('task_id'), original.get('chunk_id'),
                 original.get('generation_mode')) == (run_id, slot, chunk_id, MODE)
            and not (original.get('request_id') or original.get('job_id'))
            and state.get('digest') == _digest(original)
            and dispatch.get('status') == 'returned' and dispatch.get('budget_scope') == 'recognition'
            and (dispatch.get('run_id'), dispatch.get('task_id')) == (run_id, slot)
            and receipt and receipt == native_receipt and receipt.get('digest') == dispatch.get('digest')
            and isinstance(receipt.get('body'), str)):
        return None
    try:
        model = (jobs / '.model').read_text(encoding='utf-8')
    except OSError:
        return None
    if not model or model != state.get('account_model') or model.rsplit('/', 1)[-1] != receipt.get('requested_model'):
        return None
    nonce = _digest([run_id, slot, chunk_id, model, state['digest'], receipt])
    return root, jobs, job, nonce, original, model


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def pending_choice(run, status):
    from .cursor_recognition_recovery import FALLBACK, WAITING

    failed = status.get('failed_chunk') or ''
    if (status.get('local_fallback_confirmed') or (run / 'history_hidden.json').exists()
            or status.get('recognition_outcome') not in {WAITING, FALLBACK}
            or not isinstance(failed, str) or not re.fullmatch(r'CHK-\d{4}', failed)):
        return None
    chunk = run / 'tender/chunks' / failed
    attempts = read(chunk / 'recognition_retry_audit.json').get('attempts') or []
    if not attempts or attempts[-1].get('error_code') != MISMATCH:
        return None
    call = attempts[-1].get('call_dir') or ''
    if not re.fullmatch(r'provider_call(?:_retry_\d{2})?', call):
        return None
    segments = read(chunk / call / 'recognition_segments/plan.json').get('segments') or []
    if not segments or segments[-1].get('status') != 'FAIL':
        return None
    segment = segments[-1]
    chunk_id, slot = segment.get('segment_id') or '', segment.get('task_id') or ''
    if (not re.fullmatch(re.escape(failed) + r'-Z\d{3}', chunk_id)
            or not re.fullmatch(re.escape(f'{run.name}-tender-{failed}') + r'-segment-[a-f0-9]{20}', slot)):
        return None
    proof = _receipt_proof(run.name, slot, chunk_id)
    if not proof:
        return None
    root, jobs, job, nonce, _, _ = proof
    retry = task_key(PROVIDER, {'run_id': run.name, 'task_id': slot + RETRY_SUFFIX}, root)
    retry_started = (root.parent / 'account_agents' / retry / 'state.json').exists()
    used = sum(read(path).get('status') != 'not_dispatched' and read(path).get('budget_scope') in {None, 'recognition'}
               for path in jobs.glob('*.json') if not path.name.endswith('.answer.json'))
    return {'action': 'retry' if not retry_started and used < MAX_CALLS else None, 'nonce': nonce,
            'chunk_id': chunk_id, 'job': job, 'terminal_code': None, 'zcode_identity_mismatch': True}


def apply_approved_retry(payload, connection):
    run_id, slot, chunk_id = (payload.get(key) for key in ('run_id', 'task_id', 'chunk_id'))
    if payload.get('generation_mode') != MODE or not all(isinstance(key, str) for key in (run_id, slot, chunk_id)):
        return payload
    _, _, job = _locations(run_id, slot)
    approval = read(job / 'recognition_choice.json')
    if not approval:
        return payload
    proof = _receipt_proof(run_id, slot, chunk_id)
    if (not proof or approval != {'action': 'retry', 'nonce': proof[3], 'chunk_id': chunk_id}
            or payload != proof[4] or connection.selection.get('model') != proof[5]):
        raise ProviderError('ZCode 原子块或模型与接续授权不一致，未发送请求。', error_code='REQUEST_IDENTITY_MISMATCH')
    instruction = (f'本次结果的顶层 chunk_id 必须逐字为 "{chunk_id}"。'
                   'PAR、TBL 等编号只用于引用来源，不能用来构造 chunk_id。')
    return {**payload, 'task_id': slot + RETRY_SUFFIX,
            'system_prompt': str(payload.get('system_prompt') or '') + '\n' + instruction}
