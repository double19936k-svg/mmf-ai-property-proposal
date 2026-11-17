"""§38–§42：Scope-Gated Requirement Authority —— Final QA 的需求分母只有一个来源。

Round 3 的 QR-001 直接吃 `coverage_status == "MAPPED"` 的规划期行集，那是**范围门之前**
的集合，于是最终 TECHNICAL_ONLY_SCOPE_GATE 已经排除的条款仍能留在 MUST 分母和
missing requirement 名单里。Run D 只读回放的真实形状（`r4_qr001_denominator_replay_runD.json`）：
分母 52 = 48 条 MUST + 4 条"只因挂评分项而入"的行（REQ-0271…0274 ↔ SCR-026…029），
后四条 §24 判为 TECHNICAL_CONTEXTUAL（处置 TECHNICAL_NOT_SCORED，本就不在技术评分分母里），
QR-001 却按正文必须响应去追 —— Round 3 点名的 REQ-0273「详细评分因素」正是其中之一。

本模块不重做范围判定：结构/预算意义上的判定仍只有 TECHNICAL_ONLY_SCOPE_GATE 一处真值，
这里只把**最终门的结论传播到每个消费端**（coverage denominator、QR-001、missing
requirement、scoring mapping、Generation Requirement Pack），并逐条登记被移除的原因，
使分母的收缩可回放对账（与 §8 的预算守恒同构）。
"""

from __future__ import annotations

import re
from typing import Any

from .content_engineering import is_excluded_technical_scope, technical_scope_disposition
from .scoring_taxonomy import SCORING_CLASS_DISPOSITION, is_denominator_class

AUTHORITY_SCHEMA_VERSION = "scope-gated-requirement-authority-v0.1"

# 分母口径常量只在这里定义一次；planner / orchestrator / 报告都取同一个字符串。
REQUIREMENT_DENOMINATOR = "SCOPE_GATED_MUST_OR_SCORING_ROWS"

# 最终门缺席时的兜底口径：仍按行上已登记的 scope_class 判定，但必须声明是兜底。
GATE_AUTHORITY = "TECHNICAL_ONLY_SCOPE_GATE"
ROW_FALLBACK_AUTHORITY = "MATRIX_ROW_SCOPE_CLASS_GATE_ABSENT"

# 这些评分类别本身不要求正文响应（§24 十类的判据），因此不得进入 MUST 分母。
# 类别名单从 §24 的处置表推导，而不是在这里再抄一份：抄一份就是第二把尺子，
# 而历史 Run D 已经证明两处各写一套会把表头行留在分母里。
NON_RESPONSE_DISPOSITIONS = frozenset({"NON_RESPONSE_ROW", "MERGED_INTO_CANONICAL"})
NON_RESPONSE_SCORING_CLASSES = frozenset(
    cls for cls, disposition in SCORING_CLASS_DISPOSITION.items()
    if disposition in NON_RESPONSE_DISPOSITIONS
)
# 规划期就没落到任何 Section 的行不可能在正文里被覆盖，留在分母只会产出假失败。
NON_ROUTED_STATUSES = frozenset({"UNMAPPED", "CONFLICT_BLOCKED"})

REMOVAL_REASONS = (
    "FINAL_GATE_EXCLUDED",
    "ROW_SCOPE_EXCLUDED",
    "SCORING_NOT_IN_TECHNICAL_DENOMINATOR",
    "NON_RESPONSE_SCORING_ROW",
    "NOT_ROUTED",
)

