"""Round 4.1 P0-1 的回归：页数尺必须钉在测量上，合成夹具必须是"有代表性的替身"。

这一份为什么单独存在：上一轮 300 类合成"投影 273 页 / 实测 244 页"差了 29 页，
而测试仍然通过 —— 因为通过门是 200 页（工程地板被当成了产品门），而投影用的换算
是从"计划结构占比"倒推的、不是直量的。两头都松开时，规模验证就只是在自证。

这里钉三件事：
  1. 规划尺 `LEDGER_CHARS_PER_TECHNICAL_PAGE` 等于**当前这条链**直量出来的那个数，
     且不等于已被修掉的损失标定过的旧尺；
  2. 生成前门控的页数判决与规划额度同源，两路收益模型只能作为交叉核对存在；
  3. MockProvider 交出的正文与结构 payload 在**句级重复率**和**每单元字数**上
     与真实 Run D 同量级 —— 夹具不真实时，任何一把页数尺都不可能同时量准三档。
零 Provider 调用（纯本地字符串与纯规划函数）。
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from planning.budget_control import visible_chars_of
from planning.unified_budget import (
    LEDGER_CHARS_PER_TECHNICAL_PAGE,
    LEDGER_CHARS_PER_TECHNICAL_PAGE_RUN_D,
    LEDGER_TO_ARTIFACT_VISIBLE_YIELD,
    PAGE_PROJECTION_BASIS,
    RENDER_VISIBLE_CHARS_PER_TECHNICAL_PAGE,
    pre_generation_budget_gate,
)
from providers.mock import MockProvider

# Run D 真实产物的直量读数（只读探针 `r41_yield_decomposition.py` /
# `r41_page_yield_calibration.py`；逐结构单元密度：表行 82–99、流程步 47–53、清单项 52；
# 句级重复率 0.001）。**注意**：这条 1,183 字/页是"修复前那条链"的直量，不是产品常数。
RUN_D_LEDGER_CHARS = 467292
RUN_D_TECHNICAL_PAGES = 395
RUN_D_SENTENCE_DUPLICATION = 0.001
# R41 修复后 LEVEL-4 文档级直量（`r41_p0_1_ruler300_300p_summary.json` run1）：
#   269,646 账本字 ÷ 290 技术页等效 = 929.8 字/页（测试口径同一次 917.2）
R41_LEDGER_CHARS = 269646
R41_TECHNICAL_PAGES = 290


def _sentence_dupe_share(texts: list[str]) -> float:
    sentences = [s.strip() for text in texts for s in re.split(r"[。；]", text)
                 if len(s.strip()) >= 12]
    if not sentences:
        return 0.0
    return round(1.0 - len(set(sentences)) / len(sentences), 4)


def _mock_section(cus: list[dict]) -> dict:
    provider = MockProvider({"provider_name": "mock", "provider_type": "mock",
                             "enabled": True, "model": "mock-r41"})
    context = {"section_contract": {"section_id": "S01-01", "section_title": "门区通行组织",
                                    "must_cover": ["门区通行管理原则", "访客核验接口"],
                                    "required_outputs": ["通行组织记录"],
                                    "source_requirements": ["REQ-0001"]},
               "relevant_process_contracts": []}
    request = {"prompt": "Context Pack：\n" + json.dumps(context, ensure_ascii=False),
               "content_unit_contracts": cus}
    return provider._section_fragment(request)


def _cus(count: int = 12, per_target: int = 900) -> list[dict]:
    kinds = ("NARRATIVE", "STANDARD_TABLE", "SOP", "CHECKLIST")
    rows = []
    for index in range(count):
        archetype = kinds[index % len(kinds)]
        budget = {"effective_chars": per_target}
        if archetype == "STANDARD_TABLE":
            budget["rows"] = 6
        elif archetype == "SOP":
            budget["steps"] = 6
        elif archetype == "CHECKLIST":
            budget["items"] = 6
        rows.append({"content_unit_id": f"CU-S01-01-{index + 1:02d}",
                     "parent_section_id": "S01-01", "archetype": archetype,
                     "intent": f"门区通行组织要点{index + 1}",
                     "title": f"门区通行组织要点{index + 1}",
                     "must_cover": [f"通行核验口径{index + 1}", "记录与检查"],
                     "target_budget": budget})
    return rows


class PageRulerIsMeasured(unittest.TestCase):
    """页数尺必须钉在**当前这条链**的直量上。

    Run D 的 1,183 字/页不是错数，它是"修复前那条链"的直量：那时候 CU 装配出来的表格行
    与流程步骤会在提交站被跨列表去重删空，产物只剩正文，所以每个账本字"买"到的页更少。
    修掉之后同一批夹具、同一条链直量到 930 字/页 —— 继续拿 1,183 排产，285 页的档就会
    被排成 337,155 账本字，实测落到 352 页，直接越 320 上沿（R41 实测见证据目录
    `r41_p0_1_fix_300p_summary.json`）。所以钉两头：等于新的直量，且不等于失效的旧尺。
    """

    def test_ruler_equals_the_current_chain_direct_measurement(self):
        self.assertLess(
            abs(LEDGER_CHARS_PER_TECHNICAL_PAGE - R41_LEDGER_CHARS / R41_TECHNICAL_PAGES), 0.5,
            "规划尺不再等于 R41 修复后 LEVEL-4 的文档级直量值")
        # 失效的旧尺（修复前那条链）与倒推出来的混合尺都不得回到规划里
        self.assertLess(LEDGER_CHARS_PER_TECHNICAL_PAGE, LEDGER_CHARS_PER_TECHNICAL_PAGE_RUN_D,
                        "规划尺又用回了修复前那条链的 1,183 —— 那是 bug 标定")
        self.assertNotAlmostEqual(LEDGER_CHARS_PER_TECHNICAL_PAGE, 1062.2, places=1)
        self.assertAlmostEqual(LEDGER_TO_ARTIFACT_VISIBLE_YIELD,
                               round(RENDER_VISIBLE_CHARS_PER_TECHNICAL_PAGE
                                     / LEDGER_CHARS_PER_TECHNICAL_PAGE, 4), places=4)

    def test_pre_fix_ruler_still_matches_its_own_measurement(self):
        """旧尺换下来不等于把它改数：Run D 的 1,183 仍是它那一次跑的直量。"""
        self.assertLess(abs(LEDGER_CHARS_PER_TECHNICAL_PAGE_RUN_D
                            - RUN_D_LEDGER_CHARS / RUN_D_TECHNICAL_PAGES), 0.5)

    def test_stale_ruler_would_plan_the_class_out_of_band(self):
        """旧尺为什么是"越顶"而不是"欠字"：同一个 285 页档，用 1,183 排出来实测 352 页。"""
        planning_pages = 285
        stale_chars = planning_pages * LEDGER_CHARS_PER_TECHNICAL_PAGE_RUN_D
        measured_chars_per_page = R41_LEDGER_CHARS / R41_TECHNICAL_PAGES
        self.assertGreater(stale_chars / measured_chars_per_page, 320,
                           "旧尺已经不再越顶的话，这条钉的就不是当前事实")
        current_chars = planning_pages * LEDGER_CHARS_PER_TECHNICAL_PAGE
        self.assertLessEqual(current_chars / measured_chars_per_page, 320)
        self.assertGreaterEqual(current_chars / measured_chars_per_page, 250)

    def test_gate_projects_pages_with_the_same_ruler_that_buys_the_plan(self):
        from test_mmf007c_unified_budget import _planned_tree

        mix = ["NARRATIVE", "STANDARD_TABLE", "SOP", "CHECKLIST"]
        specs = [(f"S-{index:02d}", mix, 40000) for index in range(1, 9)]
        gate = pre_generation_budget_gate(_planned_tree(specs), page_targets=(250, 320))
        self.assertEqual(gate["projection_method"], PAGE_PROJECTION_BASIS)
        self.assertEqual(gate["projected_technical_effective_pages"],
                         round(gate["allocated_total_chars"] / LEDGER_CHARS_PER_TECHNICAL_PAGE))
        self.assertEqual(gate["worst_case_technical_effective_pages"],
                         round(gate["planned_total_chars"] / LEDGER_CHARS_PER_TECHNICAL_PAGE))
        # 两路收益留着，但它只能是交叉核对，并且**它与判决尺的差必须被具名报出来**：
        # 上一轮 273 vs 244 那 29 页就是这个差，当时它不在任何读数里。
        diagnostic = gate["two_route_cross_check"]
        self.assertEqual(diagnostic["basis"], "MEASURED_R41_POST_FIX_TWO_ROUTE_YIELD")
        self.assertEqual(diagnostic["delta_vs_measured_ruler_pages"],
                         diagnostic["projected_technical_effective_pages"]
                         - gate["projected_technical_effective_pages"])


class SyntheticFixtureIsRepresentative(unittest.TestCase):
    def test_cu_prose_is_not_repeated_across_content_units(self):
        fragment = _mock_section(_cus())
        texts = [row["content"] for row in fragment["content_unit_fragments"]]
        dupe = _sentence_dupe_share(texts)
        self.assertLess(dupe, 0.05,
                        f"夹具 CU 正文句级重复率 {dupe}，真实 Run D 同口径 {RUN_D_SENTENCE_DUPLICATION}"
                        "（旧夹具实测 0.935，会被装配去重剥掉约四分之一正文）")

    def test_structure_payloads_match_measured_unit_density(self):
        fragment = _mock_section(_cus())
        per_archetype: dict[str, list[float]] = {}
        for row in fragment["content_unit_fragments"]:
            payload = row.get("payload") or {}
            for key, floor in (("rows", 80), ("steps", 40), ("items", 45)):
                units = payload.get(key)
                if isinstance(units, list) and units:
                    per_archetype.setdefault(key, []).append(
                        visible_chars_of(units) / len(units))
        for key, values in per_archetype.items():
            mean = sum(values) / len(values)
            floor = {"rows": 80, "steps": 40, "items": 45}[key]
            self.assertGreaterEqual(
                mean, floor,
                f"夹具每个{key}实测 {mean:.0f} 字，低于 Run D 直量下沿 {floor} 字 —— "
                "计划按 105/70/60 字/单元排结构额度，夹具交不出这个密度时"
                "实测结构占比会掉到计划的一半以下，页数尺子必然失准")

    def test_cu_delivery_sits_on_its_contract_not_above_it(self):
        target = 900
        fragment = _mock_section(_cus(per_target=target))
        ratios = []
        for row in fragment["content_unit_fragments"]:
            delivered = visible_chars_of(row.get("content")) + visible_chars_of(row.get("payload"))
            ratios.append(delivered / target)
        mean = sum(ratios) / len(ratios)
        self.assertLessEqual(mean, 1.2, f"平均交付 {mean:.2f}×额度，夹具在超写")
        self.assertGreaterEqual(mean, 0.9,
                                f"平均交付 {mean:.2f}×额度 —— 正文与 payload 合起来必须够到额度，"
                                "否则 CU 全档 UNDER_BUDGET，页数与额度对不上是必然的")


class ProductBandIsNotTheEngineeringFloor(unittest.TestCase):
    def test_300_class_product_gate_is_the_250_320_band(self):
        sys.path.insert(0, str(Path(__file__).parent))
        import test_mmf007_scale_levels as scale

        self.assertEqual(scale.PRODUCT_ACCEPTANCE_BAND[300], (250, 320))
        self.assertEqual(scale.ENGINEERING_REGRESSION_FLOOR[300], 250,
                         "300 类的工程地板必须抬到硬门下沿：200 页当产品门是上一轮的错")
        self.assertGreaterEqual(scale.ENGINEERING_REGRESSION_FLOOR[300],
                                scale.PRODUCT_ACCEPTANCE_BAND[300][0])
        # 300 档不再存在任何"数字地板"式通过门：规模测试必须把档位交出去，
        # 由 `PRODUCT_ACCEPTANCE_BAND` 决定门，而不是在调用点写一个数。
        source = (Path(__file__).parent / "test_mmf007_scale_levels.py").read_text(encoding="utf-8")
        calls = [line for line in source.splitlines()
                 if "_assert_level(" in line and not line.strip().startswith("#")]
        self.assertTrue(calls, "规模测试里已经找不到 _assert_level 调用了")
        for line in calls:
            match = re.search(r"_assert_level\(\s*\w+\s*,\s*(\d+)", line)
            if match:
                self.assertIn(
                    int(match.group(1)), scale.PRODUCT_ACCEPTANCE_BAND,
                    f"_assert_level 的第二个参数又变回页数字而不是档位（{line.strip()}）"
                    "—— 门的取值只能来自 PRODUCT_ACCEPTANCE_BAND")


if __name__ == "__main__":
    unittest.main()
