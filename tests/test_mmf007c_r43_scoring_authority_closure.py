# -*- coding: utf-8 -*-
"""MMF-007C Round 4.3：Technical Scoring Authority 收口（Codex 第二次 Final Gate 的窄范围返修）。

Codex 独立 Final Gate：P0-1 页带 PASS 冻结，P0-2 技术评分 FAIL，P0-3 最终 QA/终态 FAIL
（根因是"评分门没进入 Final Clean Authority"）。本文件按任务书 §13 的 S1–S8 逐条钉死，
并且**一律走生产入口与正式落盘边界**（§17 的第 4、5 步），不走 helper 旁路：

  S1：canonical `09_scoring_matrix.json` 不在场，派生门文件自报 `PASS` ⇒ 必须 BLOCK。
      §4/§5 的 Authority 关系：派生文件的声称**不能**替代对 canonical 输入的重新验证。
  S2：抽取里确实有 1 个技术实质主题，评分状态却自报 `NOT_APPLICABLE` ⇒ 必须 BLOCK。
      §7：N/A 是算出来的事实，不是上游文件想写就写的词。
  S3：零适用主题 ⇒ 合法 N/A 必须继续放行（收紧不许误杀）。
  S4：canonical 矩阵合法、引用齐 ⇒ 允许进生成。
  S5–S7：QA PASS + 实测 286 页在带内 + 评分无效（幽灵落点 / 矩阵缺失+旧 PASS / 假 N/A）
      ⇒ 正式 `finalize_delivery_state` 绝不得落 `COMPLETED_CLEAN`（§9/§10）。
  S8：所有门通过 ⇒ 合法 Clean 仍然拿得到（正例）。

外加 §11/§12 的结构哨兵（终态不得再实现一遍映射算法；Clean 绑定**当前**的 09/08），
§14 的上一轮已 PASS 攻击，§15 的 P0-1 页带（只测不改），§16 的最终 QA 八态。
零 Provider 调用：纯本地临时目录 + Mock 夹具。
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
    read_pre_generation_scoring_mapping_gate, require_pre_generation_scoring_mapping_gate,
    write_plan_artifacts,
)
from longform.run_state import (  # noqa: E402
    QA_PASS, collect_facts, derive_run_state, evaluate_invariants, finalize_delivery_state,
    guard_clean_claim,
)
from longform.scoring_authority import canonical_scoring_authority  # noqa: E402
from planning.planner import build_planning_bundle  # noqa: E402
from planning.scoring_taxonomy import (  # noqa: E402
    COVERAGE_DENOMINATOR,
    SCORING_TAXONOMY_VERSION,
    SCORING_MAPPING_GATE_FILE,
)
from providers.mock import MockProvider  # noqa: E402
from test_longform_orchestrator import _brief, _pack, _selection  # noqa: E402
from test_mmf007c_r4_final_state_contract import _make_run, _write  # noqa: E402

MATRIX = "09_scoring_matrix.json"
HIERARCHY = "08_document_hierarchy.json"
PLAN_SECTIONS = ("S01-01", "S02-01")
PLAN_UNITS = ("CU-001-01", "CU-002-01")
STATE_09 = "STATE-09_clean_implies_canonical_scoring_authority_valid"
SURFACES = ("run_audit.json", "report/run_audit.json", "checkpoint/state.json",
            "volume_progress.json", "11_volume_generation_plan.json",
            "generation_status.json")


def _planned_run(root: Path, *, drop_matrix: bool = False,
                 scoring_items: list | None = None) -> Path:
    """真规划链：pack → planning bundle → `write_plan_artifacts`（08/09/门由生产自己写）。"""
    pack = _pack()
    if scoring_items is not None:
        pack["scoring_items"] = scoring_items
        pack["requirements"] = [row for row in pack["requirements"]
                                if not row.get("scoring_item_id")]
    run = root / "run"
    (run / "tender").mkdir(parents=True)
    _write(run / "tender" / "requirement_pack.json", pack)
    write_plan_artifacts(run, build_planning_bundle(pack, _brief(), _selection(), production=True))
    if drop_matrix and (run / MATRIX).is_file():
        (run / MATRIX).unlink()
    return run


def _record(section: str = "S01-01", cu: tuple[str, ...] = PLAN_UNITS[:1]) -> dict:
    return {"canonical_scoring_id": "SCN-001", "topic": "安全秩序管理含门岗巡逻与异常处置",
            "source_ids": ["SCR-01"], "planned_response_section": section,
            "planned_response_sections": [section], "planned_response_cu": list(cu),
            "cu_binding": {"basis": "EXPLICIT_SCORING_ID"},
            "evidence_contract": {"concepts": ["覆盖范围", "响应时限"], "min_concepts": 2}}


def _gate_payload(topics: int, verdict: str = "PASS") -> dict:
    return {"status": verdict, "may_enter_generation": True, "substantive_topic_total": topics,
            "mapped_total": topics, "unmapped_total": 0, "evaluated_stage": "PRE_GENERATION",
            "schema_version": SCORING_TAXONOMY_VERSION}


def _matrix_payload(records: list, *, coverage_ok: bool = True,
                    verdict: str = "PASS", rows: list | None = None) -> dict:
    payload = {"schema_version": "scoring-matrix-v0.2",
               "rows": rows if rows is not None else ([{
                   "scoring_item_id": "SCR-01", "label": "安全秩序管理含门岗巡逻与异常处置",
                   "must_respond": True, "scoring_class": "TECHNICAL_SUBSTANTIVE",
                   "scope_class": "TECHNICAL_INCLUDED"}]),
               "canonical_scoring_records": records,
               "coverage_summary": {"canonical_scoring_total": len(records),
                                    "coverage_denominator": COVERAGE_DENOMINATOR},
               "pre_generation_scoring_mapping_gate": _gate_payload(len(records), verdict)}
    if coverage_ok:
        # 覆盖率端**通过**：挡这次交付的只可能是 canonical 评分有效性，不是覆盖率。
        payload["scoring_gate"] = {"measured": True, "technical_scoring_coverage": 1.0,
                                   "coverage_denominator": COVERAGE_DENOMINATOR,
                                   "completed_clean_allowed": True,
                                   "min_technical_scoring_coverage": 0.8,
                                   "legacy_row_coverage": 1.0}
    return payload


def _hierarchy(sections: tuple[str, ...] = PLAN_SECTIONS,
               units: tuple[str, ...] = PLAN_UNITS) -> dict:
    return {"schema_version": "document-hierarchy-v0.1",
            "sections": [{"section_id": sid} for sid in sections],
            "content_units": [{"content_unit_id": cu, "section_id": sections[0]} for cu in units]}


def _deliverable_run(root: Path, **scoring) -> Path:
    """一次"内容全齐、QA PASS、实测 286 页在带内"的交付，再叠上给定的评分盘上形状。

    Round 4.4 §4 之后，抽取层与矩阵必须**说同一件事**：给定矩阵行时，pack 的
    `scoring_items` 就按那些行生成。原先这里固定写 `_pack()` 的"服务方案"，而矩阵行写的是
    别的主题 —— 两份 canonical 输入自己对不上，那种夹具拿到的 Clean 从来不是
    "覆盖完整"的 Clean，而是"没人去比对两层"的 Clean。矩阵被删掉（`matrix=None`）时
    抽取层保持原样：那一档要量的就是"该有而没有矩阵"。
    """
    run = _make_run(root, pages=286, page_targets=(250, 320))
    _write(run / HIERARCHY, _hierarchy())
    pack = _pack()
    matrix = scoring.get("matrix")
    if isinstance(matrix, dict) and matrix.get("rows"):
        pack["scoring_items"] = [
            {"scoring_item_id": str(row.get("scoring_item_id") or f"SCR-{n:02d}"),
             "must_respond": True, "label": row.get("label")}
            for n, row in enumerate(matrix["rows"], start=1)
            if isinstance(row, dict)]
    _write(run / "tender" / "requirement_pack.json", pack)
    if "matrix" in scoring:
        matrix = scoring["matrix"]
        if matrix is None:
            (run / MATRIX).unlink(missing_ok=True)
        else:
            _write(run / MATRIX, matrix)
    if "derived_gate" in scoring:
        _write(run / SCORING_MAPPING_GATE_FILE, scoring["derived_gate"])
    return run


def _forged_clean(run: Path) -> dict:
    """把 COMPLETED_CLEAN 硬写进派生结果，看守卫让不让它进投影与落盘（§10）。"""
    facts = collect_facts(run, medium="WORD")
    derived = {**derive_run_state(facts), "delivery_status": "COMPLETED_CLEAN",
               "internal_phase": "COMPLETED", "final_qa_blockers": [],
               "content_gate_blocked": False}
    state = guard_clean_claim({"facts": facts, "derived": derived}, run)
    return {"invariants": state.get("invariants") or {},
            "delivery_status": (state.get("derived") or {}).get("delivery_status"),
            "final_qa_blockers": sorted((state.get("derived") or {}).get("final_qa_blockers") or [])}


class S1DerivedGateFileIsNotAuthority(unittest.TestCase):
    """§3/§4/§5：canonical 矩阵不在场时，派生门文件自报 PASS 不构成放行凭据。"""

    def test_s1_self_reported_pass_without_matrix_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _planned_run(Path(tmp), drop_matrix=True)
            _write(run / SCORING_MAPPING_GATE_FILE, _gate_payload(1, "PASS"))
            reading = read_pre_generation_scoring_mapping_gate(run)
        self.assertFalse(reading["may_enter_generation"],
                         "canonical 矩阵缺失，派生门文件自报 PASS 就把生成放进来了")
        self.assertEqual("SCORING_MAPPING_ARTIFACT_MISSING", reading["status"])
        self.assertEqual(1, reading["canonical_topic_total"], "分母必须由 canonical 抽取算出来")

    def test_s1_derived_claim_is_recorded_but_never_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _planned_run(Path(tmp), drop_matrix=True)
            _write(run / SCORING_MAPPING_GATE_FILE, _gate_payload(1, "PASS"))
            reading = canonical_scoring_authority(run)
        self.assertEqual("PASS", reading["declared"]["derived_file_status"],
                         "声称本身要看得见，否则排查时不知道是谁在误导")
        self.assertFalse(reading["declared"]["derived_file_honored"])
        self.assertEqual(["SCORING_AUTHORITY_MISSING"], reading["clean_blockers"])

    def test_s1_production_entry_makes_zero_provider_calls(self):
        provider = MockProvider({"provider_name": "mock", "provider_type": "mock",
                                 "enabled": True, "model": "mock-longform"})
        with tempfile.TemporaryDirectory() as tmp:
            run = _planned_run(Path(tmp), drop_matrix=True)
            _write(run / SCORING_MAPPING_GATE_FILE, _gate_payload(1, "PASS"))
            with self.assertRaises(PreGenerationScoringGateError) as raised:
                generate_longform(run_dir=run, provider=provider, provider_name="mock",
                                  brief=_brief(), selection=_selection(),
                                  selected_ids=["KU-0001"], task_mode="standard")
        self.assertEqual([], getattr(provider, "received_requests", []),
                         "没有 canonical 矩阵还是把请求发给了 Provider")
        self.assertIn("本轮未发起任何 Provider 调用", str(raised.exception))


class S2FalseNotApplicableBlocks(unittest.TestCase):
    """§6/§7：有真实主题却自报 NOT_APPLICABLE ⇒ stale / contradictory / invalid。"""

    def test_s2_empty_records_against_one_real_topic_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _planned_run(Path(tmp))
            _write(run / MATRIX, _matrix_payload([], verdict="NOT_APPLICABLE"))
            reading = read_pre_generation_scoring_mapping_gate(run)
        self.assertFalse(reading["may_enter_generation"],
                         "1 个真实技术主题摆在那儿，N/A 的声称还是把门开了")
        self.assertEqual("SCORING_MAPPING_FALSE_NOT_APPLICABLE", reading["status"])

    def test_s2_na_derived_file_without_matrix_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _planned_run(Path(tmp), drop_matrix=True)
            _write(run / SCORING_MAPPING_GATE_FILE, _gate_payload(0, "NOT_APPLICABLE"))
            reading = read_pre_generation_scoring_mapping_gate(run)
        self.assertFalse(reading["may_enter_generation"])
        self.assertEqual("SCORING_MAPPING_FALSE_NOT_APPLICABLE", reading["status"])

    def test_s2_matrix_that_dropped_the_only_topic_blocks(self):
        """矩阵的 canonical 记录被清空、但 rows 里那条主题还在：同样是假 N/A。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _planned_run(Path(tmp))
            _write(run / MATRIX, _matrix_payload([], verdict="NOT_APPLICABLE"))
            reading = read_pre_generation_scoring_mapping_gate(run)
        self.assertFalse(reading["may_enter_generation"])
        self.assertEqual(1, reading["canonical_topic_sources"]["CANONICAL_MATRIX_ROWS"],
                         "矩阵行里那条主题必须被算进分母，否则清空记录就等于宣布 N/A")

    def test_s2_entry_makes_zero_provider_calls(self):
        provider = MockProvider({"provider_name": "mock", "provider_type": "mock",
                                 "enabled": True, "model": "mock-longform"})
        with tempfile.TemporaryDirectory() as tmp:
            run = _planned_run(Path(tmp))
            _write(run / MATRIX, _matrix_payload([], verdict="NOT_APPLICABLE"))
            with self.assertRaises(PreGenerationScoringGateError):
                generate_longform(run_dir=run, provider=provider, provider_name="mock",
                                  brief=_brief(), selection=_selection(),
                                  selected_ids=["KU-0001"], task_mode="standard")
        self.assertEqual([], getattr(provider, "received_requests", []))


