from __future__ import annotations

from copy import deepcopy
from typing import Any

from .models import TenderError, now_iso


HIGH_IMPACT_TYPES = {
    "staffing", "service_hours", "sla_kpi", "service_scope", "exclusion",
    "contract_or_duration", "commercial_or_assessment", "scoring",
}


def _high_impact(requirement: dict[str, Any]) -> bool:
    return bool(
        requirement.get("mandatory_level") == "MUST"
        or requirement.get("scoring_item_id")
        or requirement.get("requirement_type") in HIGH_IMPACT_TYPES
    )


def apply_confirmation(pack: dict[str, Any], decisions: dict[str, Any]) -> dict[str, Any]:
    updated = deepcopy(pack)
    reviewer_role = str(decisions.get("reviewer_role") or "UNSPECIFIED")
    reviewer_name = str(decisions.get("reviewer_name") or reviewer_role)
    human_signoff = reviewer_role == "HUMAN" and decisions.get("human_signoff_claimed") is True
    by_id = {row["requirement_id"]: row for row in updated["requirements"]}
    auto_accept = bool((decisions or {}).get("accept_remaining", True))
    for requirement_id, decision in (decisions.get("requirements") or {}).items():
        row = by_id.get(requirement_id)
        if row is None:
            continue
        action = str((decision or {}).get("action", "defer"))
        if action == "confirm":
            row["confirmation_status"] = "CONFIRMED"
        elif action == "edit":
            edit = str((decision or {}).get("edit", "")).strip()
            if not edit:
                raise TenderError("UNDERSTAND_SCHEMA_INVALID", f"{requirement_id}选择修改时必须填写内容。")
            row["todd_edit"] = edit
            row["confirmation_status"] = "EDITED"
        elif action == "not_applicable":
            row["confirmation_status"] = "REJECTED_AS_NOT_APPLICABLE"
        else:
            row["confirmation_status"] = "DEFERRED"

    conflict_decisions = decisions.get("conflicts") or {}
    for conflict in updated["conflicts"]:
        decision = conflict_decisions.get(conflict["conflict_group_id"], {})
        resolution = str((decision or {}).get("resolution", "UNRESOLVED"))
        if resolution not in {"UNRESOLVED", "USE_A", "USE_B", "USE_CUSTOM", "KEEP_CONDITIONAL"}:
            raise TenderError("UNDERSTAND_SCHEMA_INVALID", "冲突处理选项无效。")
        conflict["resolution"] = resolution
        conflict["custom_value"] = str((decision or {}).get("custom_value", "")).strip() or None
        rows = [by_id[rid] for rid in conflict["requirement_ids"] if rid in by_id]
        if resolution in {"USE_A", "USE_B"} and rows:
            selected_index = 0 if resolution == "USE_A" else min(1, len(rows) - 1)
            for index, row in enumerate(rows):
                row["confirmation_status"] = "CONFIRMED" if index == selected_index else "REJECTED_AS_NOT_APPLICABLE"
        elif resolution == "USE_CUSTOM":
            if not conflict["custom_value"]:
                raise TenderError("UNDERSTAND_SCHEMA_INVALID", "USE_CUSTOM必须填写自定义处理。")
            for row in rows:
                row["confirmation_status"] = "EDITED"
                row["todd_edit"] = conflict["custom_value"]
        elif resolution == "KEEP_CONDITIONAL":
            for row in rows:
                row["confirmation_status"] = "DEFERRED"

    boilerplate_decisions = decisions.get("boilerplate") or {}
    for flag in updated["potential_boilerplate"]:
        decision = str(boilerplate_decisions.get(flag["boilerplate_id"], "UNCONFIRMED"))
        if decision not in {"APPLICABLE", "NOT_APPLICABLE", "CONDITIONAL", "UNCONFIRMED"}:
            raise TenderError("UNDERSTAND_SCHEMA_INVALID", "疑似模板处理选项无效。")
        flag["todd_decision"] = decision
        for requirement_id in flag["requirement_ids"]:
            row = by_id.get(requirement_id)
            if row is None:
                continue
            if decision == "APPLICABLE": row["confirmation_status"] = "CONFIRMED"
            elif decision == "NOT_APPLICABLE": row["confirmation_status"] = "REJECTED_AS_NOT_APPLICABLE"
            elif decision == "CONDITIONAL": row["confirmation_status"] = "DEFERRED"

    clarification_answers = decisions.get("clarifications") or {}
    for item in updated["clarification_items"]:
        answer = str(clarification_answers.get(item["clarification_id"], "")).strip()
        if answer:
            item["answer"] = answer
            item["answered_by"] = reviewer_name
            item["reviewer_role"] = reviewer_role

    if auto_accept:
        for row in updated["requirements"]:
            if row.get("confirmation_status") == "UNCONFIRMED":
                row["confirmation_status"] = "CONFIRMED"
        for conflict in updated["conflicts"]:
            if conflict.get("resolution", "UNRESOLVED") == "UNRESOLVED":
                conflict["resolution"] = "KEEP_CONDITIONAL"
                for requirement_id in conflict.get("requirement_ids", []):
                    row = by_id.get(requirement_id)
                    if row and row.get("confirmation_status") == "UNCONFIRMED":
                        row["confirmation_status"] = "DEFERRED"
        for flag in updated["potential_boilerplate"]:
            if flag.get("todd_decision", "UNCONFIRMED") == "UNCONFIRMED":
                flag["todd_decision"] = "APPLICABLE"
                for requirement_id in flag.get("requirement_ids", []):
                    row = by_id.get(requirement_id)
                    if row and row.get("confirmation_status") == "UNCONFIRMED":
                        row["confirmation_status"] = "CONFIRMED"
        for item in updated["clarification_items"]:
            if item.get("blocks_plan") and not str(item.get("answer") or "").strip():
                item["answer"] = "以招标或需求文件载明内容为准；文件未明确事项待项目确认。"
                item["answered_by"] = "user_confirm_accept_remaining"
        for fact in updated.get("project_facts", {}).values():
            if fact.get("confirmation_status") == "UNCONFIRMED" and fact.get("value"):
                fact["confirmation_status"] = "CONFIRMED"

    # A template decision cannot re-confirm a fact still held conditionally.
    held_ids = {rid for conflict in updated["conflicts"]
                if conflict.get("resolution", "UNRESOLVED") in {"UNRESOLVED", "KEEP_CONDITIONAL"}
                for rid in conflict.get("requirement_ids", [])}
    for rid in held_ids:
        if rid in by_id:
            by_id[rid]["confirmation_status"] = "DEFERRED"

    for key, fact in updated.get("project_facts", {}).items():
        if fact.get("fact_status") == "MULTI_VALUE":
            from .fact_sources import comparison_value
            active = [item for item in fact.get("candidates", [])
                      if item["requirement_id"] not in held_ids
                      and by_id.get(item["requirement_id"], {}).get("confirmation_status") == "CONFIRMED"]
            if active and len({comparison_value(key, item["value"]) for item in active}) == 1:
                fact.update(value=active[0]["value"], source_requirement_id=active[0]["requirement_id"],
                            confirmation_status="CONFIRMED", fact_status="EXPLICIT_SOURCE_SELECTION")
            else:
                fact.update(value="", source_requirement_id=None, confirmation_status="UNCONFIRMED")
            continue
        requirement = by_id.get(str(fact.get("source_requirement_id", "")))
        if requirement and requirement["confirmation_status"] in {"CONFIRMED", "EDITED"}:
            fact["confirmation_status"] = "CONFIRMED"
            if requirement.get("todd_edit"):
                fact["value"] = requirement["todd_edit"]

    proposal_reviews = decisions.get("proposal_reviews") or {}
    for proposal in updated.get("provider_proposals", []):
        decision = proposal_reviews.get(proposal["proposal_id"])
        if decision is not None:
            if decision != "KEEP_PENDING":
                raise TenderError("UNDERSTAND_SCHEMA_INVALID", "Provider提议只能保留待核实，不能作为已确认项目事实。")
            proposal["review_status"] = "REVIEWED_KEEP_PENDING"
            proposal["reviewer_role"] = reviewer_role
            proposal["reviewed_by"] = reviewer_name
    proposals_reviewed = all(row.get("review_status") == "REVIEWED_KEEP_PENDING" for row in updated.get("provider_proposals", []))

    high_impact_reviewed = all(row["confirmation_status"] != "UNCONFIRMED" for row in updated["requirements"] if _high_impact(row))
    conflicts_reviewed = all(row["resolution"] != "UNRESOLVED" for row in updated["conflicts"])
    boilerplate_reviewed = all(row.get("todd_decision") != "UNCONFIRMED" for row in updated["potential_boilerplate"])
    missing_reviewed = all((not row["blocks_plan"]) or bool(row.get("answer")) or row["reason_code"] in {"UNRESOLVED_CONFLICT", "UNCONFIRMED_BOILERPLATE"} for row in updated["clarification_items"])
    unresolved_blocker = any(row["resolution"] == "UNRESOLVED" and row["blocker_for_plan"] for row in updated["conflicts"])
    unconfirmed_boilerplate = any(row.get("todd_decision") == "UNCONFIRMED" for row in updated["potential_boilerplate"])
    unanswered_other = any(row["blocks_plan"] and not row.get("answer") and row["reason_code"] not in {"UNRESOLVED_CONFLICT", "UNCONFIRMED_BOILERPLATE"} for row in updated["clarification_items"])
    ready = high_impact_reviewed and conflicts_reviewed and boilerplate_reviewed and proposals_reviewed and not unresolved_blocker and not unconfirmed_boilerplate and not unanswered_other
    updated["confirmation"] = {
        "confirmed_at": now_iso() if ready else None, "confirmed_by": reviewer_name if ready else None,
        "reviewer_role": reviewer_role, "human_signoff_claimed": human_signoff,
        "high_impact_reviewed": high_impact_reviewed, "conflicts_reviewed": conflicts_reviewed,
        "boilerplate_reviewed": boilerplate_reviewed, "missing_reviewed": missing_reviewed,
        "ready_for_brief_seed": ready,
        "provider_proposals_reviewed": proposals_reviewed,
    }
    updated["status"] = "ready_for_plan" if ready else "blocked_clarification"
    updated["pack_version"] = int(updated.get("pack_version", 1)) + 1
    return updated


