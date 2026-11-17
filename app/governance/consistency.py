"""MMF-007 Phase 2：Cross-Section Consistency Engine（V1.1 GLOBAL_CONSISTENCY_QA）。

对长文全部片段做规范事实一致性核查：以 Canonical Facts / Global State 为唯一事实源，
扫描各 Section 的数值与关键术语漂移。输出 consistency_registry.json（事实登记）与
consistency_issues.json（问题清单）。可确定来源的冲突给 Local Rewrite 建议；无法确定
的进 NEEDS_REVIEW；核心事实重大冲突允许 Terminal Block（遵守 Fail-Soft 边界）。
"""
from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from qa_severity import CLASS_FALSE_POSITIVE, clause_of, semantic_status

# §33：命中的数值/名称处在否定、引述或条件语境时，只是"被提到"而不是"被使用"，
# 不能据此判定与 Canonical Facts 冲突（"不是 30 分钟内到场" / "若建筑面积为…"）。
_NON_ASSERTIVE = {"DENIED", "MENTION", "CONDITIONAL"}
_NON_ASSERTIVE_REASONS = {
    "DENIED": "命中的事实值处在否定语境（不/未/无/非 + 禁止类措辞），不是对事实的主张。",
    "MENTION": "命中的事实值只是被引述或被质疑（被当成/表述为/而非/避免），不是对事实的主张。",
    "CONDITIONAL": "命中的事实值处在条件句里（如果/若/无论是否），尚未成立，不构成漂移。",
}

NUMBER_PATTERNS = {
    "gross_area_sqm": re.compile(r"(?:总建筑面积|建筑面积|管理面积)\s*[：:]?\s*([0-9][0-9,，.]*\s*(?:万平方米|平方米|㎡|万㎡))"),
    "team_headcount": re.compile(r"(?:人员配置|配备人员|项目人员|配置人数)\s*[：:]?\s*(?:不少于|不超过|约为?)?\s*([0-9]+)\s*人"),
    "service_hours": re.compile(r"(?:服务时间|值守时间)\s*[：:]?\s*([0-9]{1,2}(?::[0-9]{2})?\s*[-—~至]\s*[0-9]{1,2}(?::[0-9]{2})?)"),
    "response_minutes": re.compile(r"([0-9]+)\s*分钟内(?:到达|到场|响应)"),
}
# Blocker 7（Round 2）：Canonical Key Registry —— 全模块统一 key，alias 归一。
CANONICAL_KEY_REGISTRY = {
    "project_name": [],
    "address": [],
    "gross_area_sqm": ["area_sqm", "gross_area", "managed_area"],
    "property_types": ["property_type"],
    "service_scope": [],
    "contract_period": ["contract_duration"],
    "team_headcount": ["headcount", "staffing"],
    "department_names": ["departments"],
    "service_hours": [],
    "response_minutes": ["response_time"],
    "sla_kpi": ["kpi"],
}
REGISTRY_KEYS = list(CANONICAL_KEY_REGISTRY.keys())
REGISTRY_KEYS_ALL = list(REGISTRY_KEYS)

# "不少于/不超过/至少/最多" 是数量修饰而不是否定：45 人写在"配置人数不超过 45 人"里
# 仍然是一个需要跟 Canonical 比对的事实值，不能被 §33 的否定判据放过。
_QUANTIFIER_HEAD = re.compile(r"(?:不少于|不超过|不低于|不高于|不少于约?|至少|最多|约|以上|以下|以内|左右)\s*$")


def _clause_status(text: str, start: int, end: int) -> str:
    """命中片段的语义档位；数量修饰前缀直接按主张处理，其余交给 qa_severity。"""
    head = re.sub(r"\s+", "", str(text)[max(0, start - 12):start])
    if _QUANTIFIER_HEAD.search(head):
        return "ASSERTED"
    return semantic_status(text, start, end)


def _suppressed_row(kind: str, status: str, sid: str, observed: Any, canonical: Any, text: str, start: int, end: int) -> dict[str, Any]:
    return {
        "rule_id": f"CONSISTENCY_{kind}",
        "qa_class": CLASS_FALSE_POSITIVE,
        "section_id": sid,
        "observed": observed,
        "canonical": canonical,
        "semantic_status": status,
        "clause": clause_of(text, start, end),
        "classification_reason": f"negation_aware:{status}",
        "reason": _NON_ASSERTIVE_REASONS.get(status, ""),
    }


