"""User decisions on Cursor recognition validation and confirmed stream interruptions."""
import hashlib
import json
import re
from pathlib import Path

from account_delivery import read
from providers.base import write_json
from providers.cursor_account import (
    RECOGNITION_LIMIT, RECOGNITION_TERMINAL_ERRORS, is_cursor_provider, slot_key,
    terminal_recognition_interruption,
)
from providers.zcode_recognition import ARRAY_KEYS
import paths

WAITING = 'AI_RECOGNITION_AWAITING_DECISION'
FALLBACK = 'AI_RECOGNITION_FAILED_WITH_LOCAL_FALLBACK'
VALIDATION_ERRORS = {
    'CURSOR_RECOGNITION_FORMAT', 'CURSOR_RECOGNITION_SCHEMA', 'CURSOR_RECOGNITION_ID_MISMATCH',
}
INTERRUPTION_ERRORS = {code for code, _ in RECOGNITION_TERMINAL_ERRORS.values()}
CHOICE_ERRORS = VALIDATION_ERRORS | INTERRUPTION_ERRORS


def repair_reply(raw, chunk_id):
    """Remove only a short punctuation tail after one complete, matching JSON object."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result

    if not isinstance(raw, str):
        return None
    text = raw.strip().lstrip('\ufeff')
    try:
        doc, end = json.JSONDecoder(object_pairs_hook=unique).raw_decode(text)
    except (ValueError, RecursionError):
        return None
    tail = text[end:]
    if not (0 < len(tail) <= 16 and re.fullmatch(r'[\s,\]}]+', tail)
            and any(char in tail for char in ']}') and isinstance(doc, dict)
            and doc.get('chunk_id') == chunk_id
            and all(isinstance(doc.get(key), list) for key in ARRAY_KEYS)):
        return None
    return text[:end], len(tail)


def retry_key(slot):
    return slot_key(str(slot) + '-recognition-user-retry')


def decision_token(job, model):
    marker = read(job / 'dispatch.json')
    if not (marker.get('scope') == 'recognition' and marker.get('model') == model and marker.get('digest')):
        return None
    receipt = read(job / 'reply.json')
    raw = receipt.get('result')
    terminal_code = None
    if (receipt.get('state') == 'finished'
            and receipt.get('requested_model') == receipt.get('observed_model') == model
            and receipt.get('run_id') and isinstance(raw, str)):
        identity = [receipt['run_id'], raw]
    elif not (job / 'reply.json').exists() and (terminal := terminal_recognition_interruption(job, model)):
        raw = None
        terminal_code = terminal['error_code']
        identity = [terminal['run_id'], None, terminal_code]
    else:
        return None
    material = json.dumps([job.name, marker['digest'], *identity], ensure_ascii=False)
    return hashlib.sha256(material.encode('utf-8')).hexdigest(), raw, terminal_code


def approved_action(job, model, chunk_id):
    approval = read(job / 'recognition_choice.json')
    if approval.get('chunk_id') != chunk_id:
        return None
    token = decision_token(job, model)
    action = approval.get('action')
    if token and token[0] == approval.get('nonce') and (action == 'retry' or (action == 'repair' and token[1] is not None)):
        return action
    return None


def pending_choice(run_dir: Path, status):
    provider = status.get('provider_name')
    if provider == 'zcode_cn':
        from .zcode_identity_recovery import pending_choice as pending_zcode_choice
        return pending_zcode_choice(run_dir, status)
    failed = status.get('failed_chunk')
    if (not is_cursor_provider(provider) or status.get('local_fallback_confirmed')
            or status.get('recognition_outcome') not in {WAITING, FALLBACK}
            or not isinstance(failed, str) or not re.fullmatch(r'CHK-\d{4}', failed)
            or (run_dir / 'history_hidden.json').exists()):
        return None
    chunk_dir = run_dir / 'tender/chunks' / failed
    attempts = read(chunk_dir / 'recognition_retry_audit.json').get('attempts') or []
    error_code = attempts[-1].get('error_code') if attempts else None
    if error_code not in CHOICE_ERRORS | {'CURSOR_RESPONSE_UNKNOWN'}:
        return None
    call_dir = attempts[-1].get('call_dir') or ''
    if not re.fullmatch(r'provider_call(?:_retry_\d{2})?', call_dir):
        return None
    slot = f'{run_dir.name}-tender-{failed}'
    segment_plan = read(chunk_dir / call_dir / 'recognition_segments/plan.json')
    chunk_id = failed
    if segment_plan:
        rows = segment_plan.get('segments') or []
        if not rows or rows[-1].get('status') != 'FAIL':
            return None
        chunk_id = rows[-1].get('segment_id') or ''
        task_id = rows[-1].get('task_id') or ''
        if (not re.fullmatch(re.escape(failed) + r'-Z\d{3}', chunk_id)
                or not re.fullmatch(re.escape(slot) + r'-segment-[a-f0-9]{20}', task_id)):
            return None
        slot = task_id
    root = paths.current().runtime_dir / 'cursor_account' / slot_key(run_dir.name)
    job = root / slot_key(slot)
    model = 'composer-2.5' if provider == 'cursor_account' else provider.removeprefix('cursor_').replace('_', '-', 1).replace('_', '.')
    token = decision_token(job, model)
    if not token:
        return None
    nonce, raw, terminal_code = token
    retry = root / retry_key(slot)
    retry_terminal = terminal_recognition_interruption(retry, model) if not (retry / 'reply.json').exists() else None
    if retry_terminal:
        terminal_code = retry_terminal['error_code']
    if bool(terminal_code) != (error_code in INTERRUPTION_ERRORS | {'CURSOR_RESPONSE_UNKNOWN'}):
        return None
    action = None
    if not (retry / 'dispatch.json').exists():
        if repair_reply(raw, chunk_id):
            action = 'repair'
        elif sum(read(path).get('scope') == 'recognition' for path in root.glob('*/dispatch.json')) < RECOGNITION_LIMIT:
            action = 'retry'
    return {'action': action, 'nonce': nonce, 'chunk_id': chunk_id, 'job': job, 'terminal_code': terminal_code}


def authorize_choice(choice, action, nonce):
    if action not in {'repair', 'retry'} or action != choice['action'] or nonce != choice['nonce']:
        return False
    write_json(choice['job'] / 'recognition_choice.json', {
        'action': action, 'nonce': nonce, 'chunk_id': choice['chunk_id'],
    })
    return True
