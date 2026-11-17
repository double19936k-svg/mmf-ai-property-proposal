# -*- coding: utf-8 -*-
"""MMF-007C Round 4.1 Final Gate 返修：Codex 三个 FAIL blocker 的反例钉死。

Codex 独立 Final Gate 结论 3 PASS / 3 FAIL。本文件只钉那三个 FAIL，
并且**一律走生产入口与最终落盘结果**，不走 helper 旁路（任务书 §7/§11）：

  A（§4）：250–320 的**实测**产品页带必须是 `COMPLETED_CLEAN` 的必要条件。
      旧代码只在 `page_band == "OVERSHOOT"` 时挡，于是 `UNDER_LENGTH`（244 页）、
      "量不到"、"只有投影"三条路都能进 Clean。
  B（§5）：评分映射必须过引用完整性；mapping artifact missing/unreadable/malformed
      一律 fail-closed。旧代码 ①只验 `planned_response_section` 非空，不验它在不在计划里；
      ②CU 用 `set(bound) & known` **交集非空**即算过，一个无效 CU 被一个有效 CU 掩护；
      ③生成前的读取器把"文件不在 / 读不出 / 矩阵里没有门"一律返回 `{}`，而调用点写的是
      `if scoring_mapping and not ...` —— 空对象等于"这道门跳过"。
  C（§6）：终态必须是**当前可验证输入重新派生**的结果，不得从 draft/upstream 继承 Clean。
      旧代码在 `not final_exists` 分支里直接 `delivery = draft`，而 `run_layout` 又把
      上一份 authority 的 `delivery_status` 当 draft 传回来 —— 于是"上一轮写过 Clean"
      本身就成了这一轮 Clean 的凭据，同时 STATE-07 只报红不拦写。

反例编号与任务书 §4.4 / §5.7 / §6.4 一一对应，便于 Codex 复跑对表。
零 Provider 调用：纯本地文件 + 纯派生函数。
"""
from __future__ import annotations

import ast
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from longform.orchestrator import (  # noqa: E402
    PreGenerationScoringGateError,
    read_pre_generation_scoring_mapping_gate,
    require_pre_generation_scoring_mapping_gate,
)
from longform.run_state import (  # noqa: E402
    QA_BLOCK, QA_PASS, QA_REVIEW, collect_facts, derive_run_state,
    finalize_delivery_state,
)
from planning.scoring_taxonomy import pre_generation_scoring_mapping_gate  # noqa: E402
from test_mmf007c_r4_final_state_contract import _make_run  # noqa: E402  同一份"内容全齐"夹具

# 六面投影里"交付状态"真正落盘的黑板（`project_authority_state` 的写入目标），
# 以及每一面**由投影负责**的那个键 —— 断言只对"这一面本来就写着交付状态的地方"提要求。
SURFACE_DELIVERY_KEYS = {
    "run_audit.json": ("delivery_status",),
    "report/run_audit.json": ("delivery_status",),
    "checkpoint/state.json": ("delivery_status",),
    "volume_progress.json": ("delivery_status", "authority_delivery_status"),
    "11_volume_generation_plan.json": ("delivery_status", "authority_delivery_status"),
    "generation_status.json": ("delivery_status", "result.delivery_status"),
}


def _surface_value(row: dict, key: str) -> object:
    value: object = row
    for part in key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _fake_clean_surfaces(run: Path) -> None:
    """把上游黑板**已有的**那些文件写成假 Clean：finalize 之后必须被真实派生值改回去。

    只补 delivery 相关键，绝不整份覆盖 —— `11_volume_generation_plan.json` 一旦被覆盖，
    测的就不是"假 Clean 被纠正"，而是"计划被测试自己弄坏了"。
    """
    for rel, keys in SURFACE_DELIVERY_KEYS.items():
        path = run / rel
        row = {}
        if path.is_file():
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                row = {}
        for key in keys:
            head, _, tail = key.rpartition(".")
            if head:
                row.setdefault(head, {})[tail] = "COMPLETED_CLEAN"
            else:
                row[key] = "COMPLETED_CLEAN"
        if rel.startswith("generation_status"):
            row.setdefault("result", {})["delivery_status"] = "COMPLETED_CLEAN"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")


