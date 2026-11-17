"""Content Unit and Section lifecycle states for the long-task pipeline.

MMF-007C separates "the text arrived" from "the text is good enough".  During
initial generation only a technical failure may interrupt the main chain; a
thin or ordinary-quality result is persisted and carried forward instead of
re-triggering a whole-Section request.
"""

from __future__ import annotations

from typing import Any

from planning.unified_budget import artifact_chars_from_ledger


CU_GENERATED = "GENERATED"
CU_PROVISIONAL = "PROVISIONAL"
CU_TECHNICAL_FAILED = "TECHNICAL_FAILED"
CU_MISSING = "MISSING"
CU_NEEDS_SUPPLEMENT = "NEEDS_SUPPLEMENT"
CU_TRUNCATED = "TRUNCATED"
CU_PAYLOAD_INCOMPLETE = "PAYLOAD_INCOMPLETE"

CU_ARRIVED_STATES = {CU_GENERATED, CU_PROVISIONAL, CU_TRUNCATED, CU_PAYLOAD_INCOMPLETE}
CU_SETTLED_STATES = {CU_GENERATED, CU_PROVISIONAL, CU_TECHNICAL_FAILED}

SECTION_COMPLETED = "COMPLETED"
SECTION_COMPLETED_WITH_WARNING = "COMPLETED_WITH_WARNING"
SECTION_COMPLETED_CONDITIONAL = "COMPLETED_CONDITIONAL"
SECTION_PROVISIONAL = "COMPLETED_PROVISIONAL"
SECTION_DEPTH_INCOMPLETE = "COMPLETED_DEPTH_INCOMPLETE"
SECTION_TECHNICAL_FAILED = "TECHNICAL_FAILED_SECTION"

SECTION_DRAFTABLE_STATES = {
    SECTION_COMPLETED,
    SECTION_COMPLETED_WITH_WARNING,
    SECTION_COMPLETED_CONDITIONAL,
    SECTION_PROVISIONAL,
    SECTION_DEPTH_INCOMPLETE,
}
SECTION_PASS_STATES = {
    SECTION_COMPLETED,
    SECTION_COMPLETED_WITH_WARNING,
    SECTION_COMPLETED_CONDITIONAL,
}

TECHNICAL_RETRY_CODES = {
    "NETWORK_ERROR",
    "DNS_ERROR",
    "PROXY_ERROR",
    "TIMEOUT",
    "RATE_LIMIT",
    "RATE_LIMIT_OVERSIZED",
    "RATE_LIMIT_TIMEOUT",
    "EMPTY_RESPONSE",
    # Section 正文为空在链路上就是 EMPTY_RESPONSE 的节内别名：内容没有到达，
    # 而不是到达了但质量不够，因此允许有界技术重试。
    "EMPTY_BODY",
    "RESPONSE_MALFORMED_UNRECOVERABLE",
    "PROVIDER_RUNTIME_ERROR",
    "PROVIDER_UNAVAILABLE",
    "REQUEST_NOT_SENT",
    "OUTPUT_TRUNCATED",
}

TECHNICAL_DEFER_CODES = {
    "AUTH_FAILED",
    "AUTH_STATE_UNKNOWN",
    "PROVIDER_AUTH_REQUIRED",
    "QUOTA_EXCEEDED",
    "MODEL_NOT_FOUND",
    "PLATFORM_CONFIRMATION_REQUIRED",
    "CLI_MAX_TURNS",
    "CLI_NOT_FOUND",
    "RESPONSE_UNKNOWN",
}

PROVISIONAL_FRICTION_RATIO = 0.85
MATERIAL_DEPTH_RATIO = 0.55


def is_technical_retryable(code: str | None) -> bool:
    return str(code or "").upper() in TECHNICAL_RETRY_CODES


def is_terminal_technical(code: str | None) -> bool:
    return str(code or "").upper() in TECHNICAL_DEFER_CODES


def audit_error_code(audit: dict[str, Any] | None) -> str:
    audit = audit or {}
    return str(audit.get("error_code") or "").upper()


def depth_ratio(content_units: int, contract: dict[str, Any]) -> float:
    """到达量 ÷ 契约额度 —— 两边必须是同一种货币。

    `content_units` 是装配后正文的可见字，`target_words` 是统一账本字（含结构 payload
    与 CU 回声），Run D 实测比值 0.7707。直接相除会把每一节凭空多算 29.7% 的深度，
    `depth_deficit_class` 于是把写满契约的节判成 NONE、把真正薄的节也一起放过。
    """
    target_min = artifact_chars_from_ledger(
        int((contract.get("target_words") or {}).get("min") or 0))
    units = int(content_units or 0)
    if target_min <= 0:
        return 1.0
    return units / target_min


def depth_deficit_class(content_units: int, contract: dict[str, Any]) -> str:
    """Classify how far a Section sits below its budget without failing it."""
    ratio = depth_ratio(content_units, contract)
    if ratio >= PROVISIONAL_FRICTION_RATIO:
        return "NONE"
    if ratio >= MATERIAL_DEPTH_RATIO:
        return "MINOR"
    return "MATERIAL"


def cu_status_from_arrival(
    *,
    arrived: bool,
    truncated: bool = False,
    payload_incomplete: bool = False,
    content_units: int = 0,
    contract: dict[str, Any] | None = None,
    technical_code: str | None = None,
) -> str:
    if technical_code and not arrived:
        return CU_TECHNICAL_FAILED
    if not arrived:
        return CU_MISSING
    if truncated:
        return CU_TRUNCATED
    if payload_incomplete:
        return CU_PAYLOAD_INCOMPLETE
    if content_units <= 0:
        return CU_MISSING
    if contract is not None and depth_deficit_class(content_units, contract) != "NONE":
        return CU_PROVISIONAL
    return CU_GENERATED
