"""MMF-007 Phase 3：300页 → 30~60页精编 Word Compression Planner。

原则（总纲 V1.1 / 任务书 17-20 节）：
- 压缩按优先级保留：MUST > 高分评分项 > Core Facts > 差异化 > 关键流程 > 承诺边界 > 证据；
- 可压缩：背景套话、重复说明、低价值操作细节；
- 禁止：把"建议"压成"已具备"、把条件承诺压成无条件承诺、丢评分点；
- 每个精编章节保留 source_volume_ids / source_section_ids / requirement_ids /
  scoring_item_ids 追溯链，输出 compression_trace.json；
- 验收：CORE_SCORING_LOSS = 0，CORE_MUST_LOSS = 0（Mock 验证口径 SYNTHETIC_VALIDATED）。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

TARGET_CHARS_PER_PAGE = 900


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _visible_chars(value: Any) -> int:
    if isinstance(value, dict):
        return sum(_visible_chars(v) for v in value.values())
    if isinstance(value, list):
        return sum(_visible_chars(v) for v in value)
    return len(str(value)) if isinstance(value, str) else 0


def build_compression_plan(
    longform_artifact: dict[str, Any],
    hierarchy: dict[str, Any],
    scoring_matrix: dict[str, Any],
    requirement_matrix: dict[str, Any],
    *,
    target_pages: tuple[int, int] = (30, 60),
) -> dict[str, Any]:
    """从长文 Artifact 构建精编计划。

    MUST/高分评分项所属 Section 全保；其余按字符预算从低优先级开始压缩
    （SUMMARY_ONLY 模式保留标题+要点）。
    """
    budget_chars = int(target_pages[1] * TARGET_CHARS_PER_PAGE * 0.92)
    floor_chars = int(target_pages[0] * TARGET_CHARS_PER_PAGE * 0.8)

    section_volume: dict[str, str] = {}
    for section in hierarchy.get("sections") or []:
        section_volume[str(section.get("section_id"))] = str(section.get("volume_id") or "")
    scoring_weight: dict[str, float] = {}
    for row in scoring_matrix.get("rows") or []:
        scoring_weight[str(row.get("scoring_item_id"))] = float(row.get("max_score") or 0) or (20.0 if row.get("must_respond") else 5.0)
    must_requirements: set[str] = {
        str(row.get("requirement_id"))
        for row in requirement_matrix.get("matrix") or []
        if row.get("mandatory_level") == "MUST"
    }
    # 覆盖关系一律从矩阵推导。旧实现只看长文 Artifact 自报的 scoring_items /
    # used_requirement_ids，而真实链路与 Mock 产物的 Section 都没有这两个字段，
    # 于是"核心保护集"恒为空，KEEP_FULL 退化成"按字数从大到小塞预算"，评分点的唯一
    # 应答 Section 会被无声降成摘要——验收器抓到过一次（SCR-C12 / S03-01）。
    # 规划和验收从此用同一把尺子，不再各说各话。
    core_scoring_rows = [
        row for row in scoring_matrix.get("rows") or []
        if row.get("response_status") in {"PLANNED", "DERIVED_PLANNED"} and row.get("scoring_item_id")
    ]
    scoring_by_section: dict[str, list[str]] = {}
    primary_scoring: dict[str, list[str]] = {}
    for row in core_scoring_rows:
        item = str(row.get("scoring_item_id"))
        primary = str(row.get("primary_section_id") or "")
        if primary:
            primary_scoring.setdefault(primary, []).append(item)
        candidates = [primary] + [str(sid) for sid in (row.get("section_ids") or [])]
        for sid in {c for c in candidates if c}:
            scoring_by_section.setdefault(sid, []).append(item)
    must_sections: set[str] = {
        str(row.get("primary_section_id") or "")
        for row in requirement_matrix.get("matrix") or []
        if row.get("mandatory_level") == "MUST" and row.get("primary_section_id")
    }

    scored_sections: list[dict[str, Any]] = []
    for section in longform_artifact.get("sections") or []:
        sid = str(section.get("section_id") or "")
        chars = _visible_chars(section)
        section_scoring = sorted(
            {str(item) for item in section.get("scoring_items") or []} | set(scoring_by_section.get(sid) or []))
        # 权重只算"本 Section 应答的"评分项：共担同一评分项的旁证章节拿满分权重，
        # 会让优先级虚高、把预算让给不是唯一应答人的章节。
        weight = sum(scoring_weight.get(item, 0.0) for item in primary_scoring.get(sid) or [])
        priority = 100.0 * weight + min(50.0, chars / 100.0)
        has_must = bool(
            sid in must_sections
            or (section.get("used_requirement_ids")
                and (must_requirements & {str(r) for r in section.get("used_requirement_ids")}))
        )
        scored_sections.append({
            "section_id": sid,
            "title": section.get("title"),
            "chars": chars,
            "priority": round(priority, 1),
            "scoring_item_ids": section_scoring,
            "volume_id": section_volume.get(sid, ""),
            "has_must": has_must,
            "is_primary_responder": bool(primary_scoring.get(sid)),
        })

    kept_ids = {
        row["section_id"] for row in scored_sections
        if row["has_must"] or row["is_primary_responder"] or row["scoring_item_ids"]
    }
    remaining = budget_chars - sum(row["chars"] for row in scored_sections if row["section_id"] in kept_ids)
    for row in sorted(scored_sections, key=lambda r: r["priority"], reverse=True):
        if row["section_id"] in kept_ids:
            continue
        if remaining - row["chars"] >= floor_chars * 0.35:
            kept_ids.add(row["section_id"])
            remaining -= row["chars"]
        else:
            row["compression_mode"] = "SUMMARY_ONLY"

    plan_rows: list[dict[str, Any]] = []
    for row in scored_sections:
        compressed = row["section_id"] not in kept_ids
        plan_rows.append({
            "section_id": row["section_id"],
            "title": row["title"],
            "volume_id": row["volume_id"],
            "priority": row["priority"],
            "original_chars": row["chars"],
            "compression_mode": row.get("compression_mode") or "KEEP_FULL",
            "target_chars": row["chars"] if not compressed else max(200, int(row["chars"] * 0.25)),
            "scoring_item_ids": row["scoring_item_ids"],
            "core_protected": bool(row["has_must"] or row["is_primary_responder"]),
            "source_volume_ids": [row["volume_id"]] if row["volume_id"] else [],
            "source_section_ids": [row["section_id"]],
        })
    # 压缩目标不能反向覆盖“核心内容零损失”约束。只继续压缩非MUST、非应答评分项章节；
    # 若核心章节本身已超过页数目标，应如实报告超限（over_budget_chars），交由后续人工精编，
    # 而不是把评分点/MUST章节降成摘要后再用追溯字段掩盖语义损失。
    while sum(row["target_chars"] for row in plan_rows) > budget_chars:
        candidates = [
            row for row in plan_rows
            if row["compression_mode"] == "KEEP_FULL"
            and not row["core_protected"]
            and row["original_chars"] > 260
        ]
        if not candidates:
            break
        row = min(candidates, key=lambda item: (item["priority"], -item["original_chars"]))
        row["compression_mode"] = "SUMMARY_ONLY"
        row["target_chars"] = max(200, int(row["original_chars"] * 0.25))
    total_target = sum(row["target_chars"] for row in plan_rows)
    protected_count = sum(1 for row in plan_rows if row["core_protected"])
    for row in plan_rows:
        row.pop("core_protected", None)
    return {
        "schema_version": "compression-plan-v0.1",
        "generated_at": _now(),
        "target_pages": list(target_pages),
        "budget_chars": budget_chars,
        "plan_rows": plan_rows,
        "summary": {
            "original_chars": sum(row["chars"] for row in scored_sections),
            "target_chars": total_target,
            "sections_kept_full": sum(row["compression_mode"] == "KEEP_FULL" for row in plan_rows),
            "sections_summary_only": sum(row["compression_mode"] == "SUMMARY_ONLY" for row in plan_rows),
            "sections_core_protected": protected_count,
            "over_budget_chars": max(0, total_target - budget_chars),
            "estimated_pages": round(total_target / TARGET_CHARS_PER_PAGE),
        },
    }


def render_condensed_artifact(longform_artifact: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """按计划产出精编 Artifact：KEEP_FULL 原样保留；SUMMARY_ONLY 保留标题+首段要点。

    SUMMARY 页不得把条件表述改为无条件承诺：仅做原文首段摘录，不改写语义。
    """
    by_id = {str(section.get("section_id") or ""): section for section in longform_artifact.get("sections") or []}
    sections_out: list[dict[str, Any]] = []
    for row in plan.get("plan_rows") or []:
        section = by_id.get(str(row.get("section_id")))
        if section is None:
            continue
        if row.get("compression_mode") == "KEEP_FULL":
            sections_out.append({**section, "compression_mode": "KEEP_FULL"})
        else:
            blocks = section.get("body_blocks") or section.get("content_blocks") or []
            first_text = ""
            for block in blocks:
                content = block.get("content") or block.get("description") or block.get("text") if isinstance(block, dict) else str(block)
                if content:
                    first_text = str(content)[:280]
                    break
            sections_out.append({
                "section_id": section.get("section_id"),
                "title": section.get("title"),
                "core_message": section.get("core_message") or section.get("key_message") or "",
                "body_blocks": [{"type": "paragraph", "content": first_text or "本节为要点摘要，完整执行细节见长文版对应章节。"}],
                "compression_mode": "SUMMARY_ONLY",
                "source_trace": {
                    "source_volume_ids": row.get("source_volume_ids") or [],
                    "source_section_ids": row.get("source_section_ids") or [],
                    "scoring_item_ids": row.get("scoring_item_ids") or [],
                },
            })
    return {
        "title": longform_artifact.get("title"),
        "subtitle": "精编版",
        "sections": sections_out,
    }


def build_compression_trace(plan: dict[str, Any], requirement_matrix: dict[str, Any]) -> dict[str, Any]:
    """追溯链：精编章节 ↔ 长文 Volume/Section/Requirement/Scoring。"""
    trace_rows = []
    for row in plan.get("plan_rows") or []:
        requirement_ids = [
            matrix_row.get("requirement_id")
            for matrix_row in requirement_matrix.get("matrix") or []
            if matrix_row.get("primary_section_id") == row.get("section_id")
        ]
        trace_rows.append({
            "section_id": row.get("section_id"),
            "compression_mode": row.get("compression_mode"),
            "source_volume_ids": row.get("source_volume_ids") or [],
            "source_section_ids": row.get("source_section_ids") or [],
            "requirement_ids": [r for r in requirement_ids if r],
            "scoring_item_ids": row.get("scoring_item_ids") or [],
        })
    return {"schema_version": "compression-trace-v0.1", "rows": trace_rows, "generated_at": _now()}


def validate_no_core_loss(
    plan: dict[str, Any],
    scoring_matrix: dict[str, Any],
    requirement_matrix: dict[str, Any],
) -> dict[str, Any]:
    """验收：CORE_SCORING_LOSS = 0，CORE_MUST_LOSS = 0。"""
    dropped = {row["section_id"] for row in plan["plan_rows"] if row["compression_mode"] == "SUMMARY_ONLY"}
    kept_scoring: set[str] = set()
    kept_full_sections: set[str] = set()
    for row in plan["plan_rows"]:
        if row["compression_mode"] == "KEEP_FULL":
            kept_scoring.update(row.get("scoring_item_ids") or [])
            kept_full_sections.add(row["section_id"])
    scoring_loss = [
        str(row.get("scoring_item_id"))
        for row in scoring_matrix.get("rows") or []
        if row.get("response_status") in {"PLANNED", "DERIVED_PLANNED"}
        and (row.get("primary_section_id") or "") in dropped
        and str(row.get("scoring_item_id")) not in kept_scoring
        and row.get("scoring_item_id")
    ]
    must_rows = [
        row for row in requirement_matrix.get("matrix") or []
        if row.get("mandatory_level") == "MUST" and row.get("primary_section_id")
    ]
    must_loss = [
        row.get("requirement_id") for row in must_rows
        if row.get("primary_section_id") in dropped and row.get("primary_section_id") not in kept_full_sections
    ]
    return {
        "schema_version": "compression-loss-check-v0.1",
        "CORE_SCORING_LOSS": len(scoring_loss),
        "CORE_MUST_LOSS": len([x for x in must_loss if x]),
        "scoring_loss_ids": scoring_loss,
        "must_loss_ids": [x for x in must_loss if x],
        "status": "PASS" if not scoring_loss and not must_loss else "CORE_LOSS_DETECTED",
        "checked_at": _now(),
    }


def write_compression_bundle(
    run_dir: Path,
    longform_artifact: dict[str, Any],
    hierarchy: dict[str, Any],
    scoring_matrix: dict[str, Any],
    requirement_matrix: dict[str, Any],
    *,
    target_pages: tuple[int, int] = (30, 60),
) -> dict[str, Any]:
    """Round 2 Blocker 2 修复：三个 JSON 对象（plan/trace/loss report）先构造为
    Python dict/list 再序列化落盘；Path 只作为 output_path metadata，绝不作为
    JSON payload 写入正文。所有输出固定进入 <run_dir>/compression/。"""
    plan = build_compression_plan(longform_artifact, hierarchy, scoring_matrix, requirement_matrix, target_pages=target_pages)
    trace = build_compression_trace(plan, requirement_matrix)
    loss = validate_no_core_loss(plan, scoring_matrix, requirement_matrix)
    out_dir = Path(run_dir).resolve() / "compression"
    out_dir.mkdir(parents=True, exist_ok=True)
    documents = {
        "compression_plan.json": plan,
        "compression_trace.json": trace,
        "compression_loss_report.json": loss,
    }
    paths_meta = {}
    for name, payload in documents.items():
        target = out_dir / name
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        paths_meta[name.replace(".json", "")] = {"output_path": str(target), "bytes": target.stat().st_size}
    return {
        "plan": plan, "trace": trace, "loss_check": loss,
        "out_dir": str(out_dir), "paths": paths_meta,
    }


def run_compression_pipeline(
    run_dir: Path,
    *,
    target_pages: tuple[int, int] = (30, 60),
    render_docx: bool = True,
) -> dict[str, Any]:
    """正式压缩入口（生产链）：加载 run 产物 → 计划 → 追溯 → 校验 → 精编 DOCX → 结构 QA。

    输入：run_dir 的 08_document_hierarchy / 09_scoring_matrix / 02_requirement_section_matrix /
    generation_raw（长文 Artifact）。输出落盘 compression_* 并渲染 condensed_<页数>p.docx。
    """
    run_dir = Path(run_dir)
    hierarchy = json.loads((run_dir / "08_document_hierarchy.json").read_text(encoding="utf-8-sig"))
    scoring_matrix = json.loads((run_dir / "09_scoring_matrix.json").read_text(encoding="utf-8-sig"))
    requirement_matrix = json.loads((run_dir / "02_requirement_section_matrix.json").read_text(encoding="utf-8-sig"))
    generated = json.loads((run_dir / "generation_raw.json").read_text(encoding="utf-8-sig"))
    longform_artifact = generated["artifact"]

    bundle = write_compression_bundle(
        run_dir, longform_artifact, hierarchy, scoring_matrix, requirement_matrix, target_pages=target_pages
    )
    # Round 2 验收：落盘后读回，断言 JSON root 是 dict/list（不得是路径字符串）。
    readback_valid = True
    for meta in bundle["paths"].values():
        parsed = json.loads(Path(meta["output_path"]).read_text(encoding="utf-8-sig"))
        if not isinstance(parsed, (dict, list)):
            readback_valid = False
    condensed = render_condensed_artifact(longform_artifact, bundle["plan"])
    condensed_artifact = {
        "title": longform_artifact.get("title"),
        "subtitle": "精编版",
        "sections": condensed.get("sections") or [],
    }
    brief = {}
    brief_path = run_dir / "brief.json"
    if brief_path.is_file():
        try:
            brief = json.loads(brief_path.read_text(encoding="utf-8-sig"))
        except Exception:
            brief = {}
    content_path = run_dir / "compression_content.json"
    content_path.write_text(json.dumps({"brief": brief, "artifact": condensed_artifact}, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {
        "plan": bundle["plan"],
        "trace": bundle["trace"],
        "loss_check": bundle["loss_check"],
        "paths": bundle["paths"],
        "out_dir": bundle["out_dir"],
        "source_artifact": str(run_dir / "generation_raw.json"),
        "json_readback_valid": readback_valid,
        "condensed_chars": _visible_chars(condensed_artifact),
        "condensed_sections": len(condensed_artifact.get("sections") or []),
        "condensed_docx": None,
        "structural_qa": {"status": "SKIPPED", "reason": "render disabled"},
    }

    if render_docx:
        import subprocess
        import sys

        app_root = Path(__file__).resolve().parents[1]
        estimated_pages = bundle["plan"]["summary"]["estimated_pages"]
        docx_path = run_dir / f"condensed_{estimated_pages}p.docx"
        render = subprocess.run(
            [sys.executable, str(app_root / "build_docx.py"), str(content_path), str(docx_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
        )
        result["condensed_docx"] = str(docx_path) if render.returncode == 0 and docx_path.is_file() else None
        structural = {"status": "FAIL", "paragraphs": 0, "bytes": 0}
        if result["condensed_docx"]:
            try:
                from docx import Document as _Doc

                doc = _Doc(str(docx_path))
                paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
                structural = {"status": "PASS" if paragraphs else "FAIL", "paragraphs": len(paragraphs),
                              "bytes": docx_path.stat().st_size}
            except Exception as exc:
                structural = {"status": "FAIL", "error": str(exc)[:160]}
        result["structural_qa"] = structural
    result["loss_check"] = bundle["loss_check"]
    return result