def _derive(tmp: Path, **fixture) -> dict:
    return derive_run_state(collect_facts(_make_run(tmp, **fixture), medium="WORD"))


def _record(section="S01-01", cu=("CU-001-01",), contract=None):
    return {
        "canonical_scoring_id": "SCN-001",
        "topic": "项目服务方案",
        "source_ids": ["SC-01"],
        "planned_response_section": section,
        "planned_response_sections": [section] if section else [],
        "planned_response_cu": list(cu),
        "evidence_contract": contract if contract is not None else {
            "concepts": ["覆盖范围", "响应时限"], "min_concepts": 2},
    }


class APageBandIsCleanPrecondition(unittest.TestCase):
    """§4.4 A1–A4 + A7：产品页带进入正式终态判定，且各档共用一把尺。"""

    def test_a1_244_pages_with_qa_pass_is_not_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), pages=244, page_targets=(250, 320))
            state = finalize_delivery_state(run, medium="WORD",
                                            draft_delivery="COMPLETED_CLEAN")
        self.assertNotEqual("COMPLETED_CLEAN", state["derived"]["delivery_status"])
        self.assertIn("PAGE_BAND_UNDER_LENGTH", state["derived"]["final_qa_blockers"])

    def test_a2_overshoot_is_not_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            derived = _derive(Path(tmp), pages=321, page_targets=(250, 320))
        self.assertNotEqual("COMPLETED_CLEAN", derived["delivery_status"])
        self.assertIn("PAGE_BAND_OVERSHOOT", derived["final_qa_blockers"])

    def test_a3_a4_band_boundaries_stay_clean_eligible(self):
        for pages in (250, 320):
            with self.subTest(pages=pages), tempfile.TemporaryDirectory() as tmp:
                derived = _derive(Path(tmp), pages=pages, page_targets=(250, 320))
                self.assertEqual("COMPLETED_CLEAN", derived["delivery_status"])
                self.assertEqual([], derived["final_qa_blockers"])

    def test_a7_other_bands_use_the_same_ruler(self):
        """150/70 档语义不变：带内放行、带外挡住，来源同一个 `classify_page_state`。"""
        for pages, targets, clean in ((146, (120, 150), True), (119, (120, 150), False),
                                      (67, (50, 70), True), (49, (50, 70), False)):
            with self.subTest(pages=pages, targets=targets), \
                    tempfile.TemporaryDirectory() as tmp:
                derived = _derive(Path(tmp), pages=pages, page_targets=targets)
                self.assertEqual(clean, derived["delivery_status"] == "COMPLETED_CLEAN",
                                 f"{pages} 页在 {targets} 档的 Clean 资格判错了")

    def test_a6_projection_never_overrides_measured_actual(self):
        """A6：初稿那份是投影（264 在带内、`measured:false`），最终分页实测 244 在带外。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), pages=244, page_targets=(250, 320))
            (run / "document_scale_report.json").write_text(json.dumps({
                "page_targets": [250, 320],
                "page_report": {"technical_effective_pages": 264, "measured": False},
            }), encoding="utf-8")
            facts = collect_facts(run, medium="WORD")
            derived = derive_run_state(facts)
        self.assertEqual("FINAL_ARTIFACT_PAGINATION", facts["page_band_source"])
        self.assertEqual(244, facts["page_band_pages"])
        self.assertNotEqual("COMPLETED_CLEAN", derived["delivery_status"])

    def test_projected_only_pages_are_not_measured_and_block_clean(self):
        """只有投影没有实测 ⇒ 具名"量不到"，不得当成"页带内"。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), pages=None, projected_pages=264,
                            page_targets=(250, 320))
            facts = collect_facts(run, medium="WORD")
            derived = derive_run_state(facts)
        self.assertEqual("PROJECTED_FROM_CHARS", facts["page_band_basis"])
        self.assertNotEqual("COMPLETED_CLEAN", derived["delivery_status"])
        self.assertIn("PAGE_BAND_NOT_MEASURED", derived["final_qa_blockers"])

    def test_unmeasured_pages_are_named_not_silently_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            derived = _derive(Path(tmp), pages=None, page_targets=None)
        self.assertNotEqual("COMPLETED_CLEAN", derived["delivery_status"])
        self.assertIn("PAGE_BAND_NOT_MEASURED", derived["final_qa_blockers"])


