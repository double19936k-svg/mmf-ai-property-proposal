"""MMF-007C Round 3 / P0-1：Content Unit 级预算账本与超页控制（§17–§22）。

设计约束（来自指令，不得越界）：
- Provider 返回即记账：超过 CU 上限只 **记录** OVERSHOOT，不当场删字（§19）；
- 再平衡只作用于**尚未生成**的 Content Unit，已写出的正文不被静默改写（§20）；
- 超页削减不得是黑盒压缩器：逐条给出理由、保护类不得动（关键需求应答、评分应答、
  SOP、技术表格、关键流程），且必须自检"核心零损失"（§21/§22）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .content_padding import measure_payload
from .page_calibration import project_pages
from .unified_budget import ARTIFACT_CHARS_PER_LEDGER_CHAR_RUN_D

BUDGET_LEDGER_VERSION = "content-unit-budget-ledger-v0.1"
REDUCTION_PLAN_VERSION = "overshoot-reduction-plan-v0.1"
# 运行目录里的落盘名。不用 11_/12_ 前缀：那几个号位已被 volume plan、合规矩阵、
# 资信台账占用，编号只表达链路顺序，新账本硬插一个号位会让两边都对不上。
BUDGET_LEDGER_FILE = "content_unit_budget_ledger.json"
REDUCTION_PLAN_FILE = "overshoot_reduction_plan.json"
REBALANCE_FILE = "budget_rebalancing.json"

# §22 保护类：这些内容单元永不被削减计划点名删除。
PROTECTED_ARCHETYPES = {
    "SOP", "PROCESS", "EMERGENCY_PLAYBOOK", "STANDARD_TABLE", "RISK_REGISTER",
    "KPI_TABLE", "RESPONSIBILITY_MATRIX", "MATRIX", "SCHEDULE",
}
# 已生成单元的处置建议：只登记，不改写。
ARRIVED_ACTION = "REVIEW_CONDENSE_CANDIDATE"
PENDING_ACTION = "TARGET_REDUCED"

WITHIN_BUDGET = "WITHIN_BUDGET"
OVER_TARGET = "OVER_TARGET"
OVER_BUDGET = "OVER_BUDGET"
UNDER_BUDGET = "UNDER_BUDGET"


def _budget_chars(contract: dict[str, Any], key: str, default: int) -> int:
    value = (contract or {}).get(key)
    if isinstance(value, dict):
        for field in ("effective_chars", "chars", "words"):
            if value.get(field) is not None:
                return int(value[field] or 0)
        return default
    if isinstance(value, (int, float)):
        return int(value)
    return default


def budget_of(contract: dict[str, Any]) -> dict[str, int]:
    """取一条 CU 契约的 min/target/max 字数（缺字段时按 planned_words 派生）。"""
    planned = _budget_chars(contract, "planned_words", 0)
    target = _budget_chars(contract, "target_budget", planned) or planned
    minimum = _budget_chars(contract, "min_budget", int(target * 0.8)) or int(target * 0.8)
    maximum = _budget_chars(contract, "max_budget", int(target * 1.2)) or int(target * 1.2)
    return {"min": max(1, minimum), "target": max(1, target), "max": max(maximum, target)}


def visible_chars_of(payload: Any) -> int:
    if isinstance(payload, str):
        return len(re.sub(r"\s+", "", payload))
    if isinstance(payload, dict):
        return sum(visible_chars_of(value) for value in payload.values())
    if isinstance(payload, list):
        return sum(visible_chars_of(value) for value in payload)
    return 0


def judge_content_unit(contract: dict[str, Any], *, content: Any = None,
                       chars: int | None = None) -> dict[str, Any]:
    """Provider 返回后立即判定预算档位（只记账，不改正文）。

    同时记一条 §45 的低信息增量占比：重复检测看不见"任何项目都能照抄"的句子，
    削减计划要按信息增量而不是只按字数挑候选，所以这一维必须在返回当场、在正文还在
    手里的时候量。纯本地计算，不多花一次 Provider 调用。
    """
    budget = budget_of(contract)
    payload = content.get("content") if isinstance(content, dict) else content
    structure = content.get("payload") if isinstance(content, dict) else None
    # Structured envelopes can echo prose in their payload. Count the same
    # rendered prose once across transports; preserve distinct structural text
    # and leave the original provider payload unchanged for audit.
    if isinstance(structure, dict) and isinstance(payload, str) and payload:
        structure = {key: value for key, value in structure.items()
                     if not (key in {"content", "text", "summary"}
                             and isinstance(value, str) and value.strip() == payload.strip())}
    structure_chars = visible_chars_of(structure)
    actual = int(chars) if chars is not None else (
        visible_chars_of(payload) + structure_chars)
    # §14 的生成中账要分"已用 narrative / 已用 table"，所以正文与结构两路各自也要留下。
    # 外部只给总量时不知道拆分，差额按正文记账（宁可高估正文，也不要把表格算多）。
    prose_chars = max(0, actual - structure_chars)
    padding = measure_payload(content)
    ratio = round(actual / budget["target"], 3) if budget["target"] else None
    if actual > budget["max"]:
        status = OVER_BUDGET
    elif actual > budget["target"]:
        status = OVER_TARGET
    elif actual < budget["min"]:
        status = UNDER_BUDGET
    else:
        status = WITHIN_BUDGET
    return {
        "content_unit_id": str(contract.get("content_unit_id") or contract.get("cu_id") or ""),
        "section_id": str(contract.get("parent_section_id") or contract.get("section_id") or ""),
        "archetype": str(contract.get("archetype") or "NARRATIVE"),
        "actual_chars": actual,
        "prose_chars": prose_chars,
        "structure_chars": structure_chars,
        "budget_min": budget["min"],
        "budget_target": budget["target"],
        "budget_max": budget["max"],
        "budget_ratio": ratio,
        "budget_status": status,
        "overshoot_chars": max(0, actual - budget["max"]),
        # §45：低增量占比。没量到正文时记 0 条并保留 measured_items=0，
        # 让"没量"与"量了没有注水"在账本里是两件事。
        "padding_measured_items": padding["items"],
        "low_gain_items": padding["low_gain_items"],
        "low_gain_chars": padding["low_gain_chars"],
        "padding_item_share": padding["padding_item_share"],
        "padding_char_share": padding["padding_char_share"],
        "protected": bool(
            (contract.get("archetype") in PROTECTED_ARCHETYPES)
            or (contract.get("source_scoring_items") or contract.get("scoring_item_ids"))
            or (contract.get("source_requirements") or contract.get("requirement_ids"))),
    }


def merge_ledger(existing: dict[str, Any] | None, records: list[dict[str, Any]]
                 ) -> dict[str, Any]:
    """按 content_unit_id 合并账本：同一单元重生成后以最新一次为准，历史次数保留。"""
    rows = {str(row.get("content_unit_id")): dict(row)
            for row in (existing or {}).get("records") or [] if row.get("content_unit_id")}
    for record in records or []:
        key = str(record.get("content_unit_id") or "")
        if not key:
            continue
        prior = rows.get(key) or {}
        merged = dict(record)
        merged["attempt_count"] = int(prior.get("attempt_count") or 0) + 1
        rows[key] = merged
    values = [rows[key] for key in sorted(rows)]
    arrived = sum(1 for row in values if row.get("actual_chars"))
    return {
        "schema_version": BUDGET_LEDGER_VERSION,
        "records": values,
        "totals": {
            "content_units_recorded": len(values),
            "content_units_arrived": arrived,
            "over_budget_units": sum(1 for row in values if row.get("budget_status") == OVER_BUDGET),
            "over_target_units": sum(1 for row in values if row.get("budget_status") in (OVER_TARGET, OVER_BUDGET)),
            "under_budget_units": sum(1 for row in values if row.get("budget_status") == UNDER_BUDGET),
            "actual_chars": sum(int(row.get("actual_chars") or 0) for row in values),
            # §14：文档级的"已用正文 / 已用结构"两条余量要从同一本账算出来，
            # 否则"表格在额度之外长字"这一路又只能事后靠渲染反推。
            "prose_chars": sum(int(row.get("prose_chars") or 0) for row in values),
            "structure_chars": sum(int(row.get("structure_chars") or 0) for row in values),
            "target_chars": sum(int(row.get("budget_target") or 0) for row in values),
            "max_chars": sum(int(row.get("budget_max") or 0) for row in values),
            "overshoot_chars": sum(int(row.get("overshoot_chars") or 0) for row in values),
            # §45：低增量汇总。分母是"量到的句子条数"（padding_measured_items），不是 CU 数 ——
            # 一个 CU 里有几十条句子，按 CU 摊会把比例稀释掉。
            "padding_measured_items": sum(int(row.get("padding_measured_items") or 0) for row in values),
            "low_gain_items": sum(int(row.get("low_gain_items") or 0) for row in values),
            "low_gain_chars": sum(int(row.get("low_gain_chars") or 0) for row in values),
        },
    }


def observed_inflation(ledger: dict[str, Any] | None) -> float:
    """实测超写系数：实际字数 / 计划目标字数。Run C 真实值约 1.65。"""
    totals = (ledger or {}).get("totals") or {}
    target = int(totals.get("target_chars") or 0)
    actual = int(totals.get("actual_chars") or 0)
    if not target or not actual:
        return 1.0
    return max(0.2, round(actual / target, 3))


def rebalance_pending_targets(content_units: list[dict[str, Any]], ledger: dict[str, Any] | None,
                              *, budget_chars_target: int, inflation: float | None = None
                              ) -> dict[str, Any]:
    """动态再平衡：把剩余预算按实测系数摊到**未生成**的 Content Unit 上。

    已生成单元不改写；未生成单元的新目标 = 原目标 × 缩放系数，并被 min/max 夹住。
    """
    factor = float(inflation if inflation is not None else observed_inflation(ledger))
    written = {str(row.get("content_unit_id"))
               for row in (ledger or {}).get("records") or [] if row.get("actual_chars")}
    pending = [unit for unit in content_units or []
               if str(unit.get("content_unit_id")) not in written]
    arrived_chars = sum(int(row.get("actual_chars") or 0)
                        for row in (ledger or {}).get("records") or [] if row.get("actual_chars"))
    remaining_budget = max(0, int(budget_chars_target) - arrived_chars)
    pending_target = sum(budget_of(unit)["target"] for unit in pending)
    # Provider 会按 factor 超写，所以未生成单元的"目标之和"只允许占剩余预算/factor；
    # 否则 arrived + 新目标×factor 会再次冲破文档目标（早期版本在这里多乘了一次 factor）。
    scaled_pool = pending_target * factor
    shrink = round(remaining_budget / scaled_pool, 3) if scaled_pool > 0 else 1.0
    adjustments: list[dict[str, Any]] = []
    projected_target = 0
    floor_limited = 0
    for unit in pending:
        budget = budget_of(unit)
        proposed = int(round(budget["target"] * shrink))
        new_target = min(budget["max"], max(budget["min"], proposed))
        if new_target > proposed:
            floor_limited += 1
        projected_target += new_target
        if new_target == budget["target"]:
            continue
        adjustments.append({
            "content_unit_id": str(unit.get("content_unit_id")),
            "section_id": str(unit.get("section_id") or unit.get("parent_section_id") or ""),
            "target_before": budget["target"],
            "target_after": new_target,
            "reason": ("PROJECTED_PAGE_CEILING_SHRINK" if shrink < 1.0 else "PROJECTED_PAGE_FLOOR_RAISE")
            + f"（实测超写系数 {factor:.2f}，缩放 {shrink:.2f}）",
        })
    return {
        "schema_version": BUDGET_LEDGER_VERSION,
        "observed_inflation_factor": factor,
        "arrived_chars": arrived_chars,
        "budget_chars_target": int(budget_chars_target),
        "remaining_budget_chars": remaining_budget,
        "pending_units": len(pending),
        "pending_target_chars_before": pending_target,
        "pending_target_chars_after": projected_target,
        # 自检：再平衡后按同一实测系数投影，必须落回文档目标附近，否则这个数就是空谈。
        "projected_total_chars": arrived_chars + int(round(projected_target * factor)),
        "shrink_factor": shrink,
        # §20 的诚实边界：单元还有字数下限（min_budget），压不动时残余超写必须显式报出来，
        # 让调用方知道"剩下的缺口只能靠减少单元数（定向削减）解决"，而不是假装摊平了。
        "floor_limited_units": floor_limited,
        "residual_overrun_chars": max(0, arrived_chars + int(round(projected_target * factor))
                                      - int(budget_chars_target)),
        "adjustments": adjustments,
        "adjusted_units": len(adjustments),
        "written_units_untouched": len(written),
    }


def apply_rebalanced_targets(contracts: Any, rebalance: dict[str, Any]) -> dict[str, Any]:
    """把再平衡结果写回**未生成**单元的契约（§20）。

    契约是 Provider 请求的直接来源，所以只有写进 `target_budget`/`planned_words` 才算
    真的改了目标；旧值留在 `budget_rebalanced.target_before` 里供审计。已生成单元不在
    adjustments 里（rebalance_pending_targets 已经排除），这里也不做二次改写。
    """
    rows = list(contracts.values() if isinstance(contracts, dict) else (contracts or []))
    by_id = {str(row.get("content_unit_id")): row for row in rows if row.get("content_unit_id")}
    applied: list[dict[str, Any]] = []
    for adjustment in rebalance.get("adjustments") or []:
        cu_id = str(adjustment.get("content_unit_id") or "")
        contract = by_id.get(cu_id)
        target = int(adjustment.get("target_after") or 0)
        if contract is None or target <= 0:
            continue
        before = budget_of(contract)["target"]
        if before == target:
            continue
        contract["planned_words"] = target
        current = contract.get("target_budget")
        if isinstance(current, dict):
            current["effective_chars"] = target
        else:
            contract["target_budget"] = {"measure": "effective_chars", "effective_chars": target}
        ceiling = contract.get("max_budget")
        if isinstance(ceiling, dict) and int(ceiling.get("effective_chars") or 0) < target:
            # 目标抬高后上限必须跟着抬，否则契约自己前后矛盾。
            ceiling["effective_chars"] = target
        floor = contract.get("min_budget")
        if isinstance(floor, dict) and int(floor.get("effective_chars") or 0) > target:
            floor["effective_chars"] = target
        contract["budget_rebalanced"] = {
            "target_before": before,
            "target_after": target,
            "reason": str(adjustment.get("reason") or ""),
            "observed_inflation_factor": rebalance.get("observed_inflation_factor"),
        }
        applied.append({"content_unit_id": cu_id, "target_before": before, "target_after": target})
    return {
        "applied_units": len(applied),
        "contracts_available": len(by_id),
        "adjustments_offered": len(rebalance.get("adjustments") or []),
        "applied": applied,
    }


def targeted_reduction_plan(*, ledger: dict[str, Any] | None, content_units: list[dict[str, Any]],
                            page_targets: tuple[int, int], ruler: dict[str, Any] | None,
                            measured_technical_pages: int = 0
                            ) -> dict[str, Any]:
    """超页时的定向削减计划：只动未生成单元的目标与已登记的候选，保护类不动。"""
    records = [row for row in (ledger or {}).get("records") or [] if row.get("content_unit_id")]
    arrived_chars = sum(int(row.get("actual_chars") or 0) for row in records)
    projection = project_pages(arrived_chars, ruler=ruler)
    # 有真实渲染就先用渲染页数：字符投影只是没有分页器时的退路，两条口径不得混用。
    projection_pages = int(measured_technical_pages or 0)
    projection_basis = "MEASURED_RENDER" if projection_pages else projection["basis"]
    if not projection_pages:
        projection_pages = projection["projected_technical_effective_pages"]
    ceiling = int(page_targets[1] or 0)
    over_pages = (max(0, projection_pages - ceiling)
                  if ceiling > 0 else 0)  # 没有上限就没有"超页"可言，不得凭空造出削减计划
    per_page = float((ruler or {}).get("chars_per_technical_page") or 900) or 900.0
    excess_chars = int(round(over_pages * per_page))
    # §7 的"一种货币"在这里也必须成立：候选的可减字数是**账本字**（CU 正文 + payload），
    # 而 超页数 × 每页可见字 是**产物可见字**。直接比会把缺口少算约 1/0.7707 − 1 ≈ 30 %，
    # 于是"计划已覆盖缺口"是量出来的假话。换算系数带 provenance，不另造尺子。
    excess_ledger_chars = int(round(excess_chars / ARTIFACT_CHARS_PER_LEDGER_CHAR_RUN_D))

    written = {str(row.get("content_unit_id")) for row in records if row.get("actual_chars")}
    candidates_pending = [
        {
            "content_unit_id": str(unit.get("content_unit_id")),
            "section_id": str(unit.get("section_id") or unit.get("parent_section_id") or ""),
            "archetype": str(unit.get("archetype") or "NARRATIVE"),
            "action": PENDING_ACTION,
            "reducible_chars": max(0, budget_of(unit)["target"] - budget_of(unit)["min"]),
            "protected": bool(
                (unit.get("archetype") in PROTECTED_ARCHETYPES)
                or (unit.get("source_scoring_items") or unit.get("scoring_item_ids"))
                or (unit.get("source_requirements") or unit.get("requirement_ids"))),
        }
        for unit in content_units or [] if str(unit.get("content_unit_id")) not in written
    ]
    candidates_arrived = [
        {
            "content_unit_id": str(row.get("content_unit_id")),
            "section_id": str(row.get("section_id") or ""),
            "archetype": str(row.get("archetype") or "NARRATIVE"),
            "action": ARRIVED_ACTION,
            "reducible_chars": int(row.get("overshoot_chars") or 0),
            "padding_item_share": float(row.get("padding_item_share") or 0.0),
            "low_gain_items": int(row.get("low_gain_items") or 0),
            "protected": bool(row.get("protected")),
        }
        for row in records if int(row.get("overshoot_chars") or 0) > 0
    ]
    for row in candidates_pending:
        row.setdefault("padding_item_share", 0.0)
        row.setdefault("low_gain_items", 0)
    usable = [row for row in candidates_pending + candidates_arrived
              if not row["protected"] and row["reducible_chars"] > 0]
    # §22/§45：削减顺序按"信息增量最低"，字数只是同档内的次级排序。
    # 未生成的 CU 还没正文可量，padding_item_share 记 0，因此排在已量到低增量最高者之后 ——
    # 这符合指令：先动"写出来但没信息"的，再动"还没写"的额度。
    usable.sort(key=lambda row: (-row["padding_item_share"], -row["reducible_chars"]))
    selected: list[dict[str, Any]] = []
    covered = 0
    for row in usable:
        if covered >= excess_ledger_chars:
            break
        selected.append(row)
        covered += row["reducible_chars"]
    return {
        "schema_version": REDUCTION_PLAN_VERSION,
        "status": ("NOT_REQUIRED" if over_pages <= 0 else
                   ("TARGETED_REDUCTION_PLANNED" if covered >= excess_ledger_chars
                    else "REDUCTION_INSUFFICIENT")),
        "trigger": (None if over_pages <= 0 else
                    f"技术有效页 {projection_pages} > 上限 {ceiling}"),
        "projected_technical_effective_pages": projection_pages,
        "page_ceiling": ceiling,
        "projection_basis": projection_basis,
        "over_pages": over_pages,
        "excess_chars": excess_chars,
        "excess_chars_currency": "RENDER_VISIBLE_CHARS",
        "excess_ledger_chars": excess_ledger_chars,
        "excess_ledger_chars_currency": "LEDGER_CHARS",
        "covered_chars": covered,
        "uncovered_chars": max(0, excess_ledger_chars - covered),
        "actions": selected,
        "protected_units_skipped": sum(1 for row in candidates_pending + candidates_arrived if row["protected"]),
        "deleted_content_units": [],
        "content_written_by_provider": len(written),
        "policy": "NEVER_DELETE_SCORING_RESPONSE_MUST_REQUIREMENT_SOP_TECHNICAL_TABLE_KEY_PROCESS",
        # §22/§45：挑候选的顺序单独报出来，让人能看出"先删的是信息增量最低的"，
        # 而不是只看字数最多的。
        "reduction_order_basis": "LOWEST_INFORMATION_INCREMENT_FIRST_THEN_LARGEST_OVERSHOOT",
        "low_gain_candidates": sum(1 for row in selected if row["padding_item_share"] > 0),
    }


def read_ledger(path: Path) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        return {"schema_version": BUDGET_LEDGER_VERSION, "records": []}
    try:
        payload = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"schema_version": BUDGET_LEDGER_VERSION, "records": []}
    return payload if isinstance(payload, dict) else {"schema_version": BUDGET_LEDGER_VERSION, "records": []}


def write_ledger(path: Path, ledger: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target
