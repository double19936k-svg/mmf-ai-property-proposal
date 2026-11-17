"""Inspect interrupted Cursor Word requests and settle only proven legacy rows."""
import json
import os
from datetime import datetime

import paths
from account_delivery import read
from longform import request_ledger
from providers.cursor_account import is_cursor_provider, slot_key, terminal_word_interruption

LEGACY_BLOCK = '上一条 Cursor 请求结果待核对，已暂停后续请求。'


def inspect(run):
    brief = read(run / 'brief.json')
    provider = brief.get('provider_name')
    if brief.get('medium') != 'WORD' or not is_cursor_provider(provider):
        return None
    if (run / 'history_hidden.json').exists():
        return None
    root = paths.current().runtime_dir / 'cursor_account' / slot_key(run.name)
    model = 'composer-2.5' if provider == 'cursor_account' else provider.removeprefix('cursor_').replace('_', '-', 1).replace('_', '.')
    jobs = {path.parent.name: path.parent for path in root.glob('*/dispatch.json')
            if read(path).get('scope') == 'generation'}
    if not jobs:
        return None
    interrupted = {}
    unknown = False
    for key, job in jobs.items():
        if read(job / 'dispatch.json').get('model') != model:
            return None
        if (job / 'reply.json').is_file():
            continue
        terminal = terminal_word_interruption(job, model)
        if terminal:
            interrupted[key] = terminal['error_code']
        else:
            unknown = True
    if not interrupted and not unknown:
        return _budget_not_dispatched(run, root, jobs, model, provider)

    integrity = {}
    ledger_path = run / request_ledger.LEDGER_FILENAME
    events = request_ledger.read_events(ledger_path, integrity)
    replies_unknown = {row['request_id']: row for row in events
                       if row.get('status') == request_ledger.RESPONSE_UNKNOWN and row.get('request_id')}
    settlements = []
    for event in request_ledger.open_requests(events):
        request_id = event['request_id']
        row = replies_unknown.get(request_id, {})
        if (row.get('provider') != provider or row.get('run_id') != run.name
                or not request_id.startswith(f'REQ-{run.name}-')
                or row.get('error_code') != 'CURSOR_RESPONSE_UNKNOWN'
                or event.get('status') not in {request_ledger.RESPONSE_UNKNOWN, request_ledger.RESEND_CONTROLLED}):
            unknown = True
            continue
        key = slot_key(request_id)
        if key in interrupted:
            code, resolution = 'CURSOR_WORD_STREAM_INTERRUPTED', 'definitive_failure'
        elif row.get('error') == LEGACY_BLOCK and not (root / key / 'dispatch.json').exists():
            code, resolution = 'CURSOR_WORD_PREVIOUS_UNKNOWN', 'not_dispatched'
        else:
            unknown = True
            continue
        settlements.append({**row, 'status': request_ledger.RESPONSE_REJECTED, 'error_code': code,
                            'resolution': resolution, 'reconciled_from': request_ledger.RESPONSE_UNKNOWN})
    can_resume = bool(interrupted and not unknown and integrity.get('ledger_readable')
                      and not integrity.get('malformed_line_count') and (run / 'generation_input.json').is_file()
                      and read(run / 'generation_status.json').get('status') == 'failed')
    policy = read(root / '.word_policy')
    if policy:
        cap = policy.get('call_cap')
        can_resume = (can_resume and policy.get('model') == model and policy.get('run_id') == run.name
                      and type(cap) is int and len(jobs) < cap)
    return {'interrupted': interrupted, 'can_resume': can_resume, 'settlements': settlements,
            'path': ledger_path, 'sent': len(jobs), 'returned': sum((job / 'reply.json').is_file() for job in jobs.values())}


def settle(snapshot):
    """Append corrections under the server's per-run lock immediately before resume."""
    if not snapshot['can_resume'] or not snapshot['settlements']:
        return
    path = snapshot['path']
    timestamp = datetime.now().astimezone().isoformat(timespec='seconds')
    rows = [json.dumps({**row, 'event_at': timestamp}, ensure_ascii=False) for row in snapshot['settlements']]
    with path.open('a', encoding='utf-8') as handle:
        handle.write('\n' + '\n'.join(rows) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def _budget_not_dispatched(run, root, jobs, model, provider):
    """Resume only when every later refusal is a confirmed budget non-dispatch.

    A saved writing policy is read and left unchanged. The dispatch count is
    the number of bridge jobs already on disk, so a later call cannot start
    again from zero.
    """
    from account_budget import WORD_CALL_CAPS
    from longform import request_ledger

    integrity = {}
    events = request_ledger.read_events(run / request_ledger.LEDGER_FILENAME, integrity)
    blocked = {'interrupted': {}, 'can_resume': False, 'settlements': [], 'reason': 'budget_not_dispatched',
               'path': run / request_ledger.LEDGER_FILENAME, 'sent': len(jobs),
               'returned': sum((job / 'reply.json').is_file() for job in jobs.values())}
    if not integrity.get('ledger_readable') or integrity.get('malformed_line_count'):
        return blocked
    latest = request_ledger.latest_status(events)
    last = {}
    for row in events:
        request_id = str(row.get('request_id') or '')
        if request_id:
            last[request_id] = row
    budget_rows = []
    for request_id, status in latest.items():
        row = last.get(request_id) or {}
        if status in request_ledger.TERMINAL_STATES and not (
                row.get('error_code') == 'CURSOR_RUN_BUDGET' and row.get('resolution') == 'not_dispatched'):
            continue
        if row.get('error_code') != 'CURSOR_RUN_BUDGET' or row.get('resolution') != 'not_dispatched':
            return blocked
        if (row.get('provider') != provider or row.get('run_id') != run.name
                or not request_id.startswith(f'REQ-{run.name}-')):
            return blocked
        if (root / slot_key(request_id) / 'dispatch.json').is_file():
            return blocked
        budget_rows.append(row)
    if not budget_rows:
        return None
    if any(read(job / 'dispatch.json').get('model') != model for job in jobs.values()):
        return blocked
    brief = read(run / 'brief.json')
    policy = read(root / '.word_policy')
    if policy:
        cap = policy.get('call_cap')
        if (policy.get('model') != model or policy.get('run_id') != run.name or type(cap) is not int):
            return blocked
    else:
        cap = WORD_CALL_CAPS.get(brief.get('document_scale'), WORD_CALL_CAPS['regular_solution'])
    reusable = (run / 'generation_input.json').is_file() and read(run / 'generation_status.json').get('status') == 'failed'
    blocked['can_resume'] = bool(reusable and len(jobs) < cap)
    blocked['call_cap'] = cap
    blocked['dispatched'] = len(jobs)
    return blocked


def public_issue(snapshot):
    if snapshot.get('reason') == 'budget_not_dispatched':
        if not snapshot.get('can_resume'):
            return "写稿曾因预算判断未派发，但派发记录、模型或预算政策无法核对，已保留拦截。"
        return (f"写稿曾因预算判断未派发。已核对 {snapshot['sent']} 次派发和 {snapshot['returned']} 份回执。"
                "接续使用原模型处理缺失正文，可能消耗账户额度。")
    if snapshot['interrupted']:
        reason = ('Cursor 写稿连接停滞，已确认中断。'
                  if 'connection_stalled' in snapshot['interrupted'].values() else 'Cursor 写稿请求已结束，未返回完整结果。')
    else:
        reason = 'Cursor 有一条写稿请求的结果仍待核对。'
    return f"{reason} 本轮实际派发 {snapshot['sent']} 次写稿，收到 {snapshot['returned']} 份完整回执。"
