import hashlib
from types import SimpleNamespace

import pytest

import paths
from providers.base import ProviderError, write_json
from providers.zcode_recognition import parse_recognition
from tender_intake.cursor_recognition_recovery import FALLBACK, authorize_choice, pending_choice
from tender_intake.zcode_identity_recovery import (
    MISMATCH, MODE, RETRY_SUFFIX, _digest, _locations, apply_approved_retry,
)


def test_mismatched_zcode_receipt_stays_isolated_and_requires_an_explicit_retry(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    monkeypatch.setattr(paths, 'current', lambda: SimpleNamespace(runtime_dir=runtime))
    run = tmp_path / 'runs' / 'tender-fixture'
    chunk = 'CHK-0002'
    segment = chunk + '-Z006'
    slot = f'{run.name}-tender-{chunk}-segment-' + 'a' * 20
    request = {'run_id': run.name, 'task_id': slot, 'chunk_id': segment, 'generation_mode': MODE,
               'prompt': 'synthetic', 'system_prompt': 'system'}
    model = 'account:bigmodel-individual-coding-plan/GLM-5.3-Flash'
    ctl = SimpleNamespace(selection={'model': model})
    _, jobs, agent = _locations(run.name, slot)
    key = hashlib.sha256(slot.encode()).hexdigest()[:24]
    receipt = {'digest': 'original', 'requested_model': 'GLM-5.3-Flash',
               'body': '{"chunk_id":"CHK-000643-Z006","proposed_items":[]}'}
    write_json(agent / 'state.json', {'provider': 'zcode_cn', 'state': 'needs_attention',
                                      'turn': 1, 'account_model': model, 'error_code': MISMATCH, 'digest': _digest(request)})
    write_json(agent / 'request.json', request)
    write_json(agent / 'receipt-0.json', receipt)
    write_json(jobs / f'{key}.json', {'status': 'returned', 'budget_scope': 'recognition', 'digest': 'original',
                                      'run_id': run.name, 'task_id': slot})
    write_json(jobs / f'{key}.answer.json', receipt)
    (jobs / '.model').write_text(model, encoding='utf-8')
    block = run / 'tender/chunks' / chunk
    write_json(block / 'recognition_retry_audit.json', {'attempts': [{'error_code': MISMATCH, 'call_dir': 'provider_call'}]})
    write_json(block / 'provider_call/recognition_segments/plan.json',
               {'segments': [{'status': 'FAIL', 'segment_id': segment, 'task_id': slot}]})
    status = {'provider_name': 'zcode_cn', 'failed_chunk': chunk, 'recognition_outcome': FALLBACK}
    with pytest.raises(ProviderError) as rejected:
        parse_recognition(request, receipt['body'])
    assert rejected.value.error_code == MISMATCH
    assert apply_approved_retry(request, ctl) == request
    choice = pending_choice(run, status)
    assert choice['action'] == 'retry' and not authorize_choice(choice, 'retry', 'stale')
    assert authorize_choice(choice, 'retry', choice['nonce'])
    retry = apply_approved_retry(request, ctl)
    assert retry['task_id'] == slot + RETRY_SUFFIX and retry['chunk_id'] == segment
    assert retry['prompt'] == request['prompt'] and segment in retry['system_prompt']
    with pytest.raises(ProviderError, match='授权不一致'):
        apply_approved_retry({**request, 'prompt': 'different'}, ctl)
    _, _, retry_agent = _locations(run.name, slot + RETRY_SUFFIX)
    write_json(retry_agent / 'state.json', {'state': 'interrupted'})
    assert pending_choice(run, status)['action'] is None
    write_json(jobs / f'{key}.answer.json', {**receipt, 'digest': 'other'})
    assert pending_choice(run, status) is None
