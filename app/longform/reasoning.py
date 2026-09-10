from __future__ import annotations

from typing import Any


SPEED_PROFILES = {
    "fast": {
        "id": "fast",
        "label": "快速",
        "summary": "适合内部初稿、快速验证和普通方案，优先控制等待时间和成本。",
        "draft_product_level": "low",
        "recommended": False,
    },
    "balanced": {
        "id": "balanced",
        "label": "均衡",
        "summary": "质量、时间和成本平衡，默认推荐。",
        "draft_product_level": "medium",
        "recommended": True,
    },
    "deep": {
        "id": "deep",
        "label": "深度",
        "summary": "适合复杂项目、重要方案和高风险章节，允许更高推理成本。",
        "draft_product_level": "high",
        "recommended": False,
    },
}

PRODUCT_LEVELS = ("low", "medium", "high", "xhigh")
QUALITY_FAILURES = {
    "QUALITY_FAILURE",
    "REQUIREMENT_MISS",
    "CONTRADICTION",
    "COMMITMENT_PROBLEM",
}
LATENCY_FAILURES = {"LATENCY_FAILURE", "TIMEOUT", "SOFT_LATENCY_EXCEEDED"}


def normalize_speed_profile(value: Any) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "快速": "fast",
        "fast": "fast",
        "quick": "fast",
        "均衡": "balanced",
        "balanced": "balanced",
        "standard": "balanced",
        "推荐": "balanced",
        "深度": "deep",
        "deep": "deep",
        "thorough": "deep",
    }
    return aliases.get(raw, "balanced")


def product_speed_catalog() -> list[dict[str, Any]]:
    return [dict(row) for row in SPEED_PROFILES.values()]


def _nearest_allowed(level: str, allowed: list[str]) -> str:
    if not allowed:
        return level
    if level in allowed:
        return level
    order = list(PRODUCT_LEVELS)
    idx = order.index(level) if level in order else 0
    return sorted(allowed, key=lambda item: abs((order.index(item) if item in order else 99) - idx))[0]


def _shift_level(level: str, delta: int, allowed: list[str]) -> str:
    if not allowed:
        return level
    current = _nearest_allowed(level, allowed)
    idx = allowed.index(current)
    return allowed[max(0, min(len(allowed) - 1, idx + delta))]


def policy_reasoning_for_stage(speed_profile: str, stage: str) -> str:
    profile = SPEED_PROFILES[normalize_speed_profile(speed_profile)]
    stage_name = str(stage or "draft").lower()
    if stage_name in {"planning", "plan"}:
        return "high"
    if stage_name in {"complex_repair", "commitment_repair"}:
        return "high"
    return profile["draft_product_level"]


def adjust_for_failure(level: str, failure_class: str | None, allowed: list[str]) -> tuple[str, str]:
    kind = str(failure_class or "").upper()
    current = _nearest_allowed(level, allowed) if allowed else level
    if kind in QUALITY_FAILURES or kind in {"OUTPUT_CONTRACT_ERROR", "UNDER_LENGTH"}:
        nxt = _shift_level(current, 1, allowed)
        if nxt == current:
            return nxt, "QUALITY_ESCALATION_UNAVAILABLE"
        return nxt, "QUALITY_ESCALATION"
    if kind in LATENCY_FAILURES:
        nxt = _shift_level(current, -1, allowed)
        if nxt == current:
            return nxt, "LATENCY_DOWNGRADE"
        return nxt, "LATENCY_DOWNGRADE"
    return level, "NONE"


def _missing_map_values(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(item).upper() == "MISSING" for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(str(item).upper() == "MISSING" for item in value)
    return str(value or "").upper() in {"MISSING", "BLOCK", "FAIL"}


def quality_failure_reason(gate: dict[str, Any] | None, audit: dict[str, Any] | None = None) -> str | None:
    """Detailed quality evidence. Used for repair mode; never demotes to latency."""
    gate = gate or {}
    audit = audit or {}
    coverage = gate.get("must_cover") or {}
    if isinstance(coverage, dict) and any(str(value).upper() == "MISSING" for value in coverage.values()):
        return "MUST_REQUIREMENT_MISSING"
    for key in ("scoring_cover", "scoring_requirements", "scoring", "scoring_must_cover"):
        if key in gate and _missing_map_values(gate.get(key)):
            return "SCORING_REQUIREMENT_MISSING"
    if (gate.get("artifact_qa") or {}).get("status") == "BLOCK":
        return "ARTIFACT_QA_BLOCK"
    if (gate.get("commitment") or {}).get("status") == "BLOCK":
        return "COMMITMENT_BLOCK"
    if (gate.get("compliance") or {}).get("status") == "BLOCK":
        return "COMPLIANCE_BLOCK"
    contradictions = gate.get("contradiction") or gate.get("contradictions")
    if contradictions:
        return "CONTRADICTION"
    length_status = str(gate.get("length_status") or "")
    if length_status in {"SECTION_UNDER_LENGTH", "UNDER_LENGTH", "DEPTH_FAIL", "SECTION_DEPTH_FAILURE"}:
        return "SECTION_DEPTH_FAILURE"
    if gate.get("status") == "BLOCK":
        return "QUALITY_GATE_BLOCK"
    if gate.get("status") == "FAIL":
        return "QUALITY_GATE_BLOCK"
    if str(audit.get("error_code") or "") == "OUTPUT_CONTRACT_ERROR":
        return "OUTPUT_CONTRACT_ERROR"
    return None


def classify_generation_outcome(gate: dict[str, Any] | None, audit: dict[str, Any] | None = None) -> dict[str, Any]:
    audit = audit or {}
    error = str(audit.get("error_code") or "")
    reason = quality_failure_reason(gate, audit)
    if error == "RATE_LIMIT":
        return {
            "failure_class": "RATE_LIMIT",
            "quality_failure_reason": reason,
            "latency_exceeded": bool(audit.get("latency_hard_exceeded") or audit.get("latency_soft_exceeded")),
        }
    # Content/quality evidence always precedes elapsed-time markers.
    if reason:
        return {
            "failure_class": "QUALITY_FAILURE",
            "quality_failure_reason": reason,
            "latency_exceeded": bool(audit.get("latency_hard_exceeded") or audit.get("latency_soft_exceeded") or error in {"TIMEOUT", "NETWORK_ERROR"}),
        }
    latency_marker = error in {"TIMEOUT", "NETWORK_ERROR"} or bool(audit.get("latency_hard_exceeded"))
    if latency_marker:
        return {
            "failure_class": "LATENCY_FAILURE",
            "quality_failure_reason": None,
            "latency_exceeded": True,
        }
    if audit.get("latency_soft_exceeded") and audit.get("status") != "SUCCESS":
        return {
            "failure_class": "LATENCY_FAILURE",
            "quality_failure_reason": None,
            "latency_exceeded": True,
        }
    if not gate and audit.get("status") not in {None, "SUCCESS"}:
        return {
            "failure_class": "QUALITY_FAILURE",
            "quality_failure_reason": "PROVIDER_OUTPUT_FAILURE",
            "latency_exceeded": False,
        }
    return {"failure_class": None, "quality_failure_reason": None, "latency_exceeded": False}


def classify_generation_failure(gate: dict[str, Any] | None, audit: dict[str, Any] | None = None) -> str | None:
    return classify_generation_outcome(gate, audit).get("failure_class")
