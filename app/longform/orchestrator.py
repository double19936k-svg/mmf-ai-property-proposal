from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parents[1]

from governance.artifact_qa import evaluate_artifact, flatten_structured_text
from governance import apply_artifact_repairs, apply_local_repairs
from governance.consistency import REGISTRY_KEYS_ALL, check_cross_section_consistency
from governance.document_qa import count_artifact_structure, estimate_rendered_pages, evaluate_document_scale
from governance.longform_qa import evaluate_longform_depth
from planning.budget_control import (
    BUDGET_LEDGER_FILE,
    REDUCTION_PLAN_FILE,
    REBALANCE_FILE,
    apply_rebalanced_targets,
    observed_inflation,
    read_ledger,
    rebalance_pending_targets,
    targeted_reduction_plan,
)
from planning.page_calibration import calibrate_ruler, classify_pages, page_report
from planning.page_contract import evaluate_page_contract
from planning.unified_budget import (
    ARTIFACT_CHARS_PER_LEDGER_CHAR_RUN_D,
    BUDGET_GATE_FILE,
    artifact_chars_from_ledger,
    classify_page_state,
)
from planning.canonical import (
    build_canonical_project_brief,
    build_canonical_requirement_map,
    build_canonical_tender_analysis,
    mode_section_ids,
    resolve_task_mode,
    write_canonical_bundle,
)
from planning.hierarchy import _planned_section_ids, update_scoring_fulfillment
from planning.scoring_taxonomy import (  # noqa: F401
    SCORING_MAPPING_GATE_FILE, SCORING_TAXONOMY_VERSION, classify_scoring_row)
from .scoring_authority import canonical_scoring_authority
from planning.scope_gated_requirements import (
    REQUIREMENT_DENOMINATOR, evidence_alias_hits, scope_gated_denominator, split_requirement_evidence,
)
from planning.content_engineering import content_quality_report, evaluate_bidder_evidence, repair_template_leakage
from planning.content_padding import content_padding_report
from planning.planner import PlanningError, build_planning_bundle, now_iso, write_json
from providers.capability import resolve_profile
from workflow_timing import record_stage_duration
from .eta import estimate_runtime
from .document_repair import build_repair_plan
from .reduction import EXECUTION_FILE, execute_reduction_plan
from .factory import (
    LongformGenerationFactory,
    _repair_customer_text,
    content_units,
    keyword_coverage,
    read_json,
    visible_text,
)
from .cu_state import SECTION_DRAFTABLE_STATES, SECTION_PASS_STATES
from .final_assembly import _strip_scoring, build_ppt_slide_data
from .hierarchical import (
    VOLUME_ARTIFACT_CONTRACT, apply_plan_invalidation, load_or_build_volume_plan,
    volume_artifact_contract, volume_word_plan, atomic_write_json,
)
from .local_normalize import run_local_normalization
from planning.hierarchy import compute_lineage, plan_invalidation
from .reasoning import normalize_speed_profile, policy_reasoning_for_stage


