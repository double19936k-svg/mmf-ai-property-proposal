# Changelog

## v0.1.1-alpha

R2 Stability & Performance Release. Initial Deployable Alpha — not production ready.

### Added

- Adaptive long-form planning
- Generation batching
- Fast / Balanced / Deep profiles
- Concurrent provider execution
- Workflow timing
- Fail-soft delivery
- Reviewable artifacts
- Artifact preservation

### Improved

- Provider-independent orchestration
- Long-form generation performance
- Repair budgeting
- Final artifact QA
- Requirement recognition stability

### Release hygiene (2026-11-17)

Documentation only. The 2026-09-13 package named by `build_manifest.json` was not rebuilt, and no new SHA256 was produced.

- Kimi is documented as a usable text provider with `MOONSHOT_API_KEY` and model `kimi-k3`
- User guide, README and SECURITY distinguish local UI, cloud providers and Grok Bridge
- Developer machine paths were removed from tracked files

### Fixed

- One-shot generation bypass
- Production dist mismatch
- QA false terminal failures
- Knowledge zero-selection blocking
- History timing mismatch
- Open output folder
- Multiple artifact QA / serialization issues
