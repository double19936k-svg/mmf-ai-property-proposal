from __future__ import annotations

from typing import Any

from planning.unified_budget import (
    ARTIFACT_CHARS_PER_LEDGER_CHAR_RUN_D,
    artifact_chars_from_ledger,
)


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
    content_budget: dict[str, Any] | None = None,
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
    totals = (content_budget or {}).get("totals") or {}
    # 额度是账本字（CU 正文 + 结构 payload），`total_effective_chars` 是装配产物可见字。
    # 直接相比就是拿两把尺子对表：Run D 实测产物/账本 = 0.7707，所以先把额度换过去。
    budget_min = int(totals.get("target_effective_chars_min") or 0)
    budget_target = int(totals.get("target_effective_chars_target") or totals.get("target_effective_chars_max") or 0)
    budget_min_in_artifact_currency = artifact_chars_from_ledger(budget_min)
    document_scale = str((content_budget or {}).get("document_scale_code") or "")
    effective_budget_ok = not budget_min or total_effective_chars >= budget_min_in_artifact_currency
    checks = {
        "DOCUMENT_PLAN_PRESENT": bool(word_plan and word_plan.get("outline")),
        "SECTION_CONTRACT_COVERAGE": bool(contract_ids) and not missing,
        "REQUIREMENT_COVERAGE": must_total == 0 or covered / max(1, must_total) >= 0.6,
        "MISSING_SECTION_COUNT": len(missing) == 0,
        "SECTION_TOO_SHORT_COUNT": too_short == 0 if task_mode != "full_longform" else too_short <= max(1, len(contract_ids or planned) // 5),
        "TOTAL_EFFECTIVE_CONTENT": total_effective_chars >= (FULL_LONGFORM_MIN_CHARS if task_mode == "full_longform" else 800),
        "DOCUMENT_EFFECTIVE_BUDGET": effective_budget_ok,
    }
    if not checks["DOCUMENT_PLAN_PRESENT"]:
        findings.append({"rule_id": "LF-001", "severity": "BLOCK", "issue": "DOCUMENT_PLAN missing"})
    if not checks["SECTION_CONTRACT_COVERAGE"]:
        findings.append({"rule_id": "LF-002", "severity": "BLOCK", "issue": f"missing sections: {missing[:12]}"})
    if task_mode == "full_longform" and not checks["TOTAL_EFFECTIVE_CONTENT"]:
        findings.append({"rule_id": "LF-003", "severity": "BLOCK", "issue": f"full_longform only {total_effective_chars} chars"})
    if task_mode == "full_longform" and not effective_budget_ok:
        findings.append({
            "rule_id": "LF-300-BUDGET",
            "severity": "BLOCK",
            "issue": (f"{document_scale or 'document'} effective budget incomplete: "
                      f"{total_effective_chars}/{budget_min_in_artifact_currency}"
                      f"（账本额度{budget_min}×{ARTIFACT_CHARS_PER_LEDGER_CHAR_RUN_D}）"),
            "repair_mode": "TARGETED_CONTINUATION",
        })
    if task_mode == "full_longform" and generated and len(generated) / max(1, len(contract_ids or planned)) < FULL_LONGFORM_MIN_SECTION_RATIO:
        findings.append({"rule_id": "LF-004", "severity": "BLOCK", "issue": "section coverage below longform gate"})
        checks["SECTION_CONTRACT_COVERAGE"] = False
    if not checks["REQUIREMENT_COVERAGE"]:
        findings.append({"rule_id": "LF-005", "severity": "BLOCK", "issue": "requirement coverage below gate"})
    status = "DEPTH_INCOMPLETE" if any(item.get("rule_id") == "LF-300-BUDGET" for item in findings) else ("BLOCK" if any(item["severity"] == "BLOCK" for item in findings) else "PASS")
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
            "target_effective_chars_min": budget_min,
            "target_effective_chars_min_artifact_currency": budget_min_in_artifact_currency,
            "target_effective_chars_target": budget_target,
            "effective_budget_ratio": (
                round(total_effective_chars / max(1, budget_min_in_artifact_currency), 4)
                if budget_min else None),
            "must_covered": f"{covered}/{must_total}",
        },
        "findings": findings,
        "targeted_continuation": {
            "required": status == "DEPTH_INCOMPLETE",
            "scope": "under_budget_sections_or_content_units_only",
            "full_document_regeneration": False,
        },
    }
