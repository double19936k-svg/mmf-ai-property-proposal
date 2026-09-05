from __future__ import annotations

import re
import unicodedata
import uuid
from collections import defaultdict
from typing import Any

from .models import CANONICAL_FACT_KEYS, TenderError, now_iso, validate_pack_shape


REQUIREMENT_BUCKETS = {
    "explicit": "explicit_requirements",
    "mandatory": "mandatory_requirements",
    "service_standard": "service_standards",
    "staffing": "staffing_requirements",
    "service_hours": "service_hours",
    "sla_kpi": "sla_kpi",
    "facility": "facility_requirements",
    "handover": "handover_requirements",
    "security": "security_requirements",
    "environment": "environment_requirements",
    "engineering": "engineering_requirements",
    "customer_service": "customer_service_requirements",
    "commercial_or_assessment": "commercial_or_assessment_requirements",
    "contract_or_duration": "contract_or_duration_requirements",
    "exclusion": "exclusions",
}
HIGH_IMPACT_TYPES = {"staffing", "service_hours", "sla_kpi", "service_scope", "exclusion", "contract_or_duration", "commercial_or_assessment", "scoring"}
MALL_TERMS = ("商场", "闭店", "店铺", "专柜", "营业时间")
INDUSTRIAL_TERMS = ("产业园", "园区", "厂房", "厂区")


def _normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[，。；：、,.!！?？（）()\[\]【】]", "", text)
    return text.lower()


def _mandatory_level(text: str, guess: str = "UNKNOWN") -> str:
    if re.search(r"必须|不得|应至少|否则(?:否决|废标)|严禁", text):
        return "MUST"
    if re.search(r"应当|应做到|应建立|应提供|建议", text):
        return "SHOULD"
    if guess in {"MUST", "SHOULD", "INFO", "UNKNOWN"}:
        return guess
    return "INFO"


def _requirement_type(text: str, guess: str = "other") -> str:
    if re.search(r"评分|分值|得分|权重", text): return "scoring"
    if re.search(r"不包含|不含|排除|不提供|不属于服务范围", text): return "exclusion"
    if re.search(r"(?:服务|值守|工作|运营).{0,8}\d+\s*(?:小时|h\b)|\d+\s*(?:小时|h\b).{0,8}(?:服务|值守|工作)", text, re.I): return "service_hours"
    if re.search(r"人员|岗位|编制|不少于\s*\d+\s*人|\d+\s*人", text): return "staffing"
    if re.search(r"SLA|KPI|响应时间|完成率|满意率|分钟内|小时内", text, re.I): return "sla_kpi"
    if re.search(r"合同期|服务期|合同期限|进场日期|服务开始", text): return "contract_or_duration"
    if re.search(r"巡逻|巡更|安保|秩序|门岗|消防", text): return "security"
    if re.search(r"保洁|环境|垃圾|绿化|消杀", text): return "environment"
    if re.search(r"设施|设备|维修|机电|工程", text): return "engineering"
    if re.search(r"客户|投诉|报修|服务中心", text): return "customer_service"
    if re.search(r"项目名称|项目类型|位于|建筑面积|管理面积|产业园", text): return "project_fact"
    if guess in set(REQUIREMENT_BUCKETS) | {"project_fact", "service_scope", "other"}:
        return guess
    return "explicit"


def _hours_value(text: str) -> str | None:
    matches = re.findall(r"(?<!\d)(\d{1,3})\s*(?:小时|h\b)", text, re.I)
    return f"{matches[0]}h" if matches else None


def _numeric_value(text: str, requirement_type: str) -> tuple[str | int | float | None, str | None]:
    if requirement_type == "service_hours":
        value = _hours_value(text)
        return (int(value[:-1]), "hour") if value else (None, None)
    if requirement_type == "staffing":
        match = re.search(r"(\d+)\s*人", text)
        return (int(match.group(1)), "person") if match else (None, None)
    match = re.search(r"(\d+(?:\.\d+)?)\s*(%|分钟|小时|平方米|㎡|分)", text)
    if match:
        raw = float(match.group(1))
        value: int | float = int(raw) if raw.is_integer() else raw
        return value, match.group(2)
    return None, None