# §41/§42：等价证据只做**明确**的一种 —— 需求里被 Technical-Only 合同禁止写进正文的
# 分句（价格上限、资信/黑名单声明、投标程序），登记而不要求正文复述；技术实质仍必须覆盖。
# 这些模式**不改动** scope_class，因此不会另立一处范围真值，只影响一条需求的取证文本。
FORBIDDEN_BODY_FRAGMENTS = (
    ("PRICING_FRAGMENT", re.compile(r"费用预算|预算不超|报价不超|最高限价|人民币[\d,.]+\s*万[\d,.]*|[\d,.]+\s*元/(?:月|平方米|年)|含税总价|下浮率")),
    ("BIDDER_CREDENTIAL_FRAGMENT", re.compile(r"未被[^。；]{0,24}(?:黑名单|失信名单|不良记录)|列入[^。；]{0,16}(?:黑名单|失信名单)|重大违法记录|无不良记录")),
    ("BID_PROCEDURAL_FRAGMENT", re.compile(r"授权委托[书函]|法定代表人身份|密封|装订份数|正本副本|投标保证金|履约保证金|投标保函")),
)
# 技术实质的同义写法：只登记已被历史 Run 反复验证的窄口径，不做泛化近义。
TECHNICAL_EVIDENCE_ALIASES: dict[str, tuple[str, ...]] = {
    "服务期限": ("服务期", "合同期", "委托期限", "管理期限", "服务年限"),
    "人员配置": ("岗位配置", "人力配置", "人员安排", "岗位设置"),
    "响应时限": ("响应时间", "到场时限", "响应安排"),
    "投诉": ("诉求", "客户意见", "报修"),
    "台账": ("记录表", "登记表", "清册"),
}


def _row_scope_class(row: dict[str, Any]) -> str:
    # 只认行上**已登记**的 scope_class。不在这里重新判范围：那会造成第二处真值，
    # 也会让从未登记过 scope 的历史矩阵凭空改变分母。
    return str(row.get("scope_class") or "")


def _gate_views(scope_gate: dict[str, Any] | None) -> tuple[set[str], set[str], bool]:
    gate = scope_gate or {}
    excluded_ids = {
        str(row.get("requirement_id")) for row in (gate.get("excluded_register") or [])
        if row.get("requirement_id")
    }
    technical_scoring_ids = {str(sid) for sid in (gate.get("technical_scoring_item_ids") or []) if sid}
    return excluded_ids, technical_scoring_ids, bool(gate)


