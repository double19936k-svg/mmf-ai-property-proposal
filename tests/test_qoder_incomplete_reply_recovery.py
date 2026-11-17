import json
from types import SimpleNamespace

import account_progress
from tender_intake import understanding
from tender_intake.qoder_recognition_recovery import split_dense_paragraphs


def save(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


def dense_chunk():
    return {'chunk_id': 'CHK-0002', 'blocks': [
        {'kind': 'paragraph', 'source_id': f'P-{i}', 'text': f'Line {i}'}
        for i in range(331)
    ]}


def test_dense_qoder_recovery_uses_new_children_only_for_approved_parent(tmp_path, monkeypatch):
    from tender_intake import recognition_segments

    chunk = dense_chunk()
    original = {'task_id': 'r-tender-CHK-0002', 'chunk_id': 'CHK-0002'}
    calls = []
    provider = SimpleNamespace(config={'provider_type': 'qoder_account'},
                               invoke_structured=lambda request, _: calls.append(request['task_id']))
    monkeypatch.setattr(recognition_segments, 'execute_segments',
                        lambda _p, _r, _c, _e, _d, segments, *_a: calls.extend(segments))
    save(tmp_path / 'qoder_request_identity.json', {'task_id': original['task_id'], 'digest': 'original'})
    save(tmp_path / 'qoder_raw_answer.json',
         {'event': 'result', 'stop_reason': 'end_turn', 'is_error': False, 'digest': 'original'})
    save(tmp_path / 'qoder_incomplete_reply_recovery.json',
         {'task_id': original['task_id'], 'original_digest': 'different', 'reason': 'verified_incomplete_terminal_reply'})
    understanding._invoke_recognition(provider, original, tmp_path, chunk=chunk, extraction={})
    assert calls == [original['task_id']]

    save(tmp_path / 'qoder_incomplete_reply_recovery.json',
         {'task_id': original['task_id'], 'original_digest': 'original', 'reason': 'verified_incomplete_terminal_reply'})
    calls.clear()
    understanding._invoke_recognition(provider, original, tmp_path, chunk=chunk, extraction={})
    assert [child['chunk_id'] for child in calls] == ['CHK-0002-S001', 'CHK-0002-S002']
    assert [block for child in calls for block in child['blocks']] == chunk['blocks']


def test_bounded_dense_split_keeps_normal_paragraphs_and_table_blocks():
    chunk = dense_chunk()
    assert [len(child['blocks']) for child in split_dense_paragraphs(chunk)] == [166, 165]
    chunk['blocks'][0]['kind'] = 'table'
    assert split_dense_paragraphs(chunk) == [chunk]


def test_running_account_reply_supersedes_previous_recognition_warning(tmp_path, monkeypatch):
    run = tmp_path / 'r'
    run.mkdir()
    monkeypatch.setattr(account_progress.paths, 'current', lambda: SimpleNamespace(runs_dir=tmp_path))
    monkeypatch.setattr(account_progress, 'usage', lambda *_: {})
    tasks = [{'task_id': 'r-tender-CHK-0002', 'state': 'resuming', 'provider': 'qoder_account'}]
    status = {'provider_name': 'qoder_account', 'chunks_completed': 1, 'chunks_total': 6,
              'recognition_outcome': 'AI_RECOGNITION_FAILED_WITH_LOCAL_FALLBACK'}
    active = account_progress.snapshot('r', tasks, recognition=status)
    assert active['state'] == 'running' and active['task'] is None
    assert active['recognition_controls']['accept_local'] is False
    tasks[0]['state'] = 'needs_attention'
    stopped = account_progress.snapshot('r', tasks, recognition=status)
    assert stopped['state'] == 'needs_attention'
    assert stopped['recognition_controls']['accept_local'] is True
