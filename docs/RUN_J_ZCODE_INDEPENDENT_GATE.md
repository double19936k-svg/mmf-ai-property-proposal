# Zcode independent review command — Run J correction v2

Review only. The user dispatches this command to Zcode. Do not modify code, frozen artifacts or original evidence; do not merge, cherry-pick, invoke providers, start longform generation, change frozen boundaries, or declare Release Candidate.

## Exact target and output locations

Worktree: D:\YourProject

Branch: run-j-visual, base 0ec3e9cc107af903652ee2f8ff6509368fa5f2f4. Read runtime/run_j/fix_v2/RUN_J_FINAL_FREEZE.json as UTF-8. Its head is the exact review target. Do not review a later HEAD or repair mismatches.

Write ALL reports, scripts, logs, XML, caches and temporary files inside:
D:\YourProject

Final reports: <output root>/REVIEW.md and REVIEW.json. Do not write at the drive workspace root, repository root, historical zcode_independent_gate, or another Run. Use a fresh child directory for each check. Helpers with Chinese literals must be UTF-8 WITH BOM for PowerShell 5.1, or ASCII only. The supplied runner is ASCII only.

## Corrections to review

The original PASS applies only to 030e7e0106bcefb41f58a0f2f81ad4c93e736b30. Original freeze/source bytes and review records are archived at runtime/run_j/history/reviewed_030e7e01/. That PASS does not approve this revision.

- OBS-1: the supplied runner explicitly decodes UTF-8, stops on errors and rejects anything other than 20 unique existing generation-test exclusions before launching tests. Invalid JSON, empty/duplicate exclusions and missing paths have real PowerShell 5.1 regression controls. Never run an unrestricted suite.
- OBS-2: 17 unreferenced iteration PNGs were moved to runtime/run_j/history/unused_iteration_assets/; hashes and old/new paths are recorded in history/UNUSED_ASSET_ARCHIVE.json. Formal assets contain only seven referenced images.
- OBS-3: native PPT text uses supported zero insets and wrap:"none", honoring the shared scene's explicit lines. Inspect slide 12 for complete one-line 确认作业面就绪 and 归集全周期成果. Inspect every other visual slide for overflow. IR, scene geometry, font size, DOCX, pagination and frozen C4 semantics are unchanged.
- OBS-4 and source disagreements: S04's existing unrelated table and S04/S11 differences remain upstream backlog; do not repair or adjudicate them.

Read the charter, IR contract, delivery/backlog, correction report, branch diff and full IR. Independently verify all seven embedded IRs, original source pointers/spans and every business edge. Review five real controls in DOCX-first order, especially S10's nine directed relationships. Inspect seven DOCX pages, thirteen PPT slides and five pagination figures. DOCX/pagination are reused byte-for-byte and bound by the new freeze. No longform generation was performed.

## PowerShell commands

Initialize in the shell; do not save Chinese literals in an ANSI or BOM-less PowerShell 5.1 file.

```powershell
$ErrorActionPreference = 'Stop'
$runJRoot = 'D:\YourProject'
$runJManifest = Join-Path $runJRoot 'runtime/run_j/fix_v2/RUN_J_FINAL_FREEZE.json'
$runJOutput = Join-Path $runJRoot 'runtime/run_j/agent_outputs/zcode/j2'
Set-Location -LiteralPath $runJRoot
$runJFreeze = Get-Content -Raw -Encoding UTF8 -LiteralPath $runJManifest | ConvertFrom-Json -ErrorAction Stop
$runJPowerShell = 'C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
$runJPython = 'C:\YourProject'
& $runJPython (Join-Path $runJRoot 'tools/verify_run_j_freeze.py') --manifest $runJManifest
if ($LASTEXITCODE -ne 0) { throw 'Frozen target mismatch.' }
& $runJPowerShell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $runJRoot 'tools/run_j_gate_checks.ps1') -Manifest $runJManifest -OutputDirectory (Join-Path $runJOutput 'p') -Mode ValidateOnly
if ($LASTEXITCODE -ne 0) { throw 'Review plan invalid; stop.' }
& $runJPowerShell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $runJRoot 'tools/run_j_gate_checks.ps1') -Manifest $runJManifest -OutputDirectory (Join-Path $runJOutput 'v') -Mode Targeted
if ($LASTEXITCODE -ne 0) { throw 'Targeted check failed; stop.' }
& $runJPowerShell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $runJRoot 'tools/run_j_gate_checks.ps1') -Manifest $runJManifest -OutputDirectory (Join-Path $runJOutput 'o') -Mode Offline
if ($LASTEXITCODE -ne 0) { throw 'Offline check failed; stop.' }
& $runJPython (Join-Path $runJRoot 'tools/verify_run_j_freeze.py') --manifest $runJManifest
if ($LASTEXITCODE -ne 0) { throw 'Freeze changed during review.' }
```

The runner sets provider lockdown, absolute TEMP/TMP and pytest basetemp/XML, disables cache output, and sets a defensive scale-evidence directory. Inspect execution_plan.json and XML/logs; confirm all 20 generation exclusions reached pytest. Offline checks execute the four unchanged root-redirect containment tests separately with a fixture package and sibling TEMP inside the agent output directory; they are delegated, not excluded. Require tests.xml/tests.log plus containment.xml/containment.log: 1034 + 4 passed, 197 subtests and 2 historical skips. Visual targeted: 31 passed. Permitted offline: 1038 passed / 197 subtests / 2 historical skips, including four command failure controls. These counts apply only to this revision. No provider calls, installation, regeneration or repeated 300-Class generation is authorized.

Inspect exported customXml, PPT notes, native text body settings and connectors directly. The artifact verification helper rewrites its report; do not run it against frozen evidence. Keep all fresh inspection results in the output root. Unavailable native PowerPoint execution and representative pagination remain disclosed limits; this gate does not approve source authority or full-tender Page Band.

## Verdict

Record reviewer Zcode, exact HEAD and manifest hash; PASS/FAIL/INCOMPLETE for every charter gate; all observations and resolution; source/artifact references; five controls, S10 result, pagination, regression counts, exclusions/skips, Provider calls and limitations.

Only a separate PASS on this exact target permits RUN_J_VISUAL_RENDERING_INDEPENDENT_GATE_VERIFIED. Otherwise end FAILED or INCOMPLETE with actionable findings. Do not declare MERGE_READY or Release Candidate. Run J decides merge readiness after review; Run K owns integration.


ExecutionPolicy Bypass applies only to each child check process; do not change the machine/user execution policy.

Keep these short p/v/o check names and the runner's t temporary directory: the native LibreOffice runtime crashes with the deeper old temporary path on this machine. Shorter paths retain all evidence inside the dedicated agent directory. Do not change pagination code or suppress its test.