def scope_gated_denominator(
    matrix: dict[str, Any] | None,
    *,
    scope_gate: dict[str, Any] | None = None,
    scoring_class_by_requirement: dict[str, str] | None = None,
) -> dict[str, Any]:
    """把规划期行集换算成 Final QA 唯一可用的需求分母。

    返回值里的 `rows` 就是 QR-001 的分母；`excluded` 逐条写明是哪一类移除，
    使"分母变小"永远可审计，而不是静默放宽。
    """
    rows_in = [row for row in ((matrix or {}).get("matrix") or []) if isinstance(row, dict)]
    excluded_ids, technical_scoring_ids, gate_present = _gate_views(scope_gate)
    class_by_req = scoring_class_by_requirement or {}
    authority = GATE_AUTHORITY if gate_present else ROW_FALLBACK_AUTHORITY

    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for row in rows_in:
        req_id = str(row.get("requirement_id") or "")
        scoring_id = str(row.get("scoring_item_id") or "")
        row_scope = _row_scope_class(row)
        reasons: list[str] = []
        if gate_present and req_id and req_id in excluded_ids:
            reasons.append("FINAL_GATE_EXCLUDED")
        if not reasons and is_excluded_technical_scope(row_scope):
            reasons.append("ROW_SCOPE_EXCLUDED")
        if scoring_id and gate_present and technical_scoring_ids and scoring_id not in technical_scoring_ids:
            reasons.append("SCORING_NOT_IN_TECHNICAL_DENOMINATOR")
        if class_by_req.get(req_id) in NON_RESPONSE_SCORING_CLASSES:
            reasons.append("NON_RESPONSE_SCORING_ROW")
        # §38/§39 与 §24 的唯一连接点：一条只因"挂了评分项"才进分母的行，若 §24 判它
        # 不进技术评分分母（TECHNICAL_CONTEXTUAL / 商务 / 资信…），需求侧也必须同步离开，
        # 否则 Run D 那种"详细评分因素"表头行仍被 QR-001 当正文响应追。MUST 条款不因
        # 评分口径而免答——招标强制项与是否计分是两回事，所以 MUST 一律保留。
        scoring_class = class_by_req.get(req_id)
        must_row = str(row.get("mandatory_level") or "") == "MUST"
        if scoring_id and not must_row and scoring_class and not is_denominator_class(scoring_class) \
                and "SCORING_NOT_IN_TECHNICAL_DENOMINATOR" not in reasons \
                and "NON_RESPONSE_SCORING_ROW" not in reasons:
            reasons.append("SCORING_NOT_IN_TECHNICAL_DENOMINATOR")
        if str(row.get("coverage_status") or "") in NON_ROUTED_STATUSES:
            reasons.append("NOT_ROUTED")
        if reasons:
            removed.append({
                "requirement_id": req_id or None,
                "scoring_item_id": scoring_id or None,
                "mandatory_level": row.get("mandatory_level"),
                "scope_class": row_scope,
                "scope_disposition": technical_scope_disposition(row_scope),
                "removal_reasons": reasons,
                "scoring_class": scoring_class or None,
                "primary_section_id": row.get("primary_section_id"),
                "text": str(row.get("requirement_text") or row.get("text") or "")[:160],
            })
            continue
        kept.append(row)

    must_rows = [r for r in kept if r.get("mandatory_level") == "MUST" or r.get("scoring_item_id")]
    counts: dict[str, int] = {reason: 0 for reason in REMOVAL_REASONS}
    for row in removed:
        for reason in row["removal_reasons"]:
            counts[reason] += 1
    return {
        "schema_version": AUTHORITY_SCHEMA_VERSION,
        "denominator": REQUIREMENT_DENOMINATOR,
        "denominator_authority": authority,
        "final_scope_gate_present": gate_present,
        "matrix_rows_total": len(rows_in),
        "denominator_rows_total": len(kept),
        "must_or_scoring_total": len(must_rows),
        "removed_total": len(removed),
        "removal_counts": counts,
        # 守恒：进来的每一条要么在分母里，要么在移除名单里，不存在第三种下落。
        "conservation_holds": len(kept) + len(removed) == len(rows_in),
        "rows": kept,
        "must_or_scoring_rows": must_rows,
        "excluded": removed,
        "excluded_requirement_ids": sorted({r["requirement_id"] for r in removed if r["requirement_id"]}),
        "legacy_quoting": {
            "planned_must_before_authority": sum(
                1 for r in rows_in
                if r.get("coverage_status") == "MAPPED"
                and (r.get("mandatory_level") == "MUST" or r.get("scoring_item_id"))
            ),
            "note": "Round 3 QR-001 的口径（scope 前集合），仅作对照，不作为交付判据。",
        },
    }


_CLAUSE_BREAKS = "。；，、且"


def _clip_span(value: str, start: int, end: int) -> tuple[int, int]:
    """取最小可整段删除的单位：命中所在的括号注记，否则命中所在的分句。

    "服务期限为3年（费用预算不超人民币570万）。" 要删的是整个括注，不能把左边界一路
    扩到句首 —— 那会把"服务期限为3年"这段技术实质一起删掉。
    """
    opener = max(value.rfind("（", 0, start), value.rfind("(", 0, start))
    closer_candidates = [pos for pos in (value.find("）", end - 1), value.find(")", end - 1)) if pos >= 0]
    if opener >= 0 and closer_candidates and max(closer_candidates) >= end - 1:
        return opener, min(closer_candidates) + 1
    while start > 0 and value[start - 1] not in _CLAUSE_BREAKS:
        start -= 1
    while end < len(value) and value[end] not in _CLAUSE_BREAKS:
        end += 1
    return start, end


