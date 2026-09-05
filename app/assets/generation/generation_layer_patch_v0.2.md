# Generation Layer Patch V0.2

- 状态：`candidate_patch`
- 作用层：`generation_layer_only`
- 变更：保留 GLP-001～003，新增 GLP-004 Guardrail Internalization 与 GLP-005 Natural Property Language
- 边界：不修改 KU、B1.2、Candidate Corpus、Writing Style Model、治理规则或 Case 输入包

## GLP-001 Structure-to-Presentation

**触发条件**：When converting internally organized knowledge into user-visible scheme text (especially PPT/Word body), and the draft would otherwise leak backend field labels such as 对象/动作/责任人/成果 as sentence templates.

**生成要求**：

- Keep internal structure labels for planning only; do not copy them into visible prose as “对象为…… / 动作为……” templates.
- Choose presentation form by information shape: table for two-dimensional relations, process for sequence, measure modules for parallel actions, org chart/RACI for hierarchy.
- In Word, use natural paragraphs, subheads, short tables or processes; still forbid repeated structure-exposing sentence patterns.
- Do not force every block into a table; convert only when the relation is naturally tabular.

**媒介应用**：

- PPT: prefer table / process / measure module / responsibility matrix over labeled prose.
- Word: natural paragraphs + short tables/process; no field-name sentence loops.
- Internal checklists: field format allowed only if the current task explicitly requests that format.

**禁止模式**：

- 对象为……；动作为……；责任人为……；成果为…… stacked in body text
- Backend field names used as PPT/Word visible copy
- Maximizing tables just to look like a PPT

**允许例外**：The current task explicitly requires the field format as an internal checklist or inspection table.

**回归重点**：Case 1 opening-preparation section: confirm “对象为…… / 动作为……” disappear; convert multidimensional measures to table/process/module only when suitable.

## GLP-002 Internal-vs-Submission Separation

**触发条件**：When a bid/scheme draft contains scoring-point mapping, response notes, evidence correspondence, citation logs, or other review-aid language that would be useful internally but risky in the formal submission.

**生成要求**：

- Split output into internal review layer vs formal submission layer.
- Formal technical bid body must cover scoring requirements through chapter structure, measures, standards and implementation—not by telling evaluators which scoring point is being answered.
- Keep scoring-point response notes, mapping tables, citation registers, source logs, risk/gap reminders in internal or experiment-attachment outputs only.
- Do not claim that scoring-point language “will certainly cause bid rejection”; treat it as a delivery risk that is hidden by default.

**媒介应用**：

- Formal Word/PPT submitted to evaluators or clients: hide explicit scoring navigation.
- Internal review pack: scoring-point response, measure-standard-evidence map, citation register allowed.
- MVP experiment attachments (citation register, unused-guardrail statement, clarification list) must not be mixed into formal body A.

**禁止模式**：

- 评分点响应说明 as a standalone formal-body section
- 评分点—措施—标准—证据 correspondence tables in the submitted technical bid
- 对应评分项 / 得分点 / 本章节对应第X评分项 navigation language in formal copy

**允许例外**：The procurement/tender file explicitly requires that structure or template.

**回归重点**：Case 2: retain internal coverage of scoring items, but remove scoring-response and correspondence language from the formal technical-bid body; keep citation register as experiment attachment, labeled not part of the submitted bid.

## GLP-003 Professional-Language Grounding

**触发条件**：When generating professional action language that would invent abstract management verbs (识别/洞察/赋能/建模/研判 and similar) not present as real site work in the given knowledge or confirmed practice.

**生成要求**：

- Prefer real site actions, records, post behaviors, inspection methods and management processes over invented abstract actions.
- Language priority: (1) site action stated in the source KU; (2) Todd-confirmed property practice; (3) plain wording a site person would understand; (4) abstract management terms last.
- If a “professional” term cannot be confirmed as real, use plainer wording rather than coining a term.
- Do not add extra knowledge, numbers, or peak-time facts to sound more professional; if the pack has no specific schedule/parameters, write that they are determined from operating records and on-site use patterns.

