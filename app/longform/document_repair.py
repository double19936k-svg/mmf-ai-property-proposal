"""Whole-Artifact QA 之后的修复计划（MMF-007C §20-34、Round 2 §10-§17）。

本模块只把已经算好的文档级 QA 事实翻译成"带优先级的修复候选"，本身不产生任何
Provider 调用；真正花调用的动作由 ``LongformGenerationFactory.selective_repair``
和 ``supplement_depth`` 执行，并且全部受 ``GlobalRepairBudget`` 约束。

补深候选有四个来源，缺一不可：Scoring Gap、Requirement Gap、篇幅不足的 Section，
以及**页面缺口翻译出来的专业域能力缺口**（`capability_gaps`）。上一轮只有第三个来源，
而真实 300-Class 运行里没有一个 Section 判为 UNDER_LENGTH，于是 Supplement = 0。

优先级固定为：P0 BLOCK → 高价值 Scoring Gap → 关键 Requirement Gap → 明显 Depth
Gap → 其它 Needs Review。UNDER_LENGTH / 篇幅不足只能进 DEPTH_SUPPLEMENT，
不允许再以"整节重写"的形式占用修复预算。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from governance.document_qa import regrade_findings, summarize_severity

from .capability_gaps import plan_capability_gaps
from .repair_policy import (
    PRIORITY_BLOCK,
    PRIORITY_DEPTH_GAP,
    PRIORITY_REQUIREMENT_GAP,
    PRIORITY_SCORING_GAP,
    REPAIR_PRIORITY_ORDER,
)

MODE_REPAIR = "repair"
MODE_SUPPLEMENT = "supplement"


def _topic_of(row: dict[str, Any]) -> str:
    """Best-effort human-readable subject for a gap, used as supplement intent."""
    for key in ("normalized_requirement", "requirement", "text", "label", "name", "title"):
        value = str(row.get(key) or "").strip()
        if value:
            return value[:80]
    return ""


# 候选清单可以远大于预算；这里只是防止一次 300 页运行把上千个缺口塞进排序。
MAX_CANDIDATES_PER_MODE = 24
# 补深候选单独放宽：一次页面缺口可以翻译出几十条能力缺口，谁真正花调用由
# GlobalRepairBudget 的补深上限决定。截在 24 条会让高优先级的需求缺口把
# "还差 40 页"这件事整个挤掉，等于回到上一轮 Supplement = 0 的老路。
MAX_SUPPLEMENT_CANDIDATES = 40


def _score_of(row: dict[str, Any]) -> int:
    for key in ("max_score", "score", "weight", "planned_response_words"):
        try:
            return int(float(row.get(key) or 0))
        except (TypeError, ValueError):
            continue
    return 0


def collect_document_findings(qa: dict[str, Any]) -> list[dict[str, Any]]:
    """汇总文档级发现，并统一成 BLOCK / NEEDS_REVIEW / WARNING 三档。"""
    findings: list[dict[str, Any]] = []
    for source in (
        "depth", "document_scale", "bidder_evidence_gate",
        "content_budget_quality", "consistency", "local_normalization",
    ):
        report = qa.get(source) or {}
        for row in report.get("findings") or []:
            finding = dict(row)
            finding.setdefault("source", source)
            findings.append(finding)
    coverage = qa.get("coverage_gate") or {}
    if str(coverage.get("status") or "").upper() == "FAIL":
        findings.append({
            "source": "coverage_gate",
            "rule_id": "QR-001",
            "severity": "BLOCK",
            "issue": "关键招标需求未覆盖: " + "、".join(str(item) for item in (coverage.get("missing") or [])[:8]),
        })
    return regrade_findings(findings)


def _owner_section(requirement_id: str, matrix: dict[str, Any], known: set[str]) -> tuple[str | None, str]:
    for row in matrix.get("matrix") or []:
        if str(row.get("requirement_id") or "") != str(requirement_id):
            continue
        owners = [row.get("primary_section_id"), *(row.get("secondary_section_ids") or [])]
        owners = [str(sid) for sid in owners if sid]
        if not owners:
            return None, _topic_of(row)
        topic = _topic_of(row)
        for sid in owners:
            if sid in known:
                return sid, topic
        return owners[0], topic
    return None, ""


def _scoring_gaps(run_dir: Path, known: set[str]) -> list[dict[str, Any]]:
    path = run_dir / "09_scoring_matrix.json"
    if not path.is_file():
        return []
    try:
        rows = (json.loads(path.read_text(encoding="utf-8-sig")) or {}).get("rows") or []
    except (OSError, ValueError, TypeError):
        return []
    gaps = []
    for row in rows:
        status = str(((row.get("fulfillment") or {}).get("status")) or "").upper()
        if status != "MISSING":
            continue
        owners = [str(sid) for sid in (row.get("section_ids") or []) if sid]
        sid = next((value for value in owners if value in known), owners[0] if owners else "")
        if not sid:
            continue
        gaps.append({
            "section_id": sid,
            "scoring_item_id": str(row.get("scoring_item_id") or ""),
            "topic": _topic_of(row),
            "value_score": min(90, 60 + _score_of(row)),
        })
    return gaps


def _page_gap(qa: dict[str, Any]) -> tuple[int, str, int]:
    """离目标区间下沿还差几页，以及这个数字是从哪里量出来的。

    优先级：初稿真实渲染的**技术有效页**（§13，扣掉封面/目录/空页/占位页/越界页）→
    渲染物理页 → 900 字/页估算。后两种口径都可能把"撑篇幅的页"算成缺口，所以能用量
    第一种就用第一种；拿不到时退回下一级，并把 basis 如实写出来 —— 补深依据的是哪种
    口径，报告里必须能看出来，不能把估算当实测。
    """
    page_min = int(((qa.get("document_scale") or {}).get("page_targets") or [0, 0])[0] or 0)
    technical = int(qa.get("DRAFT_TECHNICAL_EFFECTIVE_PAGES") or 0)
    if technical > 0:
        return max(0, page_min - technical), "RENDERED_TECHNICAL", technical
    rendered = int(qa.get("DRAFT_RENDERED_PAGES") or 0)
    if rendered > 0:
        return max(0, page_min - rendered), "RENDERED", rendered
    estimated = int(qa.get("rendered_pages") or 0)
    return max(0, page_min - estimated), "ESTIMATED", estimated


def build_repair_plan(
    *,
    qa: dict[str, Any],
    word: dict[str, Any] | None,
    contracts: list[dict[str, Any]],
    requirement_matrix: dict[str, Any] | None,
    run_dir: Path,
) -> dict[str, Any]:
    """产出 {"repair": [...], "supplement": [...]}；同一 section 的同类缺口只留一条。"""
    word = word or {}
    contract_ids = {str(row.get("section_id") or "") for row in contracts or [] if row.get("section_id")}
    states: dict[str, Any] = dict(word.get("section_states") or {})
    # 修复预算只花在本轮实际负责生成的 Section 上：局部/续跑运行不得把预算烧在
    # 从未计划生成的章节，否则真正的缺口反而没预算可修。
    scope = {str(sid) for sid in (word.get("SCOPE_SECTION_IDS") or []) if sid}
    known = (scope | set(states)) or contract_ids

    findings = collect_document_findings(qa)
    severity_summary = summarize_severity(findings)

    repair: dict[str, dict[str, Any]] = {}
    supplement: dict[str, dict[str, Any]] = {}

    def add(bucket: dict[str, dict[str, Any]], *, sid: str, priority: str, kind: str,
            mode: str, issue_key: str, value_score: int, reason: str,
            extra: dict[str, Any] | None = None) -> None:
        if not sid or sid not in known:
            return
        key = f"{mode}:{issue_key}"
        existing = bucket.get(key)
        if existing is not None and int(existing.get("value_score") or 0) >= value_score:
            return
        row = {
            "section_id": sid,
            "issue_key": issue_key,
            "priority": priority,
            "priority_rank": REPAIR_PRIORITY_ORDER.index(priority) if priority in REPAIR_PRIORITY_ORDER else len(REPAIR_PRIORITY_ORDER),
            "failure_class": kind,
            "value_score": value_score,
            "reason": reason,
            "mode": mode,
        }
        row.update(extra or {})
        bucket[key] = row

    def arrived(sid: str) -> bool:
        return str(states.get(sid) or "").startswith("COMPLETED")

    def focus(reason: str, topic: str, **refs: str) -> list[dict[str, Any]]:
        return [{"reason": reason, "topic": topic, **refs}]

    # P0：整节缺失 / 技术性失败 —— 文档缺章是 BLOCK，且只能靠重新请求该节解决。
    for sid in sorted(set((word.get("TECHNICAL_FAILED_SECTIONS") or []))):
        add(repair, sid=sid, priority=PRIORITY_BLOCK, kind="TECHNICAL_FAILED_SECTION",
            mode=MODE_REPAIR, issue_key=f"BLOCK:{sid}:MISSING", value_score=95,
            reason="section technically failed with no arrived content")
    for sid in sorted(set(((qa.get("depth") or {}).get("metrics") or {}).get("missing_sections") or [])):
        add(repair, sid=sid, priority=PRIORITY_BLOCK, kind="MISSING_SECTION",
            mode=MODE_REPAIR, issue_key=f"BLOCK:{sid}:ABSENT", value_score=95,
            reason="contracted section absent from the assembled document")
    # 节内 QA 判定为 BLOCK 且仍未解决的（承诺/事实/契约缺口），按 P0 处理。
    artifact = qa.get("artifact") or {}
    for finding in findings:
        sid = str(finding.get("section_id") or "") or str(
            section_id_from_artifact_path(str(finding.get("path") or ""), artifact) or ""
        )
        if str(finding.get("severity") or "") != "BLOCK" or not sid:
            continue
        add(repair, sid=sid, priority=PRIORITY_BLOCK,
            kind=str(finding.get("rule_id") or "QUALITY_FAILURE"), mode=MODE_REPAIR,
            issue_key=f"BLOCK:{sid}:{finding.get('rule_id')}", value_score=90,
            reason=str(finding.get("issue") or "")[:160])

    # 高价值 Scoring Gap（评分项完全没有响应正文）
    for gap in _scoring_gaps(Path(run_dir), known):
        sid = gap["section_id"]
        refs = {"scoring_item_id": gap.get("scoring_item_id") or ""}
        bucket = supplement if arrived(sid) else repair
        add(bucket, sid=sid,
            priority=PRIORITY_SCORING_GAP, kind="SCORING_GAP",
            mode=MODE_SUPPLEMENT if arrived(sid) else MODE_REPAIR,
            issue_key=f"SCORING:{gap['scoring_item_id'] or sid}",
            value_score=int(gap["value_score"]),
            reason="scoring item fulfillment MISSING",
            extra={"focus": focus("SCORING_GAP", gap.get("topic") or sid, **refs)} if arrived(sid) else None)

    # 关键 Requirement Gap
    matrix = requirement_matrix or {}
    requirement_rows={str(row.get('requirement_id') or ''):row for row in matrix.get('matrix') or []}
    for requirement_id in (qa.get("coverage_gate") or {}).get("missing") or []:
        sid, topic = _owner_section(str(requirement_id), matrix, known)
        sid = sid or ""
        source_row=requirement_rows.get(str(requirement_id),{})
        domain=str(source_row.get('requirement_domain') or source_row.get('domain') or '')
        from planning.content_engineering import classify_technical_scope
        current_scope=classify_technical_scope(source_row)
        # Spend scarce supplement calls on concrete service obligations before
        # rubric wording, commercial amounts, or submission instructions. This
        # changes scheduling only: all original gaps remain in coverage QA.
        service_obligation=(str(source_row.get('mandatory_level') or '')=='MUST'
            and current_scope=='TECHNICAL_INCLUDED'
            and domain not in {'scoring','pricing','qualification','procedural','bidder_evidence'}
            and not re.search(r'人民币|万元|滞纳金|物业收费|物业管理费|偏差说明|评分|得[0-9一二三四五六七八九十至-]+分',topic or ''))
        value_score=85 if service_obligation else 70
        gap_class='SERVICE_OBLIGATION' if service_obligation else 'OTHER_REQUIREMENT'
        if arrived(sid):
            # 正文已在该节内：只新增承接该要求的 Content Unit，不重跑整节。
            add(supplement, sid=sid, priority=PRIORITY_REQUIREMENT_GAP, kind="REQUIREMENT_GAP",
                mode=MODE_SUPPLEMENT, issue_key=f"REQUIREMENT:{requirement_id}", value_score=value_score,
                reason="mandatory requirement not covered",
                extra={"focus": focus("REQUIREMENT_GAP", topic or str(requirement_id), requirement_id=str(requirement_id)),
                       'requirement_gap_class':gap_class,'grouped_requirement_ids':[str(requirement_id)]})
            continue
        add(repair, sid=sid, priority=PRIORITY_REQUIREMENT_GAP, kind="REQUIREMENT_GAP",
            mode=MODE_REPAIR, issue_key=f"REQUIREMENT:{requirement_id}", value_score=value_score,
            reason="mandatory requirement not covered")

    grouped_requirements={}
    for key,row in list(supplement.items()):
        if row.get('failure_class')!='REQUIREMENT_GAP':continue
        group=(row['section_id'],row.get('requirement_gap_class'))
        if group not in grouped_requirements:
            grouped_requirements[group]=row
            continue
        survivor=grouped_requirements[group]
        survivor['focus'].extend(row.get('focus') or [])
        survivor['grouped_requirement_ids'].extend(row.get('grouped_requirement_ids') or [])
        del supplement[key]

    # 明显 Depth Gap：篇幅不足永远走 DEPTH_SUPPLEMENT（新增 Content Unit），不重跑整节。
    for row in (word.get("DEPTH_GAP_SECTIONS") or []):
        sid = str(row.get("section_id") or "")
        if str(row.get("deficit") or "") == "NONE":
            continue
        add(supplement, sid=sid, priority=PRIORITY_DEPTH_GAP, kind="UNDER_LENGTH",
            mode=MODE_SUPPLEMENT, issue_key=f"DEPTH:{sid}", value_score=45 if row.get("deficit") == "MATERIAL" else 25,
            reason=str(row.get("reason") or "DEPTH_INCOMPLETE"))

    # §12：页面缺口 → 专业域能力缺口 → 新增 Content Unit。候选不再只来自"这一节字数不够"。
    gap_pages, page_basis, arrived_pages = _page_gap(qa)
    artifact_sids = {str(row.get("section_id") or "") for row in (artifact.get("sections") or [])
                     if isinstance(row, dict)}
    capability_plan = plan_capability_gaps(
        artifact,
        page_gap=gap_pages,
        arrived_pages=arrived_pages,
        archetypes_by_section=(word.get("SECTION_CU_ARCHETYPES") or {}),
        exclude_section_ids=artifact_sids - known,
    )
    grouped_gaps: dict[str, list[dict[str, Any]]] = {}
    for gap in capability_plan.get("gaps") or []:
        grouped_gaps.setdefault(str(gap.get("section_id") or ""), []).append(gap)
    # 同一节的几项能力缺口合成一条候选：一次调用带回 1-3 个新 CU。拆成"同一节、
    # 一次只补一项"会把有限的补充预算花在重复请求上，能覆盖的章节反而更少。
    for sid, section_gaps in grouped_gaps.items():
        add(supplement, sid=sid, priority=PRIORITY_DEPTH_GAP,
            kind="CAPABILITY_GAP", mode=MODE_SUPPLEMENT,
            issue_key=f"CAPABILITY:{sid}",
            value_score=max(int(row.get("value_score") or 0) for row in section_gaps),
            reason=" + ".join(f"{row.get('reason')}:{row.get('capability_key')}" for row in section_gaps)[:180],
            extra={"focus": [{
                "reason": "CAPABILITY_GAP",
                "topic": str(row.get("missing_capability") or ""),
                "missing_capability": str(row.get("missing_capability") or ""),
                "required_output": str(row.get("required_output") or ""),
                "archetype": str(row.get("archetype") or ""),
                "novelty_basis": str(row.get("novelty_basis") or ""),
            } for row in section_gaps]})

    ordered_repair = sorted(repair.values(), key=lambda row: (row["priority_rank"], -row["value_score"]))[:MAX_CANDIDATES_PER_MODE]
    ordered_supplement = sorted(supplement.values(),
                                key=lambda row: (row["priority_rank"], -row["value_score"]))[:MAX_SUPPLEMENT_CANDIDATES]
    return {
        "schema_version": "document-repair-plan-v0.1",
        "findings": findings,
        "findings_summary": severity_summary,
        "repair": ordered_repair,
        "supplement": ordered_supplement,
        "counts": {
            "findings": len(findings),
            "block": severity_summary.get("counts", {}).get("BLOCK", 0),
            "needs_review": severity_summary.get("counts", {}).get("NEEDS_REVIEW", 0),
            "warning": severity_summary.get("counts", {}).get("WARNING", 0),
            "repair_candidates": len(ordered_repair),
            "supplement_candidates": len(ordered_supplement),
            "capability_gap_candidates": sum(1 for row in ordered_supplement if row.get("failure_class") == "CAPABILITY_GAP"),
        },
        "content_capability_gaps": {
            "status": capability_plan.get("status"),
            "page_gap": gap_pages,
            "page_basis": page_basis,
            "arrived_pages": arrived_pages,
            "planned": len(capability_plan.get("gaps") or []),
            "metrics": capability_plan.get("metrics") or {},
        },
        "policy": "severity graded BLOCK/NEEDS_REVIEW/WARNING; provider repair only for budgeted priorities; UNDER_LENGTH supplements new Content Units and never re-runs a Section",
    }


_PATH_SECTION = re.compile(r"sections\[(\d+)\]")


def section_id_from_artifact_path(path: str, artifact: dict[str, Any] | None) -> str | None:
    """把 QA 发现里的 ``artifact.sections[7].…`` 路径映射回 section_id。"""
    match = _PATH_SECTION.search(str(path or ""))
    if not match:
        return None
    rows = (artifact or {}).get("sections") or []
    index = int(match.group(1))
    if 0 <= index < len(rows):
        return str(rows[index].get("section_id") or "") or None
    return None
