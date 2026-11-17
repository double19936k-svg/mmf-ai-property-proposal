# -*- coding: utf-8 -*-
"""MMF-007C Round 4.7：最终 Artifact QA 必须进 Clean Authority（Q1–Q6 + 攻击 + 结构哨兵）。

被修缺陷（Grok Build 独立验证确认的唯一 Run F blocker，已先由只读探针在现场复现）：
正式路径 `artifact_qa_report.json → _final_qa_facts → derive_run_state →
finalize_delivery_state` 里，Clean 的 QA 凭据读的是**渲染前 / 装配期**那份报告。
于是真实交付 DOCX 已被 `final_artifact_qa.json` 判成 BLOCK 的一场 Run，仍然可以拿到
`delivery_status=COMPLETED_CLEAN / final_qa_state=QA_PASS / source=artifact_qa_report.json
/ STATE-07=PASS`。现场 18 份带最终 QA 的真实 Run 里有 7 份两份报告互相矛盾。

本轮立的是任务书 §必须建立的不变量 那四条：
  1. 交付件已在场 ⇒ COMPLETED_CLEAN 至少要求 `final_artifact_qa = PASS` **且**该 QA 绑当前
     Artifact identity；
  2. 该 QA BLOCK ⇒ 不得 Clean；NEEDS_REVIEW / WARNING ⇒ 不得 Clean；
  3. 交付件在场而那份 QA 缺失 / 不可读 / 词表外 / 绑的是别的字节 ⇒ fail-closed，且具名；
  4. 渲染前那份不删、仍读数（过程质量），但不再覆盖最终 Artifact QA。

Q3（pre BLOCK + final PASS）不是猜的：设计选择写在
`canonical_final_qa_authority` 的注释与 STATE-07 那条反向检查的注释里，凭据是
`governance/final_artifact_qa.py:531` 的 `evaluate_docx_artifact` 自己会再跑一遍
`evaluate_artifact`（同一套规则、量的是真实 DOCX 字节），而
`app/app_core.py:2028-2037` 早在用 `findings=list(final_artifact_qa["findings"])`
重算 `classify_fail_soft` —— 渲染后的结论本来就已经在决定交付，只是终态那一步没跟着换源。

全部离线：tmp 目录里造文件 + 纯函数调用，零 Provider 调用。
"""
from __future__ import annotations

