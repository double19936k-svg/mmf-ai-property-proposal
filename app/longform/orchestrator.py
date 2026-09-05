from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from governance.artifact_qa import evaluate_artifact
from governance.longform_qa import evaluate_longform_depth
from planning.canonical import (
    build_canonical_project_brief,
    build_canonical_requirement_map,
    build_canonical_tender_analysis,
    mode_section_ids,
    resolve_task_mode,
    write_canonical_bundle,
)
from planning.planner import PlanningError, build_planning_bundle, now_iso, write_json
from providers.capability import resolve_profile

from .factory import LongformGenerationFactory, content_units, read_json, visible_text


PLAN_FILES = {
    "word_plan": "01_word_document_plan.json",
    "requirement_matrix": "02_requirement_section_matrix.json",
    "content_budget": "03_word_content_budget.json",
    "section_contracts": "04_section_contracts.json",
    "global_state": "05_document_global_state_v0.json",
    "ppt_plan": "06_ppt_presentation_plan.json",
    "dependency_map": "07_cross_section_dependency.json",
}


def _normalize_knowledge(selection: dict[str, Any], selected_ids: list[str]) -> dict[str, Any]:
    rows = list(selection.get("knowledge_usage_contracts") or [])
    have = {row.get("ku_id") for row in rows}
    for ku_id in selected_ids:
        if ku_id not in have:
            rows.append({"ku_id": ku_id, "selection_status": "SELECTED", "usable_content": "", "language_level": "method"})
    for row in rows:
        if row.get("ku_id") in selected_ids:
            row.setdefault("selection_status", "SELECTED")
    return {**selection, "knowledge_usage_contracts": rows}


def load_requirement_pack(run_dir: Path) -> dict[str, Any]:
    for candidate in (run_dir / "tender" / "requirement_pack.json", run_dir / "requirement_pack.json"):
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8-sig"))
    raise PlanningError("当前Run缺少Requirement Pack，无法进入长文规划。")


def write_plan_artifacts(run_dir: Path, bundle: dict[str, Any]) -> dict[str, Path]:
    written = {}
    for key, name in PLAN_FILES.items():
        path = run_dir / name
        write_json(path, bundle[key])
        written[key] = path
    return written


