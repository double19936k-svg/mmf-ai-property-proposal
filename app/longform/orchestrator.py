from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from governance.artifact_qa import evaluate_artifact, flatten_structured_text
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
from .eta import estimate_runtime
from .factory import LongformGenerationFactory, content_units, keyword_coverage, read_json, visible_text
from .reasoning import normalize_speed_profile, policy_reasoning_for_stage


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
            paragraphs.extend(flatten_structured_text(block))
            continue
        kind = str(block.get("type") or "")
        content = block.get("content")
        if content is None or content == "":
            content = block.get("text") or ""
        items = block.get("items") or block.get("points") or []
        if kind in {"bullet_group", "numbered_steps"}:
            nested = flatten_structured_text(items) or flatten_structured_text(content)
            bullets.extend(nested)
            if items and content is not None and content != "" and content is not items:
                extra = flatten_structured_text(content)
                for line in extra:
                    if line not in bullets:
                        paragraphs.append(line)
        elif kind == "subheading":
            paragraphs.extend(flatten_structured_text(content))
        else:
            paragraphs.extend(flatten_structured_text(content))
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
            f"本方案结合{brief.get('project_name', '本项目')}的项目条件、服务范围及已确认需求编制。",
            "方案围绕服务组织、专业运营、风险响应与质量改进展开，具体实施安排以双方确认的项目要求为依据。",
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
    speed_profile: str | None = None,
    max_parallelism: int | None = None,
    history_path: Any = None,
) -> dict[str, Any]:
    pack = load_requirement_pack(run_dir)
    mode = resolve_task_mode(brief, task_mode)
    speed = normalize_speed_profile(speed_profile or brief.get("speed_profile") or "balanced")
    wall_started = time.monotonic()
    planning_started = wall_started
    knowledge = _normalize_knowledge(selection, selected_ids)
    existing_plan = run_dir / "01_word_document_plan.json"
    have_all_plan = existing_plan.is_file() and all((run_dir / name).is_file() for name in PLAN_FILES.values())
    if have_all_plan:
        bundle = {key: json.loads((run_dir / name).read_text(encoding="utf-8-sig")) for key, name in PLAN_FILES.items()}
        bundle.setdefault("validation", {"status": "PASS", "mode": "reused"})
        if (run_dir / "adaptive_section_decision.json").is_file():
            bundle["adaptive_section_decision"] = json.loads((run_dir / "adaptive_section_decision.json").read_text(encoding="utf-8-sig"))
        analysis = json.loads((run_dir / "canonical_tender_analysis.json").read_text(encoding="utf-8-sig")) if (run_dir / "canonical_tender_analysis.json").is_file() else build_canonical_tender_analysis(pack, brief)
        requirement_map = json.loads((run_dir / "canonical_requirement_map.json").read_text(encoding="utf-8-sig")) if (run_dir / "canonical_requirement_map.json").is_file() else build_canonical_requirement_map(pack, bundle["requirement_matrix"])
        project_brief = json.loads((run_dir / "canonical_project_brief.json").read_text(encoding="utf-8-sig")) if (run_dir / "canonical_project_brief.json").is_file() else build_canonical_project_brief(pack, brief, analysis)
    else:
        bundle = build_planning_bundle(pack, brief, knowledge, production=True)
        if bundle["validation"]["status"] != "PASS":
            raise PlanningError("生产规划门禁未通过：" + json.dumps(bundle["validation"].get("checks"), ensure_ascii=False))
        write_plan_artifacts(run_dir, bundle)
        if bundle.get("adaptive_section_decision"):
            write_json(run_dir / "adaptive_section_decision.json", bundle["adaptive_section_decision"])
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
            # FAST_MODE may change reasoning/latency only. Full proposals keep the same
            # section minimum and content budget as balanced/deep.
            "require_section_min": True if mode == "full_longform" else (require_section_min if require_section_min is not None else bool(chosen and len(chosen) <= 6)),
            "speed_profile": speed,
            "max_parallelism": max_parallelism if max_parallelism is not None else brief.get("max_parallelism"),
            "history_path": history_path or (run_dir.parent.parent / "runtime" / "latency_history.jsonl"),
        },
    )
    planning_seconds = round(time.monotonic() - planning_started, 3)
    generation_started = time.monotonic()
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
        try:
            word = factory.generate_word(chosen)
        except Exception:
            factory.performance["phase"] = "generation"
            factory.performance["generation_seconds"] = round(time.monotonic() - generation_started, 3)
            factory.performance["provider_wait_seconds"] = round(float(factory.provider_wait_seconds or 0), 3)
            factory.performance["token_accounting"] = factory.token_accountant.summary()
            raise
        fragments = collect_fragments(run_dir)
        gates = collect_gates(run_dir)
        artifact = assemble_word_artifact(brief, bundle["word_plan"], fragments)
    generation_seconds = round(time.monotonic() - generation_started, 3)
    qa_started = time.monotonic()
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
    coverage_gate = evaluate_requirement_coverage_regression(
        bundle.get("requirement_matrix") or {},
        fragments,
        expected_section_ids=chosen if medium != "PPT" else None,
        task_mode=mode,
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
            "GENERATION_BATCH_LAYER": True,
            "task_mode": mode,
            "speed_profile": speed,
            "word_summary": word,
            "capability": factory.capability,
            "QUALITY_REGRESSION_GATE": coverage_gate.get("status"),
            "planning": {
                "execution": "local_deterministic",
                "provider_invoked": False,
                "reasoning_policy": policy_reasoning_for_stage(speed, "planning"),
                "note": "Document Plan is built locally; high reasoning is configured policy, not an actual provider call.",
            },
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
        "QUALITY_REGRESSION_GATE": coverage_gate.get("status"),
        "GENERATION_BATCH_LAYER": True,
        "task_mode": mode,
        "speed_profile": speed,
        "logical_section_count": (word or {}).get("logical_section_count"),
        "generation_batch_count": (word or {}).get("generation_batch_count"),
        "provider_call_count": factory.provider_call_count,
        "updated_at": now_iso(),
    })
    qa_seconds = round(time.monotonic() - qa_started, 3)
    wall_elapsed = round(time.monotonic() - wall_started, 3)
    performance = dict(factory.performance or {})
    performance.update({
        "planning_seconds": planning_seconds,
        "generation_seconds": performance.get("generation_seconds") if performance.get("generation_seconds") is not None else generation_seconds,
        "generation_wall_seconds": generation_seconds,
        "provider_wait_seconds": round(float(performance.get("provider_wait_seconds") or factory.provider_wait_seconds or 0), 3),
        "qa_seconds": qa_seconds,
        "wall_elapsed_seconds": wall_elapsed,
        "repair_reasoning": performance.get("repair_reasoning") or "not_invoked",
        "token_accounting": performance.get("token_accounting") or factory.token_accountant.summary(),
    })
    factory.performance = performance
    eta = estimate_runtime(
        provider=provider_name,
        model=str(((factory.capability.get("effective_settings") or {}).get("model") or "")),
        mode=speed,
        stage="draft",
        history_path=factory.history_path,
        section_count=(word or {}).get("logical_section_count"),
    )
    return {
        "generated": generated,
        "bundle": bundle,
        "word": word,
        "depth": depth,
        "capability": factory.capability,
        "task_mode": mode,
        "speed_profile": speed,
        "adaptive_section_decision": bundle.get("adaptive_section_decision"),
        "quality_regression": coverage_gate,
        "performance": performance,
        "eta": eta,
        "total_effective_chars": total_chars,
        "content_units": content_units(visible_text(artifact)),
        "profile": resolve_profile(provider_name, getattr(provider, "config", {}), speed_profile=speed, stage="draft"),
        "planning": generated["longform"]["planning"],
    }


