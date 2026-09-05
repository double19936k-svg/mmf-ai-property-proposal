from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Runtime V0.1 only treats a number as a compliance-sensitive business value
# when the unit is present.  Bare digits inside IDs, dates or slash-separated
# shorthand (for example 5/10/60) are not safe evidence by themselves.
NUMERIC = re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?:\s*[—–-]\s*\d+(?:\.\d+)?)?\s*(?:分钟|小时|天|次|名|人|%|％|平方米|m²|㎡)")
STRUCTURAL_ID = re.compile(
    r"(?i)\bS\d{2}-\d{2}\b|\bCH\d{2}\b|\bP\d{2}\b|\bSL-\d{3}\b|section[_ -]?id|chapter[_ -]?id"
)
# Level C requires an explicit headcount commitment: quantity + person unit + staffing verb.
# Bare "人" after a section id (S03-01 / 负责人) or "20人次" must not match.
STAFFING_LEVEL_C = re.compile(
    r"(?:(?:固定)?(?:配置|配备|编制|定编|定岗定编)|不少于|不得少于|至少|共计|合计|共)"
    r"\S{0,16}?"
    r"\d+(?:\s*[—–-]\s*\d+)?\s*(?:名|人)(?!次)"
    r"|"
    r"(?:人员|岗位|团队)\s*\d+(?:\s*[—–-]\s*\d+)?\s*(?:名|人)(?!次)"
    r"|"
    r"\d+(?:\s*[—–-]\s*\d+)?\s*(?:名|人)(?!次)\S{0,8}(?:编制|配置)"
    r"|"
    r"(?<!第)\d+\s*名"
    r"|"
    r"(?:每组|每班次|每班|白班|夜班)\s*\d+(?:\s*[—–-]\s*\d+)?\s*人(?!次)"
)
STAFFING_LEVEL_B = re.compile(
    r"(?:设置|增设|新设|设立|新增|成立|组建|配置|增配)\S{0,16}(?:专属服务岗|VIP服务岗|礼宾专岗|专岗|岗位|专项查验组|专项保障组|品质巡查组|专班|驻场专项小组|专属服务力量)"
)
STAFFING_AMBIGUOUS = re.compile(r"(?:配置|配备|增配|加强)\S{0,8}(?:充足|适量|必要)?(?:人员|服务力量)")
NEGATION_BEFORE = re.compile(r"(?:不|未|无|禁止|避免|不得|无需|不再)\S{0,4}$")


def _mask_structural_ids(text: str) -> str:
    return STRUCTURAL_ID.sub(" ", text)

def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_text(v) for k, v in value.items() if k not in {"provider_metadata"})
    if isinstance(value, list):
        return "\n".join(_text(v) for v in value)
    return ""

def _norm_token(token: str) -> str:
    return re.sub(r"\s+", "", token).replace("–", "—").replace("-", "—")

def _negated(text: str, start: int) -> bool:
    return bool(NEGATION_BEFORE.search(text[max(0, start - 10):start]))

