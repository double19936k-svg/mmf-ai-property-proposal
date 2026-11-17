import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import paths
from providers import cursor_account
from providers.base import ProviderError


@pytest.fixture
def connected(tmp_path, monkeypatch):
    monkeypatch.setenv('MMF_CURSOR_BRIDGE_ROOT', str(tmp_path))
    monkeypatch.setattr(cursor_account.shutil, 'which', lambda _: 'node.exe')
    (tmp_path / 'cursor_bridge.mjs').touch()
    (tmp_path / 'catalog.json').write_text(
        json.dumps({'state': 'ready', 'models': [{'id': 'composer-2.5'}]}), encoding='utf-8')
    monkeypatch.setattr(paths, 'current', lambda: SimpleNamespace(
        runtime_dir=tmp_path / 'runtime', runs_dir=tmp_path / 'runs'))
    return cursor_account.CursorAccountProvider(cursor_account.cursor_provider_rows()[0])


def test_sdk_reply_is_reused_by_request_identity(connected, tmp_path, monkeypatch):
    calls = []

    def reply(root, request, run_id):
        calls.append((request['model'], run_id))
        return {'state':'finished', 'observed_model':'composer-2.5', 'run_id':'sdk-run',
                'result':'{"ok":true}', 'usage':{'totalTokens':12}}

    monkeypatch.setattr(cursor_account, 'run_bridge', reply)
    request = {'run_id':'sample-cursor', 'task_id':'s1', 'prompt':'合成测试', 'required_keys':['ok']}
    result = connected.invoke_structured(request, tmp_path / 'first')
    again = connected.invoke_structured(request, tmp_path / 'resumed')
    assert calls == [('composer-2.5', 'sample-cursor')]
    assert result['ok'] is True and again == result
    assert result['provider_metadata']['usage']['totalTokens'] == 12
    with pytest.raises(ProviderError, match='原请求结果待核对'):
        connected.invoke_structured({**request, 'prompt':'changed'}, tmp_path / 'changed')
    assert len(calls) == 1


def test_unconfirmed_dispatch_is_not_resent(connected, tmp_path, monkeypatch):
    calls = []

    def fail(*args):
        calls.append(args)
        raise ProviderError('unknown', error_code='CURSOR_RESPONSE_UNKNOWN')

    monkeypatch.setattr(cursor_account, 'run_bridge', fail)
    request = {'run_id':'sample-cursor', 'task_id':'s2', 'prompt':'合成测试'}
    for item in [request, request, {**request, 'task_id': 'another-slot'}]:
        with pytest.raises(ProviderError) as error:
            connected.invoke_structured(item, tmp_path / 'call')
        assert error.value.error_code == 'CURSOR_RESPONSE_UNKNOWN'
    assert len(calls) == 1


def test_unknown_recognition_only_reads_original_sdk_run(connected, tmp_path, monkeypatch):
    from tender_intake.understanding import _is_transient_provider_error

    requests = []

    def bridge(root, request, run_id):
        requests.append(request)
        if request.get('operation') == 'recheck':
            raise ProviderError('unknown', error_code='CURSOR_RESPONSE_UNKNOWN')
        with open(request['journal'], 'w', encoding='utf-8') as file:
            file.write(json.dumps({
                'state':'stopping', 'reason':'idle_timeout', 'run_id':'run-example',
                'workspace':'text-workspace-abc', 'agent_id':'agent-example',
                'requested_model':request['model'],
            }))
        raise ProviderError('连接超时', error_code='CURSOR_RESPONSE_UNKNOWN')

    monkeypatch.setattr(cursor_account, 'run_bridge', bridge)
    request = {'run_id':'synthetic', 'task_id':'chunk', 'prompt':'recognize',
               'generation_mode':'tender_requirement_recognition', 'reasoning_effort':'low'}
    with pytest.raises(ProviderError) as first:
        connected.invoke_structured(request, tmp_path / 'initial')
    assert not _is_transient_provider_error(first.value)
    with pytest.raises(ProviderError, match='长时间没有新的模型输出'):
        connected.invoke_structured(request, tmp_path / 'recheck')
    assert len(requests) == 2
    assert requests[0]['limit_seconds'] == 600
    assert requests[0]['reasoning_effort'] == 'low'
    assert requests[1] == {'operation':'recheck', 'model':'composer-2.5', 'scope':'recognition',
                           'workspace':'text-workspace-abc', 'run_id':'run-example', 'agent_id':'agent-example'}