**媒介应用**：

- Engineering/OM Word bids: equipment peaks, maintenance windows and use patterns stated as record- or experience-based judgment.
- All media: no extra management concept layered onto a simple site action.
- Citation/fabrication gates remain in force; this rule only changes wording, not knowledge inventory.

**禁止模式**：

- 组织高峰识别 and similar outsider/AI abstract actions
- Adding 识别/洞察/赋能/建模/研判 unless the business actually has that process
- Inventing a specific peak period, runtime parameter, or device pattern not in the allowed knowledge pack

**允许例外**：The business process itself truly includes that abstract step, and it is stated in the allowed KU or already confirmed by Todd as actual site practice.

**回归重点**：Case 3: search for leftover phrases like “组织高峰识别”; rewrite peak/window/schedule language as 根据运行记录判断 / 结合历史使用规律 / 由工程人员根据设备运行经验判断 / 结合现场生产或办公使用时间安排, without fabricating clock times.

## GLP-004 Guardrail Internalization

**触发条件**：When a formal bid/scheme draft would otherwise display AI-facing or internal-reviewer safety statements, coverage disclaimers, unconfirmed-hypothesis banners, or stacked risk-control reminders that belong to internal control rather than customer-facing service description.

**生成要求**：

- Keep all guardrails and risk-control constraints in force internally; do not delete, weaken, or ignore them.
- By default, hide from the formal body any safety statement written for AI or internal reviewers.
- If a boundary still needs to hold in the service logic, express the confirmation or limit in natural business wording, or move the hidden statement by nature into layer B/C/D or internal control.
- Describe the service clearly in the formal body; do not fill the submitted draft with repeated risk-prompt language.

**媒介应用**：

- Formal Word/PPT body (layer A) to clients or evaluators: hide AI/internal safety declarations; keep customer-facing service description.
- Unconfirmed assumptions and interview hypotheses: state the upcoming confirmation action in natural business language; move the explicit “not yet confirmed / do not expand or shrink scope” banner to attachments or internal control.
- Internal review pack and experiment layers B/C/D: retain full guardrail text, coverage reminders, and risk-control statements.

**禁止模式**：

- 本页不覆盖：…… as visible formal-body copy
- 应急不与培训并成同一保障模块 and similar internal module-separation reminders in the submitted draft
- 下列内容是访谈假设，不是本项目已经完成调研后的确认事实 stacked as a safety banner in formal copy
- Repeated 风险提示 / 安全提示 blocks that make the formal scheme look like an AI self-check

**允许例外**：The business truly requires the client to see that boundary (contract/scope exclusion, safety notice, or procurement-mandated disclaimer); then state it once in customer-facing business language, not as an AI/internal review banner.

**回归重点**：Case 1: remove ‘本页不覆盖’ and ‘应急不与培训’ type internal reminders from the formal body while keeping the constraints internally or in B/C/D. Case 2: naturalize unconfirmed/hypothesis boundaries and move explicit safety banners to attachments. Case 3 is not rerun.

## GLP-005 Natural Property Language

**触发条件**：When generating professional, formal, executable wording that would otherwise coin consulting-style or AI-abstract terms for a simple property action whose meaning already exists in Todd-confirmed, KU-mature, or everyday formal property language.

**生成要求**：

- Keep language professional, formal and executable, but prefer real property-business wording over newly coined abstract concepts.
- Language priority: (1) Todd-confirmed expressions; (2) mature KU expressions; (3) everyday formal property-business language; (4) general management terms; (5) newly coined concepts—principally forbidden.
- Do not invent abstract new words merely to sound professional; if the business meaning is unchanged, rewrite consulting/AI coinages into natural property language (e.g. 低干扰窗口安排 → 合理安排维保时间).
- Naturalization is wording only: do not add facts, parameters, schedules, or extra knowledge while rewriting.

