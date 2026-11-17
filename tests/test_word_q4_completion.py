"""Cancellation and partial-batch regressions; no model or network."""
import json
import pytest

from providers.qoder_internal import QoderInternalProvider
from providers.openai_compatible import GenerationCancelled
from longform.factory import LongformGenerationFactory


@pytest.mark.parametrize('cancellation_source', ['queue_stop', 'run_context'])
def test_cancelled_generation_cannot_enqueue_remaining_units(tmp_path, monkeypatch, cancellation_source):
    queue = tmp_path / 'q'
    queue.mkdir()
    provider = QoderInternalProvider({'provider_name': 'qoder_internal', 'queue_dir': str(queue), 'test_mode': True})
    if cancellation_source == 'queue_stop':
        (queue / 'STOP').write_text('{}', encoding='utf-8')
    else:
        monkeypatch.setattr(provider, '_cancelled', lambda: True)
    for n in range(3):
        with pytest.raises(GenerationCancelled):
            provider.invoke_structured({'task_id': f'offline-{n}', 'prompt': 'fixture'}, tmp_path / f'task{n}')
    assert not list((queue / 'out').glob('*.request.json'))
    ledger = queue / 'ledger/calls.jsonl'
    assert not ledger.exists() or 'REQUEST_ENQUEUED' not in ledger.read_text(encoding='utf-8')


def test_partial_cu_job_does_not_instruct_full_section_length():
    context = {'section_contract': {'section_id': 'S01-01', 'target_words': {'min': 9000, 'max': 11000}},
               'content_unit_contracts': [{'content_unit_id': 'CU-1', 'target_budget': 700}]}
    prompt = LongformGenerationFactory._word_prompt(context)['prompt']
    # The full section range remains in the input context as evidence, not as the batch delivery demand.
    instruction = prompt.split('Context Pack：')[0]
    assert '9000～11000' not in instruction
    assert '1个CU' in instruction
    assert '系统累计检查' in instruction


def test_legacy_section_job_still_has_a_section_budget():
    context = {'section_contract': {'section_id': 'S01-01', 'target_words': {'min': 9000, 'max': 11000}}}
    assert '9000～11000' in LongformGenerationFactory._word_prompt(context)['prompt']
