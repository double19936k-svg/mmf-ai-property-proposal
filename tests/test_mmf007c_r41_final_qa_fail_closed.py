# -*- coding: utf-8 -*-
"""Round 4.1 P0-3：最终 QA 缺席不得被读成通过（fail-closed + 六面单一真值）。

被复现的缺陷（Codex 在 Round 4 复核里给出）：其它输入齐、**唯独没有
`artifact_qa_report.json`** 时，`derive_run_state` 仍能派生出 `COMPLETED_CLEAN`。
根因是那条判据写成 `final_qa in {"PASS", "NOT_APPLICABLE", None}` —— 把"这份报告不存在"
和"这份报告说通过"当成了同一件事。

本文件钉 §24–§29 的四件事：
  1. 只有真正量过并通过的形状（PASS / PASS_AFTER_REPAIR / 显式 NOT_APPLICABLE）
     才能进 COMPLETED_CLEAN 候选；
  2. None / 词表外值 / 报告缺失 / 字段缺失 / JSON 读不出来 —— 一律**具名**，且绝不进 Clean；
  3. NEEDS_REVIEW、BLOCK/FAIL、没量过 三档**不得被压成同一个状态**（§29）；
  4. 终态派生之后，run_audit / checkpoint / generation_status / volume_progress /
     生成计划 / run_state 这六面上写的必须是同一个真值（§27）。
零 Provider 调用（纯本地文件与纯派生函数）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from longform.run_state import (  # noqa: E402
    FINAL_QA_BLOCKED, FINAL_QA_NOT_MEASURED, FINAL_QA_UNKNOWN_STATUS, QA_BLOCK, QA_NOT_MEASURED,
    QA_NOT_APPLICABLE, QA_PASS, QA_REVIEW, QA_UNKNOWN, collect_facts, derive_run_state,
    evaluate_invariants, reconcile_run_state,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_mmf007c_r4_final_state_contract import (  # noqa: E402  同一份"内容全齐"夹具
    _make_run, _write_final_artifact_qa)

# Round 4.7 把这支尺**重锚**到"终态真正读的那一份"上。
#
# 本文件的判据是"QA 的结论必须进终态；没量过不等于通过"。Round 4.1 时终态只读
# `artifact_qa_report.json`，所以那份就是尺；Round 4.7 之后真实 Word 交付件一旦落位，
# 做数的凭据换成 `final_artifact_qa.json`（渲染前那份降为过程读数）。
# 尺若还留在渲染前那份上，本文件里"BLOCK 的报告 ⇒ 不得 Clean"会退化成在量一份
# **不再决定终态**的历史文件 —— 那不是收紧，是量错。九个形状一个不少，逐条同尺：
# 两份报告不一致时谁说了算，由 `test_mmf007c_r47_final_artifact_qa_authority.py` 钉。
STATE_REPORT = "final_artifact_qa.json"


def _shape_report(run: Path, shape: object) -> None:
    """把一种"QA 长什么样"写到**做数的那一份**报告上（在场且绑定当前交付件才算真量过）。"""
    if shape == "__NO_FILE__":
        (run / STATE_REPORT).unlink()
    elif isinstance(shape, str):
        _write_final_artifact_qa(run, status=shape)
    else:
        (run / STATE_REPORT).write_text(json.dumps(shape), encoding="utf-8")


def _derive(tmp: Path, final_qa: object) -> dict:
    run = _make_run(tmp, final_qa=final_qa)
    return derive_run_state(collect_facts(run, medium="WORD"))


class OnlyMeasuredPassMayEnterClean(unittest.TestCase):
    def test_pass_shapes_are_clean_candidates(self):
        for shape in ("PASS", "PASS_AFTER_REPAIR"):
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as tmp:
                derived = _derive(Path(tmp), shape)
                self.assertEqual(QA_PASS, derived["final_qa_state"], shape)
                self.assertEqual("COMPLETED_CLEAN", derived["delivery_status"], shape)
                self.assertEqual([], derived["final_qa_blockers"], shape)

    def test_declared_not_applicable_is_not_silence(self):
        """显式 NOT_APPLICABLE 是"这份交付没有可量的最终 QA"这个**声明**，与"没写报告"不同事。"""
        with tempfile.TemporaryDirectory() as tmp:
            derived = _derive(Path(tmp), "NOT_APPLICABLE")
        self.assertEqual(QA_NOT_APPLICABLE, derived["final_qa_state"])
        self.assertEqual("COMPLETED_CLEAN", derived["delivery_status"])

    def test_the_nine_measuring_shapes(self):
        """§29 点名的形状逐个过：只有 PASS 类进 Clean 候选。"""
        cases = [
            ("PASS", QA_PASS, True),
            ("NEEDS_REVIEW", QA_REVIEW, False),
            ("AUTO_REPAIR", QA_REVIEW, False),
            ("FAIL", QA_BLOCK, False),
            ("BLOCK", QA_BLOCK, False),
            ("__FIELD_NONE__", QA_NOT_MEASURED, False),
            ("__UNKNOWN_WORD__", QA_UNKNOWN, False),
            ("__NOT_MEASURED_NO_FILE__", QA_NOT_MEASURED, False),
            ("__UNREADABLE__", QA_NOT_MEASURED, False),
        ]
        for raw, expected_state, clean_allowed in cases:
            with self.subTest(shape=raw), tempfile.TemporaryDirectory() as tmp:
                run = _make_run(Path(tmp))
                if raw == "__FIELD_NONE__":
                    _shape_report(run, {"status": None, "findings": []})
                elif raw == "__NOT_MEASURED_NO_FILE__":
                    _shape_report(run, "__NO_FILE__")
                elif raw == "__UNREADABLE__":
                    (run / STATE_REPORT).write_text("{不是 JSON", encoding="utf-8")
                elif raw == "__UNKNOWN_WORD__":
                    _shape_report(run, {"status": "MAYBE_OK"})
                else:
                    _shape_report(run, raw)
                derived = derive_run_state(collect_facts(run, medium="WORD"))
                self.assertEqual(expected_state, derived["final_qa_state"], raw)
                self.assertEqual(clean_allowed, derived["delivery_status"] == "COMPLETED_CLEAN",
                                 f"{raw} 的 Clean 资格不对：{derived['delivery_status']}")


class UnmeasuredGetsItsOwnName(unittest.TestCase):
    def test_missing_file_names_the_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            derived = _derive(Path(tmp), None)
        self.assertEqual(QA_NOT_MEASURED, derived["final_qa_state"])
        self.assertIn(FINAL_QA_NOT_MEASURED, derived["final_qa_blockers"])
        self.assertEqual("FINAL_QA_REPORT_MISSING", derived["final_qa_missing_reason"])
        self.assertNotEqual("COMPLETED_CLEAN", derived["delivery_status"])
        self.assertTrue(derived["content_gate_blocked"], "没量过却对外说没被挡")

    def test_field_missing_and_unreadable_are_three_different_reasons(self):
        reasons = {}
        for shape, expected in (("__none__", "FINAL_QA_STATUS_FIELD_MISSING",),
                                ("__broken__", "FINAL_QA_REPORT_UNREADABLE",),
                                ("__absent__", "FINAL_QA_REPORT_MISSING",)):
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as tmp:
                run = _make_run(Path(tmp))
                if shape == "__none__":
                    _shape_report(run, {"findings": []})
                elif shape == "__broken__":
                    (run / STATE_REPORT).write_text("[1,2,", encoding="utf-8")
                else:
                    _shape_report(run, "__NO_FILE__")
                derived = derive_run_state(collect_facts(run, medium="WORD"))
                reasons[shape] = derived["final_qa_missing_reason"]
                self.assertEqual(expected, reasons[shape])
                self.assertEqual(QA_NOT_MEASURED, derived["final_qa_state"])
        self.assertEqual(3, len(set(reasons.values())), "三种'没量过'必须分别可辨")


class ReviewBlockAndUnmeasuredStayDistinct(unittest.TestCase):
    """§29：不许把 NEEDS_REVIEW / FAIL / BLOCK / 没量过 压成一个状态。"""

    def _four(self) -> list[dict]:
        rows = []
        for shape in ("AUTO_REPAIR", "BLOCK", None, {"status": "MAYBE_OK"}):
            with tempfile.TemporaryDirectory() as tmp:
                run = _make_run(Path(tmp))
                _shape_report(run, "__NO_FILE__" if shape is None else shape)
                rows.append(derive_run_state(collect_facts(run, medium="WORD")))
        return rows

    def test_internal_phases_are_pairwise_distinct(self):
        review, block, unmeasured, unknown = self._four()
        phases = [row["internal_phase"] for row in (review, block, unmeasured, unknown)]
        self.assertEqual(["COMPLETED_WITH_REVIEW", "COMPLETED_WITH_BLOCKERS",
                          "COMPLETED_UNVERIFIED", "COMPLETED_UNVERIFIED"], phases)
        self.assertEqual(3, len(set(phases)), "三档被压成了更少")

    def test_each_carries_its_own_named_blocker(self):
        review, block, unmeasured, unknown = self._four()
        self.assertIn(FINAL_QA_BLOCKED, block["final_qa_blockers"])
        self.assertIn(FINAL_QA_NOT_MEASURED, unmeasured["final_qa_blockers"])
        self.assertIn(FINAL_QA_UNKNOWN_STATUS, unknown["final_qa_blockers"])
        self.assertNotIn(FINAL_QA_BLOCKED, review["final_qa_blockers"],
                         "AUTO_REPAIR 被当成了 BLOCK")

    def test_delivery_and_public_face_stay_downloadable_but_not_clean(self):
        """BLOCK 不隐藏文件（AGENTS.md），但也绝不对外是"干净完成"。"""
        _, block, _, _ = self._four()
        self.assertEqual("NEEDS_REVIEW", block["delivery_status"])
        self.assertTrue(block["content_gate_blocked"])
        self.assertNotEqual("COMPLETED_CLEAN", block["delivery_status"])


class SixSurfacesTellTheSameTruth(unittest.TestCase):
    """§27：run_state / delivery / internal / public / history / 审计面 不许一面 Clean 一面 Unknown。"""

    def test_projection_agrees_after_reconcile(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), final_qa=None)
            for name in ("run_audit.json", "generation_status.json", "volume_progress.json",
                         "checkpoint/state.json"):
                (run / name).parent.mkdir(parents=True, exist_ok=True)
                (run / name).write_text(json.dumps({"delivery_status": "COMPLETED_CLEAN",
                                                    "result": {"delivery_status": "COMPLETED_CLEAN"},
                                                    "authority_delivery_status": "COMPLETED_CLEAN",
                                                    "completed_sections": ["S01-01", "S01-02", "S02-01"]}),
                                        encoding="utf-8")
            # 先把所有面写成"干净"，再看对账之后还有没有哪一面留在 Clean —— 这测的是
            # "投影是否真的覆盖每一面"，而不是"初始化恰好一致"。
            state = reconcile_run_state(run, medium="WORD")
            derived = state["derived"]
            # 三条轴各自横切所有面：同一条轴上出现两个值，就是"一面 Clean、另一面 Unknown"。
            audit = json.loads((run / "run_audit.json").read_text(encoding="utf-8"))
            checkpoint = json.loads((run / "checkpoint" / "state.json").read_text(encoding="utf-8"))
            history = json.loads((run / "generation_status.json").read_text(encoding="utf-8"))
            progress = json.loads((run / "volume_progress.json").read_text(encoding="utf-8"))
            plan = json.loads((run / "11_volume_generation_plan.json").read_text(encoding="utf-8"))
            axes = {
                "delivery": {"run_state": derived["delivery_status"], "run_audit": audit["delivery_status"],
                             "checkpoint": checkpoint["delivery_status"],
                             "history": history["result"]["delivery_status"],
                             "volume_progress": progress["authority_delivery_status"],
                             "plan": plan["authority_delivery_status"]},
                "internal_phase": {"run_state": derived["internal_phase"],
                                   "run_audit": audit["internal_phase"],
                                   "checkpoint": checkpoint["internal_phase"],
                                   "history": history["result"]["internal_phase"],
                                   "volume_progress": progress["authority_internal_phase"],
                                   "plan": plan["authority_internal_phase"]},
                "final_qa_state": {"run_state": derived["final_qa_state"],
                                   "run_audit": audit["final_qa_state"],
                                   "checkpoint": checkpoint["final_qa_state"],
                                   "history": history["result"]["final_qa_state"],
                                   "volume_progress": progress["final_qa_state"]},
            }
            for axis, surfaces in axes.items():
                self.assertEqual(1, len(set(surfaces.values())),
                                 f"{axis} 这条轴上各面读数不一致：{surfaces}")
            self.assertEqual("NEEDS_REVIEW", axes["delivery"]["run_audit"],
                             "六面一致地写着别的东西也不行 —— 缺最终 QA 就是待复核")
            self.assertEqual(QA_NOT_MEASURED, axes["final_qa_state"]["run_audit"],
                             "审计面只写了结果、没写为什么")
            self.assertIn(FINAL_QA_NOT_MEASURED, audit["final_qa_blockers"])
            self.assertEqual("PASS", state["invariants"][
                "STATE-07_clean_implies_final_qa_measured_and_passed"])


class State07CatchesAHandWrittenCleanClaim(unittest.TestCase):
    def test_clean_without_a_qa_report_fails_the_invariant(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), final_qa=None)
            facts = collect_facts(run, medium="WORD")
            forged = {**derive_run_state(facts), "delivery_status": "COMPLETED_CLEAN"}
            invariants = evaluate_invariants({"facts": facts, "derived": forged}, run)
            self.assertEqual("FAIL",
                             invariants["STATE-07_clean_implies_final_qa_measured_and_passed"],
                             "手写一个 COMPLETED_CLEAN 就能绕过最终 QA —— 这条门必须是死的")

    def test_clean_with_a_blocking_report_fails_the_invariant(self):
        """另一半：报告明明写着 BLOCK，authority 却报干净。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp))
            facts = collect_facts(run, medium="WORD")
            forged = {**derive_run_state(facts), "delivery_status": "COMPLETED_CLEAN"}
            invariants = evaluate_invariants({"facts": facts, "derived": forged}, run)
            self.assertEqual("PASS",
                             invariants["STATE-07_clean_implies_final_qa_measured_and_passed"],
                             "报告是 PASS 时不该误报 —— 这条不变量只在假声明上失败")
            (run / STATE_REPORT).write_text(json.dumps({"status": "BLOCK"}), encoding="utf-8")
            facts = collect_facts(run, medium="WORD")
            invariants = evaluate_invariants({"facts": facts, "derived": forged}, run)
            self.assertEqual("FAIL",
                             invariants["STATE-07_clean_implies_final_qa_measured_and_passed"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