import ast
import hashlib
import os
import json
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
APP = APP_ROOT / "app"
for _path in (str(APP), str(APP_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from longform import run_state  # noqa: E402
from longform.run_state import (  # noqa: E402
    FINAL_ARTIFACT_QA_FILE, FINAL_QA_BLOCKED, FINAL_QA_NOT_BOUND_TO_LANDED_ARTIFACT,
    FINAL_QA_NOT_MEASURED, FINAL_QA_UNKNOWN_STATUS, PRE_RENDER_QA_FILE,
    QA_BLOCK, QA_CLEAN_ELIGIBLE_STATES, QA_GOVERNS_FINAL_ARTIFACT,
    QA_GOVERNS_PRE_RENDER, QA_NOT_APPLICABLE, QA_NOT_MEASURED, QA_PASS, QA_REVIEW,
    QA_UNKNOWN, canonical_final_qa_authority, collect_facts, derive_run_state,
    evaluate_invariants, finalize_delivery_state)

sys.path.insert(0, str(APP_ROOT / "tests"))
from test_mmf007c_r4_final_state_contract import (  # noqa: E402
    _make_run, _write, _write_final_artifact_qa)

STATE07 = "STATE-07_clean_implies_final_qa_measured_and_passed"
ARTIFACT = "artifact/final.docx"


def _derive(run: Path, *, medium: str = "WORD") -> tuple[dict, dict, dict]:
    facts = collect_facts(run, medium=medium)
    derived = derive_run_state(facts)
    return facts, derived, evaluate_invariants({"facts": facts, "derived": derived}, run)


def _artifact_sha(run: Path) -> str:
    return hashlib.sha256((run / ARTIFACT).read_bytes()).hexdigest()


def _is_clean(derived: dict) -> bool:
    return derived["delivery_status"] == "COMPLETED_CLEAN"


class Q1_Q6_TaskBookMatrix(unittest.TestCase):
    """任务书 §必须新增的反例 Q1–Q6，一条一格。"""

    def test_q1_both_pass_and_bound_is_a_legal_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp))          # 夹具默认：两份都 PASS，且绑定当前交付件
            facts, derived, invariants = _derive(run)
        self.assertEqual(QA_PASS, derived["final_qa_state"])
        self.assertEqual(QA_GOVERNS_FINAL_ARTIFACT, facts["final_qa_governs"])
        self.assertEqual(FINAL_ARTIFACT_QA_FILE, facts["final_qa_source"])
        self.assertTrue(facts["final_qa_binding"]["bound_to_landed_artifact"])
        self.assertTrue(_is_clean(derived), derived["final_qa_blockers"])
        self.assertEqual("PASS", invariants[STATE07])

    def test_q2_pre_render_pass_final_block_is_not_clean(self) -> None:
        """Grok 复现的那一条：渲染前 PASS、真实交付件 BLOCK。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), artifact_qa="BLOCK")
            facts, derived, invariants = _derive(run)
        self.assertEqual(QA_PASS, facts["pre_render_qa_state"], "渲染前那份还得读数（过程质量）")
        self.assertEqual(QA_BLOCK, facts["final_artifact_qa_state"])
        self.assertEqual(QA_BLOCK, derived["final_qa_state"], "Clean 凭据必须换成最终 QA")
        self.assertIn(FINAL_QA_BLOCKED, derived["final_qa_blockers"])
        self.assertFalse(_is_clean(derived))
        self.assertTrue(derived["content_gate_blocked"])

    def test_q3_pre_render_block_final_pass_enters_clean_by_design(self) -> None:
        """Q3 的设计选择（见模块 docstring 的两处代码凭据）：终态按最终 Artifact QA。

        这一格钉的不是"随便挑一个"，而是**旧结论不许冒充否决票**：渲染前那份对的是
        装配期内容，渲染后那份对真实 DOCX 重新量过一遍。所以 final 修好了就能 Clean，
        但渲染前的 BLOCK 必须仍然看得见，不能被抹成 PASS。
        """
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), final_qa="BLOCK", artifact_qa="PASS")
            facts, derived, invariants = _derive(run)
        self.assertEqual(QA_BLOCK, facts["pre_render_qa_state"], "过程读数不得被改写")
        self.assertEqual(QA_PASS, facts["final_artifact_qa_state"])
        self.assertEqual(QA_PASS, derived["final_qa_state"])
        self.assertTrue(_is_clean(derived), derived["final_qa_blockers"])
        self.assertEqual("PASS", invariants[STATE07])

    def test_q4_landed_artifact_without_final_qa_report_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), artifact_qa=None)
            facts, derived, invariants = _derive(run)
        self.assertEqual(QA_NOT_MEASURED, derived["final_qa_state"])
        self.assertEqual("FINAL_QA_REPORT_MISSING", derived["final_qa_missing_reason"])
        self.assertIn(FINAL_QA_NOT_MEASURED, derived["final_qa_blockers"])
        self.assertFalse(_is_clean(derived))
        self.assertEqual(QA_GOVERNS_FINAL_ARTIFACT, facts["final_qa_governs"])

    def test_q5_final_qa_pass_bound_to_other_bytes_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), artifact_qa=None)   # 先不要那份报告
            _write_final_artifact_qa(run, status="PASS", bind=False)  # 量的是别的字节
            facts, derived, invariants = _derive(run)
        self.assertEqual(QA_PASS, facts["final_artifact_qa_state"], "报告自报通过")
        self.assertEqual(QA_NOT_MEASURED, derived["final_qa_state"], "但它不是对当前交付件量的")
        self.assertIn(FINAL_QA_NOT_BOUND_TO_LANDED_ARTIFACT, derived["final_qa_blockers"])
        self.assertIn("SHA256_MISMATCH", derived["final_qa_missing_reason"])
        self.assertEqual(["SHA256_MISMATCH"], facts["final_qa_binding"]["binding_defects"])
        self.assertFalse(_is_clean(derived))

    def test_q6_binding_to_current_artifact_is_a_clean_prerequisite(self) -> None:
        """正面：身份对得上才谈得上"最终 QA 量过了"，而且身份是**现算**的，不是抄来的。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), artifact_qa=None)
            path = _write_final_artifact_qa(run, status="PASS")
            facts, derived, _ = _derive(run)
            self.assertTrue(_is_clean(derived), derived["final_qa_blockers"])
            self.assertEqual(f"{ARTIFACT.split('/')[-1]}={_artifact_sha(run)[:16]}",
                             derived["final_qa_artifact_identity"])
            # 同一条 authority 的哈希与产物身份用的是同一把尺（现场只有一处算 sha256）
            self.assertEqual(_artifact_sha(run), json.loads(path.read_text(encoding="utf-8"))["sha256"])
            self.assertEqual(_artifact_sha(run), facts["final_artifact"]["sha256"])


