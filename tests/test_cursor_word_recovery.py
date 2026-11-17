import json
from types import SimpleNamespace

import paths
from cursor_word_recovery import LEGACY_BLOCK, inspect, settle
from longform import request_ledger as ledger
from providers.base import write_json
from providers.cursor_account import slot_key


def test_resume_reconciles_confirmed_stop_and_legacy_local_refusal_only(tmp_path, monkeypatch):
    run = tmp_path / 'runs' / 'test-cursor-word'
    root = tmp_path / 'runtime'
    monkeypatch.setattr(paths, 'current', lambda: SimpleNamespace(runtime_dir=root))
    provider = 'cursor_grok_4_7'
    write_json(run / 'brief.json', {'provider_name': provider, 'medium': 'WORD'})
    write_json(run / 'generation_status.json', {'status': 'failed'})
    write_json(run / 'generation_input.json', {'provider_name': provider})
    requests = [f'REQ-{run.name}-{i:012d}-A1' for i in (1, 2, 3)]
    interrupted = root / 'cursor_account' / slot_key(run.name) / slot_key(requests[0])
    write_json(interrupted / 'dispatch.json', {'scope': 'generation', 'model': 'grok-4.7', 'digest': 'existing'})
    identity = {'agent_id': 'agent-original', 'run_id': 'run-original', 'workspace': 'text-workspace-test',
                'requested_model': 'grok-4.7'}
    journal = interrupted / 'bridge.jsonl'
    journal.write_text('\n'.join(map(json.dumps, [
        {**identity, 'state': 'running'},
        {**identity, 'state': 'incomplete', 'terminal_status': 'error', 'error_code': 'connection_stalled'},
    ])), encoding='utf-8')
    events = run / ledger.LEDGER_FILENAME
    rows = [{'request_id': rid, 'run_id': run.name, 'provider': provider, 'status': ledger.RESPONSE_UNKNOWN,
             'error_code': 'CURSOR_RESPONSE_UNKNOWN', 'error': error}
            for rid, error in zip(requests, ['terminal', LEGACY_BLOCK, 'unverified'])]
    original = journal.read_bytes()
    events.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
    candidate = inspect(run)
    assert not candidate['can_resume']
    unchanged = events.read_bytes()
    settle(candidate)
    assert events.read_bytes() == unchanged

    events.write_text('\n'.join(map(json.dumps, rows[:2])), encoding='utf-8')
    unchanged = events.read_bytes()
    candidate = inspect(run)
    assert candidate['can_resume'] and candidate['sent'] == 1 and candidate['returned'] == 0
    settle(candidate)
    assert events.read_bytes().startswith(unchanged)
    assert not ledger.open_requests(ledger.read_events(events))
    assert [row['resolution'] for row in ledger.read_events(events)[-2:]] == ['definitive_failure', 'not_dispatched']
    assert not inspect(run)['settlements'] and journal.read_bytes() == original
