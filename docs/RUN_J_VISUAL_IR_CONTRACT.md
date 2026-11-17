# Run J Visual IR v1

Status: candidate for independent visual gate. Base: `0ec3e9cc107af903652ee2f8ff6509368fa5f2f4`. No mainline integration, release declaration, authority promotion or provider call.

## One semantic source, two render targets

`Visual IR -> validate -> compile_scene -> DOCX PNG + embedded IR / PPT native shapes + IR notes`.

`app/visuals/visual_ir.schema.json` defines the JSON envelope. `ir.validate` adds source-file, exact field/span, graph, ordering and role checks beyond JSON Schema. `scene.compile_scene` owns all geometry and line wrapping. Neither target derives business content or abbreviates labels.

| Kind | Input semantics | Render behavior |
|---|---|---|
| ORG_CHART | Nodes and evidenced REPORTS_TO edges | One-root reporting hierarchy, explicit positions |
| PROCESS_FLOW | Main sequence, conditional/return edges, explicit branch nodes | Directed flow, branches kept, no arbitrary five-step cap |
| RELATIONSHIP_DIAGRAM | Typed relationships and source references | Explicit reviewed relationship graph |
| RESPONSIBILITY_MATRIX_VISUAL | Role identities, responsibility rows and complete role cells | Editable grid in PPT, printable figure in DOCX |
| TIMELINE | Ordered milestones and conditions | Ordered flow retaining each milestone |
| MILESTONE_GANTT | Milestones, dependencies, conditions, time_basis | Ordinal milestone grid; no invented dates/durations |

Calendar-date fields can be represented and must literally bind source dates. The v1 renderer supports ordinal milestone Gantt only; a calendar-scaled renderer requires separate review and currently fails explicitly. Ordinal widths never imply proportional duration.

## Required semantic identities and evidence

Every visual has `schema_version`, `visual_id`, `section_id`, `kind`, `title`, `sources`, `nodes`, `relationships`, and root `evidence`. Optional `roles`, `responsibilities`, `sequence`, `groups`, `caption`, and `layout_hints` are part of the same IR, not target-specific logic.

Each source records source/CU identity, one of TENDER / ACCEPTED_FACT / GENERATED_PROPOSED_METHOD / APPROVED_SERVICE_DESIGN, relative original file, raw SHA256, complete portable snapshot, and canonical snapshot digest. An optional identity_pointer validates the source's own identity. Generated CU files additionally retain their actual CU identity.

Each business node, edge, group, role and responsibility cell has evidence containing `source_id`, RFC 6901 `pointer`, Unicode code-point `span=[start,end)`, and the exact `quote`. Validation rejects missing evidence, changed source files, changed snapshots, nonexistent pointers, mismatched spans, dangling/duplicate edges or nodes, invalid reporting trees and dropped ordering.

Nonliteral displayed labels or captions require `reviewed_projection=true`. This flag means the author supplied an explicit inspectable mapping; it is not an approval, provenance verdict, semantic authority decision or proof that a relation is true. Run J real mappings live in `tools/run_j_visual_cases.py`. Source values and complete snapshots survive DOCX customXml and PPT notes. Independent business gate still reviews every mapping and edge. Renderers never extract a graph from arbitrary prose.

The existing structured CU steps, matrix cells and schedule rows are used directly. S10's explicit responsibility-chain sentence supplies its reporting edges because no structured relationship graph exists; its nodes and duties also bind the existing responsibility matrix. No inference from unrelated narrative occurs.

## Layout and target behavior

The scene compiler rejects overlapping nodes/labels, edges crossing text or unrelated nodes, endpoints detached from their business nodes, out-of-page geometry and text that does not fit. It preserves all nodes, full source details and conditions. It never truncates, slices a graph or silently shrinks unreadable labels.

DOCX uses a 288-DPI grayscale-safe PNG with Chinese font measurement, alt text and complete semantic customXml. Caption and image stay together. Available page width and height are measured; minimum printed label is 9 pt. A figure moves as an intact block when insufficient space remains. Optional dependency: `app/visuals/requirements.txt`; the bundled artifact runtime includes Pillow. Provide a Chinese font through `MMF_VISUAL_FONT_PATH` or the renderer argument.

PPT uses editable text, rectangles and native directed connectors, consuming the exact same scene. It verifies both semantic and geometry digests and retains full IR in notes. The renderer does not make its own business mapping or layout decisions. Native export uses tailEnd for the destination arrow; the test suite verifies this library-specific direction explicitly.

## Existing assembly and C4 boundary

`build_docx.py` accepts optional `sections[].visuals`; absent visuals retain the original assembly path. `visual_slide` maps a validated IR to a `layout=visual` production slide. `build_ppt.mjs` changes only the drawing dispatch and routing record. Cover, chapters, first-seen Divider and Closing statements retain the frozen insertion behavior.

`app/visuals/c4_insertion_contract.py` is a byte-for-byte copy of the independently verified MMF-008C4 helper. SHA256: `7bbb9f3a1a9717134250b37699ea498bd4d291539757278f09d66c70a8da2d11`. Source: `MMF_Project_Records/MMF-008/Grok_PPT_Sidecar_008C4_20260927/scratch/production_ppt_insertion_contract.py`. Physical page count is distinct from the frozen footer variable, which excludes Closing. No rule redesign or new IR Divider insertion.

The generation factory, scheduler, editorial, scoring, provenance, Current Authority, Page Band, R46/R47 and QA thresholds have no changes. Callers must supply a source-validated IR explicitly. This is an offline visual/PPT sidecar candidate, not automatic production visual generation.

## Offline reproduction

1. Run `tools/run_j_visual_cases.py --source <frozen Run G snapshot> --output <isolated directory>` using the bundled Python runtime. Outputs include shared IR, DOCX samples and production PPT input. Networking is denied in the fixture process.
2. Render the PPT input with the existing `app/build_ppt.mjs` and the configured bundled Node dependencies.
3. Validate/export through `tools/run_j_finalize.mjs` and inspect all actual rendered slides and DOCX pages.
4. Run `tools/verify_run_j_artifacts.py` against the exported packages and original snapshot. It independently verifies native objects, full source/semantic retention, physical C4 count and the existing S10 visual-presence predicate.
5. Run visual tests and the permitted offline regression. Exclude any test that starts longform generation under the user's no-new-300-Class constraint. Record exclusions, skips and any abort without presenting the run as an unqualified full regression.

Customer snapshots, diagrams and artifacts live only in ignored local `runtime/run_j/`; they are not committed. Synthetic contract fixtures are inline in `tests/test_run_j_visuals.py`.