def test_confirmed_unsent_resumes_same_slot_and_keeps_receipt(connected, tmp_path, monkeypatch):
    calls = []
    legacy = {'state':'error','error_code':'CURSOR_SDK_ERROR','at':1000}

    def bridge(root, request, run_id):
        calls.append(request)
        if len(calls) == 1:
            Path(request['journal']).write_text(json.dumps(legacy) + '\n', encoding='utf-8')
            raise ProviderError('old error', error_code='CURSOR_RESPONSE_UNKNOWN')
        return {'state':'finished','observed_model':'composer-2.5','run_id':'sdk-run','result':'{"ok":true}'}

    monkeypatch.setattr(cursor_account, 'run_bridge', bridge)
    request = {'run_id':'demo','task_id':'pending','prompt':'pending','required_keys':['ok']}
    with pytest.raises(ProviderError):
        connected.invoke_structured(request, tmp_path / 'pending')
    with pytest.raises(ProviderError, match='原请求结果待核对'):
        connected.invoke_structured({**request,'prompt':'different'}, tmp_path / 'pending')
    assert connected.invoke_structured(request, tmp_path / 'pending')['ok']
    assert connected.invoke_structured(request, tmp_path / 'reused')['ok']
    assert len(calls) == 2 and calls[0] == calls[1]
    assert not cursor_account.confirmed_not_sent([{'state':'sending'}, legacy])


def test_confirmed_transient_failure_retries_at_most_once(connected, tmp_path, monkeypatch):
    calls = []

    def bridge(root, request, run_id):
        calls.append(request)
        with open(request['journal'], 'a', encoding='utf-8') as journal:
            journal.write(json.dumps({'state':'error','stage':'catalog','error_code':'network_error',
                                      'not_sent':True,'retryable':True}) + '\n')
        raise ProviderError('preflight failed', error_code='CURSOR_NOT_DISPATCHED')

    monkeypatch.setattr(cursor_account, 'run_bridge', bridge)
    monkeypatch.setattr(cursor_account.time, 'sleep', lambda _: None)
    with pytest.raises(ProviderError, match='preflight failed'):
        connected.invoke_structured({'run_id':'r1','task_id':'step1','prompt':'sample'}, tmp_path / 'first')
    assert len(calls) == 2 and calls[0] == calls[1]


