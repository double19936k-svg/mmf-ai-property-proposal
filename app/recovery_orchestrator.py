"""Classify QA/governance events and recover locally before any terminal UI state."""

from __future__ import annotations

from typing import Any

from compliance import apply_numeric_commitment_repairs, evaluate_compliance
from governance.artifact_qa import apply_artifact_repairs, evaluate_artifact, merge_longform_qa
from governance.commitment_provenance import apply_local_repairs, evaluate_commitments
from governance.customer_hygiene import apply_hygiene_tree
from governance.text_sanitize import rewrite_unconfirmed_durations, sanitize_public_text

LEVEL_INTERNAL = 0
LEVEL_RECOVERABLE = 1
LEVEL_DEGRADED = 2
LEVEL_TERMINAL = 3

RECOVERABLE_RULES = {
    "CG-001",
    "CG-002",
    "CG-005",
    "UNSUPPORTED_COMPLETION_CLAIM",
    "ABSOLUTE_PERFORMANCE_COMMITMENT",
    "CLIENT_ARTIFACT_INTERNAL_META",
    "QUOTE_BALANCE_QA",
    "BROKEN_QUOTE_SERIALIZATION",
    "EMPTY_HEADING_BLOCK",
    "INTERNAL_SECTION_ID",
    "CONDITIONAL_POSTPROCESS_POLLUTION",
    "CONDITIONAL_REWRITE_GRAMMAR",
    "BROKEN_CROSS_REFERENCE",
    "BROKEN_FLOW_SERIALIZATION",
    "UNSUPPORTED_SYSTEM_CAPABILITY",
    "UNSUPPORTED_NUMERIC_THRESHOLD",
    "INTERNAL_GOVERNANCE_TONE",
    "FAQ-002",
    "FAQ-004",
    "FAQ-005",
}
TERMINAL_RULES = {
    "PROJECT_FACT_CONSISTENCY",
    "FINAL_ARTIFACT_MISSING",
}
MAX_LOCAL_RECOVERY_ROUNDS = 2


def classify_rule(rule_id: str) -> int:
    rid = str(rule_id or "")
    if rid in TERMINAL_RULES:
        return LEVEL_TERMINAL
    if rid.startswith("LF-") or rid.startswith("QR-"):
        return LEVEL_RECOVERABLE
    if rid in RECOVERABLE_RULES or rid.startswith("CG-") or rid.startswith("FAQ-"):
        return LEVEL_RECOVERABLE
    if rid:
        return LEVEL_RECOVERABLE
    return LEVEL_INTERNAL


def _block_rules(compliance: dict[str, Any], commitment: dict[str, Any], artifact_qa: dict[str, Any], quality: dict[str, Any] | None = None) -> list[str]:
    rules: list[str] = []
    for row in (compliance or {}).get("violations") or []:
        if row.get("severity") == "BLOCK":
            rules.append(str(row.get("rule_id") or "CG"))
    for row in (commitment or {}).get("violations") or []:
        if row.get("severity") == "BLOCK" or row.get("repair_action"):
            rules.append(str(row.get("claim_type") or row.get("rule_id") or "COMMITMENT"))
    for row in (artifact_qa or {}).get("findings") or []:
        if row.get("severity") in {"BLOCK", "AUTO_REPAIR"}:
            rules.append(str(row.get("rule_id") or "ARTIFACT"))
    quality = quality or {}
    if quality.get("status") == "FAIL" or quality.get("QUALITY_REGRESSION_GATE") == "FAIL":
        rules.append("QR-001")
    return rules


def classify_reports(compliance: dict[str, Any], commitment: dict[str, Any], artifact_qa: dict[str, Any], quality: dict[str, Any] | None = None) -> int:
    rules = _block_rules(compliance, commitment, artifact_qa, quality)
    if not rules:
        return LEVEL_INTERNAL
    levels = [classify_rule(rule) for rule in rules]
    if any(level == LEVEL_TERMINAL for level in levels):
        return LEVEL_TERMINAL
    if (artifact_qa or {}).get("status") == "BLOCK" and any(str(rule).startswith(("LF-", "QR-")) for rule in rules):
        return LEVEL_RECOVERABLE
    return max(levels)


def _rewrite_tree(value: Any, *, include_bare_minutes: bool) -> Any:
    if isinstance(value, dict):
        return {key: _rewrite_tree(item, include_bare_minutes=include_bare_minutes) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_tree(item, include_bare_minutes=include_bare_minutes) for item in value]
    if isinstance(value, str):
        return rewrite_unconfirmed_durations(value, include_bare_minutes=include_bare_minutes)
    return value


def apply_local_recovery(generated: dict[str, Any], compliance: dict[str, Any] | None = None) -> dict[str, Any]:
    repaired = apply_artifact_repairs(apply_local_repairs(generated))
    repaired = apply_numeric_commitment_repairs(repaired, compliance)
    include_bare = any(row.get("rule_id") in {"CG-001", "CG-005"} for row in (compliance or {}).get("violations") or [])
    repaired = _rewrite_tree(repaired, include_bare_minutes=include_bare)
    repaired = apply_hygiene_tree(repaired)
    return sanitize_public_text(repaired)