class S3LegitimateNotApplicableStaysOpen(unittest.TestCase):
    """§7 的反方向：合法 N/A 不得被收紧误杀。"""

    def test_s3_zero_topic_matrix_is_not_applicable(self):
        for rows in ([], None):
            with self.subTest(rows=rows), tempfile.TemporaryDirectory() as tmp:
                run = _planned_run(Path(tmp), scoring_items=[])
                payload = _matrix_payload([], verdict="NOT_APPLICABLE", rows=rows or [])
                _write(run / MATRIX, payload)
                _write(run / SCORING_MAPPING_GATE_FILE, _gate_payload(0, "NOT_APPLICABLE"))
                reading = read_pre_generation_scoring_mapping_gate(run)
            self.assertTrue(reading["may_enter_generation"], reading["reason"])
            self.assertEqual("NOT_APPLICABLE", reading["status"])
            self.assertEqual(0, reading["canonical_topic_total"])

    def test_s3_zero_topic_extraction_without_matrix_is_not_applicable(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _planned_run(Path(tmp), scoring_items=[])
            (run / MATRIX).unlink(missing_ok=True)
            reading = read_pre_generation_scoring_mapping_gate(run)
        self.assertTrue(reading["may_enter_generation"])
        self.assertEqual("NOT_APPLICABLE", reading["status"],
                         "分母由事实算出为 0 时，N/A 是派生结果而不是缺口")

    def test_s3_zero_topic_na_does_not_need_the_plan_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            run.mkdir(parents=True)
            _write(run / MATRIX, _matrix_payload([], verdict="NOT_APPLICABLE", rows=[]))
            reading = read_pre_generation_scoring_mapping_gate(run)
        self.assertTrue(reading["may_enter_generation"], reading["reason"])


class S4CanonicalValidEnters(unittest.TestCase):
    """§8：canonical 输入齐全 + 引用按当前计划重算通过 ⇒ 允许继续生成。"""

    def test_s4_planner_output_enters_and_names_what_it_was_verified_against(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _planned_run(Path(tmp))
            reading = read_pre_generation_scoring_mapping_gate(run)
        self.assertTrue(reading["may_enter_generation"], reading["reason"])
        self.assertEqual("PASS", reading["status"])
        self.assertEqual("RECOMPUTED_FROM_CANONICAL_INPUTS", reading["verdict_basis"])
        for name in (MATRIX, HIERARCHY):
            self.assertNotIn(reading["verified_against"][name], ("", "ABSENT", "UNREADABLE"),
                             f"结论没有绑定当前 {name}，就等于可以继承旧读数")


class S5FinalizerNeverCleanWithInvalidMapping(unittest.TestCase):
    """§9/§10：QA PASS + 286 页在带内也挡不住"评分无效 ⇒ 不得 Clean"。"""

    def test_s5_formal_finalize_downgrades_ghost_section_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _deliverable_run(Path(tmp), matrix=_matrix_payload([_record("S99-99")]),
                                   derived_gate=_gate_payload(1, "PASS"))
            state = finalize_delivery_state(run, medium="WORD", draft_delivery="COMPLETED_CLEAN")
        derived = state["derived"]
        self.assertNotEqual("COMPLETED_CLEAN", derived["delivery_status"])
        self.assertIn("SCORING_AUTHORITY_INVALID_REFERENCE", derived["final_qa_blockers"])
        self.assertEqual("BLOCKED_SCORING_MAPPING_INCOMPLETE",
                         derived["scoring_authority_status"])

    def test_s5_forged_clean_fails_state09_and_is_downgraded_before_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _deliverable_run(Path(tmp), matrix=_matrix_payload([_record("S99-99")]),
                                   derived_gate=_gate_payload(1, "PASS"))
            forged = _forged_clean(run)
        self.assertEqual("FAIL", forged["invariants"][STATE_09])
        self.assertEqual("NEEDS_REVIEW", forged["delivery_status"],
                         "硬写的 Clean 没有在投影之前被拦下")
        self.assertIn("SCORING_AUTHORITY_INVALID_REFERENCE", forged["final_qa_blockers"])

    def test_s5_clean_and_invalid_scoring_never_coexist_on_surfaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _deliverable_run(Path(tmp),
                                   matrix=_matrix_payload([_record("S01-01", cu=("CU-NOPE",))]),
                                   derived_gate=_gate_payload(1, "PASS"))
            finalize_delivery_state(run, medium="WORD", draft_delivery="COMPLETED_CLEAN")
            surfaces = {name: json.loads((run / name).read_text(encoding="utf-8-sig"))
                        for name in SURFACES if (run / name).is_file()}
        for name, row in surfaces.items():
            values = {str(row.get("delivery_status") or ""),
                      str((row.get("result") or {}).get("delivery_status") or ""),
                      str(row.get("authority_delivery_status") or "")}
            self.assertNotIn("COMPLETED_CLEAN", values - {""},
                             f"{name} 上落着 COMPLETED_CLEAN，而评分映射是无效的")


class S6FinalizerMatrixMissingWithStalePass(unittest.TestCase):
    def test_s6_missing_matrix_with_stale_pass_is_not_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _deliverable_run(Path(tmp), matrix=None,
                                   derived_gate=_gate_payload(1, "PASS"))
            state = finalize_delivery_state(run, medium="WORD", draft_delivery="COMPLETED_CLEAN")
        self.assertNotEqual("COMPLETED_CLEAN", state["derived"]["delivery_status"])
        self.assertIn("SCORING_AUTHORITY_MISSING", state["derived"]["final_qa_blockers"])


class S7FinalizerFalseNotApplicable(unittest.TestCase):
    def test_s7_false_na_is_not_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _deliverable_run(Path(tmp),
                                   matrix=_matrix_payload([], verdict="NOT_APPLICABLE"),
                                   derived_gate=None)
            state = finalize_delivery_state(run, medium="WORD", draft_delivery="COMPLETED_CLEAN")
        self.assertNotEqual("COMPLETED_CLEAN", state["derived"]["delivery_status"])
        self.assertIn("SCORING_AUTHORITY_FALSE_NOT_APPLICABLE",
                      state["derived"]["final_qa_blockers"])


class S8LegitimateCleanStillReachable(unittest.TestCase):
    def test_s8_all_canonical_gates_pass_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _deliverable_run(Path(tmp), matrix=_matrix_payload([_record()]), derived_gate=None)
            state = finalize_delivery_state(run, medium="WORD", draft_delivery="COMPLETED_CLEAN")
            derived = state["derived"]
            surfaces = {name: json.loads((run / name).read_text(encoding="utf-8-sig"))
                        for name in SURFACES if (run / name).is_file()}
        self.assertEqual("COMPLETED_CLEAN", derived["delivery_status"],
                         derived["final_qa_blockers"])
        self.assertEqual("PASS", derived["scoring_authority_status"])
        self.assertEqual("PASS", state["invariants"][STATE_09])
        for name, row in surfaces.items():
            self.assertNotIn("NEEDS_REVIEW",
                             {str(row.get("delivery_status") or ""),
                              str((row.get("result") or {}).get("delivery_status") or "")} - {""},
                             f"{name} 没有跟上合法 Clean：六面单一真值漏了")


class ScoringAuthorityBindsToCurrentArtifacts(unittest.TestCase):
    """§12：Final Clean 针对**当前**工件与当前计划，不继承旧读数。"""

    def _clean_state(self, run: Path) -> dict:
        facts = collect_facts(run, medium="WORD")
        return {"facts": facts, "derived": derive_run_state(facts)}

    def test_state09_fails_when_the_matrix_is_replaced_after_derivation(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _deliverable_run(Path(tmp), matrix=_matrix_payload([_record()]))
            state = self._clean_state(run)
            self.assertEqual("COMPLETED_CLEAN", state["derived"]["delivery_status"])
            self.assertEqual("PASS", evaluate_invariants(state, run)[STATE_09])
            # 派生之后矩阵被换成"落点查无此节"：旧结论必须立刻失去效力。
            _write(run / MATRIX, _matrix_payload([_record("S99-99")]))
            invariants = evaluate_invariants(state, run)
        self.assertEqual("FAIL", invariants[STATE_09],
                         "09 被换过之后，按旧矩阵算出来的 Clean 还在盘上站得住")

    def test_state09_fails_when_only_the_identity_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _deliverable_run(Path(tmp), matrix=_matrix_payload([_record()]))
            state = self._clean_state(run)
            matrix = json.loads((run / MATRIX).read_text(encoding="utf-8-sig"))
            matrix["coverage_summary"]["note"] = "字节变了但结论仍然合法"
            _write(run / MATRIX, matrix)
            invariants = evaluate_invariants(state, run)
        self.assertEqual("FAIL", invariants[STATE_09],
                         "身份变了还能沿用旧读数，就等于允许继承上一阶段的 PASS")

    def test_state09_fails_when_the_plan_tree_is_replaced_after_derivation(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _deliverable_run(Path(tmp), matrix=_matrix_payload([_record()]))
            state = self._clean_state(run)
            _write(run / HIERARCHY, _hierarchy(sections=("S77-77",), units=("CU-777-77",)))
            invariants = evaluate_invariants(state, run)
        self.assertEqual("FAIL", invariants[STATE_09], "旧计划树的读数被新计划继承了")

    def test_state09_does_not_fire_without_a_clean_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _deliverable_run(Path(tmp), matrix=_matrix_payload([_record("S99-99")]))
            state = self._clean_state(run)
            self.assertNotEqual("COMPLETED_CLEAN", state["derived"]["delivery_status"])
            invariants = evaluate_invariants(state, run)
        self.assertEqual("PASS", invariants[STATE_09],
                         "没自称 Clean 时不该由这条不变量报红 —— 它管的是「说了干净就得真干净」")


class SingleCanonicalAuthorityOwnership(unittest.TestCase):
    """§7/§11：入口与终态消费同一个结果；终态不得再实现一遍映射算法。"""

    @staticmethod
    def _code_lines(path: Path) -> list[str]:
        text = path.read_text(encoding="utf-8")
        non_code = set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                non_code.update(range(node.lineno, getattr(node, "end_lineno", node.lineno) + 1))
        return [line for number, line in enumerate(text.splitlines(), 1)
                if number not in non_code and not line.lstrip().startswith("#")]

    def test_finalizer_does_not_reimplement_the_mapping_algorithm(self):
        lines = self._code_lines(APP / "longform" / "run_state.py")
        for forbidden in ("planned_response_section", "planned_response_cu",
                          "invalid_content_units", "pre_generation_scoring_mapping_gate("):
            hits = [line.strip() for line in lines if forbidden in line]
            self.assertEqual([], hits, f"终态端又在自己算一遍映射：{forbidden} {hits[:3]}")
        self.assertTrue(any("canonical_scoring_authority" in line for line in lines),
                        "终态没有消费那份唯一 Authority")

    def test_generation_entry_delegates_to_the_canonical_authority(self):
        lines = self._code_lines(APP / "longform" / "orchestrator.py")
        self.assertTrue(any("canonical_scoring_authority(run_dir)" in line.replace(" ", "")
                            for line in lines),
                        "生成入口还在自己算一份门结论")
        for removed in ("def _validate_scoring_gate", "def _reconcile_scoring_gate",
                        "def _scoring_gate_reading", "def _scoring_plan_reference_ids"):
            self.assertNotIn(removed, "\n".join(lines), f"上一轮的第二套判据 {removed} 还留着")

    def test_authority_reuses_the_canonical_gate_instead_of_a_second_engine(self):
        lines = self._code_lines(APP / "longform" / "scoring_authority.py")
        source = "\n".join(lines)
        self.assertIn("pre_generation_scoring_mapping_gate", source)
        self.assertIn("build_scoring_taxonomy", source)
        self.assertNotIn("def pre_generation_scoring_mapping_gate", source,
                         "Authority 里不许另起一台评分引擎")
        self.assertNotIn("classify_scoring_row", source,
                         "分类只在 canonical 那一处发生")

    def test_authority_is_the_only_writer_of_the_clean_blocker_names(self):
        lines = self._code_lines(APP / "longform" / "run_state.py")
        self.assertFalse([line for line in lines if "SCORING_AUTHORITY_INVALID_REFERENCE" in line],
                         "原因码的词表在落盘端被又写了一遍")


class PreservedAdversarialCases(unittest.TestCase):
    """§14：上一轮 Codex 已 PASS 的攻击必须继续绿（这里查的是收口后的同一入口）。"""

    def _reading(self, record: dict, *, sections=PLAN_SECTIONS, units=PLAN_UNITS,
                 matrix=None) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            run = _deliverable_run(Path(tmp), matrix=matrix if matrix is not None
                                   else _matrix_payload([record]))
            _write(run / HIERARCHY, _hierarchy(sections=sections, units=units))
            return read_pre_generation_scoring_mapping_gate(run)

    def test_ghost_section_valid_cu_still_blocks(self):
        reading = self._reading(_record("S99-99"))
        self.assertFalse(reading["may_enter_generation"])
        self.assertEqual(["S99-99"], reading["invalid_reference"]["sections"])

    def test_mixed_valid_and_invalid_cu_still_blocks(self):
        reading = self._reading(_record("S01-01", cu=("CU-001-01", "CU-NOPE")))
        self.assertFalse(reading["may_enter_generation"])
        self.assertEqual(["CU-NOPE"], reading["invalid_reference"]["content_units"])

    def test_invalid_cu_only_still_blocks(self):
        self.assertFalse(self._reading(_record("S01-01", cu=("CU-NOPE",)))["may_enter_generation"])

    def test_stale_plan_tree_still_blocks(self):
        self.assertFalse(self._reading(_record("S01-01"), sections=("S77-77",),
                                       units=("CU-777-77",))["may_enter_generation"])

    def test_unreadable_and_malformed_mapping_artifacts_still_block(self):
        for payload, expected in (("{不是 JSON", "SCORING_MAPPING_ARTIFACT_UNREADABLE"),
                                  ("[1,2]", "SCORING_MAPPING_ARTIFACT_MALFORMED"),
                                  # `{}` 是**读得出来**的空矩阵：抽取里有 1 个主题，而这份矩阵
                                  # 连 canonical 记录这一段都没有 ⇒ 名字是"从未落盘"，
                                  # 不是"虚假 N/A"（它没做过那个声称），也不是解析失败。
                                  ("{}", "SCORING_MAPPING_RECORDS_NOT_PERSISTED")):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                run = _deliverable_run(Path(tmp), matrix=None)
                (run / MATRIX).write_text(payload, encoding="utf-8")
                reading = read_pre_generation_scoring_mapping_gate(run)
            self.assertFalse(reading["may_enter_generation"])
            self.assertEqual(expected, reading["status"])

    def test_unknown_verdict_and_missing_fields_still_block(self):
        cases = ({"status": "VERIFIED", "may_enter_generation": True,
                  "substantive_topic_total": 1, "mapped_total": 1, "unmapped_total": 0},
                 {"status": "PASS", "substantive_topic_total": 1},
                 "PASS")
        for gate in cases:
            with self.subTest(gate=gate), tempfile.TemporaryDirectory() as tmp:
                matrix = _matrix_payload([_record()])
                matrix["pre_generation_scoring_mapping_gate"] = gate
                run = _deliverable_run(Path(tmp), matrix=matrix)
                reading = read_pre_generation_scoring_mapping_gate(run)
            self.assertFalse(reading["may_enter_generation"], "词表外/缺字段的结论被当成通过了")
            self.assertTrue(reading["status"].startswith("SCORING_MAPPING_GATE_"))

    def test_stale_gate_over_current_matrix_still_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            matrix = _matrix_payload([_record()])
            matrix["pre_generation_scoring_mapping_gate"]["substantive_topic_total"] = 7
            matrix["pre_generation_scoring_mapping_gate"]["mapped_total"] = 7
            run = _deliverable_run(Path(tmp), matrix=matrix)
            reading = read_pre_generation_scoring_mapping_gate(run)
        self.assertFalse(reading["may_enter_generation"])
        self.assertEqual("SCORING_MAPPING_GATE_STALE", reading["status"])

    def test_legacy_matrix_without_the_records_key_is_named_honestly(self):
        """老工件（Run D 那一代）连 `canonical_scoring_records` 键都没有：挡，但别指控它说谎。

        两条都必须 `may_enter=false`，区别只在名字 —— 把"从未落盘"叫成"虚假 N/A"
        会让下一位复核人去查一个根本没发生过的声明。
        """
        with tempfile.TemporaryDirectory() as tmp:
            legacy = _matrix_payload([])
            legacy.pop("canonical_scoring_records")
            legacy.pop("pre_generation_scoring_mapping_gate")
            run = _deliverable_run(Path(tmp), matrix=legacy)
            reading = read_pre_generation_scoring_mapping_gate(run)
        self.assertFalse(reading["may_enter_generation"])
        self.assertEqual("SCORING_MAPPING_RECORDS_NOT_PERSISTED", reading["status"])
        # Round 4.4 §5 之后，这一档的原因码多了一条：从未落盘**同时**就是覆盖不完整
        # （适用主题 1 个、已映射 0 个）。名字没变，也没因此放行过一次。
        self.assertEqual(["SCORING_AUTHORITY_MISSING",
                          "SCORING_AUTHORITY_TOPIC_COVERAGE_INCOMPLETE"],
                         reading["clean_blockers"])
        self.assertEqual(1, reading["topic_expected_count"])
        self.assertEqual(0, reading["topic_mapped_count"])
        # 对照：同样有主题，但矩阵**算过并把记录落成空表** —— 那才是假 N/A。
        with tempfile.TemporaryDirectory() as tmp:
            run = _deliverable_run(Path(tmp), matrix=_matrix_payload([], verdict="NOT_APPLICABLE"))
            claim = read_pre_generation_scoring_mapping_gate(run)
        self.assertFalse(claim["may_enter_generation"])
        self.assertEqual("SCORING_MAPPING_FALSE_NOT_APPLICABLE", claim["status"])

    def test_technical_only_leak_still_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            matrix = _matrix_payload([_record()])
            matrix["rows"].append({"scoring_item_id": "SCR-9", "label": "投标报价合理性",
                                   "scoring_class": "PRICING",
                                   "section_ids": ["S01-01"], "response_status": "PLANNED"})
            run = _deliverable_run(Path(tmp), matrix=matrix)
            reading = read_pre_generation_scoring_mapping_gate(run)
        self.assertFalse(reading["may_enter_generation"])
        self.assertEqual("TECHNICAL_SCORING_LEAK", reading["status"])


class P01PageBandFrozenRegression(unittest.TestCase):
    """§15：P0-1 已经 Codex 独立 PASS —— 这里只测不改。"""

    def _derived(self, **fixture):
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), **fixture)
            return derive_run_state(collect_facts(run, medium="WORD"))

    def test_out_of_band_and_projection_only_are_never_clean(self):
        for pages, targets, expected_blocker in (
                (244, (250, 320), "PAGE_BAND_UNDER_LENGTH"),
                (321, (250, 320), "PAGE_BAND_OVERSHOOT")):
            with self.subTest(pages=pages), tempfile.TemporaryDirectory() as tmp:
                run = _make_run(Path(tmp), pages=pages, page_targets=targets)
                state = finalize_delivery_state(run, medium="WORD",
                                                draft_delivery="COMPLETED_CLEAN")
            self.assertNotEqual("COMPLETED_CLEAN", state["derived"]["delivery_status"])
            self.assertIn(expected_blocker, state["derived"]["final_qa_blockers"])
        derived = self._derived(pages=None, projected_pages=264, page_targets=(250, 320))
        self.assertNotEqual("COMPLETED_CLEAN", derived["delivery_status"])
        self.assertIn("PAGE_BAND_NOT_MEASURED", derived["final_qa_blockers"])

    def test_band_boundaries_stay_clean_eligible(self):
        for pages in (250, 320):
            with self.subTest(pages=pages):
                derived = self._derived(pages=pages, page_targets=(250, 320))
                self.assertEqual("COMPLETED_CLEAN", derived["delivery_status"],
                                 derived["final_qa_blockers"])

    def test_other_page_bands_use_the_same_ruler(self):
        for pages, targets, clean in ((146, (120, 150), True), (119, (120, 150), False),
                                      (67, (50, 70), True), (49, (50, 70), False)):
            with self.subTest(pages=pages, targets=targets):
                derived = self._derived(pages=pages, page_targets=targets)
                self.assertEqual(clean, derived["delivery_status"] == "COMPLETED_CLEAN")


class FinalQaShapesFrozenRegression(unittest.TestCase):
    """§16：最终 QA 的八态照旧全挡，本轮只新增"评分无效 ⇒ Clean 不可能"。"""

    def test_unmeasured_review_and_blocked_qa_shapes_are_never_clean(self):
        # Round 4.7 同尺重锚：终态读的那一份换了 `final_artifact_qa.json`，所以"读不出来"
        # 这个形状要在两份报告上都成立 —— 九态一个不少，钉的仍是同一件事。
        shapes = ({"final_qa": None},
                  {"final_qa_raw": "{坏 JSON", "artifact_qa_raw": "{坏 JSON"},
                  {"final_qa": {"summary": {}}}, {"final_qa": "SOMETHING_ELSE"},
                  {"final_qa": "NEEDS_REVIEW"}, {"final_qa": "REVIEW"},
                  {"final_qa": "FAIL"}, {"final_qa": "BLOCK"},
                  {"final_qa": "NOT_MEASURED"})
        for fixture in shapes:
            with self.subTest(**fixture), tempfile.TemporaryDirectory() as tmp:
                run = _make_run(Path(tmp), pages=286, page_targets=(250, 320), **fixture)
                state = finalize_delivery_state(run, medium="WORD",
                                                draft_delivery="COMPLETED_CLEAN")
            self.assertNotEqual("COMPLETED_CLEAN", state["derived"]["delivery_status"], fixture)

    def test_scoring_invalid_overrides_a_passing_qa_and_page_band(self):
        """C 类与新轴的合流点：QA PASS + 页带内 + 评分无效 ⇒ 仍然不可能 Clean。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _deliverable_run(Path(tmp), matrix=_matrix_payload([_record("S99-99")]))
            derived = derive_run_state(collect_facts(run, medium="WORD"))
        self.assertEqual(QA_PASS, derived["final_qa_state"])
        self.assertEqual("TARGET_RANGE", derived["page_band"])
        self.assertNotEqual("COMPLETED_CLEAN", derived["delivery_status"])
        self.assertIn("SCORING_AUTHORITY_INVALID_REFERENCE", derived["final_qa_blockers"])
        self.assertTrue(derived["content_gate_blocked"])


if __name__ == "__main__":
    unittest.main()