@pytest.mark.parametrize('streaming', [True, False])
def test_worker_distinguishes_activity_from_idle_timeout(tmp_path, streaming, monkeypatch):
    fake_sdk = tmp_path / 'node_modules/@cursor/sdk'
    fake_sdk.mkdir(parents=True)
    (fake_sdk / 'index.js').write_text("""
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const { appendFileSync } = require('node:fs');
exports.Cursor = {configure(){}, auth:{status:async()=>({status:'logged-in'})},
                  models:{list:async()=>{
                    appendFileSync('catalog_count', '1');
                    return [{id:'grok-4.6'}];
                  }}};
exports.emptyFs = () => ({});
exports.Agent = {create:async()=>({agentId:'agent-test',async [Symbol.asyncDispose](){},
  send:async prompt=>{
    const active = prompt.endsWith('active');
    let finish;
    const done = new Promise(resolve=>{finish=resolve;});
    return {id:'run-test',wait:()=>done,
      cancel:async()=>finish({status:'cancelled'}),
      async *stream(){
        if (!active) return;
        for(let i=0;i<28;i++){
          await wait(50);
          yield {type:'assistant',message:{content:[{text:'content must not be written to the journal'}]}};
        }
        finish({status:'finished',result:'{"ok":true}',model:{id:'grok-4.6'}});
      }};
  }})};
""", encoding='utf-8')
    journal = tmp_path / 'bridge.jsonl'
    node = os.environ.get('RUN_J_NODE') or cursor_account.shutil.which('node')
    def run_worker():
        return subprocess.run(
            [node, str(Path(cursor_account.__file__).with_name('cursor_bridge.mjs'))],
            cwd=tmp_path, input=json.dumps({
                'model':'grok-4.6','prompt':'active' if streaming else 'silent',
                'journal':str(journal), 'idle_seconds':0.2, 'limit_seconds':4,
                'model_cache':str(tmp_path/'selected_model.json'),
            }), text=True, encoding='utf-8', capture_output=True, timeout=12, check=True,
        )
    process = run_worker()
    rows = cursor_account.bridge_events(journal)
    final = json.loads(process.stdout)
    assert all('message' not in row and 'result' not in row for row in rows)
    assert final['run_id'] == 'run-test'
    if streaming:
        assert any(row.get('activity') == 'assistant' for row in rows)
        assert final['state'] == 'finished' and json.loads(final['result']) == {'ok':True}
    else:
        assert final['state'] == 'incomplete' and final['reason'] == 'idle_timeout'
    if streaming:
        journal.write_text('{"state":"error","error_code":"CURSOR_SDK_ERROR","at":1000}\n', encoding='utf-8')
        monkeypatch.setattr(cursor_account.shutil, 'which', lambda _: node)
        response = cursor_account.run_bridge(tmp_path, {
            'model':'grok-4.6','prompt':'active','journal':str(journal),
            'idle_seconds':0.2,'limit_seconds':4,'model_cache':str(tmp_path/'selected_model.json'),
        }, 'fixture')
        assert response['state'] == 'finished'
        assert (tmp_path/'catalog_count').read_text(encoding='utf-8') == '1'


@pytest.mark.parametrize('legacy', [False, True])
def test_unconfirmed_recommendation_does_not_block_writing(connected, tmp_path, monkeypatch, legacy):
    run_id = 'sample-cursor'
    calls = []

    def bridge(root, request, run_id):
        calls.append(request['prompt'].strip())
        if calls[-1] == 'recommend':
            raise ProviderError('timeout', error_code='CURSOR_RESPONSE_UNKNOWN')
        return {'state':'finished', 'observed_model':'composer-2.5', 'run_id':'sdk-write',
                'result':'{"ok":true}'}

    monkeypatch.setattr(cursor_account, 'run_bridge', bridge)
    recommend = {'run_id':run_id, 'task_id':f'{run_id}-recommend', 'prompt':'recommend'}
    with pytest.raises(ProviderError, match='timeout'):
        connected.recommend_knowledge(recommend, tmp_path / 'recommend')
    if legacy:
        run_key = hashlib.sha256(run_id.encode()).hexdigest()[:24]
        marker_path = next((tmp_path / 'runtime' / 'cursor_account' / run_key).glob('*/dispatch.json'))
        marker = json.loads(marker_path.read_text(encoding='utf-8'))
        marker_path.write_text(json.dumps({**marker, 'scope':'generation'}), encoding='utf-8')
    with pytest.raises(ProviderError, match='原请求结果待核对'):
        connected.recommend_knowledge(recommend, tmp_path / 'recommend_again')
    assert connected.invoke_structured(
        {'run_id':run_id, 'task_id':'draft', 'prompt':'write', 'required_keys':['ok']},
        tmp_path / 'draft',
    )['ok'] is True
    assert calls == ['recommend', 'write']


def test_connection_probe_never_dispatches(connected, tmp_path):
    assert connected.health_check()['available']
    with pytest.raises(ProviderError) as error:
        connected.invoke_structured({'purpose':'capability_probe'}, tmp_path / 'probe')
    assert error.value.error_code == 'CURSOR_EXPLICIT_GENERATION_REQUIRED'