def overlay_knowledge(contracts: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
    selected = [row.get("ku_id") for row in selection.get("knowledge_usage_contracts", []) if row.get("selection_status") == "SELECTED"]
    conditional = [row.get("ku_id") for row in selection.get("knowledge_usage_contracts", []) if row.get("selection_status") == "CONDITIONAL"]
    patched = json.loads(json.dumps(contracts))
    for row in patched.get("contracts", []):
        row["allowed_knowledge"] = selected
        row["conditional_knowledge"] = conditional
    return patched


def fragment_to_section(fragment: dict[str, Any], fallback_title: str) -> dict[str, Any]:
    paragraphs: list[str] = []
    bullets: list[str] = []
    for block in fragment.get("body_blocks") or []:
        if not isinstance(block, dict):
            if block:
                paragraphs.append(str(block))
            continue
        kind = str(block.get("type") or "")
        content = block.get("content") or block.get("text") or ""
        items = block.get("items") or block.get("points") or []
        if kind in {"bullet_group", "numbered_steps"}:
            bullets.extend(str(item) for item in items if item)
            if content:
                paragraphs.append(str(content))
        elif kind == "subheading" and content:
            paragraphs.append(str(content))
        elif content:
            paragraphs.append(str(content))
    table = None
    for item in fragment.get("tables") or []:
        if isinstance(item, dict) and item.get("columns") and item.get("rows"):
            table = {"columns": item["columns"], "rows": item["rows"]}
            break
    return {
        "heading": fragment.get("title") or fallback_title,
        "paragraphs": paragraphs,
        "bullets": bullets,
        "table": table,
        "section_id": fragment.get("section_id"),
    }


def assemble_word_artifact(brief: dict[str, Any], word_plan: dict[str, Any], fragments: dict[str, dict[str, Any]]) -> dict[str, Any]:
    sections = []
    for chapter in word_plan.get("outline", []):
        for row in chapter.get("sections", []):
            fragment = fragments.get(row["section_id"])
            if not fragment:
                continue
            sections.append(fragment_to_section(fragment, row.get("section_title") or row["section_id"]))
    return {
        "title": f"{brief.get('project_name', '本项目')}｜{brief.get('scenario', '物业服务方案')}",
        "subtitle": brief.get("project_type") or "物业服务方案",
        "lead": [
            f"本方案按统一Document Plan分章节编制，项目为{brief.get('project_name', '本项目')}。",
            "结构、需求覆盖和篇幅由MMF规划层控制，模型仅撰写已确定的Section。",
        ],
        "sections": sections,
    }


def collect_fragments(run_dir: Path) -> dict[str, dict[str, Any]]:
    fragments = {}
    root = run_dir / "longform" / "word" / "sections"
    if not root.is_dir():
        return fragments
    for path in root.glob("*/fragment.json"):
        fragments[path.parent.name] = json.loads(path.read_text(encoding="utf-8-sig"))
    return fragments


def collect_gates(run_dir: Path) -> list[dict[str, Any]]:
    gates = []
    root = run_dir / "longform" / "word" / "sections"
    if not root.is_dir():
        return gates
    for path in list(root.glob("*/qa.json")) + list(root.glob("*/governance.json")):
        gates.append(json.loads(path.read_text(encoding="utf-8-sig")))
    return gates


def generate_longform(
    *,
    run_dir: Path,
    provider: Any,
    provider_name: str,
    brief: dict[str, Any],
    selection: dict[str, Any],
    selected_ids: list[str],
    section_ids: list[str] | None = None,
    task_mode: str | None = None,
    require_section_min: bool | None = None,
) -> dict[str, Any]:
    pack = load_requirement_pack(run_dir)
    mode = resolve_task_mode(brief, task_mode)
    knowledge = _normalize_knowledge(selection, selected_ids)
    existing_plan = run_dir / "01_word_document_plan.json"
    have_all_plan = existing_plan.is_file() and all((run_dir / name).is_file() for name in PLAN_FILES.values())
    if have_all_plan:
        bundle = {key: json.loads((run_dir / name).read_text(encoding="utf-8-sig")) for key, name in PLAN_FILES.items()}
        bundle.setdefault("validation", {"status": "PASS", "mode": "reused"})
        analysis = json.loads((run_dir / "canonical_tender_analysis.json").read_text(encoding="utf-8-sig")) if (run_dir / "canonical_tender_analysis.json").is_file() else build_canonical_tender_analysis(pack, brief)
        requirement_map = json.loads((run_dir / "canonical_requirement_map.json").read_text(encoding="utf-8-sig")) if (run_dir / "canonical_requirement_map.json").is_file() else build_canonical_requirement_map(pack, bundle["requirement_matrix"])
        project_brief = json.loads((run_dir / "canonical_project_brief.json").read_text(encoding="utf-8-sig")) if (run_dir / "canonical_project_brief.json").is_file() else build_canonical_project_brief(pack, brief, analysis)
    else:
        bundle = build_planning_bundle(pack, brief, knowledge, production=True)
        if bundle["validation"]["status"] != "PASS":
            raise PlanningError("生产规划门禁未通过：" + json.dumps(bundle["validation"].get("checks"), ensure_ascii=False))
        write_plan_artifacts(run_dir, bundle)
        analysis = build_canonical_tender_analysis(pack, brief)
        requirement_map = build_canonical_requirement_map(pack, bundle["requirement_matrix"])
        project_brief = build_canonical_project_brief(pack, brief, analysis)
        write_canonical_bundle(run_dir, analysis, requirement_map, project_brief)
    contracts = overlay_knowledge(bundle["section_contracts"], knowledge)
    all_ids = [row["section_id"] for row in contracts["contracts"]]
    chosen = section_ids if section_ids is not None else mode_section_ids(mode, all_ids)
    factory = LongformGenerationFactory(
        run_root=run_dir,
        provider=provider,
        provider_name=provider_name,
        inputs={
            "word_plan": bundle["word_plan"],
            "requirement_matrix": bundle["requirement_matrix"],
            "section_contracts": contracts,
            "global_state": bundle["global_state"],
            "ppt_plan": bundle["ppt_plan"],
            "dependency_map": bundle["dependency_map"],
            "knowledge_selection": knowledge,
            "require_section_min": require_section_min if require_section_min is not None else (mode == "full_longform" or bool(chosen and len(chosen) <= 6)),
        },
    )
    medium = str(brief.get("medium") or "WORD").upper()
    word = {"status": "SKIPPED"}
    ppt = {"status": "SKIPPED"}
    if medium == "PPT":
        storyboard = bundle["ppt_plan"].get("slide_storyboard") or []
        slide_ids = None if mode == "full_longform" else [row["slide_id"] for row in storyboard[:8]]
        ppt = factory.generate_ppt(slide_ids)
        slides = []
        for path in sorted((run_dir / "longform" / "ppt" / "slides").glob("*/payload.json")):
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            slides.append({
                "title": payload.get("headline") or path.parent.name,
                "core_message": payload.get("key_message") or "",
                "layout": "overview",
                "bullets": [str(item) for block in payload.get("content_blocks") or [] for item in (block.get("items") or block.get("points") or [])][:6],
            })
        artifact = {
            "title": f"{brief.get('project_name', '本项目')}｜{brief.get('scenario', '物业服务方案')}",
            "subtitle": brief.get("project_type") or "",
            "slides": slides,
        }
        fragments = {path.parent.name: json.loads(path.read_text(encoding="utf-8-sig")) for path in (run_dir / "longform" / "ppt" / "slides").glob("*/payload.json")}
        gates = []
    else:
        word = factory.generate_word(chosen)
        fragments = collect_fragments(run_dir)
        gates = collect_gates(run_dir)
        artifact = assemble_word_artifact(brief, bundle["word_plan"], fragments)
    total_chars = len(visible_text(artifact))
    depth = evaluate_longform_depth(
        task_mode=mode,
        word_plan=bundle["word_plan"],
        contracts=contracts,
        matrix=bundle["requirement_matrix"],
        fragments=fragments,
        gates=gates,
        total_effective_chars=total_chars,
    )
    generated = {
        "artifact": artifact,
        "citation_registry": [{"claim": "方案结构由MMF Document Plan确定", "source_type": "current_project_fact", "source_id": "canonical_project_brief"}],
        "guardrail_non_use": [],
        "clarification_list": list(bundle["global_state"].get("open_clarifications") or [])[:12],
        "provider_metadata": getattr(provider, "get_metadata", lambda: {})(),
        "longform": {
            "LONGFORM_ORCHESTRATOR": "ACTIVE",
            "ONE_SHOT_FULL_DOCUMENT_GENERATION": False,
            "SECTION_LEVEL_GENERATION": True,
            "task_mode": mode,
            "word_summary": word,
            "capability": factory.capability,
        },
    }
    write_json(run_dir / "generation_raw.json", generated)
    write_json(run_dir / "longform_depth_qa.json", depth)
    write_json(run_dir / "orchestrator_status.json", {
        "LONGFORM_ORCHESTRATOR": "ACTIVE",
        "DOCUMENT_PLAN": bundle["validation"]["status"],
        "REQUIREMENT_SECTION_MATRIX": "PASS" if bundle.get("requirement_matrix") else "FAIL",
        "CONTENT_BUDGET": "PASS" if bundle.get("content_budget") else "FAIL",
        "SECTION_CONTRACTS": "PASS" if contracts.get("contracts") else "FAIL",
        "ONE_SHOT_FULL_DOCUMENT_GENERATION": False,
        "SECTION_LEVEL_GENERATION": True,
        "SECTION_CHECKPOINT": "PASS" if (run_dir / "checkpoint" / "state.json").is_file() else "FAIL",
        "SECTION_CONTINUATION": "PASS" if any((run_dir / "longform" / "word" / "sections").glob("*/generation.json")) else "FAIL",
        "CANONICAL_PLAN_PROVIDER_INDEPENDENT": "PASS",
        "PROVIDER_EFFECTIVE_SETTINGS_AUDIT": "PASS" if factory.capability else "FAIL",
        "LONGFORM_DEPTH_GATE": depth["status"],
        "task_mode": mode,
        "updated_at": now_iso(),
    })
    return {
        "generated": generated,
        "bundle": bundle,
        "word": word,
        "depth": depth,
        "capability": factory.capability,
        "task_mode": mode,
        "total_effective_chars": total_chars,
        "content_units": content_units(visible_text(artifact)),
        "profile": resolve_profile(provider_name, getattr(provider, "config", {})),
    }
