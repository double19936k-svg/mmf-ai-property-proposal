# R2 Stability Risk Summary

Stable baseline: `MMF006E-R2.2-1a1c2db17c61-20260909060616`  
Current: `MMF006E-R2.4.3-4df94d1ceea6-20260909233832`

R2.2 → current tracked source delta: **39 changed + 21 added = 60 files**. No tracked files removed.

## What changed

R2.2 through R2.4.3 stacked scheduler/RPM/TPM, requirement recognition, artifact QA, workflow timing, live artifact closure, and provider socket fallback onto the same three hubs: `app_core.py`, `server.py`, `static/index.html`. Those files plus `pack_portable.py` are the hotspot set.

## Highest-risk contract split (the live bug)

The knowledge confirmation page already treats “nothing to confirm” as legal. Generation still requires at least one selected positive KU.

Live dist run `tender_20260910_000212_62888f` had **10 recommended KUs**, **0 auto-selected**, and confirmation questions only for two non-recommended outsourcing KUs. The UI therefore rendered “本次没有需要逐条确认的知识项。” and `/api/generate` failed with “请至少保留一条推荐知识。”

This is old validation + new empty-shown UX, not a missing feature. `selection_gate.py` itself was **not** changed in these rounds.

## Other risks

- Risk control / guardrails are already independent of selected positives in `generate_artifact`. Empty knowledge must not skip Fact Control, Commitment Gate, Artifact QA, or Semantic Firewall.
- Knowledge provider failure must not be labelled as “user selected none”.
- production_dist was rebuilt often; this freeze must pack again after the contract fix and test the dist, not only source.

## Out of scope

No 006D/R10, `00_项目管理与索引`, `github_release_candidate`, or MMF-007 edits found. Do not roll back the tree. Minimal contract unification only.