@pytest.mark.parametrize('repairable', [True, False])
def test_recognition_needs_choice_before_repair_or_retry(connected, tmp_path, monkeypatch, repairable):
    from providers.base import write_json
    from tender_intake.cursor_recognition_recovery import FALLBACK, authorize_choice, pending_choice
    from providers.zcode_recognition import ARRAY_KEYS

    run_id = 'tender-test'
    chunk_id = 'CHK-0001'
    run = tmp_path / 'runs' / run_id
    result = json.dumps({'chunk_id':chunk_id, **{key:[] for key in ARRAY_KEYS}})
    raw = result + '],\n}' if repairable else result[:-1]
    calls = []

    def bridge(root, request, run_id):
        calls.append(request)
        return {'state':'finished','requested_model':'composer-2.5','observed_model':'composer-2.5',
                'run_id':f'sdk-{len(calls)}','result':raw if len(calls)==1 else result}

    monkeypatch.setattr(cursor_account, 'run_bridge', bridge)
    request = {'run_id':run_id,'task_id':f'{run_id}-tender-{chunk_id}','chunk_id':chunk_id,'prompt':'sample',
               'generation_mode':'tender_requirement_recognition'}
    for _ in range(2):
        with pytest.raises(ProviderError) as caught:
            connected.invoke_structured(request, run/'call')
        assert caught.value.error_code == 'CURSOR_RECOGNITION_FORMAT'
    assert len(calls) == 1
    status = {'provider_name':'cursor_account','failed_chunk':chunk_id,'recognition_outcome':FALLBACK}
    write_json(run/'tender/chunks'/chunk_id/'recognition_retry_audit.json', {'attempts':[
        {'error_code':'CURSOR_RECOGNITION_FORMAT','call_dir':'provider_call'},
    ]})
    choice = pending_choice(run, status)
    action = 'repair' if repairable else 'retry'
    assert choice['action'] == action and not authorize_choice(choice, action, 'stale')
    from account_progress import snapshot
    ui = snapshot(run_id, [], recognition={**status, 'chunks_completed':0,'chunks_total':6})
    assert ui['state'] == 'waiting_for_user' and ui['recognition_controls']['accept_local']
    assert ui['recognition_controls']['choice'] == {'action':action, 'nonce':choice['nonce']}
    original = (choice['job']/'reply.json').read_bytes()
    assert authorize_choice(choice, action, choice['nonce'])
    for _ in range(2):
        fixed = connected.invoke_structured(request, run/'call')
        assert fixed['chunk_id'] == chunk_id
    assert len(calls) == (1 if repairable else 2)
    assert (choice['job']/'reply.json').read_bytes() == original
    if repairable:
        assert fixed['provider_metadata']['serialization_repair']['cursor_recovery_authorized']
    else:
        assert pending_choice(run, status)['action'] is None


def test_cursor_punctuation_repair_rejects_wrong_id_and_extra_content():
    from tender_intake.cursor_recognition_recovery import repair_reply
    from providers.zcode_recognition import ARRAY_KEYS

    body = json.dumps({'chunk_id':'CHK-0001-Z008', **{key:[] for key in ARRAY_KEYS}})
    assert repair_reply(body + '],\n}', 'CHK-0001-Z008') == (body, 4)
    assert repair_reply(body + '],\n}', 'CHK-0001-Z009') is None
    assert repair_reply(body + '{"chunk_id":"other"}', 'CHK-0001-Z008') is None
    assert repair_reply(body[:-1], 'CHK-0001-Z008') is None


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('code, reason', [('STREAM_ABORTED', 'stream_aborted'), ('connection_stalled', None)])
def test_word_stream_abort_keeps_the_original_slot_and_unblocks_later_batches(connected, tmp_path, monkeypatch, legacy, code, reason):
    calls = []
    identity = {'workspace': 'text-workspace-one', 'agent_id': 'agent-one',
                'run_id': 'run-one', 'requested_model': 'composer-2.5'}
    terminal = {**identity, 'state': 'incomplete', 'terminal_status': 'error',
                'error_code': code, 'reason': reason}

    def bridge(root, request, rid):
        calls.append(request)
        assert request['word_generation']
        if request.get('operation') == 'recheck':
            assert legacy and 'prompt' not in request and request['run_id'] == identity['run_id']
            raise cursor_account.CursorWordStreamInterrupted(terminal)
        if len(calls) == 1:
            last = {**terminal, 'error_code': None, 'reason': None} if legacy else terminal
            Path(request['journal']).write_text(''.join(json.dumps(row) + '\n' for row in
                                                       ({**identity, 'state': 'running'}, last)), encoding='utf-8')
            if legacy:
                raise ProviderError('Result unknown', error_code='CURSOR_RESPONSE_UNKNOWN')
            raise cursor_account.CursorWordStreamInterrupted(terminal)
        return {'state': 'finished', 'observed_model': 'composer-2.5', 'run_id': 'run-later', 'result': '{"ok":true}'}

    monkeypatch.setattr(cursor_account, 'run_bridge', bridge)
    first = {'run_id': 'word', 'task_id': 'section-1', 'generation_mode': 'longform_section', 'prompt': 'first'}
    later = {**first, 'task_id': 'batch-2', 'generation_mode': 'longform_batch', 'prompt': 'second'}
    with pytest.raises(ProviderError) as failed:
        connected.invoke_structured(first, tmp_path / 'first')
    assert failed.value.error_code == ('CURSOR_RESPONSE_UNKNOWN' if legacy else 'CURSOR_WORD_STREAM_INTERRUPTED')
    if legacy:
        with pytest.raises(ProviderError, match='上一条 Cursor 请求结果待核对'):
            connected.invoke_structured(later, tmp_path / 'later')
    with pytest.raises(cursor_account.CursorWordStreamInterrupted):
        connected.invoke_structured(first, tmp_path / 'first')
    assert connected.invoke_structured(later, tmp_path / 'later')['ok'] is True
    assert [r.get('prompt', '').strip() for r in calls if not r.get('operation')] == ['first', 'second']
    assert len(calls) == (3 if legacy else 2)
    job = paths.current().runtime_dir / 'cursor_account' / cursor_account.slot_key('word') / cursor_account.slot_key('section-1')
    assert (job / 'dispatch.json').is_file() and not (job / 'reply.json').exists()