def recover_governance(
    generated: dict[str, Any],
    *,
    brief: dict[str, Any],
    positives: list[dict[str, Any]],
    guardrails: list[dict[str, Any]],
    contracts: list[dict[str, Any]],
    canonical_facts: dict[str, Any] | None = None,
    depth: dict[str, Any] | None = None,
    quality: dict[str, Any] | None = None,
    max_rounds: int = MAX_LOCAL_RECOVERY_ROUNDS,
) -> dict[str, Any]:
    """Repair recoverable governance/hygiene locally. Never regenerates the whole document."""
    current = generated
    audit: list[dict[str, Any]] = []
    commitment = evaluate_commitments(brief, contracts, current)
    artifact_qa = merge_longform_qa(evaluate_artifact(current, canonical_facts=canonical_facts), depth)
    compliance = evaluate_compliance(brief, positives, guardrails, current)
    level = classify_reports(compliance, commitment, artifact_qa, quality)
    for round_no in range(1, max_rounds + 1):
        if level <= LEVEL_INTERNAL:
            break
        if level >= LEVEL_TERMINAL:
            break
        if level == LEVEL_RECOVERABLE:
            before = {"compliance": compliance.get("status"), "commitment": commitment.get("status"), "artifact_qa": artifact_qa.get("status")}
            current = apply_local_recovery(current, compliance)
            commitment = evaluate_commitments(brief, contracts, current)
            artifact_qa = merge_longform_qa(evaluate_artifact(current, canonical_facts=canonical_facts), depth)
            compliance = evaluate_compliance(brief, positives, guardrails, current)
            level = classify_reports(compliance, commitment, artifact_qa, quality)
            audit.append({
                "round": round_no,
                "level": LEVEL_RECOVERABLE,
                "before": before,
                "after": {"compliance": compliance.get("status"), "commitment": commitment.get("status"), "artifact_qa": artifact_qa.get("status")},
            })
            continue
        break
    user_phase = "正在自动完善方案内容。" if audit else "正在检查事实与承诺。"
    terminal = withhold_delivery(compliance, commitment, artifact_qa, quality)
    return {
        "generated": current,
        "commitment": commitment,
        "artifact_qa": artifact_qa,
        "compliance": compliance,
        "level": LEVEL_TERMINAL if terminal else level,
        "terminal": terminal,
        "audit": audit,
        "user_phase": user_phase,
        "repair_count": len(audit),
        "warnings": _block_rules(compliance, commitment, artifact_qa, quality) if not terminal else [],
    }


def withhold_delivery(
    compliance: dict[str, Any],
    commitment: dict[str, Any],
    artifact_qa: dict[str, Any],
    quality: dict[str, Any] | None = None,
) -> bool:
    """Withhold assembly only when the document cannot form a reviewable artifact."""
    from delivery_state import should_withhold_before_render
    return should_withhold_before_render(compliance, commitment, artifact_qa, quality)


def leftover_warning_rows(
    compliance: dict[str, Any],
    commitment: dict[str, Any],
    artifact_qa: dict[str, Any],
    quality: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Collect leftover sentence findings that must not fail delivery."""
    if withhold_delivery(compliance, commitment, artifact_qa, quality):
        return []
    rows: list[dict[str, Any]] = []
    for row in (compliance or {}).get("violations") or []:
        if row.get("severity") == "BLOCK":
            rows.append({**row, "severity": "WARNING", "delivery": "accepted_with_warning"})
    for row in (commitment or {}).get("violations") or []:
        if row.get("severity") == "BLOCK" or row.get("repair_action"):
            rows.append({**row, "severity": "WARNING", "delivery": "accepted_with_warning"})
    for row in (artifact_qa or {}).get("findings") or []:
        if row.get("severity") in {"BLOCK", "AUTO_REPAIR"}:
            rows.append({**row, "severity": "WARNING", "delivery": "accepted_with_warning"})
    return rows


def accept_leftover_reports(
    compliance: dict[str, Any],
    commitment: dict[str, Any],
    artifact_qa: dict[str, Any],
    quality: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Downgrade leftover BLOCK reports so a complete Word can be delivered."""
    warnings = leftover_warning_rows(compliance, commitment, artifact_qa, quality)
    if not warnings:
        return compliance, commitment, artifact_qa, warnings

    def _warn(report: dict[str, Any] | None) -> dict[str, Any]:
        current = report or {}
        if current.get("status") != "BLOCK":
            return current
        return {**current, "status": "WARNING", "leftover_accepted": True}

    return _warn(compliance), _warn(commitment), _warn(artifact_qa), warnings


def should_quarantine_artifact(final_report: dict[str, Any] | None) -> bool:
    """Content QA leftovers must not hide a valid Word. Physical missing still withholds."""
    blocked = [row for row in ((final_report or {}).get("findings") or []) if row.get("severity") == "BLOCK"]
    return any(str(row.get("rule_id") or "") == "FINAL_ARTIFACT_MISSING" for row in blocked)


def accept_leftover_findings(report: dict[str, Any] | None) -> dict[str, Any]:
    """Keep the Word; leftover sentence findings become warnings."""
    current = dict(report or {})
    if should_quarantine_artifact(current):
        return current
    findings = []
    accepted = False
    for row in current.get("findings") or []:
        if row.get("severity") == "BLOCK":
            findings.append({**row, "severity": "WARNING", "delivery": "accepted_with_warning"})
            accepted = True
        else:
            findings.append(row)
    warning_count = sum(1 for row in findings if row.get("severity") != "BLOCK")
    block_count = sum(1 for row in findings if row.get("severity") == "BLOCK")
    return {
        **current,
        "status": "BLOCK" if block_count else ("WARNING" if warning_count else "PASS"),
        "findings": findings,
        "leftover_accepted": accepted,
        "summary": {**(current.get("summary") or {}), "block_count": block_count, "warning_count": warning_count},
    }