def evaluate_compliance(brief: dict[str, Any], positives: list[dict[str, Any]], guardrails: list[dict[str, Any]], generated: dict[str, Any]) -> dict[str, Any]:
    artifact_text = _text(generated.get("artifact", {}))
    citations = generated.get("citation_registry", []) if isinstance(generated.get("citation_registry"), list) else []
    violations: list[dict[str, Any]] = []
    confirmed_text = "\n".join(str(brief.get(k, "") or "") for k in ("confirmed_sla_kpi", "confirmed_service_hours", "confirmed_staffing", "requirements", "client_requirements"))

    conditional_sources = [p for p in positives if str(p.get("non_applicable_conditions", "")).strip() or "条件" in str(p.get("applicability", ""))]
    conditional_numeric: dict[str, str] = {}
    for source in conditional_sources:
        for token in NUMERIC.findall(_text(source)):
            if re.search(r"\d", token):
                conditional_numeric[_norm_token(token)] = source.get("ku_id", "unknown")
    for token in NUMERIC.findall(artifact_text):
        normalized = _norm_token(token)
        if normalized in conditional_numeric and normalized not in _norm_token(confirmed_text):
            violations.append({"rule_id": "CG-001", "severity": "BLOCK", "message": "条件性历史数字被写成当前项目执行标准。", "evidence": token, "source_id": conditional_numeric[normalized], "repair_constraint": f"不得写入{token}；改为按甲方制度、会议等级或项目确认条件确定，并列入待确认项。"})

    if conditional_sources:
        absolute_markers = re.findall(r"(?:必须|确保|统一|一律|不得少于|提前)\S{0,24}", artifact_text)
        for marker in absolute_markers:
            if any(_norm_token(n) in _norm_token(marker) for n in conditional_numeric):
                violations.append({"rule_id": "CG-002", "severity": "BLOCK", "message": "带适用条件的知识被升级为无条件规则。", "evidence": marker, "source_id": "conditional_positive_ku", "repair_constraint": "保留方法性检查事项，但必须写明适用条件或待甲方确认，不得形成无条件承诺。"})

    if not str(brief.get("confirmed_staffing", "")).strip():
        staffing_text = _mask_structural_ids(artifact_text)
        for match in STAFFING_LEVEL_C.finditer(staffing_text):
            if not _negated(staffing_text, match.start()):
                violations.append({"rule_id": "CG-003", "severity": "BLOCK", "classification_level": "Level_C_explicit_headcount_or_establishment", "message": "当前Brief未确认人员编制，但输出形成明确人数或编制承诺。", "evidence": match.group(0), "source_id": "brief.confirmed_staffing", "repair_constraint": "删除未经current_project_fact确认的人数、班次人数或编制；只保留职责机制，并写明按项目规模、合同范围和甲方确认配置。"})
        for match in STAFFING_LEVEL_B.finditer(staffing_text):
            if not _negated(staffing_text, match.start()):
                violations.append({"rule_id": "CG-003", "severity": "BLOCK", "classification_level": "Level_B_new_role_or_team", "message": "当前Brief未确认人员编制，但输出新设固定岗位或专项组织。", "evidence": match.group(0), "source_id": "brief.confirmed_staffing", "repair_constraint": "将新增岗位改写为服务责任机制，例如‘根据项目实际人员配置明确专属服务责任’，不得形成固定设岗承诺。"})
        for match in STAFFING_AMBIGUOUS.finditer(staffing_text):
            if not _negated(staffing_text, match.start()):
                violations.append({"rule_id": "CG-003", "severity": "WARNING", "classification_level": "Level_A_or_ambiguous_mechanism", "message": "当前Brief未确认人员编制，输出出现模糊人员配置表述。", "evidence": match.group(0), "source_id": "brief.confirmed_staffing", "repair_constraint": "明确这是现有人员责任机制，或列为待项目规模与合同范围确认；不得隐含扩编。"})

    if not citations:
        violations.append({"rule_id": "CG-004", "severity": "WARNING", "message": "正式输出缺少引用登记，关键措施无法追溯。", "evidence": "citation_registry为空", "source_id": "citation_registry", "repair_constraint": "为关键措施、项目事实、数字和责任承诺补充current_project_fact或positive_ku引用。"})
    else:
        invalid = [c for c in citations if c.get("source_type") not in {"current_project_fact", "positive_ku"} or not str(c.get("source_id", "")).strip()]
        if invalid:
            violations.append({"rule_id": "CG-004", "severity": "WARNING", "message": "部分关键主张引用结构不完整。", "evidence": json.dumps(invalid[:3], ensure_ascii=False), "source_id": "citation_registry", "repair_constraint": "补齐合法source_type与source_id。"})

    for guardrail in guardrails:
        guard_text = _text(guardrail)
        for token in NUMERIC.findall(guard_text):
            if re.search(r"\d", token) and _norm_token(token) in _norm_token(artifact_text) and _norm_token(token) not in _norm_token(confirmed_text):
                violations.append({"rule_id": "CG-005", "severity": "BLOCK", "message": "Guardrail禁止的历史数字重新进入正式输出。", "evidence": token, "source_id": guardrail.get("ku_id", "unknown"), "repair_constraint": f"删除Guardrail来源的历史数字{token}，除非Brief明确确认。"})

    unique = []
    seen = set()
    for item in violations:
        key = (item["rule_id"], item["severity"], item["evidence"], item["source_id"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    status = "BLOCK" if any(v["severity"] == "BLOCK" for v in unique) else "WARNING" if unique else "PASS"
    return {"schema_version": "provider-compliance-v0.1", "status": status, "violations": unique, "repair_constraints": list(dict.fromkeys(v["repair_constraint"] for v in unique)), "summary": {"block_count": sum(v["severity"] == "BLOCK" for v in unique), "warning_count": sum(v["severity"] == "WARNING" for v in unique)}, "runtime_provider_agnostic": True}

def write_default_rules(path: Path) -> None:
    rules = {"schema_version": "provider-compliance-rules-v0.1", "rules": [
        {"rule_id": "CG-001", "name": "Unsupported Numeric Commitment", "default_severity": "BLOCK"},
        {"rule_id": "CG-002", "name": "Conditional Knowledge Escalation", "default_severity": "WARNING_OR_BLOCK"},
        {"rule_id": "CG-003", "name": "Unsupported Staffing Expansion", "default_severity": "LEVEL_A_PASS_OR_WARNING_LEVEL_B_C_BLOCK"},
        {"rule_id": "CG-004", "name": "Uncited Material Claim", "default_severity": "WARNING"},
        {"rule_id": "CG-005", "name": "Guardrail Leakage", "default_severity": "BLOCK"}
    ], "source": "MMF-004 Todd directive", "grok_rule_design_status": "calibrated_by_grok-4.6-build_xhigh_20260829"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rules, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
