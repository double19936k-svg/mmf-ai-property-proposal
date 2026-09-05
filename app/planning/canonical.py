from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .planner import now_iso, write_json


FULL_PROPOSAL_SCENARIOS = {"完整物业服务方案", "投标全套服务方案"}
BRIEF_SECTIONS = ["S01-01", "S01-02", "S11-03"]
STANDARD_SECTIONS = [
    "S01-01", "S02-01", "S03-01", "S04-01", "S05-01",
    "S06-01", "S07-01", "S08-01", "S09-01", "S10-01", "S11-01",
]


def ensure_requirement_pack(run_dir: Path, brief: dict[str, Any]) -> Path:
    for candidate in (run_dir / "tender" / "requirement_pack.json", run_dir / "requirement_pack.json"):
        if candidate.is_file():
            return candidate
    text = str(brief.get("requirements") or brief.get("client_requirements") or "本项目物业服务要求")
    pack = {
        "schema_version": "tender-requirement-pack-v0.1",
        "pack_id": f"PACK-{run_dir.name}",
        "status": "ready_for_plan",
        "project_facts": {
            "project_name": {"value": brief.get("project_name") or "本项目"},
            "location": {"value": brief.get("location") or ""},
            "gross_area": {"value": brief.get("area") or ""},
        },
        "service_scope": {"included": [], "excluded": [], "deprioritized": [], "conditional": []},
        "requirements": [{
            "requirement_id": "REQ-0001",
            "normalized_requirement": text[:800],
            "domain": "other",
            "mandatory_level": "MUST",
            "confirmation_status": "CONFIRMED",
        }],
        "confirmation": {"ready_for_brief_seed": True},
    }
    target = run_dir / "requirement_pack.json"
    write_json(target, pack)
    return target


def resolve_task_mode(brief: dict[str, Any], override: str | None = None) -> str:
    if override in {"brief", "standard", "full_longform"}:
        return override
    scenario = str(brief.get("scenario") or "")
    if scenario in FULL_PROPOSAL_SCENARIOS:
        return "full_longform"
    if scenario in {"前期介入", "进场启动与承接查验"}:
        return "standard"
    return "standard"


def mode_section_ids(mode: str, all_ids: list[str]) -> list[str] | None:
    if mode == "full_longform":
        return None
    if mode == "brief":
        return [sid for sid in BRIEF_SECTIONS if sid in all_ids]
    return [sid for sid in STANDARD_SECTIONS if sid in all_ids]


def _fact_value(pack: dict[str, Any], key: str, fallback: str = "") -> str:
    row = (pack.get("project_facts") or {}).get(key) or {}
    if isinstance(row, dict):
        return str(row.get("value") or fallback)
    return str(row or fallback)


def build_canonical_tender_analysis(pack: dict[str, Any], brief: dict[str, Any]) -> dict[str, Any]:
    requirements = [row for row in pack.get("requirements", []) if isinstance(row, dict)]
    must = [row for row in requirements if row.get("mandatory_level") == "MUST"]
    scoring = pack.get("scoring_items") or []
    domains: dict[str, int] = {}
    for row in requirements:
        domain = str(row.get("domain") or "other")
        domains[domain] = domains.get(domain, 0) + 1
    return {
        "schema_version": "canonical-tender-analysis-v0.1",
        "pack_id": pack.get("pack_id"),
        "project_name": _fact_value(pack, "project_name", brief.get("project_name", "")),
        "location": _fact_value(pack, "location", brief.get("location", "")),
        "gross_area": _fact_value(pack, "gross_area", brief.get("area", "")),
        "project_type": brief.get("project_type", ""),
        "scenario": brief.get("scenario", ""),
        "requirement_count": len(requirements),
        "must_count": len(must),
        "scoring_item_count": len(scoring),
        "domain_counts": domains,
        "excluded_scope": deepcopy(pack.get("service_scope", {}).get("excluded", [])),
        "provider_independent": True,
        "created_at": now_iso(),
    }


def build_canonical_requirement_map(pack: dict[str, Any], matrix: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "canonical-requirement-map-v0.1",
        "pack_id": pack.get("pack_id"),
        "provider_independent": True,
        "matrix": deepcopy(matrix.get("matrix", [])),
        "coverage_summary": deepcopy(matrix.get("coverage_summary", {})),
        "excluded_requirements": deepcopy(matrix.get("excluded_requirements", [])),
        "created_at": now_iso(),
    }


def build_canonical_project_brief(pack: dict[str, Any], brief: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    must_texts = [
        str(row.get("normalized_requirement") or row.get("text") or "")
        for row in pack.get("requirements", [])
        if row.get("mandatory_level") == "MUST"
    ][:40]
    return {
        "schema_version": "canonical-project-brief-v0.1",
        "provider_independent": True,
        "project_name": analysis["project_name"],
        "project_type": brief.get("project_type", ""),
        "scenario": brief.get("scenario", ""),
        "medium": brief.get("medium", "WORD"),
        "location": analysis["location"],
        "area": analysis["gross_area"],
        "must_requirements": [text for text in must_texts if text],
        "excluded_scope": analysis.get("excluded_scope", []),
        "fact_boundary": brief.get("fact_boundary") or "表单中的全部数字均为current_project_fact；历史KU数字不得迁移。",
        "requirement_pack_id": pack.get("pack_id") or brief.get("requirement_pack_id"),
        "created_at": now_iso(),
    }


def write_canonical_bundle(run_dir, analysis: dict[str, Any], requirement_map: dict[str, Any], project_brief: dict[str, Any]) -> dict[str, Any]:
    files = {
        "canonical_tender_analysis": run_dir / "canonical_tender_analysis.json",
        "canonical_requirement_map": run_dir / "canonical_requirement_map.json",
        "canonical_project_brief": run_dir / "canonical_project_brief.json",
    }
    write_json(files["canonical_tender_analysis"], analysis)
    write_json(files["canonical_requirement_map"], requirement_map)
    write_json(files["canonical_project_brief"], project_brief)
    return {key: str(path) for key, path in files.items()}