def _confirmed_texts(pack: dict[str, Any], requirement_type: str) -> list[str]:
    return [str(row.get("todd_edit") or row["normalized_requirement"]) for row in pack["requirements"] if row["requirement_type"] == requirement_type and row["confirmation_status"] in {"CONFIRMED", "EDITED"} and not row.get("excluded_from_solution_body")]


def seed_brief(pack: dict[str, Any], options: dict[str, Any], *,
               project_fields: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    if not pack.get("confirmation", {}).get("ready_for_brief_seed"):
        raise TenderError("UNDERSTAND_SCHEMA_INVALID", "Requirement Pack尚未完成Todd确认，不能播种Brief。")
    facts = {key: row.get("value") for key, row in pack.get("project_facts", {}).items() if row.get("confirmation_status") == "CONFIRMED"}
    if project_fields is None:
        project_name = str(facts.get("project_name") or options.get("project_name") or "").strip() or "未命名项目"
        project_type = str(facts.get("project_type") or options.get("project_type") or "").strip() or "物业服务项目"
    else:
        name_field = project_fields["project_name"]
        project_name = (name_field["value"] if name_field.get("official") else
                        str(options.get("project_name") or "").strip() or name_field["value"])
        project_type = str(options.get("project_type") or "").strip() or project_fields["project_type"]["value"]
        if not project_name or project_name == "未命名项目" or not project_type:
            missing = [label for label, present in (("项目名称", project_name and project_name != "未命名项目"),
                                                     ("项目类型", project_type)) if not present]
            raise TenderError("UNDERSTAND_SCHEMA_INVALID", "请补充" + "、".join(missing) + "后继续。")
    from providers.cursor_account import is_cursor_provider

    provider_name = options.get('provider_name')
    if (provider_name in {'zcode_cn','qoder_account'} or is_cursor_provider(provider_name)) and project_name=='未命名项目':
        raise TenderError('UNDERSTAND_SCHEMA_INVALID','请确认项目名称后继续，不能以未命名项目生成技术标。')
    confirmed_requirements = [str(row.get("todd_edit") or row["normalized_requirement"]) for row in pack["requirements"] if row["confirmation_status"] in {"CONFIRMED", "EDITED"} and not row.get("excluded_from_solution_body")]
    excluded = _confirmed_texts(pack, "exclusion")
    service_scope_parts = [text for text in confirmed_requirements if text not in excluded]
    if excluded:
        service_scope_parts.append("明确排除：" + "；".join(excluded))
    return {
        "provider_name": str(options.get("provider_name", "")).strip(),
        "project_name": project_name,
        "project_type": project_type,
        "scenario": str(options.get("scenario", "")).strip(),
        "medium": str(options.get("medium", "")).strip(),
        "requirements": "；".join(confirmed_requirements),
        "location": str(facts.get("location") or ""),
        "area": str(facts.get("managed_area") or facts.get("gross_area") or ""),
        "functional_composition": str(facts.get("building_functions") or ""),
        "service_scope": "；".join(service_scope_parts),
        "project_features": "",
        "client_requirements": "；".join(confirmed_requirements),
        "confirmed_staffing": "；".join(_confirmed_texts(pack, "staffing")),
        "confirmed_service_hours": "；".join(_confirmed_texts(pack, "service_hours")),
        "confirmed_sla_kpi": "；".join(_confirmed_texts(pack, "sla_kpi")),
        "schedule": str(facts.get("service_start_date") or ""),
        "additional_information": "来源：已确认Project Requirement Pack；未确认内容保持为空。",
        "requirement_pack_id": pack["pack_id"],
        "requirement_pack_version": pack.get("pack_version", 1),
        "speed_profile": str(options.get("speed_profile") or "balanced"),
        "document_scale": str(options.get("document_scale") or "regular_solution"),
        "max_parallelism": options.get("max_parallelism"),
    }