@pytest.mark.parametrize('legacy', [True, False])
@pytest.mark.parametrize('sdk_code, app_code, message', [
    ('ECONNRESET', cursor_account.RECOGNITION_RESET, '连接被重置'),
    ('connection_stalled', cursor_account.RECOGNITION_STALLED, '输出连接停滞'),
])
def test_confirmed_recognition_interruption_needs_choice_and_reuses_retry(
        connected, tmp_path, monkeypatch, legacy, sdk_code, app_code, message):
    from account_progress import snapshot
    from providers.base import write_json
    from providers.zcode_recognition import ARRAY_KEYS
    from tender_intake.cursor_recognition_recovery import FALLBACK, WAITING, authorize_choice, pending_choice, retry_key
    from tender_intake.understanding import _is_transient_provider_error

    run_id, chunk_id = 'tender-reset', 'CHK-0002'
    slot = f'{run_id}-tender-{chunk_id}'
    run = tmp_path / 'runs' / run_id
    job = paths.current().runtime_dir / 'cursor_account' / cursor_account.slot_key(run_id) / cursor_account.slot_key(slot)
    identity = {'workspace': 'text-workspace-abc', 'agent_id': 'agent-1', 'run_id': 'run-1',
                'requested_model': 'composer-2.5'}
    terminal = {**identity, 'state': 'incomplete', 'terminal_status': 'error', 'error_code': sdk_code}
    unknown = 'CURSOR_RESPONSE_UNKNOWN'
    calls = []

    def bridge(root, request, rid):
        calls.append(request)
        if request.get('operation') == 'recheck':
            assert legacy and 'prompt' not in request and request['run_id'] == identity['run_id']
            raise cursor_account.CursorRecognitionInterrupted(terminal)
        if len(calls) == 1:
            journal = [{**identity, 'state': 'running'}, {**terminal, 'error_code': None} if legacy else terminal]
            Path(request['journal']).write_text(''.join(json.dumps(row) + '\n' for row in journal), encoding='utf-8')
            if legacy:
                raise ProviderError('unknown', error_code=unknown)
            raise cursor_account.CursorRecognitionInterrupted(terminal)
        target = chunk_id if Path(request['journal']).parent.name == retry_key(slot) else 'CHK-0003'
        return {'state': 'finished', 'requested_model': 'composer-2.5', 'observed_model': 'composer-2.5',
                'run_id': f'run-{len(calls)}', 'result': json.dumps({'chunk_id': target, **{k: [] for k in ARRAY_KEYS}})}

    monkeypatch.setattr(cursor_account, 'run_bridge', bridge)
    request = {'run_id': run_id, 'task_id': slot, 'chunk_id': chunk_id, 'prompt': 'sample',
               'generation_mode': 'tender_requirement_recognition'}
    later = {**request, 'task_id': f'{run_id}-tender-CHK-0003', 'chunk_id': 'CHK-0003'}
    with pytest.raises(ProviderError) as caught:
        connected.invoke_structured(request, run / 'call')
    assert caught.value.error_code == (unknown if legacy else app_code)
    assert not _is_transient_provider_error(caught.value)
    status = {'provider_name': 'cursor_account', 'failed_chunk': chunk_id,
              'recognition_outcome': FALLBACK if legacy else WAITING, 'chunks_completed': 1, 'chunks_total': 6}
    write_json(run / 'tender/chunks' / chunk_id / 'recognition_retry_audit.json',
               {'attempts': [{'error_code': caught.value.error_code, 'call_dir': 'provider_call'}]})
    original = (job / 'bridge.jsonl').read_bytes()
    if legacy:
        assert pending_choice(run, status) is None
        write_json(job / 'terminal_error.json', {**terminal, 'run_id': 'run-other'})
        assert pending_choice(run, status) is None
    with pytest.raises(ProviderError) as caught:
        connected.invoke_structured(request, run / 'call')
    assert caught.value.error_code == app_code
    with pytest.raises(ProviderError) as caught:
        connected.invoke_structured(later, run / 'later')
    assert caught.value.error_code == unknown and len(calls) == (2 if legacy else 1)
    choice = pending_choice(run, status)
    assert choice['terminal_code'] == sdk_code and choice['action'] == 'retry'
    assert not authorize_choice(choice, 'repair', choice['nonce'])
    assert not authorize_choice(choice, 'retry', 'old-choice')
    ui = snapshot(run_id, [], recognition=status)
    assert ui['state'] == 'waiting_for_user' and message in ui['message']
    assert ui['recognition_controls']['choice']['action'] == 'retry'
    assert authorize_choice(choice, 'retry', choice['nonce'])
    assert connected.invoke_structured(request, run / 'call')['chunk_id'] == chunk_id
    assert connected.invoke_structured(request, run / 'call')['chunk_id'] == chunk_id
    assert connected.invoke_structured(later, run / 'later')['chunk_id'] == 'CHK-0003'
    assert len(calls) == (4 if legacy else 3)
    assert pending_choice(run, status)['action'] is None
    assert (job / 'bridge.jsonl').read_bytes() == original and not (job / 'reply.json').exists()