def evaluate_requirement_coverage_regression(
    matrix: dict[str, Any],
    fragments: dict[str, dict[str, Any]],
    *,
    expected_section_ids: list[str] | None = None,
    task_mode: str | None = None,
) -> dict[str, Any]:
    generated_ids = set(fragments)
    rows = [row for row in matrix.get("matrix") or [] if row.get("coverage_status") == "MAPPED"]
    scoped = []
    for row in rows:
        if row.get("mandatory_level") != "MUST" and not row.get("scoring_item_id"):
            continue
        owners = [row.get("primary_section_id"), *(row.get("secondary_section_ids") or [])]
        owners = [sid for sid in owners if sid]
        if expected_section_ids is not None:
            if not any(sid in set(expected_section_ids) for sid in owners):
                continue
        scoped.append(row)
    if not scoped:
        if any(row.get("mandatory_level") == "MUST" for row in rows) and not generated_ids:
            missing = [row.get("requirement_id") for row in rows if row.get("mandatory_level") == "MUST"]
            return {"status": "FAIL", "planned_must": len(missing), "covered_must": 0, "missing": missing, "QUALITY_REGRESSION_GATE": "FAIL"}
        return {"status": "PASS", "planned_must": 0, "covered_must": 0, "missing": [], "QUALITY_REGRESSION_GATE": "PASS"}
    missing = []
    covered = 0
    for row in scoped:
        owners = [sid for sid in [row.get("primary_section_id"), *(row.get("secondary_section_ids") or [])] if sid]
        present = [sid for sid in owners if sid in generated_ids]
        if not present:
            missing.append(row.get("requirement_id"))
            continue
        req_text = str(row.get("requirement_text") or "")
        text = "\n".join(visible_text(fragments[sid]) for sid in present)
        if not req_text:
            covered += 1
            continue
        if keyword_coverage(req_text, text) == "COVERED":
            covered += 1
            continue
        all_text = "\n".join(visible_text(frag) for frag in fragments.values())
        if keyword_coverage(req_text, all_text) == "COVERED":
            covered += 1
        else:
            missing.append(row.get("requirement_id"))
    status = "PASS" if not missing else "FAIL"
    return {
        "status": status,
        "planned_must": len(scoped),
        "covered_must": covered,
        "missing": missing,
        "QUALITY_REGRESSION_GATE": status,
        "task_mode": task_mode,
    }