class BReferentialIntegrity(unittest.TestCase):
    """§5.7 B1/B2/B3：引用完整性逐条验，有效项不得掩护无效项。"""

    SECTIONS = ("S01-01", "S02-01")
    UNITS = ("CU-001-01", "CU-002-01")

    def _gate(self, record):
        return pre_generation_scoring_mapping_gate(
            {"canonical_scoring_records": [record], "rows": [], "coverage_summary": {}},
            planned_unit_ids=list(self.UNITS), planned_section_ids=list(self.SECTIONS))

    def test_b1_nonexistent_section_blocks_even_with_valid_cu(self):
        gate = self._gate(_record(section="S99-99", cu=("CU-001-01",)))
        self.assertEqual("BLOCKED_SCORING_MAPPING_INCOMPLETE", gate["status"])
        self.assertFalse(gate["may_enter_generation"])
        self.assertIn("PLANNED_SECTION_NOT_IN_PLAN", gate["unmapped_records"][0]["missing"])
        self.assertEqual(["S99-99"], gate["unmapped_records"][0]["invalid_sections"])

    def test_b2_mixed_valid_and_invalid_cu_blocks(self):
        gate = self._gate(_record(cu=("CU-001-01", "CU-NOPE")))
        self.assertEqual("BLOCKED_SCORING_MAPPING_INCOMPLETE", gate["status"])
        self.assertIn("PLANNED_CONTENT_UNIT_NOT_IN_PLAN",
                      gate["unmapped_records"][0]["missing"])
        self.assertEqual(["CU-NOPE"], gate["unmapped_records"][0]["invalid_content_units"])

    def test_b3_invalid_cu_only_blocks(self):
        gate = self._gate(_record(cu=("CU-NOPE",)))
        self.assertEqual("BLOCKED_SCORING_MAPPING_INCOMPLETE", gate["status"])

    def test_all_declared_targets_must_be_valid_not_any(self):
        """§5.3：一个主题映射多个 CU 时，声明为正式落点的**每一个**都得合法。"""
        gate = self._gate(_record(cu=("CU-001-01", "CU-002-01", "CU-GHOST")))
        self.assertEqual("BLOCKED_SCORING_MAPPING_INCOMPLETE", gate["status"])

    def test_all_valid_references_pass(self):
        gate = self._gate(_record(section="S02-01", cu=("CU-001-01", "CU-002-01")))
        self.assertEqual("PASS", gate["status"])
        self.assertTrue(gate["may_enter_generation"])
        self.assertTrue(gate["rules"]["references_verified_against_plan"])

    def test_reference_checking_reports_when_nothing_to_check_against(self):
        """没有计划清单可对照时必须**具名声明未校验**，不能把"非空字符串"当有效性。"""
        gate = pre_generation_scoring_mapping_gate(
            {"canonical_scoring_records": [_record(section="S99-99", cu=("CU-NOPE",))],
             "rows": [], "coverage_summary": {}})
        self.assertFalse(gate["rules"]["references_verified_against_plan"])

    def test_evidence_contract_references_stay_required(self):
        gate = self._gate(_record(contract={}))
        self.assertIn("EVIDENCE_CONTRACT", gate["unmapped_records"][0]["missing"])


