# -*- coding: utf-8 -*-
"""MMF-007C Round 4.4：Technical Scoring 的 **Topic 覆盖完整性**（Codex 最新 Final Gate）。

Codex 的反例事实：抽取适用技术主题 2 个、矩阵适用技术主题 2 个、
`canonical_scoring_records` 只有 1 条，而那条的 Section/CU 全都合法 —— 上一轮收口的
Authority 却报 `PASS`、`may_enter_generation=true`，终态因此拿得到 `COMPLETED_CLEAN`。
它证明的只是"已经存在的记录引用合法"，没证明"该映射的主题都映射了"。

本文件按任务书 §7 的 T1–T7 逐条钉死，并且一律走**生产入口**
（`require_pre_generation_scoring_mapping_gate` / 真实 `generate_longform` + Mock 观察者）
与**正式落盘边界**（`finalize_delivery_state` / `guard_clean_claim`），不测 helper 旁路：

  T1 期望 {A,B}、记录只有 {A}（A 完全合法）⇒ 必须 BLOCK；
  T2 期望 {A,B}、记录 {A,B}、引用齐 ⇒ 必须 PASS；
  T3 记录里多出宇宙外的主题 X ⇒ 必须 BLOCK；
  T4 两条记录都映射 A（条数 == 期望数）⇒ 仍必须 BLOCK，缺 B —— 覆盖按**身份集合**算；
  T5 零适用主题 ⇒ 合法 N/A 继续放行（§5 的反方向不许误杀）；
  T6 QA PASS + 286 页在带内 + 缺一整主题 ⇒ Authority BLOCK、入口 BLOCK、
     Provider 调用 0、终态绝不得 `COMPLETED_CLEAN`；
  T7 完整覆盖 + QA PASS + 286 页在带内 ⇒ 合法 Clean 仍然拿得到（反向守卫：不许假 blocker）。

外加 §4 的三层对账（抽取/矩阵/记录）与 §6 的结构哨兵：覆盖算法只准有一份，
住在 Authority 里，终态只消费读数。零 Provider 调用：纯本地临时目录 + Mock 夹具。
"""
from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from longform.orchestrator import (  # noqa: E402
    PreGenerationScoringGateError, generate_longform,
    require_pre_generation_scoring_mapping_gate,
)
from longform.run_state import (  # noqa: E402
    finalize_delivery_state,
)
from longform.scoring_authority import (  # noqa: E402
    canonical_scoring_authority, canonical_topic_coverage,
)
from planning.scoring_taxonomy import display_topic, topic_key  # noqa: E402
from providers.mock import MockProvider  # noqa: E402
from test_longform_orchestrator import _brief, _pack, _selection  # noqa: E402
from test_mmf007c_r43_scoring_authority_closure import (  # noqa: E402
    HIERARCHY, MATRIX, PLAN_SECTIONS, PLAN_UNITS, STATE_09, SURFACES, _deliverable_run,
    _forged_clean, _gate_payload, _hierarchy, _matrix_payload, _planned_run, _write,
)

TOPIC_A = "安全秩序管理含门岗巡逻与异常处置"
TOPIC_B = "设备设施运行维护与保养方案"
TOPIC_X = "智慧物业平台建设投入承诺"
KEY_A, KEY_B, KEY_X = topic_key(TOPIC_A), topic_key(TOPIC_B), topic_key(TOPIC_X)
COVERAGE_BLOCKER = "SCORING_AUTHORITY_TOPIC_COVERAGE_INCOMPLETE"


def _row(index: int, label: str, **over) -> dict:
    """一行矩阵评分项：默认就是过了 Technical-Only scope gate 的技术实质主题。"""
    row = {"scoring_item_id": f"SCR-{index:02d}", "label": label, "must_respond": True,
           "scoring_class": "TECHNICAL_SUBSTANTIVE", "scope_class": "TECHNICAL_INCLUDED",
           "primary_section_id": PLAN_SECTIONS[0], "requirement_ids": ["REQ-0002"],
           "planned_response_cu": list(PLAN_UNITS[:1])}
    row.update(over)
    return row


