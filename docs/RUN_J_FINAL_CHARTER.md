# Run J final charter and freeze boundary

Run J owns visual rendering and the PPT sidecar only. Its endpoint is a verified merge-ready visual capability. Run K owns integration. Run J must not merge, cherry-pick I/H, or declare Release Candidate/Release Ready.

## Frozen architecture and priority

`Visual IR -> source validation -> shared scene -> DOCX / PPT`

The canonical schema is `mmf-visual-ir-v1`; the target-independent scene is `mmf-visual-scene-v1`. Nodes, relationships, sequence, role/responsibility cells, conditions and source/CU references belong to one semantic source. Neither renderer is a second business-semantic authority.

Priority is DOCX organization structure, process flows, emergency relationships, responsibility/interface visuals, implementation timeline/Gantt, then PPT reuse. DOCX usefulness, page fit and readable Chinese take precedence over decorative PPT treatment. PPT retains the previously verified C4 insertion contract wherever compatible.

Every business node and edge binds Tender, an accepted fact, or an explicit proposed service method. Approved service design qualifies only as a source-backed accepted fact or explicit proposed method, not as an untraceable new assertion. Source binding establishes inspectable trace and integrity; independent review must still assess whether the mapping is supported. It must not invent staff, departments, relationships, facilities, SLA, dates or responsibilities.

Scoring, Final Editorial, provenance, canonical claim authority, QA thresholds, Page Band semantics and Run H artifacts are frozen boundaries. Source disagreements are backlog entries, never Run J source repairs.

## Freeze packet

The original implementation/build baseline is commit `27d9951f8fa8d6c4ab290226dcdcd741491c165d`, based on `0ec3e9cc107af903652ee2f8ff6509368fa5f2f4`. Zcode verified the original frozen HEAD `030e7e0106bcefb41f58a0f2f81ad4c93e736b30`. The subsequent observation corrections require a separate exact HEAD and independent review; the original verdict is historical evidence only.

Current local `runtime/run_j/fix_v2/RUN_J_FINAL_FREEZE.json` binds the exact final HEAD, schema/scene versions and schema hash, changed-file boundary, render/build runtime versions, font identity, Visual IR and sample hashes, source/CU file hashes and quoted-span bindings, all rendered sample pages/slides, the representative pagination probe, regression XML/logs, skipped/excluded tests and Provider calls. The original packet is archived in `runtime/run_j/history/reviewed_030e7e01/`. The full original customer/source snapshots remain in ignored local IR and exported semantic attachments. No source snapshot or customer artifact is committed.

Five real controls are S03 organization structure, S04 complaint closure, S06 fault closure, S10-01 emergency relationship diagram and S11 implementation milestones. The packet also includes real responsibility/interface and timeline examples so all six Visual IR kinds have real controls.

The existing samples contain seven DOCX diagrams/seven sample pages and seven native editable PPT diagrams/thirteen physical slides. Representative pagination is nine -> twelve pages with five added intact figures and unchanged original paragraph/table content. The IR snapshots survive DOCX customXml and PPT notes. The physical slide count and frozen footer count are recorded separately.

## Independent review gates

The user assigns the independent reviewer to Zcode and will dispatch the review command. Run J prepares that command and evidence; it does not arrange another reviewer. Zcode must be separate from the implementation author, inspect the frozen HEAD/artifact hashes, and record PASS/FAIL plus findings and the exact reviewed HEAD. Repeating implementation-author checks or observing a passing regression is not independent review. See `RUN_J_ZCODE_INDEPENDENT_GATE.md`.

| Gate | Required independent evidence |
|---|---|
| Branch/scope | Isolated Run J descends from frozen baseline; no I/H cherry-pick, merge or frozen-module change |
| Single semantic model | DOCX and PPT consume identical Visual IR; no independent target-specific business extraction |
| Evidence safety | Each business node/edge and displayed projection is supported by its exact source span/structured field; no invented business commitments |
| Five real controls | Read each actual DOCX diagram and its PPT counterpart; S10 has a real correctly directed reporting/linkage relationship graph |
| DOCX usefulness | Chinese readable, grayscale-safe, intact figure/caption, no clipping/omission; original representative paragraphs and tables retained |
| Pagination | Review measured nine -> twelve page effect; do not reinterpret Page Band or claim full-tender layout validation |
| PPT compatibility | Frozen C4 helper unchanged, existing insertion order retained, native editable visuals, full embedded semantics |
| Regression/calls | Correction v2 permitted offline result 1038 passed/197 subtests/2 skips, visual targeted 31 passed, four command failure controls, twenty generation-test-file exclusions disclosed, Provider calls zero |
| Known limits | Source discrepancies/backlog, ordinal-only Gantt, no automatic graph reflow, no native Microsoft PowerPoint execution verification are disclosed |

Any actionable visual finding must be repaired only within Run J scope, followed by relevant verification and a new freeze manifest/review HEAD. Upstream source disagreements cannot be repaired or adjudicated by Run J. Independent PASS may explicitly accept disclosed limitations where the charter permits them; no material failure may be hidden as a backlog item.

## Status sequence

1. Implementation frozen, independent review pending. Current preparation status: `RUN_J_VISUAL_RENDERING_READY_FOR_INDEPENDENT_GATE`.
2. Only a separate reviewer PASS on the exact frozen HEAD permits `RUN_J_VISUAL_RENDERING_INDEPENDENT_GATE_VERIFIED`.
3. Only after that verified gate, and with no required visual work remaining, may Run J emit `RUN_J_MERGE_READY` for Run K.

The original PASS does not approve correction v2. Current review remains pending Zcode; only its separate PASS may restore VERIFIED and MERGE_READY for the new HEAD. The user dispatches the supplied command. Run J does not arrange a reviewer or approve integration itself.