class BFailClosedReaders(unittest.TestCase):
    """§5.7 B4/B5/B6 + §5.4：坏掉的 mapping artifact 必须在 Provider 之前 BLOCK。"""

    def _planned_run(self, tmp: Path) -> Path:
        """一次跑过规划的 Run：08 在场 ⇒ "这一单该有评分映射"（§5.5 的判据来源）。"""
        run = tmp / "run"
        (run / "08_document_hierarchy.json").parent.mkdir(parents=True, exist_ok=True)
        (run / "08_document_hierarchy.json").write_text(
            json.dumps({"schema_version": "document-hierarchy-v0.1", "sections": []}),
            encoding="utf-8")
        return run

    def _matrix(self, run: Path, payload) -> None:
        (run / "09_scoring_matrix.json").write_text(
            payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")

    def test_b4_missing_mapping_artifact_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._planned_run(Path(tmp))
            reading = read_pre_generation_scoring_mapping_gate(run)
        self.assertFalse(reading["may_enter_generation"])
        self.assertEqual("SCORING_MAPPING_ARTIFACT_MISSING", reading["status"])
        self.assertTrue(reading["scoring_mapping_expected"])

    def test_b5_unreadable_mapping_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._planned_run(Path(tmp))
            self._matrix(run, "{不是 JSON")
            reading = read_pre_generation_scoring_mapping_gate(run)
        self.assertFalse(reading["may_enter_generation"])
        self.assertEqual("SCORING_MAPPING_ARTIFACT_UNREADABLE", reading["status"])

    def test_b6_malformed_gate_payload_blocks(self):
        payloads = (
            {"canonical_scoring_records": [_record()]},                     # 门没被评估
            {"canonical_scoring_records": [_record()],
             "pre_generation_scoring_mapping_gate": {"status": "PASS"}},    # 缺必备字段
            {"canonical_scoring_records": [_record()],
             "pre_generation_scoring_mapping_gate": "PASS"},                # 形状就不对
        )
        for payload in payloads:
            with self.subTest(keys=sorted(payload)), tempfile.TemporaryDirectory() as tmp:
                run = self._planned_run(Path(tmp))
                self._matrix(run, payload)
                reading = read_pre_generation_scoring_mapping_gate(run)
                self.assertFalse(reading["may_enter_generation"], payload)
                self.assertNotEqual("PASS", reading["status"])

    def test_production_entry_raises_before_any_provider_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._planned_run(Path(tmp))
            with self.assertRaises(PreGenerationScoringGateError) as raised:
                require_pre_generation_scoring_mapping_gate(run)
        self.assertIn("本轮未发起任何 Provider 调用", str(raised.exception))

    def test_b7_no_applicable_topic_stays_not_applicable(self):
        """§5.5：真正没有适用技术评分主题 ≠ 文件坏了，不得误杀。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = self._planned_run(Path(tmp))
            self._matrix(run, {"canonical_scoring_records": [], "rows": [],
                              "pre_generation_scoring_mapping_gate": {
                                  "status": "NOT_APPLICABLE",
                                  "may_enter_generation": True,
                                  "substantive_topic_total": 0,
                                  "mapped_total": 0, "unmapped_total": 0}})
            reading = read_pre_generation_scoring_mapping_gate(run)
        self.assertTrue(reading["may_enter_generation"])
        self.assertEqual("NOT_APPLICABLE", reading["status"])

    def test_run_without_planning_is_not_claimed_as_verified(self):
        """没有规划产物就没有评分映射可言：放行，但必须具名 NOT_PLANNED 而不是 PASS。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            run.mkdir(parents=True)
            reading = read_pre_generation_scoring_mapping_gate(run)
        self.assertTrue(reading["may_enter_generation"])
        self.assertEqual("SCORING_MAPPING_NOT_PLANNED", reading["status"])
        self.assertFalse(reading["scoring_mapping_expected"])

    def test_b7_legitimate_na_does_not_need_the_plan_tree_to_be_read(self):
        """§5.5 反向哨兵：合法 N/A **不得**被"拿不到清单来核对引用"误杀。

        引用重算是为"声称映射成功了"的门结论设的；零主题没有引用可查，
        这时要求 08 在场就把 §5.5 要求保住的 N/A 关掉了（返修过程中真踩过这一次）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            run.mkdir(parents=True)
            self._matrix(run, {"canonical_scoring_records": [], "rows": [],
                               "pre_generation_scoring_mapping_gate": {
                                   "status": "NOT_APPLICABLE", "may_enter_generation": True,
                                   "substantive_topic_total": 0, "mapped_total": 0,
                                   "unmapped_total": 0}})
            reading = read_pre_generation_scoring_mapping_gate(run)
        self.assertTrue(reading["may_enter_generation"], reading)
        self.assertEqual("NOT_APPLICABLE", reading["status"])

    def test_gate_file_is_read_but_never_authorizes_without_the_canonical_matrix(self):
        """Round 4.3 §4/§5 更正本文件上一轮的那条退路（原 `test_gate_file_may_still_be_read_standalone`）。

        上一轮这里钉的是"09 不在但独立门文件合法 ⇒ 读它并放行"。Codex 的第二次 Final Gate
        指出这条退路本身就是错的 Authority 关系：**派生文件自报 PASS 替代了 canonical 输入校验**。
        现在门文件仍然要"看得见"（排查时要知道是谁在误导），但不再构成放行凭据。
        """
        with tempfile.TemporaryDirectory() as tmp:
            run = self._planned_run(Path(tmp))
            (run / "pre_generation_scoring_mapping_gate.json").write_text(json.dumps({
                "status": "PASS", "may_enter_generation": True,
                "substantive_topic_total": 1, "mapped_total": 1, "unmapped_total": 0}),
                encoding="utf-8")
            reading = read_pre_generation_scoring_mapping_gate(run)
        self.assertFalse(reading["may_enter_generation"],
                         "canonical 矩阵不在场，派生门文件的自报 PASS 又把人放进去了")
        self.assertEqual("SCORING_MAPPING_ARTIFACT_MISSING", reading["status"])
        self.assertEqual("PASS", reading["declared"]["derived_file_status"],
                         "声称必须照旧看得见，只是不算数")
        self.assertFalse(reading["declared"]["derived_file_honored"])

    def test_source_no_longer_consumes_the_gate_with_a_falsy_shortcut(self):
        """§7：调用点不得再用"空对象即跳过"的形状消费这道门。

        扫的是**代码**：注释与 docstring 里允许出现旧写法（本文件与 orchestrator 的说明
        就得引用它，否则"这里曾经 fail-open"这件事没法留在代码里解释给后来的人）。
        """
        text = (APP / "longform" / "orchestrator.py").read_text(encoding="utf-8")
        non_code = set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                non_code.update(range(node.lineno, getattr(node, "end_lineno", node.lineno) + 1))
        hits = []
        for number, line in enumerate(text.splitlines(), 1):
            if number in non_code or line.lstrip().startswith("#"):
                continue
            if re.search(r"if\s+(scoring_mapping|mapping_gate|reading)\s+and\s+not", line):
                hits.append(f"{number}:{line.strip()}")
        self.assertEqual([], hits, "生成入口仍在用短路形状消费评分门")


class BProductionEntryObserver(unittest.TestCase):
    """§5.7 B10：故意破坏 Mapping 后，生产入口的 Provider 观察者必须一条请求都没收到。

    走的是 `generate_longform` 真身（规划产物已落盘 ⇒ 复用计划那条正式生产路），
    不改代码、不 mock 生产路径：把 09 改成"引用计划里查无此节"，再数请求。
    """

    def _planned_run(self, root: Path) -> Path:
        from longform.orchestrator import write_plan_artifacts
        from planning.planner import build_planning_bundle
        from test_longform_orchestrator import _brief, _pack, _selection

        pack = _pack()
        run = root / "run"
        (run / "tender").mkdir(parents=True)
        (run / "tender" / "requirement_pack.json").write_text(
            json.dumps(pack, ensure_ascii=False), encoding="utf-8")
        bundle = build_planning_bundle(pack, _brief(), _selection(), production=True)
        write_plan_artifacts(run, bundle)
        return run

    def _broken_matrix(self, run: Path) -> int:
        matrix = json.loads((run / "09_scoring_matrix.json").read_text(encoding="utf-8"))
        records = matrix.get("canonical_scoring_records") or []
        if not records:
            records = matrix["canonical_scoring_records"] = [{
                "canonical_scoring_id": "SCN-999", "topic": "返修反例主题",
                "source_ids": ["SC-99"], "planned_response_cu": ["CU-001-01"],
                "evidence_contract": {"concepts": ["覆盖范围"], "min_concepts": 1}}]
        for row in records:
            row["planned_response_section"] = "S99-99"
            row["planned_response_sections"] = ["S99-99"]
        matrix["pre_generation_scoring_mapping_gate"] = {
            "status": "PASS", "may_enter_generation": True,
            "substantive_topic_total": len(records), "mapped_total": len(records),
            "unmapped_total": 0}
        (run / "09_scoring_matrix.json").write_text(
            json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
        return len(records)

    def test_b10_broken_mapping_reaches_zero_provider_requests(self):
        from longform.orchestrator import generate_longform
        from planning.scoring_taxonomy import pre_generation_scoring_mapping_gate
        from providers.mock import MockProvider
        from test_longform_orchestrator import _brief, _selection

        with tempfile.TemporaryDirectory() as tmp:
            run = self._planned_run(Path(tmp))
            if not (run / "09_scoring_matrix.json").is_file():
                self.fail("规划链没有产出 09_scoring_matrix.json，B10 无从破坏")
            total = self._broken_matrix(run)
            gate = pre_generation_scoring_mapping_gate(
                json.loads((run / "09_scoring_matrix.json").read_text(encoding="utf-8")),
                planned_unit_ids=["CU-001-01"], planned_section_ids=["S01-01"])
            self.assertFalse(gate["may_enter_generation"], f"{total} 条幽灵落点没被判住")
            provider = MockProvider({"provider_name": "mock", "provider_type": "mock",
                                     "enabled": True, "model": "mock-longform"})
            with self.assertRaises(PreGenerationScoringGateError):
                generate_longform(run_dir=run, provider=provider, provider_name="mock",
                                  brief=_brief(), selection=_selection(),
                                  selected_ids=["KU-0001"], task_mode="standard")
            self.assertEqual([], getattr(provider, "received_requests", []),
                             "评分映射已被破坏，生产入口还是把请求发给了 Provider")


class CFinalizerFailClosed(unittest.TestCase):
    """§6.4 C1–C8：一律调用正式 `finalize_delivery_state`，断言最终落盘结果。"""

    def _finalized(self, **fixture):
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), **fixture)
            _fake_clean_surfaces(run)
            state = finalize_delivery_state(run, medium="WORD",
                                            draft_delivery="COMPLETED_CLEAN")
            on_disk = json.loads((run / "run_state.json").read_text(encoding="utf-8"))
            surfaces = {}
            for rel, keys in SURFACE_DELIVERY_KEYS.items():
                path = run / rel
                if not path.is_file():
                    continue
                row = json.loads(path.read_text(encoding="utf-8"))
                surfaces[rel] = {key: _surface_value(row, key) for key in keys}
        return state, on_disk, surfaces

    def _assert_no_clean_on_surfaces(self, surfaces) -> None:
        for rel, values in surfaces.items():
            for key, value in values.items():
                self.assertNotEqual("COMPLETED_CLEAN", value, f"{rel}:{key} 还留着假 Clean")

    def test_c1_to_c5_unmeasured_qa_shapes_are_never_clean(self):
        shapes = {
            "missing": {"final_qa": None},
            # Round 4.7：这份尺量的是"QA 读不出来 ⇒ 终态不得 Clean"。产物落位后做数的
            # 是 `final_artifact_qa.json`，所以这个形状把同一份坏 JSON 写到**两份**报告上 ——
            # 两条腿（with_final True/False）各读各的那一份，判据不变。
            "unreadable": {"final_qa": "PASS", "final_qa_raw": "{不是 JSON",
                           "artifact_qa_raw": "{不是 JSON"},
            "null_status": {"final_qa": {"status": None, "findings": []}},
            "unknown_token": {"final_qa": {"status": "MAYBE_OK", "findings": []}},
            "not_measured": {"final_qa": {"status": "NOT_MEASURED", "findings": []}},
        }
        for case, fixture in shapes.items():
            # 两种形状都要过：最终产物已落位（走 QA 判决那条链），以及产物尚未落位
            # （走"不得继承 draft"那条链）—— Codex 的 C1 复现出来的正是后者。
            for with_final in (True, False):
                with self.subTest(case=case, final_artifact=with_final):
                    _, on_disk, surfaces = self._finalized(
                        pages=286, page_targets=(250, 320), with_final=with_final, **fixture)
                    self.assertNotEqual("COMPLETED_CLEAN",
                                        on_disk["derived"]["delivery_status"], case)
                    self._assert_no_clean_on_surfaces(surfaces)

    def test_c6_c7_review_and_block_stay_distinct_and_not_clean(self):
        expected = {"NEEDS_REVIEW": QA_REVIEW, "AUTO_REPAIR": QA_REVIEW,
                    "FAIL": QA_BLOCK, "BLOCK": QA_BLOCK}
        for word, qa_state in expected.items():
            with self.subTest(qa=word):
                _, on_disk, _ = self._finalized(final_qa=word, pages=286,
                                                page_targets=(250, 320))
                self.assertEqual(qa_state, on_disk["derived"]["final_qa_state"], word)
                self.assertNotEqual("COMPLETED_CLEAN", on_disk["derived"]["delivery_status"])

    def test_c8_legitimate_clean_is_still_reachable(self):
        """正向对照：全门齐 + 实测带内 + QA PASS —— 必须仍是 Clean，返修不是把 Clean 关掉。"""
        state, on_disk, surfaces = self._finalized(final_qa="PASS", pages=286,
                                                   page_targets=(250, 320))
        self.assertEqual("COMPLETED_CLEAN", state["derived"]["delivery_status"])
        self.assertEqual([], state["derived"]["final_qa_blockers"])
        self.assertEqual("COMPLETED_CLEAN", on_disk["derived"]["delivery_status"])
        for rel, values in surfaces.items():
            for key, value in values.items():
                self.assertEqual("COMPLETED_CLEAN", value,
                                 f"六面里的 {rel}:{key} 没被投影到真实 Clean")

    def test_clean_and_qa_page_invariants_never_coexist_on_disk(self):
        """§6.3：禁止 `COMPLETED_CLEAN` 与 guard 类不变式 FAIL 同时落盘。"""
        for fixture in ({"final_qa": None},
                        {"pages": 244, "page_targets": (250, 320)},
                        {"pages": 400, "page_targets": (250, 320)}):
            with self.subTest(fixture=sorted(fixture)):
                _, on_disk, _ = self._finalized(**{"final_qa": "PASS", **fixture})
                clean = on_disk["derived"]["delivery_status"] == "COMPLETED_CLEAN"
                guarding = {key: value for key, value in on_disk["invariants"].items()
                            if key.startswith(("STATE-07", "STATE-08"))}
                self.assertFalse(clean and any(v == "FAIL" for v in guarding.values()),
                                 f"Clean 与 {guarding} 同时落盘")

    def test_missing_final_artifact_cannot_inherit_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), pages=286, page_targets=(250, 320), with_final=False)
            _fake_clean_surfaces(run)
            state = finalize_delivery_state(run, medium="WORD",
                                            draft_delivery="COMPLETED_CLEAN")
            plan = json.loads((run / "11_volume_generation_plan.json").read_text(encoding="utf-8"))
        self.assertNotEqual("COMPLETED_CLEAN", state["derived"]["delivery_status"])
        self.assertEqual("INCOMPLETE", state["derived"]["internal_phase"])
        self.assertNotEqual("COMPLETED_CLEAN", plan.get("delivery_status"))


class CrossBlockerCases(unittest.TestCase):
    """§6.4 C9/C10：三条 blocker 的交叉反例。"""

    def test_c9_qa_pass_with_under_length_is_not_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            derived = _derive(Path(tmp), final_qa="PASS", pages=244,
                              page_targets=(250, 320))
        self.assertNotEqual("COMPLETED_CLEAN", derived["delivery_status"])
        self.assertIn("PAGE_BAND_UNDER_LENGTH", derived["final_qa_blockers"])

    def test_c10_qa_pass_with_unmeasured_scoring_is_not_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), pages=286, page_targets=(250, 320))
            (run / "09_scoring_matrix.json").write_text(json.dumps({
                "canonical_scoring_records": [{
                    "canonical_scoring_id": "SCN-001", "topic": "项目服务方案",
                    "source_ids": ["SC-01"],
                    "fulfillment": {"status": "NOT_GENERATED"}}]}), encoding="utf-8")
            derived = derive_run_state(collect_facts(run, medium="WORD"))
        self.assertNotEqual("COMPLETED_CLEAN", derived["delivery_status"])
        self.assertTrue([b for b in derived["final_qa_blockers"]
                         if str(b).startswith("SCORING_GATE_")])

    def test_two_failures_report_independently_not_merged(self):
        """两条都不满足时各自具名，不得糊成一个 NEEDS_REVIEW。"""
        with tempfile.TemporaryDirectory() as tmp:
            derived = _derive(Path(tmp), final_qa="BLOCK", pages=244,
                              page_targets=(250, 320))
        self.assertEqual(QA_BLOCK, derived["final_qa_state"])
        self.assertIn("FINAL_QA_BLOCKED", derived["final_qa_blockers"])
        self.assertIn("PAGE_BAND_UNDER_LENGTH", derived["final_qa_blockers"])


if __name__ == "__main__":
    unittest.main()