class StaleAndUnreadableAttacks(unittest.TestCase):
    """任务书 §验证顺序 3：stale / missing / wrong-artifact 三种攻击逐个具名。"""

    def test_a1_artifact_rewritten_after_qa_is_stale_not_clean(self) -> None:
        """新 DOCX 误消费旧 final_artifact_qa.json —— 这是本轮要堵的第一号形状。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp))
            facts, derived, _ = _derive(run)
            self.assertTrue(_is_clean(derived))
            (run / ARTIFACT).write_bytes(b"re-rendered-docx-bytes")   # 交付件又变了一次
            facts, derived, invariants = _derive(run)
        self.assertEqual(QA_NOT_MEASURED, derived["final_qa_state"])
        self.assertIn(FINAL_QA_NOT_BOUND_TO_LANDED_ARTIFACT, derived["final_qa_blockers"])
        self.assertFalse(_is_clean(derived))
        self.assertEqual("PASS", invariants[STATE07],
                         "派生已经挡住了，不变量不该再误报成 FAIL")

    def test_a2_artifact_replaced_by_other_name(self) -> None:
        """换名/回滚那份：字节碰巧一样也不算数 —— 名字轴与哈希轴各自单独成立。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), artifact_qa=None)
            _write_final_artifact_qa(run, status="PASS")           # 绑 final.docx
            (run / "artifact" / "final.docx").unlink()
            (run / "artifact" / "rollback.docx").write_bytes(b"docx-bytes")
            facts, derived, _ = _derive(run)
        self.assertIn("ARTIFACT_NAME_MISMATCH", facts["final_qa_binding"]["binding_defects"])
        self.assertFalse(_is_clean(derived))

    def test_a3_declared_sha_field_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), artifact_qa=None)
            _write(run / FINAL_ARTIFACT_QA_FILE, {"status": "PASS", "findings": []})
            facts, derived, _ = _derive(run)
        self.assertIn("SHA256_FIELD_ABSENT", facts["final_qa_binding"]["binding_defects"])
        self.assertEqual(QA_NOT_MEASURED, derived["final_qa_state"])
        self.assertFalse(_is_clean(derived))

    def test_a4_actual_docx_not_inspected(self) -> None:
        """报告自称 PASS，却没真打开过交付件 —— 那不算"对最终产物量过"。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), artifact_qa=None)
            _write_final_artifact_qa(run, status="PASS", inspected=False)
            facts, derived, _ = _derive(run)
        self.assertIn("ACTUAL_DOCX_NOT_INSPECTED", facts["final_qa_binding"]["binding_defects"])
        self.assertFalse(_is_clean(derived))

    def test_a5_unreadable_and_unknown_word_stay_named_and_not_clean(self) -> None:
        for shape, expected_state, expected_reason, expected_blocker in (
                ("{不是 JSON", QA_NOT_MEASURED, "FINAL_QA_REPORT_UNREADABLE",
                 FINAL_QA_NOT_MEASURED),
                ({"status": "MAYBE_OK"}, QA_UNKNOWN, "FINAL_QA_UNKNOWN_STATUS:MAYBE_OK",
                 FINAL_QA_UNKNOWN_STATUS),
                ({"findings": []}, QA_NOT_MEASURED, "FINAL_QA_STATUS_FIELD_MISSING",
                 FINAL_QA_NOT_MEASURED)):
            with self.subTest(shape=str(shape)[:20]), tempfile.TemporaryDirectory() as tmp:
                run = _make_run(Path(tmp), artifact_qa=None)
                if isinstance(shape, str):
                    (run / FINAL_ARTIFACT_QA_FILE).write_text(shape, encoding="utf-8")
                else:
                    _write(run / FINAL_ARTIFACT_QA_FILE, shape)
                _, derived, _ = _derive(run)
                self.assertEqual(expected_state, derived["final_qa_state"])
                self.assertEqual(expected_reason, derived["final_qa_missing_reason"])
                self.assertIn(expected_blocker, derived["final_qa_blockers"])
                self.assertFalse(_is_clean(derived))

    def test_a6_review_and_warning_are_not_clean_eligible(self) -> None:
        """§必须建立的不变量 2：NEEDS_REVIEW / WARNING 都不得 Clean（沿用既有状态词）。"""
        self.assertEqual({QA_PASS, QA_NOT_APPLICABLE}, QA_CLEAN_ELIGIBLE_STATES)
        for word in ("NEEDS_REVIEW", "WARNING", "AUTO_REPAIR", "WARNINGS_REMAIN",
                     "REVIEW_REQUIRED"):
            with self.subTest(status=word), tempfile.TemporaryDirectory() as tmp:
                run = _make_run(Path(tmp), artifact_qa=word)
                _, derived, _ = _derive(run)
                self.assertEqual(QA_REVIEW, derived["final_qa_state"], word)
                self.assertFalse(_is_clean(derived), word)


class FormalFinalizePathIsTheSameAuthority(unittest.TestCase):
    """任务书 §已复现 走的是正式路径：finalize_delivery_state 不许再复制一套判断。"""

    @staticmethod
    def _seed_draft_clean(run: Path) -> None:
        """先把各投影面写成"干净完成"，再看终态边界肯不肯覆盖 —— 测的是投影真的覆盖面，
        不是初始化恰好一致（与 Round 4.1 §27 同一手法）。"""
        for name in ("run_audit.json", "generation_status.json", "volume_progress.json",
                     "checkpoint/state.json"):
            path = run / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"delivery_status": "COMPLETED_CLEAN",
                                        "result": {"delivery_status": "COMPLETED_CLEAN"},
                                        "authority_delivery_status": "COMPLETED_CLEAN",
                                        "completed_sections": ["S01-01", "S01-02", "S02-01"]}),
                            encoding="utf-8")

    def test_finalize_refuses_a_draft_clean_claim_when_final_qa_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), artifact_qa="BLOCK")
            self._seed_draft_clean(run)
            state = finalize_delivery_state(run, medium="WORD",
                                            draft_delivery="COMPLETED_CLEAN")
            audit = json.loads((run / "run_audit.json").read_text(encoding="utf-8"))
        self.assertNotEqual("COMPLETED_CLEAN", state["derived"]["delivery_status"])
        self.assertEqual(QA_BLOCK, state["derived"]["final_qa_state"])
        self.assertEqual(FINAL_ARTIFACT_QA_FILE, state["derived"]["final_qa_source"])
        self.assertEqual(QA_BLOCK, audit["final_qa_state"], "六面必须投影同一个真值")
        self.assertNotEqual("COMPLETED_CLEAN", audit["delivery_status"],
                            "草稿里的假 Clean 必须被权威状态覆盖")

    def test_finalize_fails_closed_when_final_qa_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), artifact_qa=None)
            self._seed_draft_clean(run)
            state = finalize_delivery_state(run, medium="WORD",
                                            draft_delivery="COMPLETED_CLEAN")
        self.assertNotEqual("COMPLETED_CLEAN", state["derived"]["delivery_status"])
        self.assertEqual("FINAL_QA_REPORT_MISSING", state["derived"]["final_qa_missing_reason"])

    def test_state07_fails_a_hand_written_clean_claim(self) -> None:
        """手写 COMPLETED_CLEAN 绕不过去 —— 这条门必须是死的。"""
        for shape in ("BLOCK", None, "UNBOUND"):
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as tmp:
                run = _make_run(Path(tmp), artifact_qa=None)
                if shape == "BLOCK":
                    _write_final_artifact_qa(run, status="BLOCK")
                elif shape == "UNBOUND":
                    _write_final_artifact_qa(run, status="PASS", bind=False)
                facts = collect_facts(run, medium="WORD")
                forged = {**derive_run_state(facts), "delivery_status": "COMPLETED_CLEAN"}
                invariants = evaluate_invariants({"facts": facts, "derived": forged}, run)
                self.assertEqual("FAIL", invariants[STATE07],
                                 "伪造一个 COMPLETED_CLEAN 就能宣称最终 QA 过了")

    def test_ppt_or_unlanded_artifact_is_not_forced_into_the_new_ruler(self) -> None:
        """边界：没有落位 Word 产物时没有"对最终 DOCX 的 QA"可消费，凭据仍是渲染前那份。

        R-PPT-FREEZE-001  frozen 区域内不动 —— 这条钉的是"本轮新判据没有渗进 PPT 链"。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            no_artifact = _make_run(root / "a", with_final=False)
            facts, derived, _ = _derive(no_artifact)
            self.assertEqual(QA_GOVERNS_PRE_RENDER, facts["final_qa_governs"])
            self.assertEqual(PRE_RENDER_QA_FILE, facts["final_qa_source"])
            self.assertEqual(QA_PASS, derived["final_qa_state"], "QA 这条轴读的是渲染前那份")
            self.assertIn("FINAL_ARTIFACT_NOT_LANDED", derived["final_qa_blockers"],
                          "产物没落位仍由既有那条挡，不靠本轮新判据")

            pptx_only = _make_run(root / "b", with_final=False)
            (pptx_only / "artifact").mkdir(parents=True, exist_ok=True)
            (pptx_only / "artifact" / "deck.pptx").write_bytes(b"pptx-bytes")
            facts, derived, _ = _derive(pptx_only, medium="PPT")
        self.assertEqual(QA_GOVERNS_PRE_RENDER, facts["final_qa_governs"],
                         "纯 PPT 交付不得被要求有一份 DOCX 的最终 QA")
        self.assertFalse(facts["final_qa_binding"]["required_by_landed_word_artifact"])


    def test_a7_two_landed_docx_same_mtime_binds_the_later_one(self) -> None:
        """真实现场形状（tender_20260909_183128_8e0a4d）：`artifact/` 留着两份 DOCX，
        整目录被复制过 ⇒ mtime 全同。交付件名里带秒，所以"后一秒那份"才是当前交付件。

        只按 mtime 取最大会挑到旧的那一份，于是"QA 绑的是新交付件"被读成"QA 过期"——
        那是量具误伤，不是产品缺陷。这条把平票打破规则钉住：正反两个方向都要对。
        """
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), artifact_qa=None)
            stamp = "190727"
            old = run / "artifact" / f"scheme_{stamp}.docx"
            new = run / "artifact" / "scheme_190828.docx"
            (run / "artifact" / "final.docx").unlink()
            old.write_bytes(b"first-render")
            new.write_bytes(b"second-render")
            os.utime(old, (1_700_000_000, 1_700_000_000))
            os.utime(new, (1_700_000_000, 1_700_000_000))   # 故意平票
            _write_final_artifact_qa(run, status="PASS", artifact=new)
            facts, derived, _ = _derive(run)
            self.assertEqual("scheme_190828.docx", facts["final_qa_binding"]["landed_artifact"])
            self.assertTrue(_is_clean(derived), derived["final_qa_blockers"])
            # 反向：同一份平票现场，QA 若绑的是前一秒那份，仍然是"过期"。
            _write_final_artifact_qa(run, status="PASS", artifact=old)
            facts, derived, _ = _derive(run)
            self.assertIn("SHA256_MISMATCH", facts["final_qa_binding"]["binding_defects"])
            self.assertFalse(_is_clean(derived))


    def test_formal_run_boundary_cannot_project_clean_over_blocked_final_qa(self) -> None:
        """Grok 复现走的就是这条路：`run_layout.finalize_run` → 终态权威 → 投影与下载面。

        夹具与 Round 4.6 的正式路径测试同一手法（patch `paths.current`），但把渲染后那份
        QA 写成 BLOCK、渲染前那份留 PASS —— 正是现场那 7 场的形状。成果目录里有 DOCX，
        终态却不许是 COMPLETED_CLEAN。
        """
        import run_layout
        from types import SimpleNamespace
        from unittest.mock import patch

        import paths as paths_module
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs, output = root / "runs", root / "output"
            run = _make_run(runs, artifact_qa="BLOCK")
            for name in ("run_audit.json", "generation_status.json"):
                _write(run / name, {"status": "generation_completed",
                                    "candidate_delivery_status": "COMPLETED_CLEAN",
                                    "result": {"delivery_status": "COMPLETED_CLEAN"},
                                    "delivery_status": "COMPLETED_CLEAN"})
            roots = SimpleNamespace(runs_dir=runs, output_root=output,
                                    logs_dir=root / "logs", runtime_dir=root / "runtime")
            with patch.object(paths_module, "current", return_value=roots):
                result = run_layout.finalize_run(run.name, {"medium": "WORD",
                                                            "delivery_status": "COMPLETED_CLEAN"})
            landed = sorted((Path(result["artifact_dir"])).glob("*.docx"))
            state = json.loads((run / "run_state.json").read_text(encoding="utf-8"))
        self.assertTrue(landed, "成果目录里必须真的有那份 DOCX —— 否则测的是'产物没落位'那条轴")
        self.assertNotEqual("COMPLETED_CLEAN", result["delivery_status"])
        self.assertNotEqual("COMPLETED_CLEAN", result["qa_state"],
                            "`qa_state` 是 finalize_run 投影出去的那一格，不得留在假 Clean 上")
        self.assertEqual(FINAL_ARTIFACT_QA_FILE, state["derived"]["final_qa_source"])
        self.assertEqual(QA_BLOCK, state["derived"]["final_qa_state"])
        self.assertEqual(QA_GOVERNS_FINAL_ARTIFACT, state["derived"]["final_qa_governs"])


