"""MMF-007C Round 4 §16–§20：超页定向削减的**执行器**（Round 3 只有计划，没有执行）。

Round 3 的 `targeted_reduction_plan` 把候选、理由、保护类都算清楚了，然后只把一个 JSON
写进运行目录就收工 —— Codex 复判因此认定 Reduction 是 PLAN_ONLY。本模块补的就是"执行"：

- 粒度只有 Section / Content Unit：改的是 `word/sections/<sid>/cus/CU-*.json` 与它的
  `fragment.json`，交付装配再读这两样，所以削减是真的会体现在最终 DOCX 上；
- **不得**调用 30～60 页整篇 Compression Pipeline，也不得重走一次生成：全程零 Provider
  调用，报告里的 `provider_called` 必须能被账本核对；
- 只动计划点名、且不带保护标记的单元。CU 级保护（MUST 应答 / 评分应答 / SOP / 技术表格 /
  关键流程）连"改一句"都不允许；句级再兜一层，含"必须/严禁/不得/不低于"的承诺句永不删，
  压不到额度就如实报残余缺口，不靠删承诺凑数；
- 每个动作留下 before/after、删了什么、为什么删、预计省几页；原文先落
  `sections/<sid>/reduction/` 快照，削减从不以"永久删除"的方式发生。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from planning.budget_control import (
    BUDGET_LEDGER_FILE,
    PENDING_ACTION,
    budget_of,
    judge_content_unit,
    merge_ledger,
    read_ledger,
    visible_chars_of,
    write_ledger,
)
from planning.content_padding import GENERIC_PADDING, HIGH_VALUE, classify_text
from planning.unified_budget import (
    ARTIFACT_CHARS_PER_LEDGER_CHAR_RUN_D,
    MIN_UNITS,
    RENDER_VISIBLE_CHARS_PER_TECHNICAL_PAGE,
    artifact_chars_from_ledger,
    structure_kind,
)
from .cu_assembly import (PROCESS_ARCHETYPES, apply_content_units, cu_table_descriptors,
                          strip_cu_structures)
from .hierarchical import atomic_write_json

EXECUTION_SCHEMA = "overshoot-reduction-execution-v0.1"
EXECUTION_FILE = "overshoot_reduction_execution.json"
SNAPSHOT_DIR = "reduction"
# 只有这些键会真的渲染进 Word；重建前后的长度对比只数这些，见 `_fragment_rendered_chars`。
_RENDERED_SLOT_KEYS = ("title", "columns", "headers", "rows", "steps", "items", "fields",
                       "notes", "content", "form_name", "purpose")

# 句级保护：含这些标记的句子是硬性承诺或量化指标，删掉可能直接把需求应答删没。
MUST_MARKER = re.compile(r"必须|严禁|不得|应[达满]到|不少于|不低于|不超过|按时限|违约责任")
SENTENCE_SPLIT = re.compile(r"(?<=[。！？；\n])")

# 执行状态字面量。SKIPPED_* 不是失败，是"按规则不许动"，必须与 EXECUTED 一样逐条点名。
EXECUTED = "EXECUTED_LOCAL_REDUCTION"
SKIPPED_PROTECTED = "SKIPPED_PROTECTED"
SKIPPED_NOT_PERSISTED = "SKIPPED_NOT_PERSISTED"
SKIPPED_NOT_GENERATED = "SKIPPED_NOT_GENERATED"
NO_REDUCTION_POSSIBLE = "NO_LOCAL_REDUCTION_POSSIBLE"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _sentences(text: str) -> list[str]:
    return [part for part in SENTENCE_SPLIT.split(str(text or "")) if part.strip()]


def _row_text(row: Any) -> str:
    """一条结构单元的可读文本：MUST 保护与增量分档读的是同一份文本。"""
    if isinstance(row, dict):
        return " ".join(str(value) for value in row.values())
    if isinstance(row, (list, tuple)):
        return " ".join(str(value) for value in row)
    return str(row)


def condense_prose(text: str, *, keep_to_chars: int) -> dict[str, Any]:
    """把叙述压到 `keep_to_chars` 以内：先删低增量句，再删单载体长句，MUST 句永不删。

    删除顺序就是 §22 的优先级（信息增量最低的先走），字数只是同档内的次级排序。
    """
    sentences = _sentences(text)
    rows = []
    for index, sentence in enumerate(sentences):
        verdict = classify_text(sentence)
        forced_protect = bool(MUST_MARKER.search(sentence))
        bucket = HIGH_VALUE if (forced_protect or verdict["bucket"] == HIGH_VALUE) else verdict["bucket"]
        rows.append({"index": index, "chars": len(sentence.strip()), "bucket": bucket,
                     "anchor_kinds": verdict["anchor_kinds"], "protected_sentence": forced_protect,
                     "text_head": sentence.strip()[:24]})
    total = sum(row["chars"] for row in rows)
    order = {GENERIC_PADDING: 0, "USABLE": 1, HIGH_VALUE: 2}
    droppable = sorted((row for row in rows if row["bucket"] != HIGH_VALUE),
                       key=lambda row: (order[row["bucket"]], -row["chars"], row["index"]))
    dropped: list[dict[str, Any]] = []
    removed = 0
    for row in droppable:
        if total - removed <= max(0, int(keep_to_chars)):
            break
        removed += row["chars"]
        dropped.append({"index": row["index"], "chars": row["chars"], "bucket": row["bucket"],
                        "anchor_kinds": row["anchor_kinds"], "text_head": row["text_head"]})
    dropped_indexes = {row["index"] for row in dropped}
    kept_text = "".join(sentence for index, sentence in enumerate(sentences)
                        if index not in dropped_indexes)
    return {
        "text": kept_text,
        "chars_before": total,
        "chars_removed": removed,
        "sentences_before": len(rows),
        "sentences_removed": len(dropped),
        "dropped": dropped,
        "protected_sentences_kept": sum(1 for row in rows if row["protected_sentence"]),
        "residual_over_chars": max(0, total - removed - max(0, int(keep_to_chars))),
    }


def shrink_structure(payload: dict[str, Any], *, archetype: str, keep_to_chars: int
                     ) -> dict[str, Any]:
    """把结构化 payload 压到额度内：按单元（行/步/项/栏）从低增量那头删，不破几何下限。

    含 MUST 标记（必须/严禁/不得/不少于…）的单元整条不可删 —— 与 `condense_prose` 的句级
    保护同一口径，否则削减会把承诺从表格与清单里整行带走。
    """
    kind = structure_kind(archetype)
    key = {"table": "rows", "step": "steps", "item": "items", "field": "fields"}.get(kind)
    if not key or not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        return {"payload": payload, "chars_removed": 0, "units_before": 0, "units_after": 0,
                "unit_key": key, "must_protected_units": 0, "residual_over_chars": 0}
    units = list(payload[key])
    floor = MIN_UNITS.get(kind, 1)
    before = visible_chars_of(payload)
    protected = sum(1 for row in units if MUST_MARKER.search(_row_text(row)))
    if before <= keep_to_chars or len(units) <= floor:
        return {"payload": payload, "chars_removed": 0, "units_before": len(units),
                "units_after": len(units), "unit_key": key,
                "must_protected_units": protected,
                "residual_over_chars": max(0, before - max(0, int(keep_to_chars)))}

    def unit_cost(row: Any) -> tuple[int, int]:
        verdict = classify_text(_row_text(row))
        low_first = 0 if verdict["bucket"] == GENERIC_PADDING else (1 if verdict["anchor_kinds"] < 2 else 2)
        return low_first, -len(_row_text(row))

    kept = list(units)
    removed = 0
    # 句级 MUST 保护原来只在 `condense_prose` 里有，结构单元（CHECKLIST 的项、TABLE 的行）
    # 走的这条分支不认，所以 Run D 真实回放里 5 条「不得/不少于/必须」承诺行被整行删掉，
    # 违反计划自己声明的 NEVER_DELETE_..._MUST_REQUIREMENT 政策。
    droppable = [row for row in sorted(units, key=unit_cost)
                 if not MUST_MARKER.search(_row_text(row))]
    for row in droppable:
        if before - removed <= max(0, int(keep_to_chars)) or len(kept) <= floor:
            break
        kept.remove(row)
        removed += visible_chars_of(row)
    updated = dict(payload)
    updated[key] = kept
    return {"payload": updated, "chars_removed": removed, "units_before": len(units),
            "units_after": len(kept), "unit_key": key,
            "must_protected_units": protected,
            "residual_over_chars": max(0, before - removed - max(0, int(keep_to_chars)))}


def _allowance(record: dict[str, Any], contract: dict[str, Any] | None) -> int:
    """削到哪：账本里的 CU 上限（budget_max），没有上限就退回契约上限。"""
    allowance = int(record.get("budget_max") or 0)
    if not allowance and contract:
        allowance = int(budget_of(contract)["max"])
    return allowance


def reduce_content_unit(cu_path: Path, *, contract: dict[str, Any], record: dict[str, Any],
                        allowance_chars: int, reason: str) -> dict[str, Any]:
    """就地削减一条已生成 CU：原文先落快照，再把新的 content/payload/budget 写回去。"""
    cu_id = str(record.get("content_unit_id") or cu_path.stem)
    original = _read_json(cu_path)
    if not isinstance(original, dict):
        return {"content_unit_id": cu_id, "execution_status": SKIPPED_NOT_PERSISTED}
    if original.get("protected") or record.get("protected"):
        return {"content_unit_id": cu_id, "execution_status": SKIPPED_PROTECTED}
    archetype = str(original.get("archetype") or (contract or {}).get("archetype") or "NARRATIVE")
    prose = str(original.get("content") or "")
    payload = original.get("payload") if isinstance(original.get("payload"), dict) else None
    before_total = int(record.get("actual_chars") or 0) or (
        visible_chars_of(prose) + visible_chars_of(payload))
    if before_total <= allowance_chars:
        return {"content_unit_id": cu_id, "execution_status": NO_REDUCTION_POSSIBLE,
                "before_chars": before_total, "after_chars": before_total, "removed_chars": 0}

    # 结构与正文共用同一条额度：先把结构压到它在统一预算里那份，剩下的才给正文。
    # 反过来（先砍正文留表格）正好是 §12 要避免的同义重复形状。
    structure_allowance = int(((contract or {}).get("unified_budget") or {}).get("structure_chars")
                              or 0) or max(0, allowance_chars // 3)
    structure_row = shrink_structure(payload or {}, archetype=archetype,
                                     keep_to_chars=structure_allowance)
    structure_chars = visible_chars_of(structure_row["payload"]) if payload is not None else 0
    prose_row = condense_prose(prose, keep_to_chars=max(0, allowance_chars - structure_chars))

    after_total = visible_chars_of(prose_row["text"]) + structure_chars
    removed = max(0, before_total - after_total)
    if removed <= 0:
        return {"content_unit_id": cu_id, "execution_status": NO_REDUCTION_POSSIBLE,
                "before_chars": before_total, "after_chars": after_total, "removed_chars": 0}
    snapshot = cu_path.parent.parent / SNAPSHOT_DIR / f"{cu_id}.json"
    atomic_write_json(snapshot, {**original, "reduction_snapshot": {
        "reason": reason, "allowance_chars": allowance_chars, "before_chars": before_total,
        "snapshot_at": _now()}})
    updated = {**original,
               "content": prose_row["text"],
               "payload": structure_row["payload"] if payload is not None else original.get("payload"),
               "effective_chars": after_total,
               "reduction": {
                   "reason": reason,
                   "allowance_chars": allowance_chars,
                   "before_chars": before_total,
                   "after_chars": after_total,
                   "removed_chars": removed,
                   "prose_removed": prose_row["chars_removed"],
                   "structure_removed": structure_row["chars_removed"],
                   "sentences_removed": prose_row["sentences_removed"],
                   "structure_units": f"{structure_row['units_before']}→{structure_row['units_after']}",
                   "structure_must_protected_units": int(
                       structure_row.get("must_protected_units") or 0),
                   "snapshot_path": str(snapshot),
                   "executed_by": EXECUTION_SCHEMA,
                   "executed_at": _now(),
               }}
    # 重判必须沿用原来那条额度，否则没有契约的 CU 会被 min/max=0 判成全员 OVER_BUDGET，
    # 账本就被执行器自己写脏了。
    judged_contract = contract or {
        "content_unit_id": cu_id,
        "parent_section_id": record.get("section_id") or "",
        "archetype": archetype,
        "min_budget": {"effective_chars": int(record.get("budget_min") or 0)},
        "target_budget": {"effective_chars": int(record.get("budget_target") or 0)},
        "max_budget": {"effective_chars": int(record.get("budget_max") or 0)},
    }
    updated["budget"] = judge_content_unit(judged_contract, content=updated)
    atomic_write_json(cu_path, updated)
    return {
        "content_unit_id": cu_id,
        "execution_status": EXECUTED,
        "before_chars": before_total,
        "after_chars": after_total,
        "removed_chars": removed,
        "prose_removed": prose_row["chars_removed"],
        "structure_removed": structure_row["chars_removed"],
        "sentences_removed": prose_row["sentences_removed"],
        "structure_units": f"{structure_row['units_before']}→{structure_row['units_after']}",
        "protected_sentences_kept": prose_row["protected_sentences_kept"],
        "residual_over_chars": max(0, after_total - allowance_chars),
        "snapshot_path": str(snapshot),
        "dropped_prose": prose_row["dropped"][:12],
    }


def _fragment_rendered_chars(doc: dict[str, Any]) -> int:
    """一节交付件里"会被渲染出去"的字：正文 + 表格 + 流程，三路同一个口径。

    只数渲染槽位的白名单键：`source_content_unit_id`、`protected` 这类记账字段重建后才会出现，
    一起数会把"没变长"误判成"变长了"。
    """
    total = 0
    for block in doc.get("body_blocks") or []:
        if isinstance(block, dict):
            total += visible_chars_of(block.get("content"))
    for slot in (doc.get("tables") or []) + (doc.get("processes") or []):
        if not isinstance(slot, dict):
            continue
        total += sum(visible_chars_of(slot.get(key)) for key in _RENDERED_SLOT_KEYS)
    return total


def rebuild_section_fragment(section_dir: Path, *,
                             normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None
                             ) -> dict[str, Any]:
    """用削减后的 CU 文件重建节正文（交付装配读的就是这一份）。

    `normalize` 传的是提交时同一条本地治理链：CU 文件存的是**治理前**的 Provider 正文，
    重建会把传输标记、客户名等修复一起回退掉（Round 2 就为此单独修过一次产物），
    所以重建后必须把同一条链再走一遍，而不是指望削减顺手产出干净文本。
    """
    fragment_path = section_dir / "fragment.json"
    fragment = _read_json(fragment_path)
    if not isinstance(fragment, dict):
        return {"rebuilt": False, "reason": "NO_FRAGMENT"}
    execution = fragment.get("content_unit_execution") or {}
    if not execution.get("planned_content_unit_ids"):
        # 不是 CU 粒度装配出来的节（历史/迁移件）：没有 CU 出处就不重建，否则会把手工
        # 规范化过的正文换成一份只含 CU 的形状。
        return {"rebuilt": False, "reason": "NO_CU_PROVENANCE"}
    cus_dir = section_dir / "cus"
    by_id: dict[str, dict[str, Any]] = {}
    for path in sorted(cus_dir.glob("CU-*.json")) if cus_dir.is_dir() else []:
        row = _read_json(path)
        if isinstance(row, dict) and row.get("content_unit_id"):
            by_id[str(row["content_unit_id"])] = row
    planned = [str(cu) for cu in execution.get("planned_content_unit_ids") or []]
    rows = [by_id[cu] for cu in planned
            if cu in by_id and by_id[cu].get("generation_origin", "provider_content_unit")
            == "provider_content_unit"]
    arrived = {str(row.get("content_unit_id")) for row in rows}
    missing = [cu for cu in planned if cu not in arrived]
    previously = sorted({str(cu) for cu in execution.get("assembled_content_unit_ids") or []})
    unrecallable = sorted(set(previously) - arrived)
    provenance_basis = "PLANNED_CONTENT_UNIT_IDS"
    if unrecallable:
        # 出处 id 找回来了几条、却没找回全部：`apply_content_units` 是**整体替换**
        # `body_blocks`，少一条 CU 就等于把那段正文从交付件里抹掉。Run D 的真实形状就是这样
        # —— Round 3 的卫生治理把 `planned_content_unit_ids` 写成中文归类词
        # （`CU-相关专项安排-01`），磁盘上却是位置 id（`CU-S02-02-01`），27 节 join 全部落空，
        # S02-02 的正文实测 8,600 可见字 → 0。
        same_section = [row for cu_id, row in sorted(by_id.items())
                        if str(row.get("generation_origin") or "provider_content_unit")
                        == "provider_content_unit"]
        if len(same_section) == len(set(previously)):
            # 退一档：本节的磁盘 CU 目录就是执行器削减的那一份 —— 只认**条数恰好相等**。
            # 条数多于装配过的记录不能放行：Run D 的 S06-01 磁盘 11 条、记录 11 条看似相等，
            # 但记录里的 archetype 被治理抹掉了一批（`MATRIX`/`PROCESS` 变空或译成中文），
            # 照磁盘重装就多装出 2 张表 + 2 段流程 —— 削减不许让交付件变长。
            rows = same_section
            planned = [str(row.get("content_unit_id")) for row in same_section]
            missing = []
            provenance_basis = "SECTION_DIR_CONTENT_UNITS"
        else:
            return {"rebuilt": False, "reason": "WOULD_DROP_ASSEMBLED_CONTENT",
                    "content_units_assembled": len(rows),
                    "unrecallable_content_unit_ids": unrecallable[:20]}
    # 表格与流程是在 base 上追加的：不先剥掉上一次 CU 加的那几份，重建一次就多一版表，
    # "削减后变短"会在表格这一路反变成"削减后变长"。
    base = strip_cu_structures(fragment)
    structure_basis = "CU_STRUCTURE_PROVENANCE_MARKER"
    stripped_any = sum(int(value or 0)
                       for value in (base.get("cu_structures_stripped") or {}).values())
    if not stripped_any:
        # 上一次装配进正文的表格/流程**没有出处标记**（Round 3 及更早的构建）：这时候分不出
        # "节自带的表"和"CU 加的表"，照直追加就是两张一样的表（Run D 实测 166 张 → 278 张）。
        # 判据必须用**这一次要装进去的那批 CU** 换算，不能拿 fragment 自己记的装配账换算：
        # 历史记录里的 archetype 会被治理抹掉一批（Run D 的 S06-01 记录算出 4 张、实装 6 张），
        # 用它就成了假绿灯。整槽条数与这一批 CU 要渲染的条数**完全相等**才认得全，
        # 这时整槽都是 CU 的，可以安全清空再按削减后的 CU 重装；对不上就不重建。
        want_tables = len(cu_table_descriptors(rows))
        want_processes = sum(1 for row in rows
                             if str(row.get("archetype") or "") in PROCESS_ARCHETYPES
                             and isinstance(row.get("payload"), dict) and row["payload"].get("steps"))
        have_tables = len([row for row in (base.get("tables") or []) if isinstance(row, dict)])
        have_processes = len([row for row in (base.get("processes") or []) if isinstance(row, dict)])
        if (want_tables, want_processes) != (have_tables, have_processes):
            return {"rebuilt": False, "reason": "CU_STRUCTURE_PROVENANCE_AMBIGUOUS",
                    "content_units_assembled": len(rows),
                    "structure_slots": {"cu_side": [want_tables, want_processes],
                                         "fragment_side": [have_tables, have_processes]}}
        base = {**base, "tables": [], "processes": [],
                "cu_structures_stripped": {"tables": have_tables, "processes": have_processes}}
        structure_basis = ("NO_CU_STRUCTURES" if not have_tables and not have_processes
                           else "DERIVED_FROM_ASSEMBLED_CONTENT_UNITS")
    rebuilt = apply_content_units(rows, base_fragment=base, section_id=section_dir.name,
                                  planned_ids=planned, missing_ids=missing)
    if normalize is not None:
        rebuilt = normalize(rebuilt)
    # 削减的定义就是变短：磁盘上的 CU 存的是**治理前**正文，历史件的 fragment 存的是治理后的，
    # 重装会把治理抹掉的字装回来（Run D 的 S02-02 实测 8,600 → 8,620）。
    # 宁可这一节不短，也不许交付件因为"削减"而变长 —— 落盘前最后一道，逐节量。
    before_chars = _fragment_rendered_chars(fragment)
    after_chars = _fragment_rendered_chars(rebuilt)
    if after_chars > before_chars:
        return {"rebuilt": False, "reason": "WOULD_LENGTHEN_SECTION",
                "content_units_assembled": len(rows),
                "rendered_chars": {"before": before_chars, "after": after_chars},
                "provenance_basis": provenance_basis, "structure_basis": structure_basis}
    rebuilt["reduction_rebuild"] = {
        "schema_version": EXECUTION_SCHEMA,
        "content_units_assembled": len(rows),
        "provenance_basis": provenance_basis,
        "provenance_ids_recovered_by_section_dir": (
            len(unrecallable) if provenance_basis == "SECTION_DIR_CONTENT_UNITS" else 0),
        "dropped_content_unit_ids": ([] if provenance_basis == "SECTION_DIR_CONTENT_UNITS"
                                     else sorted(set(previously) - set(planned))),
        "cu_structures_stripped": base.get("cu_structures_stripped") or {},
        "structure_basis": structure_basis,
        "rendered_chars": {"before": before_chars, "after": after_chars},
        "normalized_after_rebuild": normalize is not None,
        "rebuilt_at": _now(),
    }
    atomic_write_json(fragment_path, rebuilt)
    return {"rebuilt": True, "content_units_assembled": len(rows),
            "missing_content_units": len(missing),
            "provenance_basis": provenance_basis,
            "structure_basis": structure_basis,
            "rendered_chars": {"before": before_chars, "after": after_chars},
            "cu_structures_stripped": base.get("cu_structures_stripped") or {}}


def execute_reduction_plan(run_dir: Path, plan: dict[str, Any], *,
                           contracts: dict[str, Any] | None = None,
                           ledger: dict[str, Any] | None = None,
                           normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
                           chars_per_technical_page: float = RENDER_VISIBLE_CHARS_PER_TECHNICAL_PAGE
                           ) -> dict[str, Any]:
    """把 §16 的削减计划真的执行一遍：改 CU、重建节正文、重记账，逐条留审计。"""
    run_dir = Path(run_dir)
    contracts = contracts or {}
    ledger_before = ledger or read_ledger(run_dir / BUDGET_LEDGER_FILE)
    records = {str(row.get("content_unit_id")): row
               for row in ledger_before.get("records") or [] if row.get("content_unit_id")}
    executed: list[dict[str, Any]] = []
    touched: set[str] = set()
    pending_caps: dict[str, int] = {}
    for action in plan.get("actions") or []:
        cu_id = str(action.get("content_unit_id") or "")
        sid = str(action.get("section_id") or "")
        record = records.get(cu_id) or {}
        contract = contracts.get(cu_id) or {}
        row: dict[str, Any] = {
            "content_unit_id": cu_id,
            "section_id": sid,
            "archetype": str(action.get("archetype") or record.get("archetype") or "NARRATIVE"),
            "reason": str(plan.get("trigger") or "OVERSHOOT_TARGETED_REDUCTION"),
            "plan_action": str(action.get("action") or ""),
            "protected_requirements": sorted({str(item) for item in
                                              (contract.get("source_requirements")
                                               or contract.get("requirement_ids") or [])}),
            "protected_scoring_items": sorted({str(item) for item in
                                               (contract.get("source_scoring_items")
                                                or contract.get("scoring_item_ids") or [])}),
            "protected": bool(action.get("protected") or record.get("protected")),
            "before_chars": 0, "after_chars": 0, "removed_chars": 0,
        }
        if action.get("action") == PENDING_ACTION:
            # 未生成的单元没有正文可削；真实动作是把下一批的每单元上限压到额度下限，
            # 由调用方交给 `factory.budget_unit_caps`（§14 已经存在的那条杠杆）。
            floor = int(budget_of(contract)["min"]) if contract else 0
            row["execution_status"] = SKIPPED_NOT_GENERATED
            row["pending_target_chars_cap"] = floor
            if floor:
                pending_caps[sid] = min(int(pending_caps.get(sid) or floor), floor)
            executed.append(row)
            continue
        allowance = _allowance(record, contract)
        if not allowance:
            row["execution_status"] = SKIPPED_NOT_PERSISTED
            executed.append(row)
            continue
        row.update(reduce_content_unit(
            run_dir / "longform" / "word" / "sections" / sid / "cus" / f"{cu_id}.json",
            contract=contract, record=record, allowance_chars=allowance, reason=row["reason"]))
        row["removed_chars"] = int(row.get("removed_chars") or 0)
        if row["removed_chars"]:
            touched.add(sid)
        executed.append(row)

    rebuilds = {sid: rebuild_section_fragment(run_dir / "longform" / "word" / "sections" / sid,
                                              normalize=normalize)
                for sid in sorted(touched)}

    # 重记账：削减后的 CU 用同一本账重判一次，否则会出现"账上仍超写、正文已削完"。
    refreshed: list[dict[str, Any]] = []
    for sid in sorted(touched):
        cus_dir = run_dir / "longform" / "word" / "sections" / sid / "cus"
        for path in sorted(cus_dir.glob("CU-*.json")) if cus_dir.is_dir() else []:
            row = _read_json(path)
            if isinstance(row, dict) and isinstance(row.get("budget"), dict) \
                    and row["budget"].get("content_unit_id"):
                refreshed.append(row["budget"])
    ledger_path = run_dir / BUDGET_LEDGER_FILE
    if refreshed:
        write_ledger(ledger_path, merge_ledger(read_ledger(ledger_path), refreshed))
    totals_after = (read_ledger(ledger_path).get("totals") or {}) if refreshed \
        else (ledger_before.get("totals") or {})

    removed_chars = sum(int(row.get("removed_chars") or 0) for row in executed)
    artifact_removed = artifact_chars_from_ledger(removed_chars)
    excess_ledger = int(plan.get("excess_ledger_chars") or plan.get("excess_chars") or 0)
    per_page = float(chars_per_technical_page or RENDER_VISIBLE_CHARS_PER_TECHNICAL_PAGE)
    report = {
        "schema_version": EXECUTION_SCHEMA,
        "status": ("EXECUTED" if removed_chars else "NOTHING_REDUCIBLE"),
        "plan_status": plan.get("status"),
        "plan_projection_basis": plan.get("projection_basis"),
        "trigger": plan.get("trigger"),
        "provider_called": False,
        "compression_pipeline_called": False,
        "granularity": "SECTION_AND_CONTENT_UNIT",
        "actions_planned": len(plan.get("actions") or []),
        "actions_executed": sum(1 for row in executed if row.get("execution_status") == EXECUTED),
        "actions_skipped_protected": sum(1 for row in executed
                                         if row.get("execution_status") == SKIPPED_PROTECTED),
        "actions_skipped_other": sum(1 for row in executed
                                     if str(row.get("execution_status") or "").startswith("SKIPPED")
                                     and row.get("execution_status") != SKIPPED_PROTECTED),
        "actions_no_reduction_possible": sum(1 for row in executed
                                             if row.get("execution_status") == NO_REDUCTION_POSSIBLE),
        "ledger_chars_removed": removed_chars,
        "artifact_chars_removed_estimate": int(round(artifact_removed)),
        "artifact_char_yield": ARTIFACT_CHARS_PER_LEDGER_CHAR_RUN_D,
        "chars_per_technical_page": per_page,
        "expected_page_reduction": (round(artifact_removed / per_page, 2) if per_page else None),
        "excess_ledger_chars": excess_ledger,
        "coverage_of_excess": (round(min(1.0, removed_chars / excess_ledger), 4)
                               if excess_ledger else None),
        "residual_excess_ledger_chars": max(0, excess_ledger - removed_chars),
        "pending_section_caps": pending_caps,
        "sections_rebuilt": rebuilds,
        "local_text_pipeline_applied": normalize is not None,
        "budget_ledger_before": {key: int((ledger_before.get("totals") or {}).get(key) or 0)
                                 for key in ("actual_chars", "prose_chars", "structure_chars",
                                             "over_budget_units", "overshoot_chars")},
        "budget_ledger_after": {key: int(totals_after.get(key) or 0)
                                for key in ("actual_chars", "prose_chars", "structure_chars",
                                            "over_budget_units", "overshoot_chars")},
        "actions": executed,
        "policy": "LOCAL_ONLY_NO_PROVIDER_NO_FULL_DOCUMENT_COMPRESSION",
        "protection_basis": ("CU_LEVEL_PROTECTED_FLAG + SENTENCE_LEVEL_MUST_MARKER"
                             " + STRUCTURE_UNIT_LEVEL_MUST_MARKER"
                             " + STRUCTURE_MIN_GEOMETRY_FLOOR"),
        "reduction_currency": "LEDGER_CHARS (CU 正文+payload)；折算产物可见字用账本→产物系数",
        "executed_at": _now(),
    }
    atomic_write_json(run_dir / EXECUTION_FILE, report)
    return report