**媒介应用**：

- All formal Word/PPT body text: use site-recognizable property language for actions, windows, and arrangements.
- Engineering/OM and bid measures: name the real work (维保、巡检、记录、交接) rather than a coined management construct.
- Citation/fabrication gates remain in force; this rule changes wording, not knowledge inventory.

**禁止模式**：

- Coining consulting/AI concepts such as 低干扰窗口安排 for a simple scheduling action
- Inventing abstract new terms to appear more professional when Todd-confirmed, KU, or everyday formal property wording already exists
- Adding facts, clock times, or extra measures under the pretext of naturalizing language

**允许例外**：A term is already the mature KU expression or Todd-confirmed site wording, or it is a standard general management term required by the procurement file; newly coined concepts remain principally forbidden.

**回归重点**：Case 1 and Case 2 only: confirm professional wording stays in real property language and no new abstract coinages appear. Case 3 is not rerun; reuse the V2 passing baseline (the 低干扰窗口安排 lesson is absorbed as this rule, not as a Case 3 retest).

## 使用边界

- Applies only after knowledge is already selected: expression and delivery layering, not knowledge inventory.
- Do not modify the 42 KUs, Candidate Positive, Candidate Guardrail, B1.2, Todd Knowledge Governance Preference Model, Candidate Corpus, Writing Style Model, governance rules, Case definitions, or allowed/forbidden packs.
- Do not promote this patch to Writing Style Model V0.3 or expand it into a new multi-rule style system.
- FAB and CIT gates stay unchanged: do not add extra property knowledge to sound more professional or more natural.
- V0.2 regression is Case 1 and Case 2 only; keep Case settings, allowed KU pack, Guardrail pack, Writing Style V0.2 subset, output task, model and reasoning identical; the only new variable is this patch.
- Case 3 is not rerun and continues to use the V2 passing baseline.
- Round-1 Case1_V1 / Case2_V1 / Case3_V1 remain read-only; V2 is saved separately; V3 is generated only for Case 1 and Case 2.
- GLP-001, GLP-002 and GLP-003 are retained exactly from V0.1; V0.2 adds only GLP-004 and GLP-005.
- Codex must not reinterpret Todd notes, polish V2, swap KU/Guardrail/Case settings, auto-score outputs, or upgrade any other knowledge system.

## 限制与验证状态

- Candidate patch only: not promoted to Writing Style Model V0.3 and not a new style-rule system.
- Grounded in Todd’s Case 1–3 notes; necessary conditions and exceptions are kept, not generalized into unverified industry-wide facts.
- Does not change KU, Schema, Candidate Corpus, B1.2, governance model, Writing Style Model, Case definitions, or allowed/forbidden packs.
- Does not relax FAB/CIT: sounding more professional or more natural is not a license to add facts, peak hours, or device parameters absent from the pack.
- This file does not score or judge V3 effectiveness; only Case 1 and Case 2 are rerun for V0.2, and Case 3 is not rerun and continues to use the V2 passing baseline.
- GLP-002 records a submission-layer risk (possible marking / in severe cases bid rejection) without asserting that scoring-point language always causes rejection.
- GLP-001 forbids leaking structure labels and forbids table-maximization; conversion is shape-based, not a mandate to tabulate every section.
- GLP-003 bans ungrounded abstract actions such as 组织高峰识别 in the observed context; it does not blacklist every occurrence of words like 识别 unless they invent a non-existent site process.
- GLP-004 keeps risk control in force: formal body hides AI/internal safety statements by default; confirmation logic is naturalized; hidden content moves by nature to B/C/D or internal control; exception only when the business truly requires a client-facing notice.
- GLP-005 is wording-only natural property language with the stated priority; newly coined concepts are principally forbidden and must not introduce new facts.
