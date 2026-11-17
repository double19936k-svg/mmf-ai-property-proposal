"""Repair budgeting and repair severity for the long-task pipeline."""

from __future__ import annotations

import math
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


PRIORITY_BLOCK = "P0_BLOCK"
PRIORITY_SCORING_GAP = "SCORING_GAP"
PRIORITY_REQUIREMENT_GAP = "REQUIREMENT_GAP"
PRIORITY_DEPTH_GAP = "DEPTH_GAP"
PRIORITY_NEEDS_REVIEW = "NEEDS_REVIEW"

REPAIR_PRIORITY_ORDER = (
    PRIORITY_BLOCK,
    PRIORITY_SCORING_GAP,
    PRIORITY_REQUIREMENT_GAP,
    PRIORITY_DEPTH_GAP,
    PRIORITY_NEEDS_REVIEW,
)
DEFAULT_REPAIR_RATIO = 0.20
DEFAULT_REPAIR_CEILING = 10
DEFAULT_REPAIR_TIME_RATIO = 1.0
MAX_AUTO_REPAIR_PER_ISSUE = 1
# Round 2 §16：补深有它自己的一把上限。修复和补深抢同一个数字时，页面缺口永远排在
# 需求缺口后面，一次也不会真正发生 —— 上一轮 Supplement = 0 就是这么来的。
# 上限 16 次；比率取 0.30，让 40-53 次初稿调用的中型运行也落在 §16 建议的 12-16 次
# 区间内（0.22 在上一轮 48 次的规模上只给到 11 次，连候选队列都覆盖不完）。
# 每次调用最多打包 3 个 Content Unit，约 3 页，所以 12-16 次对应 36-48 页新增正文。
DEFAULT_SUPPLEMENT_RATIO = 0.30
DEFAULT_SUPPLEMENT_CEILING = 16


