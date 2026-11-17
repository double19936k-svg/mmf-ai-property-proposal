"""MMF-007C Round 4 · P0-A 统一内容预算（§7–§15）。

全部离线：只调用规划层纯函数、Synthetic Fixture 与 MockProvider 构造的工厂实例，
不发起任何 Provider 调用（§1）。文件里每个 `provider_calls_made == 0` 的断言都是
这条限制的可核对证据，不是叙述。
"""
from __future__ import annotations

import copy
import json
import sys
import threading
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
APP = APP_ROOT / "app"
for _path in (str(APP), str(APP_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from longform.cu_state import depth_ratio
from longform.factory import LongformGenerationFactory, write_json
from planning.budget_control import (
    BUDGET_LEDGER_FILE,
    judge_content_unit,
    merge_ledger,
    read_ledger,
    write_ledger,
)
from planning.hierarchy import attach_hierarchy_artifacts, build_document_hierarchy
from planning.planner import build_planning_bundle
from planning.unified_budget import (
    ARTIFACT_CHARS_PER_LEDGER_CHAR_RUN_D,
    BUDGET_GATE_FILE,
    LEDGER_CHARS_PER_TECHNICAL_PAGE,
    MIDRUN_BUDGET_FILE,
    STRUCTURED_SHARE_BAND,
    PAGE_PROJECTION_BASIS,
    TABLE_LIKE_ARCHETYPES,
    artifact_chars_from_ledger,
    build_budget_tree,
    cap_structure_share,
    conservation_report,
    contract_budget_row,
    empty_midrun_state,
    mid_run_reallocation,
    observe_batch,
    pages_from_ledger_chars,
    plan_section_budget,
    pre_generation_budget_gate,
    section_budget_split,
    structure_cost_for_units,
    unified_unit_budget,
)
from providers.mock import MockProvider

from test_mmf007a_hierarchy import _pack

PAGE_TARGETS = (250, 320)
# §13 的"通过样本"必须按真实密度排：每条约 1000 字、每五个单元两个结构化 —— 这才是
# 层级实际会排出来的组合。两条巨型 CU 的几何会被 MAX_UNITS 夹平（结构占比掉到 1.5%），
# 那份计划的形状就和"带内"要测的路径不是同一件事了 —— 夹平之后它测的是另一条分支。
_IN_BAND_MIX = (["NARRATIVE"] * 3 + ["STANDARD_TABLE", "SOP"]) * 6
# "带内样本"的字数必须按**当前页尺**重新推导，不能沿用上一轮的数：
#   投影 = 计划 × (1 − 机动额度占比 0.0751) ÷ 930 ≥ 250  ⇒ 每节 ≥ 28,036
#   最坏 = 计划 ÷ 930 ≤ 320 × 0.95（贴沿余量）           ⇒ 每节 ≤ 31,413
# 取 31,000（9 节 = 279,000）：投影 276 页、最坏 300 页，两端都在带内且不贴上限。
# 这一档跟着尺子走过三次：1,062.2→30,000、1,183→38,000、R41 修复后 930→31,000。
# 沿用旧数会把"带内"测成 `BLOCKED_OVER_CEILING` —— 那是尺子的账，不是计划的账。
_IN_BAND_SPECS = [(f"S-{index:02d}", _IN_BAND_MIX, 31000) for index in range(1, 10)]


# ------------------------------------------------------------- fixtures --
def _cu_contract(sid: str, index: int, archetype: str, chars: int) -> dict:
    """一条与 hierarchy 同形状的 CU 契约（额度由产品函数算出，不手填）。"""
    budget = unified_unit_budget(archetype, chars)
    target = contract_budget_row(archetype, budget)
    return {
        "content_unit_id": f"CU-{sid}-{index:02d}",
        "section_id": sid,
        "parent_section_id": sid,
        "unit_index": index,
        "archetype": archetype,
        "planned_words": target["effective_chars"],
        "unified_budget": budget,
        "min_budget": contract_budget_row(archetype, unified_unit_budget(archetype, int(chars * 0.8))),
        "target_budget": target,
        "max_budget": contract_budget_row(archetype, unified_unit_budget(archetype, int(chars * 1.2))),
        "source_requirements": [],
        "source_scoring_items": [],
    }


def _planned_units(sid: str, archetypes, chars: int) -> list[dict]:
    """一条节的 CU 契约，额度由 `plan_section_budget` 算出（与下面的树同一组数）。"""
    plan = plan_section_budget(chars, archetypes)
    return [_cu_contract(sid, index, archetype, row["effective_chars"])
            for index, (archetype, row) in enumerate(zip(archetypes, plan["unit_budgets"]), start=1)]


def _planned_tree(section_specs, *, document_target=None, page_targets=PAGE_TARGETS):
    """section_specs: [(section_id, [archetype, ...], section_chars)] → 五层统一预算树。"""
    sections = []
    for sid, archetypes, chars in section_specs:
        units = _planned_units(sid, archetypes, chars)
        sections.append({"section_id": sid, "planned_total_chars": chars, "content_units": units})
    total = sum(int(row["planned_total_chars"]) for row in sections)
    return build_budget_tree(
        document_target=document_target if document_target is not None else total,
        volumes=[{"volume_id": "V01", "sections": sections}],
        page_targets=page_targets)


def _factory(tmp: Path, *, cu_contracts=None, budget_tree=None) -> LongformGenerationFactory:
    return LongformGenerationFactory(
        run_root=tmp,
        provider=MockProvider({"provider_name": "mock", "provider_type": "mock",
                               "enabled": True, "model": "mock"}),
        provider_name="mock",
        inputs={
            "word_plan": {},
            "requirement_matrix": {},
            "section_contracts": {"contracts": [
                {"section_id": sid, "weight": 0.4 if sid == "S-A" else 0.1}
                for sid in ("S-A", "S-B", "S-C", "S-D")]},
            "global_state": {
                "canonical_terms": {}, "client_requirements": [], "project_facts": {},
                "staffing": {}, "service_hours": {}, "sla_kpi": {}, "service_scope": {},
                "excluded_scope": [], "commitment_registry": [], "topic_ownership": [],
                "canonical_roles": {}},
            "ppt_plan": {},
            "dependency_map": {"dependencies": []},
            "knowledge_selection": {"knowledge_usage_contracts": []},
            "cu_contracts": cu_contracts or {},
            "budget_tree": budget_tree or {},
            "require_section_min": False,
        },
    )


# ------------------------------------------------ §7/§9 结构计入 CU 额度 --
def test_table_structure_is_deducted_from_the_unit_allowance() -> None:
    """§7/§9：表体字数从这一条 CU 的额度里扣，不是额度之外长出来的东西。"""
    row = unified_unit_budget("STANDARD_TABLE", 8000)
    assert row["measure"] == "unified_effective_chars"
    assert row["prose_chars"] + row["structure_chars"] == row["effective_chars"]
    assert 0 < row["structure_chars"] < row["effective_chars"]
    # 几何与额度必须自洽：排出来的行 × 单行成本 == 记在额度里的结构成本
    assert row["structure_chars"] == structure_cost_for_units(
        "STANDARD_TABLE", row["geometry"]["units"], columns=row["geometry"]["columns"] or None)
    assert row["estimated_char_cost"] == row["structure_chars"]
    assert row["estimated_page_cost"] == round(
        row["structure_chars"] / LEDGER_CHARS_PER_TECHNICAL_PAGE, 3)


def test_narrative_unit_carries_zero_structure_cost() -> None:
    row = unified_unit_budget("NARRATIVE", 5000)
    assert row["structure_chars"] == 0
    assert row["prose_chars"] == row["effective_chars"] == 5000
    assert row["estimated_char_cost"] == 0
    assert row["geometry"]["kind"] == "prose"


def test_every_table_carries_role_and_page_cost() -> None:
    """§9：每张表都带 estimated_char_cost / estimated_page_cost / information_role。"""
    for archetype in TABLE_LIKE_ARCHETYPES:
        budget = unified_unit_budget(archetype, 4000)
        row = contract_budget_row(archetype, budget)
        assert row["estimated_char_cost"] == budget["structure_chars"] > 0
        assert row["estimated_page_cost"] > 0
        assert row["information_role"] in {"SUMMARY_TABLE", "CONTROL_MATRIX", "PROCESS_TABLE"}
        assert row["rows"] >= 3 and row["columns"] >= 1
        # 每格下限从实测行成本推出：行数砍到底也不许留 §63 那种空壳格
        assert row["min_cell_chars"] >= 12
        assert row["prose_chars"] + row["structure_chars"] == row["effective_chars"]


# --------------------------------------- §8/§10/§11 Section 级切分与守恒 --
def test_section_plan_conserves_units_plus_reserve() -> None:
    archetypes = ["NARRATIVE", "STANDARD_TABLE", "SOP", "CHECKLIST", "FORM_TEMPLATE"]
    plan = plan_section_budget(40000, archetypes)
    rows = plan["unit_budgets"]
    assert len(rows) == len(archetypes)
    for row in rows:
        assert row["prose_chars"] + row["structure_chars"] == row["effective_chars"]
    # 守恒是构造出来的：CU 合计 + 机动额度 == 该节契约额度，一条字都不许丢在层间
    assert sum(row["effective_chars"] for row in rows) + plan["reserve_budget"] == 40000
    split = section_budget_split(40000, rows)
    assert (split["narrative_budget"] + split["structured_budget"]
            + split["reserve_budget"]) == 40000
    assert split["reserve_share_in_band"] is True
    assert split["split_basis"] == "DERIVED_FROM_PLANNED_ARCHETYPE_MIX"


def test_structure_share_is_capped_and_returns_chars_to_prose() -> None:
    """§10：结构额度超出参考带上沿要扣回，差额回吐正文面而不是丢掉。"""
    archetypes = ["STANDARD_TABLE"] * 10
    plan = plan_section_budget(12000, archetypes)
    rows = plan["unit_budgets"]
    cap = plan["structure_cap"]
    assert cap["capped"] is True, "十条表格 CU 的规划结构占比必然超过 0.35，必须触发扣减"
    assert cap["structure_chars_after_cap"] <= cap["structure_cap_chars"] + cap.get(
        "residual_over_cap_chars", 0)
    assert cap["prose_reclaimed_chars"] == (cap["structure_chars_before_cap"]
                                            - cap["structure_chars_after_cap"])
    split = section_budget_split(12000, rows)
    assert split["structured_share"] <= STRUCTURED_SHARE_BAND[1] + 1e-9
    # 页数尺子就标定在这条混合上：超带的结构必须扣，否则投影永远低于页带下沿
    assert sum(row["effective_chars"] for row in rows) + plan["reserve_budget"] == 12000


def test_uncapped_section_reports_capped_false() -> None:
    rows, cap = cap_structure_share(["NARRATIVE", "NARRATIVE"],
                                    [unified_unit_budget("NARRATIVE", 5000),
                                     unified_unit_budget("NARRATIVE", 5000)], 10000)
    assert cap["capped"] is False
    assert cap["prose_reclaimed_chars"] == 0
    assert [row["effective_chars"] for row in rows] == [5000, 5000]


def test_shares_follow_each_sections_own_archetype_mix() -> None:
    """§10：比例按该节 Archetype 动态决定，不是一组全局写死的数。"""
    prose_only = plan_section_budget(30000, ["NARRATIVE"] * 4)
    mixed = plan_section_budget(30000, ["STANDARD_TABLE", "FORM_TEMPLATE", "CHECKLIST", "SOP"])
    prose_split = section_budget_split(30000, prose_only["unit_budgets"])
    mixed_split = section_budget_split(30000, mixed["unit_budgets"])
    assert prose_split["structured_share"] == 0.0
    # 纯叙述节算不出 structured 是事实，不掰一个"看起来合规"的比例出来
    assert prose_split["structured_share_in_band"] is False
    assert mixed_split["structured_share"] > 0.0
    assert prose_split["narrative_share"] != mixed_split["narrative_share"]
    assert mixed_split["reserve_budget"] == prose_split["reserve_budget"]


# ---------------------------------------------------------- §8 五层守恒 --
def test_five_level_tree_conserves() -> None:
    tree = _planned_tree([("S-A", ["NARRATIVE", "STANDARD_TABLE"], 22000),
                          ("S-B", ["SOP", "CHECKLIST"], 16000)])
    conservation = tree["conservation"]
    assert tree["measure"] == "unified_effective_chars"
    assert tree["levels"] == ("Document", "Volume", "Section", "Content Archetype", "Content Unit")
    assert conservation["conserved"] is True, conservation["levels"]
    assert conservation["broken_levels"] == []
    assert conservation["worst_delta_chars"] <= conservation["tolerance_chars"]
    assert tree["planned_total_chars"] == tree["document_budget"] == 38000
    assert (tree["allocated_total_chars"] + tree["reserve_total_chars"]
            == tree["planned_total_chars"])
    assert sum(row["effective_chars"] for row in tree["archetype_budgets"]) == tree["allocated_total_chars"]


def test_broken_level_is_reported_not_masked() -> None:
    """§8：某一层对不上时必须点名是哪一层，不能把 validation 写成 PASS。"""
    tree = _planned_tree([("S-A", ["NARRATIVE", "STANDARD_TABLE"], 22000),
                          ("S-B", ["SOP", "CHECKLIST"], 16000)])
    # 只把一条节的 CU 额度抬高，Section 契约额度不动 → 只有该层会裂
    section = tree["volumes"][0]["sections"][0]
    unit = section["content_units"][0]
    unit["effective_chars"] = int(unit["effective_chars"]) + 9000
    broken = conservation_recheck(tree)
    assert broken["conserved"] is False
    assert broken["broken_levels"] == ["section_vs_allocated_plus_reserve"], broken["levels"]
    level = broken["levels"]["section_vs_allocated_plus_reserve"]
    assert level["delta"] > broken["tolerance_chars"]
    # 差额指得出来是哪一层丢的：Section 契约额度没有被抬高，多出来的是 CU 层
    assert level["allocated_plus_reserve"] > level["section_sum"]


def conservation_recheck(tree: dict) -> dict:
    """用产品函数原地重算五层核对（不复制一份算法，避免测试自己变成第二套口径）。"""
    roll: dict[str, dict[str, int]] = {}
    allocated = 0
    for section in tree["volumes"][0]["sections"]:
        for unit in section["content_units"]:
            roll.setdefault(unit["archetype"], {"effective_chars": 0})
            roll[unit["archetype"]]["effective_chars"] += int(unit["effective_chars"])
            allocated += int(unit["effective_chars"])
    return conservation_report(
        tree["document_budget"], tree["volumes"], roll, allocated,
        max(0, tree["planned_total_chars"] - allocated))


# ------------------------------------------------------- §13 生成前门控 --
def test_gate_passes_a_conserved_in_band_plan() -> None:
    tree = _planned_tree(_IN_BAND_SPECS)
    gate = pre_generation_budget_gate(tree, page_targets=PAGE_TARGETS)
    assert gate["status"] == "PASS", gate["blocked_reasons"]
    assert gate["may_enter_generation"] is True
    assert gate["provider_calls_made"] == 0
    assert gate["projection_method"] == PAGE_PROJECTION_BASIS
    assert gate["page_floor"] <= gate["projected_technical_effective_pages"] <= gate["page_ceiling"]
    assert gate["worst_case_technical_effective_pages"] <= gate["page_ceiling"]
    assert gate["at_ceiling_margin"] is False
    assert gate["planned_structure_share"] <= STRUCTURED_SHARE_BAND[1] + 1e-9
    assert gate["reallocation_demand"] == []
    # 两路收益投影必须与页尺子一致：最坏情况 = 已排 + 机动额度全花在正文上
    assert gate["planned_prose_chars"] + gate["planned_structure_chars"] == gate["allocated_total_chars"]
    assert gate["reserve_total_chars"] == gate["planned_total_chars"] - gate["allocated_total_chars"]
    assert gate["worst_case_technical_effective_pages"] >= gate["projected_technical_effective_pages"]
    # Round 4.1 §5/§6：判决必须走**直量尺**，两路收益只许留在交叉核对里。这条断言钉的是
    # "两个数可以不相等，但必须都在场并且差值被点名" —— 上一轮正是因为它当了判决尺，
    # 244 页被投影成 273 页，而报告里只有一个页数。
    diagnostic = gate["two_route_cross_check"]
    assert diagnostic["basis"] == "MEASURED_R41_POST_FIX_TWO_ROUTE_YIELD"
    assert diagnostic["delta_vs_measured_ruler_pages"] == (
        diagnostic["projected_technical_effective_pages"]
        - gate["projected_technical_effective_pages"])
    assert gate["projected_technical_effective_pages"] == pages_from_ledger_chars(
        gate["allocated_total_chars"], chars_per_technical_page=LEDGER_CHARS_PER_TECHNICAL_PAGE)
    assert gate["worst_case_technical_effective_pages"] == pages_from_ledger_chars(
        gate["planned_total_chars"], chars_per_technical_page=LEDGER_CHARS_PER_TECHNICAL_PAGE)


def test_gate_blocks_over_ceiling_before_any_provider_call() -> None:
    """§13：计划总和超文档上限 → 不得进入正式生成，且要给出逐节摊派需求。"""
    tree = _planned_tree([("S-A", ["NARRATIVE", "STANDARD_TABLE"], 300000),
                          ("S-B", ["SOP", "CHECKLIST"], 260000)])
    gate = pre_generation_budget_gate(tree, page_targets=PAGE_TARGETS)
    assert gate["status"] == "BLOCKED_OVER_CEILING"
    assert gate["may_enter_generation"] is False
    assert gate["worst_case_page_state"] == "OVERSHOOT"
    assert gate["worst_case_technical_effective_pages"] > gate["page_ceiling"]
    assert gate["blocked_reasons"] == ["BLOCKED_OVER_CEILING"]
    assert gate["provider_calls_made"] == 0
    demand = gate["reallocation_demand"]
    assert {row["section_id"] for row in demand} == {"S-A", "S-B"}
    for row in demand:
        assert row["allowed_chars"] < row["planned_chars"]
        assert row["reduction_ratio"] > 0
    # 摊派需求加总要落回上限可容纳的量级，否则"先重新分配"只是一句口号
    assert sum(row["allowed_chars"] for row in demand) <= gate["ceiling_chars"] * 1.02


def test_gate_blocks_non_conserved_plan() -> None:
    tree = _planned_tree(_IN_BAND_SPECS, document_target=1000)
    gate = pre_generation_budget_gate(tree, page_targets=PAGE_TARGETS)
    assert gate["conservation_ok"] is False
    assert gate["status"] == "PASS"          # 页数本身在带内
    assert gate["may_enter_generation"] is False  # 但层间对不上就不许进生成
    assert gate["blocked_reasons"] == ["PLAN_NOT_CONSERVED"]


def test_gate_without_page_band_does_not_invent_one() -> None:
    tree = _planned_tree([("S-A", ["NARRATIVE"], 12000)], page_targets=None)
    gate = pre_generation_budget_gate(tree, page_targets=None)
    assert gate["page_ceiling"] == 0
    assert gate["headroom_pages"] == 0
    assert gate["at_ceiling_margin"] is False


# ----------------------------------------------- §14/§15 生成中账与再平衡 --
def _midrun_state():
    tree = _planned_tree([("S-A", ["NARRATIVE", "NARRATIVE"], 22000),
                          ("S-B", ["NARRATIVE", "NARRATIVE"], 22000),
                          ("S-C", ["NARRATIVE"], 11000)])
    return tree, empty_midrun_state(tree)


def _record(sid: str, cu_index: int, prose: int, structure: int = 0) -> dict:
    return {"content_unit_id": f"CU-{sid}-{cu_index:02d}", "section_id": sid,
            "archetype": "NARRATIVE", "prose_chars": prose, "structure_chars": structure,
            "actual_chars": prose + structure}


def test_midrun_observation_tracks_four_margins_and_caps_next_batch() -> None:
    """§14：不等 27 节全跑完 —— 第一批回来就要看见四条余量并压下一批额度。"""
    tree, plan = _midrun_state()
    assert plan["document_target_chars"] == 55000
    assert plan["document_remaining_chars"] == 55000
    observed = observe_batch(plan, [_record("S-A", 1, 18000),
                                    _record("S-B", 1, 12000, 2000),
                                    _record("S-B", 2, 14000),
                                    _record("S-C", 1, 2000)],
                             weights={"S-C": 0.30})
    assert observed["provider_calls_made"] == 0
    assert observed["batches_observed"] == 1
    assert observed["document_used_chars"] == 48000
    assert observed["document_remaining_chars"] == 7000
    assert observed["used_by_archetype"] == {"NARRATIVE": 48000}

    section_a = observed["sections"]["S-A"]
    assert section_a["used_narrative_chars"] == 18000
    assert section_a["used_table_chars"] == 0
    assert section_a["remaining_section_chars"] == 22000 - 18000
    assert section_a["remaining_narrative_chars"] == section_a["narrative_budget"] - 18000

    section_b = observed["sections"]["S-B"]
    assert section_b["used_table_chars"] == 2000
    assert section_b["used_narrative_chars"] == 26000, "表格字数走已用 table 这一路，不混进正文"

    adjustments = {row["section_id"]: row for row in observed["next_batch_adjustments"]}
    assert set(adjustments) == {"S-A", "S-B"}
    assert adjustments["S-A"]["status"] == "LOCAL_OVER_RUN"
    assert adjustments["S-A"]["next_batch_chars_per_unit_cap"] == 4000
    assert adjustments["S-A"]["next_batch_chars_per_unit_cap"] < adjustments["S-A"]["observed_chars_per_unit"]
    assert adjustments["S-B"]["next_batch_chars_per_unit_cap"] == 240  # 下限：不许降到 0

    assert observed["frozen_sections"] == ["S-B"]
    donors = observed["donor_candidates"]
    assert [row["section_id"] for row in donors] == ["S-C"]
    assert donors[0]["spare_chars"] == 9000
    assert donors[0]["weight"] == 0.30


def test_in_budget_batch_produces_no_adjustment() -> None:
    tree, plan = _midrun_state()
    observed = observe_batch(plan, [_record("S-A", 1, 10000)])
    assert observed["next_batch_adjustments"] == []
    assert observed["frozen_sections"] == []
    assert observed["donor_candidates"] == []


def test_midrun_reallocation_freezes_overruns_and_moves_spare() -> None:
    """§15：超预算节先冻结，富余节的机动额度才谈得上让给高权重节。"""
    tree, plan = _midrun_state()
    observed = observe_batch(plan, [_record("S-A", 1, 18000),
                                    _record("S-B", 1, 12000, 2000),
                                    _record("S-B", 2, 14000),
                                    _record("S-C", 1, 2000)])
    result = mid_run_reallocation(observed, demand=[
        {"section_id": "S-A", "needed_chars": 3000, "reason": "HIGH_WEIGHT_SCORING_GAP"},
        {"section_id": "S-B", "needed_chars": 2000, "reason": "OVER_BUDGET_WANTS_MORE"}])
    assert result["gate"] == "MID_RUN_REALLOCATION"
    assert result["provider_calls_made"] == 0
    assert result["frozen_sections"] == ["S-B"]
    assert [(row["from_section"], row["to_section"], row["chars"]) for row in result["moves"]] == [
        ("S-C", "S-A", 3000)]
    assert result["moved_chars"] == 3000
    assert result["unmet_demand_chars"] == 2000   # 冻结的 S-B 拿不到额度，如实记未满足
    assert result["sections"]["S-A"]["remaining_section_chars"] == 7000
    assert result["sections"]["S-C"]["section_budget"] == 8000
    # 搬运只搬额度，不造字
    assert sum(row["section_budget"] for row in result["sections"].values()) == 55000


# ------------------------------------- §7 账本：结构与正文分两路记账 --
def test_ledger_splits_prose_and_structure_and_catches_appended_tables() -> None:
    """Round 3 的漏口："正文有预算、表格无限追加"。现在 payload 计入同一把尺子。"""
    contract = _cu_contract("S-A", 1, "STANDARD_TABLE", 4000)
    record = judge_content_unit(contract, content={
        "content": "x" * 1000,                       # 正文面写在额度之内
        "payload": {"cells": "y" * 4200},            # 表体在额度之外长出来
    })
    assert record["actual_chars"] == 5200
    assert record["prose_chars"] == 1000
    assert record["structure_chars"] == 4200
    assert record["budget_status"] == "OVER_BUDGET"
    assert record["overshoot_chars"] == record["actual_chars"] - record["budget_max"]
    assert record["archetype"] == "STANDARD_TABLE"

    within = judge_content_unit(_cu_contract("S-A", 2, "STANDARD_TABLE", 4000),
                                content={"content": "x" * 1500, "payload": {"cells": "y" * 2000}})
    assert within["budget_status"] == "WITHIN_BUDGET"
    assert within["prose_chars"] + within["structure_chars"] == within["actual_chars"]

    ledger = merge_ledger(None, [record, within])
    assert ledger["totals"]["content_units_arrived"] == 2
    assert ledger["totals"]["over_budget_units"] == 1
    assert ledger["totals"]["prose_chars"] == 2500
    assert ledger["totals"]["structure_chars"] == 6200
    assert (ledger["totals"]["prose_chars"] + ledger["totals"]["structure_chars"]
            == ledger["totals"]["actual_chars"])
    # 外部只给总量（老账本、补深度记录）时按正文记账：宁可高估正文，不要把表格算多
    legacy = judge_content_unit(contract, chars=3800)
    assert legacy["prose_chars"] == 3800 and legacy["structure_chars"] == 0


# --------------------------------------------- §14 生产链：工厂侧的账 --
def test_factory_lowers_next_batch_contract_to_the_midrun_cap(tmp_path: Path) -> None:
    """§14：本节在超写 → 下一批的 CU 契约当场降额度，几何跟着重算。"""
    row = _cu_contract("S-A", 1, "STANDARD_TABLE", 12000)
    before = copy.deepcopy(row)
    factory = _factory(tmp_path)
    factory.budget_unit_caps = {"S-A": 3000}
    [lowered] = factory._apply_midrun_budget_cap("S-A", [copy.deepcopy(row)])

    assert lowered["planned_words"] == 3000
    assert lowered["unified_budget"]["effective_chars"] == 3000
    target = lowered["target_budget"]
    assert target["effective_chars"] == 3000
    assert target["prose_chars"] + target["structure_chars"] == 3000
    assert target["structure_chars"] < before["target_budget"]["structure_chars"], \
        "降额度必须连着少排行数，不能留一张大表只砍正文（§12 的同义重复形状）"
    assert lowered["budget_adjustment"]["reason"] == "MID_RUN_OVER_RUN_CAP"
    assert lowered["budget_adjustment"]["planned_before_chars"] == before["planned_words"]
    assert lowered["min_budget"]["effective_chars"] < 3000 < lowered["max_budget"]["effective_chars"]

    # 未触发上限的节：契约一个字都不动
    untouched = factory._apply_midrun_budget_cap("S-B", [copy.deepcopy(row)])
    assert "budget_adjustment" not in untouched[0]
    assert untouched[0]["planned_words"] == before["planned_words"]
    # 已经在上限之下的行也不动
    small = _cu_contract("S-A", 2, "NARRATIVE", 1500)
    kept = factory._apply_midrun_budget_cap("S-A", [copy.deepcopy(small)])
    assert "budget_adjustment" not in kept[0]


def test_factory_midrun_tracking_artifact_is_rebuilt_from_the_ledger(tmp_path: Path) -> None:
    """§14：账本是这一段唯一事实源 —— 每批重算，重复落账不重复计数。"""
    tree = _planned_tree([("S-A", ["NARRATIVE", "NARRATIVE"], 22000)])
    contracts = {row["content_unit_id"]: row for row in _planned_units(
        "S-A", ["NARRATIVE", "NARRATIVE"], 22000)}
    first_id, second_id = sorted(contracts)
    factory = _factory(tmp_path, cu_contracts=contracts, budget_tree=tree)
    cu_dir = tmp_path / "longform" / "word" / "sections" / "S-A" / "cus"
    cu_dir.mkdir(parents=True)
    write_json(cu_dir / f"{first_id}.json", {
        "content_unit_id": first_id, "parent_section_id": "S-A",
        "content": "x" * 18000, "payload": None, "archetype": "NARRATIVE",
        "budget": judge_content_unit(contracts[first_id], content={"content": "x" * 18000}),
    })

    factory._record_content_unit_budget("S-A")
    ledger = read_ledger(tmp_path / BUDGET_LEDGER_FILE)
    assert ledger["totals"]["content_units_arrived"] == 1
    assert ledger["records"][0]["budget_status"] == "OVER_BUDGET"

    tracking = json.loads((tmp_path / MIDRUN_BUDGET_FILE).read_text(encoding="utf-8"))
    assert tracking["gate"] == "MID_RUN_BUDGET_TRACKING"
    assert tracking["provider_calls_made"] == 0
    assert tracking["plan"]["document_target_chars"] == 22000
    observed = tracking["observed"]
    assert observed["sections"]["S-A"]["used_narrative_chars"] == 18000
    assert observed["sections"]["S-A"]["remaining_section_chars"] == 4000
    assert tracking["next_batch_chars_per_unit_cap"] == {"S-A": 4000}
    assert factory.budget_unit_caps == {"S-A": 4000}

    # 同一批再记一次（Resume / 重跑同一节）：账不重复计，上限不重复降
    factory._record_content_unit_budget("S-A")
    again = json.loads((tmp_path / MIDRUN_BUDGET_FILE).read_text(encoding="utf-8"))
    assert again["observed"]["sections"]["S-A"]["used_narrative_chars"] == 18000
    assert again["observed"]["sections"]["S-A"]["units_arrived"] == 1
    assert again["observed"]["document_used_chars"] == 18000
    assert again["next_batch_chars_per_unit_cap"] == {"S-A": 4000}
    assert again["observed"]["batches_observed"] == observed["batches_observed"] + 1

    # 下一批的契约当场降额度（同一节超写不再按原额度继续花）；已到达的那条不动
    context = factory._attach_cu_contracts({"section_contract": {"section_id": "S-A"}}, "S-A")
    rows = context["content_unit_contracts"]
    assert [row["content_unit_id"] for row in rows] == [second_id]
    assert rows[0]["budget_adjustment"]["reason"] == "MID_RUN_OVER_RUN_CAP"
    assert rows[0]["target_budget"]["effective_chars"] == 4000
    assert rows[0]["target_budget"]["prose_chars"] == 4000
    assert context["content_unit_execution"]["remaining_content_unit_ids"] == [second_id]
    assert context["content_unit_execution"]["completed_content_unit_ids"] == [first_id]


def test_factory_midrun_sync_never_writes_into_a_section_fragment(tmp_path: Path) -> None:
    """§14/§64：midrun 账落在**自己**的文件里，绝不落在最后一节的 fragment.json 上。

    Round 4 合成 300 页试炼抓到的真缺陷：§64 读取治理账的循环复用了 `path` 这个变量名，
    循环结束后 `path` 已经是某个节的 fragment 路径，末尾的 write_json 于是把
    MID_RUN_BUDGET_TRACKING 覆盖上去 —— 那一节的正文在装配链上整节消失，只剩标题，
    而 CU 账本、四条余量、页数预算全都读成正常。26 节里 16 节这样丢了正文，
    合成 300 类只跑出 111 页等效。这条测试是那次的回归屏障。

    同一个函数里的第二个真缺陷也一并钉住：治理账的键取自账里的 `section_id`，而真实
    装配链上那一格是**标题**（合成 300 类 22/22 节都因此回填不上，§64 整条链静默
    NOT_OBSERVED）。入账必须按节目录名 —— 与预算账本、`plan.sections` 同一个 id。
    """
    tree = _planned_tree([("S-A", ["NARRATIVE"], 11000), ("S-B", ["STANDARD_TABLE"], 11000)])
    contracts = {row["content_unit_id"]: row
                 for row in _planned_units("S-B", ["STANDARD_TABLE"], 11000)}
    cu_id = sorted(contracts)[0]
    factory = _factory(tmp_path, cu_contracts=contracts, budget_tree=tree)

    sections_root = tmp_path / "longform" / "word" / "sections"
    cu_dir = sections_root / "S-B" / "cus"
    cu_dir.mkdir(parents=True)
    arrived = {"content_unit_id": cu_id, "parent_section_id": "S-B",
               "content": "x" * 1000, "payload": {"cells": "y" * 4000},
               "archetype": "STANDARD_TABLE"}
    arrived["budget"] = judge_content_unit(contracts[cu_id], content=arrived)
    assert arrived["budget"]["structure_chars"] == 4000
    write_json(cu_dir / f"{cu_id}.json", arrived)

    def _fragment(sid: str, account: dict | None) -> dict:
        row = {"section_id": sid, "title": f"{sid} 正文",
               "body_blocks": [{"type": "paragraph", "text": "x" * 900},
                               {"type": "paragraph", "text": "y" * 900}]}
        if account is not None:
            row["table_governance"] = account
        return row

    sb_account = {"schema_version": "section-table-governance-account-v0.1",
                  # 真实装配链上读到的就是这种形状：账里写的是**标题式 id**，而预算账本、
                  # `plan.sections`、目录名用的是 `S-B`。按标题入账 → 22/22 节回填不上。
                  "section_id": "相关专项安排",
                  "structure_chars_charged": 4000,
                  "structure_chars_governed": 1000,
                  "tables_merged_groups": 0, "tables_dropped": 0,
                  "low_information_dropped": 0,
                  "basis": "longform/cu_assembly.table_governance_accounting"}
    # 排序把 S-B 放在最后 —— 正是被覆盖的那一个；S-A 用来证明"没有账的节"也不许被写坏。
    written = {sid: (sections_root / sid / "fragment.json") for sid in ("S-A", "S-B")}
    for sid, fragment_path in written.items():
        fragment_path.parent.mkdir(parents=True, exist_ok=True)
        fragment_path.write_text(json.dumps(
            _fragment(sid, sb_account if sid == "S-B" else None), ensure_ascii=False),
            encoding="utf-8")
    pristine = {sid: path.read_text(encoding="utf-8") for sid, path in written.items()}

    factory._record_content_unit_budget("S-B")

    tracking = json.loads((tmp_path / MIDRUN_BUDGET_FILE).read_text(encoding="utf-8"))
    assert tracking["gate"] == "MID_RUN_BUDGET_TRACKING"
    assert tracking["provider_calls_made"] == 0
    observed = tracking["observed"]
    # §64 回填真的发生了：治理掉的 3000 字从"已用结构额度"里退回来。
    row = observed["sections"]["S-B"]
    assert row["used_table_chars"] == 1000
    assert row["remaining_structured_chars"] == row["structured_budget"] - 1000
    governance = observed["table_governance"]
    assert governance["status"] == "OBSERVED"
    note = governance["per_section"]["S-B"]
    assert note["reclaimed_chars"] == 3000
    assert note["applied_chars"] == 3000
    assert note["unapplied_chars"] == 0
    # 键不一致必须看得见：入账按目录名，但账里那个标题式 id 要留下计数。
    assert tracking["table_governance"]["account_key_mismatch_sections"] == 1
    assert tracking["table_governance"]["status"] == "OBSERVED"
    assert "per_section" not in tracking["table_governance"]

    for sid, path in written.items():
        text = path.read_text(encoding="utf-8")
        assert text == pristine[sid], f"{sid} 的 fragment.json 被 midrun 账覆盖了"
        assert "MID_RUN_BUDGET_TRACKING" not in text
        assert len(json.loads(text)["body_blocks"]) == 2, f"{sid} 的正文块不再完整"


def test_concurrent_ledger_updates_do_not_lose_records(tmp_path: Path) -> None:
    """多批同时返回：读-改-写必须串行，否则后写的线程把前一笔账覆盖掉。"""
    tree = _planned_tree([(sid, ["NARRATIVE"], 11000)
                          for sid in ("S-A", "S-B", "S-C", "S-D")])
    factory = _factory(tmp_path, budget_tree=tree)
    barrier = threading.Barrier(4)
    for sid in ("S-A", "S-B", "S-C", "S-D"):
        cu_dir = tmp_path / "longform" / "word" / "sections" / sid / "cus"
        cu_dir.mkdir(parents=True)
        write_json(cu_dir / f"CU-{sid}-01.json", {
            "content_unit_id": f"CU-{sid}-01", "parent_section_id": sid,
            "content": "x" * 3000, "payload": None, "archetype": "NARRATIVE",
            "budget": judge_content_unit(_cu_contract(sid, 1, "NARRATIVE", 11000),
                                        content={"content": "x" * 3000}),
        })

    def worker(sid: str) -> None:
        barrier.wait()
        factory._record_content_unit_budget(sid)

    threads = [threading.Thread(target=worker, args=(sid,)) for sid in ("S-A", "S-B", "S-C", "S-D")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    ledger = read_ledger(tmp_path / BUDGET_LEDGER_FILE)
    assert sorted(row["content_unit_id"] for row in ledger["records"]) == [
        "CU-S-A-01", "CU-S-B-01", "CU-S-C-01", "CU-S-D-01"]
    assert ledger["totals"]["actual_chars"] == 12000
    tracking = json.loads((tmp_path / MIDRUN_BUDGET_FILE).read_text(encoding="utf-8"))
    assert tracking["observed"]["document_used_chars"] == 12000
    assert set(tracking["observed"]["sections"]) == {"S-A", "S-B", "S-C", "S-D"}


# ------------------------------------------------------- §7 货币一致性 --
def test_artifact_and_ledger_currencies_do_not_mix() -> None:
    """事后 QA 量装配产物可见字，规划额度是账本字 —— 直接相除会把每节多算 29.7% 深度。"""
    assert ARTIFACT_CHARS_PER_LEDGER_CHAR_RUN_D == 0.7707
    assert artifact_chars_from_ledger(10000) == 7707
    contract = {"target_words": {"min": 10000}}
    assert 0.99 < depth_ratio(7707, contract) < 1.01
    # 旧的混用口径会把"按新额度写满的一篇"读成缺字
    assert depth_ratio(7707, contract) > 0.77 * 1.2
    assert depth_ratio(5000, contract) < 0.7
    assert depth_ratio(0, contract) == 0.0
    assert depth_ratio(123, {}) == 1.0


# -------------------------------------------------- §13 规划产物落盘 --
def test_planning_stage_writes_the_budget_gate_artifact(tmp_path: Path) -> None:
    """§13：规划阶段就要能看见"这个计划能不能进生成"，且证据里零 Provider 调用。"""
    pack = _pack()
    brief = {"project_name": "产业园物业项目", "medium": "WORD", "project_type": "园区物业",
             "scenario": "技术标", "document_scale": "regular_solution"}
    selection = {"knowledge_usage_contracts": []}
    bundle = build_planning_bundle(pack, brief, selection, production=True)
    (tmp_path / "tender").mkdir(parents=True)
    (tmp_path / "tender" / "requirement_pack.json").write_text(
        json.dumps(pack, ensure_ascii=False), encoding="utf-8")

    written = attach_hierarchy_artifacts(tmp_path, bundle)
    assert written is not None
    gate_path = tmp_path / BUDGET_GATE_FILE
    assert gate_path.is_file()
    assert written["budget_gate"] == gate_path
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    hierarchy = build_document_hierarchy(pack, bundle)
    assert gate == hierarchy["pre_generation_budget_gate"]
    assert gate["gate"] == "PRE_GENERATION_BUDGET_GATE"
    assert gate["status"] in {"PASS", "BLOCKED_OVER_CEILING", "RAISE_PLAN_UNDER_FLOOR"}
    assert gate["provider_calls_made"] == 0
    # 页带取的是**本规模自己**的区间，不是硬编码的 250–320（§4）
    word = (bundle.get("word_plan") or {}).get("document_target") or bundle["document_target"]["word"]
    assert (gate["page_floor"], gate["page_ceiling"]) == (
        int(word["target_pages_min"]), int(word["target_pages_max"]))
    assert gate["chars_per_technical_page"] == round(LEDGER_CHARS_PER_TECHNICAL_PAGE, 1)
    assert gate["conservation"]["conserved"] is True
    assert gate["planned_total_chars"] == hierarchy["counts"]["planned_words"] + sum(
        int((row.get("budget_split") or {}).get("reserve_budget") or 0)
        for row in hierarchy["sections"])
