"""MMF008A2-C：provisional 必须带着**原因**参与终态判定，而不是"列表非空就挡"。

Run F 的 11 节 `COMPLETED_PROVISIONAL` 是这个问题的实测样本：逐节读 `governance.json`，
11 节全部是 `depth_deficit=NONE`、`depth_ratio 1.22–1.83`（正文**超出**契约下限）、
`must_cover` 无缺项、required outputs/tables/process 无缺项、`meaningful_body=true`，
唯一的 BLOCK 来源是 `length_status=SECTION_OVER_LENGTH` 与整件产物级 QA。
也就是说：这一档 provisional 记录的是"预算规则的作用"，不是"正文缺一块"。

上一轮的终态门只看 provisional 列表是否非空（`derive_run_state` 读 `provisional_sections`），
于是"规则效应"与"真实内容缺口"被压成同一个 NEEDS_REVIEW 原因，两者都分不清，
也没法回答复核方的反例：只留 S01-01 一条时为什么还是挡 Clean。

本模块因此只回答一个问题——**这一节为什么 provisional**，并把答案分成两类效力：

- `REAL_CONTENT_DEFICIENCY`：正文确实缺一块（深度不足、must_cover 缺项、必需产出/表/流程缺、
  正文不成立）→ **可以继续挡 Clean**，PROVISIONAL_SECTIONS 这道门一个字节都不删。
- `RULE_DERIVED / PROTECTED_EVIDENCE / GLOBAL_RUBRIC / NON_RESPONSE`：节内该写的都写了，
  provisional 来自长度/预算规则，或来自"这条义务的举证本来就不在技术正文里"
  （跨文件证明材料 / 整本合同质量口径 / 合同约束与计分规则）→ 记录、报出，但不挡 Clean。

两点不许踩的线：
1. **不减少 Clean 的必要条件总数**。OVER_LENGTH 与整件产物级 QA 各自另有承重门
   （`PAGE_BAND_OVERSHOOT`、`FINAL_QA_BLOCKED`），本节把 provisional 的判定按原因收窄，
   不是把那次超编放过——超编仍挡，只是由该挡它的那道门挡，具名原因不换到内容门上。
2. **认不出原因就按真实缺口处理**（fail-closed）。一条 provisional 节如果落不到上面任何
   一个具名理由，它继续挡 Clean；放宽只在读得出凭据时发生。
"""
from __future__ import annotations

from typing import Any

from planning.scoring_responsiveness import (
    CONTRACTUAL_CONTEXT,
    CROSS_FILE_PROTECTED_EVIDENCE,
    HEADER_FORMULA_NON_RESPONSE,
    WHOLE_DOCUMENT_RUBRIC,
)

PROVISIONAL_SETTLEMENT_VERSION = "provisional-settlement-v0.1"

REAL_CONTENT_DEFICIENCY = "REAL_CONTENT_DEFICIENCY"
RULE_DERIVED = "RULE_DERIVED"
PROTECTED_EVIDENCE = "PROTECTED_EVIDENCE"
GLOBAL_RUBRIC = "GLOBAL_RUBRIC"
NON_RESPONSE = "NON_RESPONSE"

PROVISIONAL_REASON_CODES: tuple[str, ...] = (
    REAL_CONTENT_DEFICIENCY, RULE_DERIVED, PROTECTED_EVIDENCE, GLOBAL_RUBRIC, NON_RESPONSE,
)

# 只有这一类有挡 Clean 的效力。名单写在这里而不是散在判定分支里：
# 下一轮要改的是"哪些原因算内容缺口"，不是"要不要查 provisional"。
CLEAN_BLOCKING_PROVISIONAL_REASONS: frozenset[str] = frozenset({REAL_CONTENT_DEFICIENCY})

# 义务类别 → 不挡 Clean 的原因。类别名直接从第二维 import，不在这里另抄一份字面量：
# 上一轮五类改本轮六类时，手抄的映射会静默失效，而 import 会立刻报错。
_OBLIGATION_REASON: dict[str, str] = {
    CROSS_FILE_PROTECTED_EVIDENCE: PROTECTED_EVIDENCE,
    WHOLE_DOCUMENT_RUBRIC: GLOBAL_RUBRIC,
    CONTRACTUAL_CONTEXT: NON_RESPONSE,
    HEADER_FORMULA_NON_RESPONSE: NON_RESPONSE,
}


def missing_gate_items(gate: dict[str, Any]) -> dict[str, list[str]]:
    """节内 QA 门禁记录里**真实缺失**的内容项，逐项点名。"""
    must_cover = gate.get("must_cover") or {}
    return {
        "must_cover_missing": sorted(str(key) for key, value in must_cover.items()
                                     if str(value or "").upper() in {"MISSING", "NOT_COVERED", "ABSENT"}),
        "required_outputs_missing": [str(item) for item in (gate.get("required_outputs_missing") or []) if item],
        "required_tables_missing": [str(item) for item in (gate.get("required_tables_missing") or []) if item],
        "process_missing": [str(item) for item in (gate.get("process_missing") or []) if item],
        "empty_headings": [str(item) for item in (gate.get("empty_headings") or []) if item],
    }


