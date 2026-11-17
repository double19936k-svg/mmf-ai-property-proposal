# MMF-006E-R2.1 Targeted Closure Patch

Primary implementer: Grok Build. Scope: Q01–Q04 only. Offline/mock only. No live provider calls. No GitHub push. Q05/Q06 deferred.

## Fixes

### Q01 Batch ID provider contract
- Multi-section `_batch_prompt()` now writes `CURRENT_BATCH_ID = GB-xx` into the user prompt actually sent by OpenAI-compatible HTTP JSON and Grok Bridge `--prompt-file`.
- Batch JSON schema requires `batch_id` and constrains `const` to the current ID.
- `_split_batch_output()` rejects missing, empty, or mismatched `batch_id` as `OUTPUT_CONTRACT_ERROR`. Single-section fallback/repair remains without `batch_id`.
- Mock echoes `CURRENT_BATCH_ID` from prompt text so it cannot hide a missing prompt injection.

### Q02 Quality before latency
- `classify_generation_failure` inspects MUST/scoring/QA/commitment/compliance/contradiction/depth evidence before elapsed-time markers.
- SUCCESS + hard timeout + MUST missing → `QUALITY_FAILURE` (detail: `MUST_REQUIREMENT_MISSING`).
- Hard timeout + complete content + QA PASS → `LATENCY_FAILURE`.

### Q03 Real reasoning escalation
- Product speed profile is separate from provider-native sequences.
- `next_higher_reasoning(provider, current_effective)`:
  - Grok: low → medium → high → xhigh (xhigh only in planning/complex/quality repair)
  - Kimi default/unknown model: low → high, with **high as conservative ceiling**. `max` is **not** assumed.
  - Kimi `max` only when explicitly configured deployment `supported_reasoning_levels` includes `max`. Model names/prefixes are not treated as capability evidence; no live capability probe (Q05 deferred). Codex added this small conservative guard during independent review.
  - Qwen with thinking disabled: `REASONING_ESCALATION_UNAVAILABLE` (does not invent `enable_thinking=true`)
- Unknown Kimi model quality retry: requested=true, applied=false, `QUALITY_ESCALATION_UNAVAILABLE`, outgoing `reasoning_effort=high` (not max).
- Ceiling or unsupported upgrade records `QUALITY_ESCALATION_UNAVAILABLE` / `REASONING_ESCALATION_UNAVAILABLE` instead of fake `QUALITY_ESCALATION_APPLIED`.
- Depth-only under-length continue_section still records quality escalation requested/applied (or honest UNAVAILABLE).
- Per-attempt and final audits persist `quality_escalation_requested/applied`, `reasoning_before/after`, `escalation_unavailable_reason`.

### Q04 Checkpoint consistency
- After a batch is completed (including scheduler skip/resume), it is removed from `pending_batches` and `failed_batches`/`retry_queue`.
- Invariant: `completed ∩ pending = ∅`, `completed ∩ failed = ∅`, `pending ∩ failed = ∅` (failed is retry/failed, not pending).
- `provider_calls_total` is restored across new factory instances. Zero-call resume keeps historic total and sets `this_run_calls` / `this_resume_calls` = 0.
- `_resume_session` is true for any prior attempted/failed job or historic calls, not only jobs that already have completed sections. All-empty first run (13) then successful resume (15) records `this_resume_calls=15` and `provider_calls_total=28`.

## Out of scope
- Q05: soft latency scheduling, model-specific budgets, TPM/RPM windows, live capability probe
- Q06: full `qa_seconds` / `actual_time` closeout, token statistics rebuild, LatencyHistory rebuild

## Tests
- `tests/test_mmf006e_r21_q01_q04.py` (HTTP JSON + Grok Bridge prompt intercept, fake transports only)
- Existing R2 scheduler/adversarial/orchestrator/precision tests: 55/55 source and dist passed before the final conservative guard; the final independent review records the rebuilt package result.

## Production dist verification (native `app_core.generate_artifact`, Mock only)

- TEST_TARGET = production_dist (native run_audit)
- runtime_source = dist
- Grok pass2 build_id = MMF006E-R2.1-e8d8a0e88c9b-20260908145154 (historical; final guard rebuild ID is in build_manifest.json)
- logical_section_count = 27
- generation_batch_count = 15
- provider_calls_total first = 15; this_run_calls = 15; this_resume_calls = 0
- resume measured invoke_structured = 0; provider_calls_total remains 15
- all-failed then success: 13 + 15 = 28, this_resume_calls = 15
- partial one-short-section: 16 + 1 = 17
- completed ∩ pending = ∅
- 27 fragment hashes unchanged on resume
- QUALITY_REGRESSION_GATE = PASS
- CURRENT_BATCH_ID = GB-01 present in provider prompt
- blocked empty-mock path = compliance_blocked (native)
- no live Qwen/Kimi/Grok longform run
