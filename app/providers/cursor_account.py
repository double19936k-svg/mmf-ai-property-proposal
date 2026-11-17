"""Opt-in, text-only Cursor SDK provider with a local dispatch ledger."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .base import AIProvider, ProviderError, normalize_generation, normalize_recommendation, parse_provider_payload, write_json

MODEL = 'composer-2.5'
SERIAL = threading.Lock()
NAME = 'cursor_account'
WORD_PREVIOUS_UNKNOWN = 'CURSOR_WORD_PREVIOUS_UNKNOWN'
WORD_PREVIOUS_UNKNOWN_MESSAGE = '上一条 Cursor 请求结果待核对，本批次尚未发送。'
ACTIVE_LIMIT_SECONDS = 600
IDLE_LIMIT_SECONDS = 150
RECOGNITION_LIMIT = 48
RECOGNITION_RESET = 'CURSOR_RECOGNITION_CONNECTION_RESET'
RECOGNITION_STALLED = 'CURSOR_RECOGNITION_CONNECTION_STALLED'
RESET_MESSAGE = 'Cursor 在接收模型输出时连接被重置，原请求已结束且未返回完整结果。'
RECOGNITION_TERMINAL_ERRORS = {
    'ECONNRESET': (RECOGNITION_RESET, RESET_MESSAGE),
    'connection_stalled': (RECOGNITION_STALLED, 'Cursor SDK 判定输出连接停滞，原请求已结束且未返回完整结果。'),
}


class CursorRecognitionInterrupted(ProviderError):
    def __init__(self, evidence):
        code, message = RECOGNITION_TERMINAL_ERRORS[evidence['error_code']]
        super().__init__(message, error_code=code)
        self.evidence = evidence


class CursorWordStreamInterrupted(ProviderError):
    def __init__(self, evidence):
        message = ('Cursor 写稿连接停滞，原请求已结束且未返回完整结果。'
                   if evidence['error_code'] == 'connection_stalled'
                   else 'Cursor 已结束本次写稿请求，未返回完整结果。')
        super().__init__(message,
                         error_code='CURSOR_WORD_STREAM_INTERRUPTED')
        self.evidence = evidence


def is_cursor_provider(name):
    return name == NAME or (isinstance(name, str) and name.startswith('cursor_grok_'))


def slot_key(slot):
    return hashlib.sha256(str(slot).encode()).hexdigest()[:24]


def dispatch_scope(marker, key, run_id):
    scope = marker.get('scope')
    # Older releases counted the optional recommendation as a writing request.
    if scope == 'generation' and key == slot_key(f'{run_id}-recommend'):
        return 'recommendation'
    return scope


def runtime():
    root = os.environ.get('MMF_CURSOR_BRIDGE_ROOT')
    return Path(root).resolve() if root else None


def read_catalog(root):
    try:
        catalog = json.loads((root / 'catalog.json').read_text(encoding='utf-8-sig'))
        return catalog if isinstance(catalog, dict) else {}
    except (OSError, ValueError):
        return {}


def cursor_provider_rows():
    root = runtime()
    if root is None:
        return []
    models = {MODEL: (NAME, 'Composer 2.5')}
    for row in read_catalog(root).get('models', []):
        model = row.get('id', '') if isinstance(row, dict) else ''
        if isinstance(model, str) and re.fullmatch(r'grok-[0-9]+(?:\.[0-9]+)+', model):
            name = model.replace('-', '_').replace('.', '_')
            models[model] = ('cursor_' + name, str(row.get('displayName') or model))
    return [{
        'provider_name': name, 'provider_type': NAME, 'display_name': f'Cursor 账户 · {label}（试用）',
        'enabled': True, 'model': model, 'timeout': 270, 'qualification_status': 'experimental',
        'endpoint_alias': 'cursor_official_local_sdk',
        'billing_note': '使用已授权的 Cursor 账户。额度与金额以 Cursor 账户用量页面为准。',
    } for model, (name, label) in models.items()]


def bridge_health(model=MODEL):
    root = runtime()
    if not root or not (root / 'cursor_bridge.mjs').is_file() or not shutil.which('node'):
        return False, '未找到 Cursor 桥接运行环境。'
    try:
        state = read_catalog(root)
        expires = state.get('expires_at_ms')
        ready = (state.get('state') == 'ready' and any(isinstance(row, dict) and row.get('id') == model for row in state.get('models', []))
                 and (expires is None or expires > time.time() * 1000))
    except (OSError, ValueError, TypeError):
        ready = False
    return ready, ('Cursor 授权已验证，可使用所选模型。' if ready
                   else 'Cursor 授权或模型清单需要重新检查。')


def bridge_events(path, *, strict=False, offset=0):
    try:
        lines = path.read_bytes()[offset:].decode('utf-8').splitlines()
    except (OSError, UnicodeError):
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            if strict:
                return []
            continue
        if isinstance(event, dict):
            events.append(event)
        elif strict:
            return []
    return events


def confirmed_not_sent(events):
    if not events or any(row.get('run_id') or row.get('state') == 'sending' for row in events):
        return False
    last = events[-1]
    if last.get('state') != 'error':
        return False
    if last.get('not_sent') is True:
        return True
    # The earlier packaged worker flushed an error and always recorded 'sending'
    # before agent.send(). A sole, complete legacy error is a pre-send failure.
    return (len(events) == 1 and set(last) == {'state', 'error_code', 'at'}
            and isinstance(last.get('at'), (int, float)) and isinstance(last.get('error_code'), str))


def is_terminal_reset(row):
    return (row.get('state') == 'incomplete' and row.get('terminal_status') == 'error'
            and row.get('error_code') == 'ECONNRESET')


def _is_recognition_interruption(row):
    return (row.get('state') == 'incomplete' and row.get('terminal_status') == 'error'
            and row.get('error_code') in RECOGNITION_TERMINAL_ERRORS)


def terminal_recognition_interruption(job, model):
    """Bind a confirmed recognition interruption to the original SDK run."""
    return _terminal_stream_error(job, model, _is_recognition_interruption)


def _is_word_stream_interruption(row):
    return _is_recognition_interruption(row) or (
        row.get('state') == 'incomplete' and row.get('terminal_status') == 'error'
        and row.get('error_code') == 'STREAM_ABORTED' and row.get('reason') == 'stream_aborted'
    )


def terminal_word_interruption(job, model):
    return _terminal_stream_error(job, model, _is_word_stream_interruption)


def _terminal_stream_error(job, model, matches):
    events = bridge_events(job / 'bridge.jsonl', strict=True)
    origin = next((row for row in reversed(events) if row.get('state') == 'running'), None)
    if not origin or origin.get('requested_model') != model:
        return None
    keys = ('agent_id', 'run_id', 'workspace', 'requested_model')
    terminal = next((row for row in reversed(events)
                     if row.get('state') in {'finished', 'incomplete'}
                     and all(row.get(key) and row[key] == origin.get(key) for key in keys)), None)
    if terminal and terminal['state'] == 'finished':
        return None
    if terminal and matches(terminal):
        return terminal
    try:
        checked = json.loads((job / 'terminal_error.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if (isinstance(checked, dict) and matches(checked)
            and all(checked.get(key) and checked[key] == origin.get(key) for key in keys)):
        return checked
    return None


def bridge_problem(events):
    reason = next((row['reason'] for row in reversed(events) if row.get('reason')), None)
    if reason == 'idle_timeout':
        return 'Cursor 长时间没有新的模型输出，已停止等待，原请求结果待核对。'
    if reason == 'hard_timeout':
        return 'Cursor 在单次执行时限内未返回完整结果，原请求结果待核对。'
    last = events[-1] if events else {}
    if is_terminal_reset(last):
        return RESET_MESSAGE
    if confirmed_not_sent(events):
        stage = {'auth': '检查登录', 'catalog': '读取模型清单', 'agent_create': '建立会话'}.get(last.get('stage'), '准备连接')
        code = last.get('error_code')
        detail = '。请重新检查 Cursor 登录' if code == 'LOGIN_REQUIRED' else ''
        return f'Cursor 在{stage}时出错，此子块未发送，可从原进度接续{detail}。'
    if last.get('state') == 'error' and last.get('error_code'):
        return f"Cursor 桥接报告错误（{last['error_code']}），原请求记录已保留。"
    if last.get('terminal_status') == 'error' and last.get('error_code'):
        return f"Cursor SDK 报告执行错误（{last['error_code']}），原请求记录已保留。"
    if last.get('terminal_status') == 'cancelled':
        return 'Cursor SDK 已结束此轮执行，原请求记录已保留。'
    return 'Cursor 未返回可核验的完整结果，原请求结果待核对。'


def pending_bridge_progress(run_id, scope):
    import paths

    run_root = paths.current().runtime_dir / NAME / slot_key(run_id)
    candidates = []
    for path in run_root.glob('*/bridge.jsonl'):
        if path.with_name('reply.json').is_file():
            continue
        try:
            marker = json.loads(path.with_name('dispatch.json').read_text(encoding='utf-8'))
            if dispatch_scope(marker, path.parent.name, run_id) == scope:
                candidates.append((path.stat().st_mtime_ns, path))
        except (OSError, ValueError):
            continue
    return bridge_events(max(candidates)[1], strict=True) if candidates else []


def run_bridge(root, request, run_id):
    from .openai_compatible import is_cancelled
    operation = request.get('operation')
    started = time.monotonic()
    limit = 20 if operation == 'recheck' else request['limit_seconds'] + 115
    journal = Path(request['journal']) if request.get('journal') else None
    offset = journal.stat().st_size if journal and journal.is_file() else 0
    process = subprocess.Popen(
        [shutil.which('node'), str(Path(__file__).with_name('cursor_bridge.mjs'))],
        cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    payload = json.dumps(request, ensure_ascii=False).encode('utf-8')
    timed_out = False
    try:
        while True:
            try:
                output, _ = process.communicate(payload, timeout=1)
                break
            except subprocess.TimeoutExpired:
                payload = None
                if is_cancelled(run_id):
                    raise ProviderError('Cursor 任务已停止，原请求结果待核对。', error_code='CURSOR_RESPONSE_UNKNOWN')
                elapsed = time.monotonic() - started
                events = bridge_events(journal, offset=offset) if journal else []
                no_run = elapsed >= 90 and not any(row.get('run_id') for row in events)
                terminal_at = next((row.get('at') for row in reversed(events)
                                    if row.get('state') in {'finished', 'incomplete', 'error'}), None)
                if elapsed >= limit or no_run or (terminal_at and time.time() - terminal_at >= 8):
                    timed_out = True
                    if process.poll() is None:
                        process.kill()
                    try:
                        output, _ = process.communicate(timeout=5)
                    except subprocess.TimeoutExpired as exc:
                        output = exc.output or b''
                    break
        events = []
        for line in output.decode('utf-8', errors='replace').splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        terminal = next((row for row in reversed(events) if isinstance(row, dict) and row.get('state') == 'finished'), None)
        if not terminal:
            diagnostics = bridge_events(journal, strict=True, offset=offset) if journal else []
            word = request.get('word_generation') is True
            if word or request.get('scope') == 'recognition':
                matches = _is_word_stream_interruption if word else _is_recognition_interruption
                if operation == 'recheck':
                    row = events[-1] if events and isinstance(events[-1], dict) else {}
                    interruption = row if matches(row) and row.get('requested_model') == request['model'] and all(
                        row.get(key) == request[key] for key in ('agent_id', 'run_id', 'workspace')
                    ) else None
                else:
                    interruption = _terminal_stream_error(journal.parent, request['model'], matches) if journal else None
                if interruption:
                    if word:
                        raise CursorWordStreamInterrupted(interruption)
                    raise CursorRecognitionInterrupted(interruption)
            if (timed_out and process.poll() is not None and diagnostics
                    and all(row.get('state') == 'preparing' for row in diagnostics)):
                # This attempt ended before reaching the flushed send boundary.
                event = {'state': 'error', 'stage': diagnostics[-1]['stage'],
                         'error_code': 'CURSOR_PRECHECK_TIMEOUT', 'not_sent': True,
                         'retryable': True, 'at': time.time()}
                with journal.open('a', encoding='utf-8') as log:
                    log.write(json.dumps(event) + '\n')
                diagnostics.append(event)
            message = bridge_problem(diagnostics)
            if confirmed_not_sent(diagnostics):
                raise ProviderError(message, error_code='CURSOR_NOT_DISPATCHED')
            if timed_out and not any(row.get('run_id') for row in diagnostics):
                message = '等待 Cursor 建立会话超时，原请求结果待核对。'
            raise ProviderError(message, error_code='CURSOR_RESPONSE_UNKNOWN')
        if terminal.get('requested_model') != request['model'] or terminal.get('observed_model') != request['model']:
            raise ProviderError('Cursor 回执模型与选定模型不一致。', error_code='CURSOR_MODEL_MISMATCH')
        return terminal
    finally:
        if process.poll() is None:
            process.kill()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass


class CursorAccountProvider(AIProvider):
    provider_version = '0.1-experimental'

    def health_check(self):
        ready, message = bridge_health(self.config['model'])
        return {'configured': runtime() is not None, 'available': ready,
                'status': 'available' if ready else 'authentication_required', 'message': message,
                'credential_source': 'official_cursor_sdk_login', 'metadata': self.get_metadata()}

    def _invoke(self, request, task_dir, *, recommendation=False):
        import paths
        from .openai_compatible import CURRENT_RUN_ID, is_cancelled

        if request.get('purpose') == 'capability_probe' or request.get('generation_mode') == 'capability_probe':
            raise ProviderError('Cursor 连接检查只读取账号模型清单。', error_code='CURSOR_EXPLICIT_GENERATION_REQUIRED')
        model = self.config['model']
        ready, message = bridge_health(model)
        if not ready:
            raise ProviderError(message, error_code='PROVIDER_AUTH_REQUIRED')
        run_id = str(request.get('run_id') or CURRENT_RUN_ID.get() or 'standalone')
        slot = request.get('request_id') or request.get('job_id') or request['task_id']
        digest = hashlib.sha256(json.dumps([model, request], ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
        run_root = paths.current().runtime_dir / 'cursor_account' / hashlib.sha256(run_id.encode()).hexdigest()[:24]
        job = run_root / slot_key(slot)
        original_job = job
        scope = 'generation'
        if recommendation:
            scope = 'recommendation'
        elif request.get('generation_mode') == 'tender_requirement_recognition':
            scope = 'recognition'
        word = scope == 'generation' and request.get('generation_mode') in {'longform_section', 'longform_batch'}
        choice = None
        with SERIAL:
            if is_cancelled(run_id):
                raise ProviderError('任务已停止。', error_code='CURSOR_CANCELLED')
            if scope == 'recognition' and (job / 'recognition_choice.json').is_file():
                from tender_intake.cursor_recognition_recovery import approved_action, retry_key

                original = json.loads((job / 'dispatch.json').read_text(encoding='utf-8'))
                if original.get('digest') != digest:
                    raise ProviderError('原识别内容已变化，请重新核对。', error_code='CURSOR_RESPONSE_UNKNOWN')
                choice = approved_action(job, model, request.get('chunk_id'))
                if choice == 'retry':
                    job = run_root / retry_key(slot)
            identity = job / 'dispatch.json'
            receipt = job / 'reply.json'
            if identity.is_file():
                marker = json.loads(identity.read_text(encoding='utf-8'))
                if marker.get('digest') != digest:
                    raise ProviderError('Cursor 原请求结果待核对，已保留派发记录。', error_code='CURSOR_RESPONSE_UNKNOWN')
                if not receipt.is_file():
                    events = bridge_events(job / 'bridge.jsonl', strict=True)
                    run = next((row for row in reversed(events)
                                if all(row.get(key) for key in ('run_id', 'agent_id', 'workspace'))), None)
                    if scope == 'recognition' and (terminal := terminal_recognition_interruption(job, model)):
                        raise CursorRecognitionInterrupted(terminal)
                    interruption = terminal_word_interruption(job, model) if word else None
                    if interruption:
                        raise CursorWordStreamInterrupted(interruption)
                    if confirmed_not_sent(events):
                        write_json(receipt, self._dispatch(request, job, run_id, scope))
                    elif run and run.get('requested_model') == model:
                        try:
                            response = run_bridge(runtime(), {
                                'operation': 'recheck', 'model': model, 'scope': scope,
                                **({'word_generation': True} if word else {}),
                                **{key: run[key] for key in ('run_id', 'agent_id', 'workspace')},
                            }, run_id)
                            write_json(receipt, response)
                        except (CursorRecognitionInterrupted, CursorWordStreamInterrupted) as exc:
                            write_json(job / 'terminal_error.json', exc.evidence)
                            raise
                        except ProviderError as exc:
                            raise ProviderError(bridge_problem(events), error_code='CURSOR_RESPONSE_UNKNOWN') from exc
                    else:
                        raise ProviderError(bridge_problem(events), error_code='CURSOR_RESPONSE_UNKNOWN')
                response = json.loads(receipt.read_text(encoding='utf-8'))
            else:
                jobs = [(path, json.loads(path.read_text(encoding='utf-8')))
                        for path in run_root.glob('*/dispatch.json')]
                jobs = [(path, row) for path, row in jobs if dispatch_scope(row, path.parent.name, run_id) == scope]
                if any(row.get('model') != model for _, row in jobs):
                    raise ProviderError('此阶段已绑定原 Cursor 模型，请为新模型新建任务。',
                                        error_code='CURSOR_MODEL_MISMATCH')
                def settled(path):
                    if path.with_name('reply.json').is_file():
                        return True
                    if word:
                        # A confirmed terminal failure leaves this slot closed and
                        # its original journal intact. Other Word batches can run.
                        return bool(terminal_word_interruption(path.parent, model))
                    if scope != 'recognition' or not terminal_recognition_interruption(path.parent, model):
                        return False
                    if choice == 'retry' and path.parent == original_job:
                        return True
                    # Only a saved reply from the explicitly approved retry can
                    # unblock later chunks when the original has no reply.
                    from tender_intake.cursor_recognition_recovery import approved_action
                    retry = path.with_name('recognition_choice.json')
                    if not retry.is_file():
                        return False
                    approval = json.loads(retry.read_text(encoding='utf-8'))
                    return (approved_action(path.parent, model, approval.get('chunk_id')) == 'retry'
                            and any(row.get('retries') == path.parent.name and candidate.with_name('reply.json').is_file()
                                    for candidate, row in jobs))

                if any(not settled(path) for path, _ in jobs):
                    if word:
                        raise ProviderError(WORD_PREVIOUS_UNKNOWN_MESSAGE, error_code=WORD_PREVIOUS_UNKNOWN)
                    raise ProviderError('上一条 Cursor 请求结果待核对，已暂停后续请求。',
                                        error_code='CURSOR_RESPONSE_UNKNOWN')
                limit = {'recommendation': 1, 'recognition': RECOGNITION_LIMIT}.get(scope)
                if limit is None:
                    limit = self._writing_limit(run_root, run_id)
                if len(jobs) >= limit:
                    raise ProviderError('本轮 Cursor 试用请求已到上限，已有结果已保留。', error_code='CURSOR_RUN_BUDGET')
                job.mkdir(parents=True, exist_ok=True)
                write_json(identity, {'digest': digest, 'scope': scope, 'model': model,
                                      **({'retries': original_job.name} if choice == 'retry' else {})})
                response = self._dispatch(request, job, run_id, scope)
                write_json(receipt, response)
        if scope == 'recognition':
            from .zcode_recognition import parse_recognition
            raw = response['result']
            punctuation_tail = None
            if choice == 'repair':
                from tender_intake.cursor_recognition_recovery import repair_reply

                fixed = repair_reply(raw, request.get('chunk_id'))
                if fixed:
                    raw, punctuation_tail = fixed
            try:
                result, repair = parse_recognition(request, raw)
                if punctuation_tail:
                    repair.update(applied=True, cursor_removed_trailing_punctuation=punctuation_tail,
                                  cursor_recovery_authorized=True)
            except ProviderError as error:
                raise ProviderError(str(error).replace('ZCode', 'Cursor'),
                                    error_code=error.error_code.replace('ZCODE_', 'CURSOR_', 1)) from error
        else:
            result, repair = parse_provider_payload(request, response['result'], set(request.get('required_keys') or []))
        if request.get('generation_mode') == 'ppt_slide' and result.get('slide_id') != request.get('slide_id'):
            raise ProviderError('Cursor 回执页号与原请求不一致。', error_code='PPT_SLIDE_ID_MISMATCH')
        metadata = self._task_metadata(request['task_id'], formal_output=True, requested_model=model,
                                       observed_model=response['observed_model'], usage=response.get('usage'),
                                       cursor_run_id=response['run_id'], serialization_repair=repair)
        write_json(Path(task_dir) / 'provider_audit.json', metadata)
        return result, metadata

    def _dispatch(self, request, job, run_id, scope):
        from .openai_compatible import is_cancelled

        model = self.config['model']
        journal = job / 'bridge.jsonl'
        bridge_request = {
            'model': model,
            'scope': scope,
            **({'word_generation': True} if scope == 'generation'
               and request.get('generation_mode') in {'longform_section', 'longform_batch'} else {}),
            'prompt': str(request.get('system_prompt') or '') + '\n\n' + str(request['prompt']),
            'journal': str(journal),
            'model_cache': str(job.parent / f'model-{slot_key(model)}.json'),
            'limit_seconds': 240 if scope == 'recommendation' else ACTIVE_LIMIT_SECONDS,
            'idle_seconds': IDLE_LIMIT_SECONDS,
        }
        effort = str(request.get('reasoning_effort') or '').strip().lower()
        if effort in {'low', 'medium', 'high', 'xhigh', 'max'}:
            bridge_request['reasoning_effort'] = effort
        try:
            return run_bridge(runtime(), bridge_request, run_id)
        except ProviderError as exc:
            events = bridge_events(journal, strict=True)
            if (exc.error_code != 'CURSOR_NOT_DISPATCHED' or not confirmed_not_sent(events)
                    or not events[-1].get('retryable')):
                raise
        time.sleep(1)
        if is_cancelled(run_id):
            raise ProviderError('任务已停止。', error_code='CURSOR_CANCELLED')
        return run_bridge(runtime(), bridge_request, run_id)

    def _writing_limit(self, jobs, run_id):
        import paths
        from account_budget import word_cap

        brief = paths.current().runs_dir / run_id / 'brief.json'
        if brief.is_file() and json.loads(brief.read_text(encoding='utf-8-sig')).get('medium') == 'WORD':
            return word_cap(jobs, run_id, self.config['model'])
        return 32

    def invoke_structured(self, request, task_dir):
        result, metadata = self._invoke(request, task_dir)
        return {**result, 'provider_metadata': metadata}

    def recommend_knowledge(self, request, task_dir):
        result, metadata = self._invoke(request, task_dir, recommendation=True)
        return normalize_recommendation(result, metadata)

    def generate_solution(self, request, task_dir):
        result, metadata = self._invoke(request, task_dir)
        return normalize_generation(result, metadata)
