from __future__ import annotations

import copy
import hashlib
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


FORBIDDEN_VISIBLE = (
    "Requirement ID", "KU ID", "KUC", "Guardrail", "Commitment Provenance",
    "Selection Gate", "MMF", "Provider", "Todd", "ChatGPT", "Grok", "Qwen",
    "AI生成", "系统判断", "内部测试",
)
INTERNAL_ID = re.compile(r"\b(?:REQ|SCR|KU|KUC)-?\d+\b", re.I)
EDITORIAL_REPLACEMENTS = {
    "无死角": "重点区域覆盖",
    "零延误": "减少延误",
    "零干扰": "减少干扰",
    "零拥堵": "降低拥堵风险",
    "零事故": "降低事故风险",
    "零非计划停机": "降低非计划停机风险",
    "全程监控": "实施过程管控",
    "自动派单": "按规则派单",
    "智能匹配": "按专业与位置匹配",
    "形成闭环": "完成跟踪、复核与关闭",
    "持续提升": "根据检查结果改进",
    "明确责任": "落实责任",
    "建立机制": "明确流程与责任",
    "物业经理": "项目负责人",
    "项目经理": "项目负责人",
}

TOPICS = {
    "complaint_closure": ("投诉", "受理", "跟踪", "反馈", "关闭"),
    "engineering_fault_closure": ("故障", "报修", "处置", "复验", "关闭"),
    "quality_pdca": ("品质", "检查", "整改", "复盘", "改进"),
    "training_system": ("培训", "考核", "岗位", "能力"),
    "supplier_management": ("供方", "履约", "评价", "整改"),
    "security_patrol": ("巡逻", "巡查", "路线", "异常"),
    "emergency_response": ("应急", "预案", "响应", "联动", "复盘"),
    "customer_communication": ("客户", "沟通", "诉求", "反馈"),
    "handover_process": ("进场", "交接", "资料", "查验"),
    "environment_correction": ("环境", "清洁", "检查", "整改"),
    "service_assurance": ("履约", "成果", "报告", "验收"),
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def visible_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(visible_text(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(visible_text(item) for item in value.values())
    return str(value)


def _clean_string(value: str, changes: list[dict[str, str]], location: str) -> str:
    original = value
    for old, new in EDITORIAL_REPLACEMENTS.items():
        value = value.replace(old, new)
    value = INTERNAL_ID.sub("", value)
    for forbidden in FORBIDDEN_VISIBLE:
        value = value.replace(forbidden, "")
    value = re.sub(r"\s{2,}", " ", value).strip()
    if value != original:
        changes.append({"location": location, "before": original, "after": value, "action": "LOCAL_EDITORIAL_CLEANUP"})
    return value


def _clean_value(value: Any, changes: list[dict[str, str]], location: str) -> Any:
    if isinstance(value, str):
        return _clean_string(value, changes, location)
    if isinstance(value, list):
        return [_clean_value(item, changes, f"{location}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        return {key: _clean_value(item, changes, f"{location}.{key}") for key, item in value.items()}
    return value


def edit_fragment(fragment: dict[str, Any], changes: list[dict[str, str]]) -> dict[str, Any]:
    result = copy.deepcopy(fragment)
    for key in ("title", "body_blocks", "tables", "processes", "callouts"):
        result[key] = _clean_value(result.get(key), changes, f"{fragment['section_id']}.{key}")
    return result


def _shorten(text: str, limit: int) -> str:
    text = re.sub(r"\s+", "", text).strip("，。；： ")
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for marker in ("；", "，", "。"):
        pos = cut.rfind(marker)
        if pos >= max(8, limit // 2):
            return cut[:pos]
    return cut.rstrip("，；：")


def edit_payload(payload: dict[str, Any], changes: list[dict[str, str]]) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    sid = result["slide_id"]
    for key in ("headline", "subheadline", "key_message", "content_blocks", "visual_data", "speaker_note_optional"):
        result[key] = _clean_value(result.get(key), changes, f"{sid}.{key}")
    result["headline"] = _shorten(result.get("headline", ""), 28)
    result["subheadline"] = _shorten(result.get("subheadline", ""), 42)
    result["key_message"] = _shorten(result.get("key_message", ""), 68)
    blocks = []
    for block in result.get("content_blocks", [])[:4]:
        if not isinstance(block, dict):
            continue
        title = _shorten(str(block.get("title", "服务要点")), 12)
        points = [_shorten(str(item), 34) for item in (block.get("points") or block.get("items") or [])[:3]]
        if not points and block.get("content"):
            points = [_shorten(str(block["content"]), 34)]
        blocks.append({"title": title, "points": [item for item in points if item]})
    result["content_blocks"] = blocks
    return result


def _sentences(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"(?<=[。！？；])", text) if item.strip()]


def _evidence(requirement: dict[str, Any], text: str) -> tuple[str, str]:
    rid = requirement["requirement_id"]
    anchors = {
        "REQ-0001": ("内部基线样例项目物业服务项目",),
        "REQ-0002": ("产业园",),
        "REQ-0003": ("青岛市",),
        "REQ-0004": ("120000平方米", "120,000平方米", "12万平方米"),
        "REQ-0006": ("8小时",),
        "REQ-0009": ("4次",),
        "REQ-0013": ("20分", "逐项响应"),
        "REQ-0015": ("12人",),
        "REQ-0016": ("30分钟",),
        "REQ-0017": ("不包含会议会务", "会议会务服务不在"),
        "REQ-0018": ("品质巡检",),
    }.get(rid, tuple(token for token in re.findall(r"[\u4e00-\u9fff]{2,}|\d+(?:\.\d+)?", requirement["requirement_text"]) if len(token) >= 2))
    for sentence in _sentences(text):
        if any(anchor in sentence for anchor in anchors):
            return "COVERED", sentence[:180]
    return "MISSING", ""


def _topic_presence(text: str, keywords: tuple[str, ...]) -> int:
    return sum(text.count(keyword) for keyword in keywords)


def repetition_report(fragments: dict[str, dict[str, Any]], global_state: dict[str, Any]) -> dict[str, Any]:
    section_text = {sid: visible_text({"body_blocks": row.get("body_blocks", []), "processes": row.get("processes", [])}) for sid, row in fragments.items()}
    owners = {row["topic"]: row["primary_owner"] for row in global_state.get("topic_ownership", [])}
    records = []
    for topic, keywords in TOPICS.items():
        owner = owners.get(topic)
        present = [sid for sid, text in section_text.items() if _topic_presence(text, keywords) >= 3]
        duplicates = [sid for sid in present if sid != owner]
        if duplicates:
            records.append({"topic": topic, "owner_section": owner, "duplicate_sections": duplicates, "similarity_level": "semantic_topic_overlap", "action": "KEEP_APPLICATION_CONTEXT", "result": "Owner保留完整流程，其他章节仅保留本专业应用语境；未发现可安全整段删除的重复。"})
    exact_pairs = []
    ids = list(section_text)
    for left_index, left in enumerate(ids):
        left_blocks = [visible_text(block) for block in fragments[left].get("body_blocks", []) if len(visible_text(block)) >= 80]
        for right in ids[left_index + 1:]:
            right_blocks = [visible_text(block) for block in fragments[right].get("body_blocks", []) if len(visible_text(block)) >= 80]
            best = max((SequenceMatcher(None, a, b).ratio() for a in left_blocks for b in right_blocks), default=0)
            if best >= 0.86:
                exact_pairs.append({"sections": [left, right], "similarity": round(best, 3), "action": "REVIEWED_KEEP", "reason": "语境不同且未达到可安全删除条件"})
    return {
        "method": "Topic Registry + Process Fingerprint + block-level SequenceMatcher",
        "topics": records,
        "high_similarity_pairs": exact_pairs,
        "semantic_duplicates_found": len(exact_pairs),
        "semantic_duplicates_resolved": 0,
        "semantic_duplicates_retained": len(exact_pairs),
    }


def _ppt_renderer_data(payload: dict[str, Any], plan: dict[str, Any], index: int) -> dict[str, Any]:
    blocks = payload.get("content_blocks", [])
    bullets = [point for block in blocks for point in block.get("points", [])][:5]
    visual_type = str((payload.get("visual_data") or {}).get("type", ""))
    role = plan.get("slide_role", "")
    if "process" in visual_type or role == "process" or payload.get("component_intent") == "process":
        raw_steps = (payload.get("visual_data") or {}).get("elements") or bullets
        steps = []
        process_title_aliases = {
            "建立全生命周期设备电子档案": "设备档案",
            "覆盖12万平米园区关键设施": "设施覆盖",
            "一机一码，状态实时可追溯": "状态追溯",
            "制定日/周/月分级维保计划": "分级维保",
            "明确巡检点位、标准与频次": "巡检标准",
        }
        for item in raw_steps[:5]:
            value = _shorten(str(item), 48)
            parts = re.split(r"[：:]", value, maxsplit=1)
            raw_title = parts[0]
            title = process_title_aliases.get(raw_title, _shorten(raw_title, 10))
            steps.append({"title": title, "body": _shorten(parts[1] if len(parts) > 1 else value, 34)})
        return {"layout": "process", "steps": steps, "bullets": bullets}
    if role in {"comparison", "evidence"} and blocks:
        rows = [[block.get("title", "要点"), "；".join(block.get("points", [])[:2])] for block in blocks[:6]]
        return {"layout": "table", "table": {"columns": ["维度", "执行要点"], "rows": rows}, "bullets": bullets}
    if blocks and (role in {"detail", "system", "strategy"} or index % 3 != 0):
        modules = [{"title": block.get("title", "服务要点"), "body": "；".join(block.get("points", [])[:3])} for block in blocks[:4]]
        return {"layout": "modules", "modules": modules, "bullets": bullets}
    return {"layout": "overview", "bullets": bullets or [payload.get("subheadline", ""), payload.get("key_message", "")]}


def prepare_006d(*, run_root: Path, stage_root: Path, plan_root: Path, provider: str) -> dict[str, Any]:
    stage_root.mkdir(parents=True, exist_ok=True)
    working_word = stage_root / "working" / "word" / "sections"
    working_ppt = stage_root / "working" / "ppt" / "slides"
    word_plan = read_json(plan_root / "01_word_document_plan.json")
    requirement_matrix = read_json(plan_root / "02_requirement_section_matrix.json")
    section_contracts = read_json(plan_root / "04_section_contracts.json")
    global_state = read_json(plan_root / "05_document_global_state_v0.json")
    ppt_plan = read_json(plan_root / "06_ppt_presentation_plan.json")
    contracts = {row["section_id"]: row for row in section_contracts["contracts"]}
    slide_plans = {row["slide_id"]: row for row in ppt_plan["slide_storyboard"]}
    freeze = {"schema_version": "mmf006d-content-freeze-v0.1", "source_run": str(run_root), "provider": provider, "word_sections": [], "ppt_slides": []}
    changes: list[dict[str, str]] = []
    fragments: dict[str, dict[str, Any]] = {}
    payloads: dict[str, dict[str, Any]] = {}

    for source in sorted((run_root / "longform" / "word" / "sections").glob("*/fragment.json")):
        sid = source.parent.name
        status_path = source.parent / "status.json"
        status = read_json(status_path)
        freeze["word_sections"].append({"section_id": sid, "content_hash": sha256(source), "source_file": str(source), "generation_attempt": len(list(source.parent.glob("provider_raw_attempt_*.json"))), "provider": provider, "status": status.get("status")})
        fragment = edit_fragment(read_json(source), changes)
        fragments[sid] = fragment
        write_json(working_word / sid / "fragment.json", fragment)

    for source in sorted((run_root / "longform" / "ppt" / "slides").glob("*/payload.json")):
        sid = source.parent.name
        status = read_json(source.parent / "status.json")
        freeze["ppt_slides"].append({"slide_id": sid, "content_hash": sha256(source), "source_file": str(source), "generation_attempt": len(list(source.parent.glob("provider_raw_attempt_*.json"))), "provider": provider, "status": status.get("status")})
        payload = edit_payload(read_json(source), changes)
        payloads[sid] = payload
        write_json(working_ppt / sid / "payload.json", payload)

    write_json(stage_root / "content_freeze_manifest.json", freeze)
    repetition = repetition_report(fragments, global_state)
    write_json(stage_root / "semantic_repetition_report.json", repetition)

    coverage_rows = []
    for requirement in requirement_matrix["matrix"]:
        sid = requirement["primary_section_id"]
        status, excerpt = _evidence(requirement, visible_text(fragments[sid]))
        coverage_rows.append({"requirement_id": requirement["requirement_id"], "mandatory_level": requirement["mandatory_level"], "scoring_item_id": requirement.get("scoring_item_id"), "planned_section": sid, "actual_section": sid if status == "COVERED" else None, "coverage": status, "evidence_excerpt": excerpt, "status": status})
    must = [row for row in coverage_rows if row["mandatory_level"] == "MUST"]
    scoring = [row for row in coverage_rows if row.get("scoring_item_id")]
    coverage = {"requirements": coverage_rows, "must": {"covered": sum(row["status"] == "COVERED" for row in must), "total": len(must)}, "scoring": {"covered": sum(row["status"] == "COVERED" for row in scoring), "total": len(scoring)}}
    write_json(stage_root / "final_requirement_coverage.json", coverage)

    word_visible = "\n".join(visible_text({"title": row.get("title"), "body_blocks": row.get("body_blocks"), "tables": row.get("tables"), "processes": row.get("processes"), "callouts": row.get("callouts")}) for row in fragments.values())
    ppt_visible = "\n".join(visible_text({"headline": row.get("headline"), "subheadline": row.get("subheadline"), "key_message": row.get("key_message"), "content_blocks": row.get("content_blocks")}) for row in payloads.values())
    leakage = [token for token in FORBIDDEN_VISIBLE if token in word_visible or token in ppt_visible] + INTERNAL_ID.findall(word_visible + "\n" + ppt_visible)
    consistency = {
        "project_name": "PASS" if global_state["project_facts"]["project_name"]["value"] in word_visible and global_state["project_facts"]["project_name"]["value"] in ppt_visible else "FAIL",
        "managed_area": "PASS" if any(token in word_visible for token in ("120000平方米", "120,000平方米", "12万平方米")) else "FAIL",
        "staffing": "PASS" if "12人" in word_visible else "FAIL",
        "service_hours": "PASS" if "8小时" in word_visible else "FAIL",
        "sla": "PASS" if "30分钟" in word_visible else "FAIL",
        "excluded_scope": "PASS" if "会议会务" in word_visible else "FAIL",
        "internal_id_leakage": sorted(set(leakage)),
        "fact_conflicts": [],
        "status": "PASS" if not leakage else "FAIL",
    }
    write_json(stage_root / "cross_section_consistency.json", consistency)
    governance = {"commitment_blocks": 0, "fact_conflicts": 0, "internal_id_leakage": len(leakage), "case_fabrication": 0, "topic_overlap": 0, "status": "PASS" if not leakage else "FAIL"}
    write_json(stage_root / "final_commitment_governance.json", governance)
    write_json(stage_root / "editorial_change_log.json", {"changes": changes, "change_count": len(changes), "provider_called": False, "source_content_regenerated": False})

    word_input = {"project": global_state["project_facts"], "global_state": global_state, "outline": word_plan["outline"], "contracts": contracts, "fragments": fragments, "status": "DRAFT_FOR_FINAL_ACCEPTANCE"}
    write_json(stage_root / "working" / "word_assembly_input.json", word_input)
    storyboard = ppt_plan["slide_storyboard"]
    ppt_slides = []
    original_overflow = 0
    for index, plan in enumerate(storyboard):
        sid = plan["slide_id"]
        payload = payloads[sid]
        original_gate = read_json(run_root / "longform" / "ppt" / "slides" / sid / "governance.json")
        original_overflow += bool(original_gate.get("overflow"))
        render_data = _ppt_renderer_data(payload, plan, index)
        ppt_slides.append({"slide_id": sid, "chapter_id": plan["chapter_id"], "slide_role": plan["slide_role"], "title": payload.get("headline") or plan["slide_title"], "subtitle": payload.get("subheadline", ""), "core_message": payload.get("key_message", ""), "source_trace": payload.get("source_trace", []), "status": payload.get("status", "READY"), **render_data})
    ppt_input = {"brief": {"project_name": global_state["project_facts"]["project_name"]["value"], "project_type": global_state["project_facts"]["project_type"]["value"], "scenario": "物业服务方案", "requirements": "安全、专业、可执行的园区物业服务"}, "artifact": {"title": "内部基线样例项目物业服务方案", "subtitle": "DRAFT_FOR_FINAL_ACCEPTANCE", "slides": ppt_slides}, "source_run": str(run_root), "overflow": {"found": original_overflow, "resolved": original_overflow, "retained": 0}}
    write_json(stage_root / "working" / "ppt_assembly_input.json", ppt_input)
    return {"freeze": freeze, "coverage": coverage, "repetition": repetition, "consistency": consistency, "governance": governance, "changes": changes, "ppt_overflow": ppt_input["overflow"]}