def test_catalog_grok_choice_dispatches_exact_model_and_binds_phase(connected, tmp_path, monkeypatch):
    models = [{'id':'composer-2.5'}, {'id':'grok-4.7','displayName':'Grok 4.7'},
              {'id':'grok-4.6'}, {'id':'grok-4.5'}, {'id':'default'}, {'id':'custom-grok'}]
    (tmp_path/'catalog.json').write_text(json.dumps({'state':'ready','models':models}), encoding='utf-8')
    rows = cursor_account.cursor_provider_rows()
    assert [row['model'] for row in rows] == ['composer-2.5','grok-4.7','grok-4.6','grok-4.5']
    selected = cursor_account.CursorAccountProvider(rows[1])
    seen = []

    def reply(root, request, run_id):
        seen.append(request['model'])
        return {'state':'finished','observed_model':request['model'],'run_id':'sdk-grok',
                'result':'{"ok":true}'}

    monkeypatch.setattr(cursor_account, 'run_bridge', reply)
    request = {'run_id':'demo','task_id':'s1','prompt':'Sample','required_keys':['ok']}
    assert selected.invoke_structured(request,tmp_path/'selected')['provider_metadata']['observed_model']=='grok-4.7'
    with pytest.raises(ProviderError, match='此阶段已绑定原 Cursor 模型'):
        connected.invoke_structured({**request,'task_id':'s2'},tmp_path/'other')
    assert seen == ['grok-4.7']
