from __future__ import annotations
from filesystem_paths import filesystem_path

import copy
import hashlib
import json
import math
import os
import re
import threading
import uuid
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from governance import apply_artifact_repairs, apply_local_repairs, evaluate_artifact, evaluate_commitments
from governance.customer_hygiene import is_structural_path
from governance.table_qa import FORM_PAYLOAD_SPEC
from governance.text_sanitize import collapse_repeated_conditionals, replace_once, replace_outsourcing_phrase
from planning.budget_control import (
    BUDGET_LEDGER_FILE,
    judge_content_unit,
    merge_ledger,
    read_ledger,
    write_ledger,
)
from planning.planner import SECTION_HARDENING, is_bid_process_text
from planning.unified_budget import (
    MIDRUN_BUDGET_FILE,
    artifact_chars_from_ledger,
    contract_budget_row,
    empty_midrun_state,
    observe_batch,
    scale_budget,
)
from providers import ProviderError, ProviderUnavailableError
from providers.cursor_account import is_cursor_provider
from providers.base import ENVELOPE_TEXT_KEY, atomic_temp_path, write_json as _atomic_write_json
from providers.capability import apply_to_request, recommended_parallelism, resolve_profile
from providers.execution_reliability import classify_provider_failure, deferred_status
from providers.rate_limit import AdmissionError, default_limiter
from providers.token_usage import TokenAccountant, extract_token_usage, resolve_attempt_tokens
from workflow_timing import union_interval_seconds
from .eta import load_budget_history_samples
from .batch_planner import build_prereq_maps, content_unit_batches, plan_generation_batches, shrink_batch, split_micro_batches
from .performance_planner import profile_batch_plan
from .cu_state import (
    CU_PAYLOAD_INCOMPLETE,
    CU_TRUNCATED,
    SECTION_COMPLETED,
    SECTION_COMPLETED_CONDITIONAL,
    SECTION_COMPLETED_WITH_WARNING,
    SECTION_DEPTH_INCOMPLETE,
    SECTION_DRAFTABLE_STATES,
    SECTION_PASS_STATES,
    SECTION_PROVISIONAL,
    SECTION_TECHNICAL_FAILED,
    depth_deficit_class,
    depth_ratio,
    is_technical_retryable,
)
from .envelope import envelope_contract_lines, parse_cu_envelope, should_use_envelope
from .cu_assembly import apply_content_units, rendered_shape
from .depth_supplement import novelty_gate, plan_depth_supplement, supplement_prompt_rules
from .supplement_registry import (discard as discard_supplement, load_registry,
                                  owns_arrival, reserve as reserve_supplement)
from .local_normalize import normalize_fragment, normalize_text
from .reasoning import classify_generation_failure, classify_generation_outcome, normalize_speed_profile
from .repair_policy import (
    FastRepairBudget,
    GlobalRepairBudget,
    PRIORITY_BLOCK,
    PRIORITY_DEPTH_GAP,
    PRIORITY_NEEDS_REVIEW,
    classify_section_issue,
    compact_generation_context,
    compact_repair_context,
    repair_prompt_rules,
    sort_repair_candidates,
)
from .scheduler import ConcurrentBatchScheduler
from .cu_audit import rebuild_content_unit_generation_audit
from . import request_ledger


# 300 页级任务的技术性重试也必须收敛：初稿生成只允许对"请求没有成功送达或
# 响应不可用"做有限次立即重试，深度与质量问题一律留到文档级 QA 之后处理。
TECHNICAL_RETRY_RATIO = 0.35
TECHNICAL_RETRY_CEILING = 60
MAX_TECHNICAL_RETRY_PER_BATCH = 2


def technical_retry_allowance(initial_batches: int) -> int:
    """全局技术性重试预算：与初稿批量成比例，但保留每个 Section 至少 2 次的下限。"""
    derived = math.ceil(max(0, int(initial_batches or 0)) * TECHNICAL_RETRY_RATIO)
    return max(MAX_TECHNICAL_RETRY_PER_BATCH, min(TECHNICAL_RETRY_CEILING, derived))


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
# v12 的正文口径：只描述"写成什么样"，不列举被否定的原句——举原句等于把
# 模板交给模型抄进正文（Run F 实测如此）。单句与批量两条生成路径共用这一份，
# 避免两个 prompt 各自演化出不同的写作要求。
WORD_V12_WRITING_POLICY = (
    "按当前CU契约写可执行初稿：交代岗位、动作、处理对象与记录；围绕target_budget完成必要内容，"
    "不追逐额度上限，不新增契约外表样或说明；采购评分计算方法不改写成物业服务指标",
    "使用物业服务行业表述：登记要素、登记项、表样、接待窗口、答复口径、对接人或渠道、工单记录按语境使用；"
    "字段、接口、工单号、编号只在确指系统标识或来源键时保留，技术标识不得为了换词而改动",
    "句子主谓完整、语义闭合；同一列表各项句末标点保持一致，不写孤立短语、重复句或未写完的句子",
    "以服务岗位和服务动作为主语，技术正文不突出招标代理机构，不列代理机构地址或项目地址；地址属商务信息",
    "不把服务动作写成条件说明式的合同语言；避免用'不属于其职责''触发条件''接口方式'这类定义式说法组织正文句子，"
    "改用岗位、动作、时点与业务过程表达同一含义；表单payload的结构性键名不受此限",
    "同一事实不在相邻段落重复陈述，同一表格不在不同章节重复出现；不写无业务意义的设备编号、空白表样、占位符或待填写表格",
    "事项归口按原事项的连续性与可追溯性处理：需改派时保留原记录并登记交接，业务规则不足以判定时列为待决事项",
    "保留必需的回应、数字、频次、时限、成果与记录要求；不得删减内容、留空白页或编造未具备的能力。"
    "未经确认的人员、资质、救援资源、保留期限、商业条款与项目冲突只登记为待确认，不得写成已经具备或已经承诺",
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path: Path, value: Any) -> None:
    """原子写：唯一实现在 `providers/base.write_json`（含 Windows 改名撞车退避）。

    两条各写一遍"临时文件 + `os.replace`"的原子写，迟早一条有退避、一条没有 ——
    Round 4 的"Provider 已返回、CU 却没落盘"就抛在长文这一侧的改名上。
    """
    _atomic_write_json(path, value)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(filesystem_path(path).read_text(encoding="utf-8-sig"))


def public_strings(value: Any, path: str = "") -> list[str]:
    if isinstance(value, dict):
        structural = {"generation_notes", "provider_metadata", "source_trace", "cross_references", "claims", "process_id", "step_order", "type", "layout", "component_intent", "content_source", "component_family_hint", "slide_id", "section_id", "used_requirement_ids", "used_ku_ids", "status", "content_unit_execution", "content_unit_contracts", "content_unit_id", "parent_section_id", "scoring_refs", "dependency_refs", "generation_origin"}
        structural.update({'table_source_contract', 'source_content_unit_id',
                           'source_table_findings', 'source_table_repairs',
                           'table_governance', 'cu_structures_stripped',
                           'planned_requirement_ids', 'planned_scoring_item_ids',
                           'section_response_used_requirement_ids', 'requirement_usage_kind',
                           'supplement_registration_id'})
        if 'content_blocks' in value:
            structural.update({'paragraphs','bullets'})
        return [text for key, item in value.items() if key not in structural for text in public_strings(item, f"{path}.{key}")]
    if isinstance(value, list):
        return [text for item in value for text in public_strings(item, path)]
    return [value] if isinstance(value, str) else []


def visible_text(value: Any) -> str:
    return "\n".join(public_strings(value))


def rendered_supplement_text(fragment: dict[str, Any]) -> str:
    """Information gain excludes fields that the Word projection never renders."""
    return visible_text({
        "body_blocks": [{"content": block.get("content") or block.get("items") or []}
                        for block in fragment.get("body_blocks") or [] if isinstance(block, dict)],
        "tables": [rendered_shape(table) for table in fragment.get("tables") or []
                   if isinstance(table, dict)],
        "processes": [{"title": process.get("purpose") or process.get("title") or "",
                       "steps": process.get("steps") or []}
                      for process in fragment.get("processes") or [] if isinstance(process, dict)],
    })


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
            "source_controls": copy.deepcopy(self.global_state.get("source_controls") or {}),
            "confirmed_requirements": requirements,
            "writing_constraints": copy.deepcopy(self.global_state.get("writing_constraints", [])),
            "project_reference_material": copy.deepcopy(self.global_state.get("reference_material", [])),
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
            "writing_constraints": copy.deepcopy(self.global_state.get("writing_constraints", [])),
            "project_reference_material": copy.deepcopy(self.global_state.get("reference_material", [])),
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
    source_roles = global_state.get("staffing", {}).get("roles") or {}
    if source_roles:
        for role, expected in source_roles.items():
            for value in re.findall(re.escape(role) + r"[^\d。；\n]{0,12}(\d+)\s*人", text):
                if int(value) != expected: issues.append(f"staffing:{role}:{value}")
        for value in re.findall(r"(?:合计|总计|总人数)[^\d]{0,6}(\d+)\s*人", text):
            if int(value) != confirmed_staff: issues.append(f"staffing:total:{value}")
    else:
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


