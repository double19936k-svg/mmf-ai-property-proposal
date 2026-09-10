from __future__ import annotations

import copy
import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from governance import apply_artifact_repairs, apply_local_repairs, evaluate_artifact, evaluate_commitments
from governance.text_sanitize import collapse_repeated_conditionals, replace_once, replace_outsourcing_phrase
from planning.planner import SECTION_HARDENING, is_bid_process_text
from providers import ProviderError, ProviderUnavailableError
from providers.capability import apply_to_request, recommended_parallelism, resolve_profile
from providers.execution_reliability import classify_provider_failure, deferred_status
from providers.rate_limit import AdmissionError, default_limiter
from providers.token_usage import TokenAccountant, extract_token_usage, resolve_attempt_tokens
from workflow_timing import union_interval_seconds
from .eta import load_budget_history_samples
from .batch_planner import build_prereq_maps, plan_generation_batches, shrink_batch, split_micro_batches
from .reasoning import classify_generation_failure, classify_generation_outcome, normalize_speed_profile
from .repair_policy import (
    FastRepairBudget,
    classify_section_issue,
    compact_generation_context,
    compact_repair_context,
    repair_prompt_rules,
)
from .scheduler import ConcurrentBatchScheduler


WORD_REQUIRED_KEYS = {
    "section_id", "title", "body_blocks", "tables", "processes", "callouts",
    "cross_references", "claims", "used_requirement_ids", "used_ku_ids", "generation_notes",
}
PPT_REQUIRED_KEYS = {
    "slide_id", "headline", "subheadline", "key_message", "content_blocks", "visual_data",
    "component_intent", "speaker_note_optional", "source_trace", "generation_notes",
}
INTERNAL_ID = re.compile(r"\b(?:REQ|SCR|KU)-[A-Z0-9-]+\b", re.I)
AI_LANGUAGE = re.compile(r"(?:作为AI|本模型|根据提示词|内部治理|Section Contract|Requirement ID|Knowledge Unit)", re.I)
BROKEN_JSON = re.compile(r"```json|\{\s*[\"']\w+[\"']\s*:", re.I)
ROLE_DRIFT = {"物业经理": "项目负责人", "项目经理": "项目负责人"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def public_strings(value: Any, path: str = "") -> list[str]:
    if isinstance(value, dict):
        structural = {"generation_notes", "provider_metadata", "source_trace", "cross_references", "claims", "process_id", "step_order", "type", "layout", "component_intent", "content_source", "component_family_hint", "slide_id", "section_id", "used_requirement_ids", "used_ku_ids", "status"}
        return [text for key, item in value.items() if key not in structural for text in public_strings(item, f"{path}.{key}")]
    if isinstance(value, list):
        return [text for item in value for text in public_strings(item, path)]
    return [value] if isinstance(value, str) else []


def visible_text(value: Any) -> str:
    return "\n".join(public_strings(value))


def content_units(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9]+", text))


def estimate_tokens(value: Any) -> int:
    return max(1, len(json.dumps(value, ensure_ascii=False)) // 4)


def normalize_for_similarity(text: str) -> str:
    return re.sub(r"[\W_]+", "", text)


def keyword_coverage(requirement: str, text: str) -> str:
    if "会议会务" in requirement and "会议会务" in text and re.search(r"(?:不包含|不在本次范围|排除项|不配置|不设置|不承担|不负责|不展开)", text):
        return "COVERED"
    tokens = [x for x in re.findall(r"[\u4e00-\u9fff]{2,}", requirement) if x not in {"明确", "本节", "项目", "服务", "当前", "相关", "不得", "形成", "说明"}]
    if not tokens:
        return "COVERED"
    hits = sum(token in text for token in tokens)
    ratio = hits / len(tokens)
    if ratio >= 0.34:
        return "COVERED"
    # Chinese must-cover items are often semantic instructions rather than text
    # that should be copied verbatim. Character-bigram coverage catches faithful
    # paraphrases while keeping the gate deterministic.
    normalized = re.sub(r"面向本项目|本项目|项目|当前|明确|必须|不得|的|与|及|从|到|但|不将|写成|进行|形成|相关|服务|本节|说明", "", requirement)
    normalized = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", normalized)
    grams = {normalized[index:index + 2] for index in range(max(0, len(normalized) - 1))}
    gram_ratio = (sum(gram in text for gram in grams) / len(grams)) if grams else 0
    return "COVERED" if gram_ratio >= 0.42 else ("PARTIAL" if hits or gram_ratio >= 0.18 else "MISSING")


REQUIRED_OUTPUT_ALIASES = {
    # Customer-facing drafts need not repeat an internal contract label verbatim.
    # Aliases stay deliberately narrow and require an explicit equivalent label.
    "项目事实清单": ("项目核心数据", "项目基本事实", "项目概况"),
    "服务边界清单": ("服务范围与责任边界", "服务范围及责任边界", "服务边界与排除项"),
}


def required_output_coverage(output_name: str, text: str) -> str:
    status = keyword_coverage(output_name, text)
    if status == "COVERED":
        return status
    if any(alias in text for alias in REQUIRED_OUTPUT_ALIASES.get(output_name, ())):
        return "COVERED"
    return status


@dataclass
class LongformGenerationJob:
    job_id: str
    run_id: str
    provider: str
    medium: str
    plan_version: str
    requirement_pack_id: str
    global_state_version: str
    status: str = "LOCAL_ENGINEERING_COMPLETED"
    started_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    current_unit: str | None = None
    total_units: int = 0
    completed_units: list[str] = field(default_factory=list)
    failed_units: list[str] = field(default_factory=list)
    stale_units: list[str] = field(default_factory=list)
    retry_count: int = 0
    deferred_external_events: list[dict[str, Any]] = field(default_factory=list)
    completed_batches: list[str] = field(default_factory=list)
    failed_batches: list[str] = field(default_factory=list)
    pending_batches: list[str] = field(default_factory=list)
    blocked_batches: list[str] = field(default_factory=list)
    retry_queue: list[dict[str, Any]] = field(default_factory=list)
    provider_call_count: int = 0
    provider_calls_total: int = 0
    this_run_calls: int = 0
    this_resume_calls: int = 0
    parallelism: int = 1
    speed_profile: str = "balanced"
    latency_downgrade_count: int = 0
    quality_escalation_count: int = 0
    quality_escalation_requested_count: int = 0
    quality_escalation_applied_count: int = 0
    public_phase: str = "GENERATING"
    active_repair_sections: list[str] = field(default_factory=list)
    qa_events: list[dict[str, Any]] = field(default_factory=list)

    def dump(self, path: Path) -> None:
        write_json(path, asdict(self))

    @classmethod
    def load(cls, path: Path) -> "LongformGenerationJob":
        data = read_json(path)
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in allowed})


class ContextPackBuilder:
    def __init__(self, global_state: dict[str, Any], contracts: dict[str, Any], knowledge_selection: dict[str, Any], dependency_map: dict[str, Any]):
        self.global_state = global_state
        self.contracts = contracts
        self.knowledge = {row["ku_id"]: row for row in knowledge_selection.get("knowledge_usage_contracts", [])}
        self.processes = {row["process_id"]: row for row in contracts.get("process_contracts", [])}
        self.dependencies = dependency_map.get("dependencies", [])

    def word(self, contract: dict[str, Any], incremental_state: dict[str, Any]) -> dict[str, Any]:
        source_ids = set(contract.get("source_requirements", []))
        requirements = [row for row in self.global_state.get("client_requirements", []) if row.get("requirement_id") in source_ids]
        allowed_ids = set(contract.get("allowed_knowledge", []) + contract.get("conditional_knowledge", []))
        knowledge = [
            {key: row.get(key) for key in ("ku_id", "usable_content", "selection_status", "required_conditions", "forbidden_escalations", "language_level")}
            for ku_id, row in self.knowledge.items() if ku_id in allowed_ids
        ]
        relevant_processes = [copy.deepcopy(self.processes[pid]) for pid in contract.get("required_processes", []) if pid in self.processes]
        predecessors = [
            dep["source_section"] for dep in self.dependencies
            if contract["section_id"] in dep.get("dependent_sections", [])
        ]
        summaries = [incremental_state.get("section_summaries", {}).get(sid) for sid in predecessors]
        return {
            "project_facts": self.global_state["project_facts"],
            "confirmed_requirements": requirements,
            "section_contract": copy.deepcopy(contract),
            "allowed_knowledge": knowledge,
            "relevant_process_contracts": relevant_processes,
            "canonical_terms": self.global_state["canonical_terms"],
            "canonical_roles": self.global_state["canonical_roles"],
            "confirmed_governance": {key: copy.deepcopy(self.global_state[key]) for key in ("staffing", "service_hours", "sla_kpi", "service_scope", "excluded_scope", "commitment_registry")},
            "topic_ownership": self.global_state["topic_ownership"],
            "previous_section_summaries": [row for row in summaries if row],
            "used_core_arguments": incremental_state.get("used_core_arguments", []),
            "cross_section_dependencies": [row for row in self.dependencies if row.get("source_section") == contract["section_id"] or contract["section_id"] in row.get("dependent_sections", [])],
        }

    def ppt(self, slide: dict[str, Any], chapter: dict[str, Any]) -> dict[str, Any]:
        source_ids = set(slide.get("source_requirements", []))
        requirements = [row for row in self.global_state.get("client_requirements", []) if row.get("requirement_id") in source_ids]
        return {
            "slide": copy.deepcopy(slide),
            "chapter": {key: copy.deepcopy(chapter.get(key)) for key in ("chapter_id", "chapter_title", "narrative_goal", "key_messages")},
            "project_facts": self.global_state["project_facts"],
            "confirmed_requirements": requirements,
            "canonical_terms": self.global_state["canonical_terms"],
            "confirmed_governance": {key: copy.deepcopy(self.global_state[key]) for key in ("staffing", "service_hours", "sla_kpi", "excluded_scope")},
            "ppt_topic_ownership": slide.get("ppt_topic_owner"),
        }


def _governance_artifact(fragment: dict[str, Any]) -> dict[str, Any]:
    return {"artifact": {"content": public_strings(fragment)}}