def _mapped(index: int, label: str, *, section: str = PLAN_SECTIONS[0],
            cu: tuple[str, ...] = PLAN_UNITS[:1]) -> dict:
    """一条 canonical 映射记录：主题身份带 `topic_key`（与规划链同一把尺）。"""
    return {"canonical_scoring_id": f"SCN-{index:03d}", "source_ids": [f"SCR-{index:02d}"],
            "topic": display_topic(label), "topic_key": topic_key(label),
            "planned_response_section": section, "planned_response_sections": [section],
            "planned_response_cu": list(cu), "cu_binding": {"basis": "EXPLICIT_SCORING_ID"},
            "evidence_contract": {"concepts": ["覆盖范围", "响应时限"], "min_concepts": 2}}


def _items(labels: list[str]) -> list[dict]:
    return [{"scoring_item_id": f"SCR-{n:02d}", "must_respond": True, "label": label}
            for n, label in enumerate(labels, start=1)]


def _run(root: Path, *, rows: list[dict], records: list[dict], pack_items: list[str],
         deliverable: bool = False, verdict: str = "PASS") -> Path:
    """按事实落一份 Run 盘：09（行 + 记录 + 嵌门）/ 08（计划树）/ 抽取层 pack。

    嵌进去的门结论一律"自洽"（topics == 记录条数、引用可查），
    这样挡下来的原因只可能是本轮新增的**覆盖完整性**，不是上一轮已经关掉的形状。
    """
    def build(target: Path) -> Path:
        _write(target / MATRIX, _matrix_payload(records, rows=rows, verdict=verdict))
        _write(target / HIERARCHY, _hierarchy())
        pack = _pack()
        pack["scoring_items"] = _items(pack_items)
        _write(target / "tender" / "requirement_pack.json", pack)
        return target

    return build(_deliverable_run(root, matrix=None, derived_gate=None) if deliverable
                 else root / "run")


def _truncate_records(run: Path, *, keep: int) -> dict:
    """把真规划链写出的 canonical 记录砍到 `keep` 条，门结论同步改成"自洽"。

    砍完之后的盘上形状与 Codex 反例逐字对应：矩阵里适用主题还是那么多、记录少了，
    而嵌在矩阵里的那份门结论仍然按"记录条数"评估并写着 PASS —— 它没撒谎，只是问错了题。
    """
    matrix = json.loads((run / MATRIX).read_text(encoding="utf-8-sig"))
    records = matrix.get("canonical_scoring_records") or []
    if len(records) <= keep:
        raise AssertionError(f"夹具需要至少 {keep + 1} 条 canonical 记录，实得 {len(records)}")
    matrix["canonical_scoring_records"] = records[:keep]
    matrix["pre_generation_scoring_mapping_gate"] = _gate_payload(keep, "PASS")
    _write(run / MATRIX, matrix)
    return matrix


class T1CodexCounterexampleBlocks(unittest.TestCase):
    """§1/§7-T1：适用主题 2 个、记录 1 条且那条完全合法 ⇒ 不许 PASS。"""

    def test_t1_authority_blocks_missing_topic_and_names_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _run(Path(tmp), rows=[_row(1, TOPIC_A), _row(2, TOPIC_B)],
                       records=[_mapped(1, TOPIC_A)], pack_items=[TOPIC_A, TOPIC_B])
            reading = canonical_scoring_authority(run)
        self.assertFalse(reading["may_enter_generation"],
                         "缺一整个适用主题还是放行了（Codex 反例未关）")
        self.assertEqual("SCORING_MAPPING_TOPIC_COVERAGE_INCOMPLETE", reading["status"])
        self.assertEqual(2, reading["topic_expected_count"])
        self.assertEqual(1, reading["topic_mapped_count"])
        self.assertEqual([KEY_B], reading["topic_missing_ids"])
        self.assertIn(COVERAGE_BLOCKER, reading["clean_blockers"])
        # §3：expected/mapped/missing/extra 必须显式在场，不能只给一个布尔
        self.assertEqual("CANONICAL_MATRIX_ROWS", reading["topic_expected_basis"])

    def test_t1_reason_says_valid_references_are_not_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _run(Path(tmp), rows=[_row(1, TOPIC_A), _row(2, TOPIC_B)],
                       records=[_mapped(1, TOPIC_A)], pack_items=[TOPIC_A, TOPIC_B])
            reading = canonical_scoring_authority(run)
        self.assertIn("引用都合法", reading["reason"])
        self.assertIn("不等于", reading["reason"])

    def test_t1_production_entry_raises_with_zero_provider_calls(self):
        """在**真规划链**的产物上复现 Codex 形状：规划算出 2 个主题，落盘记录只剩 1 条。"""
        provider = MockProvider({"provider_name": "mock", "provider_type": "mock",
                                 "enabled": True, "model": "mock-longform"})
        with tempfile.TemporaryDirectory() as tmp:
            run = _planned_run(Path(tmp), scoring_items=_items([TOPIC_A, TOPIC_B]))
            matrix = _truncate_records(run, keep=1)
            self.assertEqual(2, len(matrix["rows"]), "夹具应当真的带着两个适用主题")
            with self.assertRaises(PreGenerationScoringGateError) as raised:
                generate_longform(run_dir=run, provider=provider, provider_name="mock",
                                  brief=_brief(), selection=_selection(),
                                  selected_ids=["KU-0001"], task_mode="standard")
        self.assertEqual([], getattr(provider, "received_requests") or [],
                         "覆盖不完整还是在生成前把请求发给了 Provider")
        self.assertIn("本轮未发起任何 Provider 调用", str(raised.exception))
        self.assertIn("缺 1 个", str(raised.exception))


