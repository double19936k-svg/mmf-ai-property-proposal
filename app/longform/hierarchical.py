"""MMF-007 Phase 2：分层生成接线（Volume 作为长文生产一级执行单元）。

增量设计（总纲 V1.1 / 任务书 Phase 2）：
- 不改变既有 Section 批次执行路径；Volume 是 Section 之上的分组与进度单位；
- VOLUME_GENERATION_PLAN 落盘为 11_volume_generation_plan.json；
- 每个 Volume 完成后产出中间 DOCX（runs/<run>/volumes/Vxx.docx）并记录
  LAST_VALID_VOLUME_ARTIFACT，长任务中途失败仍保留已完成篇章；
- Checkpoint v0.3-hierarchical：新增 completed_volumes / volume_artifacts /
  lineage_fingerprint，并提供 v0.2 → v0.3 幂等迁移（老 Run 可继续 Resume）。
"""
from __future__ import annotations
from filesystem_paths import filesystem_path

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from providers.base import atomic_replace, short_stem

CHECKPOINT_SCHEMA_V3 = "longform-checkpoint-v0.3-hierarchical"
VOLUME_PLAN_SCHEMA = "volume-generation-plan-v0.1"

# §44–§49 决策记录：Volume 中间 DOCX 是 **B（Checkpoint / Debug 辅助）**，不是 A（必需 Artifact）。
#
# 依据（都在 Run D `tender_20260922_005351_b6002a` 上量到，不是推测）：
# 1) 最终装配吃的是 Section fragment（`collect_fragments` → `assemble_word_artifact`），
#    从不读 `volumes/Vxx.docx`；Run D 交付件完整（27 Section / 304 CU / 已下载 DOCX），
#    而 `volumes/` 只有 V03、V06 两份 —— 若按 A 判，一次合法交付会被 STATE-04 记成 FAIL。
# 2) 5 份缺件的原因是 9 个 Section 处于 `COMPLETED_PROVISIONAL`（正文已到、深度待补），
#    分卷渲染端比状态端更严，同一个"完成"有两套定义。
# 3) 分卷 DOCX 的真实作用写在模块 docstring 里：长任务中途失败时保留已完成篇章（Resume 锚点）。
#
# 因此：交付判定不得依赖分卷 DOCX 是否落盘，但**必须**依赖下面这组 B 语义事实 ——
# 分卷 Section 齐 / CU 齐（审计要带 ID 清单，只有计数＝量不到）/ 分组守恒 /
# 装配输入齐（权威源是引擎落的 first_draft_artifact.json，不是 app_core 投影）/
# 最终件谱系齐。`run_state.evaluate_invariants` 的 STATE-04a…d 与 `derive_run_state`
# 都只读这一份口径，不得再各自另立。
VOLUME_ARTIFACT_CONTRACT = "B_CHECKPOINT_ONLY"
VOLUME_ARTIFACT_CONTRACT_RECORD = {
    "contract": VOLUME_ARTIFACT_CONTRACT,
    "required_for_delivery": False,
    "delivery_evidence": (
        "volume_sections_complete", "volume_content_units_complete",
        "volume_grouping_conservation_holds", "assembly_inputs_complete",
        "final_artifact_content_present"),
    "checkpoint_role": ("Resume 锚点与中途失败保全：last_valid_volume_artifact / Vxx.docx 仅用于恢复与排查"),
    "forbidden": ("为凑齐分卷 DOCX 而放宽内容门、补写商务页或重复渲染撑交付（§3 Technical-Only 边界）",),
}


def volume_artifact_contract() -> dict[str, Any]:
    return dict(VOLUME_ARTIFACT_CONTRACT_RECORD)