class GlobalRepairBudget:
    """One hard ceiling over every provider-consuming repair in the whole run.

    The per-section attempt counters made a 300-class run unbounded in aggregate:
    each Section could be re-sent a couple of times and the document still burned
    over a hundred calls.  Priority now decides who gets the remaining calls.

    Depth supplement keeps a separate, also-bounded ceiling: repairs fix what is
    wrong, supplements add what is missing, and neither may spend the other's
    allowance.  Both counters stay in one audit record so the total is visible.
    """

    def __init__(
        self,
        *,
        initial_calls: int,
        max_calls: int | None = None,
        ratio: float = DEFAULT_REPAIR_RATIO,
        ceiling: int = DEFAULT_REPAIR_CEILING,
        supplement_max_calls: int | None = None,
        supplement_ratio: float = DEFAULT_SUPPLEMENT_RATIO,
        supplement_ceiling: int = DEFAULT_SUPPLEMENT_CEILING,
        per_issue_limit: int = MAX_AUTO_REPAIR_PER_ISSUE,
        time_ratio_limit: float = DEFAULT_REPAIR_TIME_RATIO,
    ):
        self.initial_calls = max(0, int(initial_calls or 0))
        self.ratio = float(ratio)
        self.ceiling = int(ceiling)
        self.supplement_ratio = float(supplement_ratio)
        self.supplement_ceiling = int(supplement_ceiling)
        self.per_issue_limit = max(1, int(per_issue_limit or 1))
        self.time_ratio_limit = float(time_ratio_limit)
        self.formula = f"min({self.ceiling}, ceil(initial_calls * {self.ratio}))"
        self.supplement_formula = (f"min({self.supplement_ceiling}, "
                                   f"ceil(initial_calls * {self.supplement_ratio}))")
        self.max_calls = int(max_calls) if max_calls is not None else self.derive(
            self.initial_calls, ratio=self.ratio, ceiling=self.ceiling
        )
        self.supplement_max_calls = int(supplement_max_calls) if supplement_max_calls is not None else (
            self.derive_supplement(self.initial_calls, ratio=self.supplement_ratio,
                                   ceiling=self.supplement_ceiling)
        )
        self.repair_used = 0
        self.supplement_used = 0
        self.technical_retry_used = 0
        self.issue_attempts: dict[str, int] = {}
        self.issue_outcomes: dict[str, str] = {}
        self.stopped_reason: str | None = None
        self.storm_flags: list[dict[str, Any]] = []
        self.initial_seconds = 0.0
        self.repair_seconds = 0.0

    @staticmethod
    def derive(initial_calls: int, *, ratio: float = DEFAULT_REPAIR_RATIO, ceiling: int = DEFAULT_REPAIR_CEILING) -> int:
        return min(int(ceiling), int(math.ceil(max(0, int(initial_calls or 0)) * float(ratio))))

    @staticmethod
    def derive_supplement(initial_calls: int, *, ratio: float = DEFAULT_SUPPLEMENT_RATIO,
                          ceiling: int = DEFAULT_SUPPLEMENT_CEILING) -> int:
        return min(int(ceiling), int(math.ceil(max(0, int(initial_calls or 0)) * float(ratio))))

    @property
    def provider_used(self) -> int:
        return self.repair_used + self.supplement_used

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self.repair_used)

    @property
    def supplement_remaining(self) -> int:
        return max(0, self.supplement_max_calls - self.supplement_used)

    @property
    def storm_triggered(self) -> bool:
        return bool(self.stopped_reason) or bool(self.storm_flags)

    def allow(
        self,
        issue_key: str,
        *,
        priority: str = PRIORITY_NEEDS_REVIEW,
        technical_retry: bool = False,
        kind: str = "repair",
    ) -> tuple[bool, str]:
        if self.stopped_reason:
            return False, self.stopped_reason
        if technical_retry:
            return True, "TECHNICAL_RETRY_ALLOWED"
        if priority not in REPAIR_PRIORITY_ORDER:
            return False, "UNKNOWN_PRIORITY"
        attempts = int(self.issue_attempts.get(str(issue_key) or "") or 0)
        if attempts >= self.per_issue_limit:
            return False, "ISSUE_ALREADY_REPAIRED"
        if str(kind or "repair") == "supplement":
            if self.supplement_used >= self.supplement_max_calls:
                return False, "SUPPLEMENT_BUDGET_EXHAUSTED"
            return True, "ALLOWED"
        if self.repair_used >= self.max_calls:
            return False, "GLOBAL_REPAIR_BUDGET_EXHAUSTED"
        return True, "ALLOWED"

    def note_attempt(self, issue_key: str, *, kind: str = "repair") -> None:
        key = str(issue_key or "")
        self.issue_attempts[key] = int(self.issue_attempts.get(key, 0)) + 1
        if kind == "supplement":
            self.supplement_used += 1
        else:
            self.repair_used += 1

    def note_technical_retry(self) -> None:
        self.technical_retry_used += 1

    def note_outcome(self, issue_key: str, *, repaired: bool) -> None:
        self.issue_outcomes[str(issue_key or "")] = "REPAIRED" if repaired else "STILL_OPEN"
        if not repaired and str(issue_key or "") in self.issue_attempts:
            self.issue_outcomes.setdefault(f"{issue_key}:attempts", str(self.issue_attempts[issue_key]))

    def observe_time(self, *, initial_seconds: float, repair_seconds: float) -> None:
        self.initial_seconds = max(0.0, float(initial_seconds or 0.0))
        self.repair_seconds = max(0.0, float(repair_seconds or 0.0))
        if self.initial_seconds and self.repair_seconds > self.initial_seconds * self.time_ratio_limit:
            flag = {
                "reason": "REPAIR_TIME_EXCEEDED_GENERATION_TIME",
                "initial_seconds": round(self.initial_seconds, 3),
                "repair_seconds": round(self.repair_seconds, 3),
                "limit_ratio": self.time_ratio_limit,
            }
            if flag not in self.storm_flags:
                self.storm_flags.append(flag)

    def observe_repeated_failure(self, issue_key: str) -> None:
        key = str(issue_key or "")
        if int(self.issue_attempts.get(key, 0)) > self.per_issue_limit:
            flag = {"reason": "SAME_ISSUE_REPEATED", "issue_key": key, "attempts": self.issue_attempts[key]}
            if flag not in self.storm_flags:
                self.storm_flags.append(flag)

    def stop(self, reason: str) -> None:
        self.stopped_reason = self.stopped_reason or reason

    def audit(self) -> dict[str, Any]:
        return {
            "formula": self.formula,
            "initial_provider_calls": self.initial_calls,
            "max_repair_calls": self.max_calls,
            "repair_calls_used": self.repair_used,
            "supplement_formula": self.supplement_formula,
            "max_supplement_calls": self.supplement_max_calls,
            "supplement_calls_used": self.supplement_used,
            "supplement_remaining_calls": self.supplement_remaining,
            "provider_calls_used": self.provider_used,
            "technical_retry_calls_used": self.technical_retry_used,
            "remaining_calls": self.remaining,
            "per_issue_limit": self.per_issue_limit,
            "issue_attempts": dict(self.issue_attempts),
            "issue_outcomes": dict(self.issue_outcomes),
            "storm_triggered": self.storm_triggered,
            "storm_flags": list(self.storm_flags),
            "stopped_reason": self.stopped_reason,
            "initial_generation_seconds": round(self.initial_seconds, 3),
            "repair_seconds": round(self.repair_seconds, 3),
            "repair_over_initial": bool(self.initial_seconds and self.repair_seconds > self.initial_seconds),
        }


def sort_repair_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order = {name: index for index, name in enumerate(REPAIR_PRIORITY_ORDER)}
    return sorted(
        candidates,
        key=lambda row: (order.get(str(row.get("priority") or ""), len(order)), -int(row.get("value_score") or 0)),
    )


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