def _fact_drift(text: str, global_state: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    confirmed_staff = global_state.get("staffing", {}).get("minimum_staffing")
    confirmed_hours = global_state.get("service_hours", {}).get("daily_service_hours")
    confirmed_sla = global_state.get("sla_kpi", {}).get("complaint_first_response_minutes")
    for value in re.findall(r"(\d+)[ \t]*人", text):
        if confirmed_staff and int(value) != int(confirmed_staff): issues.append(f"staffing:{value}")
    for value in re.findall(r"(\d+)[ \t]*小时", text):
        if confirmed_hours and int(value) != int(confirmed_hours): issues.append(f"service_hours:{value}")
    for value in re.findall(r"(\d+)[ \t]*分钟", text):
        if confirmed_sla and int(value) != int(confirmed_sla): issues.append(f"sla:{value}")
    if "24小时" in text and confirmed_hours == 8: issues.append("service_hours:24")
    return sorted(set(issues))


def _local_repetition(blocks: list[dict[str, Any]]) -> int:
    texts = [normalize_for_similarity(visible_text(row)) for row in blocks]
    count = 0
    for left, right in zip(texts, texts[1:]):
        if min(len(left), len(right)) >= 40 and SequenceMatcher(None, left, right).ratio() >= 0.82:
            count += 1
    return count


def meaningful_section_body(fragment: dict[str, Any], minimum_units: int = 20) -> tuple[bool, int]:
    """Measure customer-visible body, excluding title, IDs and generation notes."""
    payload = {
        "body_blocks": fragment.get("body_blocks") or [],
        "tables": fragment.get("tables") or [],
        "processes": fragment.get("processes") or [],
        "callouts": fragment.get("callouts") or [],
    }
    units = content_units(visible_text(payload))
    return units >= minimum_units, units


def depth_repair_required(gate: dict[str, Any], contract: dict[str, Any]) -> bool:
    """Require depth repair only for materially short or contract-incomplete sections.

    A small target-length miss remains a warning when the Section Contract is fully
    satisfied.  A severely short response still needs continuation even when simple
    keyword coverage happens to pass.
    """
    if gate.get("length_status") != "SECTION_UNDER_LENGTH":
        return False
    target_min = int((contract.get("target_words") or {}).get("min") or 0)
    units = int(gate.get("content_units") or 0)
    severely_short = bool(target_min and units < target_min * 0.55)
    return severely_short or not bool(gate.get("contract_complete"))


def empty_subheadings(fragment: dict[str, Any]) -> list[str]:
    """Find explicit subheadings that have no body/list/table before the next heading."""
    blocks = list(fragment.get("body_blocks") or [])
    missing: list[str] = []
    heading_types = {"heading", "subheading", "heading2", "heading3"}
    for index, block in enumerate(blocks):
        if not isinstance(block, dict) or str(block.get("type") or "").lower() not in heading_types:
            continue
        title = "".join(public_strings(block)).strip() or f"body_blocks[{index}]"
        body_units = 0
        for following in blocks[index + 1:]:
            if isinstance(following, dict) and str(following.get("type") or "").lower() in heading_types:
                break
            body_units += content_units(visible_text(following))
        if body_units == 0:
            missing.append(title)
    return missing


def _excluded_scope_violations(text: str, global_state: dict[str, Any]) -> list[str]:
    """Allow boundary declarations while blocking operational expansion of excluded work."""
    issues: list[str] = []
    for row in global_state.get("excluded_scope", []):
        excluded = row.get("text", "")
        marker = "会议会务" if "会议会务" in excluded else excluded
        if not marker or marker not in text:
            continue
        sentences = [part for part in re.split(r"[。；;\n]", text) if marker in part]
        for sentence in sentences:
            boundary_only = re.search(r"(?:不包含|不在本次.{0,4}范围|超出.{0,8}范围|排除|排除项|不属于|不展开|不予展开|不配置|不设置|不承担|不负责|不涉及|不视为|按变更流程|另行确认|另行约定|另行协商|仅作边界|待澄清)", sentence)
            operational = re.search(r"(?:负责|执行|安排|配置|提供|组织|实施|保障|承诺)", sentence)
            if operational and not boundary_only:
                issues.append(excluded)
                break
    return issues


def _must_cover_status(item: str, text: str, section_id: str = "") -> str:
    if is_bid_process_text(item):
        return "N/A"
    core = list((SECTION_HARDENING.get(section_id) or {}).get("must_cover") or [])
    status = keyword_coverage(item, text)
    if core and item not in core and status == "MISSING":
        return "N/A"
    return status


def evaluate_word_fragment(fragment: dict[str, Any], contract: dict[str, Any], global_state: dict[str, Any], kuc_rows: list[dict[str, Any]]) -> dict[str, Any]:
    missing_keys = sorted(WORD_REQUIRED_KEYS - set(fragment))
    text = visible_text(fragment)
    coverage = {item: _must_cover_status(item, text, str(contract.get("section_id") or "")) for item in contract["must_cover"]}
    processes = {row["process_id"]: {"steps_total": len(row["steps"]), "steps_present": sum(step in text for step in row["steps"])} for row in kuc_rows}
    process_missing = [pid for pid, row in processes.items() if row["steps_total"] and row["steps_present"] / row["steps_total"] < 0.55]
    output_coverage = {str(item): required_output_coverage(str(item), text) for item in (contract.get("required_outputs") or [])}
    output_missing = [item for item, status in output_coverage.items() if status == "MISSING"]
    required_tables = list(contract.get("required_tables") or [])
    table_missing = bool(required_tables and not (fragment.get("tables") or []))
    body_ok, body_units = meaningful_section_body(fragment)
    empty_headings = empty_subheadings(fragment)
    target = contract["target_words"]
    units = content_units(text)
    length_status = "PASS" if target["min"] * 0.85 <= units <= target["max"] * 1.15 else ("SECTION_UNDER_LENGTH" if units < target["min"] * 0.85 else "SECTION_OVER_LENGTH")
    commitment = evaluate_commitments(global_state, kuc_rows, _governance_artifact(fragment))
    artifact = evaluate_artifact(_governance_artifact(fragment), canonical_facts=global_state)
    fact_drift = _fact_drift(text, global_state)
    role_drift = [old for old in ROLE_DRIFT if old in text]
    scope_violation = _excluded_scope_violations(text, global_state)
    id_leaks = INTERNAL_ID.findall(text)
    repetition = _local_repetition(fragment.get("body_blocks", []))
    blocking_missing = any(value == "MISSING" for value in coverage.values())
    contract_complete = bool(body_ok and not blocking_missing and not process_missing and not output_missing and not table_missing and not empty_headings)
    blocking = bool(missing_keys or not body_ok or empty_headings or blocking_missing or process_missing or output_missing or table_missing or fact_drift or role_drift or scope_violation or id_leaks or AI_LANGUAGE.search(text) or BROKEN_JSON.search(text) or commitment["status"] == "BLOCK" or artifact["status"] == "BLOCK")
    return {
        "status": "BLOCK" if blocking else ("WARNING" if length_status != "PASS" or any(value == "PARTIAL" for value in coverage.values()) or artifact["status"] == "AUTO_REPAIR" else "PASS"),
        "missing_keys": missing_keys, "must_cover": coverage, "process_gate": processes, "process_missing": process_missing,
        "content_units": units, "length_status": length_status, "fact_drift": fact_drift, "role_drift": role_drift,
        "scope_violation": scope_violation, "internal_id_leaks": id_leaks, "local_repetition": repetition,
        "meaningful_body": body_ok, "meaningful_body_units": body_units, "empty_headings": empty_headings,
        "required_output_coverage": output_coverage, "required_outputs_missing": output_missing,
        "required_tables_missing": required_tables if table_missing else [], "contract_complete": contract_complete,
        "commitment": commitment, "artifact_qa": artifact,
    }


SLIDE_BUDGET = {"opening": 80, "insight": 220, "strategy": 220, "system": 240, "process": 260, "detail": 260, "evidence": 220, "case": 180, "summary": 180, "comparison": 220}


def evaluate_ppt_payload(payload: dict[str, Any], slide: dict[str, Any], global_state: dict[str, Any]) -> dict[str, Any]:
    missing_keys = sorted(PPT_REQUIRED_KEYS - set(payload))
    text = visible_text({key: payload.get(key) for key in ("headline", "subheadline", "key_message", "content_blocks")})
    units = content_units(text)
    budget = SLIDE_BUDGET.get(slide.get("slide_role"), 220)
    overflow = units > budget
    id_leaks = INTERNAL_ID.findall(text)
    fact_drift = _fact_drift(text, global_state)
    case_fabrication = bool(slide.get("evidence_requirement") == "SOURCE_REQUIRED" and payload.get("status") != "SOURCE_REQUIRED" and any(token in text for token in ("案例显示", "实际项目", "客户反馈", "提升了")))
    topic_overlap = False
    if slide.get("chapter_id") == "P07" and slide.get("ppt_topic_owner") == "industrial_logistics_coordination":
        topic_overlap = sum(token in text for token in ("门岗", "访客登记", "车辆分类", "通行证")) >= 2
    language_warning = any(token in text for token in ("Section", "Contract", "Requirement", "逐条响应", "内部门禁", "责任链"))
    blocking = bool(missing_keys or id_leaks or fact_drift or case_fabrication or topic_overlap or AI_LANGUAGE.search(text) or BROKEN_JSON.search(text))
    return {"status": "BLOCK" if blocking else ("WARNING" if overflow or language_warning else "PASS"), "missing_keys": missing_keys, "content_units": units, "text_budget": budget, "overflow": overflow, "internal_id_leaks": id_leaks, "fact_drift": fact_drift, "case_fabrication": case_fabrication, "topic_overlap": topic_overlap, "language_warning": language_warning}


def _repair_customer_text(value: Any) -> Any:
    if isinstance(value, dict): return {key: _repair_customer_text(item) for key, item in value.items()}
    if isinstance(value, list): return [_repair_customer_text(item) for item in value]
    if isinstance(value, str):
        safe_conditioning = {
            "全天候": "在经确认的服务时段内",
            "封闭管控": "可结合园区开放条件评估分区管控",
        }
        for old, new in safe_conditioning.items(): value = replace_once(value, old, new)
        presentation_polish = {
            "SLA": "服务响应要求", "SOP": "标准作业流程", "LOTO": "上锁挂牌", "APP": "移动端",
            "staffing": "人员配置", "Logo": "标识", "无死角": "重点区域覆盖", "零延误": "减少延误",
            "零干扰": "减少干扰", "自动派单": "按规则派单", "智能匹配": "按专业与位置匹配",
            "电子签到": "签到记录", "增派机动岗": "动态调配现场力量", "专人押运": "按安全要求落实押运责任",
            "全程监控": "按安全要求实施过程管控",
        }
        for old, new in presentation_polish.items(): value = replace_once(value, old, new)
        value = replace_once(value, "响应时限", "响应安排")
        value = replace_once(value, "外委单位", "__CONDITIONAL_SUPPLIER__")
        value = replace_once(value, "外委管理", "__CONDITIONAL_SUPPLIER_MGMT__")
        value = replace_outsourcing_phrase(value)
        value = value.replace("__CONDITIONAL_SUPPLIER__", "如采用外委方式，可按合同要求管理的相关供方")
        value = value.replace("__CONDITIONAL_SUPPLIER_MGMT__", "如采用外委方式，可按合同要求实施供方管理")
        value = replace_once(value, "24小时", "经确认的服务时段")
        from governance.customer_hygiene import rewrite_customer_hygiene
        value = rewrite_customer_hygiene(value)
        value = re.sub(r"(?<!\d)(?!12(?:\.0)?[ \t]*人)(\d+(?:\.\d+)?)[ \t]*人", "按现场条件配置的人员", value)
        value = re.sub(r"(?<!\d)(?!8(?:\.0)?[ \t]*小时)(\d+(?:\.\d+)?)[ \t]*小时", "合同约定的服务时段", value)
        value = re.sub(r"(?<!\d)(?!4(?:\.0)?[ \t]*次)(\d+(?:\.\d+)?)[ \t]*次", "合同约定的作业频次", value)
        value = re.sub(r"\d+(?:\.\d+)?[ \t]*[%％]", "合同约定的指标", value)
        for old, new in ROLE_DRIFT.items(): value = replace_once(value, old, new)
        value = INTERNAL_ID.sub("", value)
        value = collapse_repeated_conditionals(value)
        return re.sub(r"\s{2,}", " ", value).strip()
    return value


class LongformGenerationFactory:
    def __init__(self, *, run_root: Path, provider: Any, provider_name: str, inputs: dict[str, Any]):
        self.run_root = run_root.resolve()
        self.provider = provider
        self.provider_name = provider_name
        self.inputs = inputs
        self.word_root = self.run_root / "longform" / "word"
        self.ppt_root = self.run_root / "longform" / "ppt"
        self.builder = ContextPackBuilder(inputs["global_state"], inputs["section_contracts"], inputs["knowledge_selection"], inputs["dependency_map"])
        self.incremental_state = copy.deepcopy(inputs["global_state"])
        self.incremental_state.update({"completed_sections": [], "used_core_arguments": [], "used_processes": [], "section_summaries": {}, "cross_references_created": []})
        config = getattr(provider, "config", {}) if provider is not None else {}
        self.speed_profile = normalize_speed_profile(inputs.get("speed_profile") or (config.get("speed_profile") if isinstance(config, dict) else None) or "balanced")
        self.capability = resolve_profile(provider_name, config if isinstance(config, dict) else {}, speed_profile=self.speed_profile, stage="draft")
        self.require_section_min = bool(inputs.get("require_section_min", True))
        self.provider_call_count = 0
        self.provider_calls_total = 0
        self.checkpoint_lock = threading.RLock()
        self.generation_seconds = 0.0
        self.provider_wait_seconds = 0.0
        self.admission_wait_seconds = 0.0
        self.admission_attempts = 0
        self.latency_downgrade_count = 0
        self.quality_escalation_count = 0
        self.quality_escalation_requested_count = 0
        self.quality_escalation_applied_count = 0
        self.history_path = inputs.get("history_path")
        self.max_parallelism = recommended_parallelism(provider_name, inputs.get("max_parallelism"))
        self.soft_latency_observations = 0
        self.soft_latency_policy = "NONE"
        self.run_started_monotonic = None
        self.this_run_calls = 0
        self.this_resume_calls = 0
        self._resume_session = False
        self._scheduler: ConcurrentBatchScheduler | None = None
        self.rate_limiter = inputs.get("rate_limiter") or default_limiter()
        self.token_accountant = TokenAccountant()
        self.provider_attempt_samples: list[dict[str, Any]] = []
        self.repair_intervals: list[tuple[float, float]] = []
        self.token_accountant_cumulative = TokenAccountant()
        self.executed_section_group_sizes: list[int] = []
        self._last_retry_after: float | None = None
        self.latency_only_downgrade = False
        self.initial_generation_calls = 0
        self.continuation_calls = 0
        self.section_repair_calls = 0
        self.retry_calls = 0
        self.local_repair_count = 0
        self.repair_skipped_low_value = 0
        self.repair_escalated_critical = 0
        self.repair_budget: FastRepairBudget | None = None
        self.repair_provider_seconds = 0.0
        self.initial_generation_seconds = 0.0
        self.escalation_audit: dict[str, Any] = {
            "quality_escalation_requested": False,
            "quality_escalation_applied": False,
            "reasoning_before": ((self.capability.get("effective_settings") or {}).get("effective_reasoning")),
            "reasoning_after": ((self.capability.get("effective_settings") or {}).get("effective_reasoning")),
            "escalation_unavailable_reason": None,
        }
        self.performance = {
            "logical_section_count": 0,
            "generation_batch_count": 0,
            "provider_call_count": 0,
            "provider_calls_total": 0,
            "this_run_calls": 0,
            "this_resume_calls": 0,
            "parallelism": self.max_parallelism,
            "mode": self.speed_profile,
            "planning_reasoning": "high",
            "planning_execution": "local_deterministic",
            "draft_reasoning": ((self.capability.get("effective_settings") or {}).get("effective_reasoning")),
            "repair_reasoning": "not_invoked",
            "latency_downgrade_count": 0,
            "quality_escalation_count": 0,
            "quality_escalation_requested": False,
            "quality_escalation_applied": False,
            "reasoning_before": ((self.capability.get("effective_settings") or {}).get("effective_reasoning")),
            "reasoning_after": ((self.capability.get("effective_settings") or {}).get("effective_reasoning")),
            "escalation_unavailable_reason": None,
        }

    def _snapshot(self, root: Path) -> None:
        for name in ("word_plan", "requirement_matrix", "section_contracts", "global_state", "ppt_plan", "dependency_map"):
            if name in self.inputs:
                write_json(root / "plan_snapshot" / f"{name}.json", self.inputs[name])

    def _job(self, root: Path, medium: str, total: int) -> LongformGenerationJob:
        status_path = root / "status.json"
        if status_path.is_file():
            return LongformGenerationJob.load(status_path)
        job = LongformGenerationJob(job_id=f"{self.run_root.name}-{medium.lower()}", run_id=self.run_root.name, provider=self.provider_name, medium=medium, plan_version="MMF-006B-PATCH-V0.1", requirement_pack_id=str(self.inputs["requirement_matrix"].get("pack_id", "")), global_state_version=str(self.inputs["global_state"].get("schema_version", "")), total_units=total, status="EXTERNAL_PROVIDER_PENDING")
        job.dump(status_path)
        self._snapshot(root)
        return job

    @staticmethod
    def _word_prompt(context: dict[str, Any], repair: list[str] | None = None) -> dict[str, Any]:
        contract = context["section_contract"]
        system = "你是资深物业服务方案撰稿人，只负责当前Section。禁止工具、文件、调查和追加上下文。只返回JSON。"
        rules = ["不得新增目录或改变Section目的", "不得完整展开其他Section主责Topic", "正文不得出现REQ/SCR/KU等内部ID", "不得输出AI说明", "不得新增人员、频次、时限、设备或商业承诺", "使用项目负责人等Canonical角色", "以动作、职责、异常路径、成果、记录和检查方法形成可执行初稿"]
        if repair: rules.extend(repair)
        pack = context.get("repair_pack") or context
        lead = "根据修订包只修补当前Section缺口，保留已有有效正文，禁止整章重写。" if context.get("repair_pack") else "根据Context Pack生成一个Word Section结构化Content Fragment。"
        prompt = lead + "目标内容量为{min}～{max}个中文内容单位，避免理念重复和机械注水。根字段必须且只能包含section_id,title,body_blocks,tables,processes,callouts,cross_references,claims,used_requirement_ids,used_ku_ids,generation_notes。body_blocks使用paragraph/subheading/bullet_group/numbered_steps/note/summary等type与content。你已经获得全部信息，禁止调用工具、读取文件、继续调查或要求补充材料。规则：\n- {rules}\nContext Pack：\n{context}".format(min=contract["target_words"]["min"], max=contract["target_words"]["max"], rules="\n- ".join(rules), context=json.dumps(pack, ensure_ascii=False))
        return {"system": system, "prompt": prompt}

    @staticmethod
    def _ppt_prompt(context: dict[str, Any], repair: list[str] | None = None) -> dict[str, Any]:
        slide = context["slide"]
        rules = ["只生成当前一页，不改变Storyboard", "不得读取或总结Word正文", "像面向甲方的物业服务方案汇报", "正文不得显示REQ/SCR/KU内部ID", "不得编造案例", "P04主讲通用通行秩序，P07主讲生产物流与业务协同"]
        if repair: rules.extend(repair)
        prompt = "根据Context Pack生成一个PPT Slide Content Payload。根字段必须且只能包含slide_id,headline,subheadline,key_message,content_blocks,visual_data,component_intent,speaker_note_optional,source_trace,generation_notes。content_blocks应短句化，避免Word长段。你已经获得全部信息，禁止调用工具、读取文件、继续调查或要求补充材料。规则：\n- {rules}\nContext Pack：\n{context}".format(rules="\n- ".join(rules), context=json.dumps(context, ensure_ascii=False))
        return {"system": "你是物业服务方案PPT内容策划，只负责当前Slide。禁止工具、文件和调查。只返回JSON。", "prompt": prompt}

    def _restore_call_accounting(self, job: LongformGenerationJob) -> None:
        historic = int(getattr(job, "provider_calls_total", 0) or 0) or int(job.provider_call_count or 0)
        checkpoint_path = self.run_root / "checkpoint" / "state.json"
        if checkpoint_path.is_file():
            try:
                previous = read_json(checkpoint_path)
                historic = max(
                    historic,
                    int(previous.get("provider_calls_total") or 0),
                    int(previous.get("provider_call_count") or 0),
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                pass
        self.provider_calls_total = historic
        self.provider_call_count = historic
        self.this_run_calls = 0
        self.this_resume_calls = 0
        self.admission_attempts = 0
        self.token_accountant = TokenAccountant()
        if checkpoint_path.is_file():
            try:
                previous = read_json(checkpoint_path)
                self.token_accountant_cumulative.restore(previous.get("token_accounting_cumulative") or previous.get("token_accounting"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                pass
        # Resume is any prior attempted job, not only a job that already has PASS sections.
        # Historic calls, failed/blocked batches, or retry state all count as resume.
        self._resume_session = bool(
            historic > 0
            or job.completed_units
            or job.completed_batches
            or job.failed_units
            or job.failed_batches
            or getattr(job, "blocked_batches", None)
            or job.retry_queue
            or job.retry_count
        )

    def _ordered_unique(self, values: list[str] | None, preferred: list[str] | None = None) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for item in list(preferred or []) + list(values or []):
            if not item or item in seen:
                continue
            seen.add(item)
            ordered.append(item)
        return ordered

    def _normalize_batch_state(self, job: LongformGenerationJob, plan_batches: list[dict[str, Any]] | None = None, scheduled: dict[str, Any] | None = None) -> None:
        """Keep completed/pending/failed mutually exclusive. completed wins."""
        with self.checkpoint_lock:
            plan_batches = list(plan_batches or [])
            all_ids = self._ordered_unique([str(row.get("batch_id") or "") for row in plan_batches], job.pending_batches + job.completed_batches + job.failed_batches + list(getattr(job, "blocked_batches", []) or []))
            by_id = {str(row.get("batch_id") or ""): row for row in plan_batches if row.get("batch_id")}
            completed = set(job.completed_batches or [])
            failed = set(job.failed_batches or [])
            blocked = set(getattr(job, "blocked_batches", []) or [])
            if scheduled:
                completed.update(scheduled.get("completed_batches") or [])
                completed.update(scheduled.get("skipped_batches") or [])
                failed.update(scheduled.get("failed_batches") or [])
                blocked.update(scheduled.get("blocked_batches") or [])
                all_ids = self._ordered_unique(all_ids, list(completed) + list(failed) + list(blocked) + list(scheduled.get("pending_batches") or []))
            for row in plan_batches:
                bid = str(row.get("batch_id") or "")
                sids = list(row.get("section_ids") or [])
                if bid and sids and all(self._section_is_pass(sid) for sid in sids):
                    completed.add(bid)
            completed -= {""}
            failed -= completed
            blocked -= completed
            failed -= blocked
            pending = [bid for bid in all_ids if bid and bid not in completed and bid not in failed and bid not in blocked]
            retry_queue = []
            for item in list(job.retry_queue or []) + list((scheduled or {}).get("retry_queue") or []):
                bid = item.get("batch_id") if isinstance(item, dict) else item
                if not bid or bid in completed:
                    continue
                retry_queue.append(item if isinstance(item, dict) else {"batch_id": bid})
            job.completed_batches = [bid for bid in all_ids if bid in completed] or self._ordered_unique(list(completed))
            job.failed_batches = [bid for bid in all_ids if bid in failed] or self._ordered_unique(list(failed))
            job.blocked_batches = [bid for bid in all_ids if bid in blocked] or self._ordered_unique(list(blocked))
            job.pending_batches = pending
            job.retry_queue = retry_queue
            job.provider_call_count = self.provider_calls_total
            job.provider_calls_total = self.provider_calls_total
            job.this_run_calls = self.this_run_calls
            job.this_resume_calls = self.this_resume_calls

    def _checkpoint(self, job: LongformGenerationJob) -> None:
        with self.checkpoint_lock:
            job.provider_call_count = self.provider_calls_total
            job.provider_calls_total = self.provider_calls_total
            job.this_run_calls = self.this_run_calls
            job.this_resume_calls = self.this_resume_calls
            job.parallelism = self.max_parallelism
            job.speed_profile = self.speed_profile
            job.latency_downgrade_count = self.latency_downgrade_count
            job.quality_escalation_count = self.quality_escalation_count
            job.quality_escalation_requested_count = self.quality_escalation_requested_count
            job.quality_escalation_applied_count = self.quality_escalation_applied_count
            completed = list(dict.fromkeys(job.completed_units))
            failed = list(dict.fromkeys(job.failed_units))
            job.completed_units = completed
            job.failed_units = failed
            job.dump(self.word_root / "status.json")
            snapshot = {
                "schema_version": "longform-checkpoint-v0.2",
                "run_id": job.run_id,
                "provider": job.provider,
                "status": job.status,
                "public_phase": job.public_phase,
                "active_repair_sections": list(job.active_repair_sections),
                "qa_events": list(job.qa_events),
                "current_unit": job.current_unit,
                "completed_units": list(completed),
                "completed_sections": list(completed),
                "failed_units": list(failed),
                "completed_batches": list(job.completed_batches),
                "failed_batches": list(job.failed_batches),
                "pending_batches": list(job.pending_batches),
                "blocked_batches": list(getattr(job, "blocked_batches", []) or []),
                "retry_queue": list(job.retry_queue),
                "retry_count": job.retry_count,
                "provider_call_count": self.provider_calls_total,
                "provider_calls_total": self.provider_calls_total,
                "this_run_calls": self.this_run_calls,
                "this_resume_calls": self.this_resume_calls,
                "admission_attempts": self.admission_attempts,
                "token_accounting": self.token_accountant.summary(),
                "token_accounting_cumulative": self.token_accountant_cumulative.summary(),
                "parallelism": self.max_parallelism,
                "speed_profile": self.speed_profile,
                "quality_escalation_requested": bool(self.escalation_audit.get("quality_escalation_requested")),
                "quality_escalation_applied": bool(self.escalation_audit.get("quality_escalation_applied")),
                "reasoning_before": self.escalation_audit.get("reasoning_before"),
                "reasoning_after": self.escalation_audit.get("reasoning_after"),
                "escalation_unavailable_reason": self.escalation_audit.get("escalation_unavailable_reason"),
                "updated_at": now_iso(),
                "resume_from_checkpoint": True,
                "invariants": {
                    "completed_intersect_pending": sorted(set(job.completed_batches) & set(job.pending_batches)),
                    "completed_intersect_failed": sorted(set(job.completed_batches) & set(job.failed_batches)),
                    "pending_intersect_failed": sorted(set(job.pending_batches) & set(job.failed_batches)),
                },
            }
            write_json(self.run_root / "checkpoint" / "state.json", snapshot)

    def _record_qa_event(self, job: LongformGenerationJob, *, sid: str, check: str, result: str, reason: str, repair_attempt: int = 0) -> None:
        with self.checkpoint_lock:
            job.qa_events.append({
                "section_id": sid,
                "check": check,
                "result": result,
                "repair_reason": reason,
                "repair_attempt": repair_attempt,
                "recoverable": result != "TERMINAL_BLOCKED",
                "recorded_at": now_iso(),
            })
            job.qa_events = job.qa_events[-100:]
        self._checkpoint(job)

    def _section_schema(self) -> dict[str, Any]:
        properties = {
            "section_id": {"type": "string"},
            "title": {"type": "string"},
            "body_blocks": {"type": "array"},
            "tables": {"type": "array"},
            "processes": {"type": "array"},
            "callouts": {"type": "array"},
            "cross_references": {"type": "array"},
            "claims": {"type": "array"},
            "used_requirement_ids": {"type": "array"},
            "used_ku_ids": {"type": "array"},
            "generation_notes": {"type": "array"},
        }
        return {"type": "object", "properties": properties, "required": sorted(WORD_REQUIRED_KEYS)}

    def _profile_for(self, stage: str, failure_class: str | None = None, batch_size: int = 1) -> dict[str, Any]:
        config = getattr(self.provider, "config", {}) if self.provider is not None else {}
        kind = str(failure_class or "").upper()
        model_name = ""
        if isinstance(config, dict):
            model_name = str(config.get("model") or config.get("model_alias") or "")
        samples = load_budget_history_samples(
            Path(self.history_path) if self.history_path else None,
            provider=self.provider_name,
            model=model_name,
            mode=self.speed_profile,
            stage=stage,
            batch_size=batch_size,
        )
        return resolve_profile(
            self.provider_name,
            config if isinstance(config, dict) else {},
            speed_profile=self.speed_profile,
            stage=stage,
            failure_class=kind or None,
            batch_size=batch_size,
            history_samples=samples,
        )

    def _record_escalation(self, profile: dict[str, Any], failure_class: str | None) -> None:
        effective = profile.get("effective_settings") or {}
        requested = bool(effective.get("quality_escalation_requested"))
        applied = bool(effective.get("quality_escalation_applied"))
        kind = str(failure_class or "").upper()
        if requested:
            self.quality_escalation_requested_count += 1
        if applied:
            self.quality_escalation_applied_count += 1
            self.quality_escalation_count += 1
            self.performance["repair_reasoning"] = effective.get("effective_reasoning") or effective.get("reasoning_after")
        elif kind in {"LATENCY_FAILURE", "TIMEOUT", "SOFT_LATENCY_EXCEEDED"} and effective.get("reasoning_adjustment") == "LATENCY_DOWNGRADE":
            if effective.get("reasoning_after") and effective.get("reasoning_after") != effective.get("reasoning_before"):
                self.latency_downgrade_count += 1
        acc = self.escalation_audit
        acc["quality_escalation_requested"] = bool(acc.get("quality_escalation_requested") or requested)
        acc["quality_escalation_applied"] = bool(acc.get("quality_escalation_applied") or applied)
        if requested or applied or effective.get("escalation_unavailable_reason"):
            acc["reasoning_before"] = effective.get("reasoning_before")
            acc["reasoning_after"] = effective.get("reasoning_after") or effective.get("effective_reasoning")
            acc["escalation_unavailable_reason"] = effective.get("escalation_unavailable_reason")

    def _batch_schema(self, batch_id: str | None = None) -> dict[str, Any]:
        batch_id_schema: dict[str, Any] = {"type": "string"}
        if batch_id:
            batch_id_schema["const"] = batch_id
            batch_id_schema["description"] = f"Must equal CURRENT_BATCH_ID ({batch_id})"
        required = ["batch_id", "sections"] if batch_id else ["sections"]
        return {
            "type": "object",
            "properties": {
                "batch_id": batch_id_schema,
                "sections": {"type": "array", "items": self._section_schema()},
            },
            "required": required,
        }

    def _invoke(self, request: dict[str, Any], task_dir: Path, *, stage: str = "draft", failure_class: str | None = None, batch_size: int = 1) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        started = time.monotonic()
        profile = self._profile_for(stage, failure_class, batch_size)
        payload = apply_to_request(request, profile)
        if payload.get("generation_mode") == "longform_batch":
            payload["json_schema"] = self._batch_schema(payload.get("batch_id") or request.get("batch_id"))
        else:
            payload.setdefault("json_schema", self._section_schema())
        soft = float((profile.get("effective_settings") or {}).get("soft_latency_budget") or 0)
        hard = float((profile.get("effective_settings") or {}).get("hard_timeout") or payload.get("timeout_seconds") or 300)
        estimated_input = estimate_tokens(payload.get("prompt") or "")
        estimated_output = int(payload.get("max_tokens") or estimated_input)
        provider_config = getattr(self.provider, "config", {}) if self.provider is not None else {}
        if not isinstance(provider_config, dict):
            provider_config = {}
        model_name = str((profile.get("effective_settings") or {}).get("model") or provider_config.get("model") or "")
        account_ref = str(provider_config.get("credential_ref") or provider_config.get("endpoint_alias") or "")
        reservation = None
        rate_meta = {"enforced": False, "provenance": "unknown_limits"}
        with self.checkpoint_lock:
            self.admission_attempts += 1
        try:
            admit_started = time.monotonic()
            reservation = self.rate_limiter.admit(
                provider_name=self.provider_name,
                model=model_name,
                account_ref=account_ref,
                config=provider_config,
                estimated_input=estimated_input,
                estimated_output=estimated_output,
                deadline_seconds=max(1.0, float(payload.get("timeout_seconds") or hard or 30)),
                retry_after=self._last_retry_after,
            )
            with self.checkpoint_lock:
                self.admission_wait_seconds += max(0.0, time.monotonic() - admit_started)
            rate_meta = {
                "enforced": bool(reservation.enforced),
                "provenance": reservation.provenance,
                "rpm": reservation.rpm,
                "tpm": reservation.tpm,
                "context_key": reservation.context_key,
            }
        except AdmissionError as exc:
            duration = round(time.monotonic() - started, 3)
            with self.checkpoint_lock:
                self.admission_wait_seconds += duration
            code = str(getattr(exc, "error_code", "RATE_LIMIT") or "RATE_LIMIT")
            tokens = resolve_attempt_tokens(actual=None, estimated_input=estimated_input, estimated_output=0)
            audit = {
                "status": "ADMISSION_REJECTED",
                "error_code": code,
                "error": str(exc),
                "duration_seconds": duration,
                "invoked": False,
                "input_tokens_estimate": estimated_input,
                "output_tokens_estimate": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "token_provenance": "estimate",
                "finish_reason": "error",
                "requested_settings": profile.get("requested_settings"),
                "effective_settings": profile.get("effective_settings"),
                "latency_soft_exceeded": False,
                "latency_hard_exceeded": False,
                "stage": stage,
                "rate_limit_enforcement": rate_meta,
                "admission_wait_seconds": duration,
                "quality_escalation_requested": bool((profile.get("effective_settings") or {}).get("quality_escalation_requested")),
                "quality_escalation_applied": bool((profile.get("effective_settings") or {}).get("quality_escalation_applied")),
                "reasoning_before": (profile.get("effective_settings") or {}).get("reasoning_before"),
                "reasoning_after": (profile.get("effective_settings") or {}).get("reasoning_after"),
                "escalation_unavailable_reason": (profile.get("effective_settings") or {}).get("escalation_unavailable_reason"),
            }
            return None, audit
        try:
            invoke_started = time.monotonic()
            with self.checkpoint_lock:
                self.provider_calls_total += 1
                self.provider_call_count = self.provider_calls_total
                self.this_run_calls += 1
                if self._resume_session:
                    self.this_resume_calls += 1
                mode = str(request.get("attempt_mode") or payload.get("attempt_mode") or "")
                if mode in {"generate", "generate_batch"}:
                    self.initial_generation_calls += 1
                elif mode == "continue_section":
                    self.continuation_calls += 1
                    self.section_repair_calls += 1
                elif mode == "repair_section":
                    self.section_repair_calls += 1
                if str(failure_class or "") in {"TIMEOUT", "LATENCY_FAILURE", "RATE_LIMIT"}:
                    self.retry_calls += 1
                self._record_escalation(profile, failure_class)
            output = self.provider.invoke_structured(payload, task_dir)
            duration = round(time.monotonic() - invoke_started, 3)
            with self.checkpoint_lock:
                self.provider_wait_seconds += duration
                if str(request.get("attempt_mode") or "") in {"generate", "generate_batch"}:
                    self.initial_generation_seconds += duration
                elif str(request.get("attempt_mode") or "") in {"repair_section", "continue_section"}:
                    self.repair_provider_seconds += duration
            meta = output.get("provider_metadata", {}) if isinstance(output, dict) else {}
            actual_usage = extract_token_usage(meta, output if isinstance(output, dict) else {}, meta.get("envelope") if isinstance(meta, dict) else {})
            tokens = resolve_attempt_tokens(actual=actual_usage, estimated_input=estimated_input, estimated_output=estimate_tokens(output))
            with self.checkpoint_lock:
                self.token_accountant.add(tokens)
                self.token_accountant_cumulative.add(tokens)
                self.provider_attempt_samples.append({
                    "elapsed_seconds": duration, "stage": stage, "batch_size": batch_size,
                    "model": model_name, "input_tokens": tokens["input_tokens"],
                    "output_tokens": tokens["output_tokens"], "token_provenance": tokens["token_provenance"],
                    "failure_class": failure_class,
                })
            if reservation is not None:
                self.rate_limiter.reconcile(reservation, actual_input=tokens["input_tokens"], actual_output=tokens["output_tokens"])
            soft_hit = bool(soft and duration > soft)
            hard_hit = bool(hard and duration > hard)
            if soft_hit:
                with self.checkpoint_lock:
                    self.soft_latency_observations += 1
                    if self._scheduler is not None:
                        policy = self._scheduler.observe_soft_latency(quality_blocked=False)
                        self.soft_latency_policy = policy.get("policy") or "REDUCE_FUTURE_CONCURRENCY"
                        self.max_parallelism = int(self._scheduler.current_parallelism)
                        self.latency_only_downgrade = False
            elif self._scheduler is not None:
                recovered = self._scheduler.observe_within_budget()
                self.soft_latency_policy = recovered.get("policy") or self.soft_latency_policy
                self.max_parallelism = int(self._scheduler.current_parallelism)
                if recovered.get("policy") in {"BOUNDED_RECOVERY", "RECOVERED"}:
                    self.latency_only_downgrade = False
            audit = {
                "status": "SUCCESS",
                "duration_seconds": duration,
                "input_tokens_estimate": tokens["input_tokens_estimate"],
                "output_tokens_estimate": tokens["output_tokens_estimate"],
                "input_tokens": tokens["input_tokens"],
                "output_tokens": tokens["output_tokens"],
                "cache_read_tokens": tokens["cache_read_tokens"],
                "token_provenance": tokens["token_provenance"],
                "token_measurement": tokens["token_provenance"],
                "cost_usd": None,
                "cost_provenance": tokens.get("cost_provenance") or "unknown_unverified_pricing",
                "finish_reason": meta.get("finish_reason") or "stop",
                "provider_metadata": meta,
                "requested_settings": profile.get("requested_settings"),
                "effective_settings": profile.get("effective_settings"),
                "latency_soft_exceeded": soft_hit,
                "latency_hard_exceeded": hard_hit,
                "soft_latency_policy": self.soft_latency_policy if soft_hit else "WITHIN_BUDGET",
                "rate_limit_enforcement": rate_meta,
                "stage": stage,
                "quality_escalation_requested": bool((profile.get("effective_settings") or {}).get("quality_escalation_requested")),
                "quality_escalation_applied": bool((profile.get("effective_settings") or {}).get("quality_escalation_applied")),
                "reasoning_before": (profile.get("effective_settings") or {}).get("reasoning_before"),
                "reasoning_after": (profile.get("effective_settings") or {}).get("reasoning_after"),
                "escalation_unavailable_reason": (profile.get("effective_settings") or {}).get("escalation_unavailable_reason"),
            }
            return output, audit
        except Exception as exc:
            if not isinstance(exc, (ProviderError, ProviderUnavailableError, AdmissionError)):
                wrapped = ProviderError(str(exc), error_code="PROVIDER_RUNTIME_ERROR")
                wrapped.__cause__ = exc
                exc = wrapped
            duration = round(time.monotonic() - invoke_started, 3) if "invoke_started" in locals() else round(time.monotonic() - started, 3)
            with self.checkpoint_lock:
                self.provider_wait_seconds += duration
            if reservation is not None:
                self.rate_limiter.reconcile(reservation, actual_input=estimated_input, actual_output=0)
            message = str(exc)
            raw_code = getattr(exc, "error_code", "")
            retry_after = getattr(exc, "retry_after", None)
            if retry_after is not None:
                try:
                    self._last_retry_after = float(retry_after)
                except (TypeError, ValueError):
                    self._last_retry_after = None
            tokens = resolve_attempt_tokens(actual=None, estimated_input=estimated_input, estimated_output=0)
            with self.checkpoint_lock:
                self.token_accountant.add(tokens)
                self.token_accountant_cumulative.add(tokens)
            if "429" in message or "rate limit" in message.lower() or raw_code in {"RATE_LIMIT", "RATE_LIMIT_OVERSIZED", "RATE_LIMIT_TIMEOUT"}:
                code = str(raw_code or "RATE_LIMIT")
            else:
                code = classify_provider_failure(raw_code, message)
            audit = {
                "status": deferred_status(code) if code not in {"RATE_LIMIT", "RATE_LIMIT_OVERSIZED", "RATE_LIMIT_TIMEOUT"} else "RATE_LIMIT",
                "error_code": code,
                "error": message,
                "error_type": type(getattr(exc, "__cause__", None) or exc).__name__,
                "duration_seconds": duration,
                "invoked": True,
                "input_tokens_estimate": estimated_input,
                "output_tokens_estimate": 0,
                "input_tokens": tokens["input_tokens"],
                "output_tokens": tokens["output_tokens"],
                "token_provenance": tokens["token_provenance"],
                "finish_reason": "error",
                "requested_settings": profile.get("requested_settings"),
                "effective_settings": profile.get("effective_settings"),
                "latency_soft_exceeded": bool(soft and duration > soft),
                "latency_hard_exceeded": bool(hard and duration > hard) or code in {"TIMEOUT", "NETWORK_ERROR"},
                "rate_limit_enforcement": rate_meta,
                "stage": stage,
                "quality_escalation_requested": bool((profile.get("effective_settings") or {}).get("quality_escalation_requested")),
                "quality_escalation_applied": bool((profile.get("effective_settings") or {}).get("quality_escalation_applied")),
                "reasoning_before": (profile.get("effective_settings") or {}).get("reasoning_before"),
                "reasoning_after": (profile.get("effective_settings") or {}).get("reasoning_after"),
                "escalation_unavailable_reason": (profile.get("effective_settings") or {}).get("escalation_unavailable_reason"),
            }
            return None, audit

    def _word_summary(self, fragment: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
        text = re.sub(r"\s+", "", visible_text(fragment))
        core = text[:180]
        return {"section_id": contract["section_id"], "core_points": core, "new_commitments": fragment.get("claims", []), "processes_defined": contract.get("required_processes", []), "terms_used": list(self.inputs["global_state"]["canonical_terms"].values()), "references_created": fragment.get("cross_references", []), "avoid_repeating_next": contract.get("must_cover", [])[:3]}

    def _update_state(self, summary: dict[str, Any], fragment: dict[str, Any], contract: dict[str, Any]) -> None:
        sid = contract["section_id"]
        self.incremental_state["completed_sections"] = list(dict.fromkeys(self.incremental_state["completed_sections"] + [sid]))
        self.incremental_state["generation_progress"][sid] = "completed"
        self.incremental_state["section_summaries"][sid] = summary
        self.incremental_state["used_core_arguments"] = list(dict.fromkeys(self.incremental_state["used_core_arguments"] + contract["must_cover"]))
        self.incremental_state["used_processes"] = list(dict.fromkeys(self.incremental_state["used_processes"] + contract.get("required_processes", [])))
        self.incremental_state["cross_references_created"].extend(fragment.get("cross_references", []))
        self.incremental_state["cross_references"].extend(fragment.get("cross_references", []))
        coverage = self.incremental_state.setdefault("requirement_coverage", {})
        generated = coverage.setdefault("generated_sections", {})
        generated[sid] = {rid: "COVERED" for rid in contract.get("source_requirements", [])}
        registry = self.incremental_state.setdefault("commitment_registry", [])
        known = {json.dumps(row, ensure_ascii=False, sort_keys=True) for row in registry}
        for claim in fragment.get("claims", []):
            row = {"section_id": sid, "claim": claim, "provenance": "generated_fragment", "status": "governance_passed"}
            signature = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if signature not in known:
                registry.append(row)
                known.add(signature)

    def _restore_word_state(self, contract: dict[str, Any], unit: Path) -> None:
        """Rebuild the in-memory incremental state from authoritative completed artifacts."""
        summary_path = unit / "summary.json"
        fragment_path = unit / "fragment.json"
        if summary_path.is_file() and fragment_path.is_file():
            sid = contract["section_id"]
            if sid not in self.incremental_state["completed_sections"]:
                self._update_state(read_json(summary_path), read_json(fragment_path), contract)

    def _section_is_pass(self, sid: str) -> bool:
        status_path = self.word_root / "sections" / sid / "status.json"
        if not status_path.is_file():
            return False
        return read_json(status_path).get("status") in {"COMPLETED", "COMPLETED_WITH_WARNING", "COMPLETED_CONDITIONAL"}

    def _batch_prompt(self, contexts: list[dict[str, Any]], repair: list[str] | None = None, *, batch_id: str | None = None) -> dict[str, Any]:
        system = "你是资深物业服务方案撰稿人。一次调用生成本Batch内全部Logical Section。禁止工具、文件、调查。只返回JSON。"
        rules = [
            "不得新增目录或改变各Section目的",
            "必须恰好返回请求的section_id，不得增加、删除或重复",
            "每个Section独立成文，不得把多节压成一章",
            "正文不得出现REQ/SCR/KU等内部ID",
            "不得输出AI说明",
            "不得新增人员、频次、时限、设备或商业承诺",
        ]
        if repair:
            rules.extend(repair)
        current_batch_id = str(batch_id or "").strip()
        payload = {
            "current_batch_id": current_batch_id,
            "requested_section_ids": [row["section_contract"]["section_id"] for row in contexts],
            "sections": contexts,
        }
        contract_lines = [
            "根据Context Pack一次生成多个Word Section。",
        ]
        if current_batch_id:
            contract_lines.extend([
                f"CURRENT_BATCH_ID = {current_batch_id}",
                "Output Contract:",
                f"- batch_id必须等于CURRENT_BATCH_ID（{current_batch_id}），不得省略、留空或返回其他值。",
                "- 返回JSON：{\"batch_id\":string,\"sections\":[{section_id,title,body_blocks,tables,processes,callouts,cross_references,claims,used_requirement_ids,used_ku_ids,generation_notes}]}。",
            ])
            rules.append(f"batch_id必须等于CURRENT_BATCH_ID = {current_batch_id}")
        else:
            contract_lines.append(
                "返回JSON：{\"batch_id\":string,\"sections\":[{section_id,title,body_blocks,tables,processes,callouts,cross_references,claims,used_requirement_ids,used_ku_ids,generation_notes}]}。"
            )
        prompt = (
            "\n".join(contract_lines)
            + "\nsections必须覆盖且仅覆盖请求的section_id。规则：\n- "
            + "\n- ".join(rules)
            + "\nContext Pack：\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        return {"system": system, "prompt": prompt}

    def _next_attempt(self, root: Path) -> tuple[Path, int]:
        existing = []
        for path in root.glob("provider_attempt_*"):
            try:
                existing.append(int(path.name.rsplit("_", 1)[1]))
            except ValueError:
                continue
        attempt = max(existing, default=0) + 1
        return root / f"provider_attempt_{attempt}", attempt

    def _split_batch_output(self, output: dict[str, Any], requested: list[str], expected_batch_id: str | None = None) -> tuple[dict[str, dict[str, Any]], list[str]]:
        issues: list[str] = []
        raw_sections = output.get("sections") if isinstance(output, dict) else None
        fragments: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        ambiguous: set[str] = set()
        require_batch_id = bool(expected_batch_id) and len(requested) > 1
        if require_batch_id:
            returned_batch_id = output.get("batch_id") if isinstance(output, dict) else None
            if returned_batch_id in {None, ""}:
                issues.append("batch_id_missing")
            elif str(returned_batch_id) != str(expected_batch_id):
                issues.append(f"batch_id_mismatch:{returned_batch_id}")
        if not isinstance(raw_sections, list):
            if isinstance(output, dict) and output.get("section_id") and len(requested) == 1:
                raw_sections = [output]
            else:
                return {}, ["missing_sections_array"]
        for row in raw_sections:
            if not isinstance(row, dict):
                issues.append("non_object_section")
                continue
            sid = str(row.get("section_id") or "")
            if not sid:
                issues.append("missing_section_id")
                continue
            if sid not in requested:
                issues.append(f"unrequested:{sid}")
                continue
            if sid in seen:
                issues.append(f"duplicate:{sid}")
                ambiguous.add(sid)
                fragments.pop(sid, None)
                continue
            seen.add(sid)
            fragment = {key: value for key, value in row.items() if key != "provider_metadata"}
            fragments[sid] = fragment
            body_ok, _body_units = meaningful_section_body(fragment)
            if not body_ok:
                issues.append(f"empty_body:{sid}")
        for sid in ambiguous:
            fragments.pop(sid, None)
        for sid in requested:
            if sid not in fragments:
                issues.append(f"missing:{sid}")
        return fragments, issues

    def _commit_section(self, job: LongformGenerationJob, contract: dict[str, Any], fragment: dict[str, Any], gate: dict[str, Any], attempts: list[dict[str, Any]], continuation_used: bool) -> str:
        sid = contract["section_id"]
        unit = self.word_root / "sections" / sid
        summary = self._word_summary(fragment, contract)
        under = gate.get("length_status") == "SECTION_UNDER_LENGTH"
        status = "COMPLETED_CONDITIONAL" if contract.get("section_activation_condition", {}).get("expression") != "always" else ("COMPLETED_WITH_WARNING" if gate["status"] == "WARNING" or under else "COMPLETED")
        write_json(unit / "fragment.json", fragment)
        write_json(unit / "summary.json", summary)
        write_json(unit / "governance.json", gate)
        write_json(unit / "qa.json", gate)
        last = attempts[-1] if attempts else {}
        write_json(unit / "generation.json", {
            "section_id": sid,
            "provider": self.provider_name,
            "model": ((last.get("effective_settings") or self.capability.get("effective_settings") or {}).get("model")),
            "attempt": last.get("attempt_id") or len(attempts),
            "mode": last.get("mode") or "generate",
            "input_tokens": last.get("input_tokens") or last.get("input_tokens_estimate"),
            "output_tokens": last.get("output_tokens") or last.get("output_tokens_estimate"),
            "finish_reason": last.get("finish_reason") or last.get("status"),
            "effective_chars": len(visible_text(fragment)),
            "coverage": gate.get("must_cover"),
            "retry": bool(len(attempts) > 1),
            "continuation": continuation_used,
            "requested_settings": last.get("requested_settings"),
            "effective_settings": last.get("effective_settings"),
            "batch_id": last.get("batch_id"),
        })
        write_json(unit / "status.json", {"section_id": sid, "status": status, "attempts": attempts, "content_units": gate["content_units"], "continuation": continuation_used, "updated_at": now_iso()})
        with self.checkpoint_lock:
            if sid not in job.completed_units:
                job.completed_units.append(sid)
            job.failed_units = [value for value in job.failed_units if value != sid]
            self._update_state(summary, fragment, contract)
        write_json(self.word_root / "global_state" / f"after_{sid}.json", self.incremental_state)
        self._checkpoint(job)
        return status

    def _fail_section(self, job: LongformGenerationJob, sid: str, attempts: list[dict[str, Any]], status: str) -> None:
        unit = self.word_root / "sections" / sid
        write_json(unit / "status.json", {"section_id": sid, "status": status, "attempts": attempts, "updated_at": now_iso()})
        with self.checkpoint_lock:
            if sid not in job.failed_units and not str(status).startswith("DEFERRED_"):
                job.failed_units.append(sid)
        self._checkpoint(job)

    def generate_word(self, section_ids: list[str] | None = None) -> dict[str, Any]:
        contracts = sorted(self.inputs["section_contracts"]["contracts"], key=lambda row: row["generation_order"])
        if section_ids is not None:
            contracts = [row for row in contracts if row["section_id"] in set(section_ids)]
        job = self._job(self.word_root, "WORD", len(contracts))
        job.speed_profile = self.speed_profile
        selected_ids = [row["section_id"] for row in contracts]
        prerequisites, _advisory = build_prereq_maps(selected_ids, self.inputs["dependency_map"])
        by_id = {row["section_id"]: row for row in contracts}
        wall_started = time.monotonic()
        self._restore_call_accounting(job)
        for contract in contracts:
            sid = contract["section_id"]
            unit = self.word_root / "sections" / sid
            status_path = unit / "status.json"
            if self._section_is_pass(sid):
                if sid not in job.completed_units:
                    job.completed_units.append(sid)
                self._restore_word_state(contract, unit)
                continue
            current_status = read_json(status_path).get("status") if status_path.is_file() else ""
            if current_status not in {"COMPLETED", "COMPLETED_WITH_WARNING", "COMPLETED_CONDITIONAL"} and (unit / "provider_raw.json").is_file():
                raw = read_json(unit / "provider_raw.json")
                if raw.get("sections"):
                    raw = next((row for row in raw["sections"] if row.get("section_id") == sid), raw)
                candidate = _repair_customer_text(apply_artifact_repairs(apply_local_repairs(raw)))
                processes = [self.builder.processes[pid] for pid in contract.get("required_processes", []) if pid in self.builder.processes]
                recovered_gate = evaluate_word_fragment(candidate, contract, self.inputs["global_state"], processes)
                recovered_decision = classify_section_issue(recovered_gate, contract, None, fragment=candidate, speed_profile=self.speed_profile)
                recovered_short = self.require_section_min and recovered_decision.get("severity") in {"CRITICAL_UNDER_LENGTH", "MATERIAL_UNDER_LENGTH"}
                if recovered_gate["status"] != "BLOCK" and not recovered_short:
                    self._commit_section(job, contract, candidate, recovered_gate, [{"attempt_id": 0, "mode": "local_revalidation"}], False)
        provider_config = getattr(self.provider, "config", {}) if self.provider is not None else {}
        plan = plan_generation_batches(
            contracts,
            self.inputs["dependency_map"],
            provider_name=self.provider_name,
            speed_profile=self.speed_profile,
            max_parallelism=self.max_parallelism,
            provider_config=provider_config if isinstance(provider_config, dict) else {},
        )
        write_json(self.run_root / "generation_batches.json", plan)
        write_json(self.word_root / "generation_batches.json", plan)
        job.pending_batches = [row["batch_id"] for row in plan["batches"]]
        self.performance["logical_section_count"] = len(contracts)
        self.performance["generation_batch_count"] = len(plan["batches"])
        self.repair_budget = FastRepairBudget(speed_profile=self.speed_profile, initial_batch_count=len(plan["batches"]))
        self.run_started_monotonic = time.monotonic()
        self.max_parallelism = int(plan.get("max_parallelism") or self.max_parallelism)
        job.parallelism = self.max_parallelism
        self._normalize_batch_state(job, plan["batches"])
        self._checkpoint(job)

        def _execute(batch: dict[str, Any]) -> dict[str, Any]:
            remaining = [sid for sid in batch["section_ids"] if not self._section_is_pass(sid)]
            if not remaining:
                with self.checkpoint_lock:
                    if batch["batch_id"] not in job.completed_batches:
                        job.completed_batches.append(batch["batch_id"])
                    job.pending_batches = [bid for bid in job.pending_batches if bid != batch["batch_id"]]
                    job.failed_batches = [bid for bid in job.failed_batches if bid != batch["batch_id"]]
                    job.retry_queue = [item for item in job.retry_queue if (item.get("batch_id") if isinstance(item, dict) else item) != batch["batch_id"]]
                self._checkpoint(job)
                return {"status": "SKIPPED_RESUME", "section_ids": batch["section_ids"], "provider_calls": 0}
            blocked = []
            runnable = []
            same_batch = set(batch.get("section_ids") or [])
            for sid in remaining:
                missing = [dep for dep in prerequisites.get(sid, []) if dep not in job.completed_units and dep not in same_batch]
                if missing:
                    write_json(self.word_root / "sections" / sid / "status.json", {"section_id": sid, "status": "BLOCKED_BY_DEPENDENCY", "dependencies": missing, "updated_at": now_iso()})
                    blocked.append(sid)
                else:
                    runnable.append(sid)
            if not runnable:
                return {"status": "BLOCKED_BY_DEPENDENCY", "section_ids": remaining, "blocked": blocked, "provider_calls": 0}
            work = shrink_batch(batch, runnable)
            if self._scheduler and self._scheduler.future_batch_split and self.speed_profile != "fast" and len(work.get("section_ids") or []) > 1:
                micros = split_micro_batches(list(work["section_ids"]), work)
                combined = None
                for micro in micros:
                    micro["batch_id"] = batch["batch_id"]
                    part = self._run_batch(job, micro, by_id, stage="draft")
                    if combined is None:
                        combined = dict(part)
                    else:
                        combined["provider_calls"] = int(combined.get("provider_calls") or 0) + int(part.get("provider_calls") or 0)
                        combined["failed"] = list(combined.get("failed") or []) + list(part.get("failed") or [])
                        combined["accepted"] = int(combined.get("accepted") or 0) + int(part.get("accepted") or 0)
                        if part.get("error_code") == "RATE_LIMIT":
                            combined = part
                            break
                        if part.get("status") not in {"SUCCESS", "SKIPPED_RESUME"}:
                            combined["status"] = part.get("status")
                    if part.get("error_code") == "RATE_LIMIT":
                        combined = part
                        break
                result = combined or self._run_batch(job, work, by_id, stage="draft")
            else:
                result = self._run_batch(job, work, by_id, stage="draft")
            failed = [sid for sid in runnable if not self._section_is_pass(sid)]
            classes = dict(result.get("section_failure_classes") or {})
            admission_codes = {"RATE_LIMIT_OVERSIZED", "RATE_LIMIT_TIMEOUT", "RATE_LIMIT_INVALID_LIMIT", "ADMISSION_REJECTED"}
            if failed and result.get("error_code") not in {"RATE_LIMIT", *admission_codes} and result.get("status") != "ADMISSION_REJECTED":
                for sid in failed:
                    kind = classes.get(sid) or result.get("failure_class") or "QUALITY_FAILURE"
                    unit = self.word_root / "sections" / sid
                    existing = read_json(unit / "audit.json") if (unit / "audit.json").is_file() else {"attempts": []}
                    previous_repairs = sum(row.get("mode") in {"repair_section", "continue_section"} for row in (existing.get("attempts") or []))
                    gate = read_json(unit / "qa.json") if (unit / "qa.json").is_file() else {}
                    fragment = read_json(unit / "provider_raw.json") if (unit / "provider_raw.json").is_file() else None
                    decision = classify_section_issue(gate, by_id[sid], kind, fragment=fragment, speed_profile=self.speed_profile)
                    kind = str(decision.get("failure_class") or kind)
                    elapsed = 0.0 if self.run_started_monotonic is None else (time.monotonic() - self.run_started_monotonic)
                    budget = self.repair_budget
                    self._record_qa_event(job, sid=sid, check="SECTION_QA", result="FAIL", reason=kind, repair_attempt=previous_repairs)
                    if budget is None or not budget.allow(decision, previous_repairs=previous_repairs, elapsed_seconds=elapsed):
                        self.repair_skipped_low_value += 1
                        if fragment and gate.get("status") != "BLOCK":
                            attempts = list(existing.get("attempts") or [])
                            self._commit_section(job, by_id[sid], fragment, gate, attempts, False)
                            self.local_repair_count += 1
                        continue
                    while not self._section_is_pass(sid) and budget.allow(decision, previous_repairs=previous_repairs, elapsed_seconds=elapsed):
                        previous_repairs += 1
                        budget.consume()
                        with self.checkpoint_lock:
                            job.public_phase = "AUTO_REPAIRING"
                            if sid not in job.active_repair_sections:
                                job.active_repair_sections.append(sid)
                        self._checkpoint(job)
                        repair_started = time.monotonic()
                        try:
                            repaired = self._repair_section(job, by_id[sid], kind)
                        finally:
                            repair_finished = time.monotonic()
                            with self.checkpoint_lock:
                                self.repair_intervals.append((repair_started, repair_finished))
                        self._record_qa_event(
                            job,
                            sid=sid,
                            check="RECHECKING",
                            result="PASS" if repaired else "FAIL",
                            reason=kind,
                            repair_attempt=previous_repairs,
                        )
                        if repaired:
                            break
                        qa_path = unit / "qa.json"
                        if qa_path.is_file():
                            retry_gate = read_json(qa_path)
                            retry_fragment = read_json(unit / "provider_raw.json") if (unit / "provider_raw.json").is_file() else fragment
                            decision = classify_section_issue(retry_gate, by_id[sid], kind, fragment=retry_fragment, speed_profile=self.speed_profile)
                            kind = str(decision.get("failure_class") or kind)
                    with self.checkpoint_lock:
                        job.active_repair_sections = [value for value in job.active_repair_sections if value != sid]
                        job.public_phase = "RECHECKING" if job.active_repair_sections else "GENERATING"
                    self._checkpoint(job)
                    if not self._section_is_pass(sid):
                        leftover = read_json(unit / "provider_raw.json") if (unit / "provider_raw.json").is_file() else fragment
                        leftover_gate = read_json(unit / "qa.json") if (unit / "qa.json").is_file() else gate
                        if leftover and leftover_gate.get("status") != "BLOCK":
                            attempts = list((read_json(unit / "audit.json") or {}).get("attempts") or existing.get("attempts") or [])
                            self._commit_section(job, by_id[sid], leftover, leftover_gate, attempts, False)
                        else:
                            self._record_qa_event(job, sid=sid, check="SECTION_QA", result="TERMINAL_BLOCKED", reason=kind, repair_attempt=previous_repairs)
            failed_after = [sid for sid in runnable if not self._section_is_pass(sid)]
            with self.checkpoint_lock:
                job.pending_batches = [bid for bid in job.pending_batches if bid != batch["batch_id"]]
                if blocked:
                    if batch["batch_id"] not in job.blocked_batches:
                        job.blocked_batches.append(batch["batch_id"])
                    job.failed_batches = [bid for bid in job.failed_batches if bid != batch["batch_id"]]
                    result = {**result, "status": "BLOCKED_BY_DEPENDENCY", "blocked": blocked}
                elif not failed_after:
                    if batch["batch_id"] not in job.completed_batches:
                        job.completed_batches.append(batch["batch_id"])
                    job.failed_batches = [bid for bid in job.failed_batches if bid != batch["batch_id"]]
                    job.retry_queue = [item for item in job.retry_queue if (item.get("batch_id") if isinstance(item, dict) else item) != batch["batch_id"]]
                    result = {**result, "status": "SUCCESS", "failed": []}
                else:
                    if batch["batch_id"] not in job.failed_batches:
                        job.failed_batches.append(batch["batch_id"])
                    job.completed_batches = [bid for bid in job.completed_batches if bid != batch["batch_id"]]
                    result = {**result, "status": "FAILED_BATCH", "failed": failed_after}
            self._checkpoint(job)
            return result

        scheduler = ConcurrentBatchScheduler(provider_name=self.provider_name, max_parallelism=self.max_parallelism)
        scheduler.allow_future_batch_split = self.speed_profile != "fast"
        self._scheduler = scheduler
        scheduled = scheduler.run(plan["batches"], _execute, is_section_complete=self._section_is_pass)
        self._normalize_batch_state(job, plan["batches"], scheduled)
        self.max_parallelism = int(scheduled.get("parallelism") or self.max_parallelism)
        job.finished_at = now_iso()
        job.current_unit = None
        wall = round(time.monotonic() - wall_started, 3)
        job.status = "COMPLETED" if len(job.completed_units) == job.total_units and not job.failed_units else ("DEFERRED_EXTERNAL" if job.deferred_external_events and not job.failed_units else "COMPLETED_WITH_GAPS")
        job.public_phase = "SECTION_QA" if job.status == "COMPLETED" else "TERMINAL_BLOCKED"
        self.performance.update({
            "provider_call_count": self.provider_calls_total,
            "provider_calls_total": self.provider_calls_total,
            "this_run_calls": self.this_run_calls,
            "this_resume_calls": self.this_resume_calls,
            "parallelism": self.max_parallelism,
            "latency_downgrade_count": self.latency_downgrade_count,
            "quality_escalation_count": self.quality_escalation_count,
            "quality_escalation_requested": bool(self.escalation_audit.get("quality_escalation_requested")),
            "quality_escalation_applied": bool(self.escalation_audit.get("quality_escalation_applied")),
            "quality_escalation_requested_count": self.quality_escalation_requested_count,
            "quality_escalation_applied_count": self.quality_escalation_applied_count,
            "reasoning_before": self.escalation_audit.get("reasoning_before"),
            "reasoning_after": self.escalation_audit.get("reasoning_after"),
            "escalation_unavailable_reason": self.escalation_audit.get("escalation_unavailable_reason"),
            "soft_latency_observations": self.soft_latency_observations,
            "soft_latency_policy": self.soft_latency_policy or (scheduled.get("soft_latency_policy") if scheduled else "NONE"),
            "generation_seconds": wall,
            # Concurrent repair calls are counted by their wall-clock interval
            # union rather than by summing provider worker durations.
            "repair_seconds": union_interval_seconds(self.repair_intervals),
            "provider_wait_seconds": round(self.provider_wait_seconds, 3),
            "wall_elapsed_seconds": wall,
            "token_accounting": {
                **self.token_accountant.summary(),
                "this_run": self.token_accountant.summary(),
                "cumulative": self.token_accountant_cumulative.summary(),
            },
            "admission_wait_seconds": round(self.admission_wait_seconds, 3),
            "provider_attempt_samples": list(self.provider_attempt_samples),
            "admission_attempts": self.admission_attempts,
            "executed_section_group_sizes": list(self.executed_section_group_sizes),
            "repair_reasoning": self.performance.get("repair_reasoning") or "not_invoked",
        })
        self._checkpoint(job)
        return self.word_summary(job)

    def _run_batch(self, job: LongformGenerationJob, batch: dict[str, Any], by_id: dict[str, dict[str, Any]], *, stage: str, failure_class: str | None = None) -> dict[str, Any]:
        section_ids = list(batch.get("section_ids") or [])
        if not section_ids:
            return {"status": "SUCCESS", "provider_calls": 0}
        self.executed_section_group_sizes.append(len(section_ids))
        job.current_unit, job.status = ",".join(section_ids), "EXTERNAL_PROVIDER_RUNNING"
        self._checkpoint(job)
        contexts = []
        for sid in section_ids:
            contract = by_id[sid]
            context = compact_generation_context(self.builder.word(contract, self.incremental_state))
            write_json(self.word_root / "sections" / sid / "input.json", context)
            contexts.append(context)
        batch_dir = self.word_root / "batches" / batch["batch_id"]
        if len(section_ids) == 1:
            prompt = self._word_prompt(contexts[0])
            request = {
                "task_id": f"{job.job_id}-{section_ids[0]}-{batch['batch_id']}",
                "system_prompt": prompt["system"],
                "prompt": prompt["prompt"],
                "required_keys": sorted(WORD_REQUIRED_KEYS),
                "agent_max_turns": 1,
                "section_id": section_ids[0],
                "attempt_mode": "generate",
                "generation_mode": "longform_section",
            }
            unit_dir = self.word_root / "sections" / section_ids[0]
            existing = read_json(unit_dir / "audit.json") if (unit_dir / "audit.json").is_file() else {"attempts": []}
            attempt_dir, attempt = self._next_attempt(unit_dir)
            output, audit = self._invoke(request, attempt_dir, stage=stage, failure_class=failure_class, batch_size=1)
            attempts_map = {section_ids[0]: list(existing.get("attempts", [])) + [{"attempt_id": attempt, "mode": "generate", "batch_id": batch["batch_id"], **audit}]}
        else:
            prompt = self._batch_prompt(contexts, batch_id=str(batch.get("batch_id") or ""))
            request = {
                "task_id": f"{job.job_id}-{batch['batch_id']}",
                "system_prompt": prompt["system"],
                "prompt": prompt["prompt"],
                "required_keys": ["batch_id", "sections"],
                "agent_max_turns": 1,
                "attempt_mode": "generate_batch",
                "generation_mode": "longform_batch",
                "section_ids": section_ids,
                "batch_id": batch["batch_id"],
            }
            attempt_dir, attempt = self._next_attempt(batch_dir)
            output, audit = self._invoke(request, attempt_dir, stage=stage, failure_class=failure_class, batch_size=len(section_ids))
            attempts_map = {}
            for sid in section_ids:
                existing = read_json(self.word_root / "sections" / sid / "audit.json") if (self.word_root / "sections" / sid / "audit.json").is_file() else {"attempts": []}
                attempts_map[sid] = list(existing.get("attempts", [])) + [{"attempt_id": attempt, "mode": "generate_batch", "batch_id": batch["batch_id"], **audit}]
        if str(audit.get("error_code") or "") in {"RATE_LIMIT_OVERSIZED", "RATE_LIMIT_TIMEOUT", "RATE_LIMIT_INVALID_LIMIT", "ADMISSION_REJECTED"} or audit.get("status") == "ADMISSION_REJECTED":
            return {
                "status": "ADMISSION_REJECTED",
                "error_code": audit.get("error_code") or "ADMISSION_REJECTED",
                "section_ids": section_ids,
                "provider_calls": 0,
                "invoked": False,
            }
        if str(audit.get("error_code") or "") == "RATE_LIMIT":
            return {"status": "RATE_LIMIT", "error_code": "RATE_LIMIT", "section_ids": section_ids, "provider_calls": 1}
        if output is None:
            for sid in section_ids:
                write_json(self.word_root / "sections" / sid / "audit.json", {"attempts": attempts_map[sid]})
                status = attempts_map[sid][-1]["status"] if str(attempts_map[sid][-1].get("status", "")).startswith("DEFERRED_") else "FAILED_SECTION"
                self._fail_section(job, sid, attempts_map[sid], status)
            return {"status": "FAILED_BATCH", "error_code": audit.get("error_code"), "section_ids": section_ids, "failure_class": classify_generation_failure(None, audit)}
        cleaned = {key: value for key, value in output.items() if key != "provider_metadata"}
        write_json(batch_dir / "provider_raw.json", cleaned)
        expected_batch_id = batch.get("batch_id") if len(section_ids) > 1 else None
        fragments, issues = self._split_batch_output(cleaned, section_ids, expected_batch_id=expected_batch_id)
        contract_error = any(item.startswith(("unrequested:", "duplicate:", "batch_id_mismatch:", "batch_id_missing", "missing_sections_array")) for item in issues)
        accepted = 0
        failed = []
        section_failure_classes: dict[str, str] = {}
        for sid in section_ids:
            unit = self.word_root / "sections" / sid
            contract = by_id[sid]
            empty_body = f"empty_body:{sid}" in issues
            fragment = None if contract_error or empty_body else fragments.get(sid)
            write_json(unit / "audit.json", {"attempts": attempts_map[sid]})
            if fragment is None:
                failed.append(sid)
                kind = "OUTPUT_CONTRACT_ERROR" if contract_error or any(item == f"duplicate:{sid}" or item == f"missing:{sid}" for item in issues) else "QUALITY_FAILURE"
                section_failure_classes[sid] = kind
                write_json(unit / "governance.json", {"status": "BLOCK", "issues": [item for item in issues if sid in item or item.startswith("batch_id") or item == "missing_sections_array"]})
                continue
            write_json(unit / "provider_raw.json", fragment)
            fragment = _repair_customer_text(apply_artifact_repairs(apply_local_repairs(fragment)))
            processes = [self.builder.processes[pid] for pid in contract.get("required_processes", []) if pid in self.builder.processes]
            gate = evaluate_word_fragment(fragment, contract, self.inputs["global_state"], processes)
            write_json(unit / "governance.json", gate)
            write_json(unit / "qa.json", gate)
            decision = classify_section_issue(gate, contract, None, fragment=fragment, speed_profile=self.speed_profile)
            short = self.require_section_min and decision.get("severity") in {"CRITICAL_UNDER_LENGTH", "MATERIAL_UNDER_LENGTH"}
            if gate["status"] == "BLOCK" or short:
                failed.append(sid)
                outcome = classify_generation_outcome(gate, attempts_map[sid][-1])
                kind = str(decision.get("failure_class") or outcome.get("failure_class") or "QUALITY_FAILURE")
                if not gate.get("meaningful_body"):
                    kind = "EMPTY_BODY"
                section_failure_classes[sid] = kind
                continue
            self._commit_section(job, contract, fragment, gate, attempts_map[sid], False)
            accepted += 1
        return {
            "status": "SUCCESS" if not failed else "FAILED_BATCH",
            "section_ids": section_ids,
            "accepted": accepted,
            "failed": failed,
            "issues": issues,
            "failure_class": next(iter(section_failure_classes.values()), None),
            "section_failure_classes": section_failure_classes,
            "provider_calls": 1,
        }

    def _repair_section(self, job: LongformGenerationJob, contract: dict[str, Any], failure_class: str) -> bool:
        sid = contract["section_id"]
        if self._section_is_pass(sid):
            return True
        unit = self.word_root / "sections" / sid
        existing = read_json(unit / "audit.json") if (unit / "audit.json").is_file() else {"attempts": []}
        attempts = list(existing.get("attempts", []))
        previous = max((int(row.get("attempt_id", 0)) for row in attempts), default=0)
        context = self.builder.word(contract, self.incremental_state)
        write_json(unit / "input.json", context)
        kind = str(failure_class or "QUALITY_FAILURE")
        if kind in {"LATENCY_FAILURE", "TIMEOUT", "SOFT_LATENCY_EXCEEDED"}:
            stage = "draft"
        else:
            stage = "repair"
        previous_fragment = None
        for candidate in (unit / "fragment.json", unit / "provider_raw.json"):
            if candidate.is_file():
                previous_fragment = read_json(candidate)
                break
        gate = read_json(unit / "qa.json") if (unit / "qa.json").is_file() else {}
        compact = compact_repair_context(context, gate, previous_fragment=previous_fragment, failure_class=kind)
        compact["section_contract"] = {
            "section_id": contract.get("section_id"),
            "section_title": contract.get("section_title"),
            "must_cover": contract.get("must_cover") or [],
            "required_outputs": contract.get("required_outputs") or [],
            "target_words": contract.get("target_words") or {},
            "source_requirements": contract.get("source_requirements") or [],
        }
        extra = repair_prompt_rules(kind)
        prompt = self._word_prompt({"section_contract": contract, "repair_pack": compact}, extra)
        attempt_dir, attempt = self._next_attempt(unit)
        if attempt <= previous:
            attempt = previous + 1
            attempt_dir = unit / f"provider_attempt_{attempt}"
        request = {
            "task_id": f"{job.job_id}-{sid}-R{attempt}",
            "system_prompt": prompt["system"],
            "prompt": prompt["prompt"],
            "required_keys": sorted(WORD_REQUIRED_KEYS),
            "agent_max_turns": 1,
            "section_id": sid,
            "attempt_mode": "repair_section" if kind != "UNDER_LENGTH" else "continue_section",
            "generation_mode": "longform_section",
        }
        # Depth-only continue keeps previous_fragment and continue_section mode, but the
        # native quality-escalation audit still runs (QUALITY_FAILURE), never as a silent None.
        invoke_failure = "QUALITY_FAILURE" if kind == "UNDER_LENGTH" else kind
        output, audit = self._invoke(request, attempt_dir, stage=stage, failure_class=invoke_failure, batch_size=1)
        attempts.append({"attempt_id": attempt, "mode": request["attempt_mode"], **audit})
        job.retry_count += 1
        write_json(unit / "audit.json", {"attempts": attempts})
        if output is None:
            self._fail_section(job, sid, attempts, "FAILED_SECTION")
            return False
        fragment = _repair_customer_text(apply_artifact_repairs(apply_local_repairs({key: value for key, value in output.items() if key != "provider_metadata"})))
        write_json(unit / "provider_raw.json", fragment)
        processes = [self.builder.processes[pid] for pid in contract.get("required_processes", []) if pid in self.builder.processes]
        gate = evaluate_word_fragment(fragment, contract, self.inputs["global_state"], processes)
        write_json(unit / "governance.json", gate)
        write_json(unit / "qa.json", gate)
        decision = classify_section_issue(gate, contract, kind, fragment=fragment, speed_profile=self.speed_profile)
        short = self.require_section_min and decision.get("severity") in {"CRITICAL_UNDER_LENGTH", "MATERIAL_UNDER_LENGTH"}
        if gate["status"] == "BLOCK" or short:
            self._fail_section(job, sid, attempts, "FAILED_SECTION")
            return False
        self._commit_section(job, contract, fragment, gate, attempts, kind == "UNDER_LENGTH")
        return True

    def word_summary(self, job: LongformGenerationJob) -> dict[str, Any]:
        gates = [read_json(path) for path in (self.word_root / "sections").glob("*/governance.json")]
        statuses = [read_json(path) for path in (self.word_root / "sections").glob("*/status.json")]
        audits = [read_json(path).get("attempts", []) for path in (self.word_root / "sections").glob("*/audit.json")]
        repair_count = sum(max(0, len([row for row in attempts if row.get("attempt_id")]) - 1) for attempts in audits)
        logical = job.total_units
        calls = self.provider_calls_total
        initial = int(self.initial_generation_calls or 0)
        budget = self.repair_budget
        scheduler = self._scheduler
        return {
            "status": job.status,
            "provider_used": self.provider_name,
            "completed_sections": len(job.completed_units),
            "total_sections": job.total_units,
            "logical_section_count": logical,
            "generation_batch_count": self.performance.get("generation_batch_count"),
            "provider_call_count": calls,
            "provider_calls_total": self.provider_calls_total,
            "this_run_calls": self.this_run_calls,
            "this_resume_calls": self.this_resume_calls,
            "INITIAL_GENERATION_CALLS": initial,
            "CONTINUATION_CALLS": self.continuation_calls,
            "SECTION_REPAIR_CALLS": self.section_repair_calls,
            "COMPLIANCE_REPAIR_CALLS": 0,
            "FINAL_REPAIR_CALLS": 0,
            "RETRY_CALLS": self.retry_calls,
            "TOTAL_PROVIDER_CALLS": calls,
            "INITIAL_BATCH_COUNT": self.performance.get("generation_batch_count"),
            "INITIAL_SECTIONS_PER_CALL": round(logical / initial, 2) if initial else None,
            "INITIAL_CALL_REDUCTION_RATIO": round(1 - (initial / logical), 4) if logical and initial else None,
            "SECTIONS_PER_PROVIDER_CALL": round(logical / initial, 2) if initial else (round(logical / calls, 2) if calls else None),
            "CALL_REDUCTION_RATIO": round(1 - (initial / logical), 4) if logical and initial else (round(1 - (calls / logical), 4) if logical else None),
            "PROVIDER_REPAIR_COUNT": self.section_repair_calls,
            "LOCAL_REPAIR_COUNT": self.local_repair_count + (budget.local_repair_count if budget else 0),
            "REPAIR_BUDGET_USED": budget.used if budget else 0,
            "REPAIR_BUDGET_MAX": budget.max_provider if budget else None,
            "REPAIR_SKIPPED_LOW_VALUE": self.repair_skipped_low_value + (budget.skipped_low_value if budget else 0),
            "REPAIR_ESCALATED_CRITICAL": self.repair_escalated_critical + (budget.escalated_critical if budget else 0),
            "INITIAL_GENERATION_SECONDS": round(self.initial_generation_seconds, 3),
            "REPAIR_PROVIDER_SECONDS": round(self.repair_provider_seconds, 3),
            "MAX_OBSERVED_CONCURRENCY": getattr(scheduler, "max_observed_concurrency", None) if scheduler else None,
            "AVG_OBSERVED_CONCURRENCY": getattr(scheduler, "avg_observed_concurrency", None) if scheduler else None,
            "parallelism": self.max_parallelism,
            "mode": self.speed_profile,
            "completed_batches": list(job.completed_batches),
            "pending_batches": list(job.pending_batches),
            "failed_batches": list(job.failed_batches),
            "blocked_batches": list(getattr(job, "blocked_batches", []) or []),
            "quality_escalation_requested": bool(self.escalation_audit.get("quality_escalation_requested")),
            "quality_escalation_applied": bool(self.escalation_audit.get("quality_escalation_applied")),
            "reasoning_before": self.escalation_audit.get("reasoning_before"),
            "reasoning_after": self.escalation_audit.get("reasoning_after"),
            "escalation_unavailable_reason": self.escalation_audit.get("escalation_unavailable_reason"),
            "total_content_units": sum(int(row.get("content_units", 0)) for row in statuses),
            "under_length": sum(row.get("length_status") == "SECTION_UNDER_LENGTH" for row in gates),
            "over_length": sum(row.get("length_status") == "SECTION_OVER_LENGTH" for row in gates),
            "must_cover_missing": sum(any(v == "MISSING" for v in row.get("must_cover", {}).values()) for row in gates),
            "fact_drift": sum(len(row.get("fact_drift", [])) for row in gates),
            "commitment_blocks": sum(row.get("commitment", {}).get("status") == "BLOCK" for row in gates),
            "local_repetition": sum(int(row.get("local_repetition", 0)) for row in gates),
            "internal_id_leaks": sum(len(row.get("internal_id_leaks", [])) for row in gates),
            "repair_count": repair_count,
            "latency_downgrade_count": self.latency_downgrade_count,
            "quality_escalation_count": self.quality_escalation_count,
            "deferred_external_events": job.deferred_external_events,
            "requested_settings": (self.capability or {}).get("requested_settings"),
            "effective_settings": (self.capability or {}).get("effective_settings"),
        }

    def generate_ppt(self, slide_ids: list[str] | None = None) -> dict[str, Any]:
        slides = self.inputs["ppt_plan"]["slide_storyboard"]
        if slide_ids is not None: slides = [row for row in slides if row["slide_id"] in set(slide_ids)]
        chapters = {row["chapter_id"]: row for row in self.inputs["ppt_plan"]["chapter_blocks"]}
        job = self._job(self.ppt_root, "PPT", len(slides))
        systemic_deferred = False
        for slide in slides:
            sid = slide["slide_id"]; unit = self.ppt_root / "slides" / sid; status_path = unit / "status.json"
            if status_path.is_file() and read_json(status_path).get("status") in {"COMPLETED", "COMPLETED_WITH_WARNING", "SOURCE_REQUIRED"}:
                if sid not in job.completed_units: job.completed_units.append(sid)
                continue
            context = self.builder.ppt(slide, chapters[slide["chapter_id"]]); write_json(unit / "input.json", context)
            if slide.get("evidence_requirement") == "SOURCE_REQUIRED":
                payload = {"slide_id": sid, "headline": slide["slide_title"], "subheadline": "等待真实案例来源", "key_message": "取得可核验案例资料后再补充本页内容", "content_blocks": [], "visual_data": {"status": "SOURCE_REQUIRED"}, "component_intent": slide["component_family_hint"], "speaker_note_optional": "本页不得编造案例。", "source_trace": [], "generation_notes": ["deferred_content"], "status": "SOURCE_REQUIRED"}
                gate = evaluate_ppt_payload(payload, slide, self.inputs["global_state"])
                write_json(unit / "payload.json", payload); write_json(unit / "governance.json", gate); write_json(status_path, {"slide_id": sid, "status": "SOURCE_REQUIRED", "updated_at": now_iso()})
                job.completed_units.append(sid); continue
            current_status = read_json(status_path).get("status") if status_path.is_file() else ""
            if current_status not in {"COMPLETED", "COMPLETED_WITH_WARNING", "SOURCE_REQUIRED"} and (unit / "provider_raw.json").is_file():
                candidate = _repair_customer_text(read_json(unit / "provider_raw.json"))
                recovered_gate = evaluate_ppt_payload(candidate, slide, self.inputs["global_state"])
                if recovered_gate["status"] != "BLOCK":
                    recovered_status = "COMPLETED_WITH_WARNING" if recovered_gate["status"] == "WARNING" else "COMPLETED"
                    write_json(unit / "payload.json", candidate); write_json(unit / "governance.json", recovered_gate)
                    write_json(status_path, {"slide_id": sid, "status": recovered_status, "recovered_by_local_revalidation": True, "content_units": recovered_gate["content_units"], "updated_at": now_iso()})
                    if sid not in job.completed_units: job.completed_units.append(sid)
                    job.failed_units = [value for value in job.failed_units if value != sid]
                    job.dump(self.ppt_root / "status.json")
                    continue
            job.current_unit, job.status = sid, "EXTERNAL_PROVIDER_RUNNING"; job.dump(self.ppt_root / "status.json")
            existing_audit = read_json(unit / "audit.json") if (unit / "audit.json").is_file() else {"attempts": []}
            attempts = list(existing_audit.get("attempts", []))
            previous_attempt = max((int(row.get("attempt_id", 0)) for row in attempts), default=0)
            final_payload = None; final_gate = None
            for attempt in range(previous_attempt + 1, 3):
                prompt = self._ppt_prompt(context, None if attempt == 1 else ["缩短页面文字并修复主题越界、事实漂移或内部ID泄漏"])
                request = {"task_id": f"{job.job_id}-{sid}-A{attempt}", "system_prompt": prompt["system"], "prompt": prompt["prompt"], "required_keys": sorted(PPT_REQUIRED_KEYS), "agent_max_turns": 1}
                output, audit = self._invoke(request, unit / f"provider_attempt_{attempt}"); attempts.append({"attempt_id": attempt, **audit})
                if output is None: break
                output = _repair_customer_text({key: value for key, value in output.items() if key != "provider_metadata"})
                write_json(unit / f"provider_raw_attempt_{attempt}.json", output)
                write_json(unit / "provider_raw.json", output)
                gate = evaluate_ppt_payload(output, slide, self.inputs["global_state"])
                final_payload, final_gate = output, gate
                if gate["status"] != "BLOCK" and not gate["overflow"]: break
                job.retry_count += 1
            write_json(unit / "governance.json", final_gate or {"status": "DEFERRED"}); write_json(unit / "audit.json", {"attempts": attempts})
            if final_payload is None:
                latest_attempt_status = attempts[-1]["status"] if attempts else ""
                status = latest_attempt_status if str(latest_attempt_status).startswith("DEFERRED_") else "FAILED_SLIDE"; write_json(status_path, {"slide_id": sid, "status": status, "updated_at": now_iso()})
                if status.startswith("DEFERRED_"): job.deferred_external_events.append({"unit": sid, "status": status, "at": now_iso()})
                else: job.failed_units.append(sid)
                if status in {"DEFERRED_PLATFORM_CONFIRMATION", "DEFERRED_PROVIDER_AUTH", "DEFERRED_NETWORK"}:
                    systemic_deferred = True
                    job.dump(self.ppt_root / "status.json")
                    break
                continue
            if final_gate["status"] == "BLOCK":
                if sid not in job.failed_units: job.failed_units.append(sid)
                write_json(status_path, {"slide_id": sid, "status": "FAILED_SLIDE", "updated_at": now_iso()}); continue
            status = "COMPLETED_WITH_WARNING" if final_gate["status"] == "WARNING" else "COMPLETED"
            write_json(unit / "payload.json", final_payload); write_json(status_path, {"slide_id": sid, "status": status, "content_units": final_gate["content_units"], "updated_at": now_iso()})
            if sid not in job.completed_units: job.completed_units.append(sid)
            job.failed_units = [value for value in job.failed_units if value != sid]
            job.dump(self.ppt_root / "status.json")
        job.finished_at = now_iso(); job.current_unit = None
        job.status = "COMPLETED" if len(job.completed_units) == job.total_units and not job.failed_units else ("DEFERRED_EXTERNAL" if job.deferred_external_events and not job.failed_units else "COMPLETED_WITH_GAPS")
        job.public_phase = "SECTION_QA" if job.status == "COMPLETED" else "TERMINAL_BLOCKED"
        job.dump(self.ppt_root / "status.json")
        return self.ppt_summary(job)

    def ppt_summary(self, job: LongformGenerationJob) -> dict[str, Any]:
        gates = [read_json(path) for path in (self.ppt_root / "slides").glob("*/governance.json")]
        statuses = [read_json(path) for path in (self.ppt_root / "slides").glob("*/status.json")]
        audits = [read_json(path).get("attempts", []) for path in (self.ppt_root / "slides").glob("*/audit.json")]
        repair_count = sum(max(0, len([row for row in attempts if row.get("attempt_id")]) - 1) for attempts in audits)
        return {"status": job.status, "provider_used": self.provider_name, "completed_slides": len(job.completed_units), "total_slides": job.total_units, "source_required": sum(row.get("status") == "SOURCE_REQUIRED" for row in statuses), "text_overflow": sum(bool(row.get("overflow")) for row in gates), "topic_overlap": sum(bool(row.get("topic_overlap")) for row in gates), "case_fabrication": sum(bool(row.get("case_fabrication")) for row in gates), "language_warning": sum(bool(row.get("language_warning")) for row in gates), "internal_id_leaks": sum(len(row.get("internal_id_leaks", [])) for row in gates), "repair_count": repair_count, "deferred_external_events": job.deferred_external_events}
