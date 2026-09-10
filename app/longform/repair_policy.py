"""Fast-mode repair budget, under-length severity, and compact repair context."""

from __future__ import annotations

from typing import Any

SEVERITY_OK = "OK"
SEVERITY_MINOR = "MINOR_UNDER_LENGTH"
SEVERITY_MATERIAL = "MATERIAL_UNDER_LENGTH"
SEVERITY_CRITICAL = "CRITICAL_UNDER_LENGTH"
SEVERITY_QUALITY = "QUALITY_FAILURE"

ACTION_TOKENS = ("负责", "执行", "安排", "实施", "组织", "配置", "巡查", "检查")
ROLE_TOKENS = ("项目负责人", "主管", "值班", "客服", "工程", "秩序")
PROCESS_TOKENS = ("流程", "步骤", "闭环", "升级", "响应", "交接")
RECORD_TOKENS = ("记录", "台账", "档案", "报告", "回访")
EXCEPTION_TOKENS = ("异常", "应急", "升级", "突发事件", "故障")


def _ratio(gate: dict[str, Any], contract: dict[str, Any]) -> float:
    target_min = int((contract.get("target_words") or {}).get("min") or 0)
    units = int(gate.get("content_units") or 0)
    if target_min <= 0:
        return 1.0
    return units / target_min


def _must_missing(gate: dict[str, Any]) -> bool:
    return any(value == "MISSING" for value in (gate.get("must_cover") or {}).values())


def _output_missing(gate: dict[str, Any]) -> bool:
    missing = gate.get("required_outputs_missing") or []
    return bool(missing)


def _text_blob(gate: dict[str, Any], fragment: dict[str, Any] | None = None) -> str:
    parts = [str(gate.get("issue") or "")]
    if fragment:
        parts.append(str(fragment.get("title") or ""))
        for block in fragment.get("body_blocks") or []:
            if isinstance(block, dict):
                parts.append(str(block.get("content") or ""))
            else:
                parts.append(str(block))
    return "\n".join(parts)


def _missing_dimensions(text: str) -> list[str]:
    missing = []
    if not any(token in text for token in ACTION_TOKENS):
        missing.append("action")
    if not any(token in text for token in ROLE_TOKENS):
        missing.append("role")
    if not any(token in text for token in PROCESS_TOKENS):
        missing.append("process")
    if not any(token in text for token in RECORD_TOKENS):
        missing.append("record")
    return missing


def classify_under_length(gate: dict[str, Any], contract: dict[str, Any], *, fragment: dict[str, Any] | None = None) -> str:
    if not gate.get("meaningful_body"):
        return SEVERITY_CRITICAL
    ratio = _ratio(gate, contract)
    if _must_missing(gate) or _output_missing(gate) or ratio < 0.40:
        return SEVERITY_CRITICAL
    if gate.get("length_status") != "SECTION_UNDER_LENGTH":
        return SEVERITY_OK
    if ratio >= 0.70 and gate.get("contract_complete"):
        return SEVERITY_MINOR
    text = _text_blob(gate, fragment)
    if 0.40 <= ratio < 0.70 and (not gate.get("contract_complete") or _missing_dimensions(text)):
        return SEVERITY_MATERIAL
    if ratio >= 0.70:
        return SEVERITY_MINOR
    return SEVERITY_MATERIAL


def classify_section_issue(
    gate: dict[str, Any],
    contract: dict[str, Any],
    failure_class: str | None,
    *,
    fragment: dict[str, Any] | None = None,
    speed_profile: str = "balanced",
) -> dict[str, Any]:
    kind = str(failure_class or "")
    under = classify_under_length(gate, contract, fragment=fragment)
    fact = bool(gate.get("fact_drift"))
    commitment_block = (gate.get("commitment") or {}).get("status") == "BLOCK"
    empty = not gate.get("meaningful_body")
    must_missing = _must_missing(gate)
    critical = under == SEVERITY_CRITICAL or empty or must_missing or fact
    fast = speed_profile == "fast"
    if critical:
        return {
            "severity": SEVERITY_CRITICAL,
            "failure_class": "EMPTY_BODY" if empty else (kind or "UNDER_LENGTH"),
            "max_provider_repairs": 2,
            "can_exceed_global": True,
            "value_score": 90,
            "under_length_level": under,
        }
    if under == SEVERITY_MATERIAL:
        return {
            "severity": SEVERITY_MATERIAL,
            "failure_class": "UNDER_LENGTH",
            "max_provider_repairs": 1 if fast else 2,
            "can_exceed_global": False,
            "value_score": 45,
            "under_length_level": under,
        }
    if under == SEVERITY_MINOR:
        return {
            "severity": SEVERITY_MINOR,
            "failure_class": "UNDER_LENGTH",
            "max_provider_repairs": 0 if fast else 1,
            "can_exceed_global": False,
            "value_score": 15,
            "under_length_level": under,
        }
    if gate.get("status") == "BLOCK" or kind in {"QUALITY_FAILURE", "OUTPUT_CONTRACT_ERROR"} or commitment_block:
        return {
            "severity": SEVERITY_QUALITY,
            "failure_class": kind or "QUALITY_FAILURE",
            "max_provider_repairs": 1 if fast else 2,
            "can_exceed_global": bool(commitment_block),
            "value_score": 55 if commitment_block else 40,
            "under_length_level": under,
        }
    return {
        "severity": SEVERITY_OK,
        "failure_class": kind or "NONE",
        "max_provider_repairs": 0,
        "can_exceed_global": False,
        "value_score": 0,
        "under_length_level": under,
    }


