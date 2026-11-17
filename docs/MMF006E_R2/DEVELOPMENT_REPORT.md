# MMF-006E-R2 Development Report (pass 2 defect repair)

Pass 2 fixed scheduler DAG/rate-retry bounds, hard-dependency batching, OpenAI/Grok transport, quality blocking, malformed batch handling, checkpoint/ETA, UI speed radios, and replaced synthetic dist smoke with real `app_core.generate_artifact` full_longform on production_dist.

# MMF-006E-R2 Development Report

Adaptive Longform Scheduling / Generation Batching / Reasoning & Latency Optimization V0.1

Primary implementer: Grok Build. Offline/mock only. No real provider calls. No GitHub push.

## What changed

- Logical sections remain the mapping/QA/budget/artifact unit.
- Generation Batch Layer plans provider calls independently of section count.
- Adaptive section count replaces hardcoded 27. Simple fixtures stay in 12–18; complex fixtures can use the full catalog (25–35).
- Stage-aware reasoning: planning policy is high but executed by the local deterministic planner (no provider planning call). Draft uses 快速/均衡/深度. Quality failure escalates; latency failure downgrades; rate limit reduces concurrency without cancelling healthy workers.
- Checkpoint v0.2 records batch + section completion. Resume skips PASS sections, including members of a partially completed batch.
- UI exposes 快速 / 均衡（推荐） / 深度, ETA range or “暂无足够历史数据”, and advanced concurrency/reasoning mapping without showing unsupported thinking as enabled.
- Post-processing replacements are idempotent.

## Measured offline mock result (complex fixture)

- logical_section_count = 27
- generation_batch_count = 11
- provider_call_count = 11 (real `invoke_structured`)
- SECTIONS_PER_PROVIDER_CALL = 2.45
- QUALITY_REGRESSION_GATE = PASS
- planning_provider_invoked = false

Simple fixture selected_section_count = 16 (not 27).

## Provider mapping (draft)

- Qwen: thinking remains disabled unless extra_options explicitly enable it. UI does not show 深度推理已开启.
- Kimi: 快速=low, 均衡=high, 深度=high. max reserved for planning/complex repair policy.
- Grok: 快速=low, 均衡=medium, 深度=high. xhigh is not used for draft.

## Tests

- `tests/test_mmf006e_r2_scheduler.py` A–I coverage (offline)
- existing `tests/test_longform_orchestrator.py` still PASS
- production_dist smoke: `runtime/mmf006e_r2_tests/run_production_dist_smoke.py`

## Known limitations

- Context window / token / rate numbers are conservative documented assumptions, not live-verified SLAs.
- Concurrent workers share in-memory incremental summaries only after a section commits; independent batches do not wait on each other’s prose.
- PPT generation is still per-slide (out of R2 Word batching scope).
- No live Qwen/Kimi/Grok longform runs in this pass.