class OneAuthorityMultipleConsumers(unittest.TestCase):
    """结构哨兵：AST/源码钉住"只有一个 reader、一处派生、一套 SHA"，不靠自觉。"""

    def test_only_one_reader_of_both_qa_reports(self) -> None:
        tree = ast.parse(Path(run_state.__file__).read_text(encoding="utf-8"))
        funcs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        callers = sorted(name for name, node in funcs.items()
                         if any(isinstance(c, ast.Call)
                                and getattr(c.func, "id", "") == "canonical_final_qa_authority"
                                for c in ast.walk(node)))
        self.assertEqual(["collect_facts", "evaluate_invariants"], callers,
                         "读 QA 的落点必须是 authority 自己 + 两个消费者，多一个就是第二把尺")

    def test_status_vocabulary_is_mapped_in_exactly_one_place(self) -> None:
        src = Path(run_state.__file__).read_text(encoding="utf-8")
        self.assertEqual(1, src.count("FINAL_QA_STATE_BY_STATUS.get"),
                         "状态词表被第二处 .get() 解释 = 两套 QA 判决")
        self.assertIn("def _qa_report_reading", src)

    def test_no_second_sha_scheme(self) -> None:
        src = Path(run_state.__file__).read_text(encoding="utf-8")
        self.assertEqual(1, src.count("_sha256(") - 1,
                         "sha256 只在 _sha256 之外被调用一次（_landed_final_artifact），"
                         "QA 绑定必须复用那份读数")
        self.assertEqual(1, src.count("hashlib.sha256"),
                         "终态里不得再起第二处直接哈希 = 第二套 SHA 体系")

    def test_no_new_state_words(self) -> None:
        """本轮只复用既有状态词：QA 那六个、delivery 那几个，一个都没新增。

        `QA_GOVERNS_*` 不是状态词 —— 它是新字段 `final_qa_governs` 的取值（"这份结论按
        哪一份报告下的"），把它算进状态表就是把"多了一格读数"说成"多了一套状态"。
        """
        vocabulary = {run_state.QA_PASS, run_state.QA_REVIEW, run_state.QA_BLOCK,
                      run_state.QA_NOT_APPLICABLE, run_state.QA_NOT_MEASURED,
                      run_state.QA_UNKNOWN}
        mapped = set(run_state.FINAL_QA_STATE_BY_STATUS.values())
        self.assertTrue(mapped <= vocabulary, mapped - vocabulary)
        # 词表外与"读不出"这两档由**读取步骤**产出，不在状态表里 —— 表里出现它们
        # 就等于给"没量过"编了一个看起来像结论的名字。
        self.assertEqual({run_state.QA_UNKNOWN, run_state.QA_NOT_MEASURED}, vocabulary - mapped)
        with tempfile.TemporaryDirectory() as tmp:
            for status in ("PASS", "BLOCK", "NEEDS_REVIEW", "WARNING", "MAYBE_OK", None):
                run = _make_run(Path(tmp) / str(status), artifact_qa=status)
                _, derived, _ = _derive(run)
                self.assertIn(derived["final_qa_state"], vocabulary, status)
                self.assertIn(derived["delivery_status"],
                              {"COMPLETED_CLEAN", "NEEDS_REVIEW", "TECHNICAL_FAILED",
                               "TERMINAL_BLOCKED"}, status)
        governs = {run_state.QA_GOVERNS_FINAL_ARTIFACT, run_state.QA_GOVERNS_PRE_RENDER}
        self.assertEqual(set(), governs & vocabulary,
                         "governs 标记若同时是状态词，读者就分不清挡下来的是哪一格")

    def test_pre_render_report_is_still_read_not_deleted(self) -> None:
        """任务书 §Pre-render QA 的地位：保留作过程读数，但不许覆盖最终 Artifact QA。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), final_qa="BLOCK", artifact_qa="PASS")
            facts, _, _ = _derive(run)
            self.assertTrue((run / PRE_RENDER_QA_FILE).is_file(),
                            "渲染前那份报告不得被删 —— 它仍是过程质量凭据")
        self.assertEqual(QA_BLOCK, facts["pre_render_qa_state"])
        self.assertEqual(QA_PASS, facts["final_qa_state"])
        self.assertNotEqual(PRE_RENDER_QA_FILE, facts["final_qa_source"])

    def test_guard_does_not_rejudge_qa(self) -> None:
        """guard_clean_claim 只按盘上交叉核对降级，不得自带一套 QA 判断。"""
        src = Path(run_state.__file__).read_text(encoding="utf-8")
        guard = next(node for node in ast.parse(src).body
                     if isinstance(node, ast.FunctionDef) and node.name == "guard_clean_claim")
        body = ast.get_source_segment(src, guard) or ""
        self.assertNotIn("FINAL_QA_STATE_BY_STATUS", body)
        self.assertIn("evaluate_invariants", body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