class FastRepairBudget:
    def __init__(self, *, speed_profile: str, initial_batch_count: int):
        self.speed_profile = speed_profile
        self.initial_batch_count = max(0, int(initial_batch_count or 0))
        if speed_profile == "fast":
            self.max_provider = max(1, round(self.initial_batch_count * 0.3)) if self.initial_batch_count else 4
        else:
            self.max_provider = 10**6
        self.used = 0
        self.skipped_low_value = 0
        self.escalated_critical = 0
        self.local_repair_count = 0

    def allow(self, decision: dict[str, Any], *, previous_repairs: int, elapsed_seconds: float = 0.0) -> bool:
        max_local = int(decision.get("max_provider_repairs") or 0)
        if previous_repairs >= max_local:
            return False
        if self.speed_profile == "fast" and previous_repairs >= 1 and decision.get("severity") != SEVERITY_CRITICAL:
            return False
        if decision.get("can_exceed_global") or decision.get("severity") == SEVERITY_CRITICAL:
            self.escalated_critical += 1
            return True
        if self.speed_profile == "fast" and elapsed_seconds >= 1200 and decision.get("value_score", 0) < 80:
            return False
        if self.used >= self.max_provider:
            return False
        return max_local > 0

    def consume(self) -> None:
        self.used += 1


def compact_generation_context(context: dict[str, Any]) -> dict[str, Any]:
    packed = dict(context)
    arguments = list(packed.get("used_core_arguments") or [])
    if len(arguments) > 12:
        packed["used_core_arguments"] = arguments[-12:]
    return packed


def compact_repair_context(
    context: dict[str, Any],
    gate: dict[str, Any],
    *,
    previous_fragment: dict[str, Any] | None,
    failure_class: str,
) -> dict[str, Any]:
    contract = context.get("section_contract") or {}
    must = contract.get("must_cover") or []
    missing_must = [item for item, status in (gate.get("must_cover") or {}).items() if status == "MISSING"]
    return {
        "section_id": contract.get("section_id"),
        "section_title": contract.get("section_title"),
        "project_facts": context.get("project_facts") or {},
        "failed_checks": {
            "failure_class": failure_class,
            "must_missing": missing_must,
            "process_missing": gate.get("process_missing") or [],
            "required_outputs_missing": gate.get("required_outputs_missing") or [],
            "length_status": gate.get("length_status"),
            "content_units": gate.get("content_units"),
            "commitment_status": (gate.get("commitment") or {}).get("status"),
        },
        "must_cover": must,
        "required_outputs": contract.get("required_outputs") or [],
        "target_words": contract.get("target_words") or {},
        "confirmed_requirements": [
            row for row in (context.get("confirmed_requirements") or [])
            if any(str(item) in str(row) for item in missing_must) or not missing_must
        ][:8],
        "canonical_roles": context.get("canonical_roles") or [],
        "previous_fragment": previous_fragment or {},
    }


def repair_prompt_rules(failure_class: str) -> list[str]:
    return [
        "保留现有有效正文，只补充失败项，禁止整章重写",
        "不得把已合格段落再生成一遍",
        "不得引入招标文件全文或未给出的项目数字",
        "仅针对failed_checks列出的缺口补写",
        "UNDER_LENGTH时在previous_fragment之后续写，不得删除已有内容" if failure_class == "UNDER_LENGTH" else "优先补覆盖、责任、流程、记录与异常路径",
    ]


def contains_full_tender(text: str) -> bool:
    blob = str(text or "")
    return blob.count("招标文件") >= 3 and len(blob) > 20000