def settle_provisional_section(
    section_id: str,
    *,
    gate: dict[str, Any] | None = None,
    provisional: dict[str, Any] | None = None,
    obligations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """一节 provisional → 具名原因清单 + 是否有挡 Clean 的效力。"""
    gate = gate or {}
    provisional = provisional or {}
    reasons: list[dict[str, Any]] = []

    deficit = str(provisional.get("depth_deficit") or gate.get("depth_deficit") or "")
    if deficit in {"MATERIAL", "MINOR"}:
        reasons.append({"code": REAL_CONTENT_DEFICIENCY, "basis": f"DEPTH_DEFICIT_{deficit}",
                        "evidence": {"depth_ratio": provisional.get("depth_ratio"),
                                     "content_units": provisional.get("content_units")}})
    if not gate.get("meaningful_body", True):
        reasons.append({"code": REAL_CONTENT_DEFICIENCY, "basis": "MEANINGFUL_BODY_ABSENT",
                        "evidence": {}})
    gaps = missing_gate_items(gate)
    for name, items in gaps.items():
        if name == "empty_headings" and items:
            reasons.append({"code": REAL_CONTENT_DEFICIENCY, "basis": "EMPTY_HEADINGS",
                            "evidence": {"items": items[:6]}})
        elif items:
            reasons.append({"code": REAL_CONTENT_DEFICIENCY, "basis": name.upper(),
                            "evidence": {"items": items[:6]}})

    # 义务侧：本节挂着、但按第二维"举证不在技术正文"的主题，不算内容缺口。
    for record in obligations or []:
        status = str(record.get("response_status") or record.get("status") or "").upper()
        if status in {"FULFILLED", "NOT_APPLICABLE", ""}:
            continue
        dimension = str(record.get("responsiveness_class") or "")
        code = _OBLIGATION_REASON.get(dimension)
        if code:
            reasons.append({"code": code, "basis": f"OBLIGATION_{dimension}",
                            "evidence": {"canonical_scoring_id": record.get("canonical_scoring_id"),
                                         "topic": str(record.get("display_topic") or record.get("topic") or "")[:60],
                                         "response_status": status}})

    body_gaps = [item for item in reasons if item["code"] == REAL_CONTENT_DEFICIENCY]
    if not body_gaps and str(gate.get("length_status") or "") == "SECTION_OVER_LENGTH":
        reasons.append({"code": RULE_DERIVED, "basis": "SECTION_OVER_LENGTH_RULE",
                        "evidence": {"content_units": gate.get("content_units")}})
    if not body_gaps and not any(item["code"] != RULE_DERIVED for item in reasons) \
            and str(gate.get("status") or "").upper() == "BLOCK":
        reasons.append({"code": RULE_DERIVED, "basis": "SECTION_GATE_BLOCK_WITHOUT_CONTENT_GAP",
                        "evidence": {"gate_status": gate.get("status")}})
    if not reasons:
        # 认不出原因 ⇒ 按真实缺口处理。放宽只在读得出凭据时发生。
        reasons.append({"code": REAL_CONTENT_DEFICIENCY, "basis": "UNATTRIBUTED_PROVISIONAL",
                        "evidence": {}})

    blocking = sorted({str(item["code"]) for item in reasons
                       if item["code"] in CLEAN_BLOCKING_PROVISIONAL_REASONS})
    return {
        "schema_version": PROVISIONAL_SETTLEMENT_VERSION,
        "section_id": str(section_id),
        "reasons": reasons,
        "reason_codes": sorted({str(item["code"]) for item in reasons}),
        "blocking_reasons": blocking,
        "blocks_clean": bool(blocking),
    }


def settle_provisional_sections(
    section_records: list[dict[str, Any]],
    canonical_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """整轮 provisional 节的结算读数（门消费 `blocking_section_ids`，其余照单登记）。"""
    by_section: dict[str, list[dict[str, Any]]] = {}
    for record in canonical_records or []:
        if not isinstance(record, dict):
            continue
        for section_id in (record.get("planned_response_sections") or []):
            by_section.setdefault(str(section_id), []).append(record)
    settled = [
        settle_provisional_section(
            str(item.get("section_id")),
            gate=item.get("gate") or {},
            provisional=item.get("provisional") or {},
            obligations=by_section.get(str(item.get("section_id"))) or [])
        for item in section_records if isinstance(item, dict)]
    blocking = [row["section_id"] for row in settled if row["blocks_clean"]]
    counts: dict[str, int] = {code: 0 for code in PROVISIONAL_REASON_CODES}
    for row in settled:
        for code in row["reason_codes"]:
            counts[code] = counts.get(code, 0) + 1
    return {
        "schema_version": PROVISIONAL_SETTLEMENT_VERSION,
        "sections": settled,
        "provisional_total": len(settled),
        "blocking_section_ids": sorted(blocking),
        "non_blocking_section_ids": sorted(str(row["section_id"]) for row in settled
                                           if not row["blocks_clean"]),
        "reason_code_counts": counts,
        "blocking_reason_codes": sorted(CLEAN_BLOCKING_PROVISIONAL_REASONS),
    }