def atomic_write_json(path: Path, value: Any) -> None:
    """原子写入：temp → 同目录 → os.replace（V1.1 长任务持久化硬要求）。"""
    path = filesystem_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时名不得比目标更长：MAX_PATH=260 下 .<fullname>.<32hex>.tmp 会让深层
    # Section 目录里的每次 checkpoint 写入直接失败。
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent),
        prefix=f".{short_stem(path, 22)}.", suffix=".tmp", delete=False
    )
    try:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        atomic_replace(handle.name, path)
    except Exception:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def build_volume_generation_plan(
    hierarchy: dict[str, Any],
    bundle: dict[str, Any],
    chosen_ids: list[str] | None = None,
) -> dict[str, Any]:
    """从分层文档树构建 VOLUME_GENERATION_PLAN。

    chosen_ids 限制参与生成的 Section（非 full 模式）；契约缺失的 Section 跳过。
    """
    contracts = {
        row.get("section_id"): row
        for row in (bundle.get("section_contracts") or {}).get("contracts") or []
    }
    # P0-4（Codex Blocker）：Content Unit 真实进入 Volume Plan —— content_unit_ids
    # 不得为空（除非该 Section 明确 NO_CONTENT_UNIT_REQUIRED）。
    units_by_section: dict[str, list[str]] = {}
    for unit in hierarchy.get("content_units") or []:
        units_by_section.setdefault(str(unit.get("section_id")), []).append(str(unit.get("content_unit_id")))
    budget_chapters = {
        row.get("chapter_id"): row
        for row in (bundle.get("content_budget") or {}).get("chapters") or []
    }
    chosen = set(chosen_ids) if chosen_ids is not None else None
    volumes: list[dict[str, Any]] = []
    for volume in hierarchy.get("volumes") or []:
        volume_id = str(volume.get("volume_id") or "")
        chapter_ids = [str(cid) for cid in volume.get("chapter_ids") or []]
        sections: list[dict[str, Any]] = []
        content_unit_ids: list[str] = []
        scoring_items: set[str] = set()
        must_requirements: set[str] = set()
        target_chars = 0
        for section in hierarchy.get("sections") or []:
            sid = str(section.get("section_id") or "")
            if section.get("volume_id") != volume_id:
                continue
            if chosen is not None and sid not in chosen:
                continue
            contract = contracts.get(sid) or {}
            target = int(((contract.get("target_words") or {}).get("max")) or 0)
            target_chars += target
            for item in contract.get("scoring_items") or []:
                scoring_items.add(str(item))
            for req in contract.get("source_requirements") or []:
                if str(req).startswith("REQ-"):
                    must_requirements.add(str(req))
            sections.append({"section_id": sid, "section_title": section.get("section_title"), "chapter_id": section.get("chapter_id")})
            for cu_id in units_by_section.get(sid, []):
                content_unit_ids.append(cu_id)
        if not sections:
            continue
        volumes.append({
            "volume_id": volume_id,
            "title": volume.get("volume_title"),
            "chapter_ids": chapter_ids,
            "section_ids": [row["section_id"] for row in sections],
            "sections": sections,
            "content_unit_ids": content_unit_ids,
            "target_effective_chars": target_chars,
            "target_page_range_estimate": [
                max(1, round(target_chars / 700)), round(target_chars / 850) + 1,
            ],
            "scoring_items": sorted(scoring_items),
            "must_requirement_count": len(must_requirements),
            "dependencies": sorted({
                dep.get("source_section")
                for dep in (bundle.get("dependency_map") or {}).get("dependencies") or []
                if dep.get("dependent_section") in {row["section_id"] for row in sections}
            }),
            "generation_status": "PLANNED",
            "checkpoint_state": {},
            "artifact_path": None,
        })
    return {
        "schema_version": VOLUME_PLAN_SCHEMA,
        "volumes": volumes,
        "total_target_effective_chars": sum(row["target_effective_chars"] for row in volumes),
        "budget_note": "页数为按有效字符的估计；300页是能力上限验证目标，不是每个项目的固定要求。",
    }


