from __future__ import annotations

from typing import Any


FULL_LONGFORM_MIN_CHARS = 8000
FULL_LONGFORM_MIN_SECTION_RATIO = 0.95


def evaluate_longform_depth(
    *,
    task_mode: str,
    word_plan: dict[str, Any] | None,
    contracts: dict[str, Any] | None,
    matrix: dict[str, Any] | None,
    fragments: dict[str, dict[str, Any]],
    gates: list[dict[str, Any]],
    total_effective_chars: int,
) -> dict[str, Any]:
    planned = []
    if word_plan:
        planned = [section["section_id"] for chapter in word_plan.get("outline", []) for section in chapter.get("sections", [])]
    contract_ids = [row.get("section_id") for row in (contracts or {}).get("contracts", []) if row.get("section_id")]
    generated = sorted(fragments)
    missing = [sid for sid in (contract_ids or planned) if sid not in fragments]
    too_short = sum(1 for row in gates if row.get("length_status") == "SECTION_TOO_SHORT" or row.get("length_status") == "SECTION_UNDER_LENGTH")
    covered = 0
    must_total = 0
    if matrix:
        for row in matrix.get("matrix", []):
            if row.get("mandatory_level") != "MUST":
                continue
            must_total += 1
            sid = row.get("primary_section_id")
            if sid and sid in fragments:
                covered += 1
    findings = []
    checks = {
        "DOCUMENT_PLAN_PRESENT": bool(word_plan and word_plan.get("outline")),
        "SECTION_CONTRACT_COVERAGE": bool(contract_ids) and not missing,
        "REQUIREMENT_COVERAGE": must_total == 0 or covered / max(1, must_total) >= 0.6,
        "MISSING_SECTION_COUNT": len(missing) == 0,
        "SECTION_TOO_SHORT_COUNT": too_short == 0 if task_mode != "full_longform" else too_short <= max(1, len(contract_ids or planned) // 5),
        "TOTAL_EFFECTIVE_CONTENT": total_effective_chars >= (FULL_LONGFORM_MIN_CHARS if task_mode == "full_longform" else 800),
    }
    if not checks["DOCUMENT_PLAN_PRESENT"]:
        findings.append({"rule_id": "LF-001", "severity": "BLOCK", "issue": "DOCUMENT_PLAN missing"})
    if not checks["SECTION_CONTRACT_COVERAGE"]:
        findings.append({"rule_id": "LF-002", "severity": "BLOCK", "issue": f"missing sections: {missing[:12]}"})
    if task_mode == "full_longform" and not checks["TOTAL_EFFECTIVE_CONTENT"]:
        findings.append({"rule_id": "LF-003", "severity": "BLOCK", "issue": f"full_longform only {total_effective_chars} chars"})
    if task_mode == "full_longform" and generated and len(generated) / max(1, len(contract_ids or planned)) < FULL_LONGFORM_MIN_SECTION_RATIO:
        findings.append({"rule_id": "LF-004", "severity": "BLOCK", "issue": "section coverage below longform gate"})
        checks["SECTION_CONTRACT_COVERAGE"] = False
    if not checks["REQUIREMENT_COVERAGE"]:
        findings.append({"rule_id": "LF-005", "severity": "BLOCK", "issue": "requirement coverage below gate"})
    status = "BLOCK" if any(item["severity"] == "BLOCK" for item in findings) else "PASS"
    return {
        "schema_version": "longform-depth-qa-v0.1",
        "status": status,
        "LONGFORM_DEPTH_GATE": status,
        "checks": checks,
        "metrics": {
            "planned_sections": len(contract_ids or planned),
            "generated_sections": len(generated),
            "missing_sections": missing,
            "section_too_short_count": too_short,
            "total_effective_chars": total_effective_chars,
            "must_covered": f"{covered}/{must_total}",
        },
        "findings": findings,
    }
