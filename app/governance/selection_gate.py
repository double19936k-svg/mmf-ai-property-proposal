from __future__ import annotations

import re
from typing import Any
from .rules import load_rule

STATUSES = {"SELECTED", "CONDITIONAL", "DEPRIORITIZED", "EXCLUDED"}
FORMAL_RULES = load_rule("knowledge_selection_gate_v0.1.json")


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return "\n".join(_text(v) for v in value.values())
    if isinstance(value, list):
        return "\n".join(_text(v) for v in value)
    return str(value or "")


def _provider_ids(recommendations: list[dict[str, Any]]) -> set[str]:
    return {str(row.get("ku_id", "")) for row in recommendations}


def evaluate_selection(
    brief: dict[str, Any],
    candidates: list[dict[str, Any]],
    provider_recommendations: list[dict[str, Any]],
    missing_information: list[str] | None = None,
    clarification_answers: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Apply the local authority gate. Provider output is evidence, never authority."""
    if {row["status"] for row in FORMAL_RULES["states"]} != STATUSES:
        raise ValueError("Formal Selection Gate states do not match runtime implementation")
    provider_ids = _provider_ids(provider_recommendations)
    reasons = {str(x.get("ku_id")): _text(x.get("reason")) for x in provider_recommendations}
    brief_text = _text(brief)
    missing_text = _text(missing_information or [])
    answers_text = _text(clarification_answers or {})
    user_negative_cs = bool(re.search(r"客服.{0,12}(不用|不需要|非重点|除了.{0,8}宿舍)", brief_text))
    operating_text = brief_text + answers_text
    outsourcing_unknown = bool(re.search(r"(?:自营.{0,8}外委|外包|外委).{0,8}(?:尚未确认|未确认|未知)", operating_text))
    outsourcing_known = bool(re.search(r"(外包|外委).{0,6}(已确认|采用|是)|自营.{0,6}(已确认|采用|是)", operating_text)) and not outsourcing_unknown
    access_known = bool(re.search(r"(封闭管理|开放式|门禁).{0,6}(已确认|采用|是|否)", brief_text + answers_text))
    rows: list[dict[str, Any]] = []
    for ku in candidates:
        ku_id = str(ku.get("ku_id") or ku.get("knowledge_unit_id"))
        core = _text(ku.get("core_knowledge"))
        applicability = _text(ku.get("applicability"))
        non_app = _text(ku.get("non_applicable_conditions"))
        provider_recommended = ku_id in provider_ids
        status = "SELECTED" if provider_recommended else "DEPRIORITIZED"
        positive: list[str] = ["Provider推荐"] if provider_recommended else []
        negative: list[str] = []
        preconditions: list[str] = []
        hits: list[str] = []

        if ku.get("membership") == "candidate_guardrail" or ku.get("reuse_mode") in {"do_not_reuse", "evidence_only", "structure_only"}:
            status, hits = "EXCLUDED", ["KSG-007", "KSG-013"]
            negative.append("该知识仅用于风险控制或结构证据")
        elif ku.get("abstraction_level") == "L0_original_fact" or ku.get("knowledge_type") in {"project_specific_fact", "historical_commitment", "data_fact", "company_specific_policy"}:
            status, hits = "EXCLUDED", ["KSG-008"]
            negative.append("L0事实/历史承诺不得作为跨项目正文素材")
        elif ku_id == "KU-9017-EC5D2161" and user_negative_cs:
            status, hits = "EXCLUDED", ["KSG-001", "KSG-010", "KSG-012", "KSG-015"]
            positive.append("项目含办公楼（弱证据）")
            negative.append("用户明确客服除宿舍外不作为重点（强证据）")
        elif ku_id == "KU-9028-AAA62083" and user_negative_cs:
            status, hits = "DEPRIORITIZED", ["KSG-001", "KSG-005", "KSG-012"]
            negative.append("园区级客服投诉闭环不属于用户重点；只可保留简洁报事接口")
        elif ku_id in {"KU-9007-83FCB89D", "KU-9021-6AB7A5E7"} and not outsourcing_known:
            status, hits = "CONDITIONAL", ["KSG-006", "KSG-009", "KSG-011"]
            preconditions.append("环境服务自营/外委模式及合同机制尚未确认")
            if re.search(r"大概率外包|通常外包|一般外包", reasons.get(ku_id, "")):
                negative.append("已移除Provider对外包模式的推断")
        elif ku_id == "KU-9008-1DF2811A" and not access_known:
            status, hits = "SELECTED", ["KSG-006", "KSG-009", "KSG-014"]
            positive.append("安防方法与用户重点匹配")
            preconditions.append("封闭/开放管理和岗位配置尚未确认；相关承诺必须条件化")
        elif ku_id == "KU-9012-E5E6CFB2":
            status, hits = "CONDITIONAL", ["KSG-003", "KSG-006"]
            preconditions.append("仅限办公楼宇或类似室内公共空间的日常/专项分界")
        elif ku_id == "KU-9019-03A470D6":
            status, hits = "SELECTED", ["KSG-006"]
            preconditions.append("资料保护条款仅在高信息安全区域启用")
        elif ku_id == "KU-9003-33317F36" and not re.search(r"审图.{0,6}(已确认|包含|需要)", brief_text + answers_text):
            status, hits = "CONDITIONAL", ["KSG-002", "KSG-006", "KSG-009"]
            preconditions.append("前期介入是否包含审图权限尚未确认")
        elif provider_recommended:
            hits.append("KSG-014")
            positive.append("与当前需求存在直接或方法层匹配")

        if non_app and status == "SELECTED" and any(term in missing_text for term in ("外包", "封闭", "审图", "人员")):
            status = "CONDITIONAL"
            hits.extend(["KSG-006", "KSG-009"])
            preconditions.append("Knowledge Unit不适用条件与当前缺失信息相交")
        overridden = provider_recommended and status != "SELECTED"
        rows.append({
            "ku_id": ku_id,
            "provider_recommended": provider_recommended,
            "selection_status": status,
            "selection_reason": "; ".join(negative + preconditions + positive) or "未被Provider推荐，保持低优先级",
            "positive_evidence": positive,
            "negative_evidence": negative,
            "missing_preconditions": list(dict.fromkeys(preconditions)),
            "overridden_provider_status": overridden,
            "gate_rule_hits": list(dict.fromkeys(hits)),
        })
    return rows
