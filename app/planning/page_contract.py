"""Explicit project page bounds, separate from advisory legacy size bands."""
from __future__ import annotations

from typing import Any


def evaluate_page_contract(classification: dict[str, Any] | None,
                           constraints: dict[str, Any] | None) -> dict[str, Any]:
    limits = dict(constraints or {})
    if not limits:
        return {"status": "NOT_APPLICABLE", "findings": []}
    minimum = int(limits["technical_pages_min"])
    maximum = int(limits["physical_pages_max"])
    if not 1 <= minimum <= maximum:
        raise ValueError("Invalid explicit page limits")
    measured = classification or {}
    physical = measured.get("physical_pages")
    technical = measured.get("technical_effective_pages")
    findings = []
    if (not isinstance(physical, int) or physical <= 0
            or not isinstance(technical, int) or not 0 <= technical <= physical):
        status = "NOT_MEASURED"
        findings.append({"rule_id": "PAGE_CONTRACT_NOT_MEASURED", "path": "artifact",
                         "severity": "NEEDS_REVIEW", "issue": "Actual final pagination is required; estimates cannot satisfy the page contract."})
    else:
        if technical < minimum:
            findings.append({"rule_id": "PAGE_CONTRACT_BODY_TOO_SHORT", "path": "artifact",
                             "severity": "BLOCK", "issue": f"Technical content {technical} pages below required {minimum}."})
        if physical > maximum:
            findings.append({"rule_id": "PAGE_CONTRACT_PHYSICAL_LIMIT", "path": "artifact",
                             "severity": "BLOCK", "issue": f"Whole document {physical} pages exceeds hard limit {maximum}."})
        status = "BLOCK" if findings else "PASS"
    return {"status": status, "constraints": limits, "physical_pages": physical,
            "technical_effective_pages": technical, "findings": findings,
            "layout_basis": measured.get("layout_basis", "NOT_MEASURED"),
            "technical_page_count_bound": measured.get("technical_page_count_bound", "NOT_MEASURED"),
            "manual_acceptance": "PENDING"}
