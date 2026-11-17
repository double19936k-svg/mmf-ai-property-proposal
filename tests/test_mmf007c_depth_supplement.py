"""MMF-007C Round 2 §10-§17：Depth Supplement 由 Whole-Artifact 驱动的契约测试。

Round 1 的真实 300-Class 运行里 Supplement 调用 = 0，根因是补深候选只有"这一节
UNDER_LENGTH"一个来源，而那一轮没有任何一节被判为 UNDER_LENGTH（页面缺口是整篇
文档级的）。本文件把修好之后的口径钉住：

  页面缺口（实测优先） → 专业域能力缺口（目录背书的具体交付物） → 新增 Content Unit
  → 受独立上限约束的补充调用。

任何一环退化成"把本节写得更详细"，这里的断言就会红。全部为本地计算，零 Provider 调用。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
APP = PACKAGE_ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from longform.capability_gaps import (  # noqa: E402
    MAX_CAPABILITIES_PER_SECTION,
    STATUS_CATALOGUE_UNAVAILABLE,
    STATUS_GAP,
    STATUS_NO_GAP,
    STATUS_TARGET_MET,
    SUPPLEMENT_UNIT_CHARS,
    load_catalogue,
    match_domain,
    plan_capability_gaps,
    present_capabilities,
)
from longform.depth_supplement import (  # noqa: E402
    MIN_SUPPLEMENT_CHARS,
    plan_depth_supplement,
    supplement_unit_count,
)
from longform.document_repair import MODE_SUPPLEMENT, build_repair_plan  # noqa: E402
from planning.content_engineering import CONTENT_ARCHETYPES  # noqa: E402
from longform.repair_policy import (  # noqa: E402
    DEFAULT_SUPPLEMENT_CEILING,
    PRIORITY_DEPTH_GAP,
    GlobalRepairBudget,
)


def _section_qa_shape(sid: str, title: str, text: str) -> dict:
    """QA/fragment 侧形态。"""
    return {"section_id": sid, "title": title,
            "body_blocks": [{"type": "paragraph", "content": text}],
            "tables": [], "processes": [], "callouts": []}


def _section_word_shape(sid: str, heading: str, text: str) -> dict:
    """装配后的 delivery 形态：上一轮只认 body_blocks，于是整篇产物被看成空的。"""
    return {"section_id": sid, "heading": heading, "level": 2,
            "paragraphs": [text], "bullets": [], "tables": []}


def _shell(sid: str, heading: str) -> dict:
    """只有标题的篇/章壳子：正文在同级小节里。"""
    return {"section_id": sid, "heading": heading, "level": 1,
            "paragraphs": [], "bullets": [], "tables": []}


def _domain(key: str) -> dict:
    return next(row for row in load_catalogue()["domains"] if row["key"] == key)


FAT = ("本项目建立设备运行班组，明确记录要求，落实加强完善各项安排，"
       "确保设施设备安全稳定运行。") * 6


class CapabilityCatalogueTests(unittest.TestCase):
    def test_r2_p02_01_catalogue_is_actionable_not_advice(self):
        """R2-P0-2①（§12）：目录里每条能力都必须能变成一张具体的交付物。"""
        catalog = load_catalogue()
        self.assertEqual(catalog["status"], "LOADED")
        domains = {row["key"] for row in catalog["domains"]}
        for required in ("customer_service", "order_security", "engineering", "environment",
                         "quality", "emergency", "staffing", "handover"):
            self.assertIn(required, domains, "§12 要求覆盖客服/秩序/工程/环境/品质/应急等专业域")
        total = 0
        for domain in catalog["domains"]:
            self.assertTrue(domain["signals"], "专业域没有判据信号就匹配不上章节")
            for cap in domain["capabilities"]:
                total += 1
                self.assertTrue(cap["key"] and cap["capability"])
                self.assertGreaterEqual(len(cap["detect"]), 2, "单一判据太容易误判")
                self.assertTrue(cap["required_output"], "缺口的补法必须是一张能落到纸面上的表/清单/规程")
                self.assertNotIn(cap["archetype"], {"", "NARRATIVE"},
                                 "补深补的是结构化交付物，不是又一段叙述")
        self.assertGreaterEqual(total, 40)
        # §12 点名的交付物类别要在目录里各有归属：一次规划只带 3 项/节，
        # 覆盖性只能在整本目录上核。
        named = " ".join(cap["required_output"] for row in catalog["domains"] for cap in row["capabilities"])
        for token in ("台账", "巡检", "维保", "故障", "工单", "操作卡", "投诉", "回访", "记录表"):
            self.assertIn(token, named, f"§12 点名的「{token}」类交付物在目录里没有对应条目")
        generic = present_capabilities("我们将加强管理，完善相关制度，确保服务到位。", _domain("engineering"))
        self.assertEqual(generic, [], "只说「加强/完善/确保」不算已经写了这项能力")


class CapabilityGapPlannerTests(unittest.TestCase):
    def test_r2_p02_02_page_gap_becomes_named_deliverables(self):
        """R2-P0-2②（§12）：页面缺口 → 工程/客服点名的具体交付物。"""
        artifact = {"sections": [
            _section_qa_shape("S04-01", "设施设备运行维护方案", FAT),
            _section_word_shape("S03-02", "客户服务与投诉处理安排", FAT),
        ]}
        result = plan_capability_gaps(artifact, page_gap=12, arrived_pages=207)
        self.assertEqual(result["status"], STATUS_GAP)
        gaps = result["gaps"]
        self.assertTrue(gaps)
        self.assertEqual({row["domain"] for row in gaps}, {"engineering", "customer_service"})
        outputs = " ".join(row["required_output"] for row in gaps)
        for named in ("台账", "巡检", "维保", "投诉", "回访"):
            self.assertIn(named, outputs, f"目录未覆盖 §12 点名的「{named}」类交付物")
        for row in gaps:
            self.assertTrue(row["required_output"] and row["archetype"] and row["novelty_basis"])
            self.assertGreaterEqual(row["min_items"], 4)
            self.assertEqual(row["estimated_pages"], 1.0, "一条缺口 ≈ 一页，报告口径要能核")
            self.assertEqual(row["estimated_tokens"], SUPPLEMENT_UNIT_CHARS // 4)
            self.assertIn(row["reason"], {"DOMAIN_ABSENT", "PAGE_CAPABILITY_GAP", "STRUCTURED_CAPABILITY_MISSING"})
            self.assertNotIn("更详细", json.dumps(row, ensure_ascii=False))
        self.assertEqual(result["metrics"]["estimated_pages_total"], float(len(gaps)))

    def test_r2_p02_03_page_target_already_met_stops_supplement(self):
        """R2-P0-2③（§16）：页数已达标就不再补深，不机械追 300 页。"""
        artifact = {"sections": [_section_qa_shape("S04-01", "设施设备运行维护方案", FAT)]}
        for page_gap in (0, -20):
            result = plan_capability_gaps(artifact, page_gap=page_gap, arrived_pages=251)
            self.assertEqual(result["status"], STATUS_TARGET_MET)
            self.assertEqual(result["gaps"], [], "达标之后再加 CU 就是注水")

    def test_r2_p02_04_missing_catalogue_degrades_without_raising(self):
        """R2-P0-2④：目录失踪只降级状态，不抛异常、更不凭空编缺口。"""
        artifact = {"sections": [_section_qa_shape("S04-01", "设施设备运行维护方案", FAT)]}
        result = plan_capability_gaps(artifact, page_gap=30,
                                      catalogue={"domains": [], "status": "CATALOGUE_UNAVAILABLE"})
        self.assertEqual(result["status"], STATUS_CATALOGUE_UNAVAILABLE)
        self.assertEqual(result["gaps"], [])

    def test_r2_p02_05_shells_unmatched_and_excluded_sections_are_not_planned(self):
        """R2-P0-2⑤：壳子、判不出专业域的节、本轮不负责生成的节都不立项。"""
        artifact = {"sections": [
            _shell("S04-00", "第四篇 设施设备维护"),
            _section_qa_shape("S04-01", "设施设备运行维护方案", FAT),
            _section_qa_shape("S09-01", "企业信誉与增值服务说明", FAT),
            _section_qa_shape("S05-01", "秩序维护方案", FAT),
        ]}
        result = plan_capability_gaps(artifact, page_gap=30, exclude_section_ids={"S05-01"})
        planned = {row["section_id"] for row in result["gaps"]}
        self.assertEqual(result["metrics"]["outline_shells_skipped"], 1,
                         "壳子的正文在同级小节里，往壳子上挂台账就是重复立项")
        self.assertNotIn("S04-00", planned)
        self.assertNotIn("S09-01", planned, "判不出专业域的章节不许编一条缺口出来")
        self.assertNotIn("S05-01", planned, "本轮不负责生成的章节不占补充预算")
        self.assertEqual(planned, {"S04-01"})
        self.assertEqual(match_domain("秩序维护方案"), "order_security")
        self.assertEqual(match_domain("企业信誉与增值服务说明"), "")

    def test_r2_p02_06_plan_is_capped_by_pages_sections_and_shapes(self):
        """R2-P0-2⑥：缺口数量受页面差额、单节上限与全书节数上限三重约束。"""
        artifact = {"sections": [
            _section_qa_shape(f"S04-{index:02d}", "设施设备运行维护方案", FAT)
            for index in range(1, 12)
        ]}
        for page_gap in (2, 5, 40):
            result = plan_capability_gaps(artifact, page_gap=page_gap)
            self.assertLessEqual(len(result["gaps"]), page_gap, "补深页数不能超过页面缺口")
            per_section: dict[str, int] = {}
            for row in result["gaps"]:
                per_section[row["section_id"]] = per_section.get(row["section_id"], 0) + 1
            self.assertTrue(all(count <= MAX_CAPABILITIES_PER_SECTION for count in per_section.values()))
        wide = plan_capability_gaps(artifact, page_gap=40, max_sections=3)
        self.assertLessEqual(wide["metrics"]["sections_in_plan"], 3)
        written = FAT + "设备台账按一物一档编码，巡检标准规定巡检频次与巡检项目表。"
        present = set(present_capabilities(written, _domain("engineering")))
        self.assertTrue(present)
        result = plan_capability_gaps(
            {"sections": [_section_qa_shape("S04-01", "设施设备运行维护方案", written)]}, page_gap=40)
        self.assertEqual(result["status"], STATUS_GAP)
        self.assertFalse({row["capability_key"] for row in result["gaps"]} & present,
                         "已到达的能力不得重复立项")

    def test_r2_p02_07_full_capability_coverage_needs_no_supplement(self):
        """R2-P0-2⑦：能力判据都在正文里出现过时，如实返回 NO_CAPABILITY_GAP_FOUND。"""
        engineering = _domain("engineering")
        text = FAT + "".join(cap["detect"][0] for cap in engineering["capabilities"])
        result = plan_capability_gaps(
            {"sections": [_section_qa_shape("S04-01", "设施设备运行维护方案", text)]}, page_gap=40)
        self.assertEqual(result["status"], STATUS_NO_GAP)
        self.assertEqual(result["gaps"], [])


def _contract(**overrides) -> dict:
    base = {
        "section_id": "S04-01",
        "section_title": "设施设备运行维护方案",
        "must_cover": ["设备巡检", "故障处置"],
        "required_outputs": ["设备台账"],
        "target_words": {"min": 1200, "target": 1600, "max": 2200},
        "source_requirements": ["REQ-0001"],
    }
    base.update(overrides)
    return base


def _gate(**overrides) -> dict:
    base = {
        "meaningful_body": True,
        "content_units": 1200,
        "length_status": "PASS",
        "contract_complete": True,
        "must_cover": {"设备巡检": "COVERED", "故障处置": "COVERED"},
        "required_outputs_missing": [],
        "status": "PASS",
    }
    base.update(overrides)
    return base


def _focus_rows(count: int = 3) -> list[dict]:
    rows = []
    for cap in _domain("engineering")["capabilities"][:count]:
        rows.append({
            "reason": "CAPABILITY_GAP",
            "topic": cap["capability"],
            "missing_capability": cap["capability"],
            "required_output": cap["required_output"],
            "archetype": cap["archetype"],
            "novelty_basis": f"本章未出现「{cap['detect'][0]}」判据",
        })
    return rows


class SupplementUnitContractTests(unittest.TestCase):
    def test_r2_p02_08_focus_gaps_become_new_content_units(self):
        """R2-P0-2⑧（§14）：文档级点名的缺口 → 带全部口径字段的新增 CU 契约。"""
        rows = plan_depth_supplement(_contract(), _gate(),
                                     planned_cu_ids=["CU-S04-01-01"], focus=_focus_rows(3))
        self.assertEqual(len(rows), 3, "点名三项缺口就补三个新单元，即使本节字数已达标")
        self.assertEqual([row["content_unit_id"] for row in rows],
                         ["CU-S04-01-02", "CU-S04-01-03", "CU-S04-01-04"])
        self.assertEqual({row["supplement_reason"] for row in rows}, {"CAPABILITY_GAP"})
        self.assertEqual({row["generation_origin"] for row in rows}, {"depth_supplement"})
        self.assertEqual({row["archetype"] for row in rows},
                         {row["archetype"] for row in _focus_rows(3)})
        for row, focus in zip(rows, _focus_rows(3)):
            self.assertEqual(row["supplement_cu_id"], row["content_unit_id"])
            self.assertEqual(row["parent_section_id"], "S04-01")
            self.assertEqual(row["title"], focus["missing_capability"])
            self.assertEqual(row["missing_capability"], focus["missing_capability"])
            self.assertEqual(row["required_output"], focus["required_output"])
            self.assertIn(focus["required_output"], row["required_outputs"])
            self.assertEqual(row["novelty_basis"], focus["novelty_basis"])
            self.assertEqual(row["estimated_pages"], 1.0)
            self.assertEqual(row["estimated_tokens"], SUPPLEMENT_UNIT_CHARS // 4)
            self.assertEqual(row["target_budget"]["effective_chars"], SUPPLEMENT_UNIT_CHARS)
            self.assertIn("REQ-0001", row["source_requirements"])
        blob = json.dumps(rows, ensure_ascii=False)
        for padding in ("进一步细化上文", "如上所述", "把最后一段写得更详细", "扩写本节", "更详细"):
            self.assertNotIn(padding, blob, "补深契约不得退化成注水指令")

    def test_r2_p02_09_unit_budget_is_page_anchored_and_capped(self):
        """R2-P0-2⑨（§16）：一个补充单元锚定一页；本节差额再大也不超过两页。"""
        at_target = plan_depth_supplement(_contract(), _gate(), focus=_focus_rows(3))
        self.assertTrue(all(row["target_budget"]["effective_chars"] == SUPPLEMENT_UNIT_CHARS
                            for row in at_target),
                        "缺口数就是页数依据，不再按本节契约字数摊薄")
        starved = plan_depth_supplement(
            _contract(target_words={"min": 9000, "target": 11000, "max": 14000}),
            _gate(content_units=600, length_status="SECTION_UNDER_LENGTH"),
            focus=_focus_rows(3))
        self.assertEqual(len(starved), 3)
        for row in starved:
            chars = row["target_budget"]["effective_chars"]
            self.assertLessEqual(chars, 2 * SUPPLEMENT_UNIT_CHARS,
                                 "一次补深最多加两页，否则全书页数会直接冲过区间上限")
            self.assertGreaterEqual(chars, MIN_SUPPLEMENT_CHARS)
            self.assertLess(row["min_budget"]["effective_chars"], chars)
            self.assertLess(chars, row["max_budget"]["effective_chars"])
        self.assertEqual(supplement_unit_count(_gate(), _contract()), 0, "本节不薄，旧口径就什么都补不出来")
        quiet = plan_depth_supplement(_contract(), _gate(), focus=[])
        self.assertTrue(quiet, "没有文档级点名也没有长度差额时仍有下限：一个单元，不是零")
        self.assertTrue(all(row["supplement_reason"] != "CAPABILITY_GAP" for row in quiet))

    def test_r2_p02_10_named_deliverable_keeps_its_catalogue_shape(self):
        """R2-P0-2⑩（§12/§16）：点名的交付物按目录形态计量，不被本节已有写法挤掉。"""
        focus = _focus_rows(3)
        already = focus[0]["archetype"]
        rows = plan_depth_supplement(_contract(), _gate(),
                                     planned_cu_ids=["CU-S04-01-01"],
                                     existing_archetypes=[already], focus=focus)
        self.assertEqual([row["archetype"] for row in rows], [cap["archetype"] for cap in focus],
                         "目录点名了形态，补充契约就必须按那个形态给预算")
        measures = {row["archetype"]: row["target_budget"]["measure"] for row in rows}
        self.assertTrue(all(row["target_budget"]["measure"] == measures[row["archetype"]] for row in rows))

    def test_r2_p02_10b_unnamed_gap_avoids_existing_shapes_and_narrative(self):
        """R2-P0-2⑩b：缺口没点形态时才让位，且不会补成"又一段话"。"""
        rows = plan_depth_supplement(_contract(), _gate(), planned_cu_ids=["CU-S04-01-01"],
                                     existing_archetypes=["SOP", "PROCESS"],
                                     focus=[{"reason": "DEPTH_DIMENSION_MISSING",
                                             "topic": "秩序维护人员排班与换班安排"}])
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0]["archetype"], CONTENT_ARCHETYPES)
        self.assertNotIn(rows[0]["archetype"], {"SOP", "PROCESS", "NARRATIVE"})


class RepairPlanCapabilitySourceTests(unittest.TestCase):
    """§12-§17 的第二段：文档级 QA 事实 → 带优先级的补深候选。"""

    SIDS = ("S04-01", "S03-02")

    def _qa(self, *, rendered: int | None = 207, page_targets=(250, 320)) -> dict:
        qa: dict = {
            "artifact": {"sections": [
                _section_word_shape("S04-01", "设施设备运行维护方案", FAT),
                _section_word_shape("S03-02", "客户服务与投诉处理安排", FAT),
            ]},
            "depth": {"status": "PASS", "findings": [], "metrics": {"missing_sections": []}},
            "document_scale": {"status": "PASS", "findings": [], "page_targets": list(page_targets)},
            "coverage_gate": {"status": "PASS", "missing": []},
            "consistency": {"findings": []},
            "bidder_evidence_gate": {"findings": []},
            "content_budget_quality": {"findings": []},
            "local_normalization": {"findings": []},
            "rendered_pages": 183,
        }
        if rendered is not None:
            qa["DRAFT_RENDERED_PAGES"] = rendered
        return qa

    def _word(self) -> dict:
        return {
            "section_states": {sid: "COMPLETED" for sid in self.SIDS},
            "SCOPE_SECTION_IDS": list(self.SIDS),
            # Round 1 的真实情形：没有任何一节判为 UNDER_LENGTH，旧口径因此候选为空。
            "DEPTH_GAP_SECTIONS": [],
            "TECHNICAL_FAILED_SECTIONS": [],
            "SECTION_CU_ARCHETYPES": {sid: ["NARRATIVE"] for sid in self.SIDS},
        }

    def _contracts(self) -> list[dict]:
        return [_contract(), _contract(section_id="S03-02", section_title="客户服务与投诉处理安排")]

    def _plan(self, run_dir: Path, **kwargs) -> dict:
        return build_repair_plan(qa=self._qa(**kwargs), word=self._word(),
                                 contracts=self._contracts(), requirement_matrix={"matrix": []},
                                 run_dir=run_dir)

    @staticmethod
    def _capability_rows(plan: dict) -> list[dict]:
        return [row for row in plan["supplement"] if row["failure_class"] == "CAPABILITY_GAP"]

    def test_r2_p02_11_round1_condition_now_yields_supplement_candidates(self):
        """R2-P0-2⑪（§17/§40）：复刻"零 UNDER_LENGTH"的上一轮条件，候选必须非空。"""
        with tempfile.TemporaryDirectory() as raw:
            plan = self._plan(Path(raw))
        capability = self._capability_rows(plan)
        self.assertTrue(plan["supplement"], "补深候选不能再只有 UNDER_LENGTH 一个来源")
        self.assertEqual(len(capability), len(plan["supplement"]))
        self.assertEqual(plan["counts"]["capability_gap_candidates"], len(capability))
        gaps = plan["content_capability_gaps"]
        self.assertEqual(gaps["status"], STATUS_GAP)
        self.assertEqual(gaps["page_gap"], 43)
        for row in capability:
            self.assertEqual(row["mode"], MODE_SUPPLEMENT)
            self.assertEqual(row["priority"], PRIORITY_DEPTH_GAP)
            self.assertTrue(row["issue_key"].startswith("CAPABILITY:"))
            self.assertTrue(1 <= len(row["focus"]) <= MAX_CAPABILITIES_PER_SECTION)
            for item in row["focus"]:
                self.assertTrue(item["topic"] and item["required_output"]
                                and item["archetype"] and item["novelty_basis"],
                                "候选必须自带能落到纸面的交付物，否则补出来还是「更详细一点」")
        sids = [row["section_id"] for row in capability]
        self.assertEqual(len(sids), len(set(sids)), "同一节的多项缺口合成一条候选，不重复消耗预算")
        keys = [row["issue_key"] for row in plan["repair"] + plan["supplement"]]
        self.assertEqual(len(keys), len(set(keys)), "同一 issue_key 不得出现两次")

    def test_r2_p02_12_measured_pages_are_preferred_over_estimates(self):
        """R2-P0-2⑫（§16）：页面缺口按初稿实测算；没有实测就如实标 ESTIMATED。"""
        with tempfile.TemporaryDirectory() as raw:
            measured = self._plan(Path(raw), rendered=207)
            estimated = self._plan(Path(raw), rendered=None)
        self.assertEqual(measured["content_capability_gaps"]["page_basis"], "RENDERED")
        self.assertEqual(measured["content_capability_gaps"]["arrived_pages"], 207)
        self.assertEqual(measured["content_capability_gaps"]["page_gap"], 43)
        self.assertEqual(estimated["content_capability_gaps"]["page_basis"], "ESTIMATED")
        self.assertEqual(estimated["content_capability_gaps"]["arrived_pages"], 183)
        self.assertEqual(estimated["content_capability_gaps"]["page_gap"], 67,
                         "估算口径必须如实记录，不能把 900 字/页的估算当实测")

    def test_r2_p02_13_no_page_gap_means_no_capability_candidates(self):
        """R2-P0-2⑬：初稿已达到目标下沿就不再产补深候选（§16 停止条件）。"""
        with tempfile.TemporaryDirectory() as raw:
            plan = self._plan(Path(raw), rendered=252)
        self.assertEqual(plan["content_capability_gaps"]["page_gap"], 0)
        self.assertEqual(self._capability_rows(plan), [])
        self.assertEqual(plan["content_capability_gaps"]["status"], STATUS_TARGET_MET)

    def test_r2_p02_14_capability_candidates_rank_below_coverage_gaps(self):
        """R2-P0-2⑭：预算不够时先补关键需求缺口，页面缺口排队在后。"""
        qa = self._qa()
        qa["coverage_gate"] = {"status": "FAIL", "missing": ["REQ-0001"]}
        with tempfile.TemporaryDirectory() as raw:
            plan = build_repair_plan(
                qa=qa, word=self._word(), contracts=self._contracts(),
                requirement_matrix={"matrix": [{"requirement_id": "REQ-0001",
                                                "primary_section_id": "S04-01",
                                                "normalized_requirement": "设备设施巡检与维保"}]},
                run_dir=Path(raw))
        kinds = [row["failure_class"] for row in plan["supplement"]]
        self.assertIn("REQUIREMENT_GAP", kinds)
        self.assertIn("CAPABILITY_GAP", kinds)
        self.assertLess(kinds.index("REQUIREMENT_GAP"), kinds.index("CAPABILITY_GAP"),
                        "关键需求缺口必须排在页面缺口前面拿到调用")
        ranks = [row["priority_rank"] for row in plan["supplement"]]
        self.assertEqual(ranks, sorted(ranks), "候选队列按优先级排队")


class SupplementBudgetTests(unittest.TestCase):
    def test_r2_p02_15_supplement_has_its_own_ceiling(self):
        """R2-P0-2⑮（§16）：补深不被修复预算饿死，自身有 ≤16 次硬上限。"""
        budget = GlobalRepairBudget(initial_calls=48)
        self.assertEqual(budget.max_calls, 10)
        self.assertEqual(budget.supplement_max_calls, 15)
        self.assertEqual(budget.supplement_formula, "min(16, ceil(initial_calls * 0.3))")
        self.assertLessEqual(budget.supplement_max_calls, DEFAULT_SUPPLEMENT_CEILING)
        self.assertEqual(GlobalRepairBudget.derive_supplement(400), 16, "大运行由上限截断，不随规模线性放大")
        self.assertEqual(GlobalRepairBudget.derive_supplement(0), 0)
        for index in range(20):
            key = f"BLOCK:S{index:02d}:MISSING"
            if budget.allow(key, priority=PRIORITY_DEPTH_GAP)[0]:
                budget.note_attempt(key)
        self.assertEqual(budget.repair_used, 10)
        self.assertEqual(budget.remaining, 0)
        self.assertEqual(budget.allow("BLOCK:S99:MISSING", priority=PRIORITY_DEPTH_GAP),
                         (False, "GLOBAL_REPAIR_BUDGET_EXHAUSTED"))
        allowed, reason = budget.allow("CAPABILITY:S04-01", priority=PRIORITY_DEPTH_GAP, kind="supplement")
        self.assertTrue(allowed, "修复预算耗尽不得顺带取消补深")
        self.assertEqual(reason, "ALLOWED")
        while budget.supplement_remaining:
            budget.note_attempt(f"CAPABILITY:S{budget.supplement_used:02d}", kind="supplement")
        self.assertEqual(budget.allow("CAPABILITY:LATE", priority=PRIORITY_DEPTH_GAP, kind="supplement"),
                         (False, "SUPPLEMENT_BUDGET_EXHAUSTED"))
        self.assertEqual(budget.provider_used, budget.repair_used + budget.supplement_used)
        audit = budget.audit()
        self.assertEqual(audit["max_supplement_calls"], 15)
        self.assertEqual(audit["supplement_calls_used"], 15)
        self.assertEqual(audit["supplement_remaining_calls"], 0)
        self.assertEqual(audit["provider_calls_used"], 25)

    def test_r2_p02_16_same_issue_is_never_supplemented_twice(self):
        """R2-P0-2⑯：同一缺口一次运行内最多自动补一次，补深也只跑一轮。"""
        budget = GlobalRepairBudget(initial_calls=48)
        self.assertTrue(budget.allow("CAPABILITY:S04-01", priority=PRIORITY_DEPTH_GAP, kind="supplement")[0])
        budget.note_attempt("CAPABILITY:S04-01", kind="supplement")
        self.assertEqual(budget.allow("CAPABILITY:S04-01", priority=PRIORITY_DEPTH_GAP, kind="supplement"),
                         (False, "ISSUE_ALREADY_REPAIRED"))
        budget.stop("SUPPLEMENT_BUDGET_EXHAUSTED")
        self.assertEqual(budget.allow("CAPABILITY:S05-01", priority=PRIORITY_DEPTH_GAP, kind="supplement"),
                         (False, "SUPPLEMENT_BUDGET_EXHAUSTED"))
        self.assertFalse(budget.allow("REQ-01", priority=PRIORITY_DEPTH_GAP)[0])


if __name__ == "__main__":
    unittest.main()
