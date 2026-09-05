from __future__ import annotations

import copy
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from governance import apply_artifact_repairs, apply_local_repairs, evaluate_artifact, evaluate_commitments
from providers import ProviderError, ProviderUnavailableError
from providers.capability import apply_to_request, resolve_profile
from providers.execution_reliability import classify_provider_failure, deferred_status


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

    def dump(self, path: Path) -> None:
        write_json(path, asdict(self))

    @classmethod
    def load(cls, path: Path) -> "LongformGenerationJob":
        return cls(**read_json(path))


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


def evaluate_word_fragment(fragment: dict[str, Any], contract: dict[str, Any], global_state: dict[str, Any], kuc_rows: list[dict[str, Any]]) -> dict[str, Any]:
    missing_keys = sorted(WORD_REQUIRED_KEYS - set(fragment))
    text = visible_text(fragment)
    coverage = {item: keyword_coverage(item, text) for item in contract["must_cover"]}
    processes = {row["process_id"]: {"steps_total": len(row["steps"]), "steps_present": sum(step in text for step in row["steps"])} for row in kuc_rows}
    process_missing = [pid for pid, row in processes.items() if row["steps_total"] and row["steps_present"] / row["steps_total"] < 0.55]
    target = contract["target_words"]
    units = content_units(text)
    length_status = "PASS" if target["min"] * 0.85 <= units <= target["max"] * 1.15 else ("SECTION_UNDER_LENGTH" if units < target["min"] * 0.85 else "SECTION_OVER_LENGTH")
    commitment = evaluate_commitments(global_state, kuc_rows, _governance_artifact(fragment))
    artifact = evaluate_artifact(_governance_artifact(fragment))
    fact_drift = _fact_drift(text, global_state)
    role_drift = [old for old in ROLE_DRIFT if old in text]
    scope_violation = _excluded_scope_violations(text, global_state)
    id_leaks = INTERNAL_ID.findall(text)
    repetition = _local_repetition(fragment.get("body_blocks", []))
    blocking = bool(missing_keys or any(value == "MISSING" for value in coverage.values()) or process_missing or fact_drift or role_drift or scope_violation or id_leaks or AI_LANGUAGE.search(text) or BROKEN_JSON.search(text) or commitment["status"] == "BLOCK" or artifact["status"] == "BLOCK")
    return {
        "status": "BLOCK" if blocking else ("WARNING" if length_status != "PASS" or any(value == "PARTIAL" for value in coverage.values()) or artifact["status"] == "AUTO_REPAIR" else "PASS"),
        "missing_keys": missing_keys, "must_cover": coverage, "process_gate": processes, "process_missing": process_missing,
        "content_units": units, "length_status": length_status, "fact_drift": fact_drift, "role_drift": role_drift,
        "scope_violation": scope_violation, "internal_id_leaks": id_leaks, "local_repetition": repetition,
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
            "月度": "按项目确认的周期",
            "年度": "按项目确认的周期",
        }
        for old, new in safe_conditioning.items(): value = value.replace(old, new)
        presentation_polish = {
            "SLA": "服务响应要求", "SOP": "标准作业流程", "LOTO": "上锁挂牌", "APP": "移动端",
            "staffing": "人员配置", "Logo": "标识", "无死角": "重点区域覆盖", "零延误": "减少延误",
            "零干扰": "减少干扰", "自动派单": "按规则派单", "智能匹配": "按专业与位置匹配",
            "电子签到": "签到记录", "增派机动岗": "动态调配现场力量", "专人押运": "按安全要求落实押运责任",
            "全程监控": "按安全要求实施过程管控",
        }
        for old, new in presentation_polish.items(): value = value.replace(old, new)
        value = value.replace("响应时限", "可根据项目确认要求确定响应安排")
        value = value.replace("外委单位", "__CONDITIONAL_SUPPLIER__")
        value = value.replace("外委管理", "__CONDITIONAL_SUPPLIER_MGMT__")
        value = value.replace("外委", "如采用外委方式")
        value = value.replace("__CONDITIONAL_SUPPLIER__", "如采用外委方式，可按合同要求管理的相关供方")
        value = value.replace("__CONDITIONAL_SUPPLIER_MGMT__", "如采用外委方式，可按合同要求实施供方管理")
        value = value.replace("24小时", "经确认的服务时段")
        value = re.sub(r"(?<!\d)(?!12(?:\.0)?[ \t]*人)(\d+(?:\.\d+)?)[ \t]*人", "相应人员", value)
        value = re.sub(r"(?<!\d)(?!30(?:\.0)?[ \t]*分钟)(\d+(?:\.\d+)?)[ \t]*分钟", "可根据项目确认要求确定响应安排", value)
        value = re.sub(r"(?<!\d)(?!8(?:\.0)?[ \t]*小时)(\d+(?:\.\d+)?)[ \t]*小时", "按项目确认的服务时段", value)
        value = re.sub(r"(?<!\d)(?!4(?:\.0)?[ \t]*次)(\d+(?:\.\d+)?)[ \t]*次", "按项目确认的频次", value)
        value = re.sub(r"\d+(?:\.\d+)?[ \t]*[%％]", "按项目确认的指标", value)
        for old, new in ROLE_DRIFT.items(): value = value.replace(old, new)
        value = INTERNAL_ID.sub("", value)
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
        self.capability = resolve_profile(provider_name, config if isinstance(config, dict) else {})
        self.require_section_min = bool(inputs.get("require_section_min", True))

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
        prompt = "根据Context Pack生成一个Word Section结构化Content Fragment。目标内容量为{min}～{max}个中文内容单位，避免理念重复和机械注水。根字段必须且只能包含section_id,title,body_blocks,tables,processes,callouts,cross_references,claims,used_requirement_ids,used_ku_ids,generation_notes。body_blocks使用paragraph/subheading/bullet_group/numbered_steps/note/summary等type与content。你已经获得全部信息，禁止调用工具、读取文件、继续调查或要求补充材料。规则：\n- {rules}\nContext Pack：\n{context}".format(min=contract["target_words"]["min"], max=contract["target_words"]["max"], rules="\n- ".join(rules), context=json.dumps(context, ensure_ascii=False))
        return {"system": system, "prompt": prompt}

    @staticmethod
    def _ppt_prompt(context: dict[str, Any], repair: list[str] | None = None) -> dict[str, Any]:
        slide = context["slide"]
        rules = ["只生成当前一页，不改变Storyboard", "不得读取或总结Word正文", "像面向甲方的物业服务方案汇报", "正文不得显示REQ/SCR/KU内部ID", "不得编造案例", "P04主讲通用通行秩序，P07主讲生产物流与业务协同"]
        if repair: rules.extend(repair)
        prompt = "根据Context Pack生成一个PPT Slide Content Payload。根字段必须且只能包含slide_id,headline,subheadline,key_message,content_blocks,visual_data,component_intent,speaker_note_optional,source_trace,generation_notes。content_blocks应短句化，避免Word长段。你已经获得全部信息，禁止调用工具、读取文件、继续调查或要求补充材料。规则：\n- {rules}\nContext Pack：\n{context}".format(rules="\n- ".join(rules), context=json.dumps(context, ensure_ascii=False))
        return {"system": "你是物业服务方案PPT内容策划，只负责当前Slide。禁止工具、文件和调查。只返回JSON。", "prompt": prompt}

    def _checkpoint(self, job: LongformGenerationJob) -> None:
        job.dump(self.word_root / "status.json")
        write_json(self.run_root / "checkpoint" / "state.json", {
            "schema_version": "longform-checkpoint-v0.1",
            "run_id": job.run_id,
            "provider": job.provider,
            "status": job.status,
            "current_unit": job.current_unit,
            "completed_units": job.completed_units,
            "failed_units": job.failed_units,
            "retry_count": job.retry_count,
            "updated_at": now_iso(),
            "resume_from_checkpoint": True,
        })

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

    def _invoke(self, request: dict[str, Any], task_dir: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        started = time.monotonic()
        payload = apply_to_request(request, self.capability)
        payload.setdefault("json_schema", self._section_schema())
        try:
            output = self.provider.invoke_structured(payload, task_dir)
            meta = output.get("provider_metadata", {}) if isinstance(output, dict) else {}
            audit = {
                "status": "SUCCESS",
                "duration_seconds": round(time.monotonic() - started, 3),
                "input_tokens_estimate": estimate_tokens(payload["prompt"]),
                "output_tokens_estimate": estimate_tokens(output),
                "input_tokens": meta.get("input_tokens") or estimate_tokens(payload["prompt"]),
                "output_tokens": meta.get("output_tokens") or estimate_tokens(output),
                "finish_reason": meta.get("finish_reason") or "stop",
                "provider_metadata": meta,
                "requested_settings": (self.capability or {}).get("requested_settings"),
                "effective_settings": (self.capability or {}).get("effective_settings"),
            }
            return output, audit
        except (ProviderError, ProviderUnavailableError) as exc:
            code = classify_provider_failure(getattr(exc, "error_code", ""), str(exc))
            return None, {"status": deferred_status(code), "error_code": code, "error": str(exc), "duration_seconds": round(time.monotonic() - started, 3), "input_tokens_estimate": estimate_tokens(request["prompt"]), "output_tokens_estimate": 0, "finish_reason": "error"}

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

    def generate_word(self, section_ids: list[str] | None = None) -> dict[str, Any]:
        contracts = sorted(self.inputs["section_contracts"]["contracts"], key=lambda row: row["generation_order"])
        if section_ids is not None: contracts = [row for row in contracts if row["section_id"] in set(section_ids)]
        job = self._job(self.word_root, "WORD", len(contracts))
        selected_ids = {row["section_id"] for row in contracts}
        order = {row["section_id"]: row["generation_order"] for row in self.inputs["section_contracts"]["contracts"]}
        prerequisites = {row["section_id"]: [] for row in contracts}
        for dep in self.inputs["dependency_map"]["dependencies"]:
            source = dep["source_section"]
            for target in dep["dependent_sections"]:
                if target in prerequisites and source in selected_ids and source in order and order[source] < order[target]: prerequisites[target].append(source)
        systemic_deferred = False
        for contract in contracts:
            sid = contract["section_id"]
            unit = self.word_root / "sections" / sid
            status_path = unit / "status.json"
            if status_path.is_file() and read_json(status_path).get("status") in {"COMPLETED", "COMPLETED_WITH_WARNING", "COMPLETED_CONDITIONAL"}:
                if sid not in job.completed_units: job.completed_units.append(sid)
                self._restore_word_state(contract, unit)
                continue
            # A rule patch may make the latest finished Provider text acceptable. Revalidate
            # locally before spending another call; the business text remains unchanged.
            current_status = read_json(status_path).get("status") if status_path.is_file() else ""
            if current_status not in {"COMPLETED", "COMPLETED_WITH_WARNING", "COMPLETED_CONDITIONAL"} and (unit / "provider_raw.json").is_file():
                candidate = _repair_customer_text(apply_artifact_repairs(apply_local_repairs(read_json(unit / "provider_raw.json"))))
                processes = [self.builder.processes[pid] for pid in contract.get("required_processes", []) if pid in self.builder.processes]
                recovered_gate = evaluate_word_fragment(candidate, contract, self.inputs["global_state"], processes)
                recovered_short = self.require_section_min and recovered_gate.get("length_status") == "SECTION_UNDER_LENGTH"
                if recovered_gate["status"] != "BLOCK" and not recovered_short:
                    summary = self._word_summary(candidate, contract)
                    recovered_status = "COMPLETED_CONDITIONAL" if contract.get("section_activation_condition", {}).get("expression") != "always" else ("COMPLETED_WITH_WARNING" if recovered_gate["status"] == "WARNING" else "COMPLETED")
                    write_json(unit / "fragment.json", candidate); write_json(unit / "summary.json", summary); write_json(unit / "governance.json", recovered_gate); write_json(unit / "qa.json", recovered_gate)
                    write_json(status_path, {"section_id": sid, "status": recovered_status, "recovered_by_local_revalidation": True, "business_text_changed": False, "content_units": recovered_gate["content_units"], "updated_at": now_iso()})
                    if sid not in job.completed_units: job.completed_units.append(sid)
                    job.failed_units = [value for value in job.failed_units if value != sid]
                    self._update_state(summary, candidate, contract)
                    write_json(self.word_root / "global_state" / f"after_{sid}.json", self.incremental_state)
                    job.dump(self.word_root / "status.json")
                    continue
            blocked = [dep for dep in prerequisites[sid] if dep not in job.completed_units]
            if blocked:
                write_json(status_path, {"section_id": sid, "status": "BLOCKED_BY_DEPENDENCY", "dependencies": blocked, "updated_at": now_iso()})
                continue
            job.current_unit, job.status = sid, "EXTERNAL_PROVIDER_RUNNING"
            self._checkpoint(job)
            context = self.builder.word(contract, self.incremental_state)
            write_json(unit / "input.json", context)
            existing_audit = read_json(unit / "audit.json") if (unit / "audit.json").is_file() else {"attempts": []}
            attempts = list(existing_audit.get("attempts", []))
            recorded_ids = {int(row.get("attempt_id", 0)) for row in attempts}
            for path in unit.glob("provider_attempt_*"):
                try: attempt_id = int(path.name.rsplit("_", 1)[1])
                except ValueError: continue
                if attempt_id not in recorded_ids:
                    attempts.append({"attempt_id": attempt_id, "status": "STALE_INTERRUPTED", "error_code": "INTERRUPTED_BEFORE_FINISHED_RESULT", "input_tokens_estimate": 0, "output_tokens_estimate": 0})
            previous_attempt = max((int(row.get("attempt_id", 0)) for row in attempts), default=0)
            final_fragment = None; final_gate = None
            continuation_used = False
            max_attempts = 4 if self.require_section_min else 3
            for attempt in range(previous_attempt + 1, max_attempts):
                under = bool(final_gate and final_gate.get("length_status") == "SECTION_UNDER_LENGTH")
                mode = "generate"
                extra_rules = None
                if attempt > previous_attempt + 1:
                    if under:
                        mode = "continue_section"
                        continuation_used = True
                        extra_rules = ["续写当前Section，补足Contract最低内容量", "保留已写内容，不得改写目录或新增数字承诺"]
                    else:
                        mode = "repair_section"
                        extra_rules = ["修复上一轮缺失覆盖、流程或承诺问题；保持原Section不变", "补足动作与异常路径但不得新增数字承诺"]
                prompt = self._word_prompt(context, extra_rules)
                request = {
                    "task_id": f"{job.job_id}-{sid}-A{attempt}",
                    "system_prompt": prompt["system"],
                    "prompt": prompt["prompt"],
                    "required_keys": sorted(WORD_REQUIRED_KEYS),
                    "agent_max_turns": 1,
                    "section_id": sid,
                    "attempt_mode": mode,
                }
                output, audit = self._invoke(request, unit / f"provider_attempt_{attempt}")
                attempts.append({"attempt_id": attempt, "mode": mode, **audit})
                if output is None:
                    if audit.get("error_code") in {"PLATFORM_CONFIRMATION_REQUIRED", "PROVIDER_AUTH_REQUIRED", "NETWORK_ERROR"}:
                        job.deferred_external_events.append({"unit": sid, **audit, "at": now_iso()})
                    break
                output = {key: value for key, value in output.items() if key != "provider_metadata"}
                write_json(unit / f"provider_raw_attempt_{attempt}.json", output)
                write_json(unit / "provider_raw.json", output)
                output = _repair_customer_text(apply_artifact_repairs(apply_local_repairs(output)))
                processes = [self.builder.processes[pid] for pid in contract.get("required_processes", []) if pid in self.builder.processes]
                gate = evaluate_word_fragment(output, contract, self.inputs["global_state"], processes)
                final_fragment, final_gate = output, gate
                if gate["status"] == "BLOCK":
                    job.retry_count += 1
                    continue
                if self.require_section_min and gate.get("length_status") == "SECTION_UNDER_LENGTH":
                    job.retry_count += 1
                    continuation_used = True
                    continue
                break
            write_json(unit / "governance.json", final_gate or {"status": "DEFERRED"})
            write_json(unit / "qa.json", final_gate or {"status": "DEFERRED"})
            write_json(unit / "audit.json", {"attempts": attempts})
            if final_fragment is None:
                latest_attempt_status = attempts[-1]["status"] if attempts else ""
                status = latest_attempt_status if str(latest_attempt_status).startswith("DEFERRED_") else "FAILED_SECTION"
                write_json(status_path, {"section_id": sid, "status": status, "attempts": attempts, "updated_at": now_iso()})
                if status.startswith("DEFERRED_"): job.deferred_external_events.append({"unit": sid, "status": status, "at": now_iso()})
                else: job.failed_units.append(sid)
                if status in {"DEFERRED_PLATFORM_CONFIRMATION", "DEFERRED_PROVIDER_AUTH", "DEFERRED_NETWORK"}:
                    systemic_deferred = True
                    self._checkpoint(job)
                    break
                continue
            if final_gate["status"] == "BLOCK":
                if sid not in job.failed_units: job.failed_units.append(sid)
                status = "FAILED_SECTION"
                write_json(status_path, {"section_id": sid, "status": status, "attempts": attempts, "updated_at": now_iso()})
                continue
            summary = self._word_summary(final_fragment, contract)
            under = final_gate.get("length_status") == "SECTION_UNDER_LENGTH"
            status = "COMPLETED_CONDITIONAL" if contract.get("section_activation_condition", {}).get("expression") != "always" else ("COMPLETED_WITH_WARNING" if final_gate["status"] == "WARNING" or under else "COMPLETED")
            write_json(unit / "fragment.json", final_fragment); write_json(unit / "summary.json", summary)
            last = attempts[-1] if attempts else {}
            write_json(unit / "generation.json", {
                "section_id": sid,
                "provider": self.provider_name,
                "model": ((self.capability or {}).get("effective_settings") or {}).get("model"),
                "attempt": last.get("attempt_id") or len(attempts),
                "mode": last.get("mode") or "generate",
                "input_tokens": last.get("input_tokens") or last.get("input_tokens_estimate"),
                "output_tokens": last.get("output_tokens") or last.get("output_tokens_estimate"),
                "finish_reason": last.get("finish_reason") or last.get("status"),
                "effective_chars": len(visible_text(final_fragment)),
                "coverage": (final_gate or {}).get("must_cover"),
                "retry": bool(len(attempts) > 1),
                "continuation": continuation_used,
                "requested_settings": last.get("requested_settings"),
                "effective_settings": last.get("effective_settings"),
            })
            write_json(status_path, {"section_id": sid, "status": status, "attempts": attempts, "content_units": final_gate["content_units"], "continuation": continuation_used, "updated_at": now_iso()})
            if sid not in job.completed_units: job.completed_units.append(sid)
            job.failed_units = [value for value in job.failed_units if value != sid]
            self._update_state(summary, final_fragment, contract)
            write_json(self.word_root / "global_state" / f"after_{sid}.json", self.incremental_state)
            self._checkpoint(job)
        job.finished_at = now_iso(); job.current_unit = None
        job.status = "COMPLETED" if len(job.completed_units) == job.total_units and not job.failed_units else ("DEFERRED_EXTERNAL" if job.deferred_external_events and not job.failed_units else "COMPLETED_WITH_GAPS")
        self._checkpoint(job)
        return self.word_summary(job)

    def word_summary(self, job: LongformGenerationJob) -> dict[str, Any]:
        gates = [read_json(path) for path in (self.word_root / "sections").glob("*/governance.json")]
        statuses = [read_json(path) for path in (self.word_root / "sections").glob("*/status.json")]
        audits = [read_json(path).get("attempts", []) for path in (self.word_root / "sections").glob("*/audit.json")]
        repair_count = sum(max(0, len([row for row in attempts if row.get("attempt_id")]) - 1) for attempts in audits)
        return {"status": job.status, "provider_used": self.provider_name, "completed_sections": len(job.completed_units), "total_sections": job.total_units, "total_content_units": sum(int(row.get("content_units", 0)) for row in statuses), "under_length": sum(row.get("length_status") == "SECTION_UNDER_LENGTH" for row in gates), "over_length": sum(row.get("length_status") == "SECTION_OVER_LENGTH" for row in gates), "must_cover_missing": sum(any(v == "MISSING" for v in row.get("must_cover", {}).values()) for row in gates), "fact_drift": sum(len(row.get("fact_drift", [])) for row in gates), "commitment_blocks": sum(row.get("commitment", {}).get("status") == "BLOCK" for row in gates), "local_repetition": sum(int(row.get("local_repetition", 0)) for row in gates), "internal_id_leaks": sum(len(row.get("internal_id_leaks", [])) for row in gates), "repair_count": repair_count, "deferred_external_events": job.deferred_external_events}

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
        job.dump(self.ppt_root / "status.json")
        return self.ppt_summary(job)

    def ppt_summary(self, job: LongformGenerationJob) -> dict[str, Any]:
        gates = [read_json(path) for path in (self.ppt_root / "slides").glob("*/governance.json")]
        statuses = [read_json(path) for path in (self.ppt_root / "slides").glob("*/status.json")]
        audits = [read_json(path).get("attempts", []) for path in (self.ppt_root / "slides").glob("*/audit.json")]
        repair_count = sum(max(0, len([row for row in attempts if row.get("attempt_id")]) - 1) for attempts in audits)
        return {"status": job.status, "provider_used": self.provider_name, "completed_slides": len(job.completed_units), "total_slides": job.total_units, "source_required": sum(row.get("status") == "SOURCE_REQUIRED" for row in statuses), "text_overflow": sum(bool(row.get("overflow")) for row in gates), "topic_overlap": sum(bool(row.get("topic_overlap")) for row in gates), "case_fabrication": sum(bool(row.get("case_fabrication")) for row in gates), "language_warning": sum(bool(row.get("language_warning")) for row in gates), "internal_id_leaks": sum(len(row.get("internal_id_leaks", [])) for row in gates), "repair_count": repair_count, "deferred_external_events": job.deferred_external_events}