def _source_from_paragraph(paragraph: dict[str, Any], files: dict[str, dict[str, Any]], excerpt: str) -> dict[str, Any]:
    file_meta = files[paragraph["file_id"]]
    locator = {
        "file_id": paragraph["file_id"],
        "source_file": file_meta["original_filename"],
        "source_page": paragraph.get("page_no"),
        "source_section": " / ".join(paragraph.get("heading_path") or []) or None,
        "source_paragraph_or_table": f"paragraph:{paragraph.get('paragraph_index')}",
        "outline_node_id": paragraph.get("outline_node_id"),
        "para_id": paragraph.get("para_id"),
        "table_id": None,
        "paragraph_index": paragraph.get("paragraph_index"),
        "table_index": None,
        "heading_path": paragraph.get("heading_path") or [],
        "source_excerpt": excerpt[:500],
    }
    return locator


def _source_from_table(table: dict[str, Any], files: dict[str, dict[str, Any]], excerpt: str) -> dict[str, Any]:
    file_meta = files[table["file_id"]]
    return {
        "file_id": table["file_id"], "source_file": file_meta["original_filename"],
        "source_page": table.get("page_no"), "source_section": " / ".join(table.get("heading_path") or []) or None,
        "source_paragraph_or_table": f"table:{table.get('table_index')}", "outline_node_id": table.get("outline_node_id"),
        "para_id": None, "table_id": table.get("table_id"), "paragraph_index": None,
        "table_index": table.get("table_index"), "heading_path": table.get("heading_path") or [],
        "source_excerpt": excerpt[:500],
    }


def candidates_from_extraction(extraction: dict[str, Any]) -> list[dict[str, Any]]:
    """High-recall local candidates used by Mock and as deterministic safety net."""
    files = {row["file_id"]: row for row in extraction["files"]}
    candidates: list[dict[str, Any]] = []
    for paragraph in extraction["paragraphs"]:
        if paragraph.get("heading_level") and len(paragraph.get("text", "")) < 30:
            continue
        parts = [x.strip() for x in re.split(r"[。；;\n]+", paragraph.get("text", "")) if len(x.strip()) >= 4]
        for occurrence_index, part in enumerate(parts):
            source = _source_from_paragraph(paragraph, files, part)
            source["occurrence_index"] = occurrence_index
            source["source_paragraph_or_table"] += f":segment:{occurrence_index}"
            candidates.append({
                "normalized_requirement": part,
                "requirement_type": _requirement_type(part),
                "mandatory_level_guess": _mandatory_level(part, "INFO"),
                "classification_guess": "GENERIC_REQUIREMENT",
                "source_excerpt": part,
                "source_locator": source,
                "confidence": 0.88,
            })
    for table in extraction["tables"]:
        rows = [table.get("columns", [])] + table.get("rows", [])
        for row in rows:
            text = " | ".join(str(cell).strip() for cell in row if str(cell).strip())
            if len(text) < 4:
                continue
            candidates.append({
                "normalized_requirement": text, "requirement_type": _requirement_type(text),
                "mandatory_level_guess": _mandatory_level(text, "INFO"), "classification_guess": "GENERIC_REQUIREMENT",
                "source_excerpt": text, "source_locator": _source_from_table(table, files, text), "confidence": 0.9,
            })
    return candidates