PLAN_FILES = {
    "word_plan": "01_word_document_plan.json",
    "requirement_matrix": "02_requirement_section_matrix.json",
    "content_budget": "03_word_content_budget.json",
    "section_contracts": "04_section_contracts.json",
    "global_state": "05_document_global_state_v0.json",
    "ppt_plan": "06_ppt_presentation_plan.json",
    "dependency_map": "07_cross_section_dependency.json",
    "requirement_classification": "11_requirement_classification.json",
    "commercial_compliance_matrix": "12_commercial_compliance_matrix.json",
    "bidder_evidence_ledger": "13_bidder_evidence_ledger.json",
    "technical_scope_gate": "14_technical_scope_gate.json",
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
    # MMF-007A 增量产物（08 分层文档树 / 09 Scoring Matrix / 10 谱系）：
    # 仅在 Requirement Pack 存在时追加写入，任何失败不得影响既有规划链。
    try:
        from planning.hierarchy import attach_hierarchy_artifacts
        hierarchy_paths = attach_hierarchy_artifacts(run_dir, bundle)
        if hierarchy_paths:
            written.update(hierarchy_paths)
    except Exception:
        pass
    return written


def overlay_knowledge(contracts: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
    selected = [row.get("ku_id") for row in selection.get("knowledge_usage_contracts", []) if row.get("selection_status") == "SELECTED"]
    conditional = [row.get("ku_id") for row in selection.get("knowledge_usage_contracts", []) if row.get("selection_status") == "CONDITIONAL"]
    patched = json.loads(json.dumps(contracts))
    for row in patched.get("contracts", []):
        row["allowed_knowledge"] = selected
        row["conditional_knowledge"] = conditional
    return patched


def fragment_to_section(fragment: dict[str, Any], fallback_title: str, *, section_id: str | None = None, allow_unresolved_tables: bool = False) -> dict[str, Any]:
    from governance.table_fidelity import require_source_tables, check_source_table
    from governance.table_context import restore_negative_cells
    restored = [restore_negative_cells(t) if isinstance(t, dict) else (t, []) for t in fragment.get('tables') or []]
    table_repairs = [action for _, actions in restored for action in actions]
    fragment = {**fragment, 'tables': [table for table, _ in restored]}
    source_findings = [row for table in fragment.get('tables') or [] if isinstance(table, dict)
                       for row in check_source_table(table)]
    if not allow_unresolved_tables:
        require_source_tables(fragment.get('tables') or [])
    def _items(value: Any) -> list[Any]:
        # Structured providers sometimes return a numeric count (for example
        # steps=7) instead of the actual list. Counts are metadata, not content.
        if isinstance(value, (list, tuple)):
            return list(value)
        if isinstance(value, dict):
            return [value]
        return []

    paragraphs: list[str] = []
    bullets: list[str] = []
    content_blocks: list[dict[str, Any]] = []
    for block in fragment.get("body_blocks") or []:
        block_start=len(content_blocks)
        if not isinstance(block, dict):
            paragraphs.extend(flatten_structured_text(block))
            content_blocks.extend({'type':'paragraph','content':t} for t in flatten_structured_text(block))
            continue
        kind = str(block.get("type") or "")
        content = block.get("content")
        if content is None or content == "":
            content = block.get("text") or ""
        items = _items(block.get("items") or block.get("points"))
        if kind in {"bullet_group", "numbered_steps"}:
            nested = flatten_structured_text(items) or flatten_structured_text(content)
            bullets.extend(nested)
            content_blocks.append({'type':kind,'items':nested})
            if items and content is not None and content != "" and content is not items:
                extra = flatten_structured_text(content)
                for line in extra:
                    if line not in bullets:
                        paragraphs.append(line)
                        content_blocks.append({'type':'paragraph','content':line})
        elif kind == "subheading":
            paragraphs.extend(flatten_structured_text(content))
            content_blocks.extend({'type':'subheading','content':t} for t in flatten_structured_text(content))
        else:
            paragraphs.extend(flatten_structured_text(content))
            content_blocks.extend({'type':'paragraph','content':t} for t in flatten_structured_text(content))
        for item in content_blocks[block_start:]:
            item['source_content_unit_id']=block.get('source_content_unit_id')
    tables = []
    for item in fragment.get("tables") or []:
        if not isinstance(item, dict):
            continue
        columns = _items(item.get("columns") or item.get("headers"))
        rows = _items(item.get("rows"))
        if columns and rows and all(isinstance(row, (list, tuple)) for row in rows):
            tables.append({"title": item.get("title"), "columns": columns, "rows": item["rows"], "notes": item.get("notes"),
                           **{key: item[key] for key in ('table_source_contract', 'source_content_unit_id', 'archetype') if key in item}})
    for process_index, process in enumerate(fragment.get("processes") or [], 1):
        if isinstance(process, dict):
            steps = _items(process.get("steps"))
            if steps:
                supplied = str(process.get('purpose') or process.get('title') or '').strip()
                label = supplied if supplied and supplied != '实施流程' and '步骤' not in supplied else (str(fallback_title or '').strip() or '本节流程')
                paragraphs.append(label)
                bullets.extend(str(step) for step in steps)
                cu_id=process.get('source_content_unit_id')
                process_blocks=[{'type':'subheading','content':label,'source_content_unit_id':cu_id},
                                {'type':'numbered_steps','items':[str(step) for step in steps],'source_content_unit_id':cu_id}]
                positions=[i for i,b in enumerate(content_blocks) if cu_id and b.get('source_content_unit_id')==cu_id]
                if positions:
                    at=positions[-1]+1
                    # Preserve the source introduction and make its destination
                    # explicit now that the steps are next to their own CU body.
                    for item in content_blocks[:at]:
                        if item.get('source_content_unit_id')==cu_id and item.get('content','').endswith('具体步骤如下'):
                            item['content']=item['content'][:-len('具体步骤如下')]+'步骤见下。'
                    content_blocks[at:at]=process_blocks
                else:
                    content_blocks.extend(process_blocks)
    from governance.proposal_fidelity import place_content_blocks
    content_blocks = place_content_blocks(content_blocks, tables)
    return {
        "heading": fragment.get("title") or fallback_title,
        "paragraphs": paragraphs,
        "bullets": bullets,
        "tables": tables,
        "content_blocks": content_blocks,
        "level": 2,
        # 章节身份只能来自大纲契约：Provider 回显的 section_id 在上一轮真实运行里是
        # "相关专项安排"这类中文归类词，26 个正文节全部失去可寻址身份（19 个共用同一个
        # 标签），任何按 section_id 找回契约的动作（补深、修复归属）都会落空。
        "section_id": str(section_id or fragment.get("section_id") or ""),
        "source_table_repairs": table_repairs,
        "source_table_findings": source_findings,
    }


def assemble_word_artifact(brief: dict[str, Any], word_plan: dict[str, Any], fragments: dict[str, dict[str, Any]], *, include_toc: bool = True, allow_unresolved_tables: bool = False) -> dict[str, Any]:
    from governance.proposal_fidelity import filter_exclusion_tables, gap_stub, recall_missing_facts
    whitelist = "\n".join([
        str((word_plan or {}).get("mandatory_scope_text") or ""),
        str((brief or {}).get("mandatory_scope_text") or ""),
        str((brief or {}).get("location") or ""),
    ])
    # The confirmed brief stores service_scope as prose. Only a structured
    # scope object has an excluded list; a string must not be treated as one.
    scope = (brief or {}).get("service_scope") if isinstance(brief, dict) else None
    excluded_rows = scope.get("excluded") if isinstance(scope, dict) else None
    if not excluded_rows:
        excluded_rows = (word_plan or {}).get("excluded_scope") if isinstance(word_plan, dict) else None
    if isinstance(excluded_rows, str):
        excluded_rows = [excluded_rows] if excluded_rows.strip() else []
    elif not isinstance(excluded_rows, list):
        excluded_rows = []
    excluded_text = "\n".join(str(row.get("text") if isinstance(row, dict) else row) for row in excluded_rows)
    sections = []
    for chapter in word_plan.get("outline", []):
        rows = [row for row in chapter.get("sections", []) if isinstance(row, dict)]
        present = {row.get("section_id") for row in rows if row.get("section_id") in fragments}
        emitted = []
        for index, row in enumerate(rows):
            sid = row.get("section_id")
            fragment = fragments.get(sid) if sid else None
            if fragment:
                emitted.append(fragment_to_section(
                    fragment, row.get("section_title") or sid, section_id=sid, allow_unresolved_tables=allow_unresolved_tables))
                continue
            before = any(item.get("section_id") in present for item in rows[:index])
            after = any(item.get("section_id") in present for item in rows[index + 1:])
            if sid and before and after:
                emitted.append(gap_stub(str(row.get("section_title") or sid), str(sid)))
        if not emitted:
            continue
        sections.append({"heading": chapter.get("chapter_title") or chapter.get("chapter_id"), "paragraphs": [], "bullets": [], "tables": [], "level": 1, "section_id": chapter.get("chapter_id")})
        sections.extend(filter_exclusion_tables(section, whitelist, excluded_text) for section in emitted)
    recall_missing_facts(sections, brief)
    return {
        "title": f"{brief.get('project_name', '本项目')}｜{brief.get('scenario', '物业服务方案')}",
        "subtitle": brief.get("project_type") or "物业服务方案",
        "lead": [
            f"本方案结合{brief.get('project_name', '本项目')}的项目条件、服务范围及已确认需求编制。",
            "方案围绕服务组织、专业运营、风险响应与质量改进展开，具体实施安排以双方确认的项目要求为依据。",
        ],
        "sections": sections,
        "include_toc": bool(include_toc),
        "toc_depth": int(brief.get("toc_depth") or 1),
        "source_table_findings": [item for section in sections for item in section.get('source_table_findings', [])],
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


def run_document_level_qa(
    run_dir: Path,
    *,
    brief: dict[str, Any],
    bundle: dict[str, Any],
    contracts: dict[str, Any],
    mode: str,
    medium: str,
    chosen: list[str] | None,
    fragments: dict[str, dict[str, Any]],
    gates: list[dict[str, Any]],
    artifact: dict[str, Any],
) -> dict[str, Any]:
    """Whole-Artifact QA（§20-24）：只读已落盘片段做装配与检查，零 Provider 调用。

    选择性修复之后可以原样再跑一次，得到的就是"复核后的文档级事实"，
    不需要重新规划、也不需要重新生成整篇文档。
    """
    from planning.ppt_service import VERSION as PPT_SERVICE_VERSION, content_qa, visible_parts
    if medium == "PPT" and bundle.get("ppt_plan", {}).get("profile_p_contract") == PPT_SERVICE_VERSION:
        check = content_qa(artifact, bundle["ppt_plan"])
        write_json(run_dir / "ppt_content_qa.json", check)
        findings = check["findings"]
        blocking_findings = [f for f in findings if f.get("severity") == "BLOCK"]
        blocked_ids = {f.get("slide_id") for f in blocking_findings}
        for finding in blocking_findings:
            blocked_ids.update(finding.get("slide_ids") or [])
        depth_status = "DEPTH_INCOMPLETE" if blocking_findings else "NEEDS_REVIEW"
        return {
            "artifact": artifact, "template_repairs": 0,
            "total_chars": sum(len(re.sub(r"\s", "", t)) for s in artifact.get("slides", []) for t in visible_parts(s)),
            "depth": {"status": depth_status, "LONGFORM_DEPTH_GATE": depth_status,
                "findings": findings, "planned_content_units": check["planned_content_slides"],
                "completed_content_units": check["complete_content_slides"], "basis": PPT_SERVICE_VERSION},
            "bidder_evidence_gate": {"status": "NOT_APPLICABLE", "reason": "PPT service proposals do not infer bidder credentials"},
            "budget_quality": {"status": "NOT_APPLICABLE", "reason": "Word character budget is replaced by PPT information completeness"},
            "padding_quality": {"status": "NEEDS_REVIEW", "reason": "Owner-facing information density requires rendered review"},
            "coverage_gate": {"status": "FAIL" if blocking_findings else "NEEDS_REVIEW",
                "requirements": check["requirement_trace"], "semantic_coverage": "PENDING_REVIEW"},
            "scoring_fulfillment": None,
            "consistency": {"status": "BLOCK" if blocking_findings else "NEEDS_REVIEW", "findings": findings},
            "document_scale": {"status": "PENDING_RENDER", "minimum_total_pages": artifact.get("output_contract",{}).get("min_total_pages",15)},
            "rendered_pages": None, "hierarchy_metrics": {"content_units": check["planned_content_slides"]},
            "checkpoint_metrics": {"completed_content_units": [
                s["slide_id"] for s in artifact.get("slides", []) if s.get("slide_id") not in blocked_ids]},
        }
    if medium != "PPT":
        artifact, template_repairs = repair_template_leakage(artifact, str(brief.get("project_type") or ""))
    else:
        template_repairs = 0
    total_chars = len(visible_text(artifact))
    depth = evaluate_longform_depth(
        task_mode=mode,
        word_plan=bundle["word_plan"],
        contracts=contracts,
        matrix=bundle["requirement_matrix"],
        fragments=fragments,
        gates=gates,
        total_effective_chars=total_chars,
        content_budget=bundle.get("content_budget"),
    )
    bidder_evidence_gate = evaluate_bidder_evidence(artifact, bundle.get("bidder_evidence_ledger") or {}) if medium != "PPT" else {"status": "NOT_APPLICABLE", "findings": []}
    budget_quality = content_quality_report(artifact, str(brief.get("project_type") or "")) if medium != "PPT" else {"status": "NOT_APPLICABLE"}
    budget_quality["template_leakage_repairs"] = template_repairs
    if budget_quality.get("status") == "DEPTH_GATE_FAIL":
        depth["status"] = "DEPTH_INCOMPLETE"
        depth["LONGFORM_DEPTH_GATE"] = "DEPTH_INCOMPLETE"
        depth.setdefault("findings", []).append({"rule_id": "LF-ANTI-PADDING", "severity": "BLOCK", "issue": "有效内容主要由重复段落构成", "repair_mode": "TARGETED_CONTINUATION"})
    # §45：低信息增量与 §47 的重复检测是两条门。这里只到 NEEDS_REVIEW ——
    # "话很通用"不构成 BLOCK，把通用句标成待复核再交人判，才是 §28–§33 要的分级。
    padding_quality = content_padding_report(artifact, str(brief.get("project_type") or "")) if medium != "PPT" else {"status": "NOT_APPLICABLE"}
    if padding_quality.get("status") == "NEEDS_REVIEW":
        padding_quality.setdefault("findings", []).append({
            "rule_id": "LF-GENERIC-PADDING", "severity": "NEEDS_REVIEW",
            "issue": f"低信息增量句子占比 {padding_quality.get('item_share', {}).get('GENERIC_PADDING', 0):.1%}"
                     f"（无可核查载体且无技术主题词），超限值 "
                     f"{padding_quality.get('review_thresholds', {}).get('padding_item_share', 0):.0%}",
            "repair_mode": "REPLACE_WITH_STRUCTURED_CARRIER"})
    scoring_class_map, scoring_class_map_stats = _scoring_class_by_requirement(
        run_dir / "09_scoring_matrix.json")
    scoring_fulfillment = None
    scoring_coverage_evidence = {}
    scoring_matrix_path = run_dir / "09_scoring_matrix.json"
    if medium != "PPT" and scoring_matrix_path.is_file():
        try:
            scoring_fulfillment = update_scoring_fulfillment(scoring_matrix_path, fragments)
            scoring_coverage_evidence = _scoring_requirement_evidence(
                json.loads(scoring_matrix_path.read_text(encoding='utf-8-sig')))
        except Exception:
            scoring_fulfillment = {"error": "fulfillment update failed"}
    coverage_gate = evaluate_requirement_coverage_regression(
        bundle.get("requirement_matrix") or {},
        fragments,
        expected_section_ids=chosen if medium != "PPT" else None,
        task_mode=mode,
        scope_gate=bundle.get("technical_scope_gate"),
        scoring_class_by_requirement=scoring_class_map,
        scoring_class_map=scoring_class_map_stats,
        scoring_evidence_by_requirement=scoring_coverage_evidence,
    )
    if medium == "PPT":
        consistency_result: dict[str, Any] = {
            "schema_version": "global-consistency-qa-v0.1",
            "status": "NOT_APPLICABLE",
            "medium": medium,
            "reason": "PPT媒介一致性由PPT专属QA承担；如需文字事实核查需显式启用。",
            "summary": {"issue_count": 0, "checked_fields": [], "not_checked_fields": [], "not_applicable_fields": list(REGISTRY_KEYS_ALL)},
        }
    else:
        try:
            consistency_result = check_cross_section_consistency(fragments, bundle.get("global_state"), run_dir)
        except Exception as exc:
            # V1.1：关键事实检查完全未执行 → 不得 COMPLETED_CLEAN（CS-FAIL 交由
            # app_core 交付门降级为 NEEDS_REVIEW），禁止静默 SKIPPED。
            consistency_result = {
                "schema_version": "global-consistency-qa-v0.1",
                "status": "CONSISTENCY_CHECK_FAILED",
                "medium": medium,
                "error": str(exc)[:200],
                "summary": {"issue_count": 0, "checked_fields": [], "not_checked_fields": list(REGISTRY_KEYS_ALL)},
            }
    # 文档级规模采用复合口径：页数按"到达正文 + 结构化版面"估算，字符量仅作参考。
    # 240k～280k 字符不再是单点 BLOCK，篇幅不足只把复合规模分判到 NEEDS_REVIEW。
    hierarchy_metrics = {}
    hierarchy_path = run_dir / "08_document_hierarchy.json"
    if hierarchy_path.is_file():
        try:
            hierarchy_metrics = json.loads(hierarchy_path.read_text(encoding="utf-8-sig")).get("counts") or {}
        except (OSError, ValueError, TypeError):
            hierarchy_metrics = {}
    checkpoint_metrics = {}
    checkpoint_path = run_dir / "checkpoint" / "state.json"
    if checkpoint_path.is_file():
        try:
            checkpoint_metrics = json.loads(checkpoint_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            checkpoint_metrics = {}
    document_scale: dict[str, Any] = {"status": "NOT_APPLICABLE"}
    rendered_pages: int | None = None
    if medium != "PPT":
        document_scale, rendered_pages = build_document_scale_report(
            bundle,
            artifact,
            fragments,
            total_chars=total_chars,
            coverage_gate=coverage_gate,
            hierarchy_metrics=hierarchy_metrics,
            completed_content_units=checkpoint_metrics.get("completed_content_units") or [],
            semantic_repetition=int((budget_quality or {}).get("semantic_near_duplicate_count") or 0),
            run_dir=run_dir,
        )
        write_json(run_dir / "document_scale_report.json", document_scale)
    return {
        "artifact": artifact,
        "template_repairs": template_repairs,
        "total_chars": total_chars,
        "depth": depth,
        "bidder_evidence_gate": bidder_evidence_gate,
        "budget_quality": budget_quality,
        "padding_quality": padding_quality,
        "coverage_gate": coverage_gate,
        "scoring_fulfillment": scoring_fulfillment,
        "consistency": consistency_result,
        "document_scale": document_scale,
        "rendered_pages": rendered_pages,
        "hierarchy_metrics": hierarchy_metrics,
        "checkpoint_metrics": checkpoint_metrics,
    }


def _local_text_pipeline(fragment: dict[str, Any], source_facts=None) -> dict[str, Any]:
    """Section 提交时使用的同一条文字治理链，供 LOCAL_NORMALIZATION 复用（零 Provider 调用）。"""
    from .factory import _repair_word_artifact
    return _repair_word_artifact(fragment, source_facts)


def _load_word_job(run_dir: Path) -> Any:
    from .factory import LongformGenerationJob
    path = run_dir / "longform" / "word" / "status.json"
    if not path.is_file():
        return None
    try:
        return LongformGenerationJob.load(path)
    except (OSError, ValueError, TypeError):
        return None


DRAFT_PAGE_MEASUREMENT_FLOOR = 150
FIRST_DRAFT_PAGE_CLASSIFICATION_FILE = "first_draft_page_classification.json"


def _read_page_classification(run_dir: Path) -> dict[str, Any]:
    """读取初稿分页实测的页面分类；没有实测就返回空dict（门控据此退回物理页口径）。"""
    path = Path(run_dir) / FIRST_DRAFT_PAGE_CLASSIFICATION_FILE
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def measure_first_draft_pages(run_dir: Path) -> dict[str, Any]:
    """§12：把初稿真正渲染一次，页面缺口才有实测口径可算。

    上一轮的 207 页是 900 字/页估出来的，交付前才发现与真实排版不符；补深若继续用估算，
    就是在用一把没量过的尺子决定要不要再花 Provider 调用。这里只做本地渲染（零调用），
    并且任何失败都退回估算口径 + 如实记录 basis，绝不因为一次测量搞挂整轮运行。

    P0-1 §13：同一份逐页文本还要分类出技术有效页与每技术页实测密度，不得只报物理页数。
    """
    content = (run_dir / "longform" / "word" / "first_draft_content.json").resolve()
    report: dict[str, Any] = {"schema_version": "first-draft-pagination-v0.1", "status": "SKIPPED",
                              "rendered_pages": 0, "provider_called": False}
    if not content.is_file():
        report["status"] = "SKIPPED_NO_DRAFT"
        return report
    work = content.parent
    draft_docx = work / "first_draft_pagination.docx"
    started = time.monotonic()
    try:
        done = subprocess.run([sys.executable, str(APP_ROOT / "build_docx.py"), str(content), str(draft_docx)],
                              cwd=str(APP_ROOT), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=600)
        report["assembly_returncode"] = done.returncode
        if done.returncode != 0 or not draft_docx.is_file():
            report["status"] = "DRAFT_ASSEMBLY_FAILED"
            report["stderr_tail"] = (done.stderr or "")[-300:]
            return report
    except (OSError, subprocess.SubprocessError) as exc:
        report["status"] = "DRAFT_ASSEMBLY_FAILED"
        report["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return report
    from .pagination import page_density, render_document

    # 草稿只为量页而存在：不动目录域、不登记为成果，正式交付件仍由装配链生成。
    rendered = render_document(draft_docx, diagnostics=report)
    if rendered is None:
        report["status"] = "DRAFT_RENDER_FAILED"
        report["seconds"] = round(time.monotonic() - started, 3)
        return report
    _pdf, temp_dir, page_texts, marks = rendered
    try:
        density = page_density(page_texts)
        # §13：物理页之外还必须报"技术有效页"。目录/封面页不承载技术内容，正文起始页
        # 由渲染器给出的大纲标记定，没有标记就按既有口径退到第 3 页并如实记录。
        marked = [int(index) + 1 for _title, index in (marks or []) if int(index) > 0]
        body_start = min(marked) if marked else 3
        classification = classify_pages(page_texts, body_start_page=body_start)
        ruler = calibrate_ruler(classification, visible_chars=classification["visible_chars"],
                                technical_chars=classification["technical_chars"])
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    report.update({
        "status": "MEASURED",
        "rendered_pages": int(density.get("rendered_pages") or 0),
        "visible_chars": int(density.get("visible_chars") or 0),
        "effective_content_pages": int(density.get("effective_content_pages") or 0),
        "thin_page_count": int(density.get("thin_page_count") or 0),
        "body_start_page": body_start,
        "page_classes": classification["counts"],
        "layout_basis": classification["layout_basis"],
        "technical_page_count_bound": classification["technical_page_count_bound"],
        "technical_effective_pages": int(classification["technical_effective_pages"]),
        "front_matter_pages": int(classification["front_matter_pages"]),
        "blank_pages": int(classification["blank_pages"]),
        "placeholder_pages": int(classification["placeholder_pages"]),
        "placeholder_page_ratio": float(classification["placeholder_page_ratio"]),
        "excluded_scope_pages": int(classification["excluded_scope_pages"]),
        "credential_attachment_pages_present": int(classification["credential_attachment_pages_present"]),
        "credential_attachment_pages": int(classification["credential_attachment_pages"]),
        "non_technical_page_ratio": float(classification["non_technical_page_ratio"]),
        "chars_per_technical_page": ruler["chars_per_technical_page"],
        "page_basis": ruler["basis"],
        "page_ruler": ruler,
        "seconds": round(time.monotonic() - started, 3),
    })
    # 单独落一份分类结果：后续整篇 QA（修复后那一轮）要用技术有效页做门控，
    # 而 document_qa_report.json 要到本阶段结束才写。
    write_json(run_dir / FIRST_DRAFT_PAGE_CLASSIFICATION_FILE, {
        "schema_version": "first-draft-page-classification-v0.1",
        "measured_at": now_iso(),
        "provider_called": False,
        "body_start_page": body_start,
        "physical_pages": classification["physical_pages"],
        "page_classes": classification["counts"],
        "layout_basis": classification["layout_basis"],
        "technical_page_count_bound": classification["technical_page_count_bound"],
        "page_class_by_page": classification["page_classes"],
        "page_char_counts": classification["page_char_counts"],
        "technical_effective_pages": classification["technical_effective_pages"],
        "narrative_technical_pages": classification["narrative_technical_pages"],
        "structured_technical_pages": classification["structured_technical_pages"],
        "front_matter_pages": classification["front_matter_pages"],
        "blank_pages": classification["blank_pages"],
        "placeholder_pages": classification["placeholder_pages"],
        "placeholder_page_ratio": classification["placeholder_page_ratio"],
        "excluded_scope_pages": classification["excluded_scope_pages"],
        # §41：附件页是否被生成要在**这一份**里留痕，交付阶段读它才拿得到实测数。
        "excluded_scope_group_hits": classification["excluded_scope_group_hits"],
        "credential_attachment_pages_present": classification["credential_attachment_pages_present"],
        "credential_attachment_pages": classification["credential_attachment_pages"],
        "credential_attachment_page_details": classification["credential_attachment_page_details"],
        "non_technical_page_ratio": classification["non_technical_page_ratio"],
        "visible_chars": classification["visible_chars"],
        "technical_chars": classification["technical_chars"],
        "ruler": ruler,
    })
    return report


def _page_band(technical_pages: int, page_targets: list[int]) -> str:
    """§21 双边门的字面量：UNDER_LENGTH / TARGET_RANGE / OVERSHOOT，量不到就 NOT_MEASURED。

    三态判定只此一处（`planning.unified_budget.classify_page_state`），生成前门控、初稿门控
    与交付端读的是同一把尺子，不再各写一份区间比较。
    """
    if int(technical_pages or 0) <= 0 or max([int(v or 0) for v in (page_targets or [0])] or [0]) <= 0:
        return "NOT_MEASURED"
    return classify_page_state(int(technical_pages), page_targets)


def execute_overshoot_reduction(run_dir: Path, overshoot: dict[str, Any],
                                factory: LongformGenerationFactory) -> dict[str, Any]:
    """§16–§20：超页时把削减计划**真的执行掉**（本地、零 Provider），并就地更新双边门。

    Round 3 只写计划不执行，所以 395 页的 Run D 到收口还是 395 页。这里执行完立刻把
    "削掉的字"折算成"预计少几页"，让双边门读到的是削减后的投影；最终交付页仍然由
    真实分页实测（`document_scale_report.page_band`）说了算，两者不混报。
    """
    if str(overshoot.get("status") or "") != "OVERSHOOT_REDUCTION":
        return {"status": "NOT_REQUIRED", "provider_called": False,
                "compression_pipeline_called": False}
    report = execute_reduction_plan(run_dir, overshoot.get("reduction_plan") or {},
                                    contracts=factory.cu_contracts,
                                    normalize=lambda value: _local_text_pipeline(value, getattr(factory, 'editorial_source_facts', None)))
    for sid, cap in (report.get("pending_section_caps") or {}).items():
        cap_chars = int(cap or 0)
        if cap_chars > 0:
            current = int(factory.budget_unit_caps.get(sid) or 0)
            factory.budget_unit_caps[sid] = min(current, cap_chars) if current else cap_chars
    before = int(overshoot.get("measured_technical_effective_pages") or 0)
    reduction_pages = float(report.get("expected_page_reduction") or 0.0)
    after = max(0, int(round(before - reduction_pages)))
    report["page_projection"] = {
        "measured_before_reduction_pages": before,
        "expected_page_reduction": round(reduction_pages, 2),
        "projected_pages_after_reduction": after,
        "basis": "MEASURED_RENDER_MINUS_LOCAL_REDUCTION_PROJECTION",
    }
    report["page_band_before"] = overshoot.get("page_band")
    report["page_band_after"] = _page_band(after, [overshoot.get("page_floor") or 0,
                                                   overshoot.get("page_ceiling") or 0])
    overshoot["reduction_execution_status"] = report.get("status")
    overshoot["page_band_after_reduction"] = report["page_band_after"]
    overshoot["projected_technical_effective_pages_after_reduction"] = after
    return report


def evaluate_page_overshoot(run_dir: Path, draft_pages: dict[str, Any], *,
                            page_targets: list[int], contracts: Any) -> dict[str, Any]:
    """§21 门控：初稿实测技术有效页超上限 → 禁止继续补深；低于下沿 → 先摊预算再补。

    两条口径分开处理是刻意的：超上限时任何"再补一点"都在往错的页数上加字，所以直接
    禁止 Depth Supplement 并给出定向削减计划；低于下沿时缺口是真需求，但补深之前必须
    先把未生成 Content Unit 的目标按实测超写系数摊开，否则补出来的还是 1.65 倍。
    """
    floor_pages = int(page_targets[0] or 0) if len(page_targets) > 0 else 0
    ceiling = int(page_targets[1] or 0) if len(page_targets) > 1 else 0
    technical_pages = int(draft_pages.get("technical_effective_pages") or 0)
    ruler = draft_pages.get("page_ruler") or {}
    basis = str(draft_pages.get("page_basis") or "NOT_MEASURED")
    common = {
        "schema_version": "page-overshoot-gate-v0.1",
        "provider_called": False,
        "measured_technical_effective_pages": technical_pages,
        "physical_pages": int(draft_pages.get("rendered_pages") or 0),
        "page_floor": floor_pages,
        "page_ceiling": ceiling,
        "page_basis": basis,
        # 文本口径（无逐页版面特征）的技术有效页只会偏高不会偏低：这个方向必须跟着门控走，
        # 否则"这次没超页"会被读成"确实没超页"。
        "technical_page_count_bound": str(draft_pages.get("technical_page_count_bound") or "UNKNOWN"),
        # §21：双边三态与超页/缺页动作同处报告，读门控的人不用再自己比区间。
        "page_band": _page_band(technical_pages, [floor_pages, ceiling]),
        "depth_supplement_forbidden": False,
    }
    if technical_pages <= 0 or ceiling <= 0:
        # 没有实测页数就没有门控：不得因为"量不到"而擅自停止补深，也不得停止判定。
        return {**common, "status": "NOT_EVALUATED",
                "reason": "NO_MEASURED_TECHNICAL_PAGES" if technical_pages <= 0 else "NO_PAGE_CEILING"}
    units = list(contracts.values() if isinstance(contracts, dict) else (contracts or []))
    ledger = read_ledger(run_dir / BUDGET_LEDGER_FILE)
    common["budget_ledger_totals"] = ledger.get("totals") or {}
    common["observed_inflation_factor"] = observed_inflation(ledger)
    if technical_pages > ceiling:
        plan = targeted_reduction_plan(ledger=ledger, content_units=units,
                                       page_targets=(floor_pages, ceiling), ruler=ruler,
                                       measured_technical_pages=technical_pages)
        write_json(run_dir / REDUCTION_PLAN_FILE, plan)
        return {**common, "status": "OVERSHOOT_REDUCTION", "depth_supplement_forbidden": True,
                "reduction_plan": plan, "reduction_plan_path": str(run_dir / REDUCTION_PLAN_FILE)}
    if floor_pages and technical_pages < floor_pages:
        chars_per_page = float(ruler.get("chars_per_technical_page") or 900) or 900.0
        rebalance = rebalance_pending_targets(units, ledger,
                                              budget_chars_target=int(round(floor_pages * chars_per_page)))
        applied = apply_rebalanced_targets(contracts, rebalance)
        write_json(run_dir / REBALANCE_FILE, {**rebalance, "applied": applied})
        return {**common, "status": "UNDER_FLOOR_REBALANCED", "page_gap": floor_pages - technical_pages,
                "rebalancing": {k: v for k, v in rebalance.items() if k != "adjustments"},
                "rebalancing_applied": applied,
                "rebalancing_path": str(run_dir / REBALANCE_FILE)}
    return {**common, "status": "WITHIN_PAGE_TARGETS",
            # 上界口径下已经贴到天花板：真值可能已经在区间之外，不得当作"余量充足"继续补深。
            "at_ceiling_margin": bool(ceiling and technical_pages > ceiling * 0.95)}


def run_bounded_document_repair(
    run_dir: Path,
    factory: LongformGenerationFactory,
    *,
    bundle: dict[str, Any],
    contracts: dict[str, Any],
    word: dict[str, Any],
    qa_pass: dict[str, Any],
) -> dict[str, Any]:
    """按 §25-34 执行一次（且仅一次）有界选择性修复 + 深度补充。"""
    job = _load_word_job(run_dir)
    page_targets = [int(value or 0) for value in ((qa_pass.get("document_scale") or {}).get("page_targets") or [0, 0])]
    # 只有目标区间落在"大部头"这一档才值得为量页花一次本地渲染（10-20 秒，且依赖
    # 本机办公软件）。小文档的页数估算不会决定任何花钱的动作。
    draft_pages = (measure_first_draft_pages(run_dir)
                   if max(page_targets or [0]) >= DRAFT_PAGE_MEASUREMENT_FLOOR
                   else {"status": "SKIPPED_SMALL_TARGET", "rendered_pages": 0, "provider_called": False})
    if int(draft_pages.get("rendered_pages") or 0) > 0:
        qa_pass["DRAFT_RENDERED_PAGES"] = int(draft_pages["rendered_pages"])
    if int(draft_pages.get("technical_effective_pages") or 0) > 0:
        # §13：页面缺口按技术有效页算，物理页另存一个键，两种口径在报告里都看得见。
        qa_pass["DRAFT_TECHNICAL_EFFECTIVE_PAGES"] = int(draft_pages["technical_effective_pages"])
    # §21 门控先跑：它可能直接禁止补深，所以必须排在补深候选生成之前。
    overshoot = evaluate_page_overshoot(run_dir, draft_pages, page_targets=page_targets,
                                        contracts=factory.cu_contracts)
    # §16–§20：超页不只是一条门控字面量，这里就把削减执行掉（本地、零 Provider）。
    reduction_execution = execute_overshoot_reduction(run_dir, overshoot, factory)
    plan = build_repair_plan(
        qa=qa_pass,
        word=word,
        contracts=contracts.get("contracts") or [],
        requirement_matrix=bundle.get("requirement_matrix") or {},
        run_dir=run_dir,
    )
    if overshoot.get("depth_supplement_forbidden") and plan.get("supplement"):
        plan["supplement_blocked"] = {
            "reason": str(overshoot.get("status") or ""),
            "candidates_dropped": len(plan["supplement"]),
            "candidate_section_ids": [str(row.get("section_id") or "") for row in plan["supplement"]],
        }
        plan["supplement"] = []
    if job is not None:
        with factory.checkpoint_lock:
            job.public_phase = "DOCUMENT_QA"
        factory._checkpoint(job)
    started = time.monotonic()
    repair_report = factory.selective_repair(plan["repair"], job=job) if plan["repair"] else {"status": "NOT_REQUIRED", "attempted": 0}
    supplement_report = factory.supplement_depth(plan["supplement"], job=job) if plan["supplement"] else {"status": "NOT_REQUIRED", "attempted": 0}
    attempted = int(repair_report.get("attempted") or 0) + int(supplement_report.get("attempted") or 0)
    repaired = int(repair_report.get("repaired") or 0) + int(supplement_report.get("supplemented") or 0)
    report = {
        "schema_version": "document-repair-stage-v0.1",
        "status": (
            "NOT_REQUIRED" if not attempted and not (plan["repair"] or plan["supplement"])
            else ("COMPLETED" if attempted and repaired else "COMPLETED_WITH_OPEN_ITEMS")
        ),
        "attempted": attempted,
        "repaired": repaired,
        "seconds": round(time.monotonic() - started, 3),
        "findings": plan["findings"],
        "findings_summary": plan["findings_summary"],
        "counts": plan["counts"],
        "candidates": {"repair": plan["repair"], "supplement": plan["supplement"]},
        "supplement_blocked": plan.get("supplement_blocked") or {},
        "page_overshoot_gate": {k: v for k, v in overshoot.items() if k != "reduction_plan"},
        "reduction_execution": {k: v for k, v in reduction_execution.items() if k != "actions"},
        "reduction_execution_path": (str(run_dir / EXECUTION_FILE)
                                     if reduction_execution.get("status") not in (None, "NOT_REQUIRED")
                                     else None),
        "content_capability_gaps": plan.get("content_capability_gaps"),
        "first_draft_pagination": draft_pages,
        "selective_repair": repair_report,
        "depth_supplement": supplement_report,
        "budget": repair_report.get("budget") or supplement_report.get("budget"),
        "rounds": 1,
        "full_document_regeneration": False,
        "draft_pagination_seconds": float(draft_pages.get("seconds") or 0.0),
        "updated_at": now_iso(),
    }
    # 修复/补充写回的正文必须过与初稿同一条本地治理链（零 Provider 调用）。
    # 削减执行器改的也是盘上的节正文，所以它同样要进这条链——`attempted` 仍然只数
    # 花过 Provider 的修复次数，本地动作单列，两种口径不混。
    if attempted or reduction_execution.get("actions_executed"):
        started = time.monotonic()
        normalization_after_repair = normalize_generated_document(run_dir, None)
        report["post_repair_normalization"] = {
            "status": normalization_after_repair.get("status"),
            "provider_called": False,
            "sections_changed": normalization_after_repair.get("sections_changed"),
            "seconds": round(time.monotonic() - started, 3),
        }
        record_stage_duration(run_dir, "post_repair_normalization", float(normalization_after_repair.get("normalization_seconds") or 0.0))
    write_json(run_dir / "document_qa_report.json", report)
    if job is not None:
        with factory.checkpoint_lock:
            job.public_phase = "ASSEMBLING"
        factory._checkpoint(job)
    return report


def normalize_generated_document(run_dir: Path, section_ids: list[str] | None) -> dict[str, Any]:
    """LOCAL_NORMALIZATION 阶段：把磁盘上的片段就地归一化。

    该阶段禁止 Provider 调用，且任何失败都不得阻断装配——归一化只是让初稿更干净，
    不是交付前提。
    """
    from governance.final_editorial import SourceFacts
    started = time.monotonic()
    try:
        report = run_local_normalization(
            run_dir,
            section_ids=sorted({str(sid) for sid in (section_ids or []) if sid}) or None,
            text_pipeline=lambda value: _local_text_pipeline(value, SourceFacts.from_run(run_dir)),
        )
    except Exception as exc:
        report = {
            "schema_version": "local-normalization-v0.1",
            "status": "LOCAL_NORMALIZATION_FAILED",
            "provider_called": False,
            "error": str(exc)[:200],
            "sections_scanned": 0,
            "sections_changed": 0,
            "fix_counts": {},
        }
    report["normalization_seconds"] = round(time.monotonic() - started, 3)
    return report


def persist_first_draft_artifact(
    run_dir: Path,
    brief: dict[str, Any],
    artifact: dict[str, Any],
    word: dict[str, Any] | None,
    normalization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """FIRST_DRAFT_ARTIFACT：初稿一旦装配完成就落盘。

    后续 QA、修复或渲染失败都不得让这份内容消失，因此这里只写文件、不判定质量。
    """
    summary = word or {}
    payload = {
        "schema_version": "first-draft-artifact-v0.1",
        "artifact_kind": "FIRST_DRAFT",
        "created_at": now_iso(),
        "medium": brief.get("medium"),
        "brief": brief,
        "artifact": artifact,
        "effective_chars": len(visible_text(artifact)),
        "section_states": summary.get("section_states") or {},
        "provisional_sections": summary.get("PROVISIONAL_SECTIONS") or [],
        "depth_gap_sections": summary.get("DEPTH_GAP_SECTIONS") or [],
        "local_normalization": {
            "status": (normalization or {}).get("status"),
            "sections_scanned": (normalization or {}).get("sections_scanned"),
            "sections_changed": (normalization or {}).get("sections_changed"),
            "fix_counts": (normalization or {}).get("fix_counts") or {},
            "provider_called": False,
        },
    }
    target = run_dir / "longform" / "word" / "first_draft_artifact.json"
    write_json(target, payload)
    write_json(run_dir / "longform" / "word" / "first_draft_content.json", {"brief": brief, "artifact": artifact})
    return {
        "status": "FIRST_DRAFT_PERSISTED",
        "path": str(target),
        "effective_chars": payload["effective_chars"],
        "sections": len(artifact.get("sections") or []),
    }


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
    source_projection_recovery = {"sections_changed": [], "restored_fields": 0}
    if str(brief.get("medium") or "WORD").upper() != "PPT":
        from .source_projection import restore_cached_staffing
        from governance.final_editorial import SourceFacts
        source_facts = SourceFacts.from_run(run_dir)
        source_projection_recovery = restore_cached_staffing(
            run_dir, chosen if chosen is not None else all_ids,
            lambda value: _local_text_pipeline(value, source_facts))
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
            "requirement_classification": bundle.get("requirement_classification") or {},
            "commercial_compliance_matrix": bundle.get("commercial_compliance_matrix") or {},
            "technical_scope_gate": bundle.get("technical_scope_gate") or {},
            "bidder_evidence_ledger": bundle.get("bidder_evidence_ledger") or {},
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
    # P0-1（Codex Blocker）：两条媒介分支的文档级QA字段都必须有确定值，
    # 不允许 UnboundLocalError，也不允许把未执行检查静默记成 SKIPPED。
    normalization_report: dict[str, Any] = {
        "schema_version": "local-normalization-v0.1",
        "status": "NOT_APPLICABLE",
        "provider_called": False,
        "sections_scanned": 0,
        "sections_changed": 0,
        "fix_counts": {},
        "normalization_seconds": 0.0,
    }
    first_draft: dict[str, Any] = {"status": "NOT_APPLICABLE", "path": None, "effective_chars": 0, "sections": 0}
    if medium == "PPT":
        storyboard = bundle["ppt_plan"].get("slide_storyboard") or []
        slide_ids = None if mode == "full_longform" else [row["slide_id"] for row in storyboard[:8]]
        ppt = factory.generate_ppt(slide_ids)
        slides = []
        storyboard_rows = {row.get("slide_id"): row for row in (bundle["ppt_plan"].get("slide_storyboard") or [])}
        for index, path in enumerate(sorted((run_dir / "longform" / "ppt" / "slides").glob("*/payload.json"))):
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            data = build_ppt_slide_data(payload, storyboard_rows.get(path.parent.name), index)
            slides.append({
                "slide_id": path.parent.name,
                "title": _strip_scoring(str(payload.get("headline") or "")) or path.parent.name,
                "core_message": _strip_scoring(str(payload.get("key_message") or "")),
                **data,
            })
        chapters_meta = [
            {
                "chapter_id": row.get("chapter_id"),
                "title": row.get("chapter_title") or row.get("chapter_id"),
                "goal": row.get("narrative_goal") or "",
            }
            for row in (bundle["ppt_plan"].get("chapter_blocks") or [])
        ]
        artifact = {
            "title": str(brief.get("project_name") or "本项目"),
            "subtitle": "物业服务方案汇报",
            "chapters": chapters_meta,
            "slides": slides,
        }
        if bundle["ppt_plan"].get("profile_p_contract"):
            from project_assets import attach_project_images
            artifact["profile_p_contract"] = bundle["ppt_plan"]["profile_p_contract"]
            artifact["output_contract"] = bundle["ppt_plan"].get("output_contract", {})
            attach_project_images(run_dir, artifact)
        fragments = {path.parent.name: json.loads(path.read_text(encoding="utf-8-sig")) for path in (run_dir / "longform" / "ppt" / "slides").glob("*/payload.json")}
        gates = []
    else:
        try:
            # P0-5（PROD-MIG-01）：正式 Resume 入口迁移 —— 即使全部 Volume 完成
            # 短路跳过 generate_word，checkpoint 迁移也必须发生。
            checkpoint_state = None
            try:
                from .hierarchical import load_checkpoint_state as _migrate_entry
                checkpoint_state = _migrate_entry(run_dir)
            except Exception:
                pass
            volume_plan = load_or_build_volume_plan(run_dir, bundle, chosen)
            factory.inputs["volume_plan"] = volume_plan
            # Blocker 4：CU 契约注册表（来自 08_document_hierarchy.content_units）
            hierarchy = json.loads((run_dir / "08_document_hierarchy.json").read_text(encoding="utf-8-sig")) if (run_dir / "08_document_hierarchy.json").is_file() else {}
            cu_contracts = {}
            section_contract_index = {
                str(row.get("section_id") or ""): row
                for row in (contracts.get("contracts") or [])
                if row.get("section_id")
            }
            dependency_rows = list((bundle.get("dependency_map") or {}).get("dependencies") or [])
            for cu in hierarchy.get("content_units") or []:
                cu_id = str(cu.get("content_unit_id") or "")
                if cu_id:
                    parent_section_id = str(cu.get("section_id") or "")
                    parent_contract = section_contract_index.get(parent_section_id) or {}
                    dependency_refs = [
                        f"{row.get('source_section')}->{dependent}:{row.get('dependency_type') or 'dependency'}"
                        for row in dependency_rows
                        for dependent in (row.get("dependent_sections") or [])
                        if parent_section_id in {str(row.get("source_section") or ""), str(dependent or "")}
                    ]
                    cu_contracts[cu_id] = {
                        "content_unit_id": cu_id,
                        "parent_section_id": parent_section_id,
                        "title": cu.get("title"),
                        "intent": cu.get("purpose"),
                        "must_cover": [cu.get("purpose")] if cu.get("purpose") else [],
                        "required_outputs": list(parent_contract.get("required_outputs") or []),
                        "archetype": cu.get("archetype") or "NARRATIVE",
                        "must_cover": list(cu.get("must_cover") or ([cu.get("purpose")] if cu.get("purpose") else [])),
                        "required_outputs": list(cu.get("required_outputs") or parent_contract.get("required_outputs") or []),
                        "source_requirements": list(cu.get("source_requirements") or cu.get("requirement_ids") or []),
                        "source_scoring_items": list(cu.get("source_scoring_items") or cu.get("scoring_item_ids") or []),
                        "fact_dependencies": list(cu.get("fact_dependencies") or []),
                        "evidence_dependencies": list(cu.get("evidence_dependencies") or []) + dependency_refs,
                        "min_budget": cu.get("min_budget") or {"measure": "effective_chars", "effective_chars": max(1, int(cu.get("planned_words") or 900) * 8 // 10)},
                        "target_budget": cu.get("target_budget") or {"measure": "effective_chars", "effective_chars": cu.get("planned_words")},
                        "max_budget": cu.get("max_budget") or {"measure": "effective_chars", "effective_chars": max(1, int(cu.get("planned_words") or 900) * 12 // 10)},
                        "scoring_refs": cu.get("scoring_item_ids") or [],
                        "dependency_refs": dependency_refs,
                    }
            factory.inputs["cu_contracts"] = cu_contracts
            # §13：生成前预算门。统一预算树（五层守恒 + 页数投影）随契约一起落盘，
            # 并且在这里成为进入正式生成的**前置条件**：一次 Provider 调用都不许花在
            # 一个自己都超上限或对不上账的计划上。
            factory.inputs["budget_tree"] = hierarchy.get("budget_tree") or {}
            budget_gate = hierarchy.get("pre_generation_budget_gate") or {}
            if budget_gate:
                atomic_write_json(run_dir / BUDGET_GATE_FILE, budget_gate)
                under_floor = str(budget_gate.get("status") or "") == "RAISE_PLAN_UNDER_FLOOR"
                if not budget_gate.get("may_enter_generation") and not under_floor:
                    raise PlanningError(
                        "PRE_GENERATION_BUDGET_GATE 未通过：计划投影 {projected} 页（最坏 {worst} 页），"
                        "目标页带 {floor}～{ceiling} 页，原因 {reasons}。计划总额必须先重新分配，"
                        "本轮未发起任何 Provider 调用。".format(
                            projected=budget_gate.get("projected_technical_effective_pages"),
                            worst=budget_gate.get("worst_case_technical_effective_pages"),
                            floor=budget_gate.get("page_floor"),
                            ceiling=budget_gate.get("page_ceiling"),
                            reasons="、".join(budget_gate.get("blocked_reasons") or []) or "UNKNOWN",
                        )
                    )
            # §34：生成前评分映射门。规划阶段就没落点、没绑定 CU、没实体证据契约的
            # 唯一实质评分主题，不许带着"生成时再想办法"进正式生成 —— Run D 的
            # 34.3% 覆盖率正是这条缺口的事后形态。
            # Final Gate §5.4：这道门只有**一个**消费点，而且没有"读不到就当没有门"这一支 ——
            # 旧代码在这里写的是 `if scoring_mapping and not scoring_mapping.get(...)`，
            # 于是 missing / unreadable / 矩阵里没嵌门三种"读不到"全都等于跳过这道门。
            require_pre_generation_scoring_mapping_gate(run_dir)
            # factory.__init__ 在构造时读取 inputs；此处构造后同步 live 属性。
            factory.cu_contracts = cu_contracts
            factory.cus_by_section = {}
            for cu_id, contract in cu_contracts.items():
                factory.cus_by_section.setdefault(str(contract.get("parent_section_id") or ""), []).append(cu_id)
            # 会话计数种子化：即使 Volume 全部完成短路跳过 generate_word，
            # provider_calls_total / token 累计也必须从 checkpoint 恢复（审计连续）。
            if checkpoint_state:
                try:
                    factory.provider_calls_total = int(checkpoint_state.get("provider_calls_total") or 0)
                    factory.provider_call_count = factory.provider_calls_total
                    cumulative = checkpoint_state.get("token_accounting_cumulative") or checkpoint_state.get("token_accounting")
                    if cumulative:
                        # 只恢复累计口径（审计连续）；会话口径 token_accountant 保持新鲜，
                        # 避免 this_run 被历史累计污染。
                        factory.token_accountant_cumulative.restore(cumulative)
                except Exception:
                    pass
            chosen_set = set(chosen) if chosen is not None else {
                row.get("section_id") for row in (bundle["section_contracts"].get("contracts") or [])
            }
            # P0-5（Codex Blocker）：Plan Invalidation 正式接线 —— Resume 入口
            # 加载旧 lineage → 与当前 pack/bundle 指纹对比 → 失效清除 → 审计落盘。
            try:
                lineage_path = run_dir / "10_hierarchy_lineage.json"
                current_lineage = compute_lineage(load_requirement_pack(run_dir), bundle)
                if lineage_path.is_file():
                    previous_lineage = json.loads(lineage_path.read_text(encoding="utf-8-sig"))
                    invalidation_audit = apply_plan_invalidation(run_dir, previous_lineage, current_lineage)
                else:
                    invalidation_audit = {"schema_version": "plan-invalidation-audit-v0.1", "note": "first run, no previous lineage"}
                atomic_write_json(lineage_path, current_lineage)
            except Exception as exc:
                invalidation_audit = {"status": "CONSISTENCY_CHECK_FAILED", "error": str(exc)[:160]}
            volume_progress_path = run_dir / "volume_progress.json"
            progress = {"schema_version": "volume-progress-v0.1", "volumes": [], "last_valid_volume_artifact": None}
            if volume_progress_path.is_file():
                try:
                    progress = json.loads(volume_progress_path.read_text(encoding="utf-8-sig"))
                except Exception:
                    pass
            # 契约要落在进度文件上：运营者看到 `volumes/` 少几份时，第一眼就该读到
            # "分卷 DOCX 是 Resume 锚点、不是交付件"，而不是自己去猜哪一份该存在。
            progress["volume_artifact_contract"] = volume_artifact_contract()
            for volume_index, volume in enumerate(volume_plan.get("volumes") or []):
                ids_v = [sid for sid in (volume.get("section_ids") or []) if sid in chosen_set]
                if not ids_v:
                    continue
                def _section_status(sid: str) -> str:
                    status_path = run_dir / "longform" / "word" / "sections" / sid / "status.json"
                    if not status_path.is_file():
                        return "MISSING"
                    try:
                        return str(json.loads(status_path.read_text(encoding="utf-8-sig")).get("status") or "")
                    except Exception:
                        return "UNREADABLE"

                # §44–§49：这里过去自写一份 `ok_statuses` 三元组，比状态端
                # （run_state.DRAFTABLE_SECTIONS）更严 —— 同一个"完成"两处两把尺子，
                # Run D 的 5 份分卷 DOCX 就是这么没渲染出来的。判定统一走 .cu_state。
                def _content_present(ids: list[str]) -> tuple[list[str], list[str]]:
                    present = [sid for sid in ids if _section_status(sid) in SECTION_DRAFTABLE_STATES]
                    return present, [sid for sid in ids if sid not in set(present)]

                present_v, gaps_v = _content_present(ids_v)
                provisional_v = [sid for sid in present_v if _section_status(sid) not in SECTION_PASS_STATES]
                if (
                    str(volume.get("generation_status")) == "COMPLETED"
                    and not gaps_v
                    and not set(ids_v).intersection(source_projection_recovery["sections_changed"])
                    and (run_dir / "volumes" / f"{volume.get('volume_id')}.docx").is_file()
                ):
                    continue
                factory.generate_word(ids_v, volume_continuation=volume_index > 0)
                factory.repair_table_projections(ids_v)
                fragments_all = collect_fragments(run_dir)
                fragments_v = {sid: fr for sid, fr in fragments_all.items() if sid in set(ids_v)}
                present_v, gaps_v = _content_present(ids_v)
                provisional_v = [sid for sid in present_v if _section_status(sid) not in SECTION_PASS_STATES]
                if gaps_v:
                    # 分卷只有在全部Section真实到场时才能装配；缺正文才是 gaps。
                    # PROVISIONAL（正文已到、深度待补）不是缺内容，按 B 语义照常渲染，
                    # 并把"哪几节待补"原样记进进度，不伪装成完整也不静默丢弃。
                    volume["generation_status"] = "COMPLETED_WITH_GAPS"
                    volume["artifact_path"] = None
                    entry = {
                        "volume_id": volume.get("volume_id"),
                        "title": volume.get("title"),
                        "section_ids": ids_v,
                        "generation_status": volume.get("generation_status"),
                        "artifact_path": None,
                        "missing_or_failed_sections": gaps_v,
                        "provisional_sections": provisional_v,
                        "completed_at": now_iso(),
                    }
                    progress["volumes"] = [row for row in progress.get("volumes") or [] if row.get("volume_id") != volume.get("volume_id")]
                    progress["volumes"].append(entry)
                    atomic_write_json(volume_progress_path, progress)
                    continue
                if not fragments_v:
                    # 状态在场却收不到片段：必须留下记录，不能静默跳过（静默跳过会让
                    # 这一卷在 progress 里凭空消失，读起来像"没有这一卷"）。
                    entry = {
                        "volume_id": volume.get("volume_id"),
                        "title": volume.get("title"),
                        "section_ids": ids_v,
                        "generation_status": "RENDER_SKIPPED_NO_FRAGMENTS",
                        "artifact_path": None,
                        "missing_or_failed_sections": [
                            sid for sid in ids_v
                            if not (run_dir / "longform" / "word" / "sections" / sid / "fragment.json").is_file()],
                        "provisional_sections": provisional_v,
                        "completed_at": now_iso(),
                    }
                    progress["volumes"] = [row for row in progress.get("volumes") or [] if row.get("volume_id") != volume.get("volume_id")]
                    progress["volumes"].append(entry)
                    atomic_write_json(volume_progress_path, progress)
                    continue
                vol_word_plan = volume_word_plan(bundle["word_plan"], volume)
                vol_artifact = assemble_word_artifact(brief, vol_word_plan, fragments_v, include_toc=False, allow_unresolved_tables=True)
                vol_dir = run_dir / "volumes"
                vol_dir.mkdir(parents=True, exist_ok=True)
                content_path_v = vol_dir / f"{volume.get('volume_id')}_content.json"
                write_json(content_path_v, {"brief": brief, "artifact": vol_artifact})
                vol_docx = vol_dir / f"{volume.get('volume_id')}.docx"
                if vol_docx.is_file() and set(ids_v).intersection(source_projection_recovery["sections_changed"]):
                    import hashlib
                    previous_hash = hashlib.sha256(vol_docx.read_bytes()).hexdigest()
                    previous = vol_dir / "source_projection_history" / (vol_docx.stem + "_" + previous_hash[:16] + ".docx")
                    previous.parent.mkdir(parents=True, exist_ok=True)
                    if not previous.exists():
                        shutil.copyfile(vol_docx, previous)
                render = subprocess.run(
                    [sys.executable, str(APP_ROOT / "build_docx.py"), str(content_path_v), str(vol_docx)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                )
                volume_findings = vol_artifact.get("source_table_findings") or []
                volume["generation_status"] = ("ASSEMBLED_WITH_FINDINGS" if volume_findings else "COMPLETED") if render.returncode == 0 else "ASSEMBLY_FAILED"
                volume["artifact_path"] = str(vol_docx) if render.returncode == 0 else None
                entry = {
                    "volume_id": volume.get("volume_id"),
                    "title": volume.get("title"),
                    "section_ids": ids_v,
                    "generation_status": volume.get("generation_status"),
                    "artifact_path": volume.get("artifact_path"),
                    "artifact_role": VOLUME_ARTIFACT_CONTRACT,
                    "content_state": "COMPLETE" if not provisional_v else "COMPLETE_WITH_PROVISIONAL",
                    "missing_or_failed_sections": [],
                    "provisional_sections": provisional_v,
                    "source_table_findings": volume_findings,
                    "completed_at": now_iso(),
                }
                progress["volumes"] = [row for row in progress.get("volumes") or [] if row.get("volume_id") != volume.get("volume_id")]
                progress["volumes"].append(entry)
                if render.returncode == 0 and vol_docx.is_file() and vol_docx.stat().st_size > 0 and not volume_findings:
                    progress["last_valid_volume_artifact"] = str(vol_docx)
                atomic_write_json(volume_progress_path, progress)
            try:
                from .factory import LongformGenerationJob
                word_job_path = run_dir / "longform" / "word" / "status.json"
                if word_job_path.is_file():
                    word_job = LongformGenerationJob.load(word_job_path)
                    word_job.total_units = len(chosen_set)
                    word_job.dump(word_job_path)
                    word = factory.word_summary(word_job)
                else:
                    word = factory.generate_word(sorted(chosen_set))
            except Exception:
                word = factory.generate_word(sorted(chosen_set))
        except Exception as exc:
            factory.performance["phase"] = "generation"
            factory.performance["generation_seconds"] = round(time.monotonic() - generation_started, 3)
            factory.performance["provider_wait_seconds"] = round(float(factory.provider_wait_seconds or 0), 3)
            factory.performance["token_accounting"] = factory.token_accountant.summary()
            # 技术性中断也必须留下已经到达的内容：先尽力把半程片段装配成初稿，再抛错。
            try:
                persist_first_draft_artifact(
                    run_dir, brief,
                    assemble_word_artifact(brief, bundle["word_plan"], collect_fragments(run_dir), allow_unresolved_tables=True),
                    None,
                )
            except Exception:
                pass
            factory.performance["generation_error"] = str(exc)[:200]
            raise
        normalization_report = normalize_generated_document(run_dir, chosen)
        if normalization_report.get("status") not in {"NO_SECTIONS"}:
            record_stage_duration(run_dir, "local_normalization", float(normalization_report.get("normalization_seconds") or 0.0))
        fragments = collect_fragments(run_dir)
        gates = collect_gates(run_dir)
        artifact = assemble_word_artifact(brief, bundle["word_plan"], fragments, allow_unresolved_tables=True)
        # FIRST_DRAFT 先于任何文档级 QA 判定落盘：PROVISIONAL / DEPTH_INCOMPLETE /
        # NEEDS_REVIEW 都是"可交付初稿"，不是"没有文件"。
        first_draft = persist_first_draft_artifact(run_dir, brief, artifact, word, normalization_report)
    generation_seconds = round(time.monotonic() - generation_started, 3)
    # This layer emits generation and QA facts only.  Terminal delivery is
    # derived once, after artifact assembly, by run_layout.finalize_run.
    qa_started = time.monotonic()
    qa_pass = run_document_level_qa(
        run_dir,
        brief=brief,
        bundle=bundle,
        contracts=contracts,
        mode=mode,
        medium=medium,
        chosen=chosen,
        fragments=fragments,
        gates=gates,
        artifact=artifact,
    )
    # DOCUMENT_QA → 有界 SELECTIVE_REPAIR / DEPTH_SUPPLEMENT（§25-34）。
    # 旧实现在这里整篇重跑（Round 2/3/4），300 页任务因此三小时不收敛；现在只在
    # 全局修复预算内补缺口，预算耗尽即停，剩余问题如实记 NEEDS_REVIEW。
    repair_stage: dict[str, Any] = {"schema_version": "document-repair-stage-v0.1", "status": "NOT_APPLICABLE", "attempted": 0}
    if medium != "PPT":
        repair_stage = run_bounded_document_repair(
            run_dir,
            factory,
            bundle=bundle,
            contracts=contracts,
            word=word,
            qa_pass=qa_pass,
        )
        if repair_stage.get("attempted"):
            fragments = collect_fragments(run_dir)
            gates = collect_gates(run_dir)
            artifact = assemble_word_artifact(brief, bundle["word_plan"], fragments, allow_unresolved_tables=True)
            qa_pass = run_document_level_qa(
                run_dir,
                brief=brief,
                bundle=bundle,
                contracts=contracts,
                mode=mode,
                medium=medium,
                chosen=chosen,
                fragments=fragments,
                gates=gates,
                artifact=artifact,
            )
            word = factory.word_summary(_load_word_job(run_dir)) or word
    artifact = qa_pass["artifact"]
    template_repairs = qa_pass["template_repairs"]
    total_chars = qa_pass["total_chars"]
    depth = qa_pass["depth"]
    bidder_evidence_gate = qa_pass["bidder_evidence_gate"]
    budget_quality = qa_pass["budget_quality"]
    padding_quality = qa_pass["padding_quality"]
    coverage_gate = qa_pass["coverage_gate"]
    scoring_fulfillment = qa_pass["scoring_fulfillment"]
    consistency_result = qa_pass["consistency"]
    document_scale = qa_pass["document_scale"]
    rendered_pages = qa_pass["rendered_pages"]
    hierarchy_metrics = qa_pass["hierarchy_metrics"]
    checkpoint_metrics = qa_pass["checkpoint_metrics"]
    generated = {
        "artifact": artifact,
        "scoring_fulfillment": scoring_fulfillment if medium != "PPT" else None,
        "consistency": consistency_result,
        "bidder_evidence_gate": bidder_evidence_gate,
        "content_budget_quality_gate": budget_quality,
        "content_padding_quality_gate": padding_quality,
        "citation_registry": [{"claim": "方案结构由MMF Document Plan确定", "source_type": "current_project_fact", "source_id": "canonical_project_brief"}],
        "guardrail_non_use": [],
        "clarification_list": list(bundle["global_state"].get("open_clarifications") or [])[:12],
        "provider_metadata": getattr(provider, "get_metadata", lambda: {})(),
        "longform": {
            "LONGFORM_ORCHESTRATOR": "ACTIVE",
            "ONE_SHOT_FULL_DOCUMENT_GENERATION": False,
            "SECTION_LEVEL_GENERATION": True,
            "GENERATION_BATCH_LAYER": True,
            "LOCAL_NORMALIZATION": {
                "status": normalization_report.get("status"),
                "provider_called": False,
                "sections_scanned": normalization_report.get("sections_scanned"),
                "sections_changed": normalization_report.get("sections_changed"),
                "fix_counts": normalization_report.get("fix_counts") or {},
            },
            "FIRST_DRAFT_ARTIFACT": first_draft,
            "DOCUMENT_SCALE": document_scale,
            "DOCUMENT_QA": {
                "status": repair_stage.get("status"),
                "attempted": repair_stage.get("attempted"),
                "repaired": repair_stage.get("repaired"),
                "findings_summary": repair_stage.get("findings_summary"),
                "counts": repair_stage.get("counts"),
                "budget": repair_stage.get("budget"),
                "rounds": 1,
                "full_document_regeneration": False,
            },
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
    write_json(run_dir / "bidder_evidence_gate.json", bidder_evidence_gate)
    write_json(run_dir / "content_budget_quality_gate.json", budget_quality)
    write_json(run_dir / "content_padding_quality_gate.json", padding_quality)
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
        "LOCAL_NORMALIZATION": normalization_report.get("status"),
        "FIRST_DRAFT_ARTIFACT": first_draft.get("status"),
        "CANONICAL_PLAN_PROVIDER_INDEPENDENT": "PASS",
        "PROVIDER_EFFECTIVE_SETTINGS_AUDIT": "PASS" if factory.capability else "FAIL",
        "LONGFORM_DEPTH_GATE": depth["status"],
        "BIDDER_EVIDENCE_GATE": bidder_evidence_gate["status"],
        "CONTENT_BUDGET_QUALITY_GATE": budget_quality["status"],
        # §45：低信息增量的占比与门状态一起进状态文件，交付阶段不用再解释一遍口径。
        "CONTENT_PADDING_GATE": padding_quality["status"],
        "GENERIC_PADDING_ITEM_SHARE": (padding_quality.get("item_share") or {}).get("GENERIC_PADDING"),
        "GENERIC_PADDING_CHAR_SHARE": (padding_quality.get("char_share") or {}).get("GENERIC_PADDING"),
        "QUALITY_REGRESSION_GATE": coverage_gate.get("status"),
        "DOCUMENT_SCALE": document_scale.get("status"),
        "RENDERED_PAGES_ESTIMATE": rendered_pages,
        "DOCUMENT_QA_REPAIR": repair_stage.get("status"),
        "DOCUMENT_QA_ATTEMPTED": repair_stage.get("attempted"),
        "DOCUMENT_QA_REPAIRED": repair_stage.get("repaired"),
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
        "normalization_seconds": float(normalization_report.get("normalization_seconds") or 0.0),
        "qa_seconds": qa_seconds,
        # 成本与耗时审计（§44-46）：初稿 / 技术重试 / 选择性修复 / 深度补充分开计量。
        "document_qa_seconds": round(float(repair_stage.get("seconds") or 0.0), 3),
        "selective_repair_seconds": round(float(factory.selective_repair_wall_seconds or 0.0), 3),
        "depth_supplement_seconds": round(float(factory.supplement_wall_seconds or 0.0), 3),
        "wall_elapsed_seconds": wall_elapsed,
        "repair_reasoning": performance.get("repair_reasoning") or "not_invoked",
        "token_accounting": (lambda perf_ta, session_ta, cumulative_ta: (
            perf_ta if "this_run" in (perf_ta or {}) else {**session_ta, "this_run": session_ta, "cumulative": cumulative_ta}
        ))(performance.get("token_accounting"), factory.token_accountant.summary(), factory.token_accountant_cumulative.summary()),
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
    content_metrics = {
        "document_scale": (bundle.get("content_budget") or {}).get("document_scale_code"),
        "target_effective_chars": ((bundle.get("content_budget") or {}).get("totals") or {}).get("target_effective_chars_target"),
        "actual_effective_chars": total_chars,
        "planned_content_units": hierarchy_metrics.get("content_units", 0),
        "completed_content_units": len(checkpoint_metrics.get("completed_content_units") or []),
        "content_units_by_archetype": hierarchy_metrics.get("content_units_by_archetype") or {},
        "word_tables": sum(len(section.get("tables") or []) for section in artifact.get("sections") or []) if isinstance(artifact, dict) else 0,
        "sop_count": (hierarchy_metrics.get("content_units_by_archetype") or {}).get("SOP", 0),
        "form_count": (hierarchy_metrics.get("content_units_by_archetype") or {}).get("FORM_TEMPLATE", 0),
        "actual_office_pages": rendered_pages,
    }
    return {
        "generated": generated,
        "bundle": bundle,
        "word": word,
        "depth": depth,
        "first_draft": first_draft,
        "local_normalization": generated["longform"]["LOCAL_NORMALIZATION"],
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
        "content_metrics": content_metrics,
        "document_scale": document_scale,
        "document_qa": repair_stage,
        "rendered_pages": rendered_pages,
    }


def _scoring_class_by_requirement(matrix_path: Path) -> tuple[dict[str, str], dict[str, Any]]:
    """评分行的十类判定 → 需求 ID 的反查表，供 §38 把"表头/公式行"请出 MUST 分母。

    历史 Run 的 `09_scoring_matrix.json` 里没有 `scoring_class` 字段（#49 之前建的档），
    所以缺字段时必须用 §24 的同一把尺子现算并**记明来源**，不能静默返回空表——
    空表等于让 QR-001 重新吃回 Round 3 的表头行分母。
    """
    stats: dict[str, Any] = {"source_file": matrix_path.name, "persisted": 0,
                             "derived_from_taxonomy": 0, "rows_total": 0, "unclassified_rows": 0}
    if not matrix_path.is_file():
        stats["status"] = "SCORING_MATRIX_ABSENT"
        return {}, stats
    try:
        matrix = json.loads(matrix_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        stats["status"] = "SCORING_MATRIX_UNREADABLE"
        return {}, stats
    mapping: dict[str, str] = {}
    for row in matrix.get("rows") or []:
        if not isinstance(row, dict):
            continue
        stats["rows_total"] += 1
        scoring_class = str(row.get("scoring_class") or "")
        origin = "PERSISTED_ON_SCORING_ROW"
        if not scoring_class:
            scoring_class = classify_scoring_row(row, scope_class=row.get("scope_class"))
            origin = "DERIVED_BY_SCORING_TAXONOMY"
        if not scoring_class:
            stats["unclassified_rows"] += 1
            continue
        stats["persisted" if origin == "PERSISTED_ON_SCORING_ROW" else "derived_from_taxonomy"] += 1
        for req_id in row.get("requirement_ids") or []:
            mapping.setdefault(str(req_id), scoring_class)
    stats["status"] = "OK"
    stats["requirement_ids_mapped"] = len(mapping)
    return mapping, stats


def _qr001_authority_block(authority: dict[str, Any], denominator_rows: list[dict[str, Any]],
                           equivalences: list[dict[str, Any]],
                           scoring_class_map: dict[str, Any] | None = None) -> dict[str, Any]:
    """QR-001 每次输出都必须自带它的分母来历，否则报告里又是一个没有尺子的数字。"""
    return {
        "denominator": authority["denominator"],
        "denominator_authority": authority["denominator_authority"],
        "final_scope_gate_present": authority["final_scope_gate_present"],
        "scoring_class_map": scoring_class_map or {"status": "NOT_PASSED"},
        "matrix_rows_total": authority["matrix_rows_total"],
        "scope_gated_row_total": authority["denominator_rows_total"],
        "planned_must_or_scoring_total": len(denominator_rows),
        "removed_from_denominator_total": authority["removed_total"],
        "removal_counts": authority["removal_counts"],
        "removed_from_denominator": authority["excluded"],
        "conservation_holds": authority["conservation_holds"],
        "legacy_planned_must_before_scope_gate": authority["legacy_quoting"]["planned_must_before_authority"],
        "requirement_equivalences": equivalences,
    }


def _scoring_requirement_evidence(matrix: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Use current entity judgments, not grading words as proposal body evidence.

    Call only after scoring has been refreshed against the current carriers.
    A partial topic, empty evidence, or unrelated scoring row cannot grant coverage.
    """
    from planning.scoring_responsiveness import record_requires_technical_acceptance
    rows = {str(r.get('scoring_item_id')): r for r in matrix.get('rows') or []}
    found = {}
    for record in matrix.get('canonical_scoring_records') or []:
        verdict = record.get('fulfillment') or {}
        if not record_requires_technical_acceptance(record) or verdict.get('status') != 'FULFILLED':
            continue
        for sid in record.get('evaluation_row_ids') or record.get('source_ids') or []:
            row = rows.get(str(sid), {})
            actual = row.get('fulfillment') or {}
            if actual.get('status') != 'FULFILLED' or not actual.get('evidence_found'):
                continue
            for rid in row.get('requirement_ids') or []:
                found[str(rid)] = {'basis':'CURRENT_SCORING_ENTITY_EVIDENCE',
                    'canonical_scoring_id':record.get('canonical_scoring_id'),
                    'scoring_item_id':sid,'status':'FULFILLED',
                    'response_cu':actual.get('response_cu') or [],
                    'artifact_location':actual.get('artifact_location') or [],
                    'rubric_id':actual.get('rubric_id')}
    return found


def evaluate_requirement_coverage_regression(
    matrix: dict[str, Any],
    fragments: dict[str, dict[str, Any]],
    *,
    expected_section_ids: list[str] | None = None,
    task_mode: str | None = None,
    scope_gate: dict[str, Any] | None = None,
    scoring_class_by_requirement: dict[str, str] | None = None,
    scoring_class_map: dict[str, Any] | None = None,
    scoring_evidence_by_requirement: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """QR-001：需求覆盖回归门。分母**只**来自 Scope-Gated Requirement Authority（§38）。

    Round 3 之前这里直接吃 `coverage_status == "MAPPED"` 的规划期行集，那是范围门之前
    的集合；现在最终 TECHNICAL_ONLY_SCOPE_GATE 判为排除的条款、评分表表头一类的
    非响应行，一律先离开分母与 missing 名单，再把剩下的行按技术实质取证。
    """
    generated_ids = set(fragments)
    authority = scope_gated_denominator(
        matrix, scope_gate=scope_gate, scoring_class_by_requirement=scoring_class_by_requirement)
    rows = [row for row in authority["rows"] if row.get("mandatory_level") == "MUST" or row.get("scoring_item_id")]
    scoped = []
    for row in rows:
        owners = [row.get("primary_section_id"), *(row.get("secondary_section_ids") or [])]
        owners = [sid for sid in owners if sid]
        if expected_section_ids is not None:
            if not any(sid in set(expected_section_ids) for sid in owners):
                continue
        scoped.append(row)
    equivalences: list[dict[str, Any]] = []
    if not scoped:
        if any(row.get("mandatory_level") == "MUST" for row in rows) and not generated_ids:
            missing = [row.get("requirement_id") for row in rows if row.get("mandatory_level") == "MUST"]
            return {"status": "FAIL", "planned_must": len(missing), "covered_must": 0, "missing": missing,
                    "QUALITY_REGRESSION_GATE": "FAIL",
                    **_qr001_authority_block(authority, rows, equivalences, scoring_class_map)}
        return {"status": "PASS", "planned_must": 0, "covered_must": 0, "missing": [],
                "QUALITY_REGRESSION_GATE": "PASS",
                **_qr001_authority_block(authority, rows, equivalences, scoring_class_map)}
    missing = []
    covered = 0
    for row in scoped:
        owners = [sid for sid in [row.get("primary_section_id"), *(row.get("secondary_section_ids") or [])] if sid]
        present = [sid for sid in owners if sid in generated_ids]
        if not present:
            missing.append(row.get("requirement_id"))
            continue
        scoring_evidence = (scoring_evidence_by_requirement or {}).get(str(row.get('requirement_id'))) or {}
        if (row.get('scoring_item_id') and scoring_evidence.get('status') == 'FULFILLED'
                and scoring_evidence.get('basis') == 'CURRENT_SCORING_ENTITY_EVIDENCE'
                and scoring_evidence.get('scoring_item_id') == row.get('scoring_item_id')):
            covered += 1
            equivalences.append({'requirement_id':row.get('requirement_id'),
                'basis':'SCORING_SUBSTANCE_INSTEAD_OF_GRADING_LANGUAGE','evidence':scoring_evidence})
            continue
        evidence = split_requirement_evidence(row.get("requirement_text") or "")
        req_text = evidence["technical_text"]
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
            continue
        aliases = evidence_alias_hits(req_text, all_text)
        if aliases and all(
            keyword_coverage(term, all_text) != "COVERED" and alias in all_text
            for term, alias in ((hit["term"], hit["matched_alias"]) for hit in aliases)
        ):
            covered += 1
            equivalences.append({
                "requirement_id": row.get("requirement_id"),
                "basis": "DECLARED_NARROW_ALIAS",
                "matched": aliases,
                "registered_fragments": evidence["registered_fragments"],
                "equivalence_basis": evidence["equivalence_basis"],
                "note": "原句未逐字命中，按其登记的同义技术表述计入覆盖；仅用于取证文本，不改变范围判定。",
            })
            continue
        missing.append(row.get("requirement_id"))
    status = "PASS" if not missing else "FAIL"
    return {
        "status": status,
        "planned_must": len(scoped),
        "covered_must": covered,
        "missing": missing,
        "QUALITY_REGRESSION_GATE": status,
        "task_mode": task_mode,
        **_qr001_authority_block(authority, rows, equivalences, scoring_class_map),
    }


def _clamped_ratio(numerator: Any, denominator: Any) -> float | None:
    total = int(denominator or 0)
    if total <= 0:
        return None
    return min(1.0, max(0.0, int(numerator or 0) / total))


def scale_page_targets(bundle: dict[str, Any]) -> tuple[int, int]:
    """复合规模的目标页数区间，来自规划期 Content Budget（300 级为 250～320 页）。"""
    totals = (bundle.get("content_budget") or {}).get("totals") or {}
    minimum = int(totals.get("target_pages_min") or 0)
    maximum = int(totals.get("target_pages_max") or 0)
    if minimum > 0 and maximum >= minimum:
        return minimum, maximum
    planning_pages = int(totals.get("planning_pages") or 0)
    if planning_pages > 0:
        return int(round(planning_pages * 0.88)), int(round(planning_pages * 1.13))
    return 0, 0


class PreGenerationScoringGateError(PlanningError):
    """Final Gate §5.4：评分映射门没能确立时，正式生成入口必须先停，不动 Provider。

    它是 `PlanningError` 的子类，所以对既有调用方语义不变（规划阶段就停、零 Provider 调用）。
    """


def read_pre_generation_scoring_mapping_gate(run_dir: Path) -> dict[str, Any]:
    """Round 4.3 §4/§8：生成入口读的就是那份唯一 canonical Authority，这里不再另算一套。

    上一轮这里留着一条"09 不在就读独立门文件"的退路，Codex 的第二次 Final Gate 证明
    那条退路本身就是错的 Authority 关系：派生门文件自报 `PASS` 替代了对 canonical 输入的
    校验。现在这个函数只是消费点（名字保留给既有调用方与测试），判据全部在
    `longform.scoring_authority.canonical_scoring_authority` —— 终态落盘读的也是它。
    """
    return canonical_scoring_authority(run_dir)


def _authority_scoring_gate_document(run_dir: Path, reading: dict[str, Any]) -> dict[str, Any]:
    """Round 4.6 §7：把 Authority 的**终态**读数写成门文件形状的证据，供 raise 之前落盘。

    要解决的现场缺陷很具体：错误消息写"明细见 `pre_generation_scoring_mapping_gate.json`"，
    而那份文件当时只有规划侧写的旧结论（Run E 现场是 `status=PASS / may_enter_generation=true`），
    照消息去读会读到"过了"。这里让**消息、Authority、盘上证据三者同源**。

    派生文件的地位一个字节都没改（Round 4.3）：它是**声称**，永远不授权
    （`derived_file_honored=false`）。所以旧那份声称不被删掉也不被改写，而是原样降级成
    `previous_derived_claim` 留在新证据里 —— 后来人能看见"上一次是谁、按什么结论放过"。
    """
    path = run_dir / SCORING_MAPPING_GATE_FILE
    previous: dict[str, Any] = {"file": SCORING_MAPPING_GATE_FILE, "state": "ABSENT"}
    if path.is_file():
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            value = None
            previous = {"file": SCORING_MAPPING_GATE_FILE, "state": "UNREADABLE",
                        "error_type": type(exc).__name__}
        if isinstance(value, dict):
            previous = {"file": SCORING_MAPPING_GATE_FILE, "state": "PRESENT",
                        "status": value.get("status"),
                        "may_enter_generation": value.get("may_enter_generation"),
                        "substantive_topic_total": value.get("substantive_topic_total"),
                        "evaluated_stage": value.get("evaluated_stage")}
        elif value is not None:
            previous = {"file": SCORING_MAPPING_GATE_FILE, "state": "MALFORMED",
                        "shape": type(value).__name__}
    return {
        "schema_version": SCORING_TAXONOMY_VERSION,
        "evaluated_stage": "PRE_GENERATION_ENTRY_AUTHORITY",
        "written_by": "longform.orchestrator.require_pre_generation_scoring_mapping_gate",
        "status": str(reading.get("status") or "UNKNOWN"),
        "may_enter_generation": bool(reading.get("may_enter_generation")),
        "fail_closed": bool(reading.get("fail_closed", True)),
        "reason": str(reading.get("reason") or ""),
        "blocked_reasons": [str(reading.get("reason") or reading.get("status") or "")],
        "authority_source": reading.get("source") or "UNKNOWN",
        "verdict_basis": reading.get("verdict_basis"),
        "clean_blockers": reading.get("clean_blockers") or [],
        "substantive_topic_total": reading.get("canonical_topic_total"),
        "canonical_topic_basis": reading.get("canonical_topic_basis"),
        "canonical_topic_gaps": reading.get("canonical_topic_gaps"),
        "canonical_matrix_state": reading.get("canonical_matrix_state"),
        "topic_expected_count": reading.get("topic_expected_count"),
        "topic_mapped_count": reading.get("topic_mapped_count"),
        "topic_missing_ids": reading.get("topic_missing_ids"),
        "topic_extra_ids": reading.get("topic_extra_ids"),
        "topic_duplicate_ids": reading.get("topic_duplicate_ids"),
        "topic_universe_layers": reading.get("topic_universe_layers"),
        "topic_universe_verifiable": reading.get("topic_universe_verifiable"),
        "topic_record_binding_counts": reading.get("topic_record_binding_counts"),
        "topic_identity_mismatched_records": reading.get("topic_identity_mismatched_records"),
        "topic_identity_ambiguous_records": reading.get("topic_identity_ambiguous_records"),
        "artifact_identity": reading.get("verified_against") or {},
        "scoring_authority_identity": reading.get("scoring_authority_identity") or "",
        "declared_gate_at_this_moment": reading.get("declared") or {},
        "previous_derived_claim": previous,
        "provider_calls_initiated": 0,
    }


def require_pre_generation_scoring_mapping_gate(run_dir: Path) -> dict[str, Any]:
    """正式生成入口的单一消费点：Authority 没确立就抛错，且发生在任何 Provider 调用之前。"""
    reading = read_pre_generation_scoring_mapping_gate(run_dir)
    if reading["may_enter_generation"]:
        return reading
    detail = "；".join(
        f"{defect.get('canonical_scoring_id')}:{defect.get('topic')}"
        f"[{','.join(defect.get('missing') or [])}]"
        for defect in ((reading.get("unmapped_records") or [])[:5] if isinstance(
            reading.get("unmapped_records"), list) else []))
    invalid = reading.get("invalid_reference") or {}
    invalid_ref = ("；查无此节 " + "、".join(invalid.get("sections") or [])
                   + " 查无此单元 " + "、".join(invalid.get("content_units") or [])
                   if (invalid.get("sections") or invalid.get("content_units")) else "")
    # §7：先把本次终态 BLOCK 原子持久化，再 raise —— 顺序不能反，否则"明细见该文件"
    # 指向的还是一份旧结论。写失败也不放过：那要显式说进消息里，不能默默继续装作有证据。
    evidence_note = ""
    try:
        atomic_write_json(run_dir / SCORING_MAPPING_GATE_FILE,
                          _authority_scoring_gate_document(run_dir, reading))
    except OSError as exc:
        evidence_note = (f"（注意：本轮终态 BLOCK 明细**写盘失败** {type(exc).__name__}，"
                         f"{SCORING_MAPPING_GATE_FILE} 里那份不是本轮结论）")
    raise PreGenerationScoringGateError(
        "PRE_GENERATION_SCORING_MAPPING_GATE 未通过：{}{}。门读数来自 {}，"
        "canonical 技术实质主题 {} 个（分母来源 {}），矩阵状态 {}，"
        "明细见 {}，本轮未发起任何 Provider 调用。".format(
            reading.get("reason") or reading.get("status"),
            f"（{detail}）" if detail else "",
            reading.get("source") or "UNKNOWN",
            reading.get("canonical_topic_total"),
            reading.get("canonical_topic_basis") or "UNKNOWN",
            reading.get("canonical_matrix_state") or "UNKNOWN",
            SCORING_MAPPING_GATE_FILE,
        ) + invalid_ref + evidence_note
    )




def _scoring_fulfillment_ratio(run_dir: Path) -> float | None:
    """技术评分实体覆盖率（§6/§24/§25/§33）。

    分母是去重后的唯一技术实质主题（canonical 记录），分子只计 FULFILLED；
    没有 canonical 记录的老矩阵退回按行口径，并在返回不了口径时保持 None（未测量）。
    """
    path = run_dir / "09_scoring_matrix.json"
    if not path.is_file():
        return None
    try:
        matrix = json.loads(path.read_text(encoding="utf-8-sig")) or {}
    except (OSError, ValueError, TypeError):
        return None
    records = [row for row in (matrix.get("canonical_scoring_records") or []) if isinstance(row, dict)]
    if records:
        from planning.scoring_responsiveness import record_requires_technical_acceptance
        technical_records = [row for row in records if record_requires_technical_acceptance(row)]
        if not technical_records:
            return 0.0
        statuses = [str(((row.get("fulfillment") or {}).get("status") or "")).upper()
                    for row in technical_records]
        if any(not status or status == "NOT_GENERATED" for status in statuses):
            return None
        return round(_clamped_ratio(sum(status == "FULFILLED" for status in statuses), len(statuses)), 4)
    rows = matrix.get("rows") or []
    technical = [
        row for row in rows
        if str((row.get("scope_disposition") or "")).upper() != "EXCLUDED"
    ]
    if not technical:
        return None
    statuses = [str((row.get("fulfillment") or {}).get("status") or "").upper() for row in technical]
    if any(not status or status == "NOT_GENERATED" for status in statuses):
        return None
    return _clamped_ratio(sum(status == "FULFILLED" for status in statuses), len(statuses))


def _arrived_archetype_counts(fragments: dict[str, dict[str, Any]]) -> dict[str, int]:
    """按"真正到达的 Content Unit"统计 Archetype 分布，避免用计划值冒充结构化占比。"""
    counts: dict[str, int] = {}
    for fragment in (fragments or {}).values():
        for row in fragment.get("content_unit_fragments") or []:
            archetype = str((row or {}).get("archetype") or "NARRATIVE").upper()
            counts[archetype] = counts.get(archetype, 0) + 1
    return counts


def _structured_visible_chars(fragments: dict[str, dict[str, Any]]) -> int:
    """结构化原型写出的字数（含 payload 里的表格/清单文字），供 §13 切叙述页与结构化页。

    逐页文本看不出"这页是不是表格页"，但每个 Content Unit 的原型和字数看得见；
    只有结构化原型算数，NARRATIVE 的字数留给叙述页。
    """
    total = 0
    for fragment in (fragments or {}).values():
        for row in fragment.get("content_unit_fragments") or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("archetype") or "NARRATIVE").upper() == "NARRATIVE":
                continue
            payload = row.get("payload")
            total += len(str(row.get("content") or ""))
            if isinstance(payload, (dict, list)):
                total += len(visible_text(payload))
    return total


def build_document_scale_report(
    bundle: dict[str, Any],
    artifact: dict[str, Any],
    fragments: dict[str, dict[str, Any]],
    *,
    total_chars: int,
    coverage_gate: dict[str, Any],
    hierarchy_metrics: dict[str, Any],
    completed_content_units: list[str] | None,
    semantic_repetition: int,
    run_dir: Path,
) -> tuple[dict[str, Any], int | None]:
    structure = count_artifact_structure(artifact)
    rendered_pages = estimate_rendered_pages(effective_chars=total_chars, structure=structure)
    totals = (bundle.get("content_budget") or {}).get("totals") or {}
    page_min, page_max = scale_page_targets(bundle)
    # §13：有真实分页实测就用它（技术有效页 + 六类页数），没有就明说 basis。
    measured = _read_page_classification(run_dir)
    technical_pages = int(measured.get("technical_effective_pages") or 0)
    archetypes = _arrived_archetype_counts(fragments) or {
        str(key).upper(): int(value or 0)
        for key, value in (hierarchy_metrics.get("content_units_by_archetype") or {}).items()
    }
    planned_cus = int(hierarchy_metrics.get("content_units") or 0)
    scale = evaluate_document_scale(
        rendered_pages=rendered_pages,
        page_target_min=page_min,
        page_target_max=page_max,
        effective_chars=total_chars,
        char_target_min=artifact_chars_from_ledger(int(totals.get("target_effective_chars_min") or 0)),
        char_target_currency=(f"artifact_visible_chars=ledger×{ARTIFACT_CHARS_PER_LEDGER_CHAR_RUN_D}"
                              if totals.get("target_effective_chars_min") else "not_supplied"),
        requirement_coverage=_clamped_ratio(coverage_gate.get("covered_must"), coverage_gate.get("planned_must")),
        scoring_coverage=_scoring_fulfillment_ratio(run_dir),
        cu_completion=_clamped_ratio(len(completed_content_units or []), planned_cus),
        archetype_counts=archetypes,
        table_count=structure["tables"],
        sop_count=structure["processes"],
        checklist_count=structure["list_blocks"],
        matrix_count=structure["matrices"],
        form_count=structure["forms"],
        semantic_repetition=int(semantic_repetition or 0),
        artifact_integrity=bool(artifact.get("sections")),
        technical_effective_pages=technical_pages or None,
        page_classes=measured.get("page_classes") or None,
        placeholder_page_ratio=measured.get("placeholder_page_ratio"),
    )
    # §13：六类页数单独成表，交付审计和报告都读这一份，不再从 dimensions 里反推。
    scale["page_report"] = page_report(
        measured,
        effective_chars=total_chars,
        structure=structure,
        structured_chars=_structured_visible_chars(fragments),
    )
    scale["page_targets"] = [page_min, page_max]
    constraints = ((bundle.get("document_target") or {}).get("word") or {}).get("page_constraints")
    if constraints:
        page_contract = evaluate_page_contract(measured, constraints)
        page_contract["artifact_stage"] = "FIRST_DRAFT_MEASUREMENT"
        scale["page_contract"] = page_contract
        scale["findings"].extend(page_contract["findings"])
        if page_contract["status"] == "BLOCK":
            scale["status"] = "BLOCK"
        elif page_contract["status"] == "NOT_MEASURED" and scale["status"] == "PASS":
            scale["status"] = "NEEDS_REVIEW"
    # §21 双边三态（供交付端与 run_state 读）：这里的页数来自初稿的逐页分类，
    # basis 一起写清楚，交付端若有真实最终分页会另行覆盖（`run_state` 只认带来源的那一份）。
    scale["page_band"] = _page_band(technical_pages, [page_min, page_max])
    scale["page_band_basis"] = "MEASURED_PAGE_CLASSIFICATION" if technical_pages else "NOT_MEASURED"
    scale["arrived_content_units"] = sum(archetypes.values())
    scale["planned_content_units"] = planned_cus
    scale["structure"] = structure
    return scale, rendered_pages