def split_requirement_evidence(text: Any) -> dict[str, Any]:
    """§41/§42：把一条需求拆成"技术实质取证文本"+"只登记不得写入正文的分句"。

    移除是**片段级**的，且只在取证时使用：整条需求的 scope_class 不变，因此结构、
    Section、预算仍由 TECHNICAL_ONLY_SCOPE_GATE 单点决定。
    若移除后技术实质为空，则**拒绝**这条等价（否则等于免答），仍按全文取证。
    """
    value = str(text or "")
    if not value:
        return {"technical_text": "", "registered_fragments": [], "equivalence_basis": "NO_TEXT"}
    spans: list[tuple[int, int, str]] = []
    for name, pattern in FORBIDDEN_BODY_FRAGMENTS:
        for found in pattern.finditer(value):
            spans.append(_clip_span(value, found.start(), found.end()) + (name,))
    if not spans:
        return {"technical_text": value, "registered_fragments": [], "equivalence_basis": "FULL_TEXT"}
    merged: list[tuple[int, int, str]] = []
    for start, end, name in sorted(spans):
        if merged and start <= merged[-1][1]:
            last_start, last_end, last_name = merged[-1]
            reasons = "+".join(dict.fromkeys((last_name.split("+") + name.split("+"))))
            merged[-1] = (last_start, max(last_end, end), reasons)
            continue
        merged.append((start, end, name))
    pieces: list[str] = []
    cursor = 0
    for start, end, _name in merged:
        pieces.append(value[cursor:start])
        cursor = end
    pieces.append(value[cursor:])
    technical = re.sub(r"\s+", " ", "".join(pieces)).strip()
    technical = re.sub(r'^(?:并且|且)[，、\s]*', '', technical)
    registered = [
        {"fragment": value[start:end].strip(), "reason": name}
        for start, end, name in merged
    ]
    if not technical:
        # 整条需求都是不得写入正文的内容 —— 不能靠"免答"把它算成已覆盖。
        return {
            "technical_text": value,
            "registered_fragments": registered,
            "equivalence_basis": "REJECTED_ALIAS_WOULD_EMPTY_REQUIREMENT",
        }
    return {"technical_text": technical, "registered_fragments": registered,
            "equivalence_basis": "EXPLICIT_FORBIDDEN_BODY_FRAGMENT"}


def evidence_alias_hits(technical_text: str, artifact_text: str) -> list[dict[str, Any]]:
    """已声明的窄口径等价：技术实质本体没命中、但其唯一登记别名命中时才算等价。

    只在 `keyword_coverage` 判失败之后调用，绝不替代原判定（§41：不过度放宽）。
    """
    hits: list[dict[str, Any]] = []
    source = str(technical_text or "")
    target = str(artifact_text or "")
    if not source or not target:
        return hits
    # A terminology alias cannot waive an explicit quantity or duration.
    from .scoring_context import day_value
    number = r'(\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千]+)'
    unit = r'(年|个月|月|日|天|人|%|‰)'
    def quantities(text):
        return {(float(value) if re.fullmatch(r'\d+(?:\.\d+)?',value) else day_value(value),kind)
                for value,kind in re.findall(number+r'\s*'+unit,text)}
    required = quantities(source)
    if not required.issubset(quantities(target)):
        return hits
    duration_required = {q for q in required if q[1]=='年'} if '服务期限' in source else set()
    if duration_required:
        duration_names='|'.join(map(re.escape,('服务期限',)+TECHNICAL_EVIDENCE_ALIASES['服务期限']))
        bound = {(float(value) if re.fullmatch(r'\d+(?:\.\d+)?',value) else day_value(value),'年')
                 for value in re.findall(r'(?:'+duration_names+r')(?:为|是|：|:)?\s*'+number+r'\s*年',target)}
        if not duration_required.issubset(bound):
            return hits
    for term, aliases in TECHNICAL_EVIDENCE_ALIASES.items():
        if term in source and any(alias in target for alias in aliases):
            hits.append({
                "term": term,
                "matched_alias": next(alias for alias in aliases if alias in target),
                "basis": "DECLARED_NARROW_ALIAS",
            })
    return hits