def _canonical_facts(requirements: list[dict[str, Any]]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    patterns = {
        "project_name": r"项目名称\s*[：:]\s*([^，。；\n]+)",
        "project_type": r"项目(?:类型|业态)\s*[：:]\s*([^，。；\n]+)",
        "location": r"(?:项目)?(?:地址|地点|所在地)\s*[：:]\s*([^，。；\n]+)",
        "gross_area": r"(?:总建筑面积|建筑面积)\s*[：:]?\s*([0-9.]+\s*(?:万)?(?:平方米|㎡))",
        "managed_area": r"管理面积\s*[：:]?\s*([0-9.]+\s*(?:万)?(?:平方米|㎡))",
        "contract_duration": r"(?:合同期|服务期|合同期限)\s*[：:]?\s*([^，。；\n]+)",
    }
    for requirement in requirements:
        text = requirement["normalized_requirement"]
        for key, pattern in patterns.items():
            match = re.search(pattern, text)
            if match and key not in facts:
                facts[key] = {"value": match.group(1).strip(), "source_requirement_id": requirement["requirement_id"], "confirmation_status": "UNCONFIRMED"}
        if "产业园" in text and "project_type" not in facts:
            facts["project_type"] = {"value": "产业园", "source_requirement_id": requirement["requirement_id"], "confirmation_status": "UNCONFIRMED"}
    return {key: value for key, value in facts.items() if key in CANONICAL_FACT_KEYS}


def _project_context(candidates: list[dict[str, Any]]) -> tuple[bool, set[str]]:
    all_text = "\n".join(str(row.get("normalized_requirement", "")) for row in candidates)
    industrial = any(term in all_text for term in INDUSTRIAL_TERMS)
    names = set(re.findall(r"项目名称\s*[：:]\s*([^，。；\n]+)", all_text))
    return industrial, {name.strip() for name in names if name.strip()}


def _classification(text: str, requirement_type: str, industrial: bool, project_names: set[str]) -> tuple[str, list[str]]:
    boilerplate_reasons: list[str] = []
    if industrial and any(term in text for term in MALL_TERMS):
        boilerplate_reasons = ["property_type_mismatch", "context_conflict", "template_lexicon"]
        return "POTENTIAL_BOILERPLATE", boilerplate_reasons
    strong_context = any(name in text for name in project_names) or bool(re.search(r"本项目|本园区|本次招标|项目名称|项目地址|项目位于|本合同范围", text))
    if requirement_type == "project_fact" and strong_context:
        return "PROJECT_SPECIFIC", []
    if strong_context:
        return "PROJECT_SPECIFIC", []
    return "GENERIC_REQUIREMENT", []


def _source_key(source: dict[str, Any]) -> tuple[Any, ...]:
    return (source.get("file_id"), source.get("source_page"), source.get("paragraph_index"), source.get("table_index"), source.get("occurrence_index"), source.get("source_excerpt"))


def _sortable_source_key(source: dict[str, Any]) -> tuple[str, ...]:
    return tuple("" if value is None else str(value) for value in _source_key(source))


def build_requirement_pack(extraction: dict[str, Any], proposed_items: list[dict[str, Any]], proposed_scoring_items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    industrial, project_names = _project_context(proposed_items)
    merged: dict[str, dict[str, Any]] = {}
    for candidate in proposed_items:
        text = str(candidate.get("normalized_requirement") or candidate.get("source_excerpt") or "").strip()
        source = candidate.get("source_locator") or {}
        if len(text) < 4 or not source.get("file_id") or not source.get("source_excerpt"):
            continue
        requirement_type = _requirement_type(text, str(candidate.get("requirement_type", "other")))
        level = _mandatory_level(text, str(candidate.get("mandatory_level_guess", "UNKNOWN")))
        value, unit = _numeric_value(text, requirement_type)
        key = f"{requirement_type}|{_normalize_text(text)}|{value}|{unit}"
        if key not in merged:
            classification, boilerplate_reasons = _classification(text, requirement_type, industrial, project_names)
            merged[key] = {
                "normalized_requirement": text, "requirement_type": requirement_type, "mandatory_level": level,
                "confidence": max(0.0, min(1.0, float(candidate.get("confidence", 0.75)))),
                "classification": classification, "conflict_group": None, "boilerplate_flag_id": None,
                "value_normalized": value, "value_unit": unit, "domain": requirement_type,
                "scoring_item_id": None, "sources": [], "confirmation_status": "UNCONFIRMED",
                "todd_edit": None, "must_not_use_as_project_fact": classification != "PROJECT_SPECIFIC",
                "_boilerplate_reasons": boilerplate_reasons,
            }
        if _source_key(source) not in {_source_key(existing) for existing in merged[key]["sources"]}:
            merged[key]["sources"].append(source)

    for row in merged.values():
        occurrence_bases = {
            (source.get("file_id"), source.get("source_page"), source.get("paragraph_index"), source.get("table_index"))
            for source in row["sources"] if source.get("occurrence_index") is not None
        }
        if occurrence_bases:
            row["sources"] = [
                source for source in row["sources"]
                if source.get("occurrence_index") is not None
                or (source.get("file_id"), source.get("source_page"), source.get("paragraph_index"), source.get("table_index")) not in occurrence_bases
            ]
    requirements = list(merged.values())
    requirements.sort(key=lambda row: (_sortable_source_key(row["sources"][0]), row["normalized_requirement"]))
    for index, row in enumerate(requirements, 1):
        row["requirement_id"] = f"REQ-{index:04d}"

    conflicts: list[dict[str, Any]] = []
    by_slot: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in requirements:
        if row["requirement_type"] in {"service_hours"} and row.get("value_normalized") is not None:
            by_slot[row["requirement_type"]].append(row)
    for slot, rows in by_slot.items():
        values = {str(row.get("value_normalized")) for row in rows}
        if len(values) <= 1:
            continue
        cfg_id = f"CFG-{len(conflicts)+1:03d}"
        for row in rows:
            row["classification"] = "CONFLICT_OR_AMBIGUOUS"
            row["conflict_group"] = cfg_id
            row["must_not_use_as_project_fact"] = True
        conflicts.append({
            "conflict_group_id": cfg_id, "requirement_ids": [row["requirement_id"] for row in rows],
            "issue": f"同一{slot}出现不同值，系统不自动选择。",
            "options": [{"requirement_id": row["requirement_id"], "excerpt": row["sources"][0]["source_excerpt"], "value": row.get("value_normalized")} for row in rows],
            "resolution": "UNRESOLVED", "custom_value": None,
            "blocker_for_plan": any(row["mandatory_level"] == "MUST" for row in rows),
        })

    boilerplate: list[dict[str, Any]] = []
    for row in requirements:
        reasons = row.pop("_boilerplate_reasons", [])
        if not reasons:
            continue
        bpl_id = f"BPL-{len(boilerplate)+1:03d}"
        row["boilerplate_flag_id"] = bpl_id
        boilerplate.append({
            "boilerplate_id": bpl_id, "requirement_ids": [row["requirement_id"]],
            "suspicion_reasons": reasons, "evidence": row["sources"][0]["source_excerpt"],
            "action_allowed": "flag_and_confirm_only", "todd_decision": "UNCONFIRMED",
        })

    scoring_items: list[dict[str, Any]] = []
    for row in requirements:
        if row["requirement_type"] != "scoring":
            continue
        score_match = re.search(r"(\d+(?:\.\d+)?)\s*分", row["normalized_requirement"])
        score_id = f"SCR-{len(scoring_items)+1:03d}"
        row["scoring_item_id"] = score_id
        scoring_items.append({
            "scoring_item_id": score_id, "category": "tender_scoring", "label": row["normalized_requirement"],
            "score_range": f"0-{score_match.group(1)}" if score_match else "UNPARSED",
            "weight_numeric": float(score_match.group(1)) if score_match else None,
            "must_respond": True, "source": row["sources"][0],
        })
    for proposed in proposed_scoring_items or []:
        if not isinstance(proposed, dict) or not proposed.get("source"):
            continue
        source = proposed["source"]
        if not source.get("file_id") or not source.get("source_excerpt"):
            continue
        scoring_items.append({
            "scoring_item_id": f"SCR-{len(scoring_items)+1:03d}", "category": str(proposed.get("category", "tender_scoring")),
            "label": str(proposed.get("label", source["source_excerpt"])), "score_range": str(proposed.get("score_range", "UNPARSED")),
            "weight_numeric": proposed.get("weight_numeric"), "must_respond": bool(proposed.get("must_respond", True)), "source": source,
        })

    clarification: list[dict[str, Any]] = []
    for warning in extraction.get("warnings", []):
        if warning.get("code") == "SCAN_PAGE":
            clarification.append({"clarification_id": f"CLQ-{len(clarification)+1:03d}", "reason_code": "SCAN_PAGE", "question": f"{warning.get('file_id')}第{warning.get('page_no')}页疑似扫描页，本轮未识别，是否需要人工补充？", "blocks_plan": False, "related_requirement_ids": [], "answer": None, "answered_by": "none"})
    for conflict in conflicts:
        clarification.append({"clarification_id": f"CLQ-{len(clarification)+1:03d}", "reason_code": "UNRESOLVED_CONFLICT", "question": conflict["issue"], "blocks_plan": conflict["blocker_for_plan"], "related_requirement_ids": conflict["requirement_ids"], "answer": None, "answered_by": "none"})
    for flag in boilerplate:
        clarification.append({"clarification_id": f"CLQ-{len(clarification)+1:03d}", "reason_code": "UNCONFIRMED_BOILERPLATE", "question": "该条内容疑似跨业态模板残留，请确认适用性。", "blocks_plan": True, "related_requirement_ids": flag["requirement_ids"], "answer": None, "answered_by": "none"})
    scoring_detected = any(table.get("is_scoring_table") for table in extraction.get("tables", [])) or any(re.search(r"评分|分值|得分|权重", row.get("text", "")) for row in extraction.get("paragraphs", []))
    if scoring_detected and not scoring_items:
        clarification.append({"clarification_id": f"CLQ-{len(clarification)+1:03d}", "reason_code": "SCORING_TABLE_UNPARSED", "question": "检测到评分章节或评分表，但未能形成评分项，请人工补充。", "blocks_plan": True, "related_requirement_ids": [], "answer": None, "answered_by": "none"})

    for row in requirements:
        low_risk = row["classification"] == "GENERIC_REQUIREMENT" and row["mandatory_level"] == "INFO" and row["requirement_type"] not in HIGH_IMPACT_TYPES
        if low_risk:
            row["confirmation_status"] = "CONFIRMED"

    bucket_values = {name: [] for name in REQUIREMENT_BUCKETS.values()}
    for row in requirements:
        bucket = REQUIREMENT_BUCKETS.get(row["requirement_type"])
        if bucket:
            bucket_values[bucket].append(row["requirement_id"])
    status = "awaiting_todd_confirmation"
    if any(item["blocks_plan"] for item in clarification):
        status = "blocked_clarification"
    elif extraction.get("processing_mode") == "PARTIAL_DEGRADED":
        status = "partial_degraded"
    pack = {
        "schema_version": "tender-requirement-pack-v0.1", "pack_id": f"PACK-{uuid.uuid4().hex[:12]}",
        "run_id": extraction["run_id"], "pack_version": 1, "status": status,
        "source_registry": [{key: row.get(key) for key in ("file_id", "original_filename", "media_type", "sha256", "page_count")} for row in extraction["files"]],
        "project_facts": {}, "service_scope": {"included": [], "excluded": [], "deprioritized": [], "conditional": []},
        **bucket_values, "ambiguities": [item["clarification_id"] for item in clarification if item["reason_code"] != "SCAN_PAGE"],
        "conflicts": conflicts, "potential_boilerplate": boilerplate, "clarification_items": clarification,
        "requirements": requirements, "scoring_items": scoring_items,
        "confirmation": {"confirmed_at": None, "confirmed_by": None, "high_impact_reviewed": False, "conflicts_reviewed": False, "boilerplate_reviewed": False, "missing_reviewed": False, "ready_for_brief_seed": False},
        "authority": "local_pack_builder_provider_proposes", "created_at": now_iso(),
    }
    pack["project_facts"] = _canonical_facts(requirements)
    for row in requirements:
        if row["requirement_type"] == "exclusion":
            pack["service_scope"]["excluded"].append({"requirement_id": row["requirement_id"], "text": row["normalized_requirement"]})
        elif row["requirement_type"] == "service_scope":
            pack["service_scope"]["included"].append({"requirement_id": row["requirement_id"], "text": row["normalized_requirement"]})
    validate_requirement_pack(pack)
    return pack


def validate_requirement_pack(pack: dict[str, Any]) -> None:
    validate_pack_shape(pack)
    seen: set[str] = set()
    for requirement in pack["requirements"]:
        rid = requirement["requirement_id"]
        if rid in seen or not re.fullmatch(r"REQ-\d{4}", rid):
            raise TenderError("UNDERSTAND_SCHEMA_INVALID", "Requirement ID重复或格式错误。")
        seen.add(rid)
        if requirement["classification"] == "PROJECT_SPECIFIC":
            text = requirement["normalized_requirement"]
            strong = bool(re.search(r"本项目|本园区|本次招标|项目名称|项目地址|项目位于|本合同范围", text))
            if not strong and not any(str(fact.get("value", "")) in text for fact in pack.get("project_facts", {}).values()):
                raise TenderError("UNDERSTAND_SCHEMA_INVALID", "PROJECT_SPECIFIC缺少项目上下文证据。")
    valid_ids = seen
    for conflict in pack["conflicts"]:
        if len(conflict.get("requirement_ids", [])) < 2 or not set(conflict["requirement_ids"]) <= valid_ids:
            raise TenderError("UNDERSTAND_SCHEMA_INVALID", "Conflict Group引用无效。")
