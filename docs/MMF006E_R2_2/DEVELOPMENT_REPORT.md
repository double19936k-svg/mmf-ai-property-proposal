# MMF-006E-R2.2 Remaining Issues Closure

Primary implementer: Grok Build. Scope: Q05, Q06, and Bridge completed-response classification. Offline/mock plus Bridge fixture tests. No live Qwen/Kimi/Grok longform. No GitHub push.

Baseline: `MMF006E-R2.1-e7329550b204-20260908150816`. Q01–Q04 contracts are preserved.

## Q05 performance mechanisms

### Soft latency scheduling
`ConcurrentBatchScheduler.observe_soft_latency()` changes subsequent scheduling: future concurrency is reduced, healthy in-flight workers are not cancelled, and original logical batch IDs/dependencies remain. At parallelism 1, future large batches may split while keeping original batch IDs. Soft observations do not lower reasoning before output QA. Two consecutive within-budget successes restore one worker (`BOUNDED_RECOVERY`).

### Model-specific budgets
`latency_budget_seconds` now keys provider + exact model + speed + stage + batch size. Deployment `extra_options.latency_budget` / `soft_latency_budget` overrides win. Unknown models use provider-family assumed budgets with `provenance=unknown_model_assumed` and `assumed_unknown_model=true`; they do not invent vendor SLAs. Optional measured history can adjust budgets when enough matching samples exist.

### RPM/TPM admission
`providers/rate_limit.py` admits **before** every `invoke_structured`, including retries/repairs. Sliding 60s window, reservation, conservative reconcile, bounded wait (no busy-spin), deadline, cancellation. Configured RPM/TPM are enforced; missing limits are `unknown_limits` and **not** claimed enforced. One request larger than TPM fails immediately with `RATE_LIMIT_OVERSIZED` instead of deadlock. Context key hashes provider+model+account_ref without secrets. Retry-After from HTTP 429 is carried on `ProviderError`.

### Capability probing
`providers/capability_probe.py` is wired into `ProviderManager.health()`. It persists observed model, structured/token/rate-limit exposure, provenance, timestamp/TTL. It does **not** infer max context, TPM, RPM, or native max from a single success; those remain `unverified_fields`. Unconfigured/disabled providers are `UNAVAILABLE`, not fake PASS. Model-name prefixes still do not enable unsupported reasoning (R2.1 Q03 preserved). No mandatory live probe per generation.

## Q06 truthful telemetry

`qa_seconds` and `actual_time` are numeric on success and blocked/failure paths. Planning, generation wall, provider-wait worker-seconds, QA, and render are recorded separately. Provider wait is the sum of worker durations and is not added into wall time. `this_run_calls` / `this_resume_calls` / `provider_calls_total` remain distinct. Repair not called is `repair_reasoning="not_invoked"`. Partial `run_audit.json` is written on planning/QA/render failure without masking the original error.

Token totals prefer actual provider usage including zero; otherwise numeric estimates with `token_provenance`. Strings `"estimate"` are not used as numeric totals. Cost is never treated as verified pricing. Grok `modelUsage` input/output are used; cache-read tokens are stored separately and not added onto input.

LatencyHistory is appended only after final compliance/content/artifact gates. Failed final QA is not `eligible_for_eta`. Persistence is lock-file atomic, skips duplicate terminal IDs, and ignores corrupt/legacy rows. ETA matches provider+model+profile+stage **and** size bucket (`single_1` / `small_2_8` / `medium_9_19` / `full_20_plus`). Zero-generation resume does not write a production sample.

## Bridge completion truthfulness

`evaluate_completed_response` runs **before** generic exit-warning classification. A valid envelope, expected model, normal stop reason, and nonempty structured output are accepted even when CLI exit is nonzero and stderr contains known auxiliary exit-sync warnings (`Final signal sync timed out`, `Resident session actor ... DeadFailed`, `proactive bundle sync`). Original exit code and warnings stay in audit. Auth uses CLI error channels, not legitimate body text (`401` in business JSON is not login-required). Access denied is `PERMISSION_RESTRICTED`. Completed invocations are not retried solely due to auxiliary sync. `thought` is stripped from persisted envelopes.

## Packer
Preserve list now includes `runtime`, `output`, `config`, `runs`, and `logs`. Local `providers.local.json` / `user_settings.json` are restored into the existing dist working copy and excluded from public leak claims. Recursive delete is refused if the resolved dist path is not the exact portable root or is a junction.

## First implementation tests (historical)
- Source: 75/75 PASS (`runtime/python/Scripts/python.exe -B`)
- Dist cwd: 75/75 PASS
- New module: `tests/test_mmf006e_r22_q05_q06_bridge.py` (20)
- Existing Q01–Q04 / scheduler / adversarial / orchestrator / precision regressions: 55

## First implementation Mock measurements (historical, not current Build authority)

- Build ID: `MMF006E-R2.2-0994ee954740-20260908232556`
- TEST_TARGET = production_dist; runtime_source = dist
- First complete: 27 fragments / 15 calls; qa_seconds=0.032; actual_time=0.491; repair_reasoning=not_invoked; tokens numeric (64158/11741, provenance=provider_usage)
- Zero-call resume: 0 new invokes; 27 fragment hashes unchanged; completed ∩ pending = ∅
- Empty-mock blocked: 13 calls, compliance_blocked, numeric qa_seconds/actual_time
- Failed then success: 13+15=28, this_resume_calls=15

## Independent-review repair (same R2.2 scope)

- Sliding window prune is half-open; Retry-After is a per-context cooldown even without RPM/TPM; admission deadline follows request timeout budget; invalid/zero/negative limits are rejected, not enforced.
- Bridge requires explicit normal stopReason; leftover stderr after stripping known aux-sync lines still fails (auth, segfault, mixed); schema checks type/const/required/properties.
- Probe allowlists public observation keys so `token_usage_availability` is kept; failed health invalidates cached PASS; observed_model is the configured model; tiny synthetic structured ping is used; unverified vendor ceilings remain unknown.
- ETA unknown-model queries do not pool other models; missing plan size is `unknown_size`; history-adjusted budgets use provider_attempt samples only and never override deployment budgets with full-document wall times.
- Provider call counters increment only immediately before `invoke_structured`; admission wait is separate; oversized/invalid admission does not count as a call and does not quality-retry.
- Token accountant is locked, records failed attempts, restores cumulative on resume, and keeps exact zero.
- Soft latency only reduces future concurrency/splits; it does not lower reasoning before output QA.
- Partial resume samples are ineligible for full-document ETA; terminal history IDs include invocation_id; unexpected exceptions write partial audits without being labelled as planning.

## Limitations
- RPM/TPM are enforced only when deployment-configured; live vendor quotas remain unverified.
- Capability probe does not claim discovered max context or native ceilings.
- No real-provider longform run in this closure.
- Historical R2 / R2.1 reports were not rewritten.

## Final independent guards

Codex independently reran the second implementation's 82 tests and 10 previously failing boundary cases, all passing. Final narrow guards added 6 tests (88 official total): passive status reads never generate a probe; explicit connection probes have a 128-token/30-second request budget, exact response contract and failure status; finalized clean per-attempt samples feed matching batch budgets without entering full-document ETA; local scheduler settings are not sent as unsupported vendor API fields. Real vendor ceilings remain unknown, never guessed. Current Build authority is build_manifest.json; final independent evidence is in runtime/mmf006e_r22_control, not the historical measurements above.
