"""MMF-007C Round 4 · Part E：Capacity-Aware Batching 与离线性能画像的回归。

这一组测试守的是"省调用"这件事的**底线**，不是省了多少：
省下来的每一次调用都必须仍然请求到它该请求的 CU（守恒），并且不能把硬依赖
合进同一次调用（前置条件）。这两条破了，调用数再好看也是把内容弄丢了。

离线轮：只用 Synthetic Fixture，不发起 Provider 调用，不预测真实墙钟。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from longform.batch_planner import content_unit_batches, plan_generation_batches  # noqa: E402
from longform.cu_calibration import (  # noqa: E402
    POOLED_DELIVERY_CALIBRATION,
    batch_output_ceiling_tokens,
    call_tail_amplification,
    delivery_ratio,
    predicted_cu_output_tokens,
    worst_case_cu_output_tokens,
)
from longform.performance_planner import NOT_MEASURED, profile_batch_plan  # noqa: E402

PROVIDER = "qoder_internal"
PROVIDER_CONFIG = {"max_tokens": 8192, "context_window": 128000}
OUTPUT_LIMIT = 8192
SAFE_BUDGET = int(OUTPUT_LIMIT * 0.72)

# 合批判决的**闭合**词表：产品里新增一道门，这条集合就会失败，逼着新门在报告和测试里
# 具名，而不是变成"就是没合上"。
MERGE_GATE_VOCABULARY = {
    "cu_groups_of_same_section",
    "cu_cross_chapter",
    "cu_hard_dependency",
    "cu_over_safe_budget",
    "max_sections_per_call",
    "cross_chapter",
    "hard_dependency",
    "output_over_88pct_of_hard_limit",
    "input_over_55pct_of_context_window",
    "over_capacity_singleton_not_packed",
}


def _cu(section_id: str, index: int, *, archetype: str = "CHECKLIST", chars: int = 1000) -> dict:
    return {
        "content_unit_id": f"CU-{section_id}-{index:02d}",
        "archetype": archetype,
        "planned_words": chars,
        "max_budget": {"effective_chars": chars},
        "must_cover": ["要点"],
    }


def _section(section_id: str, order: int, *, cu_count: int = 12, archetype: str = "CHECKLIST",
             chars: int = 1000, target_words_max: int = 12000, with_cus: bool = True) -> dict:
    return {
        "section_id": section_id,
        "section_title": f"第 {order} 节",
        "generation_order": order,
        "archetype": archetype,
        "target_words": {"min": 900, "max": target_words_max},
        "must_cover": ["要点一"],
        "source_requirements": ["REQ-0001"],
        "content_unit_contracts": [_cu(section_id, i, archetype=archetype, chars=chars) for i in range(1, cu_count + 1)] if with_cus else [],
    }


def _deps(pairs) -> dict:
    return {
        "schema_version": "cross-section-dependency-v0.1",
        "dependencies": [
            {"source_section": src, "dependent_sections": list(targets),
             "dependency_type": "role_and_responsibility", "stale_on_change": True}
            for src, targets in pairs
        ],
    }


def _plan(contracts, deps=None):
    return plan_generation_batches(
        contracts, deps if deps is not None else _deps([]),
        provider_name=PROVIDER, speed_profile="fast", provider_config=PROVIDER_CONFIG,
    )


def _all_batch_cus(plan) -> list[str]:
    ids: list[str] = []
    for row in plan["batches"]:
        ids.extend(str(value) for value in (row.get("content_unit_ids") or []))
    return ids


class CalibrationRulerTests(unittest.TestCase):
    """点估计与尾部保护各自只有一个家，且来历可查。"""

    def test_point_estimate_uses_ratio_of_sums(self):
        # 容量问的是"一批加起来交付多少"，所以点估计是 Σ交付/Σ预算，不是分位数之比。
        self.assertEqual(delivery_ratio("SOP"), 0.9673)
        self.assertEqual(delivery_ratio("CHECKLIST"), 0.8779)

    def test_small_sample_and_unknown_archetypes_fall_back_to_pooled(self):
        self.assertEqual(delivery_ratio("KPI_TABLE"), POOLED_DELIVERY_CALIBRATION["aggregate"])
        self.assertEqual(delivery_ratio(None), POOLED_DELIVERY_CALIBRATION["aggregate"])
        self.assertEqual(delivery_ratio("MADE_UP_ARCHETYPE"), POOLED_DELIVERY_CALIBRATION["aggregate"])

    def test_single_unit_tail_still_available_for_reporting_only(self):
        self.assertEqual(delivery_ratio("SOP", "p95"), 1.2587)
        unit = _cu("S01-01", 1, archetype="SOP", chars=1000)
        self.assertGreater(worst_case_cu_output_tokens(unit), predicted_cu_output_tokens(unit))

    def test_tail_protection_is_applied_once(self):
        # Provider 侧 0.72 已经是这一轮唯一一次尾部保护；实测批次上限必须比安全预算宽，
        # 否则就说明有人把第二层保守又乘了一遍（Run D 72 次调用的成因）。
        amplification = call_tail_amplification()
        self.assertGreater(amplification, 1.0)
        self.assertLess(amplification, 1 / 0.72)
        ceiling = batch_output_ceiling_tokens(OUTPUT_LIMIT)
        self.assertGreater(ceiling, SAFE_BUDGET)
        self.assertLess(ceiling, OUTPUT_LIMIT)
        self.assertEqual(ceiling, int(OUTPUT_LIMIT / amplification))

    def test_calibration_records_that_tokens_are_not_independently_verifiable(self):
        plan = _plan([_section("S01-01", 1)])
        provenance = plan["output_calibration"]["provenance"]
        self.assertEqual(provenance["token_dimension"], "NOT_INDEPENDENTLY_VERIFIABLE")
        self.assertEqual(provenance["content_units_measured"], 304)
        self.assertEqual(plan["batches"][0]["capacity_basis"], "calibrated_aggregate_point_estimate")


class CapacityAwareBatchingTests(unittest.TestCase):
    def test_content_units_are_conserved_exactly_once(self):
        contracts = [_section(f"S01-0{i}", i, cu_count=count) for i, count in ((1, 7), (2, 6), (3, 13))]
        plan = _plan(contracts)
        planned_ids = {cu["content_unit_id"] for c in contracts for cu in c["content_unit_contracts"]}
        batch_ids = _all_batch_cus(plan)
        self.assertEqual(len(batch_ids), len(set(batch_ids)), "同一个 CU 被两次请求 = 重复付费")
        conservation = plan["capacity_summary"]["content_unit_conservation"]
        self.assertTrue(conservation["conservation_holds"])
        self.assertEqual(conservation["planned_content_units_total"], len(planned_ids))
        self.assertEqual(conservation["grouped_content_units"] + conservation["carried_by_whole_section_calls"], len(planned_ids))
        self.assertEqual(conservation["duplicated_within_section"], [])

    def test_cu_grouping_keeps_same_section_groups_apart(self):
        # 一节的 CU 组是因为"这一节超了安全容量"才存在的，再把它们合回一次调用等于没有拆。
        plan = _plan([_section("S01-01", 1, cu_count=13)])
        grouped = [row for row in plan["batches"] if row["section_ids"] == ["S01-01"]]
        self.assertGreaterEqual(len(grouped), 2)
        for row in grouped:
            self.assertEqual(len(set(row["content_unit_ids_by_section"]["S01-01"])), len(row["content_unit_ids_by_section"]["S01-01"]))

    def test_cross_section_cu_batch_lists_every_section_it_carries(self):
        # 回归：开头切片把下一节的 CU 补进当前批时，section_ids 必须一起记账。
        # 漏记的后果不是数字难看，是 factory._run_batch 照 section_ids 取 CU，
        # 补进来的那半截 CU 永远不会被请求——省一次调用，丢一段正文。
        contracts = [_section("S01-01", 1, cu_count=7), _section("S01-02", 2, cu_count=6)]
        plan = _plan(contracts)
        sliced = [
            row for row in plan["batches"]
            if len(row["section_ids"]) > 1 and any(ids for ids in (row["content_unit_ids_by_section"] or {}).values())
        ]
        self.assertTrue(sliced, "这一组输入必须走到开头切片，否则本测试测不到回归点")
        for row in plan["batches"]:
            carried = {sid for sid, ids in (row.get("content_unit_ids_by_section") or {}).items() if ids}
            self.assertTrue(carried <= set(row["section_ids"]),
                            f"{row['batch_id']} 持有 {sorted(carried - set(row['section_ids']))} 的 CU 却没登记该 Section")

    def test_safe_budget_is_never_exceeded_by_a_cu_batch(self):
        contracts = [_section(f"S01-0{i}", i, cu_count=count) for i, count in ((1, 7), (2, 6), (3, 13))]
        plan = _plan(contracts)
        for row in plan["batches"]:
            if row.get("content_unit_ids"):
                self.assertLessEqual(row["estimated_output_tokens"], SAFE_BUDGET, row["batch_id"])
        self.assertEqual(plan["capacity_summary"]["batches_over_measured_batch_ceiling"], [])

    def test_hard_dependency_never_shares_a_provider_call(self):
        contracts = [_section("S01-01", 1, cu_count=7), _section("S01-02", 2, cu_count=6)]
        plan = _plan(contracts, _deps([("S01-01", ["S01-02"])]))
        for row in plan["batches"]:
            self.assertFalse({"S01-01", "S01-02"} <= set(row["section_ids"]),
                             "硬依赖的两节被合进同一次调用，下游读不到上游状态")
        downstream = next(row for row in plan["batches"] if row["section_ids"] == ["S01-02"] or "S01-02" == row["section_ids"][0])
        self.assertTrue(downstream["dependency_ids"], "下游批次必须显式等待上游批次")

    def test_cross_chapter_sections_stay_in_separate_calls(self):
        contracts = [_section("S01-01", 1, cu_count=3), _section("S02-01", 2, cu_count=3)]
        plan = _plan(contracts)
        for row in plan["batches"]:
            self.assertEqual(len(set(str(sid)[:3] for sid in row["section_ids"])), 1)

    def test_every_extra_call_can_name_the_gate_that_bought_it(self):
        # "为什么这一轮是 N 次"必须能指到具体的门，而不是让人重读 packing 循环猜。
        contracts = [
            _section("S01-01", 1, cu_count=7), _section("S01-02", 2, cu_count=6),
            _section("S02-01", 3, cu_count=5),
        ]
        plan = _plan(contracts, _deps([("S01-01", ["S02-01"])]))
        log = plan["merge_decision_log"]
        self.assertTrue(log, "三节跨两章又带依赖，必然有合并被拒；一条记录都没有说明判决没落账")
        for row in log:
            self.assertIn(row["reason"], MERGE_GATE_VOCABULARY, row)
            self.assertIn(row["candidate_section_id"], {c["section_id"] for c in contracts})
        distribution = plan["capacity_summary"]["merge_block_reason_distribution"]
        self.assertTrue(set(distribution) <= MERGE_GATE_VOCABULARY)
        self.assertEqual(sum(distribution.values()), len(log))
        self.assertTrue(any("cross_chapter" in reason for reason in distribution),
                        "跨章那一节必须被同章门抓到（CU 组与普通节各一条门名），"
                        "而不是含糊地『就是没合上』")
        self.assertTrue(all(row.get("carried_section_ids") or row["reason"] == "over_capacity_singleton_not_packed"
                            for row in log), "每次拒绝都要说清是跟哪一批没合上")

    def test_the_gate_that_binds_is_the_one_named(self):
        # 归因必须指向**真正起作用**的那道门。Run D 回放里曾出现 42/59 次记到
        # "实测天花板"名下，而那道门折算成 Provider 上限的 0.9018，比 CU 分支的 0.72
        # 与整节分支的 0.88 都松 —— 它从未绑定，却抢在前面署了名。
        from longform.batch_planner import _merge_block_reason
        cap = {"max_output_tokens": OUTPUT_LIMIT, "context_window": 128000}
        self.assertLess(SAFE_BUDGET, int(OUTPUT_LIMIT * 0.88))
        self.assertLess(int(OUTPUT_LIMIT * 0.88), batch_output_ceiling_tokens(OUTPUT_LIMIT))
        current = {"section_ids": ["S01-01"], "estimated_output_tokens": 4200,
                   "estimated_input_tokens": 2000, "content_unit_ids": ["CU-S01-01-01"]}
        nxt = {"section_id": "S01-02", "output_tokens": 2400, "input_tokens": 2000,
               "content_unit_ids": ["CU-S01-02-01"]}
        self.assertEqual(_merge_block_reason(current, nxt, {}, cap, 4, SAFE_BUDGET),
                         "cu_over_safe_budget")
        nxt["output_tokens"] = 1500
        self.assertIsNone(_merge_block_reason(current, nxt, {}, cap, 4, SAFE_BUDGET))
        # 依赖与同章优先于容量：省调用不能省前置条件。
        self.assertEqual(_merge_block_reason(current, nxt, {"S01-02": ["S01-01"]}, cap, 4, SAFE_BUDGET),
                         "cu_hard_dependency")
        self.assertEqual(_merge_block_reason(current, {"section_id": "S02-01", "output_tokens": 1500,
                                                      "input_tokens": 2000, "content_unit_ids": ["CU-x"]},
                                             {}, cap, 4, SAFE_BUDGET), "cu_cross_chapter")

    def test_over_capacity_section_survives_as_singleton(self):
        # 撑不下也不能丢：整节降级成 singleton，而不是静默删掉或偷偷改小预算。
        contracts = [_section("S09-01", 1, with_cus=False, target_words_max=60000), _section("S09-02", 2, cu_count=4)]
        plan = _plan(contracts)
        oversized = [row for row in plan["batches"] if row["section_ids"] == ["S09-01"]]
        self.assertEqual(len(oversized), 1)
        self.assertTrue(any(w["reason"] == "over_capacity_output_singleton" for w in plan["warnings"]))
        self.assertEqual({row["section_id"] for row in contracts},
                         {sid for row in plan["batches"] for sid in row["section_ids"]})

    def test_oversized_single_content_unit_is_not_dropped(self):
        # 一个 CU 自己的预算就超过安全容量时，只能单独成组并留痕，不能因为装不下就消失。
        groups = content_unit_batches("S01-01", [_cu("S01-01", 1, chars=9000), _cu("S01-01", 2, chars=200)],
                                      safe_budget=SAFE_BUDGET)
        ids = {cu_id for group in groups for cu_id in group["content_unit_ids"]}
        self.assertEqual(ids, {"CU-S01-01-01", "CU-S01-01-02"})
        self.assertTrue(any(group["split_reason"] == "CU_CONTINUATION_REQUIRED" for group in groups))

    def test_shrink_batch_rescales_capacity_estimates(self):
        from longform.batch_planner import shrink_batch
        row = {"batch_id": "GB-01", "section_ids": ["S01-01", "S01-02"],
               "estimated_output_tokens": 5000, "estimated_input_tokens": 3000,
               "worst_case_output_tokens": 7000}
        patched = shrink_batch(row, ["S01-01"])
        self.assertEqual(patched["section_ids"], ["S01-01"])
        self.assertEqual(patched["estimated_output_tokens"], 2500)
        self.assertEqual(patched["worst_case_output_tokens"], 3500)


class OfflinePerformanceProfileTests(unittest.TestCase):
    def test_wall_clock_is_never_invented_offline(self):
        plan = _plan([_section("S01-01", 1, cu_count=7), _section("S01-02", 2, cu_count=6)])
        self.assertEqual(plan["capacity_summary"]["wall_clock_projection"], NOT_MEASURED)
        profile = profile_batch_plan(plan)
        self.assertEqual(profile["wall_clock"]["planned_seconds"], NOT_MEASURED)
        self.assertTrue(profile["wall_clock"]["reason"])
        self.assertNotIn("observed_latency", profile)
        self.assertNotIn("observed_concurrency", profile)
        for key in profile:
            self.assertFalse(key.endswith("_seconds"), f"离线画像不应给出秒数：{key}")

    def test_throttle_gates_name_every_authority(self):
        profile = profile_batch_plan(_plan([_section("S01-01", 1, cu_count=7)]))
        kinds = profile["throttle_gate_counts"]
        self.assertEqual(kinds["concurrency"], 3)
        self.assertEqual(kinds["dependency"], 3)
        for gate in profile["throttle_gates"]:
            self.assertTrue(gate["authority"].endswith(".py") or ".py::" in gate["authority"], gate)
        # 否定结论同样要落账：并发被压低不是因为存在某个信号量。
        self.assertFalse(profile["semaphore_present"])
        self.assertTrue(profile["semaphore_note"])

    def test_call_floor_is_a_bound_not_a_target(self):
        # 画像要说清"还剩多少空间不在预测尺上"，而下界必须同时看容量与单节拆分。
        plan = _plan([_section("S01-01", 1, cu_count=7), _section("S01-02", 2, cu_count=6)],
                     _deps([("S01-01", ["S01-02"])]))
        bound = plan["capacity_summary"]["call_count_lower_bound"]
        explanation = profile_batch_plan(plan)["call_count_explanation"]
        self.assertEqual(explanation["lower_bound"], bound, "画像只能转接规划器的下界，不能自己再算一把尺")
        self.assertLessEqual(bound["lower_bound_calls"], plan["generation_batch_count"])
        self.assertEqual(bound["planned_calls"], plan["generation_batch_count"])
        self.assertEqual(bound["gap_over_lower_bound"],
                         plan["generation_batch_count"] - bound["lower_bound_calls"])
        self.assertTrue(bound["is_not_an_achievable_target"])
        self.assertEqual(sum(explanation["merge_block_reason_distribution"].values()),
                         len(plan["merge_decision_log"]))

    def test_split_floor_cannot_be_wished_away_by_token_arithmetic(self):
        # 每个 CU 各自就占掉大半窗口时，装箱会把它们一个个分开：这时"总 token 只够 3 次"
        # 是假下界。同节的两个 CU 组永远合不回去，所以真正的界是拆分出来的组数。
        plan = _plan([_section("S01-01", 1, cu_count=4, chars=3400)])
        bound = plan["capacity_summary"]["call_count_lower_bound"]
        self.assertEqual(bound["binding_bound"], "same_section_split_floor")
        self.assertGreater(bound["same_section_split_floor"], bound["capacity_token_floor"])
        self.assertGreaterEqual(plan["generation_batch_count"], bound["lower_bound_calls"])

    def test_observed_numbers_need_first_party_history(self):
        plan = _plan([_section("S01-01", 1, cu_count=7)])
        ledger = [{"max_observed_concurrency": 1, "max_observed_distinct_sections": 1,
                   "soft_latency_events": 4, "rate_limit_events": 0}]
        profile = profile_batch_plan(plan, concurrency_ledger=ledger)
        self.assertEqual(profile["observed_concurrency"]["max_observed_concurrency"], 1)
        self.assertEqual(profile["observed_concurrency"]["planned_vs_observed_gap"],
                         profile["planned_max_parallelism"] - 1)

    def test_profile_reports_conservation_from_the_plan(self):
        plan = _plan([_section("S01-01", 1, cu_count=7), _section("S01-02", 2, cu_count=6)])
        profile = profile_batch_plan(plan)
        self.assertTrue(profile["content_unit_conservation"]["conservation_holds"])
        self.assertEqual(profile["planned_provider_calls"], plan["generation_batch_count"])


if __name__ == "__main__":
    unittest.main()