def canonicalize_fact_key(key: str) -> str:
    """alias → canonical key；未注册 key 原样返回。"""
    key = str(key or "").strip()
    for canonical, aliases in CANONICAL_KEY_REGISTRY.items():
        if key == canonical or key in aliases:
            return canonical
    return key


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _flatten(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_flatten(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_flatten(v) for v in value)
    return str(value) if isinstance(value, str) else ""


def check_cross_section_consistency(
    fragments: dict[str, dict[str, Any]],
    global_state: dict[str, Any],
    run_dir: Path | None = None,
) -> dict[str, Any]:
    """核查全部 Section 片段与 Canonical Facts 的一致性。

    返回 {registry, issues, summary}；run_dir 提供时落盘 consistency_registry/issues。
    """
    facts = (global_state or {}).get("project_facts") or {}
    registry: dict[str, Any] = {"schema_version": "consistency-registry-v0.1", "canonical": {}, "observed": {}, "generated_at": _now()}
    for key in facts:
        canonical_key = canonicalize_fact_key(str(key))
        value = facts[key]
        registry["canonical"][canonical_key] = value.get("value") if isinstance(value, dict) else value
    issues: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    # department_names：canonical 部门名逐一在片段中核对（漂移 → NEEDS_REVIEW）
    dept_raw = registry["canonical"].get("department_names")
    if dept_raw:
        canonical_depts = [d.strip() for d in re.split(r"[、，,;；/]", str(dept_raw)) if len(d.strip()) >= 2]
        for sid, fragment in (fragments or {}).items():
            text = _flatten(fragment.get("body_blocks"))
            for token in re.finditer(r"[一-鿿]{2,8}(?:部|中心)", text):
                if token.group(0) in canonical_depts or any(
                    token.group(0) in d or d in token.group(0) for d in canonical_depts
                ):
                    continue
                status = _clause_status(text, token.start(), token.end())
                if status in _NON_ASSERTIVE:
                    suppressed.append(_suppressed_row(
                        "DEPARTMENT_NAME", status, sid, token.group(0), dept_raw, text, token.start(), token.end()))
                    continue
                issues.append({
                    "issue_id": f"CS-D{len(issues)+1:03d}",
                    "fact_key": "department_names",
                    "severity": "NEEDS_REVIEW",
                    "section_id": sid,
                    "observed": token.group(0),
                    "canonical": dept_raw,
                    "semantic_status": status,
                    "resolution": "LOCAL_REVIEW",
                    "reason": "片段出现的部门名称与 Canonical 部门名不一致，疑似命名漂移。",
                })
    for fact_key, pattern in NUMBER_PATTERNS.items():
        canonical_value = registry["canonical"].get(fact_key)
        canonical_digits = re.sub(r"[^0-9.]", "", str(canonical_value)) if canonical_value else ""
        observed: dict[str, list[str]] = {}
        for sid, fragment in (fragments or {}).items():
            text = _flatten(fragment.get("body_blocks"))
            for match in pattern.finditer(text):
                status = _clause_status(text, match.start(), match.end())
                if status in _NON_ASSERTIVE:
                    suppressed.append(_suppressed_row(
                        "NUMBER_VALUE", status, sid, match.group(1), canonical_value, text, match.start(), match.end()))
                    continue
                observed.setdefault(str(sid), []).append(match.group(1))
        if observed:
            registry["observed"][fact_key] = observed
        if not canonical_digits:
            continue
        for sid, values in observed.items():
            for value in values:
                value_digits = re.sub(r"[^0-9.]", "", value)
                if value_digits and canonical_digits.split(".")[0] not in value_digits and value_digits not in canonical_digits:
                    issues.append({
                        "issue_id": f"CS-{len(issues)+1:03d}",
                        "fact_key": fact_key,
                        "severity": "NEEDS_REVIEW",
                        "section_id": sid,
                        "observed": value,
                        "canonical": canonical_value,
                        "resolution": "LOCAL_REVIEW",
                        "reason": "片段数值与 Canonical Facts 不一致；无法自动确定正确值时进入人工复核。",
                    })
    actually_checked = [key for key in NUMBER_PATTERNS if key in registry["canonical"]]
    not_checked = [key for key in NUMBER_PATTERNS if key not in registry["canonical"]]
    summary = {
        "sections_checked": len(fragments or {}),
        "checked_fields": actually_checked,
        "not_checked_fields": not_checked,
        "not_applicable_fields": [key for key in REGISTRY_KEYS_ALL if key not in NUMBER_PATTERNS],
        "fact_keys_checked": actually_checked,
        "issue_count": len(issues),
        "suppressed_count": len(suppressed),
        "negation_aware": True,
        "block_count": sum(issue["severity"] == "BLOCK" for issue in issues),
    }
    result = {"schema_version": "global-consistency-qa-v0.2", "registry": registry, "issues": issues,
              "suppressed": suppressed, "summary": summary,
              "status": "BLOCK" if summary["block_count"] else ("NEEDS_REVIEW" if issues else "PASS")}
    if run_dir is not None:
        run_dir = Path(run_dir)
        (run_dir / "consistency_registry.json").write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (run_dir / "consistency_issues.json").write_text(json.dumps({"schema_version": "consistency-issues-v0.2", "issues": issues, "suppressed": suppressed, "summary": summary}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result