class T2CompleteCoverageEnters(unittest.TestCase):
    """§7-T2：期望 == 已映射、引用齐 ⇒ 必须放行（不许把收紧变成一律挡）。"""

    def test_t2_two_topics_two_records_enters(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _run(Path(tmp), rows=[_row(1, TOPIC_A), _row(2, TOPIC_B)],
                       records=[_mapped(1, TOPIC_A),
                                _mapped(2, TOPIC_B, section=PLAN_SECTIONS[1],
                                        cu=PLAN_UNITS[1:])],
                       pack_items=[TOPIC_A, TOPIC_B])
            reading = canonical_scoring_authority(run)
            entry = require_pre_generation_scoring_mapping_gate(run)
        self.assertTrue(reading["may_enter_generation"], reading["reason"])
        self.assertEqual("PASS", reading["status"])
        self.assertEqual([], reading["clean_blockers"])
        self.assertEqual([], reading["topic_missing_ids"])
        self.assertEqual([], reading["topic_extra_ids"])
        self.assertIn("覆盖完整", reading["reason"])
        self.assertEqual(reading["status"], entry["status"])
        self.assertTrue(entry["verified_against"][MATRIX], "PASS 没有绑定当前工件")


    def test_t2_real_planning_chain_with_two_topics_enters(self):
        """生产链正例：真规划链自己写出的 2 主题/2 记录必须放行（反向守卫）。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _planned_run(Path(tmp), scoring_items=_items([TOPIC_A, TOPIC_B]))
            reading = canonical_scoring_authority(run)
        self.assertEqual(2, reading["topic_expected_count"], reading["reason"])
        self.assertEqual(2, reading["topic_mapped_count"], reading["topic_missing_ids"])
        self.assertTrue(reading["may_enter_generation"], reading["reason"])

    def test_t2_truncating_one_record_on_the_real_chain_is_what_blocks(self):
        """同一条链、只把落盘记录砍掉一条 ⇒ 立刻 BLOCK：差的确实是覆盖，不是夹具。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _planned_run(Path(tmp), scoring_items=_items([TOPIC_A, TOPIC_B]))
            _truncate_records(run, keep=1)
            reading = canonical_scoring_authority(run)
        self.assertFalse(reading["may_enter_generation"])
        self.assertEqual("SCORING_MAPPING_TOPIC_COVERAGE_INCOMPLETE", reading["status"])
        self.assertEqual(1, reading["topic_mapped_count"])


class T3ExtraUnknownTopicBlocks(unittest.TestCase):
    """§7-T3：记录里冒出宇宙外的主题 ⇒ 不许 PASS。"""

    def test_t3_record_outside_applicable_universe_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _run(Path(tmp), rows=[_row(1, TOPIC_A)],
                       records=[_mapped(1, TOPIC_A), _mapped(2, TOPIC_X)],
                       pack_items=[TOPIC_A])
            reading = canonical_scoring_authority(run)
        self.assertFalse(reading["may_enter_generation"])
        self.assertEqual("SCORING_MAPPING_TOPIC_NOT_IN_UNIVERSE", reading["status"])
        self.assertEqual([KEY_X], reading["topic_extra_ids"])
        self.assertIn("SCORING_AUTHORITY_TOPIC_NOT_IN_UNIVERSE", reading["clean_blockers"])


class T4DuplicateMaskingBlocks(unittest.TestCase):
    """§7-T4：两条记录都映射同一主题 —— 条数相等不算覆盖，按身份集合算。"""

    def test_t4_row_count_equals_expected_still_blocks_when_identity_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _run(Path(tmp), rows=[_row(1, TOPIC_A), _row(2, TOPIC_B)],
                       records=[_mapped(1, TOPIC_A), _mapped(2, TOPIC_A)],
                       pack_items=[TOPIC_A, TOPIC_B])
            reading = canonical_scoring_authority(run)
        self.assertEqual(2, reading["canonical_topic_sources"]["CANONICAL_MATRIX_RECORDS"],
                         "夹具应当就是「两条记录对两个主题」，否则这条测试是空的")
        self.assertFalse(reading["may_enter_generation"],
                         "同一主题记两条就冒充覆盖了两个主题")
        self.assertEqual(1, reading["topic_mapped_count"], "mapped 必须按身份集去重")
        self.assertEqual([KEY_B], reading["topic_missing_ids"])
        self.assertEqual("SCORING_MAPPING_TOPIC_COVERAGE_INCOMPLETE", reading["status"])


class T5LegitimateZeroTopicStillNotApplicable(unittest.TestCase):
    """§5：只有宇宙算出来是 0 才允许 N/A，且这一档**不许**被本轮收紧误杀。"""

    def test_t5_zero_topic_matrix_remains_legitimate_na_and_enters(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _run(Path(tmp), rows=[], records=[], pack_items=[])
            reading = canonical_scoring_authority(run)
        self.assertEqual("NOT_APPLICABLE", reading["status"])
        self.assertTrue(reading["may_enter_generation"], reading["reason"])
        self.assertEqual(0, reading["topic_expected_count"])
        self.assertEqual(0, reading["topic_mapped_count"])


class T5bExpectedPositiveNeverReadsAsNotApplicable(unittest.TestCase):
    """§5：expected>0 而一条都没映射 ⇒ 是覆盖不完整，绝不能读成 N/A。"""

    def test_t5b_empty_records_against_two_topics_is_not_na(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _run(Path(tmp), rows=[_row(1, TOPIC_A), _row(2, TOPIC_B)], records=[],
                       pack_items=[TOPIC_A, TOPIC_B], verdict="NOT_APPLICABLE")
            reading = canonical_scoring_authority(run)
        self.assertNotEqual("NOT_APPLICABLE", reading["status"])
        self.assertFalse(reading["may_enter_generation"])
        self.assertEqual(2, reading["topic_expected_count"])
        self.assertEqual(0, reading["topic_mapped_count"])
        self.assertEqual(sorted([KEY_A, KEY_B]), sorted(reading["topic_missing_ids"]))
        self.assertIn(COVERAGE_BLOCKER, reading["clean_blockers"],
                      "空记录被挡时，终态原因码里要看得见「缺一整主题」这件事")


class T6FinalizerNeverCleanWithMissingTopic(unittest.TestCase):
    """§6/§7-T6：终态不改逻辑，只因为 Authority 修对了就自动拿不到 Clean。"""

    def _run(self, root: Path) -> Path:
        # 交付夹具里抽取层只放主题 A：期望宇宙由矩阵行给出（{A,B}），
        # 抽取 ⊆ 期望 ⇒ 分歧可解释，挡下来的原因只可能是"缺一整主题"这一条。
        return _run(root, rows=[_row(1, TOPIC_A), _row(2, TOPIC_B)],
                    records=[_mapped(1, TOPIC_A)], pack_items=[TOPIC_A], deliverable=True)

    def test_t6_formal_finalize_is_not_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._run(Path(tmp))
            state = finalize_delivery_state(run, medium="WORD", draft_delivery="COMPLETED_CLEAN")
        derived = state["derived"]
        self.assertEqual("QA_PASS", derived["final_qa_state"])
        self.assertEqual("TARGET_RANGE", derived["page_band"])
        self.assertEqual(286, derived["page_band_pages"])
        self.assertNotEqual("COMPLETED_CLEAN", derived["delivery_status"],
                            derived["final_qa_blockers"])
        self.assertEqual("SCORING_MAPPING_TOPIC_COVERAGE_INCOMPLETE",
                         derived["scoring_authority_status"])
        self.assertIn(COVERAGE_BLOCKER, derived["final_qa_blockers"])

    def test_t6_forged_clean_fails_state09_before_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._run(Path(tmp))
            forged = _forged_clean(run)
        self.assertEqual("FAIL", forged["invariants"][STATE_09])
        self.assertEqual("NEEDS_REVIEW", forged["delivery_status"],
                         "硬写的 Clean 没有在投影与落盘之前被拦下")
        self.assertIn(COVERAGE_BLOCKER, forged["final_qa_blockers"])

    def test_t6_clean_and_incomplete_coverage_never_coexist_on_surfaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._run(Path(tmp))
            finalize_delivery_state(run, medium="WORD", draft_delivery="COMPLETED_CLEAN")
            surfaces = {name: json.loads((run / name).read_text(encoding="utf-8-sig"))
                        for name in SURFACES if (run / name).is_file()}
        self.assertTrue(surfaces, "六面投影一个都没落盘，这条测试就成了空的")
        for name, row in surfaces.items():
            values = {str(row.get("delivery_status") or ""),
                      str((row.get("result") or {}).get("delivery_status") or ""),
                      str(row.get("authority_delivery_status") or "")}
            self.assertNotIn("COMPLETED_CLEAN", values - {""},
                             f"{name} 落着 COMPLETED_CLEAN，而主题覆盖是不完整的")


class T7LegitimateCleanStillReachable(unittest.TestCase):
    """§7-T7（反向守卫）：完整覆盖 + QA PASS + 286 页在带内 ⇒ Clean 必须仍然拿得到。"""

    def _run(self, root: Path) -> Path:
        return _run(root, rows=[_row(1, TOPIC_A), _row(2, TOPIC_B)],
                    records=[_mapped(1, TOPIC_A),
                             _mapped(2, TOPIC_B, section=PLAN_SECTIONS[1], cu=PLAN_UNITS[1:])],
                    pack_items=[TOPIC_A, TOPIC_B], deliverable=True)

    def test_t7_complete_coverage_delivers_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._run(Path(tmp))
            state = finalize_delivery_state(run, medium="WORD", draft_delivery="COMPLETED_CLEAN")
            derived = state["derived"]
            surfaces = {name: json.loads((run / name).read_text(encoding="utf-8-sig"))
                        for name in SURFACES if (run / name).is_file()}
        self.assertEqual("PASS", derived["scoring_authority_status"], derived["final_qa_blockers"])
        self.assertEqual("COMPLETED_CLEAN", derived["delivery_status"],
                         derived["final_qa_blockers"])
        self.assertEqual([], derived["final_qa_blockers"])
        self.assertEqual("PASS", state["invariants"][STATE_09])
        for name, row in surfaces.items():
            self.assertNotIn("NEEDS_REVIEW",
                             {str(row.get("delivery_status") or ""),
                              (row.get("result") or {}).get("delivery_status")} - {None, ""},
                             f"{name} 没跟上合法 Clean")


class ThreeLayerReconciliation(unittest.TestCase):
    """§4：抽取 / 矩阵 / 记录三层都要被真正消费；对不上时不许猜。"""

    def test_r1_matrix_dropped_a_row_whole_is_contradictory_not_pass(self):
        """矩阵整行丢了（不是 scope gate 排除），抽取还剩两个主题。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _run(Path(tmp), rows=[_row(1, TOPIC_A)], records=[_mapped(1, TOPIC_A)],
                       pack_items=[TOPIC_A, TOPIC_B])
            reading = canonical_scoring_authority(run)
        self.assertFalse(reading["may_enter_generation"],
                         "矩阵与抽取对不上，只凭记录就放行了")
        self.assertEqual("SCORING_MAPPING_TOPIC_UNIVERSE_CONTRADICTORY", reading["status"])
        self.assertEqual([KEY_B], reading["topic_unexplained_extraction_topics"])

    def test_r2_scope_gate_exclusion_is_explained_not_a_new_blocker(self):
        """同一件事被 scope gate 判成商务 ⇒ 分歧能被现有分类器解释，不该由 §4 这一档挡。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _run(Path(tmp),
                       rows=[_row(1, TOPIC_A),
                             _row(2, TOPIC_B, scope_class="COMMERCIAL_EXCLUDED")],
                       records=[_mapped(1, TOPIC_A)], pack_items=[TOPIC_A, TOPIC_B])
            reading = canonical_scoring_authority(run)
        self.assertEqual([], reading["topic_unexplained_extraction_topics"],
                         "被 scope gate 解释掉的分歧，被当成了「矩阵整行丢了」")
        self.assertNotEqual("SCORING_MAPPING_TOPIC_UNIVERSE_CONTRADICTORY", reading["status"])

    def test_r3_universe_comes_from_the_scope_gated_matrix_not_raw_extraction(self):
        """期望宇宙按既有 precedence 取"过了闸的矩阵"，不是 gate 之前的抽取层。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _run(Path(tmp),
                       rows=[_row(1, TOPIC_A),
                             _row(2, TOPIC_B, scope_class="PRICING_EXCLUDED")],
                       records=[_mapped(1, TOPIC_A)], pack_items=[TOPIC_A, TOPIC_B])
            reading = canonical_scoring_authority(run)
        self.assertEqual("CANONICAL_MATRIX_ROWS", reading["topic_expected_basis"])
        self.assertEqual(1, reading["topic_expected_count"],
                         "把 gate 之前的抽取条数当技术主题宇宙 = 系统性过量拦截")

    def test_r4_nothing_to_check_because_inputs_unreadable_is_not_pass(self):
        """有记录却核不出宇宙（既无矩阵行、抽取也读不到）⇒ 不可核实，不是"覆盖齐了"。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _run(Path(tmp), rows=[], records=[_mapped(1, TOPIC_A)], pack_items=[])
            reading = canonical_scoring_authority(run)
        self.assertFalse(reading["may_enter_generation"])
        self.assertEqual("SCORING_MAPPING_TOPIC_UNIVERSE_UNVERIFIABLE", reading["status"])
        self.assertFalse(reading["topic_universe_verifiable"])


class CoverageFactsTravelWithEveryReading(unittest.TestCase):
    """§3：覆盖读数在**每一种**结论里都在场，不只在新挡下来的那几种里。"""

    def test_facts_present_even_when_blocked_for_an_earlier_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _run(Path(tmp), rows=[_row(1, TOPIC_A), _row(2, TOPIC_B)],
                       records=[_mapped(1, TOPIC_A, section="S99-99")],
                       pack_items=[TOPIC_A, TOPIC_B])
            reading = canonical_scoring_authority(run)
        # 落点本身是幽灵 ⇒ 先报那个更具体的原因，但覆盖的数一样要看得见
        self.assertEqual("BLOCKED_SCORING_MAPPING_INCOMPLETE", reading["status"])
        self.assertEqual(2, reading["topic_expected_count"])
        self.assertEqual(1, reading["topic_mapped_count"])
        self.assertEqual([KEY_B], reading["topic_missing_ids"])
        self.assertIn("SCORING_AUTHORITY_INVALID_REFERENCE", reading["clean_blockers"])


class CoverageAuthorityOwnership(unittest.TestCase):
    """§6：一份 Authority、两处消费 —— 覆盖算法不许在终态那一侧再写一遍。"""

    def test_finalizer_never_implements_topic_coverage(self):
        tree = ast.parse((APP / "longform" / "run_state.py").read_text(encoding="utf-8"))
        source = (APP / "longform" / "run_state.py").read_text(encoding="utf-8")
        names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        self.assertNotIn("canonical_topic_coverage", names)
        for token in ("topic_missing_ids", "canonical_topic_coverage", "build_scoring_taxonomy",
                      "topic_key", "TOPIC_MERGE_SIMILARITY"):
            self.assertNotIn(token, source,
                             f"终态里出现了 {token}：覆盖判定又被实现了一遍（§6 禁止）")
        self.assertIn("canonical_scoring_authority", source,
                      "终态不再消费同一个 Authority，才是真正的问题")

    def test_coverage_engine_lives_only_in_the_authority_module(self):
        coverage_source = (APP / "longform" / "scoring_authority.py").read_text(encoding="utf-8")
        self.assertIn("def canonical_topic_coverage", coverage_source)
        # §3：主题身份/别名一律复用 canonical 那把尺，不自建第二套归一化
        self.assertIn("from planning.scoring_taxonomy import", coverage_source)
        for borrowed in ("topic_key", "topic_similarity", "TOPIC_MERGE_SIMILARITY",
                         "MIN_MERGE_TOPIC_CHARS", "build_scoring_taxonomy"):
            self.assertIn(borrowed, coverage_source, f"没有复用 canonical 的 {borrowed}")
        for forbidden in ("def topic_key", "def topic_similarity", "def normalize_topic",
                          "def _dedup_topics"):
            self.assertNotIn(forbidden, coverage_source,
                             f"Authority 里另起了第二套主题归一化：{forbidden}")

    def test_orchestrator_entry_delegates_to_the_same_authority(self):
        source = (APP / "longform" / "orchestrator.py").read_text(encoding="utf-8")
        self.assertIn("canonical_scoring_authority(run_dir)", source)
        for forbidden in ("def canonical_topic_coverage", "build_scoring_taxonomy("):
            self.assertNotIn(forbidden, source,
                             f"生成入口里又长出一套覆盖判定：{forbidden}")


class PreservedR43ScoringAttacks(unittest.TestCase):
    """上一轮已绿的反例不许因为本轮收紧而变形（每条都仍然只被挡一次、名字照旧）。"""

    def _read(self, rows: list[dict], records: list[dict], items: list[str]) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            return canonical_scoring_authority(
                _run(Path(tmp), rows=rows, records=records, pack_items=items))

    def test_derived_gate_file_self_pass_is_still_not_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _run(Path(tmp), rows=[_row(1, TOPIC_A)], records=[_mapped(1, TOPIC_A)],
                       pack_items=[TOPIC_A])
            (run / MATRIX).unlink()
            _write(run / "pre_generation_scoring_mapping_gate.json", _gate_payload(1, "PASS"))
            reading = canonical_scoring_authority(run)
        self.assertFalse(reading["may_enter_generation"])
        self.assertEqual("SCORING_MAPPING_ARTIFACT_MISSING", reading["status"])
        self.assertFalse(reading["declared"]["derived_file_honored"])

    def test_ghost_section_reference_still_blocks_before_coverage_words(self):
        reading = self._read([_row(1, TOPIC_A), _row(2, TOPIC_B)],
                             [_mapped(1, TOPIC_A, section="S99-99")], [TOPIC_A, TOPIC_B])
        self.assertFalse(reading["may_enter_generation"])
        self.assertEqual({"sections": ["S99-99"], "content_units": []},
                         reading["invalid_reference"])

    def test_unreadable_matrix_is_still_named_not_treated_as_no_topics(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _run(Path(tmp), rows=[_row(1, TOPIC_A)], records=[_mapped(1, TOPIC_A)],
                       pack_items=[TOPIC_A])
            (run / MATRIX).write_text("{ 坏 JSON", encoding="utf-8")
            reading = canonical_scoring_authority(run)
        self.assertFalse(reading["may_enter_generation"])
        self.assertEqual("SCORING_MAPPING_ARTIFACT_UNREADABLE", reading["status"])

    def test_technical_scoring_leak_shape_still_blocks(self):
        """宇宙里的主题被记成了别的东西 ⇒ 本轮的 extra/leak 两条都必须挡得住。"""
        reading = self._read([_row(1, TOPIC_A), _row(2, TOPIC_B)],
                             [_mapped(1, TOPIC_A), _mapped(2, TOPIC_X)], [TOPIC_A, TOPIC_B])
        self.assertFalse(reading["may_enter_generation"])

    def test_single_topic_valid_fixture_from_r43_still_enters(self):
        reading = self._read([_row(1, TOPIC_A)], [_mapped(1, TOPIC_A)], [TOPIC_A])
        self.assertTrue(reading["may_enter_generation"], reading["reason"])
        self.assertEqual("PASS", reading["status"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