def load_or_build_volume_plan(run_dir: Path, bundle: dict[str, Any], chosen_ids: list[str] | None = None) -> dict[str, Any]:
    """优先读取 11_volume_generation_plan.json；缺失时从 08 层级或章节目录推导。"""
    plan_path = Path(run_dir) / "11_volume_generation_plan.json"
    if plan_path.is_file():
        try:
            return json.loads(plan_path.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    hierarchy_path = Path(run_dir) / "08_document_hierarchy.json"
    hierarchy: dict[str, Any] = {}
    if hierarchy_path.is_file():
        try:
            hierarchy = json.loads(hierarchy_path.read_text(encoding="utf-8-sig"))
        except Exception:
            hierarchy = {}
    if hierarchy.get("volumes"):
        plan = build_volume_generation_plan(hierarchy, bundle, chosen_ids)
    else:
        # 兼容回退：单 Volume 承载全部（等价于既有行为）
        plan = {
            "schema_version": VOLUME_PLAN_SCHEMA,
            "volumes": [{
                "volume_id": "V00",
                "title": "完整方案（未分层回退）",
                "chapter_ids": [row.get("chapter_id") for row in (bundle.get("word_plan") or {}).get("outline") or []],
                "section_ids": [row.get("section_id") for row in (bundle.get("section_contracts") or {}).get("contracts") or []],
                "sections": [],
                "content_unit_ids": [],
                "target_effective_chars": 0,
                "target_page_range_estimate": [0, 0],
                "scoring_items": [],
                "must_requirement_count": 0,
                "dependencies": [],
                "generation_status": "PLANNED",
                "checkpoint_state": {},
                "artifact_path": None,
            }],
            "total_target_effective_chars": 0,
            "budget_note": "legacy fallback",
        }
        if chosen_ids is not None:
            plan["volumes"][0]["section_ids"] = [sid for sid in plan["volumes"][0]["section_ids"] if sid in set(chosen_ids)]
    atomic_write_json(plan_path, plan)
    return plan


def volume_word_plan(word_plan: dict[str, Any], volume: dict[str, Any]) -> dict[str, Any]:
    """为 Volume 中间 DOCX 生成裁剪后的 word_plan（仅保留本卷章节）。"""
    keep = set(volume.get("chapter_ids") or [])
    outline = [
        chapter for chapter in (word_plan.get("outline") or [])
        if str(chapter.get("chapter_id")) in keep
    ]
    return {**word_plan, "outline": outline}


def migrate_checkpoint(data: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Checkpoint 迁移：v0.2 → v0.3-hierarchical（幂等、增量、不破坏旧字段）。

    返回 (迁移后的数据, 动作)；动作 ∈ {"migrated_v0.2_to_v0.3", "already_v0.3", "unknown_version_kept"}。
    无法安全迁移的版本保留原数据并显式标注 MIGRATION_NOT_SAFE 语义（不静默清空）。
    """
    version = str(data.get("schema_version") or "")
    if version == CHECKPOINT_SCHEMA_V3:
        return data, "already_v0.3"
    if version in {"longform-checkpoint-v0.2", "longform-checkpoint-v0.1", ""}:
        migrated = dict(data)
        migrated["schema_version"] = CHECKPOINT_SCHEMA_V3
        migrated.setdefault("completed_volumes", [])
        migrated.setdefault("completed_chapters", [])
        migrated.setdefault("completed_content_units", [])
        migrated.setdefault("volume_artifacts", [])
        migrated.setdefault("lineage_fingerprint", None)
        migrated["migration"] = {
            "from_version": version or "unknown",
            "migrated_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
            "strategy": "additive_fields_only",
            "safe": True,
        }
        return migrated, "migrated_v0.2_to_v0.3"
    # 未知未来版本：保留原数据，标记不可安全迁移
    data = dict(data)
    data["migration"] = {"safe": False, "note": "MIGRATION_NOT_SAFE: unknown future checkpoint version"}
    return data, "unknown_version_kept"


def load_checkpoint_state(run_root: Path) -> dict[str, Any] | None:
    """读取 checkpoint 并透明迁移到 v0.3（迁移结果写回，保证只迁移一次）。"""
    path = Path(run_root) / "checkpoint" / "state.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    migrated, action = migrate_checkpoint(data)
    if action == "migrated_v0.2_to_v0.3":
        atomic_write_json(path, migrated)
    return migrated


def completed_content_units(volume_plan: dict[str, Any], completed_sections: list[str]) -> list[str]:
    """从 Volume Plan 的 CU 注册推导已完成 CU（真实 CU-Sxx-yy-nn，禁止 Section 伪装）。"""
    completed = set(completed_sections or [])
    units: list[str] = []
    for volume in volume_plan.get("volumes") or []:
        for cu in volume.get("content_unit_ids") or []:
            sid = str(cu)[3:].rsplit("-", 1)[0] if str(cu).startswith("CU-") else ""
            if sid in completed:
                units.append(str(cu))
    return sorted(set(units))


def apply_plan_invalidation(
    run_dir: Path,
    previous_lineage: dict[str, Any],
    current_lineage: dict[str, Any],
) -> dict[str, Any]:
    """P0-5：正式 Resume 入口的 Plan Invalidation（不通过测试 helper）。

    按 plan_invalidation 结论清除失效 Section 的完成状态（status/fragment/governance），
    使 generate_word 重新生成；并落盘 plan_invalidation_audit.json。
    """
    from planning.hierarchy import plan_invalidation as compute_invalidation

    run_dir = Path(run_dir)
    completed: list[str] = []
    for status_path in (run_dir / "longform" / "word" / "sections").glob("*/status.json"):
        try:
            if json.loads(status_path.read_text(encoding="utf-8-sig")).get("status") in {"COMPLETED", "COMPLETED_WITH_WARNING", "COMPLETED_CONDITIONAL"}:
                completed.append(status_path.parent.name)
        except Exception:
            continue
    decision = compute_invalidation(previous_lineage, current_lineage, completed)
    invalidated = set(decision.get("invalidated_sections") or [])
    cleared: list[str] = []
    for sid in invalidated:
        section_dir = run_dir / "longform" / "word" / "sections" / sid
        if not section_dir.is_dir():
            continue
        for name in ("status.json", "fragment.json", "governance.json", "qa.json", "provider_raw.json"):
            target = section_dir / name
            if target.is_file():
                target.unlink()
        cleared.append(sid)
    audit = {
        "schema_version": "plan-invalidation-audit-v0.1",
        "evaluated_at": _now_iso(),
        "decision": {key: decision.get(key) for key in ("reusable_sections", "invalidated_sections", "pending_sections", "invalidation_scope", "reason")},
        "cleared_sections": sorted(cleared),
        "requires_full_regen": decision.get("invalidation_scope") in {"ALL_REQUIREMENTS_CHANGED", "ALL_FACTS_CHANGED"},
    }
    atomic_write_json(run_dir / "plan_invalidation_audit.json", audit)
    return audit


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")