def section_arrived_units(fragment: dict[str, Any]) -> int:
    """数**一次**"真正到达的 Section 正文"。

    Round 3 的长度门直接 `content_units(visible_text(fragment))`，而 fragment 里同时带着
    装配后的 `body_blocks/tables/processes/callouts` 和原始的 `content_unit_fragments`
    回声 —— 同一段字被数了两遍。Run D 实测：门报 772,240，账本只有 467,292，
    装配后的正文 360,117，也就是说长度判定虚高 1.65 倍，27/27 节全被判成
    SECTION_OVER_LENGTH 里有一部分量的不是内容，是重复计数。
    装配体优先；只有还没装配出正文的片段（纯 CU 返回）才退回 CU 片段，
    这样同一段字在任何形态下都只被数一次。
    """
    assembled = {key: (fragment or {}).get(key) or []
                 for key in ("body_blocks", "tables", "processes", "callouts")}
    units = content_units(visible_text(assembled))
    if units:
        return units
    return content_units(visible_text({"content_unit_fragments": (fragment or {}).get("content_unit_fragments") or []}))


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
    severely_short = bool(target_min and units < artifact_chars_from_ledger(target_min) * 0.55)
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
    from .process_integrity import process_integrity, visual_output_present
    closure = process_integrity(text)
    processes = {row["process_id"]: {"steps_total": len(row["steps"]),
                 "steps_present": sum(step in text for step in row["steps"]),
                 "operational_integrity": closure} for row in kuc_rows}
    process_missing = [pid for pid, row in processes.items() if row["steps_total"] and not row["operational_integrity"]["complete"]]
    output_coverage = {str(item): required_output_coverage(str(item), text) for item in (contract.get("required_outputs") or [])}
    for item in output_coverage:
        if not visual_output_present(fragment, item):
            output_coverage[item] = "MISSING"
    output_missing = [item for item, status in output_coverage.items() if status == "MISSING"]
    required_tables = list(contract.get("required_tables") or [])
    table_missing = bool(required_tables and not (fragment.get("tables") or []))
    body_ok, body_units = meaningful_section_body(fragment)
    empty_headings = empty_subheadings(fragment)
    target = contract["target_words"]
    # 契约额度是统一账本字（含结构 payload 与 CU 回声），门量的是装配后的正文；
    # 用 Run D 实测的账本→装配收益换算之后再比，否则等于拿两把尺子对表。
    units = section_arrived_units(fragment)
    arrived_min = artifact_chars_from_ledger(int(target["min"] or 0))
    arrived_max = artifact_chars_from_ledger(int(target["max"] or 0))
    length_status = ("PASS" if arrived_min * 0.85 <= units <= arrived_max * 1.15
                     else ("SECTION_UNDER_LENGTH" if units < arrived_min * 0.85 else "SECTION_OVER_LENGTH"))
    commitment = evaluate_commitments(global_state, kuc_rows, _governance_artifact(fragment))
    artifact = evaluate_artifact(_governance_artifact(fragment), canonical_facts=global_state)
    from governance.table_fidelity import check_source_table
    source_table_findings = [finding for table in fragment.get("tables") or [] if isinstance(table, dict)
                             for finding in check_source_table(table)]
    fact_drift = _fact_drift(text, global_state)
    role_drift = [old for old in ROLE_DRIFT if old in text]
    scope_violation = _excluded_scope_violations(text, global_state)
    id_leaks = INTERNAL_ID.findall(text)
    repetition = _local_repetition(fragment.get("body_blocks", []))
    blocking_missing = any(value == "MISSING" for value in coverage.values())
    contract_complete = bool(body_ok and not blocking_missing and not process_missing and not output_missing and not table_missing and not empty_headings)
    blocking = bool(missing_keys or not body_ok or empty_headings or blocking_missing or process_missing or output_missing or table_missing or fact_drift or role_drift or scope_violation or id_leaks or AI_LANGUAGE.search(text) or BROKEN_JSON.search(text) or commitment["status"] == "BLOCK" or artifact["status"] == "BLOCK" or source_table_findings)
    return {
        "status": "BLOCK" if blocking else ("WARNING" if length_status != "PASS" or any(value == "PARTIAL" for value in coverage.values()) or artifact["status"] == "AUTO_REPAIR" else "PASS"),
        "missing_keys": missing_keys, "must_cover": coverage, "process_gate": processes, "process_missing": process_missing,
        "content_units": units, "length_status": length_status, "fact_drift": fact_drift, "role_drift": role_drift,
        "source_table_findings": source_table_findings,
        "length_measurement": {"observed_currency": "assembled_content_units", "contract_currency": "unified_ledger_chars",
                               "contract_min": int(target["min"] or 0), "artifact_min": arrived_min,
                               "acceptance_floor": arrived_min * 0.85, "acceptance_ceiling": arrived_max * 1.15},
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
    from planning.ppt_service import VERSION, render_data, visible_parts
    if slide.get("profile_p_contract") == VERSION:
        text = "\n".join(visible_parts({"title": payload.get("headline"), "core_message": payload.get("key_message"), **render_data(payload, slide)}))
    units = content_units(text)
    budget = SLIDE_BUDGET.get(slide.get("slide_role"), 220)
    overflow = units > budget
    id_leaks = INTERNAL_ID.findall(text)
    fact_drift = _fact_drift(text, global_state)
    case_fabrication = bool(slide.get("evidence_requirement") == "SOURCE_REQUIRED" and payload.get("status") != "SOURCE_REQUIRED" and any(token in text for token in ("案例显示", "实际项目", "客户反馈", "提升了")))
    topic_overlap = False
    if slide.get("chapter_id") == "P07" and slide.get("ppt_topic_owner") in {"industrial_logistics_coordination", "project_logistics_coordination"}:
        topic_overlap = sum(token in text for token in ("门岗", "访客登记", "车辆分类", "通行证")) >= 2
    language_warning = any(token in text for token in ("Section", "Contract", "Requirement", "逐条响应", "内部门禁", "责任链"))
    scoring_leak = any(token in text for token in ("评分标准", "评分项", "评分细则", "分值", "评标办法", "商务及报价"))
    from planning.ppt_service import VERSION, payload_issues, render_data, visible_parts
    service_findings = payload_issues(payload, slide) if slide.get("profile_p_contract") == VERSION else []
    if slide.get("profile_p_contract") == VERSION and slide.get("topic_id") == "service_response":
        service_text = "\n".join(visible_parts({"title": payload.get("headline"), **render_data(payload, slide)}))
        declared = re.findall(r"(\d+)\s*(分钟|小时|个工作日)", service_text)
        known = {"分钟": (global_state.get("sla_kpi") or {}).get("complaint_first_response_minutes"),
                 "小时": (global_state.get("service_hours") or {}).get("daily_service_hours"),
                 "个工作日": (global_state.get("sla_kpi") or {}).get("callback_workdays")}
        if any(known.get(unit) is None or str(known[unit]) != number for number, unit in declared):
            service_findings.append({"rule_id": "PPT-P-UNCONFIRMED-SLA", "severity": "BLOCK", "issue": "服务时效数值缺少来源追踪"})
    blocking = bool(any(f.get("severity") == "BLOCK" for f in service_findings) or missing_keys or id_leaks or fact_drift or case_fabrication or topic_overlap or scoring_leak or AI_LANGUAGE.search(text) or BROKEN_JSON.search(text))
    return {"status": "BLOCK" if blocking else ("WARNING" if overflow or language_warning or service_findings else "PASS"), "missing_keys": missing_keys, "content_units": units, "text_budget": budget, "overflow": overflow, "internal_id_leaks": id_leaks, "fact_drift": fact_drift, "case_fabrication": case_fabrication, "topic_overlap": topic_overlap, "language_warning": language_warning, "scoring_leak": scoring_leak, "profile_p_findings": service_findings}


def _metric_kept(source_facts, token: str, context: str) -> bool:
    if source_facts is None or not hasattr(source_facts, "supports_metric"):
        return False
    return bool(source_facts.supports_metric(token, context))


def _compact_listed_metrics(value: str, source_facts) -> str:
    def replace(match: re.Match) -> str:
        compact = re.sub(r"\s+", "", match.group(0))
        window = value[max(0, match.start() - 16):match.end() + 12]
        if _metric_kept(source_facts, compact, window):
            return compact
        return match.group(0)
    return re.sub(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?[ \t]*(?:人|名|小时|分钟|次|%|％)", replace, value)


def _replace_unlisted_metric(value: str, pattern: str, replacement: str, source_facts) -> str:
    """Keep a number only when the same value, unit, object and qualifier are sourced."""
    def replace(match: re.Match) -> str:
        compact = re.sub(r"\s+", "", match.group(0))
        window = value[max(0, match.start() - 16):match.end() + 12]
        if _metric_kept(source_facts, compact, window):
            return match.group(0)
        return replacement
    return re.sub(pattern, replace, value)


def _repair_customer_text(value: Any, source_facts=None) -> Any:
    if isinstance(value, dict):
        from governance.table_context import visit_table
        contextual = visit_table(value, lambda item: _repair_customer_text(item, source_facts), is_structural_path)
        if contextual is not None:
            return contextual
        # 豁免口径只有一把尺子（`customer_hygiene.is_structural_path`），与检测端、
        # `apply_artifact_repairs` 同源。这里曾经自带一份键清单，缺 `type`/`archetype`/
        # `*_ids`，于是同一条链上"哪些值不该改"有两个答案。
        return {key: (item if is_structural_path(str(key)) else _repair_customer_text(item, source_facts)) for key, item in value.items()}
    if isinstance(value, list): return [_repair_customer_text(item, source_facts) for item in value]
    if isinstance(value, str):
        from governance.final_editorial import restore_protected
        saved = {}
        if source_facts is not None:
            value, saved = source_facts.protect(value)
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
        # A register asking for an unconfirmed numeric limit asserts no limit.
        value = re.sub(r"响应时限(?!数值)", "响应安排", value)
        value = replace_once(value, "外委单位", "__CONDITIONAL_SUPPLIER__")
        value = replace_once(value, "外委管理", "__CONDITIONAL_SUPPLIER_MGMT__")
        value = replace_outsourcing_phrase(value)
        value = value.replace("__CONDITIONAL_SUPPLIER__", "如采用外委方式，可按合同要求管理的相关供方")
        value = value.replace("__CONDITIONAL_SUPPLIER_MGMT__", "如采用外委方式，可按合同要求实施供方管理")
        from governance.customer_hygiene import rewrite_customer_hygiene
        value = rewrite_customer_hygiene(value)
        value = _replace_unlisted_metric(value, r"(?<![A-Za-z0-9])(?<![\d])(?!12(?:\.0)?[ \t]*人)(\d+(?:\.\d+)?)[ \t]*人", "待核实", source_facts)
        value = _replace_unlisted_metric(value, r"(?<![A-Za-z0-9])(?<![\d])(?!8(?:\.0)?[ \t]*小时)(\d+(?:\.\d+)?)[ \t]*小时", "待核实", source_facts)
        value = _replace_unlisted_metric(value, r"(?<![A-Za-z0-9])(?<![\d])(?!4(?:\.0)?[ \t]*次)(\d+(?:\.\d+)?)[ \t]*次", "待核实", source_facts)
        value = _replace_unlisted_metric(value, r"(?<![A-Za-z0-9])(?<![\d])\d+(?:\.\d+)?[ \t]*[%％]", "待核实", source_facts)
        value = re.sub(r"约\s*待核实", "数量待核实", value)
        value = re.sub(r"在\s*待核实\s*内", "时限待核实", value)
        value = re.sub(r"在\s*(?:分钟|小时)内", "时限待核实", value)
        value = re.sub(r"(?:不少于|不低于|不超过|不大于|不小于|以内|以下)\s*待核实", "指标待核实", value)
        value = re.sub(r"(?:^|(?<=[。；\n]))(而是|而且|并且)", r"该项安排\1", value)
        for old, new in ROLE_DRIFT.items(): value = replace_once(value, old, new)
        value = INTERNAL_ID.sub("", value)
        value = collapse_repeated_conditionals(value)
        restored = restore_protected(re.sub(r"\s{2,}", " ", value).strip(), saved)
        return _compact_listed_metrics(restored, source_facts)
    return value


def _repair_word_artifact(value, source_facts=None):
    from governance.final_editorial import repair_with_source_facts
    return repair_with_source_facts(value, lambda protected: _repair_customer_text(
        apply_artifact_repairs(apply_local_repairs(protected)), source_facts), source_facts)


class LongformGenerationFactory:
    # 类级默认值：契约测试可以绕过 __init__ 直接调用无状态解析/装配helper。
    batch_plan: dict[str, Any] | None = None
    envelope_transport: bool = True
    envelope_calls: int = 0
    envelope_recovered_cus: int = 0
    envelope_truncated_cus: int = 0
    technical_retry_calls: int = 0
    technical_retry_budget: int = 0
    technical_retry_permits: int = 0
    technical_retry_seconds: float = 0.0
    provisional_sections: list[str] = []
    depth_gap_sections: list[dict[str, Any]] = []

    def __init__(self, *, run_root: Path, provider: Any, provider_name: str, inputs: dict[str, Any]):
        self.run_root = run_root.resolve()
        from governance.final_editorial import SourceFacts
        self.editorial_source_facts = SourceFacts.from_run(self.run_root)
        self.provider = provider
        self.provider_name = provider_name
        self.inputs = inputs
        self.word_root = self.run_root / "longform" / "word"
        self.ppt_root = self.run_root / "longform" / "ppt"
        # Blocker 4（Round 2）：Content Unit 真实接线 —— CU 契约注册、接收记录。
        self.cu_contracts: dict[str, dict] = dict(inputs.get("cu_contracts") or {})
        self.cus_by_section: dict[str, list[str]] = {}
        for cu_id, contract in self.cu_contracts.items():
            self.cus_by_section.setdefault(str(contract.get("parent_section_id") or ""), []).append(cu_id)
        self.received_content_unit_ids: list[str] = []
        self.provider_acknowledged_content_unit_ids: list[str] = []
        self.sent_content_unit_ids: list[str] = []
        self.returned_content_unit_ids: list[str] = []
        self.active_word_section_ids: list[str] = []
        self.allow_legacy_cu_fallback = bool(inputs.get("allow_legacy_cu_fallback", False))
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
        self._restore_supplement_cus()
        # 请求账的追加必须独占（见 `_append_request_event`），且不与 checkpoint 锁互相缠绕：
        # 清扫路径会在持账锁的同时读账。
        self.ledger_write_lock = threading.Lock()
        self.ledger_write_failures: list[dict[str, Any]] = []
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
        self.concurrency_ledger: list[dict[str, Any]] = []
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
        # MMF-007C：调用分类必须能回答"初稿花了多少、技术重试花了多少、修复花了多少"。
        self.technical_retry_calls = 0
        self.technical_retry_budget = 0
        self.technical_retry_permits = 0
        self.technical_retry_seconds = 0.0
        self.repair_calls = 0
        self.supplement_calls = 0
        self.supplement_seconds = 0.0
        self.supplement_wall_seconds = 0.0
        self.selective_repair_wall_seconds = 0.0
        self.global_repair_budget: GlobalRepairBudget | None = None
        # §14 生成中预算账：每批 CU 回来后重算"已用正文/已用结构/剩余本节/剩余全书"，
        # 并当场定出下一批每单元字数上限。计划快照只在第一次同步时从统一预算树取。
        self.budget_midrun_plan: dict[str, Any] = empty_midrun_state(inputs.get("budget_tree") or {})
        self.budget_unit_caps: dict[str, int] = {}
        self.envelope_transport = bool(inputs.get("envelope_transport", True))
        self.envelope_calls = 0
        self.envelope_recovered_cus = 0
        self.envelope_truncated_cus = 0
        # P0-1：晚到响应回收与受控重发都必须可数，否则无法证明"没有重复扣费"。
        self.late_response_recovered = 0
        self.controlled_resends = 0
        self.reconcile_provider_calls = 0
        self.late_response_grace_seconds = float(inputs.get("late_response_grace_seconds") or os.environ.get("MMF_LATE_RESPONSE_GRACE_SECONDS") or 0)
        self._settled_request_ids: set[str] = set()
        self.reconciliation: dict[str, Any] = {"requests_open": 0, "late_recovered": 0, "controlled_resends": 0, "still_unknown": 0, "sections_recovered": []}
        self.provisional_sections: list[str] = []
        self.depth_gap_sections: list[dict[str, Any]] = []
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
        for name in ("word_plan", "requirement_matrix", "section_contracts", "global_state", "ppt_plan", "dependency_map", "requirement_classification", "commercial_compliance_matrix", "technical_scope_gate", "bidder_evidence_ledger"):
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
    def _word_prompt(context: dict[str, Any], repair: list[str] | None = None, *, envelope_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        contract = context["section_contract"]
        system = "你是资深物业服务方案撰稿人，只负责当前Section。禁止工具、文件、调查和追加上下文。只返回JSON。"
        rules = ["不得新增目录或改变Section目的", "不得完整展开其他Section主责Topic", "正文不得出现REQ/SCR/KU等内部ID", "不得输出AI说明", "不得新增人员、频次、时限、设备或商业承诺", "投标人荣誉、认证、资质、案例、平台和团队事实必须引用已核验Bidder Evidence；无证据时只输出一处行内提示【待投标人确认：所需证明】，不得为它扩写整页空白履历表、待填写表格或证书复印件页", "本方案只写技术标正文：商务、报价、资格、资信、法务与投标程序条款一律不得展开，不得为其写说明段、清单或空白附件页（企业证书附件页、资质附件页、AAA附件页、ISO证书附件页、类似项目业绩附件页、人员履历附件页都属禁止项），也不得在正文中出现其内部分类名称",
            # MMF008A-C：上一句按分类逐条点名，模型就转手写一篇"本章如何处理这些分类"。
            # 这条把口径收回来：不写的内容**直接不写**，不解释写法、不分层宣告、不说明理由。
            # 刻意不举被禁句子的原话——举了就等于把那几句交给模型抄进正文（Run F 实测如此）。
            "正文只写服务内容本身：不得解释这份文档怎么写、某类材料为什么不写、内容如何分层与处置、哪些数字可以写进正文。凡决定不写的内容一律省略，不要留下一句说明理由的话；需要投标人或甲方补充的事实只按给定的行内提示格式登记",
            "结构化Archetype必须返回可渲染payload", FORM_PAYLOAD_SPEC, "使用项目负责人等Canonical角色", "以动作、职责、异常路径、成果、记录和检查方法形成可执行初稿",
            "每个小节至少写两段可执行正文，并给出一张对照表。不得只留一句话，不得把“含计划表、事项表、机制表、跟踪表”这类编写提示直接写成正文。",
            "步骤用完整中文句子，包含动作和责任岗位。不要返回空键字典，不要返回空步骤。表注写成句子，不要返回 Python 列表。",
            "小节是合理化建议时，写给委托方的建议：每条包含痛点、措施、落地步骤和可核对的管理结果，并附一张汇总表。不得写成内部员工填写建议单、评审试行、固化核销。只使用本次材料已经出现的业态和条件，不编造未出现的楼栋、面积或企业名称。",
            "人员姓名和证照未提供时，把招标文件已经写明的人员条件转成拟派团队的选拔标准，不输出“尚待核实”，也不编造姓名和证书编号。"]
        rules.extend(WORD_V12_WRITING_POLICY)
        if repair: rules.extend(repair)
        pack = context.get("repair_pack") or context
        lead = "根据修订包只修补当前Section缺口，保留已有有效正文，禁止整章重写。" if context.get("repair_pack") else "根据Context Pack生成一个Word Section结构化Content Fragment。"
        # 这条线原来写"目标内容量为 X～Y 个中文内容单位"，给的却是字数：
        # 单位名不对，模型没有理由把表格算进这份量里。改成明确的统一额度口径。
        budget = ("本节统一内容额度为{min}～{max}字，表格、步骤、清单与表单的字**已经算在**"
                  "这份额度里，不是在额度之外另交；逐单元额度见下方契约或信封。"
                  "避免理念重复和机械注水。").format(min=contract["target_words"]["min"],
                                                    max=contract["target_words"]["max"])
        requested = envelope_rows or context.get("content_unit_contracts") or (context.get("repair_pack") or {}).get("content_unit_contracts")
        if requested:
            budget = (f"本作业只交付指定的{len(requested)}个CU；各CU按自己的target_budget和必备内容完成。"
                      "整节target_words由系统累计检查，本批不补写未请求的CU，也不为凑足整节额度扩写本批。")
        if envelope_rows:
            system = system.replace("只返回JSON。", "只按CU信封输出文本块，不要JSON、不要代码围栏。")
            spec = "\n".join(envelope_contract_lines(envelope_rows)) + "\n"
        else:
            spec = (
                "根字段必须且只能包含section_id,title,body_blocks,content_unit_fragments,tables,processes,"
                "callouts,cross_references,claims,used_requirement_ids,used_ku_ids,generation_notes。"
                "content_unit_fragments必须为每个待生成Content Unit返回且仅返回一条"
                "{content_unit_id,archetype,content,payload}；结构化Archetype必须在payload返回表格、步骤、清单或表单字段，"
                "Section正文由系统按CU结果装配。body_blocks使用paragraph/subheading/bullet_group/numbered_steps/note/summary等type与content。"
            )
        prompt = lead + budget + spec + "你已经获得全部信息，禁止调用工具、读取文件、继续调查或要求补充材料。规则：\n- {rules}\nContext Pack：\n{context}".format(rules="\n- ".join(rules), context=json.dumps(pack, ensure_ascii=False))
        return {"system": system, "prompt": prompt}

    @staticmethod
    def _ppt_prompt(context: dict[str, Any], repair: list[str] | None = None) -> dict[str, Any]:
        from planning.ppt_service import VERSION, prompt
        if context["slide"].get("profile_p_contract") == VERSION:
            return prompt(context, repair, text_budget=SLIDE_BUDGET.get(context["slide"].get("slide_role"), 220))
        slide = context["slide"]
        rules = ["只生成当前一页，不改变Storyboard", "不得读取或总结Word正文", "像面向甲方的物业服务方案汇报", "正文不得显示REQ/SCR/KU内部ID", "不得编造案例", "P04主讲通用通行秩序，P07主讲生产物流与业务协同", "评分标准、分值、评标等招标信息严禁出现在任何字段", "content_blocks必须3-4个，每个含title与40-80字description，引用项目事实中的具体数字、频次或标准，禁止空话套话", "visual_data按Storyboard的required_visual填充完整结构（如elements、center_node、surrounding_nodes）"]
        if repair: rules.extend(repair)
        prompt = "根据Context Pack生成一个PPT Slide Content Payload。根字段必须且只能包含slide_id,headline,subheadline,key_message,content_blocks,visual_data,component_intent,speaker_note_optional,source_trace,generation_notes。content_blocks应短句化，避免Word长段。你已经获得全部信息，禁止调用工具、读取文件、继续调查或要求补充材料。规则：\n- {rules}\nContext Pack：\n{context}".format(rules="\n- ".join(rules), context=json.dumps(context, ensure_ascii=False))
        return {"system": "你是物业服务方案PPT内容策划，只负责当前Slide。禁止工具、文件和调查。只返回JSON。", "prompt": prompt}

    def _restore_call_accounting(self, job: LongformGenerationJob, *, volume_continuation: bool = False) -> None:
        historic = int(getattr(job, "provider_calls_total", 0) or 0) or int(job.provider_call_count or 0)
        checkpoint_path = self.run_root / "checkpoint" / "state.json"
        # P0-5（Codex Blocker）：生产 Resume 入口自动迁移 —— schema detect → migrate →
        # validate → 回写；旧 v0.2 checkpoint 在正式链路透明升级。
        previous = None
        from .hierarchical import load_checkpoint_state as _load_migrated
        migrated = _load_migrated(self.run_root)
        if migrated is not None:
            previous = migrated
            historic = max(
                historic,
                int(previous.get("provider_calls_total") or 0),
                int(previous.get("provider_call_count") or 0),
            )
        elif checkpoint_path.is_file():
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
        if checkpoint_path.is_file() and previous is not None:
            try:
                self.token_accountant_cumulative.restore(previous.get("token_accounting_cumulative") or previous.get("token_accounting"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                pass
        # Resume is any prior attempted job, not only a job that already has PASS sections.
        # Historic calls, failed/blocked batches, or retry state all count as resume.
        # volume_continuation=True（编排器第 2+ 卷）不重判恢复、不重置会话计数器：
        # 同一次运行内跨 Volume 的跳过不是恢复。
        # 会话判定闩锁：每个 factory 会话只判定一次。同一次运行内跨 Volume 的
        # 跳过不是恢复；真实崩溃后的新工厂实例会重新判定为 Resume。
        if getattr(self, "_resume_session_evaluated", False):
            return
        self._resume_session_evaluated = True
        self.this_run_calls = 0
        self.this_resume_calls = 0
        self.admission_attempts = 0
        self.token_accountant = TokenAccountant()
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
                if bid and sids and all(self._section_is_draftable(sid) for sid in sids):
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
            checkpoint_path = self.run_root / "checkpoint" / "state.json"
            try:
                previous_checkpoint = read_json(checkpoint_path) if checkpoint_path.is_file() else {}
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                previous_checkpoint = {}
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
                "schema_version": "longform-checkpoint-v0.3-hierarchical",
                "completed_volumes": sorted({
                    volume.get("volume_id")
                    for volume in ((self.inputs.get("volume_plan") or {}).get("volumes") or [])
                    if volume.get("section_ids")
                    and all(sid in job.completed_units for sid in volume["section_ids"])
                }) if self.inputs.get("volume_plan") else [],
                "completed_chapters": [],
                "completed_content_units": sorted(
                    p.stem
                    for p in (self.word_root / "sections").glob("*/cus/CU-*.json")
                ) if self.cu_contracts else list(job.completed_units),
                "volume_artifacts": (lambda p: (json.loads(p.read_text(encoding="utf-8-sig")) if p.is_file() else {}).get("volumes", []))(self.run_root / "volume_progress.json"),
                "lineage_fingerprint": (lambda p: (json.loads(p.read_text(encoding="utf-8-sig")) if p.is_file() else {}).get("requirement_fingerprint"))(self.run_root / "10_hierarchy_lineage.json"),
                "run_id": job.run_id,
                "provider": job.provider,
                "status": job.status,
                "public_phase": job.public_phase,
                "total_units": int(job.total_units or 0),
                "progress": {
                    "done": len(completed),
                    "total": int(job.total_units or 0),
                    "failed": len(failed),
                },
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
            if previous_checkpoint.get("migration"):
                snapshot["migration"] = previous_checkpoint["migration"]
            # P0-5（Codex Blocker）：Checkpoint 原子写入（temp→fsync→os.replace）。
            from .hierarchical import atomic_write_json as _atomic_write
            try:
                _atomic_write(checkpoint_path, snapshot)
            except OSError as exc:
                # Testable production seam for ENOSPC/permission failures.  The
                # previous checkpoint remains authoritative because atomic_write
                # never replaces it on failure.
                try:
                    write_json(self.run_root / "technical_failure.json", {
                        "status": "CHECKPOINT_PERSIST_FAILED",
                        "error_type": type(exc).__name__,
                        "errno": getattr(exc, "errno", None),
                        "error": str(exc)[:500],
                        "recorded_at": now_iso(),
                    })
                except OSError:
                    pass
                raise

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
            "content_unit_fragments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content_unit_id": {"type": "string"},
                        "parent_section_id": {"type": "string"},
                        "content": {"type": "string"},
                        "archetype": {"type": "string"},
                        "payload": {"type": ["object", "array", "null"]},
                    },
                    "required": ["content_unit_id", "content"],
                },
            },
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

    def _ledger_state(self) -> tuple[threading.Lock, list[dict[str, Any]]]:
        """台账容器：`__init__` 里建，但裸实例（离线恢复、以及本仓库按惯例用 `__new__`
        直构工厂的单元测试）也要能安全写，所以取值走 `setdefault` —— 同一个实例永远一把锁。"""
        lock = self.__dict__.setdefault("ledger_write_lock", threading.Lock())
        failures = self.__dict__.setdefault("ledger_write_failures", [])
        return lock, failures

    def _append_request_event(self, path: Path, marker: dict[str, Any], status: str, **extra: Any) -> None:
        """Provider 请求生命周期事件：一条记录一次成形的写，且必须串行。文本 `"a"` 句柄按块
        刷新，多线程并发追加会把一条记录在半中间撕开（Round 4 全量回归 `Fault10` 偶发失败实测
        读到过 `", "error": "Response timeout (injected)"}` 这样的残行），而这条账是晚到响应
        核对唯一的依据 —— 撕掉的半行读不出来，等于一次已经付过费的请求凭空消失。
        写不进去也不许静默返回：失败必须留在 `ledger_write_failures` 里。"""
        event = {**marker, "status": status, "event_at": now_iso(), **extra}
        lock, failures = self._ledger_state()
        try:
            payload = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        except (TypeError, ValueError) as exc:
            failures.append({"status": status, "error": str(exc)[:200]})
            return
        try:
            with lock, path.open("ab") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            failures.append({"status": status, "error": str(exc)[:200]})

    def _collect_late_response(self, origin: dict[str, Any], payload: dict[str, Any], task_dir: Path) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """P0-1：把一个 RESPONSE_UNKNOWN 请求的晚到响应回收成正常输出。

        返回 None 表示"仍然未知"，调用方据此进入受控重发或明确终态；返回元组则
        等价于一次成功调用，但不追加任何 Provider 调用计数（额度在首次发送时已用）。
        """
        collect = getattr(self.provider, "collect_late_response", None)
        job_id = str(origin.get("job_id") or "")
        if payload.get("generation_mode") == "ppt_slide" and origin.get("slide_id") != payload.get("slide_id"):
            return None
        if not callable(collect) or not job_id:
            return None
        probe = dict(payload)
        probe["job_id"] = job_id
        probe["request_id"] = str(origin.get("request_id") or "")
        probe["run_id"] = str(origin.get("run_id") or self.run_root.name)
        try:
            collected = collect(probe, task_dir)
        except Exception as exc:  # noqa: BLE001 - 回收失败必须退回"仍未知"，不得打断生成链
            if (payload.get("generation_mode") == "ppt_slide" and isinstance(exc, ProviderError)
                    and exc.error_code in {"INVALID_JSON", "OUTPUT_CONTRACT_ERROR"}):
                self._append_request_event(
                    self.run_root / "provider_unknown_requests.jsonl", dict(origin),
                    request_ledger.RESPONSE_REJECTED, error_code=exc.error_code,
                    error=str(exc)[:200], resolution="invalid_native_response",
                )
                return None
            self._append_request_event(
                self.run_root / "provider_unknown_requests.jsonl",
                {"request_id": probe["request_id"], "request_fingerprint": origin.get("request_fingerprint"),
                 "job_id": job_id, "content_unit_ids": origin.get("content_unit_ids") or []},
                "LATE_RESPONSE_RECOVERY_FAILED",
                error=str(exc)[:200],
            )
            return None
        if not collected:
            return None
        output, meta = collected
        if not isinstance(output, dict) or not isinstance(meta, dict):
            return None
        # sent_monotonic 只在同一进程内可比；跨进程回收时不伪造等待时长。
        raw_sent = origin.get("sent_monotonic")
        elapsed = round(time.monotonic() - float(raw_sent), 3) if raw_sent else 0.0
        duration = elapsed if 0.0 <= elapsed <= 86400.0 else 0.0
        tokens = resolve_attempt_tokens(
            actual=extract_token_usage(meta, output, meta.get("envelope") if isinstance(meta, dict) else {}),
            estimated_input=estimate_tokens(payload.get("prompt") or ""),
            estimated_output=estimate_tokens(output),
        )
        with self.checkpoint_lock:
            self.late_response_recovered += 1
            self.token_accountant.add(tokens)
            self.token_accountant_cumulative.add(tokens)
            self.provider_acknowledged_content_unit_ids.extend(str(x) for x in (origin.get("content_unit_ids") or []))
        marker = {
            "request_id": probe["request_id"],
            "run_id": probe["run_id"],
            "job_id": job_id,
            "request_fingerprint": origin.get("request_fingerprint"),
            "batch_id": origin.get("batch_id"),
            "section_ids": origin.get("section_ids") or [],
            "content_unit_ids": origin.get("content_unit_ids") or [],
            "provider": origin.get("provider") or self.provider_name,
            "attempt_no": origin.get("attempt_no") or 1,
        }
        path = self.run_root / "provider_unknown_requests.jsonl"
        self._append_request_event(
            path, marker, request_ledger.RESPONSE_RECEIVED_LATE,
            provider_received_content_unit_ids=list(marker["content_unit_ids"]),
            wait_since=origin.get("sent_at"),
        )
        audit = {
            "status": "SUCCESS",
            "provider_received_content_unit_ids": list(marker["content_unit_ids"]),
            "provider_received_evidence": "late_response_recovered",
            "late_response_recovered": True,
            "request_id": marker["request_id"],
            "request_fingerprint": marker["request_fingerprint"],
            "duration_seconds": duration,
            "invoked": False,
            "finish_reason": "stop",
            "input_tokens": tokens["input_tokens"],
            "output_tokens": tokens["output_tokens"],
            "token_provenance": tokens["token_provenance"],
            "provider_metadata": meta,
            "stage": "reconcile",
        }
        return output, audit

    def _settle_request(self, audit: dict[str, Any], *, resolution: str, persisted_content_unit_ids: list[str] | None = None) -> None:
        """PERSISTED → RESOLVED：请求只有在其内容落到磁盘产物后才算关闭。"""
        request_id = str(audit.get("request_id") or "")
        if not request_id:
            return
        with self.checkpoint_lock:
            if request_id in self._settled_request_ids:
                return
            self._settled_request_ids.add(request_id)
        marker = {
            "request_id": request_id,
            "request_fingerprint": audit.get("request_fingerprint"),
            "content_unit_ids": list(audit.get("provider_received_content_unit_ids") or []),
        }
        path = self.run_root / "provider_unknown_requests.jsonl"
        self._append_request_event(
            path, marker, request_ledger.PERSISTED,
            persisted_content_unit_ids=list(persisted_content_unit_ids or []),
        )
        self._append_request_event(path, marker, request_ledger.RESOLVED, resolution=resolution)

    def _supersede_resolved_fingerprints(
        self, path: Path, events: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], int]:
        """同一逻辑请求只要有一个 attempt 已经 RESOLVED，其余悬空行落到明确终态。

        受控重发成功后，首次"已发出未确认"的行不能继续算未解决，否则清扫统计会
        把已经拿回内容的请求报成 still_unknown，§7 要求的"明确状态"就落不了地。
        """
        resolved = {
            str(row.get("request_fingerprint") or "")
            for row in events
            if row.get("status") == request_ledger.RESOLVED and row.get("request_fingerprint")
        }
        superseded = 0
        targets: list[dict[str, Any]] = []
        for row in request_ledger.open_requests(events):
            fingerprint = str(row.get("request_fingerprint") or "")
            if fingerprint and fingerprint in resolved:
                self._append_request_event(
                    path,
                    {"request_id": row.get("request_id"), "request_fingerprint": fingerprint,
                     "content_unit_ids": row.get("content_unit_ids") or []},
                    request_ledger.RESOLVED,
                    resolution="superseded_by_controlled_resend",
                )
                superseded += 1
                continue
            targets.append(row)
        return targets, superseded

    def reconcile_pending_provider_requests(self, *, job: LongformGenerationJob | None = None, allow_retry: bool = True) -> dict[str, Any]:
        """P0-1 / §6：Final Assembly 之前的晚到响应清扫。

        仍处于 REQUEST_SENT / RESPONSE_UNKNOWN 的请求逐个尝试回收：响应已经在队列
        落地就走正常持久化链路（同一批次重放，内容不再重新付费）；仍然未知的，最多
        接受一次受控重发，之后落到明确状态，不进入无限重试。
        """
        path = self.run_root / "provider_unknown_requests.jsonl"
        ledger_integrity: dict[str, Any] = {}
        events = request_ledger.read_events(path, ledger_integrity)
        open_rows = request_ledger.open_requests(events)
        stats: dict[str, Any] = {
            "requests_open": len(open_rows),
            "late_recovered": 0,
            "controlled_resends": 0,
            "superseded": 0,
            "already_persisted": 0,
            "partially_persisted": 0,
            "still_unknown": len(open_rows),
            "replayed_batches": [],
            "sections_recovered": [],
            "provider_calls": 0,
        }
        targets, stats["superseded"] = self._supersede_resolved_fingerprints(path, events)
        # 内容已由后续请求落盘的悬空行直接关闭：既不重复扣费，也不把已完成的
        # Section 报成未解决。真正还缺 Content Unit 的行才进入回收/受控重发。
        live_targets: list[dict[str, Any]] = []
        for row in targets:
            requested = [str(cu) for cu in (row.get("content_unit_ids") or [])]
            completed = {str(cu) for sid in (row.get("section_ids") or []) for cu in self._completed_cus(sid)}
            if requested and set(requested) <= completed:
                self._append_request_event(
                    path,
                    {"request_id": row.get("request_id"),
                     "request_fingerprint": row.get("request_fingerprint"),
                     "content_unit_ids": requested},
                    request_ledger.RESOLVED,
                    resolution="content_already_persisted",
                )
                stats["already_persisted"] += 1
                continue
            live_targets.append(row)
        targets = live_targets
        if targets:
            by_id = {row["section_id"]: row for row in self.inputs["section_contracts"]["contracts"]}
            calls_before = self.provider_calls_total
            pending_job = job or self._job(self.word_root, "WORD", len(by_id))
            for row in targets:
                # 分卷各自持有 batch_plan，晚到请求往往属于已经跑完的分卷。按 id 查
                # 当前分卷的批次表会把别的分卷的批次当成待回收请求重放，所以这里直接
                # 用台账行自己重建批次：ledger 与 Section 契约都是全 Run 范围的。
                sections = [
                    str(sid) for sid in (row.get("section_ids") or [])
                    if str(sid) in by_id and self._missing_cus(str(sid))
                ]
                if not sections:
                    continue
                # 只有"该 Section 的 CU 一个都没落盘"时，重放出去的请求才和被超时的
                # 原请求同形（同 request_fingerprint），因此必然命中守卫、走回收而不是
                # 再次计费。部分已落盘时形状会变，等于新请求，§7 禁止这样立即重发。
                if any(
                    set(self._planned_cus(sid)) != set(self._missing_cus(sid))
                    for sid in sections if self._planned_cus(sid)
                ):
                    stats["partially_persisted"] += 1
                    continue
                batch = {
                    "batch_id": str(row.get("batch_id") or "") or "RECONCILE-" + "-".join(sections),
                    "section_ids": sections,
                }
                before_cus = {sid: set(self._missing_cus(sid)) for sid in sections}
                result = self._run_batch(pending_job, batch, by_id, stage="reconcile", allow_unknown_resend=allow_retry)
                stats["replayed_batches"].append({"batch_id": batch["batch_id"], "status": result.get("status"), "error_code": result.get("error_code")})
                for sid in sections:
                    if before_cus[sid] and not (set(self._missing_cus(sid)) & before_cus[sid]):
                        stats["sections_recovered"].append(sid)
            stats["provider_calls_this_pass"] = self.provider_calls_total - calls_before
            with self.checkpoint_lock:
                self.reconcile_provider_calls += stats["provider_calls_this_pass"]
            _, superseded_after = self._supersede_resolved_fingerprints(path, request_ledger.read_events(path))
            stats["superseded"] += superseded_after
        # 报告一律从整份台账推导：清扫会在每个分卷结束时各跑一次，任何一次的输出
        # 都必须描述整个 Run 的真实状态，而不是"这一次有没有活干"。
        events = request_ledger.read_events(path)
        open_after = request_ledger.open_requests(events)
        resolutions = [str(row.get("resolution") or "") for row in events if row.get("status") == request_ledger.RESOLVED]
        stats.update({
            "late_recovered": sum(1 for row in events if row.get("status") == request_ledger.RESPONSE_RECEIVED_LATE),
            "controlled_resends": sum(1 for row in events if row.get("status") == request_ledger.RESEND_CONTROLLED),
            "superseded": resolutions.count("superseded_by_controlled_resend"),
            "already_persisted": resolutions.count("content_already_persisted"),
            "still_unknown": len(open_after),
            "provider_calls": self.reconcile_provider_calls,
        })
        # 以磁盘上真实存在的 CU 文件为准（跨分卷可比），只有"仍在开放且确实没落盘"
        # 的 Content Unit 才进复核清单。
        unresolved: set[str] = set()
        for row in open_after:
            landed = {str(cu) for sid in (row.get("section_ids") or []) for cu in self._completed_cus(sid)}
            unresolved.update(str(cu) for cu in (row.get("content_unit_ids") or []) if str(cu) not in landed)
        stats["unresolved_content_unit_ids"] = sorted(unresolved)
        # 账本自身的完整性：坏行与写失败必须一起进清扫统计，否则"没有悬空请求"既可能是
        # 真的没有、也可能是账被写坏了 —— 这两种情况不能读成同一件事。
        stats["ledger_integrity"] = {
            **ledger_integrity,
            "append_failures": list(self.ledger_write_failures),
            "append_failure_count": len(self.ledger_write_failures),
        }
        self.reconciliation = stats
        write_json(self.run_root / "late_response_reconciliation.json", {**stats, "updated_at": now_iso()})
        return stats

    def _invoke(self, request: dict[str, Any], task_dir: Path, *, stage: str = "draft", failure_class: str | None = None, batch_size: int = 1, allow_unknown_resend: bool = False) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        started = time.monotonic()
        profile = self._profile_for(stage, failure_class, batch_size)
        payload = apply_to_request(request, profile)
        if payload.get("generation_mode") in {"longform_batch", "longform_section"}:
            payload.setdefault("response_protocol", "batch_json" if payload["generation_mode"] == "longform_batch" else "section_json")
        if payload.get("response_protocol") == "cu_envelope":
            # 信封传输由 longform.envelope 解析，强制 json_schema 会让 Provider
            # 重新回到"一个括号截断就废掉整批"的失败模式。
            payload.pop("json_schema", None)
        elif payload.get("generation_mode") == "ppt_slide":
            # A PPT page is not a Word section; transport validates PPT required keys.
            payload.pop("json_schema", None)
        elif payload.get("generation_mode") == "longform_batch":
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
        # 发送前就失败的调用不得伪造一条"可能被接受"的台账：没有 request_marker 就是
        # 没有向 Provider 发出过任何东西，异常分支据此跳过记账。
        request_marker: dict[str, Any] = {}
        unknown_path = self.run_root / request_ledger.LEDGER_FILENAME
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
            # V1.1 FAULT-10 语义：请求已发送但响应状态未知时，记录请求标识，
            # 供人工/后续核对；系统不得盲目立即重发同一请求。
            requested_cu_ids = [
                str(row.get("content_unit_id"))
                for row in (payload.get("content_unit_contracts") or [])
                if isinstance(row, dict) and row.get("content_unit_id")
            ]
            request_subject = {
                "generation_mode": payload.get("generation_mode"),
                "batch_id": payload.get("batch_id"),
                "section_ids": payload.get("section_ids") or ([payload.get("section_id")] if payload.get("section_id") else []),
                "content_unit_ids": requested_cu_ids,
            }
            if payload.get("generation_mode") == "ppt_slide":
                request_subject["slide_id"] = str(payload.get("slide_id") or "")
                if not request_subject["slide_id"]:
                    raise ProviderError("PPT request missing slide identity", error_code="PPT_SLIDE_ID_MISSING")
            request_signature = json.dumps(request_subject, ensure_ascii=False, sort_keys=True)
            request_fingerprint = hashlib.sha256(request_signature.encode("utf-8")).hexdigest()
            unknown_path = self.run_root / "provider_unknown_requests.jsonl"
            unknown_path.parent.mkdir(parents=True, exist_ok=True)
            ledger_events = request_ledger.read_events(unknown_path)
            open_rows = request_ledger.unresolved_for_fingerprint(ledger_events, request_fingerprint)
            attempt_no = request_ledger.attempts_seen(ledger_events, request_fingerprint) + 1
            if open_rows:
                origin = open_rows[-1]
                # 先回收，再决定是否重发：晚到内容一律保留，不因主协程超时而丢弃。
                recovered = self._collect_late_response(origin, payload, task_dir)
                if recovered is not None:
                    return recovered
                resends_used = request_ledger.control_resends_seen(ledger_events, request_fingerprint)
                if not allow_unknown_resend or resends_used >= 1 or (
                    is_cursor_provider(self.provider_name)
                    and payload.get("generation_mode") in {"longform_section", "longform_batch"}
                ):
                    return None, {
                        "status": "UNKNOWN_REQUEST_REVIEW",
                        "error_code": "RESPONSE_UNKNOWN",
                        "error": "A matching request may already have been accepted; automatic resend is blocked.",
                        "request_id": origin.get("request_id"),
                        "request_fingerprint": request_fingerprint,
                        "open_requests": len(open_rows),
                        "invoked": False,
                        "finish_reason": "blocked_unknown_request",
                        "stage": stage,
                    }
                # 受控重发预算先落盘再发送：进程崩溃也不会出现第二次重发。
                self._append_request_event(
                    unknown_path,
                    {"request_id": origin.get("request_id"), "request_fingerprint": request_fingerprint,
                     "job_id": origin.get("job_id"), "content_unit_ids": requested_cu_ids,
                     "batch_id": payload.get("batch_id"), "section_ids": origin.get("section_ids")},
                    request_ledger.RESEND_CONTROLLED,
                    next_attempt_no=attempt_no,
                    grace_period_seconds=self.late_response_grace_seconds,
                )
                with self.checkpoint_lock:
                    self.controlled_resends += 1
            identity = request_ledger.mint_identity(
                payload=payload,
                run_id=self.run_root.name,
                fingerprint=request_fingerprint,
                provider=self.provider_name,
                attempt_no=attempt_no,
                content_unit_ids=requested_cu_ids,
            )
            payload["job_id"] = identity["job_id"]
            payload["request_id"] = identity["request_id"]
            payload["run_id"] = identity["run_id"]
            request_marker = {
                **identity,
                "sent_at": now_iso(),
                "sent_monotonic": round(invoke_started, 3),
                "status": request_ledger.REQUEST_SENT,
            }
            if payload.get("generation_mode") == "ppt_slide":
                request_marker["slide_id"] = payload["slide_id"]
            self._append_request_event(unknown_path, request_marker, request_ledger.REQUEST_SENT)
            with self.checkpoint_lock:
                self.provider_calls_total += 1
                self.provider_call_count = self.provider_calls_total
                self.this_run_calls += 1
                if self._resume_session:
                    self.this_resume_calls += 1
                mode = str(request.get("attempt_mode") or payload.get("attempt_mode") or "")
                if mode in {"generate", "generate_batch"}:
                    self.initial_generation_calls += 1
                elif mode == "retry_technical":
                    self.technical_retry_calls += 1
                elif mode == "supplement_section":
                    self.supplement_calls += 1
                    self.repair_calls += 1
                elif mode == "continue_section":
                    self.continuation_calls += 1
                    self.section_repair_calls += 1
                    self.repair_calls += 1
                elif mode == "repair_section":
                    self.section_repair_calls += 1
                    self.repair_calls += 1
                if str(failure_class or "") in {"TIMEOUT", "LATENCY_FAILURE", "RATE_LIMIT"}:
                    self.retry_calls += 1
                self._record_escalation(profile, failure_class)
                self.sent_content_unit_ids.extend(requested_cu_ids)
            output = self.provider.invoke_structured(payload, task_dir)
            duration = round(time.monotonic() - invoke_started, 3)
            with self.checkpoint_lock:
                # 成功返回结构化响应即构成Provider已接收本次CU请求的可审计证据。
                # 不依赖Mock专用的received_content_unit_ids属性。
                self.provider_acknowledged_content_unit_ids.extend(requested_cu_ids)
                self.provider_wait_seconds += duration
                attempt_mode = str(request.get("attempt_mode") or "")
                if attempt_mode in {"generate", "generate_batch"}:
                    self.initial_generation_seconds += duration
                elif attempt_mode in {"repair_section", "continue_section"}:
                    self.repair_provider_seconds += duration
                elif attempt_mode == "supplement_section":
                    self.supplement_seconds += duration
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
                "provider_received_content_unit_ids": list(requested_cu_ids),
                "provider_received_evidence": "successful_structured_response",
                "request_id": request_marker.get("request_id"),
                "request_fingerprint": request_marker.get("request_fingerprint"),
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
            # RESPONSE_RECEIVED 只代表内容到达；PERSISTED/RESOLVED 由真正落盘的调用方
            # 追加（_settle_request），否则"已答未存"的崩溃会把内容永久丢掉。
            self._append_request_event(unknown_path, request_marker, request_ledger.RESPONSE_RECEIVED, provider_received_content_unit_ids=list(requested_cu_ids))
            return output, audit
        except Exception as exc:
            if not isinstance(exc, (ProviderError, ProviderUnavailableError, AdmissionError)):
                timeout_like = isinstance(exc, TimeoutError) or "timeout" in str(exc).lower() or "timed out" in str(exc).lower()
                wrapped = ProviderError(str(exc), error_code="TIMEOUT" if timeout_like else "PROVIDER_RUNTIME_ERROR")
                wrapped.__cause__ = exc
                exc = wrapped
            # V1.1 FAULT-10：请求已发出但响应未确认（超时类）→ 记录请求标识与
            # RESPONSE_UNKNOWN 状态；不得盲目立即重发同一请求。
            exc_message = str(getattr(exc, "__cause__", None) or exc)
            zcode_unknown = self.provider_name == 'zcode_cn' and getattr(exc,'error_code','') in {'ZCODE_RESPONSE_UNKNOWN','ZCODE_REQUEST_INCOMPLETE','ZCODE_NO_AUTOMATIC_RESEND'}
            zcode_not_sent = self.provider_name == 'zcode_cn' and getattr(exc,'error_code','') in {'ZCODE_RUN_BUDGET','ZCODE_NOT_DISPATCHED','PPT_TIME_BUDGET','ZCODE_PLAN_INVALID','ZCODE_PLAN_IDENTITY'}
            cursor_code = getattr(exc, 'error_code', '') if is_cursor_provider(self.provider_name) else ''
            unknown_response = zcode_unknown or cursor_code == 'CURSOR_RESPONSE_UNKNOWN'
            qoder_not_sent = self.provider_name == 'qoder_account' and getattr(exc, 'error_code', '') == 'QODER_BUDGET_EXHAUSTED'
            not_sent = zcode_not_sent or qoder_not_sent or cursor_code in {'CURSOR_RUN_BUDGET', 'CURSOR_NOT_DISPATCHED', 'CURSOR_WORD_PREVIOUS_UNKNOWN', 'PROVIDER_AUTH_REQUIRED'}
            if request_marker and (
                unknown_response
                or
                getattr(exc, "error_code", "") == "TIMEOUT"
                or "timed out" in exc_message.lower() or "timeout" in exc_message.lower()
            ):
                try:
                    self._append_request_event(
                        unknown_path, request_marker, request_ledger.RESPONSE_UNKNOWN,
                        error=exc_message[:160], **({'error_code':getattr(exc,'error_code','')} if unknown_response else {}),
                    )
                except OSError:
                    pass
            elif request_marker:
                # 响应明确没有到达（鉴权/配额/空响应/运行时错误）：请求进入明确终态，
                # 否则会一直被当成"可能被接受"，把正常的技术重试也一起锁死。
                self._append_request_event(
                    unknown_path, request_marker, request_ledger.RESPONSE_REJECTED,
                    error_code=str(getattr(exc, "error_code", "") or "") or None,
                    error=exc_message[:160],
                    resolution="not_dispatched" if not_sent else "definitive_failure",
                )
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
                "request_id": request_marker.get("request_id"),
                "request_fingerprint": request_marker.get("request_fingerprint"),
                "error_code": code,
                "error": message,
                "error_type": type(getattr(exc, "__cause__", None) or exc).__name__,
                "duration_seconds": duration,
                "invoked": not not_sent,
                **({'native_error_code':raw_code} if self.provider_name=='zcode_cn' else {}),
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

    def _planned_cus(self, sid: str) -> list[str]:
        return list(self.cus_by_section.get(sid) or [])

    def _restore_supplement_cus(self) -> None:
        """Resume the explicit supplement plan; a reservation is not completion."""
        section_ids = {str(row.get("section_id") or "")
                       for row in self.inputs["section_contracts"].get("contracts") or []}
        for sid in sorted(section_ids - {""}):
            unit = self.word_root / "sections" / sid
            for cu_id, record in load_registry(unit, sid).items():
                if record["status"] == "REJECTED":
                    continue
                if cu_id in self.cu_contracts:
                    if self.cu_contracts[cu_id] == record["contract"]:
                        continue
                    raise ValueError(f"supplement_base_contract_collision:{cu_id}")
                self.cu_contracts[cu_id] = copy.deepcopy(record["contract"])
                self.cus_by_section.setdefault(sid, []).append(cu_id)

    def _reserved_cu_ids(self, sid: str) -> list[str]:
        unit = self.word_root / "sections" / sid
        return sorted(set(self._planned_cus(sid)) | set(self._completed_cus(sid))
                      | set(load_registry(unit, sid)))

    def _completed_cus(self, sid: str) -> list[str]:
        cus_dir = self.word_root / "sections" / sid / "cus"
        if not cus_dir.is_dir():
            return []
        registry = load_registry(cus_dir.parent, sid)
        completed = []
        for path in cus_dir.glob("CU-*.json"):
            record = registry.get(path.stem)
            if record is not None:
                try:
                    row = read_json(path)
                except (OSError, ValueError):
                    continue
                if not isinstance(row, dict) or not owns_arrival(record, row, sid, path.stem):
                    continue
            completed.append(path.stem)
        return sorted(completed)

    def _missing_cus(self, sid: str) -> list[str]:
        completed = set(self._completed_cus(sid))
        return [cu for cu in self._planned_cus(sid) if cu not in completed]

    def _cu_contract_rows(self, sid: str, *, missing_only: bool = True) -> list[dict[str, Any]]:
        cu_ids = self._missing_cus(sid) if missing_only else self._planned_cus(sid)
        return [copy.deepcopy(self.cu_contracts[cu_id]) for cu_id in cu_ids if cu_id in self.cu_contracts]

    def _attach_cu_contracts(self, context: dict[str, Any], sid: str, content_unit_ids: list[str] | None = None) -> dict[str, Any]:
        context = copy.deepcopy(context)
        requested = content_unit_ids if content_unit_ids is not None else self._missing_cus(sid)
        missing = set(self._missing_cus(sid))
        rows = [copy.deepcopy(self.cu_contracts[cu]) for cu in requested if cu in missing and cu in self.cu_contracts]
        if not rows:
            return context
        rows = self._apply_midrun_budget_cap(sid, rows)
        context["content_unit_contracts"] = rows
        context["content_unit_execution"] = {
            "mode": "CONTENT_UNIT_GRANULAR",
            "planned_content_unit_ids": self._planned_cus(sid),
            "completed_content_unit_ids": self._completed_cus(sid),
            "remaining_content_unit_ids": [row["content_unit_id"] for row in rows],
            "output_contract": "Return one {content_unit_id, content} row for every remaining Content Unit.",
        }
        context["bidder_evidence_ledger"] = copy.deepcopy(self.inputs.get("bidder_evidence_ledger") or {})
        # P0-0 §5：商务合规矩阵只作内部台账。把 253 行矩阵塞进 Context Pack 会让
        # Provider 在技术正文里写出"归入商务响应矩阵简洁响应"这类越界说明段。
        gate = self.inputs.get("technical_scope_gate") or {}
        context["technical_scope_policy"] = {
            "document_scope": "TECHNICAL_ONLY",
            "excluded_scope_classes": [
                key for key, value in (gate.get("disposition_by_class") or {}).items() if value == "EXCLUDED"
            ],
            "forbidden_in_technical_body": [
                "商务标正文", "报价表与价格构成", "保证金与代理服务费条款",
                "营业执照/资质证书/信用与认证证明", "业绩与荣誉证明材料", "授权委托书等格式文件",
                "内部分类名称（如商务响应矩阵、商务偏离表）",
            ],
            "excluded_items_handling": "RECORD_ONLY_NO_SECTION_NO_UNIT_NO_BUDGET_NO_TABLE_NO_BLANK_PAGE",
        }
        return context

    def _persist_provider_cus(self, sid: str, fragment: dict[str, Any], expected_ids: list[str]) -> tuple[list[str], list[str]]:
        """Persist only CU fragments actually returned by the provider.

        A new hierarchical run is never allowed to manufacture CU completion from
        a section-level body.  Legacy fallback remains opt-in for migrations only.
        """
        unit = self.word_root / "sections" / sid
        cus_dir = unit / "cus"
        cus_dir.mkdir(parents=True, exist_ok=True)
        planned = set(self._planned_cus(sid))
        expected = set(expected_ids)
        returned: list[str] = []
        issues: list[str] = []
        seen: set[str] = set()
        response_rows = fragment.get("content_unit_fragments") or []
        reservations = load_registry(unit, sid)
        for row in response_rows:
            if not isinstance(row, dict):
                issues.append("non_object_content_unit")
                continue
            cu_id = str(row.get("content_unit_id") or "")
            content = str(row.get("content") or row.get("text") or "").strip()
            payload_row = row.get("payload")
            payload_text = visible_text(payload_row).strip() if isinstance(payload_row, (dict, list)) else ""
            if not cu_id or cu_id in seen:
                issues.append(f"invalid_or_duplicate_content_unit:{cu_id}")
                continue
            seen.add(cu_id)
            if cu_id not in planned or cu_id not in expected:
                issues.append(f"unrequested_content_unit:{cu_id}")
                continue
            if not content and not payload_text:
                # 结构化原型可以只有 payload（表格/清单/表单没有叙述摘要也是合法响应）；
                # 两者皆空才是真的空单元。
                issues.append(f"empty_content_unit:{cu_id}")
                continue
            def declared_ids(value: Any) -> list[str]:
                return list(dict.fromkeys(str(item) for item in value if isinstance(item, str))) if isinstance(value, list) else []
            single = len(response_rows) == 1
            usage_kind = "CU_RESPONSE" if isinstance(row.get("used_requirement_ids"), list) else (
                "SECTION_RESPONSE_SINGLE_CU" if single and isinstance(fragment.get("used_requirement_ids"), list)
                else "UNSPECIFIED")
            usage = declared_ids(row.get("used_requirement_ids")) if usage_kind == "CU_RESPONSE" else (
                declared_ids(fragment.get("used_requirement_ids")) if single else [])
            requested_contract = self.cu_contracts.get(cu_id) or {}
            provenance = {
                "used_requirement_ids": usage,
                "used_ku_ids": declared_ids(row.get("used_ku_ids")) if isinstance(row.get("used_ku_ids"), list) else (
                    declared_ids(fragment.get("used_ku_ids")) if single else []),
                "section_response_used_requirement_ids": declared_ids(fragment.get("used_requirement_ids")),
                "planned_requirement_ids": declared_ids(requested_contract.get("source_requirements")),
                "planned_scoring_item_ids": declared_ids(requested_contract.get("source_scoring_items")),
                "requirement_usage_kind": usage_kind,
            }
            if cu_id in reservations:
                provenance["supplement_registration_id"] = reservations[cu_id]["registration_id"]
            write_json(cus_dir / f"{cu_id}.json", {
                "content_unit_id": cu_id,
                "parent_section_id": sid,
                "content": content,
                "archetype": row.get("archetype") or (self.cu_contracts.get(cu_id) or {}).get("archetype") or "NARRATIVE",
                "payload": row.get("payload"),
                "effective_chars": len(content) or len(payload_text),
                "generation_origin": "provider_content_unit",
                "covered_by_section_assembly": False,
                "budget": judge_content_unit(
                    self.cu_contracts.get(cu_id) or {"content_unit_id": cu_id, "parent_section_id": sid},
                    content=row),
                "persisted_at": now_iso(),
                **provenance,
            })
            returned.append(cu_id)
        missing_return = [cu_id for cu_id in expected_ids if cu_id not in returned]
        issues.extend(f"missing_content_unit:{cu_id}" for cu_id in missing_return)
        self.returned_content_unit_ids.extend(returned)
        self._record_content_unit_budget(sid)
        return returned, issues

    def _record_content_unit_budget(self, sid: str) -> None:
        """把本 Section 到达 CU 的预算判定并进 content_unit_budget_ledger.json。

        §19：Provider 返回即记账。超上限只登记 OVER_BUDGET 与超出字数，不当场删字、
        不阻断本轮生成 —— 削减动作留给文档级门控（orchestrator 的 OVERSHOOT_REDUCTION）。
        """
        rows: list[dict[str, Any]] = []
        cus_dir = self.word_root / "sections" / sid / "cus"
        for path in sorted(cus_dir.glob("CU-*.json")) if cus_dir.is_dir() else []:
            payload = read_json(path)
            record = payload.get("budget")
            if isinstance(record, dict) and record.get("content_unit_id"):
                rows.append(record)
        if not rows:
            return
        path = self.run_root / BUDGET_LEDGER_FILE
        # 同一时刻可能有多个 Batch 回来：读-改-写必须串行，否则后写的线程会把前一笔
        # 账覆盖掉（Run D 的 run_audit.json 撞车是同一类故障）。
        with self.checkpoint_lock:
            write_ledger(path, merge_ledger(read_ledger(path), rows))
            self._sync_midrun_budget_tracking()

    def _sync_midrun_budget_tracking(self) -> dict[str, Any]:
        """§14：每批 CU 落账后重算四条余量，并当场决定下一批的每单元字数上限。

        整体重算而不是增量累加：账本按 `content_unit_id` 去重，是这一段的唯一事实源，
        所以 Resume、同一节重跑、补深新增的 CU 都不会让计数漂走。Round 3 是等 27 节
        全生成完才发现全部超长，这道账把发现点提前到第一批之后 —— 超写的那一节
        下一批就按 `next_batch_chars_per_unit_cap` 写，不再按原额度继续花。
        """
        midrun_path = self.run_root / MIDRUN_BUDGET_FILE
        if not (self.budget_midrun_plan or {}).get("sections"):
            tree = self.inputs.get("budget_tree") or {}
            persisted = read_json(midrun_path) if midrun_path.is_file() else {}
            self.budget_midrun_plan = empty_midrun_state(tree) if tree.get("volumes") else (
                persisted.get("plan") or {})
        plan = self.budget_midrun_plan or {}
        if not plan.get("sections"):
            return {}
        previous = read_json(midrun_path) if midrun_path.is_file() else {}
        # 批次计数是累计量，藏在 observed 里：从顶层取会永远读成 0，每批都从 1 重新开始
        prior_batches = ((previous.get("observed") or {}).get("batches_observed")) or 0
        records = list((read_ledger(self.run_root / BUDGET_LEDGER_FILE) or {}).get("records") or [])
        weights = {str(row.get("section_id") or ""): float(row.get("weight") or 0.0)
                   for row in (self.inputs["section_contracts"].get("contracts") or [])}
        # §64：治理账跟着 fragment 落盘，所以这里只读不猜 —— 一个节装配过就有账，
        # 还没装配的节没有账（`table_governance.status` 会如实写 NOT_OBSERVED）。
        governance: dict[str, Any] = {}
        mismatched_accounts = 0
        sections_root = self.word_root / "sections"
        # 循环变量不能叫 path：上面那个 path 是 midrun 账本的位置，一旦被 fragment 路径
        # 顶掉，末尾的 write_json 就会把 MID_RUN_BUDGET_TRACKING 写进最后一节的
        # fragment.json —— 那一节的正文从此在装配链上消失，而账面仍然全绿。
        for account_path in sorted(sections_root.glob("*/fragment.json")) if sections_root.is_dir() else []:
            account = read_json(account_path).get("table_governance")
            if not isinstance(account, dict):
                continue
            # **目录名**才是入账的键：预算账本、`plan.sections`、契约权重用的都是它。
            # 账里的 `section_id` 在真实装配链上读到过标题式 id（Round 4 合成 300 类
            # 22/22 节都回填不上，§64 整条链静默变成 NOT_OBSERVED），所以它只能当展示值，
            # 不一致必须计数，不能悄悄吞掉。
            sid = account_path.parent.name
            booked = str(account.get("section_id") or "")
            if booked and booked != sid:
                mismatched_accounts += 1
                account = {**account, "section_id": sid,
                           "accounted_under_section_id": booked}
            governance[sid] = account
        observed = observe_batch({**plan, "batches_observed": int(prior_batches)},
                                 records, weights=weights, table_governance=governance)
        self.budget_unit_caps = {
            str(row.get("section_id")): int(row.get("next_batch_chars_per_unit_cap") or 0)
            for row in observed.get("next_batch_adjustments") or [] if row.get("section_id")}
        write_json(midrun_path, {
            "schema_version": observed.get("schema_version"),
            "gate": "MID_RUN_BUDGET_TRACKING",
            "plan": plan,
            "observed": observed,
            "next_batch_chars_per_unit_cap": dict(self.budget_unit_caps),
            # §64：一眼看得见"表格治理回收了多少字、有没有真的回填进余量"。
            "table_governance": {**{key: value for key, value in
                                    (observed.get("table_governance") or {}).items()
                                    if key != "per_section"},
                                 "account_key_mismatch_sections": mismatched_accounts},
            "provider_calls_made": 0,
            "updated_at": now_iso(),
        })
        return observed

    def _apply_midrun_budget_cap(self, sid: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """§14/§15：本节已在超写 → 本批每个 CU 的统一额度就地降到上限。

        降额度走 `scale_budget`（几何跟着重算），不是把表格留着再砍正文：留下结构、
        压掉正文正好是 §12 要避免的同义重复形状。已经到达的正文一个字都不动。
        """
        cap = int(self.budget_unit_caps.get(str(sid)) or 0)
        if cap <= 0:
            return rows
        for row in rows:
            target = int((row.get("target_budget") or {}).get("effective_chars") or 0)
            if not target or target <= cap:
                continue
            archetype = str(row.get("archetype") or "NARRATIVE")
            rescaled = scale_budget(archetype, cap)
            row["budget_adjustment"] = {
                "reason": "MID_RUN_OVER_RUN_CAP",
                "planned_before_chars": target,
                "cap_chars": cap,
                "applied_at": now_iso(),
            }
            row["unified_budget"] = rescaled
            row["planned_words"] = int(rescaled.get("effective_chars") or cap)
            row["target_budget"] = contract_budget_row(archetype, rescaled)
            row["min_budget"] = contract_budget_row(
                archetype, scale_budget(archetype, max(1, int(round(cap * 0.8)))))
            row["max_budget"] = contract_budget_row(
                archetype, scale_budget(archetype, max(1, int(round(cap * 1.2)))))
        return rows

    def _assemble_section_from_cus(self, sid: str, fragment: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Assemble the Section body from the CU files that actually arrived.

        Missing CUs are reported but no longer veto the assembly: a 300-class
        run must still reach a downloadable first draft with the arrived
        content, and the gap is settled later as DEPTH_INCOMPLETE /
        TECHNICAL_FAILED instead of re-paying for the whole Section.
        """
        planned = self._planned_cus(sid)
        if not planned:
            return fragment, []
        cus_dir = self.word_root / "sections" / sid / "cus"
        registry = load_registry(cus_dir.parent, sid)
        rows: list[dict[str, Any]] = []
        missing: list[str] = []
        for cu_id in planned:
            path = cus_dir / f"{cu_id}.json"
            if not path.is_file():
                missing.append(cu_id)
                continue
            row = read_json(path)
            if cu_id in registry and not owns_arrival(registry[cu_id], row, sid, cu_id):
                missing.append(cu_id)
                continue
            if row.get("generation_origin") != "provider_content_unit":
                missing.append(cu_id)
                continue
            rows.append(row)
        issues = [f"missing_persisted_content_unit:{cu_id}" for cu_id in missing]
        if not rows:
            return fragment, issues
        # 装配口径与削减执行器共用 `longform/cu_assembly.py`：两条路各写一遍"CU 怎么变成
        # 节正文"，迟早出现"削了 CU 但交付件没变短"。
        return apply_content_units(rows, section_id=sid, planned_ids=planned,
                                   missing_ids=missing, base_fragment=fragment), issues

    def _section_status(self, sid: str) -> str:
        status_path = self.word_root / "sections" / sid / "status.json"
        if not status_path.is_file():
            return ""
        try:
            return str(read_json(status_path).get("status") or "")
        except (OSError, json.JSONDecodeError, AttributeError):
            # A process killed during atomic status write is recoverable; treat
            # the section as incomplete and let Resume repair it.
            return ""

    def _section_is_pass(self, sid: str) -> bool:
        return self._section_status(sid) in SECTION_PASS_STATES

    def _section_is_draftable(self, sid: str) -> bool:
        """初稿可用性判据：正文已到达即可，深度/质量缺口留给 Whole-Artifact QA 阶段。"""
        return self._section_status(sid) in SECTION_DRAFTABLE_STATES

    def _section_is_settled(self, sid: str) -> bool:
        """Resume 判据：正文已到达 **且** 计划内 CU 全部落盘，这节才不用再付钱。

        只看 `_section_is_draftable` 会把"已到达一半"的正文连同缺口一起锁成已完成：
        缺失的 CU 此后再没有机会被重新请求（Round 4 全量回归两条 resume 用例实测）。
        正文有没有 —— 那是 draftable 的口径，两者不能合成一把尺。
        """
        return self._section_is_draftable(sid) and not self._missing_cus(sid)

    def _section_is_missing_content(self, sid: str) -> bool:
        return self._section_status(sid) not in SECTION_DRAFTABLE_STATES

    def _batch_prompt(self, contexts: list[dict[str, Any]], repair: list[str] | None = None, *, batch_id: str | None = None, envelope_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        system = "你是资深物业服务方案撰稿人。一次调用生成本Batch内全部Logical Section。禁止工具、文件、调查。只返回JSON。"
        rules = [
            "不得新增目录或改变各Section目的",
            "必须恰好返回请求的section_id，不得增加、删除或重复",
            "每个Section独立成文，不得把多节压成一章",
            "正文不得出现REQ/SCR/KU等内部ID",
            "不得输出AI说明",
            "不得新增人员、频次、时限、设备或商业承诺",
            "投标人荣誉、认证、资质、案例、平台和团队事实必须引用已核验Bidder Evidence；无证据时只输出一处行内提示【待投标人确认：所需证明】，不得为它扩写整页空白履历表、待填写表格或证书复印件页",
            "本方案只写技术标正文：商务、报价、资格、资信、法务与投标程序条款不得展开，也不得为其生成说明段、清单或空白附件页（企业证书附件页、资质附件页、AAA附件页、ISO证书附件页、类似项目业绩附件页、人员履历附件页都属禁止项）",
            # 与 `_word_prompt` 同一条：不写的内容直接省略，不解释写法与取舍（原话不举，见彼处注释）。
            "正文只写服务内容本身：不得解释这份文档怎么写、某类材料为什么不写、内容如何分层与处置、哪些数字可以写进正文。凡决定不写的内容一律省略，不要留下一句说明理由的话；需要投标人或甲方补充的事实只按给定的行内提示格式登记",
            "结构化Archetype必须返回可渲染payload",
            FORM_PAYLOAD_SPEC,
        ]
        rules.extend(WORD_V12_WRITING_POLICY)
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
            "只交付本批指定的CU，按各CU的target_budget与必备内容完成；表格、步骤、清单与表单均计入额度。"
            "各节target_words由系统累计检查；本批不补写未请求的CU，不按整节额度重复扩写局部批次。",
        ]
        if current_batch_id:
            contract_lines.append(f"CURRENT_BATCH_ID = {current_batch_id}")
        if envelope_rows:
            system = system.replace("只返回JSON。", "只按CU信封输出文本块，不要JSON、不要代码围栏。")
            rules.append("只输出信封块，每个Content Unit一块；Section正文由系统按CU归属装配")
            rules = [row for row in rules if not row.startswith("必须恰好返回请求的section_id")]
            contract_lines.extend(envelope_contract_lines(envelope_rows))
        elif current_batch_id:
            contract_lines.extend([
                "Output Contract:",
                f"- batch_id必须等于CURRENT_BATCH_ID（{current_batch_id}），不得省略、留空或返回其他值。",
                "- 返回JSON：{\"batch_id\":string,\"sections\":[{section_id,title,body_blocks,content_unit_fragments:[{content_unit_id,parent_section_id,archetype,content,payload}],tables,processes,callouts,cross_references,claims,used_requirement_ids,used_ku_ids,generation_notes}]}。",
            ])
            rules.append(f"batch_id必须等于CURRENT_BATCH_ID = {current_batch_id}")
        else:
            contract_lines.append(
                "返回JSON：{\"batch_id\":string,\"sections\":[{section_id,title,body_blocks,content_unit_fragments:[{content_unit_id,parent_section_id,archetype,content,payload}],tables,processes,callouts,cross_references,claims,used_requirement_ids,used_ku_ids,generation_notes}]}。"
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
        # 同一个 Section 可能同时出现在多个并行 Batch（CU 拆分批次），所以 attempt 目录必须在锁内
        # 命名并占位；否则两个线程会写同一个 provider_attempt_* 目录，留下半写的证据文件。
        with self.checkpoint_lock:
            existing = []
            for path in root.glob("provider_attempt_*"):
                try:
                    existing.append(int(path.name.rsplit("_", 1)[1]))
                except ValueError:
                    continue
            attempt = max(existing, default=0)
            while True:
                attempt += 1
                candidate = root / f"provider_attempt_{attempt}"
                try:
                    candidate.mkdir(parents=True)
                    return candidate, attempt
                except FileExistsError:
                    continue

    def _batch_dir(self, batch_id: str, section_ids: list[str]) -> Path:
        """批次原始响应的落点：`word/batches/<首个 Section id>-<batch_id>/`。

        `batch_id` 只在**本卷的批次计划**里唯一（GB-01…GB-13），而 `word/batches/` 是整个
        Run 共享的：按裸 id 建目录，后一卷的同名批次就把前一卷的 `provider_raw.json`、
        `envelope_recovery.json` 盖掉（300 类合成链实测：7 卷 91 次批次在盘上只剩 13 个
        目录，V05 的 S05-01/GB-02 那份原始响应被 V07 的 S09-01 同名批次覆盖）。
        Section id 全 Run 唯一，拿首个 Section 作前缀即可区分，不需要新造一套 id。
        """
        prefix = str(section_ids[0]) if section_ids else "NOSECTION"
        return self.word_root / "batches" / f"{prefix}-{batch_id}"

    def _section_attempt_lock(self, sid: str) -> threading.Lock:
        with self.checkpoint_lock:
            locks = self.__dict__.setdefault("section_attempt_locks", {})
            return locks.setdefault(str(sid), threading.Lock())

    def _merge_section_attempts(self, sid: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把本批次的 attempt 记录并进 `sections/<sid>/audit.json`，返回合并后的全量。

        调用方拿到的 `rows` 是"发请求之前读到的旧账 + 自己这一条"，而 CU 拆批会让同一个
        Section 的两个批次在不同 worker 上同时跑完：两侧各自整份覆盖，后写的那份就把先写
        的那条 attempt 抹掉了（Round 4 合成链里 GB-02 那条就是这么丢的，只留下请求账上
        一条 `RESPONSE_RECEIVED`，看起来像凭空丢字）。所以读—并—写要放在同一把节锁里，
        并按记录身份去重，不是按"谁最后写"。
        """
        path = self.word_root / "sections" / sid / "audit.json"
        with self._section_attempt_lock(sid):
            disk = read_json(path).get("attempts") if path.is_file() else []
            merged = list(disk or [])
            seen = {self._attempt_key(row) for row in merged}
            for row in rows or []:
                key = self._attempt_key(row)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(row)
            write_json(path, {"attempts": merged})
            return merged

    @staticmethod
    def _attempt_key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
        return (str(row.get("request_id") or ""), str(row.get("attempt_id") or ""),
                str(row.get("mode") or ""), str(row.get("batch_id") or ""),
                str(row.get("status") or ""))

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
            cu_body_ok = any(
                isinstance(item, dict) and str(item.get("content") or item.get("text") or "").strip()
                for item in (fragment.get("content_unit_fragments") or [])
            )
            # In the CU execution contract the provider may legitimately return
            # CU-level content and leave body_blocks empty.  The system assembles
            # the authoritative Section body after validating/persisting every CU.
            if not body_ok and not cu_body_ok:
                issues.append(f"empty_body:{sid}")
        for sid in ambiguous:
            fragments.pop(sid, None)
        for sid in requested:
            if sid not in fragments:
                issues.append(f"missing:{sid}")
        return fragments, issues

    def _envelope_fragments(self, parsed: dict[str, Any], section_ids: list[str], expected_cus: dict[str, list[str]], by_id: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
        """Map partially recovered CU envelope blocks back to their Sections.

        A truncated or payload-incomplete block is never persisted: it stays a
        missing Content Unit, so the next request re-asks for that single unit
        instead of rewriting the whole Section.
        """
        if parsed.get("protocol") != "cu_envelope":
            return {}, ["no_envelope_markers"]
        blocks = parsed.get("blocks") or {}
        arrived = {str(cu_id): row for cu_id, row in blocks.items() if str(row.get("status") or "") == "ARRIVED"}
        issues: list[str] = []
        fragments: dict[str, dict[str, Any]] = {}
        for cu_id in blocks:
            if cu_id not in set(self.cu_contracts):
                issues.append(f"unrequested_content_unit:{cu_id}")
        for sid in section_ids:
            contract = by_id[sid]
            rows: list[dict[str, Any]] = []
            for cu_id in expected_cus.get(sid) or []:
                block = arrived.get(cu_id)
                if block is None:
                    status = str((blocks.get(cu_id) or {}).get("status") or "MISSING")
                    if status == CU_TRUNCATED:
                        self.envelope_truncated_cus += 1
                        issues.append(f"cu_truncated:{sid}:{cu_id}")
                    elif status == CU_PAYLOAD_INCOMPLETE:
                        self.envelope_truncated_cus += 1
                        issues.append(f"cu_payload_incomplete:{sid}:{cu_id}")
                    else:
                        issues.append(f"cu_missing:{sid}:{cu_id}")
                    continue
                rows.append({
                    "content_unit_id": cu_id,
                    "parent_section_id": sid,
                    "content": block.get("content") or "",
                    "archetype": block.get("archetype") or (self.cu_contracts.get(cu_id) or {}).get("archetype") or "NARRATIVE",
                    "payload": block.get("payload"),
                })
            fragment = {
                "section_id": sid,
                "title": contract.get("section_title") or contract.get("title") or "",
                "body_blocks": [],
                "content_unit_fragments": rows,
                "used_requirement_ids": list(contract.get("source_requirements") or []),
                "generation_notes": [],
                "transport": {"protocol": "cu_envelope", "raw_length": parsed.get("raw_length")},
            }
            fragment, _report = normalize_fragment(fragment, section_id=sid, contract=contract)
            if not rows:
                issues.append(f"empty_body:{sid}")
            fragments[sid] = fragment
        return fragments, issues

    def _commit_section(self, job: LongformGenerationJob, contract: dict[str, Any], fragment: dict[str, Any], gate: dict[str, Any], attempts: list[dict[str, Any]], continuation_used: bool, *, provisional: str | None = None) -> str:
        sid = contract["section_id"]
        unit = self.word_root / "sections" / sid
        summary = self._word_summary(fragment, contract)
        under = gate.get("length_status") == "SECTION_UNDER_LENGTH"
        if provisional == SECTION_DEPTH_INCOMPLETE:
            status = SECTION_DEPTH_INCOMPLETE
        elif provisional:
            status = SECTION_PROVISIONAL
        else:
            status = "COMPLETED_CONDITIONAL" if contract.get("section_activation_condition", {}).get("expression") != "always" else ("COMPLETED_WITH_WARNING" if gate["status"] == "WARNING" or under else "COMPLETED")
        recorded = dict(gate)
        if provisional:
            recorded["provisional"] = {
                "reason": provisional,
                "content_units": int(gate.get("content_units") or 0),
                "depth_deficit": depth_deficit_class(int(gate.get("content_units") or 0), contract),
                "depth_ratio": round(depth_ratio(int(gate.get("content_units") or 0), contract), 4),
            }
        write_json(unit / "fragment.json", fragment)
        write_json(unit / "summary.json", summary)
        write_json(unit / "governance.json", recorded)
        write_json(unit / "qa.json", recorded)
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
        # Provider CU fragments are persisted before this commit.  The old
        # section-body fallback is permitted only for an explicit migration run.
        planned_cus = self._planned_cus(sid)
        if planned_cus and self.allow_legacy_cu_fallback:
            cus_dir = unit / "cus"
            cus_dir.mkdir(parents=True, exist_ok=True)
            for cu_id in planned_cus:
                cu_file = cus_dir / f"{cu_id}.json"
                if not cu_file.is_file():
                    write_json(cu_file, {
                        "content_unit_id": cu_id, "parent_section_id": sid,
                        "content": visible_text(fragment)[:2000], "effective_chars": len(visible_text(fragment)),
                        "generation_origin": "legacy_section_fallback",
                        "covered_by_section_assembly": True,
                    })
        write_json(unit / "status.json", {"section_id": sid, "status": status, "attempts": attempts, "content_units": gate["content_units"], "continuation": continuation_used, "provisional_reason": provisional, "updated_at": now_iso()})
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

    def generate_word(self, section_ids: list[str] | None = None, *, volume_continuation: bool = False) -> dict[str, Any]:
        contracts = sorted(self.inputs["section_contracts"]["contracts"], key=lambda row: row["generation_order"])
        if section_ids is not None:
            contracts = [row for row in contracts if row["section_id"] in set(section_ids)]
        job = self._job(self.word_root, "WORD", len(contracts))
        job.speed_profile = self.speed_profile
        selected_ids = [row["section_id"] for row in contracts]
        self.active_word_section_ids = list(dict.fromkeys(self.active_word_section_ids + selected_ids))
        job.total_units = len(self.active_word_section_ids)
        prerequisites, _advisory = build_prereq_maps(selected_ids, self.inputs["dependency_map"])
        by_id = {row["section_id"]: row for row in contracts}
        wall_started = time.monotonic()
        self._restore_call_accounting(job, volume_continuation=volume_continuation)
        for contract in contracts:
            sid = contract["section_id"]
            unit = self.word_root / "sections" / sid
            status_path = unit / "status.json"
            # 有正文 ≠ 已齐：CU 还有未落盘的节必须重新打开（`_section_is_settled`）。
            # 重开只付差额：`_run_batch` 与 `_attach_cu_contracts` 下发前都按 `_missing_cus` 过滤。
            if self._section_is_settled(sid):
                if sid not in job.completed_units:
                    job.completed_units.append(sid)
                self._restore_word_state(contract, unit)
                continue
            current_status = read_json(status_path).get("status") if status_path.is_file() else ""
            if current_status not in SECTION_DRAFTABLE_STATES and (unit / "provider_raw.json").is_file():
                raw = read_json(unit / "provider_raw.json")
                if raw.get("sections"):
                    raw = next((row for row in raw["sections"] if row.get("section_id") == sid), raw)
                candidate = _repair_word_artifact(raw, getattr(self, "editorial_source_facts", None))
                cu_recovery_issues: list[str] = []
                if self._planned_cus(sid):
                    candidate, cu_recovery_issues = self._assemble_section_from_cus(sid, candidate)
                if candidate is None:
                    continue
                processes = [self.builder.processes[pid] for pid in contract.get("required_processes", []) if pid in self.builder.processes]
                recovered_gate = evaluate_word_fragment(candidate, contract, self.inputs["global_state"], processes)
                recovered_decision = classify_section_issue(recovered_gate, contract, None, fragment=candidate, speed_profile=self.speed_profile)
                recovered_short = self.require_section_min and recovered_decision.get("severity") in {"CRITICAL_UNDER_LENGTH", "MATERIAL_UNDER_LENGTH"}
                if not recovered_gate.get("meaningful_body"):
                    continue
                if cu_recovery_issues or recovered_gate["status"] == "BLOCK" or recovered_short:
                    # 上一次进程已经写出可用正文：直接以 PROVISIONAL 收尾，不再重新付费。
                    self._commit_section(job, contract, candidate, recovered_gate, [{"attempt_id": 0, "mode": "local_revalidation"}], False, provisional=SECTION_PROVISIONAL)
                    with self.checkpoint_lock:
                        if sid not in self.provisional_sections:
                            self.provisional_sections.append(sid)
                    continue
                self._commit_section(job, contract, candidate, recovered_gate, [{"attempt_id": 0, "mode": "local_revalidation"}], False)
        provider_config = getattr(self.provider, "config", {}) if self.provider is not None else {}
        planner_contracts = []
        for contract in contracts:
            enriched = dict(contract)
            enriched["content_unit_contracts"] = [copy.deepcopy(self.cu_contracts[cu]) for cu in self._planned_cus(contract["section_id"]) if cu in self.cu_contracts]
            planner_contracts.append(enriched)
        plan = plan_generation_batches(
            planner_contracts,
            self.inputs["dependency_map"],
            provider_name=self.provider_name,
            speed_profile=self.speed_profile,
            max_parallelism=self.max_parallelism,
            provider_config=provider_config if isinstance(provider_config, dict) else {},
        )
        write_json(self.run_root / "generation_batches.json", plan)
        write_json(self.word_root / "generation_batches.json", plan)
        # 离线性能画像与批次计划同源：谁想知道"这轮计划花多少次调用、被哪几道闸限住"，
        # 读这一份，不要去反推 generation_batches.json。分卷时每卷重写，口径同批次计划。
        self.performance_profile = profile_batch_plan(
            plan, scope_note="按本轮选中的 Section 生成；分卷/续跑时每卷重写一次。")
        write_json(self.run_root / "performance_plan.json", self.performance_profile)
        write_json(self.word_root / "performance_plan.json", self.performance_profile)
        self.batch_plan = plan
        job.pending_batches = [row["batch_id"] for row in plan["batches"]]
        self.performance["logical_section_count"] = len(contracts)
        self.performance["generation_batch_count"] = len(plan["batches"])
        self.repair_budget = FastRepairBudget(speed_profile=self.speed_profile, initial_batch_count=len(plan["batches"]))
        self.technical_retry_budget = technical_retry_allowance(len(plan["batches"]))
        # 全局修复预算按"本轮实际的首轮生成调用数"推导，跨分卷累计：修复永远只是
        # 一小部分，不能像旧的逐节计数器那样在300页任务上变成无上限的重跑。
        if self.global_repair_budget is None:
            self.global_repair_budget = GlobalRepairBudget(initial_calls=0)
        self.global_repair_budget.initial_calls = int(self.global_repair_budget.initial_calls or 0) + len(plan["batches"])
        self.global_repair_budget.max_calls = GlobalRepairBudget.derive(
            self.global_repair_budget.initial_calls,
            ratio=self.global_repair_budget.ratio,
            ceiling=self.global_repair_budget.ceiling,
        )
        # 补深单独计量：它花的是"新增内容"的预算，不能因为修复预算先耗尽就整轮归零。
        self.global_repair_budget.supplement_max_calls = GlobalRepairBudget.derive_supplement(
            self.global_repair_budget.initial_calls,
            ratio=self.global_repair_budget.supplement_ratio,
            ceiling=self.global_repair_budget.supplement_ceiling,
        )
        self.run_started_monotonic = time.monotonic()
        self.max_parallelism = int(plan.get("max_parallelism") or self.max_parallelism)
        job.parallelism = self.max_parallelism
        self._normalize_batch_state(job, plan["batches"])
        self._checkpoint(job)

        def _execute(batch: dict[str, Any]) -> dict[str, Any]:
            # 与调度器的跳过判据同一把尺：正文在但 CU 还缺的节仍要跑（只补差集）。
            remaining = [sid for sid in batch["section_ids"] if not self._section_is_settled(sid)]
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
            partial_sections = set(result.get("partial_sections") or [])
            failed = [sid for sid in runnable if sid not in partial_sections and not self._section_is_draftable(sid)]
            classes = dict(result.get("section_failure_classes") or {})
            admission_codes = {"RATE_LIMIT_OVERSIZED", "RATE_LIMIT_TIMEOUT", "RATE_LIMIT_INVALID_LIMIT", "ADMISSION_REJECTED"}
            if failed and result.get("error_code") not in {"RATE_LIMIT", *admission_codes} and result.get("status") != "ADMISSION_REJECTED":
                self._settle_generation_gaps(job, batch, failed, classes, by_id)
            failed_after = [sid for sid in runnable if sid not in partial_sections and not self._section_is_draftable(sid)]
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
        scheduled = scheduler.run(plan["batches"], _execute, is_section_complete=self._section_is_draftable,
                                  is_section_settled=self._section_is_settled)
        # 并发治理必须留下"为什么是 1"的第一方证据。上一轮只有 MAX_OBSERVED_CONCURRENCY=3
        # 这一句自报，实测跨节并发是 1，报告无从对质；这里按分卷记一条完整台账。
        with self.checkpoint_lock:
            scope_ids = [str(row.get("section_id") or "") for row in contracts if row.get("section_id")]
            volume_ledger = {
                "scope_section_ids": scope_ids,
                "scope_section_count": len(scope_ids),
                "batch_count": len(plan["batches"]),
                "initial_parallelism": int(scheduled.get("initial_parallelism") or scheduler.max_parallelism),
                "final_parallelism": int(scheduler.current_parallelism),
                "max_observed_concurrency": int(scheduler.max_observed_concurrency),
                "max_observed_distinct_sections": int(scheduler.max_observed_distinct_sections),
                "avg_observed_concurrency": scheduler.avg_observed_concurrency,
                "soft_latency_events": int(scheduler.soft_latency_events),
                "soft_latency_recoveries": int(scheduler.soft_latency_recoveries),
                "rate_limit_events": int(scheduler.rate_limit_events),
                "soft_latency_budget_seconds": float(
                    (self.capability.get("effective_settings") or {}).get("soft_latency_budget") or 0),
                "events": list(scheduler.concurrency_events),
            }
            self.concurrency_ledger.append(volume_ledger)
            # 第一方并发台账到位后刷新画像：planned_* 与 observed_* 从此在同一份文件里对质，
            # "规划并发 3、实测跨节并发 1"这类差距不再只能靠回忆争论。
            self.performance_profile = profile_batch_plan(
                plan,
                concurrency_ledger=[volume_ledger],
                scope_note="本卷画像；并发观测只覆盖本卷台账，分卷/续跑时每卷重写一次。")
        write_json(self.run_root / "performance_plan.json", self.performance_profile)
        write_json(self.word_root / "performance_plan.json", self.performance_profile)
        self._normalize_batch_state(job, plan["batches"], scheduled)
        # §6：Final Assembly 之前必须做一次晚到响应清扫。清扫失败不得阻断交付，
        # 但必须留下可审计的失败原因，而不是静默把缺的 Section 带进成品。
        try:
            self.reconcile_pending_provider_requests(job=job, allow_retry=True)
        except Exception as exc:  # noqa: BLE001
            self.reconciliation = {
                "status": "RECONCILIATION_FAILED",
                "error": f"{type(exc).__name__}: {str(exc)[:180]}",
                "requests_open": -1,
            }
        self.max_parallelism = int(scheduled.get("parallelism") or self.max_parallelism)
        job.finished_at = now_iso()
        job.current_unit = None
        wall = round(time.monotonic() - wall_started, 3)
        job.status = "COMPLETED" if len(job.completed_units) == job.total_units and not job.failed_units else ("DEFERRED_EXTERNAL" if job.deferred_external_events and not job.failed_units else "COMPLETED_WITH_GAPS")
        # 全部Section都已"到达正文"时不得显示 TERMINAL_BLOCKED：PROVISIONAL 是待复核缺口，不是技术阻塞。
        all_arrived = all(self._section_is_draftable(sid) for sid in self.active_word_section_ids) if self.active_word_section_ids else False
        job.public_phase = "SECTION_QA" if (job.status == "COMPLETED" or all_arrived) else "TERMINAL_BLOCKED"
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

    def _run_batch(self, job: LongformGenerationJob, batch: dict[str, Any], by_id: dict[str, dict[str, Any]], *, stage: str, failure_class: str | None = None, allow_unknown_resend: bool = False) -> dict[str, Any]:
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
            requested_ids = [cu for cu in list((batch.get("content_unit_ids_by_section") or {}).get(sid) or []) if cu in set(self._missing_cus(sid))]
            if not requested_ids and len(section_ids) == 1:
                requested_ids = [cu for cu in list(batch.get("content_unit_ids") or []) if cu in set(self._missing_cus(sid))]
            context = self._attach_cu_contracts(context, sid, requested_ids if requested_ids else None)
            write_json(self.word_root / "sections" / sid / "input.json", context)
            contexts.append(context)
        expected_cus = {
            sid: [row["content_unit_id"] for row in contexts[index].get("content_unit_contracts") or []]
            for index, sid in enumerate(section_ids)
        }
        envelope_rows = [row for context in contexts for row in (context.get("content_unit_contracts") or [])]
        # 信封传输只接管"文本为主 + 每个Section都在等CU"的批次；结构化批次仍走严格JSON。
        use_envelope = bool(
            self.envelope_transport
            and envelope_rows
            and all(expected_cus.get(sid) for sid in section_ids)
            and should_use_envelope([str(row.get("archetype") or "") for row in envelope_rows])
        )
        batch_dir = self._batch_dir(str(batch.get("batch_id") or ""), section_ids)
        if len(section_ids) == 1:
            prompt = self._word_prompt(contexts[0], envelope_rows=envelope_rows if use_envelope else None)
            request = {
                "task_id": f"{job.job_id}-{section_ids[0]}-{batch['batch_id']}",
                "system_prompt": prompt["system"],
                "prompt": prompt["prompt"],
                "required_keys": sorted(WORD_REQUIRED_KEYS),
                "agent_max_turns": 1,
                "section_id": section_ids[0],
                "attempt_mode": "generate",
                "generation_mode": "longform_section",
                "content_unit_contracts": contexts[0].get("content_unit_contracts") or [],
                "content_unit_execution": contexts[0].get("content_unit_execution") or {},
            }
            if use_envelope:
                # required_keys 保留：Provider 若忽略信封协议仍返回 JSON，就按 JSON 契约解析。
                request["response_protocol"] = "cu_envelope"
                request["envelope_content_unit_ids"] = [row["content_unit_id"] for row in envelope_rows]
            unit_dir = self.word_root / "sections" / section_ids[0]
            existing = read_json(unit_dir / "audit.json") if (unit_dir / "audit.json").is_file() else {"attempts": []}
            attempt_dir, attempt = self._next_attempt(unit_dir)
            output, audit = self._invoke(request, attempt_dir, stage=stage, failure_class=failure_class, batch_size=1, allow_unknown_resend=allow_unknown_resend)
            attempts_map = {section_ids[0]: list(existing.get("attempts", [])) + [{"attempt_id": attempt, "mode": "generate", "batch_id": batch["batch_id"], **audit}]}
        else:
            prompt = self._batch_prompt(contexts, batch_id=str(batch.get("batch_id") or ""), envelope_rows=envelope_rows if use_envelope else None)
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
                "content_unit_contracts": [row for context in contexts for row in (context.get("content_unit_contracts") or [])],
                "content_unit_execution": {
                    "mode": "CONTENT_UNIT_GRANULAR",
                    "sections": {
                        sid: contexts[index].get("content_unit_execution") or {}
                        for index, sid in enumerate(section_ids)
                    },
                },
            }
            if use_envelope:
                request["response_protocol"] = "cu_envelope"
                request["envelope_content_unit_ids"] = [row["content_unit_id"] for row in envelope_rows]
            attempt_dir, attempt = self._next_attempt(batch_dir)
            output, audit = self._invoke(request, attempt_dir, stage=stage, failure_class=failure_class, batch_size=len(section_ids), allow_unknown_resend=allow_unknown_resend)
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
                attempts_map[sid] = self._merge_section_attempts(sid, attempts_map[sid])
                status = attempts_map[sid][-1]["status"] if str(attempts_map[sid][-1].get("status", "")).startswith("DEFERRED_") else "FAILED_SECTION"
                self._fail_section(job, sid, attempts_map[sid], status)
            failure_class = classify_generation_failure(None, audit)
            return {"status": "FAILED_BATCH", "error_code": audit.get("error_code"), "section_ids": section_ids, "failure_class": failure_class}
        cleaned = {key: value for key, value in output.items() if key != "provider_metadata"}
        write_json(batch_dir / "provider_raw.json", cleaned)
        expected_batch_id = batch.get("batch_id") if len(section_ids) > 1 else None
        if ENVELOPE_TEXT_KEY in cleaned:
            with self.checkpoint_lock:
                self.envelope_calls += 1
            parsed = parse_cu_envelope(
                cleaned[ENVELOPE_TEXT_KEY],
                requested_ids=[str(row.get("content_unit_id") or "") for row in envelope_rows],
                archetypes={str(row.get("content_unit_id") or ""): str(row.get("archetype") or "") for row in envelope_rows},
            )
            fragments, issues = self._envelope_fragments(parsed, section_ids, expected_cus, by_id)
            write_json(batch_dir / "envelope_recovery.json", {
                "protocol": parsed.get("protocol"),
                "arrived_content_unit_ids": sorted(parsed.get("complete_ids") or []),
                "truncated_content_unit_ids": sorted(parsed.get("truncated_ids") or []),
                "payload_incomplete_content_unit_ids": sorted(parsed.get("payload_incomplete_ids") or []),
                "missing_content_unit_ids": sorted(parsed.get("missing_ids") or []),
                "unrequested_content_unit_ids": sorted(parsed.get("unrequested_ids") or []),
                "issues": parsed.get("issues") or [],
                "raw_length": parsed.get("raw_length"),
            })
            # 一个块都没到达才是契约级失败；部分到达由差集重试收敛。
            contract_error = not (parsed.get("blocks") or {})
        else:
            fragments, issues = self._split_batch_output(cleaned, section_ids, expected_batch_id=expected_batch_id)
            contract_error = any(item.startswith(("unrequested:", "duplicate:", "batch_id_mismatch:", "batch_id_missing", "missing_sections_array")) for item in issues)
        accepted = 0
        provisional_committed: list[str] = []
        failed = []
        partial_sections: list[str] = []
        persisted_cus: list[str] = []
        section_failure_classes: dict[str, str] = {}
        # 内容先落盘、请求账先结清，再动 attempt/governance 这些台账文件。Round 4 合成链
        # 实测：一次 Windows `os.replace` 撞车抛在下面的 audit 写上，那一批 5 个已经付费的
        # Content Unit 就此停在 `RESPONSE_RECEIVED`，清扫把这一节判成 partially_persisted
        # 跳过，正文再也回不来。落盘与结账不能依赖后面任何一次写盘成功。
        persisted_by_section: dict[str, list[str]] = {}
        cu_issues_by_section: dict[str, list[str]] = {}
        for sid in section_ids:
            if contract_error or f"empty_body:{sid}" in issues:
                continue
            arrived = fragments.get(sid)
            if arrived is None:
                continue
            returned, cu_issues = self._persist_provider_cus(sid, arrived, expected_cus.get(sid) or [])
            persisted_by_section[sid] = [str(cu) for cu in returned]
            cu_issues_by_section[sid] = list(cu_issues)
            persisted_cus.extend(persisted_by_section[sid])
        # 落笔即结账：请求账的关闭只依赖"这一批 CU 已经在盘上"，不依赖后面任何一次台账写盘。
        self._settle_request(
            audit,
            resolution="late_response" if audit.get("late_response_recovered") else "content_persisted",
            persisted_content_unit_ids=persisted_cus,
        )
        for sid in section_ids:
            unit = self.word_root / "sections" / sid
            contract = by_id[sid]
            empty_body = f"empty_body:{sid}" in issues
            fragment = None if contract_error or empty_body else fragments.get(sid)
            attempts_map[sid] = self._merge_section_attempts(sid, attempts_map[sid])
            if fragment is None:
                failed.append(sid)
                kind = "OUTPUT_CONTRACT_ERROR" if contract_error or any(item == f"duplicate:{sid}" or item == f"missing:{sid}" for item in issues) else "QUALITY_FAILURE"
                if empty_body:
                    kind = "EMPTY_BODY"
                section_failure_classes[sid] = kind
                write_json(unit / "governance.json", {"status": "BLOCK", "issues": [item for item in issues if sid in item or item.startswith("batch_id") or item == "missing_sections_array"]})
                continue
            cu_returned = persisted_by_section.get(sid) or []
            cu_issues = cu_issues_by_section.get(sid) or []
            # 未到达的CU只是缺口，不是整节契约错误：已经落盘的CU不再重新付费。
            outstanding_cu_issues = [item for item in cu_issues if item.startswith("missing_content_unit:")]
            hard_cu_issues = [item for item in cu_issues if not item.startswith("missing_content_unit:")]
            cu_gap: list[str] = []
            if expected_cus.get(sid):
                # A capacity-split batch is intentionally incomplete until all
                # planned CUs for the logical Section arrive.  Persist now and
                # defer Section QA/UNDER_LENGTH until final assembly.
                remaining_after_batch = self._missing_cus(sid)
                if remaining_after_batch and not hard_cu_issues:
                    write_json(self.word_root / "sections" / sid / "partial_generation.json", {
                        "status": "PARTIAL_CU_BATCH",
                        "completed_content_unit_ids": self._completed_cus(sid),
                        "remaining_content_unit_ids": remaining_after_batch,
                        "updated_at": now_iso(),
                    })
                    if self._more_cu_batches_pending(batch, sid):
                        partial_sections.append(sid)
                        continue
                    issues.extend(f"{sid}:{item}" for item in outstanding_cu_issues)
                    failed.append(sid)
                    section_failure_classes[sid] = "OUTPUT_TRUNCATED"
                    write_json(unit / "governance.json", {
                        "status": "GAP",
                        "issues": [f"content_unit_gap:{cu}" for cu in remaining_after_batch],
                        "returned_content_unit_ids": self._completed_cus(sid),
                        "expected_content_unit_ids": self._planned_cus(sid),
                    })
                    continue
                issues.extend(f"{sid}:{item}" for item in hard_cu_issues)
                fragment, assembly_issues = self._assemble_section_from_cus(sid, fragment)
                issues.extend(f"{sid}:{item}" for item in assembly_issues)
                if hard_cu_issues:
                    failed.append(sid)
                    section_failure_classes[sid] = "CONTENT_UNIT_CONTRACT_ERROR"
                    write_json(unit / "governance.json", {
                        "status": "BLOCK",
                        "issues": hard_cu_issues,
                        "returned_content_unit_ids": cu_returned,
                        "expected_content_unit_ids": expected_cus.get(sid) or [],
                    })
                    continue
                cu_gap = [item.rsplit(":", 1)[-1] for item in assembly_issues]
            write_json(unit / "provider_raw.json", fragment)
            fragment = _repair_word_artifact(fragment, getattr(self, "editorial_source_facts", None))
            processes = [self.builder.processes[pid] for pid in contract.get("required_processes", []) if pid in self.builder.processes]
            gate = evaluate_word_fragment(fragment, contract, self.inputs["global_state"], processes)
            write_json(unit / "governance.json", gate)
            write_json(unit / "qa.json", gate)
            decision = classify_section_issue(gate, contract, None, fragment=fragment, speed_profile=self.speed_profile)
            short = self.require_section_min and decision.get("severity") in {"CRITICAL_UNDER_LENGTH", "MATERIAL_UNDER_LENGTH"}
            if not gate.get("meaningful_body"):
                failed.append(sid)
                section_failure_classes[sid] = "EMPTY_BODY"
                continue
            if cu_gap:
                # 计划内CU未全部到达：交给有界技术重试只补差集，重试耗尽后由
                # settle 阶段以 PROVISIONAL 保留已有正文。
                failed.append(sid)
                section_failure_classes[sid] = "OUTPUT_TRUNCATED"
                continue
            if gate["status"] == "BLOCK" or short:
                # Generation 阶段不再重跑整节：深度与质量缺口记录后继续推进，
                # 由 Whole-Artifact QA 在统一修复预算内选择性修复。
                deficit = depth_deficit_class(int(gate.get("content_units") or 0), contract)
                reason = (
                    SECTION_DEPTH_INCOMPLETE
                    if (short or deficit != "NONE" or gate.get("length_status") == "SECTION_UNDER_LENGTH")
                    else SECTION_PROVISIONAL
                )
                self._commit_section(job, contract, fragment, gate, attempts_map[sid], False, provisional=reason)
                with self.checkpoint_lock:
                    if sid not in self.provisional_sections:
                        self.provisional_sections.append(sid)
                    self.depth_gap_sections.append({
                        "section_id": sid,
                        "reason": reason,
                        "deficit": deficit,
                        "content_units": int(gate.get("content_units") or 0),
                        "target_min": int((contract.get("target_words") or {}).get("min") or 0),
                        "gate_issues": [key for key, value in (gate.get("must_cover") or {}).items() if value == "MISSING"],
                        "blocking": gate["status"] == "BLOCK",
                    })
                provisional_committed.append(sid)
                accepted += 1
                continue
            self._commit_section(job, contract, fragment, gate, attempts_map[sid], False)
            accepted += 1
        # 请求账不在这里关：见上面"落笔即结账"那一段 —— 后面的 attempt/governance/commit
        # 任何一次写盘失败，都不该把已经落盘的内容留在账外。
        return {
            "status": "SUCCESS" if not failed else "FAILED_BATCH",
            "section_ids": section_ids,
            "accepted": accepted,
            "failed": failed,
            "issues": issues,
            "transport": "cu_envelope" if ENVELOPE_TEXT_KEY in cleaned else "json",
            "provisional_sections": provisional_committed,
            "failure_class": next(iter(section_failure_classes.values()), None),
            "section_failure_classes": section_failure_classes,
            "partial_sections": partial_sections,
            "provider_calls": 1,
        }

    def _more_cu_batches_pending(self, batch: dict[str, Any], sid: str) -> bool:
        """该 Section 在计划里是否还有后续批次会带走未到达的 CU。"""
        batches = (getattr(self, "batch_plan", None) or {}).get("batches") or []
        covering = [str(row.get("batch_id") or "") for row in batches if sid in (row.get("section_ids") or [])]
        current = str(batch.get("batch_id") or "")
        if current not in covering or not self._missing_cus(sid):
            return False
        return covering.index(current) < len(covering) - 1

    def _consume_technical_retry_budget(self) -> bool:
        """技术性重试必须有全局上限，否则回到 157 次调用的修复风暴。"""
        budget = int(getattr(self, "technical_retry_budget", 0) or 0) or MAX_TECHNICAL_RETRY_PER_BATCH
        with self.checkpoint_lock:
            if int(getattr(self, "technical_retry_permits", 0) or 0) >= budget:
                return False
            self.technical_retry_permits = int(getattr(self, "technical_retry_permits", 0) or 0) + 1
            if self.global_repair_budget is not None:
                # 技术性重试不占修复预算（它是"内容没到达"），但必须计入调用审计，
                # 否则 repair 与 retry 两种成本会在报告里混成一件事。
                self.global_repair_budget.note_technical_retry()
            return True

    def _commit_arrived_content(self, job: LongformGenerationJob, contract: dict[str, Any], attempts: list[dict[str, Any]]) -> bool:
        """把"已经到达的内容"落成 PROVISIONAL，保证FIRST_DRAFT在任何质量缺口下仍可下载。"""
        sid = contract["section_id"]
        unit = self.word_root / "sections" / sid
        raw_path = unit / "provider_raw.json"
        base = read_json(raw_path) if raw_path.is_file() else None
        if not isinstance(base, dict):
            base = {"section_id": sid, "title": contract.get("section_title") or "", "body_blocks": [], "content_unit_fragments": []}
        base, cu_gaps = self._assemble_section_from_cus(sid, base)
        assembled = [str(cu) for cu in
                     ((base.get("content_unit_execution") or {}).get("assembled_content_unit_ids") or [])]
        if cu_gaps and not assembled:
            # 一个 CU 都没到达，就不是"到达了但需要复核"：本节必须保持 TECHNICAL_FAILED，
            # 否则它会被后续 Resume 当作已完成而跳过，缺失的 CU 再也没有机会被重新请求。
            return False
        base = _repair_word_artifact(base, getattr(self, "editorial_source_facts", None))
        processes = [self.builder.processes[pid] for pid in contract.get("required_processes", []) if pid in self.builder.processes]
        gate = evaluate_word_fragment(base, contract, self.inputs["global_state"], processes)
        if not gate.get("meaningful_body"):
            return False
        if cu_gaps:
            # 批次只到齐一部分：已到达的部分必须进正文（Round 4 实测这一条被整节否决掉了，
            # 300 类合成跑因此每轮静默丢 2–5 节、3.3k–7.1k 字/节，页数在 183～230 之间摆）。
            # 缺口不藏：留在 partial_generation.json 与 outstanding 槽位里，深度门会把这
            # 一节按 DEPTH_INCOMPLETE 交给有界补深，补深只请求差集 CU，不重付整节。
            write_json(unit / "partial_generation.json", {
                "status": "PARTIAL_CU_ARRIVED",
                "completed_content_unit_ids": assembled,
                "remaining_content_unit_ids": [
                    cu for cu in self._planned_cus(sid) if cu not in set(assembled)],
                "updated_at": now_iso(),
            })
        self._commit_section(job, contract, base, gate, attempts, False,
                             provisional=SECTION_DEPTH_INCOMPLETE if cu_gaps else SECTION_PROVISIONAL)
        with self.checkpoint_lock:
            if sid not in self.provisional_sections:
                self.provisional_sections.append(sid)
        return True

    def _settle_generation_gaps(self, job: LongformGenerationJob, batch: dict[str, Any], failed: list[str], classes: dict[str, str], by_id: dict[str, dict[str, Any]]) -> None:
        """Generation 只处理"内容没有到达"，不处理"内容不够好"。

        技术性故障按节做有限次立即重试，且每次只重新请求未到达的 Content Unit；
        预算耗尽后仍带正文的节以 PROVISIONAL 保留，正文完全没有的节记为
        TECHNICAL_FAILED_SECTION 并留下 TERMINAL_BLOCKED 事件——不隐藏失败。
        """
        for sid in failed:
            if self._section_is_draftable(sid):
                continue
            unit = self.word_root / "sections" / sid
            existing = read_json(unit / "audit.json") if (unit / "audit.json").is_file() else {"attempts": []}
            gate = read_json(unit / "qa.json") if (unit / "qa.json").is_file() else {}
            fragment = read_json(unit / "provider_raw.json") if (unit / "provider_raw.json").is_file() else None
            kind = str(classes.get(sid) or "QUALITY_FAILURE")
            decision = classify_section_issue(gate, by_id[sid], kind, fragment=fragment, speed_profile=self.speed_profile)
            kind = str(decision.get("failure_class") or kind)
            retries_used = sum(1 for row in (existing.get("attempts") or []) if str(row.get("mode") or "") == "retry_technical")
            self._record_qa_event(job, sid=sid, check="SECTION_QA", result="FAIL", reason=kind, repair_attempt=retries_used)
            while (
                is_technical_retryable(kind)
                and retries_used < MAX_TECHNICAL_RETRY_PER_BATCH
                and self._consume_technical_retry_budget()
            ):
                retries_used += 1
                with self.checkpoint_lock:
                    job.public_phase = "TECHNICAL_RETRYING"
                    if sid not in job.active_repair_sections:
                        job.active_repair_sections.append(sid)
                self._checkpoint(job)
                retry_started = time.monotonic()
                try:
                    self._repair_section(job, by_id[sid], kind, attempt_mode="retry_technical", draftable_ok=True)
                finally:
                    with self.checkpoint_lock:
                        self.repair_intervals.append((retry_started, time.monotonic()))
                        self.technical_retry_seconds += time.monotonic() - retry_started
                        job.active_repair_sections = [value for value in job.active_repair_sections if value != sid]
                        job.public_phase = "GENERATING"
                recovered = self._section_is_draftable(sid)
                self._record_qa_event(
                    job,
                    sid=sid,
                    check="TECHNICAL_RETRY",
                    result="PASS" if recovered else "FAIL",
                    reason=kind,
                    repair_attempt=retries_used,
                )
                self._checkpoint(job)
                if recovered:
                    break
                if (unit / "qa.json").is_file():
                    retry_gate = read_json(unit / "qa.json")
                    retry_fragment = read_json(unit / "provider_raw.json") if (unit / "provider_raw.json").is_file() else fragment
                    decision = classify_section_issue(retry_gate, by_id[sid], kind, fragment=retry_fragment, speed_profile=self.speed_profile)
                    kind = str(decision.get("failure_class") or kind)
            if self._section_is_draftable(sid):
                continue
            attempts = list((read_json(unit / "audit.json") if (unit / "audit.json").is_file() else {"attempts": []}).get("attempts") or existing.get("attempts") or [])
            if self._commit_arrived_content(job, by_id[sid], attempts):
                self._record_qa_event(job, sid=sid, check="SECTION_QA", result="PROVISIONAL", reason=kind, repair_attempt=retries_used)
                continue
            self._fail_section(job, sid, attempts, SECTION_TECHNICAL_FAILED)
            self._record_qa_event(job, sid=sid, check="SECTION_QA", result="TERMINAL_BLOCKED", reason=kind, repair_attempt=retries_used)

    def _register_supplement_cus(self, sid: str, rows: list[dict[str, Any]]) -> None:
        """把补充计划的新 Content Unit 登记进本节契约表，使其可装配、可审计。"""
        with self.checkpoint_lock:
            unit = self.word_root / "sections" / sid
            fresh = reserve_supplement(unit, sid, rows, occupied_ids=set(self.cu_contracts))
            for cu_id, record in fresh.items():
                self.cu_contracts[cu_id] = copy.deepcopy(record["contract"])
                ids = self.cus_by_section.setdefault(sid, [])
                if cu_id not in ids:
                    ids.append(cu_id)

    def _discard_supplement_cus(self, sid: str, cu_ids: list[str]) -> None:
        """低增量补充不得进入正文：撤下登记并删除刚落盘的片段。"""
        with self.checkpoint_lock:
            unit = self.word_root / "sections" / sid
            for cu_id in discard_supplement(unit, sid, cu_ids):
                self.cu_contracts.pop(cu_id, None)
                self.cus_by_section[sid] = [value for value in self.cus_by_section.get(sid, []) if value != cu_id]

    def _repair_section(self, job: LongformGenerationJob, contract: dict[str, Any], failure_class: str, *, attempt_mode: str | None = None, draftable_ok: bool = False, supplement_rows: list[dict[str, Any]] | None = None, force: bool = False, target_content_unit_ids: list[str] | None = None) -> bool:
        sid = contract["section_id"]
        rows = [row for row in (supplement_rows or []) if row.get("content_unit_id")]
        if not force and not rows and (self._section_is_pass(sid) or (draftable_ok and self._section_is_draftable(sid))):
            return True
        unit = self.word_root / "sections" / sid
        existing = read_json(unit / "audit.json") if (unit / "audit.json").is_file() else {"attempts": []}
        attempts = list(existing.get("attempts", []))
        previous = max((int(row.get("attempt_id", 0)) for row in attempts), default=0)
        if rows:
            try:
                self._register_supplement_cus(sid, rows)
            except ValueError as error:
                write_json(unit / "supplement.json", {
                    "status": "SUPPLEMENT_IDENTITY_REJECTED", "reason": str(error),
                    "content_unit_ids": [str(row.get("content_unit_id")) for row in rows],
                    "updated_at": now_iso(),
                })
                return False
        context = self.builder.word(contract, self.incremental_state)
        # Blocker 4：Provider 必须收到当前 Section 的 Content Unit 施工契约（仅缺失 CU）。
        # 文档级QA的定向修复可以命中"已全部到达"的Section，此时按已计划 CU 重新点名，
        # 装配仍从磁盘上已到达的 CU 重建，不会因为这轮没返回而丢正文。
        remaining_cus = (
            [str(row.get("content_unit_id")) for row in rows]
            if rows
            else (self._missing_cus(contract["section_id"]) or (self._planned_cus(contract["section_id"]) if force else []))
        )
        if target_content_unit_ids is not None:
            owned = set(self._planned_cus(sid))
            if not target_content_unit_ids or not set(target_content_unit_ids).issubset(owned):
                raise ValueError("Table repair targets must belong to the section")
            remaining_cus = list(dict.fromkeys(target_content_unit_ids))
        if remaining_cus and not rows:
            provider_config = getattr(self.provider, "config", {}) if self.provider is not None else {}
            limit = int((provider_config or {}).get("max_tokens") or 8192) if isinstance(provider_config, dict) else 8192
            safe = max(256, int(limit * 0.72))
            groups = content_unit_batches(sid, [self.cu_contracts[cu] for cu in remaining_cus if cu in self.cu_contracts], safe_budget=safe)
            remaining_cus = list((groups[0] if groups else {}).get("content_unit_ids") or remaining_cus)
        if self.cu_contracts:
            context["content_unit_contracts"] = [
                {**self.cu_contracts[cu]} for cu in remaining_cus if cu in self.cu_contracts
            ]
            context["content_unit_execution"] = {
                "mode": "DEPTH_SUPPLEMENT" if rows else "CONTENT_UNIT_GRANULAR",
                "planned_content_unit_ids": self._planned_cus(contract["section_id"]),
                "completed_content_unit_ids": self._completed_cus(contract["section_id"]),
                "remaining_content_unit_ids": remaining_cus,
            }
        write_json(unit / "input.json", context)
        kind = str(failure_class or "QUALITY_FAILURE")
        if rows:
            stage = "supplement"
        elif kind in {"LATENCY_FAILURE", "TIMEOUT", "SOFT_LATENCY_EXCEEDED"} or attempt_mode == "retry_technical":
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
        compact["content_unit_contracts"] = context.get("content_unit_contracts") or []
        compact["content_unit_execution"] = context.get("content_unit_execution") or {}
        if target_content_unit_ids is not None:
            compact["source_table_findings"] = gate.get("source_table_findings") or []
        extra = supplement_prompt_rules(rows) if rows else repair_prompt_rules(kind)
        prompt = self._word_prompt({"section_contract": contract, "repair_pack": compact, "content_unit_contracts": compact["content_unit_contracts"], "content_unit_execution": compact["content_unit_execution"]}, extra)
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
            "attempt_mode": attempt_mode or (
                "supplement_section" if rows else ("repair_section" if kind != "UNDER_LENGTH" else "continue_section")
            ),
            "generation_mode": "longform_section",
            "content_unit_contracts": compact["content_unit_contracts"],
            "content_unit_execution": compact["content_unit_execution"],
        }
        # Depth-only continue keeps previous_fragment and continue_section mode, but the
        # native quality-escalation audit still runs (QUALITY_FAILURE), never as a silent None.
        invoke_failure = "QUALITY_FAILURE" if kind == "UNDER_LENGTH" else kind
        output, audit = self._invoke(request, attempt_dir, stage=stage, failure_class=invoke_failure, batch_size=1)
        attempts.append({"attempt_id": attempt, "mode": request["attempt_mode"], **audit})
        job.retry_count += 1
        write_json(unit / "audit.json", {"attempts": attempts})
        if output is None:
            if rows:
                # 补充失败只是"这一节仍然偏薄"，不得把已有内容的Section判死。
                self._discard_supplement_cus(sid, [str(row.get("content_unit_id")) for row in rows])
                failure = {
                    "status": "SUPPLEMENT_FAILED", "content_unit_ids": [str(row.get("content_unit_id")) for row in rows],
                    "error_code": audit.get("error_code"), "updated_at": now_iso(),
                }
                write_json(attempt_dir / "supplement_decision.json", failure)
                write_json(unit / "supplement.json", failure)
                return False
            self._fail_section(job, sid, attempts, "FAILED_SECTION")
            return False
        fragment = {key: value for key, value in output.items() if key != "provider_metadata"}
        expected_cus = [row["content_unit_id"] for row in compact["content_unit_contracts"]]
        unresolved_cus = False
        if not expected_cus:
            self._settle_request(audit, resolution="repair_response_no_cu_contract", persisted_content_unit_ids=[])
        if expected_cus:
            returned_cus, cu_issues = self._persist_provider_cus(sid, fragment, expected_cus)
            # Settlement describes bytes that actually landed, never the requested ids.
            self._settle_request(
                audit,
                resolution="late_response" if audit.get("late_response_recovered") else "repair_content_persisted",
                persisted_content_unit_ids=returned_cus,
            )
            if not cu_issues and self._missing_cus(sid) and not rows:
                write_json(unit / "partial_generation.json", {"status": "PARTIAL_CU_BATCH", "remaining_content_unit_ids": self._missing_cus(sid), "updated_at": now_iso()})
                self._fail_section(job, sid, attempts, "PARTIAL_CU_BATCH")
                return False
            if rows:
                arrived_rows = [read_json(unit / "cus" / f"{cu_id}.json") for cu_id in returned_cus]
                added = apply_content_units(arrived_rows, section_id=sid,
                                            planned_ids=returned_cus, base_fragment={})
                novelty = novelty_gate(rendered_supplement_text(previous_fragment or {}),
                                       rendered_supplement_text(added))
                novelty["basis"] = "RENDERED_CU_CONTENT_AND_STRUCTURE"
                supplement_decision = {
                    "status": ("ACCEPTED" if len(returned_cus) == len(expected_cus) else "PARTIAL_ACCEPTED")
                              if novelty.get("passed") else "REJECTED",
                    "content_unit_ids": returned_cus,
                    "expected_content_unit_ids": expected_cus,
                    "novelty": novelty,
                    "updated_at": now_iso(),
                }
                write_json(attempt_dir / "supplement_decision.json", supplement_decision)
                write_json(unit / "supplement.json", supplement_decision)
                if not novelty.get("passed"):
                    self._discard_supplement_cus(sid, [str(row.get("content_unit_id")) for row in rows])
                    return False
            fragment, assembly_issues = self._assemble_section_from_cus(sid, fragment)
            unresolved_cus = bool(cu_issues or assembly_issues)
            if cu_issues or assembly_issues:
                write_json(unit / "governance.json", {
                    "status": "BLOCK",
                    "issues": cu_issues + assembly_issues,
                    "returned_content_unit_ids": returned_cus,
                    "expected_content_unit_ids": expected_cus,
                })
                if not rows:
                    self._fail_section(job, sid, attempts, "FAILED_SECTION")
                    return False
                # Retain unanswered supplement reservations as explicit gaps.
                # Arrived units can be committed without declaring the plan settled.
        elif self._planned_cus(sid):
            # 本轮一个 CU 契约都没携带（`_missing_cus` 已空，缺的在别的批次里落了盘），
            # 但 Provider 交回的是**整节原始输出**：不重建就把 fragment.json 覆盖成
            # section-granular 正文，已到达 CU 的表格/清单/流程全部掉出正文（Round 4 合成
            # 300 类实测 2 节 26 个 CU、16,880 字，§64 治理账在这两节无账）。
            # 装配口径只有一把尺：节正文一律由到达的 CU 重建，与批次路径 2637 行同。
            # 这里不可能报 missing —— "expected_cus 为空"正是"无缺"的定义。
            fragment, _ = self._assemble_section_from_cus(sid, fragment)
        fragment = _repair_word_artifact(fragment, getattr(self, "editorial_source_facts", None))
        write_json(unit / "provider_raw.json", fragment)
        processes = [self.builder.processes[pid] for pid in contract.get("required_processes", []) if pid in self.builder.processes]
        gate = evaluate_word_fragment(fragment, contract, self.inputs["global_state"], processes)
        write_json(unit / "governance.json", gate)
        write_json(unit / "qa.json", gate)
        decision = classify_section_issue(gate, contract, kind, fragment=fragment, speed_profile=self.speed_profile)
        short = self.require_section_min and decision.get("severity") in {"CRITICAL_UNDER_LENGTH", "MATERIAL_UNDER_LENGTH"}
        if not gate.get("meaningful_body"):
            if rows:
                self._discard_supplement_cus(sid, [str(row.get("content_unit_id")) for row in rows])
                return False
            self._fail_section(job, sid, attempts, SECTION_TECHNICAL_FAILED if draftable_ok else "FAILED_SECTION")
            return False
        if gate["status"] == "BLOCK" or short or unresolved_cus:
            if draftable_ok or rows:
                # 技术重试的目标是"内容到达"，不是"内容达标"；剩余缺口交给文档级QA。
                self._commit_section(job, contract, fragment, gate, attempts, kind == "UNDER_LENGTH", provisional=SECTION_PROVISIONAL)
                with self.checkpoint_lock:
                    if sid not in self.provisional_sections:
                        self.provisional_sections.append(sid)
                return True
            self._fail_section(job, sid, attempts, "FAILED_SECTION")
            return False
        self._commit_section(job, contract, fragment, gate, attempts, kind == "UNDER_LENGTH" or bool(rows))
        return True

    def repair_table_projections(self, section_ids: list[str]) -> dict[str, Any]:
        """Repair local display loss first; re-ask only an unresolved CU once."""
        from governance.table_fidelity import check_source_table, project_source_table
        import hashlib
        receipt_path = self.word_root / "table_projection_repair.json"
        previous = read_json(receipt_path) if receipt_path.is_file() else {}
        asked = set(previous.get("asked_issue_keys") or [])
        report = {"schema_version": "table-projection-repair-v1", "local_actions": [],
                  "remote_results": [], "unresolved": [], "asked_issue_keys": sorted(asked)}
        contracts = {r["section_id"]: r for r in self.inputs["section_contracts"]["contracts"]}
        job = None
        for sid in section_ids:
            unit = self.word_root / "sections" / sid
            path = unit / "fragment.json"
            if sid not in contracts or not path.is_file():
                continue
            fragment = read_json(path)
            original = path.read_bytes()
            actions = []
            for index, table in enumerate(fragment.get("tables") or []):
                if not isinstance(table, dict) or not check_source_table(table):
                    continue
                projected = project_source_table(table)
                if projected is None:
                    continue
                fragment["tables"][index] = projected
                actions.append({"section_id": sid, "table_index": index,
                                "content_unit_id": (table.get("table_source_contract") or {}).get("content_unit_id"),
                                "source_digest": (table.get("table_source_contract") or {}).get("source_values_digest"),
                                "reason": "REPROJECT_FROM_INTACT_IMMUTABLE_CU_RECEIPT",
                                "provider_calls": 0})
            if actions:
                history = unit / "local_table_repair_history" / (hashlib.sha256(original).hexdigest() + ".json")
                history.parent.mkdir(parents=True, exist_ok=True)
                if not history.exists():
                    history.write_bytes(original)
                contract = contracts[sid]
                processes = [self.builder.processes[p] for p in contract.get("required_processes", [])
                             if p in self.builder.processes]
                gate = evaluate_word_fragment(fragment, contract, self.inputs["global_state"], processes)
                audit = read_json(unit / "audit.json") if (unit / "audit.json").is_file() else {}
                job = job or self._job(self.word_root, "WORD", len(contracts))
                old_depth = self._section_status(sid) == SECTION_DEPTH_INCOMPLETE and gate.get("length_status") != "PASS"
                provisional = SECTION_PROVISIONAL if gate["status"] == "BLOCK" else (SECTION_DEPTH_INCOMPLETE if old_depth else None)
                self._commit_section(job, contract, fragment, gate, list(audit.get("attempts") or []), False,
                                     provisional=provisional)
                report["local_actions"].extend(actions)
            current = read_json(path)
            failures = [f for table in current.get("tables") or [] if isinstance(table, dict)
                        for f in check_source_table(table)]
            cu_ids = sorted({str(f.get("path") or "").split("/")[0] for f in failures})
            for cu_id in cu_ids:
                key = "TABLE_SOURCE:" + sid + ":" + cu_id
                if cu_id not in self.cu_contracts or cu_id not in self._planned_cus(sid) or key in asked:
                    continue
                asked.add(key)
                # Persist before invoking: resume cannot buy the same failed repair again.
                report["asked_issue_keys"] = sorted(asked)
                write_json(receipt_path, report)
                result = self.selective_repair([{"section_id": sid, "content_unit_ids": [cu_id],
                    "issue_key": key, "priority": PRIORITY_BLOCK, "failure_class": "TABLE_SOURCE_FIDELITY"}], job=job)
                report["remote_results"].append(result)
            current = read_json(path)
            report["unresolved"].extend({**f, "section_id": sid}
                for table in current.get("tables") or [] if isinstance(table, dict)
                for f in check_source_table(table))
        report["asked_issue_keys"] = sorted(asked)
        report["status"] = "BLOCK" if report["unresolved"] else "PASS"
        if previous:
            report["prior_receipts"] = previous.get("prior_receipts", []) + [
                {k: v for k, v in previous.items() if k != "prior_receipts"}]
        write_json(receipt_path, report)
        return report


    def selective_repair(self, candidates: list[dict[str, Any]], *, job: LongformGenerationJob | None = None) -> dict[str, Any]:
        """文档级QA之后的有界选择式修复（§25-29）。

        排序按 P0 BLOCK → Scoring Gap → Requirement Gap → Depth Gap → 其它；
        全局预算耗尽即停，不再进入第二轮，同一 issue_key 最多自动修一次。
        每次修复只重新请求该缺口涉及的 Content Unit，绝不重跑整篇文档。
        """
        contracts = {row["section_id"]: row for row in self.inputs["section_contracts"]["contracts"]}
        job = job or self._job(self.word_root, "WORD", len(contracts))
        budget = self.global_repair_budget
        results: list[dict[str, Any]] = []
        attempted = repaired = skipped = 0
        started = time.monotonic()
        for candidate in sort_repair_candidates(list(candidates or [])):
            sid = str(candidate.get("section_id") or "")
            contract = contracts.get(sid)
            issue_key = str(candidate.get("issue_key") or f"BLOCK:{sid}")
            priority = str(candidate.get("priority") or PRIORITY_NEEDS_REVIEW)
            failure_class = str(candidate.get("failure_class") or "QUALITY_FAILURE")
            if not contract:
                results.append({"section_id": sid, "status": "SKIPPED", "reason": "UNKNOWN_SECTION", "issue_key": issue_key})
                skipped += 1
                continue
            allowed, reason = budget.allow(issue_key, priority=priority) if budget is not None else (True, "NO_BUDGET")
            if not allowed:
                skipped += 1
                results.append({"section_id": sid, "status": "SKIPPED", "reason": reason, "issue_key": issue_key, "priority": priority})
                self._record_qa_event(job, sid=sid, check="SELECTIVE_REPAIR", result="SKIPPED", reason=reason, repair_attempt=0)
                self._checkpoint(job)
                continue
            attempted += 1
            if budget is not None:
                budget.note_attempt(issue_key, kind="repair")
            with self.checkpoint_lock:
                job.public_phase = "SELECTIVE_REPAIR"
                if sid not in job.active_repair_sections:
                    job.active_repair_sections.append(sid)
            self._checkpoint(job)
            call_started = time.monotonic()
            ok = False
            try:
                targets = candidate.get("content_unit_ids")
                repair_options = {"target_content_unit_ids": targets} if targets is not None else {}
                ok = self._repair_section(
                    job, contract, failure_class, attempt_mode="repair_section", draftable_ok=True, force=True,
                    **repair_options,
                )
                if failure_class == "TABLE_SOURCE_FIDELITY":
                    latest = read_json(self.word_root / "sections" / sid / "qa.json")
                    remaining = [f for f in latest.get("source_table_findings") or []
                                 if targets is None or str(f.get("path") or "").split("/")[0] in targets]
                    ok = bool(ok and not remaining)
            finally:
                with self.checkpoint_lock:
                    self.selective_repair_wall_seconds += time.monotonic() - call_started
                    job.active_repair_sections = [value for value in job.active_repair_sections if value != sid]
                    job.public_phase = "DOCUMENT_QA"
            repaired += 1 if ok else 0
            results.append({
                "section_id": sid,
                "status": "REPAIRED" if ok else "STILL_OPEN",
                "issue_key": issue_key,
                "priority": priority,
                "failure_class": failure_class,
                "section_state": self._section_status(sid),
            })
            if budget is not None:
                budget.note_outcome(issue_key, repaired=bool(ok))
                budget.observe_repeated_failure(issue_key)
            self._record_qa_event(
                job, sid=sid, check="SELECTIVE_REPAIR",
                result="PASS" if ok else "FAIL", reason=failure_class, repair_attempt=1,
            )
            self._checkpoint(job)
        seconds = round(time.monotonic() - started, 3)
        if budget is not None:
            budget.observe_time(
                initial_seconds=self.initial_generation_seconds,
                repair_seconds=self.repair_provider_seconds + self.supplement_seconds,
            )
        return {
            "schema_version": "selective-repair-v0.1",
            "status": "COMPLETED" if not skipped else "COMPLETED_WITH_SKIPS",
            "candidates": len(candidates or []),
            "attempted": attempted,
            "repaired": repaired,
            "skipped": skipped,
            "seconds": seconds,
            "results": results,
            "budget": budget.audit() if budget else None,
        }

    def supplement_depth(self, targets: list[dict[str, Any]], *, job: LongformGenerationJob | None = None) -> dict[str, Any]:
        """文档级QA之后的有界深度补充（§30-34）。

        每次补充只请求"新增Content Unit"，绝不重跑整节；同一 issue_key 在
        GlobalRepairBudget 下最多自动补一次，低增量产物由 novelty 门丢弃。
        """
        contracts = {row["section_id"]: row for row in self.inputs["section_contracts"]["contracts"]}
        job = job or self._job(self.word_root, "WORD", len(contracts))
        budget = self.global_repair_budget
        results: list[dict[str, Any]] = []
        attempted = accepted = rejected = skipped = 0
        started = time.monotonic()
        for target in targets or []:
            sid = str(target.get("section_id") or "")
            contract = contracts.get(sid)
            issue_key = str(target.get("issue_key") or f"DEPTH:{sid}")
            priority = str(target.get("priority") or PRIORITY_DEPTH_GAP)
            if not contract:
                results.append({"section_id": sid, "status": "SKIPPED", "reason": "UNKNOWN_SECTION"})
                continue
            allowed, reason = budget.allow(issue_key, priority=priority, kind="supplement") if budget is not None else (True, "NO_BUDGET")
            if not allowed:
                skipped += 1
                results.append({"section_id": sid, "status": "SKIPPED", "reason": reason, "issue_key": issue_key})
                self._record_qa_event(job, sid=sid, check="DEPTH_SUPPLEMENT", result="SKIPPED", reason=reason, repair_attempt=0)
                continue
            unit = self.word_root / "sections" / sid
            gate = read_json(unit / "qa.json") if (unit / "qa.json").is_file() else {}
            fragment = None
            for candidate in (unit / "fragment.json", unit / "provider_raw.json"):
                if candidate.is_file():
                    fragment = read_json(candidate)
                    break
            rows = plan_depth_supplement(
                contract,
                gate,
                fragment=fragment,
                planned_cu_ids=self._reserved_cu_ids(sid),
                existing_archetypes=[str((self.cu_contracts.get(cu) or {}).get("archetype") or "") for cu in self._planned_cus(sid)],
                focus=[row for row in (target.get("focus") or []) if isinstance(row, dict)],
            )
            if not rows:
                skipped += 1
                results.append({"section_id": sid, "status": "NO_NEW_CONTENT_UNIT", "reason": "NOVELTY_UNAVAILABLE", "issue_key": issue_key})
                if budget is not None:
                    budget.note_outcome(issue_key, repaired=False)
                continue
            attempted += 1
            if budget is not None:
                budget.note_attempt(issue_key, kind="supplement")
            with self.checkpoint_lock:
                job.public_phase = "DEPTH_SUPPLEMENTING"
            self._checkpoint(job)
            call_started = time.monotonic()
            ok = False
            try:
                ok = self._repair_section(job, contract, "UNDER_LENGTH", attempt_mode="supplement_section", supplement_rows=rows)
            finally:
                with self.checkpoint_lock:
                    self.supplement_wall_seconds += time.monotonic() - call_started
                    job.public_phase = "DOCUMENT_QA"
            accepted += 1 if ok else 0
            rejected += 0 if ok else 1
            supplement_report = read_json(unit / "supplement.json") if (unit / "supplement.json").is_file() else {}
            results.append({
                "section_id": sid,
                "status": "SUPPLEMENTED" if ok else str(supplement_report.get("status") or "SUPPLEMENT_REJECTED"),
                "issue_key": issue_key,
                "priority": priority,
                "content_unit_ids": [str(row.get("content_unit_id")) for row in rows],
                "archetypes": [str(row.get("archetype")) for row in rows],
                "novelty": (supplement_report.get("novelty") or {}),
            })
            if budget is not None:
                budget.note_outcome(issue_key, repaired=bool(ok))
                budget.observe_repeated_failure(issue_key)
            self._record_qa_event(
                job, sid=sid, check="DEPTH_SUPPLEMENT",
                result="PASS" if ok else "REJECTED",
                reason=str((results[-1].get("novelty") or {}).get("reason") or "SUPPLEMENTED"),
                repair_attempt=1,
            )
        seconds = round(time.monotonic() - started, 3)
        return {
            "schema_version": "depth-supplement-v0.1",
            "status": "COMPLETED" if not skipped else "COMPLETED_WITH_SKIPS",
            "targets": len(targets or []),
            "attempted": attempted,
            "supplemented": accepted,
            "rejected": rejected,
            "skipped": skipped,
            "seconds": seconds,
            "results": results,
            "budget": budget.audit() if budget else None,
        }

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
        active_sections = list(self.cus_by_section)
        planned_cus = [cu_id for sid in active_sections for cu_id in self._planned_cus(sid)]
        completed_cus = [cu_id for sid in active_sections for cu_id in self._completed_cus(sid)]
        provider_received = (
            list(getattr(self.provider, "received_content_unit_ids", []) or [])
            + list(self.provider_acknowledged_content_unit_ids)
        )
        legacy_cus = []
        for path in (self.word_root / "sections").glob("*/cus/CU-*.json"):
            row = read_json(path)
            if row.get("generation_origin") != "provider_content_unit" or row.get("covered_by_section_assembly"):
                legacy_cus.append(row.get("content_unit_id") or path.stem)
        cu_audit = rebuild_content_unit_generation_audit(self.run_root)
        section_states = {str(row.get("section_id") or ""): str(row.get("status") or "") for row in statuses}
        depth_supplement_reports = [
            {**(read_json(path) or {}), "section_id": path.parent.name}
            for path in sorted((self.word_root / "sections").glob("*/supplement.json"))
        ]
        provisional_sections = sorted(sid for sid, state in section_states.items() if state in (SECTION_PROVISIONAL, SECTION_DEPTH_INCOMPLETE))
        # 生成期的硬性失败沿用旧状态字面量，QA 期使用 TECHNICAL_FAILED_SECTION；
        # 两者都是"未到达内容"的技术缺口，修复候选必须一并看到。
        technical_failed_sections = sorted(
            sid for sid, state in section_states.items()
            if state in {SECTION_TECHNICAL_FAILED, "FAILED_SECTION", "PARTIAL_CU_BATCH"}
        )
        # Preserve in-memory evidence for a currently running attempt when the
        # ledger has not flushed yet; all persisted final counts remain unique.
        sent_ids = set(cu_audit.get("sent_content_unit_ids") or []) | set(self.sent_content_unit_ids)
        received_ids = set(cu_audit.get("provider_received_content_unit_ids") or []) | set(provider_received)
        returned_ids = set(cu_audit.get("returned_content_unit_ids") or []) | set(self.returned_content_unit_ids)
        cu_audit.update({
            "planned": len(set(planned_cus)),
            "sent": len(sent_ids),
            "provider_received": len(received_ids),
            "returned": len(returned_ids),
            "completed": len(set(completed_cus)),
            "legacy_fallback": len(set(legacy_cus)),
            "planned_content_unit_ids": sorted(set(planned_cus)),
            "sent_content_unit_ids": sorted(sent_ids),
            "provider_received_content_unit_ids": sorted(received_ids),
            "returned_content_unit_ids": sorted(returned_ids),
            "completed_content_unit_ids": sorted(set(completed_cus)),
            "legacy_content_unit_ids": sorted(set(legacy_cus)),
        })
        write_json(self.run_root / "content_unit_generation_audit.json", cu_audit)
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
            # The initial schedule is represented by generation_batch_count;
            # a batch may fan out to section-level requests for independent
            # contracts, so using raw provider calls here would under-report
            # the planned aggregation and make the metric misleading.
            "INITIAL_SECTIONS_PER_CALL": (
                round(logical / float(self.performance.get("generation_batch_count") or 0), 2)
                if logical and self.performance.get("generation_batch_count")
                else (round(logical / initial, 2) if initial else None)
            ),
            "INITIAL_CALL_REDUCTION_RATIO": round(1 - (initial / logical), 4) if logical and initial else None,
            "SECTIONS_PER_PROVIDER_CALL": round(logical / initial, 2) if initial else (round(logical / calls, 2) if calls else None),
            # Reduction is measured against the logical plan and the planned
            # initial batches, not against retries/repairs.  Counting repair
            # calls here can make a successful capacity-aware plan look like
            # a negative reduction even though the initial schedule merged
            # sections correctly.
            "CALL_REDUCTION_RATIO": (
                round(1 - (float(self.performance.get("generation_batch_count") or 0) / logical), 4)
                if logical and self.performance.get("generation_batch_count")
                else (round(1 - (calls / logical), 4) if logical else None)
            ),
            "PROVIDER_REPAIR_COUNT": self.section_repair_calls,
            "LOCAL_REPAIR_COUNT": self.local_repair_count + (budget.local_repair_count if budget else 0),
            "REPAIR_BUDGET_USED": budget.used if budget else 0,
            "REPAIR_BUDGET_MAX": budget.max_provider if budget else None,
            "REPAIR_SKIPPED_LOW_VALUE": self.repair_skipped_low_value + (budget.skipped_low_value if budget else 0),
            "REPAIR_ESCALATED_CRITICAL": self.repair_escalated_critical + (budget.escalated_critical if budget else 0),
            "INITIAL_GENERATION_SECONDS": round(self.initial_generation_seconds, 3),
            "REPAIR_PROVIDER_SECONDS": round(self.repair_provider_seconds, 3),
            # MMF-007C 成本与耗时审计：初稿 / 技术重试 / 修复 / 补充必须分开计量。
            "TECHNICAL_RETRY_CALLS": self.technical_retry_calls,
            "TECHNICAL_RETRY_BUDGET": self.technical_retry_budget,
            "TECHNICAL_RETRY_SECONDS": round(self.technical_retry_seconds, 3),
            "REPAIR_CALLS": self.repair_calls,
            "SUPPLEMENT_CALLS": self.supplement_calls,
            "SUPPLEMENT_SECONDS": round(self.supplement_seconds, 3),
            "SUPPLEMENT_WALL_SECONDS": round(self.supplement_wall_seconds, 3),
            "SELECTIVE_REPAIR_WALL_SECONDS": round(self.selective_repair_wall_seconds, 3),
            "DEPTH_SUPPLEMENT": depth_supplement_reports,
            # P0-1：晚到响应必须可见——回收了几条、重发几次、还剩几条未知。
            "LATE_RESPONSE_RECOVERED": self.late_response_recovered,
            "CONTROLLED_RESENDS": self.controlled_resends,
            "LATE_RESPONSE_RECONCILIATION": self.reconciliation,
            "TOTAL_CALLS": calls,
            "section_states": section_states,
            "PROVISIONAL_SECTIONS": provisional_sections,
            "TECHNICAL_FAILED_SECTIONS": technical_failed_sections,
            # 本轮实际负责生成的 Section 范围：QA 之后的修复预算只能花在这里，
            # 不得去补本轮从未计划生成的章节。
            "SCOPE_SECTION_IDS": sorted(set(self.active_word_section_ids) or set(active_sections)) if (self.active_word_section_ids or active_sections) else [],
            # 已到达 Content Unit 的原型分布：文档级补深要知道"这一节压根没出过表单"，
            # 光看字数永远问不出这个问题。
            "SECTION_CU_ARCHETYPES": {
                sid: [str((self.cu_contracts.get(cu) or {}).get("archetype") or "")
                      for cu in self._completed_cus(sid)]
                for sid in active_sections
            },
            "DEPTH_GAP_SECTIONS": list(self.depth_gap_sections),
            "envelope_transport": {
                "provider_calls_with_envelope": self.envelope_calls,
                "recovered_content_units": self.envelope_recovered_cus,
                "truncated_content_units": self.envelope_truncated_cus,
                "enabled": self.envelope_transport,
            },
            "global_repair_budget": self.global_repair_budget.audit() if self.global_repair_budget else None,
            "MAX_OBSERVED_CONCURRENCY": getattr(scheduler, "max_observed_concurrency", None) if scheduler else None,
            "AVG_OBSERVED_CONCURRENCY": getattr(scheduler, "avg_observed_concurrency", None) if scheduler else None,
            # 批次数≠并发度：同一 Section 的多个批次同时挂起不增加吞吐，只有跨节并行才增加。
            "MAX_OBSERVED_DISTINCT_SECTIONS": getattr(scheduler, "max_observed_distinct_sections", None) if scheduler else None,
            "CONCURRENCY_LEDGER": [dict(row) for row in self.concurrency_ledger],
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
            "content_unit_generation": cu_audit,
        }

    def _generate_ppt_bounded(self, slide_ids: list[str] | None = None) -> dict[str, Any]:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from planning.ppt_delivery import execution_budget, generation_seconds_left
        from planning.ppt_delivery import PAGE_RESPONSE_SECONDS, LATE_RECONCILE_SECONDS

        plan = self.inputs["ppt_plan"]
        all_slides = plan["slide_storyboard"]
        physical = len(all_slides) + len(plan["chapter_blocks"]) + 3
        if physical > 80:
            raise ValueError("The bounded PPT profile supports at most 80 physical pages")
        slides = [s for s in all_slides if slide_ids is None or s["slide_id"] in set(slide_ids)]
        chapters = {c["chapter_id"]: c for c in plan["chapter_blocks"]}
        budget = execution_budget(self.ppt_root)
        call_cap = min(96, len(all_slides) + 6)
        budget.setdefault("page_requests_started", 0)
        workers = max(1, min(2, self.max_parallelism))
        if self.provider_name in {'zcode_cn', 'qoder_account'} and hasattr(self.provider, 'prepare_ppt_run'):
            contract=self.provider.prepare_ppt_run(self.run_root.name,len(all_slides),call_cap)
            budget['ppt_execution_contract']=contract
            if self.provider_name=='zcode_cn':
                budget['zcode_execution_contract']=contract
                workers=1
            call_cap=min(call_cap,contract['page_call_cap'])
            write_json(self.ppt_root/'execution_budget.json',budget)
        job = self._job(self.ppt_root, "PPT", len(slides))
        job.parallelism = workers
        # Rebuild completion from page checkpoints, never from a prior short deck.
        job.completed_units = []
        job.failed_units = []
        job.deferred_external_events = []
        stop = threading.Event()
        systemic_reason = {"status": ""}
        started = time.time()
        ledger = getattr(self, "run_root", self.ppt_root.parent.parent) / "provider_unknown_requests.jsonl"
        for slide in slides:
            (self.ppt_root / "slides" / slide["slide_id"]).mkdir(parents=True, exist_ok=True)

        def open_request(sid):
            with self.checkpoint_lock:
                rows = request_ledger.open_requests(request_ledger.read_events(ledger))
            return next((row for row in reversed(rows) if row.get("slide_id") == sid), None)

        def one(slide, *, recover_only=False):
            sid = slide["slide_id"]
            unit = self.ppt_root / "slides" / sid
            status_path = unit / "status.json"
            status = read_json(status_path).get("status") if status_path.is_file() else ""
            raw_path = unit / "provider_raw.json"
            # A completion marker alone is insufficient if the payload disappeared.
            if status in {"COMPLETED", "COMPLETED_WITH_WARNING"} and (unit / "payload.json").is_file():
                return sid, status, 0
            context = self.builder.ppt(slide, chapters[slide["chapter_id"]])
            context["deck_context"] = {
                "other_page_topics": [{"slide_id": s["slide_id"], "title": s["slide_title"]} for s in all_slides if s["slide_id"] != sid],
                "instruction": "本页只展开当前主题；其他页面已有独立主题，不再重复展开。",
            }
            write_json(unit / "input.json", context)
            attempts = list((read_json(unit / "audit.json") if (unit / "audit.json").is_file() else {}).get("attempts", []))
            gate = read_json(unit / "governance.json") if (unit / "governance.json").is_file() else None
            final = None
            retries = 0
            last_status = status or "FAILED_SLIDE"

            def accept(output, audit, attempt):
                nonlocal gate, final
                output = {k: v for k, v in output.items() if k != "provider_metadata"}
                write_json(unit / f"provider_raw_attempt_{attempt}.json", output)
                write_json(raw_path, output)
                self._settle_request(audit, resolution="late_response" if audit.get("late_response_recovered") else "content_persisted", persisted_content_unit_ids=[sid])
                gate = evaluate_ppt_payload(output, slide, self.inputs["global_state"])
                final = output

            if raw_path.is_file():
                candidate = read_json(raw_path)
                gate = evaluate_ppt_payload(candidate, slide, self.inputs["global_state"])
                if gate["status"] != "BLOCK":
                    final = candidate
            # Recovery precedes the retry/call/time guards: a paid-for reply is not a new call.
            origin = open_request(sid) if final is None else None
            if origin:
                prompt = self._ppt_prompt(context, None)
                probe = {"generation_mode": "ppt_slide", "slide_id": sid, "prompt": prompt["prompt"],
                         "required_keys": sorted(PPT_REQUIRED_KEYS), "task_id": origin.get("task_id", "")}
                recovered = self._collect_late_response(origin, probe, unit / "provider_recovered")
                if recovered:
                    output, audit = recovered
                    previous = max((int(a.get("attempt_id", 0)) for a in attempts), default=1)
                    attempts.append({"attempt_id": previous, "recovery_only": True, **audit})
                    accept(output, audit, previous)
                    origin = None
                else:
                    # Only a positively identified terminal rejection permits a
                    # bounded repair. A genuinely unknown request is never resent.
                    with self.checkpoint_lock:
                        terminal = next((r for r in reversed(request_ledger.read_events(ledger))
                                         if r.get("request_id") == origin.get("request_id")), {})
                    if (terminal.get("status") == request_ledger.RESPONSE_REJECTED
                            and terminal.get("resolution") == "invalid_native_response"):
                        origin = None
                        recover_only = False
                        last_status = "FAILED_OUTPUT_CONTRACT"
                        gate = {"status": "BLOCK", "transport_error": terminal.get("error")}
                    else:
                        last_status = "UNKNOWN_REQUEST_REVIEW"
            if (final is None or gate["status"] == "BLOCK" or gate.get("overflow")) and not origin and not recover_only:
                previous = max((int(a.get("attempt_id", 0)) for a in attempts if not a.get("recovery_only") and a.get("invoked", True)), default=0)
                for attempt in range(previous + 1, 3):
                    if stop.is_set():
                        last_status = systemic_reason["status"]
                        break
                    if generation_seconds_left(budget) <= 0:
                        last_status = "DEFERRED_PPT_TIME_BUDGET"
                        break
                    if slide.get("evidence_requirement") == "SOURCE_REQUIRED":
                        last_status = "FAILED_SOURCE_REQUIRED"
                        break
                    repairs = None if attempt == 1 else ["只修复当前页已发现问题，保留完整动作和条件：" + json.dumps(gate or {}, ensure_ascii=False)]
                    prompt = self._ppt_prompt(context, repairs)
                    request = {"task_id": f"{job.job_id}-{sid}-A{attempt}", "system_prompt": prompt["system"], "prompt": prompt["prompt"],
                               "required_keys": sorted(PPT_REQUIRED_KEYS), "agent_max_turns": 1, "generation_mode": "ppt_slide", "slide_id": sid,
                               "response_deadline_epoch": min(budget["generation_deadline_epoch"], time.time() + PAGE_RESPONSE_SECONDS)}
                    if self.provider_name=='zcode_cn':
                        request.update(response_budget_seconds=PAGE_RESPONSE_SECONDS,generation_deadline_epoch=budget['generation_deadline_epoch'])
                    elif self.provider_name=='qoder_account':
                        request['generation_deadline_epoch']=budget['generation_deadline_epoch']
                    with self.checkpoint_lock:
                        if budget["page_requests_started"] >= call_cap:
                            last_status = "DEFERRED_PPT_CALL_BUDGET"
                            break
                        budget["page_requests_started"] += 1
                        budget["page_request_cap"] = call_cap
                        write_json(self.ppt_root / "execution_budget.json", budget)
                    # Persist attempt identity before dispatch so an unexpected
                    # local exception cannot reset the per-page attempt bound.
                    attempts.append({"attempt_id": attempt, "status": "REQUEST_STARTED",
                                     "dispatch_uncertain": True})
                    write_json(unit / "audit.json", {"attempts": attempts})
                    output, audit = self._invoke(request, unit / f"provider_attempt_{attempt}")
                    if audit.get("invoked") is False:
                        with self.checkpoint_lock:
                            budget["page_requests_started"] -= 1
                            write_json(self.ppt_root / "execution_budget.json", budget)
                    attempts[-1] = {"attempt_id": attempt, **audit}
                    last_status = audit.get("status") or "FAILED_SLIDE"
                    if output is None:
                        if audit.get("error_code") == "OUTPUT_CONTRACT_ERROR" and audit.get("invoked") is not False:
                            last_status = "FAILED_OUTPUT_CONTRACT"
                            gate = {"status": "BLOCK", "transport_error": audit.get("error")}
                            # The existing two-attempt, call and time guards apply
                            # again at the top of the loop; no deck-wide resend.
                            continue
                        # Timeout/unknown/source failures belong to one page. Only explicit
                        # authorization/platform failures stop new requests across the deck.
                        if last_status in {"DEFERRED_PLATFORM_CONFIRMATION", "DEFERRED_PROVIDER_AUTH"} or ((self.provider_name=='zcode_cn' or is_cursor_provider(self.provider_name)) and last_status=='DEFERRED_CALL_BUDGET'):
                            systemic_reason["status"] = last_status
                            stop.set()
                        break
                    accept(output, audit, attempt)
                    if gate["status"] != "BLOCK" and not gate["overflow"]:
                        break
                    retries += 1
            write_json(unit / "audit.json", {"attempts": attempts})
            write_json(unit / "governance.json", gate or {"status": "DEFERRED"})
            if final is not None and gate["status"] != "BLOCK":
                status = "COMPLETED_WITH_WARNING" if gate["status"] == "WARNING" else "COMPLETED"
                write_json(unit / "payload.json", final)
            else:
                status = last_status if last_status.startswith(("DEFERRED_", "FAILED_")) or last_status == "UNKNOWN_REQUEST_REVIEW" else "FAILED_SLIDE"
            write_json(status_path, {"slide_id": sid, "status": status, "updated_at": now_iso()})
            return sid, status, retries

        outcomes = {}
        def safe_one(slide, *, recover_only=False):
            sid = slide["slide_id"]
            unit = self.ppt_root / "slides" / sid
            # An unexpected page error may occur after dispatch. Keep its evidence
            # and require an explicitly targeted recovery instead of blind resend.
            try:
                saved_status = read_json(unit / "status.json").get("status") if (unit / "status.json").is_file() else ""
                if (unit / "page_error.json").is_file() and saved_status == "FAILED_PAGE_ERROR" and slide_ids is None:
                    return sid, "FAILED_PAGE_ERROR", 0
                return one(slide, recover_only=recover_only)
            except Exception as exc:
                write_json(unit / "page_error.json", {
                    "slide_id": sid, "error_type": type(exc).__name__,
                    "error": str(exc)[:500], "status": "FAILED_PAGE_ERROR",
                    "requires_targeted_recovery": True, "updated_at": now_iso(),
                })
                write_json(unit / "status.json", {
                    "slide_id": sid, "status": "FAILED_PAGE_ERROR", "updated_at": now_iso(),
                })
                return sid, "FAILED_PAGE_ERROR", 0

        def record(result):
            sid, status, retries = result
            outcomes[sid] = status
            job.retry_count += retries
            job.completed_units = [s["slide_id"] for s in slides if outcomes.get(s["slide_id"]) in {"COMPLETED", "COMPLETED_WITH_WARNING"}]
            job.failed_units = [s for s, st in outcomes.items() if st.startswith("FAILED_")]
            job.deferred_external_events = [{"unit": s, "status": st} for s, st in outcomes.items() if st.startswith("DEFERRED_") or st == "UNKNOWN_REQUEST_REVIEW"]
            job.dump(self.ppt_root / "status.json")

        job.status = "EXTERNAL_PROVIDER_RUNNING"
        job.dump(self.ppt_root / "status.json")
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ppt-page") as pool:
            futures = [pool.submit(safe_one, slide) for slide in slides]
            for future in as_completed(futures):
                record(future.result())
        # No parent chat turn and no resend needed to collect a late answer.
        reconcile_until = min(budget["generation_deadline_epoch"], time.time() + LATE_RECONCILE_SECONDS)
        while not stop.is_set():
            pending = [s for s in slides if outcomes[s["slide_id"]] not in {"COMPLETED", "COMPLETED_WITH_WARNING", "FAILED_PAGE_ERROR"} and open_request(s["slide_id"])]
            if not pending:
                break
            for slide in pending:
                record(safe_one(slide, recover_only=True))
            if time.time() >= reconcile_until:
                break
            time.sleep(min(1.0, max(0.0, reconcile_until - time.time())))
        job.finished_at = now_iso()
        job.current_unit = None
        job.status = "COMPLETED" if len(job.completed_units) == job.total_units and not job.failed_units else "DEFERRED_EXTERNAL" if job.deferred_external_events and not job.failed_units else "COMPLETED_WITH_GAPS"
        job.public_phase = "SECTION_QA" if job.status == "COMPLETED" else "TERMINAL_BLOCKED"
        job.dump(self.ppt_root / "status.json")
        write_json(self.ppt_root / "execution_observation.json", {"profile": "bounded_90min_v1", "workers": workers, "physical_pages": physical,
                   "generation_elapsed_seconds": round(time.time() - started, 3), "deadline_epoch": budget["deadline_epoch"],
                   "outcomes": [{"slide_id": s["slide_id"], "status": outcomes[s["slide_id"]]} for s in slides],
                   "quality_gate_bypassed": False, "real_90min_acceptance": "REQUIRES_CURRENT_NATIVE_RUN"})
        return self.ppt_summary(job)

    def generate_ppt(self, slide_ids: list[str] | None = None) -> dict[str, Any]:
        if (self.inputs.get("ppt_plan",{}).get("execution_policy") or {}).get("profile")=="bounded_90min_v1":
            return self._generate_ppt_bounded(slide_ids)
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
            context = self.builder.ppt(slide, chapters[slide["chapter_id"]])
            if slide.get("profile_p_contract"):
                prior = []
                for row in slides:
                    if row["slide_id"] == sid:
                        break
                    unit_payload = self.ppt_root / "slides" / row["slide_id"] / "payload.json"
                    payload = read_json(unit_payload) if unit_payload.is_file() else {}
                    visual = payload.get("visual_data") or {}
                    nodes = [str(item.get("title") or "") for item in (visual.get("steps") or []) if isinstance(item, dict)]
                    nodes += [str(item.get("title") or "") for item in (payload.get("content_blocks") or []) if isinstance(item, dict)]
                    prior.append({"title": str(payload.get("headline") or row.get("slide_title") or ""),
                                  "core_message": str(payload.get("key_message") or row.get("core_message") or ""),
                                  "node_titles": [n for n in nodes if n][:6]})
                context["deck_context"] = {
                    "previous_pages": prior[-12:],
                    "instruction": "以下标题和主旨已在前页使用；本页不得复用同一标题、同一句正文或同一组模块动作，也不得把前页已讲完的主题再拆一页。",
                }
            write_json(unit / "input.json", context)
            if slide.get("evidence_requirement") == "SOURCE_REQUIRED":
                payload = {"slide_id": sid, "headline": slide["slide_title"], "subheadline": "等待真实案例来源", "key_message": "取得可核验案例资料后再补充本页内容", "content_blocks": [], "visual_data": {"status": "SOURCE_REQUIRED"}, "component_intent": slide["component_family_hint"], "speaker_note_optional": "本页不得编造案例。", "source_trace": [], "generation_notes": ["deferred_content"], "status": "SOURCE_REQUIRED"}
                gate = evaluate_ppt_payload(payload, slide, self.inputs["global_state"])
                write_json(unit / "payload.json", payload); write_json(unit / "governance.json", gate); write_json(status_path, {"slide_id": sid, "status": "SOURCE_REQUIRED", "updated_at": now_iso()})
                job.completed_units.append(sid); continue
            current_status = read_json(status_path).get("status") if status_path.is_file() else ""
            if current_status not in {"COMPLETED", "COMPLETED_WITH_WARNING", "SOURCE_REQUIRED"} and (unit / "provider_raw.json").is_file():
                candidate = read_json(unit / "provider_raw.json")
                if not slide.get("profile_p_contract"):
                    candidate = _repair_word_artifact(candidate, getattr(self, "editorial_source_facts", None))
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
                repairs = None if attempt == 1 else ["根据上次治理结果重新组织完整业务表达，不得删去必要步骤、条件或通过截断标题修复：" + json.dumps(final_gate or {},ensure_ascii=False)]
                prompt = self._ppt_prompt(context, repairs)
                request = {"task_id": f"{job.job_id}-{sid}-A{attempt}", "system_prompt": prompt["system"], "prompt": prompt["prompt"], "required_keys": sorted(PPT_REQUIRED_KEYS), "agent_max_turns": 1}
                if slide.get("profile_p_contract"):
                    request.update(generation_mode="ppt_slide", slide_id=sid)
                output, audit = self._invoke(request, unit / f"provider_attempt_{attempt}"); attempts.append({"attempt_id": attempt, **audit})
                if output is None: break
                output = {key: value for key, value in output.items() if key != "provider_metadata"}
                if not slide.get("profile_p_contract"):
                    output = _repair_word_artifact(output, getattr(self, "editorial_source_facts", None))
                write_json(unit / f"provider_raw_attempt_{attempt}.json", output)
                write_json(unit / "provider_raw.json", output)
                self._settle_request(
                    audit,
                    resolution="late_response" if audit.get("late_response_recovered") else "content_persisted",
                    persisted_content_unit_ids=[str(slide.get("slide_id") or "")],
                )
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
        repair_count = sum(max(0, len([row for row in attempts if row.get("attempt_id") and not row.get("recovery_only") and row.get("invoked") is not False]) - 1) for attempts in audits)
        return {"status": job.status, "provider_used": self.provider_name, "completed_slides": len(job.completed_units), "total_slides": job.total_units, "source_required": sum(row.get("status") == "SOURCE_REQUIRED" for row in statuses), "text_overflow": sum(bool(row.get("overflow")) for row in gates), "topic_overlap": sum(bool(row.get("topic_overlap")) for row in gates), "case_fabrication": sum(bool(row.get("case_fabrication")) for row in gates), "language_warning": sum(bool(row.get("language_warning")) for row in gates), "internal_id_leaks": sum(len(row.get("internal_id_leaks", [])) for row in gates), "repair_count": repair_count, "deferred_external_events": job.deferred_external_events}
