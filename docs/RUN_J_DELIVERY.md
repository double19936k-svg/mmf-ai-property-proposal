# Run J independent visual gate candidate

Preparation status: `RUN_J_VISUAL_RENDERING_READY_FOR_INDEPENDENT_GATE`.
The final charter requires an independent PASS before `RUN_J_VISUAL_RENDERING_INDEPENDENT_GATE_VERIFIED`, and only then `RUN_J_MERGE_READY`. Neither later status is claimed by this implementation report. See `RUN_J_FINAL_CHARTER.md` for the frozen gate boundary.

Branch: `run-j-visual`, isolated checkout `D:\YourProject/run-j-visual`, based on `0ec3e9cc107af903652ee2f8ff6509368fa5f2f4`. Delivery HEAD and artifact hashes are recorded in local `runtime/run_j/RUN_J_DELIVERY_REPORT.json` after commit. No mainline merge or Release Ready declaration. Provider calls: **0**.

## Implemented boundary

One evidence-bound Visual IR and one scene compiler feed both DOCX and PPT. Six supported kinds: ORG_CHART, PROCESS_FLOW, RELATIONSHIP_DIAGRAM, RESPONSIBILITY_MATRIX_VISUAL, TIMELINE, MILESTONE_GANTT. See `RUN_J_VISUAL_IR_CONTRACT.md` and `app/visuals/visual_ir.schema.json`.

Visual-related changes only:

- `app/visuals/`: IR validation, shared geometry, DOCX rendering/embedded semantics, PPT input adapter, optional Pillow requirements, and unchanged frozen C4 helper.
- `app/build_docx.py`: optional explicit `sections[].visuals` embedding.
- `app/ppt_renderer_v2/visuals.mjs`, `app/build_ppt.mjs`: editable native visual scene drawing and optional drawing dispatch.
- `tools/run_j_visual_cases.py`, `run_j_pagination_probe.py`, `run_j_finalize.mjs`, `verify_run_j_artifacts.py`: offline real-source fixtures and artifact verification.
- `tests/test_run_j_visuals.py` and Run J contract/backlog/delivery documents.

No changes to generation scheduling, scoring, editorial, provenance, Current Authority, R46/R47, QA thresholds, Page Band or Run H. No independent graph logic in the two target renderers. No prose guessing when structured steps, rows or matrices exist.

## Real tender cases

The existing frozen Run G snapshot is read only. Source JSON paths, CU identities, raw file hashes, field pointers and exact quoted spans accompany the complete source snapshots in the portable IR. Real customer artifacts remain ignored local evidence.

| Case | Actual structured sources | Diagram | DOCX / PPT content page |
|---|---|---|---|
| S03 organization | S03-01 CU01 hierarchy/reporting table | Reporting organization chart; aggregate layers, no invented positions or headcount | 1 / 2 |
| S04 complaint closure | S04-02 CU01 steps, CU10 revisit/closure table | Six main stages and unaccepted-result branch; evidence prerequisites retained | 2 / 4 |
| S06 fault closure | S06-02 CU02 steps, CU03 verification checklist | Six stages; failed retest returns to grading; missing evidence does not close | 3 / 6 |
| S10 emergency linkage | S10-01 CU01 explicit responsibility chain, CU07 duties matrix, CU04 external activation step | Nine nodes and nine relationships, internal reporting, division of work, external/client and communication interfaces | 4 / 8 |
| S11 implementation | S11-01 CU01 four-row milestone schedule | Ordered milestone Gantt with source conditions; expressly non-calendar, no invented dates/SLA/durations | 5 / 10 |

S11 also supplies timeline and responsibility/interface matrix samples (DOCX 6/7, PPT 11/12), so all six contract kinds have real-source examples. S11's prose/table milestone count difference is registered in backlog; no fifth node is guessed.

## Artifacts and visual checks

Local `runtime/run_j/evidence/` contains:

- `visual_ir.json`: the same seven semantic objects consumed by both targets.
- `RUN_J_VISUAL_SAMPLES.docx`: seven printable diagrams across seven rendered pages, Chinese labels measured against an actual font, grayscale-safe layout, all IR snapshots in customXml.
- `RUN_J_VISUAL_SAMPLES.pptx`: seven editable diagrams across thirteen physical slides (seven content, four frozen Dividers, Cover and Closing), full IR in notes. Existing C4 footer total remains twelve because its frozen contract excludes Closing.
- `artifact_verification.json`: final-package semantic digest parity, full source preservation, unchanged original source hashes, native objects/arrows, C4 physical count and S10 visual presence: PASS.
- `docx_render/`, `ppt_render/`: actual rendered pages/slides inspected for Chinese readability, arrow direction, label association and page fit.
- `PAGINATION_BASELINE.docx`, `PAGINATION_VISUAL.docx` and their rendered PDFs: actual DOCX builder with identical unedited first paragraph/first table excerpts from each of five sections, adding only five diagrams.
- `pagination_verification.json`: baseline nine pages -> visual twelve pages; original paragraphs retained in order, original native table cells unchanged, five intact figures. This is a representative pagination probe, not a full tender/Page Band gate.

PPT finalization receipt is `runtime/run_j/finalizer/validation.json`: final bytes verified, thirteen slides, package integrity and geometry pass, no structural/layout findings or warnings, successful artifact-tool import. Native Microsoft PowerPoint execution/font rendering was not verified. Native diagrams use editable text, rectangles and connectors; automatic graph reflow after arbitrary manual edits is not supplied.

S10-01 now contains a real relationship diagram in both outputs. The unchanged existing `visual_output_present` predicate accepts its actual DOCX diagram asset. Run J does not clear the canonical REAL_CONTENT_DEFICIENCY verdict, promote authority, change a QA threshold, or certify business semantics; independent gate ownership remains intact.

## Regression evidence and limits

- Permitted offline regression: **1033 passed, 2 skipped, 197 subtests passed**; `runtime/run_j/offline_final.xml` / `.log`.
- Visual-specific regression after final S10 label placement adjustment: **30 passed**; `runtime/run_j/targeted_final.xml`.
- Twenty entire test files that directly invoke longform/Word generation were excluded under the user's no-new-generation constraint; exact paths in `runtime/run_j/excluded_generation_tests.txt`. This is not an unrestricted full-suite pass.
- Two skips are existing project-record-dependent evidence consistency and Run D replay tests; isolated checkout does not include their production records. No test skip logic was changed.
- An initial unrestricted run was stopped before the scale-generation tests; its partial log remains `runtime/run_j/full.log`. Three missing-fixture failures in the first offline run were resolved by copying two unchanged historical DOCX fixtures into ignored local test directories with verified identical hashes. Product source and tests were not altered to mask failures.
- Fixture construction denies network connection/DNS; regression uses existing provider lockdown. No generation command, new 300-Class run, provider request, image-generation call or external API call was used.

The original baseline checkout was still clean at its original frozen HEAD during the final read-only boundary check. Changes and commits reside exclusively on Run J. Customer evidence is not staged or committed. Source disagreements are backlog entries, not Run J fixes.

## Independent gate input

Review the source-bound node/edge mappings and full embedded IR, Chinese print readability, S10 relation direction, editable PPT behavior and representative pagination effects. Dense graphs/calendar Gantt fail explicitly and need a separate visual extension. Automatic production generation wiring is intentionally outside this branch's scope.
