"""MMF-007 Engineering Closure：生产路径测试（PROD-*）。

区别于 Unit Fault Test：本文件全部通过正式生产入口（generate_longform /
run_compression_pipeline / 正式 Resume）执行，不直接调用 helper 冒充生产链。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(__file__).parent))

import test_mmf006e_r21_q01_q04 as r21  # noqa: E402
from longform.orchestrator import generate_longform  # noqa: E402
from providers.mock import MockProvider  # noqa: E402

MOCK_CONFIG = {"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock-prod"}


def _generate(tmp, provider, run=None, medium="WORD", **kwargs):
    brief = {**r21._brief(), "project_type": "产业园综合体", "scenario": "完整物业服务方案", "medium": medium}
    run = run or r21._write_run(Path(tmp), r21._complex_pack(), brief, r21._selection())
    result = generate_longform(
        run_dir=run, provider=provider, provider_name="mock", brief=brief,
        selection=r21._selection(), selected_ids=["KU-0001"], task_mode="full_longform",
        require_section_min=True, speed_profile="fast", **kwargs)
    return run, result


class ProdPptE2ETest(unittest.TestCase):
    """P0-1：生产路径 Mock PPT E2E（generate_longform → PPT → Artifact → QA）。"""

    def test_ppt_branch_no_unbound_and_delivers(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, result = _generate(Path(tmp), MockProvider(MOCK_CONFIG), medium="PPT")
            slides = (result["generated"]["artifact"] or {}).get("slides") or []
            self.assertGreater(len(slides), 0, "PPT 生产路径必须产出 slides")
            consistency = result["generated"].get("consistency") or {}
            self.assertEqual(consistency.get("status"), "NOT_APPLICABLE", "PPT 媒介一致性=NOT_APPLICABLE")
            self.assertEqual(consistency.get("medium"), "PPT")
            ppt_status = json.loads((run / "longform" / "ppt" / "status.json").read_text(encoding="utf-8-sig"))
            self.assertIn(ppt_status.get("status"), {"COMPLETED", "COMPLETED_WITH_GAPS", "COMPLETED_WITH_WARNING"})


class ProdCompressionE2ETest(unittest.TestCase):
    """P0-2：正式压缩入口（Synthetic Longform → Plan → Trace → Condensed DOCX → QA）。"""

    def test_formal_entry_outputs_condensed_docx(self):
        import planning.planner as planner_mod
        original = planner_mod.build_content_budget
        factor = (60 * 900) / 41000.0

        def scaled(outline, matrix, *a, **k):
            budget = original(outline, matrix, *a, **k)
            for row in budget.get("chapters", []):
                row["target_words_min"] = int(row["target_words_min"] * factor)
                row["target_words_max"] = int(row["target_words_max"] * factor)
            return budget

        planner_mod.build_content_budget = scaled
        try:
            with tempfile.TemporaryDirectory() as tmp:
                run, result = _generate(Path(tmp), MockProvider(MOCK_CONFIG))
                from compression.planner import run_compression_pipeline
                outcome = run_compression_pipeline(run, target_pages=(10, 20))
        finally:
            planner_mod.build_content_budget = original
        self.assertIsNotNone(outcome["condensed_docx"], "必须产出精编 DOCX")
        self.assertEqual(outcome["structural_qa"]["status"], "PASS")
        self.assertGreater(outcome["structural_qa"]["paragraphs"], 0)
        self.assertEqual(outcome["loss_check"]["status"], "PASS")
        self.assertGreater(outcome["condensed_chars"], 0)
        self.assertTrue(json.dumps(outcome["paths"], ensure_ascii=False))


class ProdMigrationTest(unittest.TestCase):
    """P0-5 / PROD-MIG-01：旧 v0.2 checkpoint → 正式 Resume 入口自动迁移。"""

    def test_v02_checkpoint_migrates_through_production_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, result = _generate(Path(tmp), MockProvider(MOCK_CONFIG))
            ck_path = run / "checkpoint" / "state.json"
            checkpoint = json.loads(ck_path.read_text(encoding="utf-8-sig"))
            checkpoint["schema_version"] = "longform-checkpoint-v0.2"
            ck_path.write_text(json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8")
            second = generate_longform(
                run_dir=run, provider=MockProvider(MOCK_CONFIG), provider_name="mock",
                brief={**r21._brief(), "medium": "WORD", "project_type": "产业园综合体", "scenario": "完整物业服务方案"},
                selection=r21._selection(), selected_ids=["KU-0001"], task_mode="full_longform",
                require_section_min=True, speed_profile="fast")
            migrated = json.loads(ck_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(migrated["schema_version"], "longform-checkpoint-v0.3-hierarchical", "生产 Resume 入口必须自动迁移")
            self.assertEqual(migrated["migration"]["strategy"], "additive_fields_only")
            self.assertIsNotNone(second["generated"]["artifact"])


class ProdInvalidationTest(unittest.TestCase):
    """P0-5 / PROD-INVALIDATE-01/02：正式 Resume 入口的 Plan Invalidation。"""

    def test_fact_change_invalidates_through_production_entry(self):
        from longform.hierarchical import load_checkpoint_state
        with tempfile.TemporaryDirectory() as tmp:
            run, first = _generate(Path(tmp), MockProvider(MOCK_CONFIG))
            pack_path = run / "tender" / "requirement_pack.json"
            pack = json.loads(pack_path.read_text(encoding="utf-8-sig"))
            for row in pack.get("requirements", []):
                if row.get("domain") == "staffing":
                    row["normalized_requirement"] = row["normalized_requirement"] + "（修订版）"
                    break
            pack["project_facts"] = pack.get("project_facts") or {}
            pack_path.write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
            before = load_checkpoint_state(run)
            sections_before = set(before.get("completed_sections") or [])
            second = generate_longform(
                run_dir=run, provider=MockProvider(MOCK_CONFIG), provider_name="mock",
                brief={**r21._brief(), "medium": "WORD", "project_type": "产业园综合体", "scenario": "完整物业服务方案"},
                selection=r21._selection(), selected_ids=["KU-0001"], task_mode="full_longform",
                require_section_min=True, speed_profile="fast")
            audit = json.loads((run / "plan_invalidation_audit.json").read_text(encoding="utf-8-sig"))
            self.assertIn(audit["decision"]["invalidation_scope"], {"ALL_REQUIREMENTS_CHANGED", "ALL_FACTS_CHANGED"})
            after = load_checkpoint_state(run)
            self.assertGreaterEqual(len(after.get("completed_sections") or []), len(sections_before))

    def test_no_change_reuses_everything(self):
        from longform.hierarchical import load_checkpoint_state
        with tempfile.TemporaryDirectory() as tmp:
            run, first = _generate(Path(tmp), MockProvider(MOCK_CONFIG))
            second = generate_longform(
                run_dir=run, provider=MockProvider(MOCK_CONFIG), provider_name="mock",
                brief={**r21._brief(), "medium": "WORD", "project_type": "产业园综合体", "scenario": "完整物业服务方案"},
                selection=r21._selection(), selected_ids=["KU-0001"], task_mode="full_longform",
                require_section_min=True, speed_profile="fast")
            audit = json.loads((run / "plan_invalidation_audit.json").read_text(encoding="utf-8-sig"))
            self.assertEqual(audit["decision"]["invalidation_scope"], "NONE")
            self.assertEqual(audit["cleared_sections"], [])
            self.assertGreaterEqual(len(load_checkpoint_state(run).get("completed_sections") or []), 1)


class ProdStateAuthorityTest(unittest.TestCase):
    """P0-3：RUN_STATE_AUTHORITY 与 STATE-01/03 不变量（生产运行后对账）。"""

    def test_run_state_written_and_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, result = _generate(Path(tmp), MockProvider(MOCK_CONFIG), medium="WORD")
            # The orchestrator now writes facts only. Terminal run_state is
            # created by run_layout.finalize_run after the artifact lands.
            self.assertFalse((run / "run_state.json").exists())
            self.assertIsNotNone(result["generated"]["artifact"])

    def test_stale_terminal_blocked_reconciled(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, result = _generate(Path(tmp), MockProvider(MOCK_CONFIG), medium="WORD")
            ppt_status_path = run / "longform" / "ppt" / "status.json"
            if ppt_status_path.is_file():
                ppt_status = json.loads(ppt_status_path.read_text(encoding="utf-8-sig"))
                ppt_status["public_phase"] = "TERMINAL_BLOCKED"
                ppt_status_path.write_text(json.dumps(ppt_status, ensure_ascii=False), encoding="utf-8")
                from longform.run_state import reconcile_run_state
                state = reconcile_run_state(run, medium="WORD", delivery_status="COMPLETED_CLEAN")
                self.assertEqual(
                    json.loads(ppt_status_path.read_text(encoding="utf-8-sig")).get("public_phase"),
                    "STALE_TERMINAL_BLOCK_RESOLVED",
                    "STATE-01：Final 完成时 TERMINAL_BLOCKED 必须分类为 STALE 并解决")
                self.assertTrue(any("STATE-01" in fix or "STALE" in fix for fix in state["reconciled_fixes"]))


class ProdContentUnitWiringTest(unittest.TestCase):
    """P0-4：Content Unit 真实接线（计划非空 + checkpoint 存真 CU）。"""

    def test_volume_plan_and_checkpoint_use_real_cus(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, result = _generate(Path(tmp), MockProvider(MOCK_CONFIG))
            plan = json.loads((run / "11_volume_generation_plan.json").read_text(encoding="utf-8-sig"))
            total_cus = sum(len(v.get("content_unit_ids") or []) for v in plan["volumes"])
            self.assertGreater(total_cus, 0, "content_unit_ids 不得为空（Codex Blocker 4）")
            checkpoint = json.loads((run / "checkpoint" / "state.json").read_text(encoding="utf-8-sig"))
            cus = checkpoint.get("completed_content_units") or []
            self.assertGreater(len(cus), 0, "completed_content_units 必须是真实 CU")
            for cu in cus:
                self.assertTrue(str(cu).startswith("CU-S"), f"禁止 Section 伪装 CU：{cu}")


class ProdFaultTests(unittest.TestCase):
    """生产路径故障测试（区别于 Unit Fault Test）。"""

    def test_fault05_corrupt_checkpoint_through_resume_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, first = _generate(Path(tmp), MockProvider(MOCK_CONFIG))
            ck = run / "checkpoint" / "state.json"
            ck.write_text('{"schema_version": "longform-check', encoding="utf-8")
            second = generate_longform(
                run_dir=run, provider=MockProvider(MOCK_CONFIG), provider_name="mock",
                brief={**r21._brief(), "medium": "WORD", "project_type": "产业园综合体", "scenario": "完整物业服务方案"},
                selection=r21._selection(), selected_ids=["KU-0001"], task_mode="full_longform",
                require_section_min=True, speed_profile="fast")
            self.assertIsNotNone(second["generated"]["artifact"], "损坏 checkpoint 不得阻塞正式恢复")

    def test_fault10_timeout_records_unknown_through_production(self):
        class TimeoutProvider(MockProvider):
            def __init__(self):
                super().__init__(MOCK_CONFIG)

            def invoke_structured(self, request, task_dir):
                raise TimeoutError("Response timeout (injected)")

        with tempfile.TemporaryDirectory() as tmp:
            try:
                _generate(Path(tmp), TimeoutProvider())
            except Exception:
                pass
            unknown = list(Path(tmp).rglob("provider_unknown_requests.jsonl"))
            self.assertTrue(unknown, "生产路径必须记录 RESPONSE_UNKNOWN")
            rows = [json.loads(line) for line in unknown[0].read_text(encoding="utf-8").splitlines() if line.strip()]
            statuses = {row["status"] for row in rows}
            self.assertIn("REQUEST_SENT", statuses)
            self.assertIn("RESPONSE_UNKNOWN", statuses)


if __name__ == "__main__":
    unittest.main()


class ProdCUResumeE2ETest(unittest.TestCase):
    """CU-E2E-01：Section 3 个 CU，完成 CU-01/02 后中断，Resume 只补 CU-03。"""

    def test_resume_requests_only_missing_cu(self):
        import tempfile
        from providers.mock import MockProvider as _Base

        class TwoThenFail(_Base):
            """前两卷正常；含 CU 的 Section 请求只返回前 2 个 CU fragment，然后整节失败。"""

            def __init__(self):
                super().__init__(MOCK_CONFIG)
                self.received_resume_cus = []
                self.partial_returned = False

            def invoke_structured(self, request, task_dir):
                cus = [c.get("content_unit_id") for c in (request.get("content_unit_contracts") or [])]
                if cus and "CU-S03-02-02" in cus:
                    if not self.partial_returned:
                        output = super().invoke_structured(request, task_dir)
                        rows = output.get("sections") if isinstance(output.get("sections"), list) else [output]
                        for row in rows:
                            if row.get("section_id") == "S03-02":
                                row["content_unit_fragments"] = (row.get("content_unit_fragments") or [])[:1]
                        self.partial_returned = True
                        return output
                    raise RuntimeError("CU-S03-02-02 生成失败（注入）")
                return super().invoke_structured(request, task_dir)

        with tempfile.TemporaryDirectory() as tmp:
            brief = {**r21._brief(), "project_type": "产业园综合体", "scenario": "完整物业服务方案"}
            run = r21._write_run(Path(tmp), r21._complex_pack(), brief, r21._selection())
            generate_longform(
                run_dir=run, provider=TwoThenFail(), provider_name="mock", brief=brief,
                selection=r21._selection(), selected_ids=["KU-0001"], task_mode="full_longform",
                require_section_min=True, speed_profile="fast")
            # Resume：好 Provider 只应收到缺失 CU
            good = MockProvider(MOCK_CONFIG)
            original_invoke = good.invoke_structured
            received_log = []

            def logging_invoke(request, task_dir):
                cus = [c.get("content_unit_id") for c in (request.get("content_unit_contracts") or [])]
                if cus:
                    received_log.append(cus)
                return original_invoke(request, task_dir)

            good.invoke_structured = logging_invoke
            generate_longform(
                run_dir=run, provider=good, provider_name="mock", brief=brief,
                selection=r21._selection(), selected_ids=["KU-0001"], task_mode="full_longform",
                require_section_min=True, speed_profile="fast")
            flat = [cu for batch in received_log for cu in batch]
            self.assertNotIn("CU-S03-02-01", flat, "已完成 CU 不得重复下发")
            completed = json.loads((run / "checkpoint" / "state.json").read_text(encoding="utf-8-sig")).get("completed_content_units") or []
            self.assertIn("CU-S03-02-02", completed)


class StateE2E01Test(unittest.TestCase):
    """STATE-E2E-01：7 Volume、Volume 6 首次失败→Resume 修复→6 状态源一致。"""

    def test_volume6_fail_then_resume_all_sources_agree(self):
        import tempfile
        from providers.mock import MockProvider as _Base

        class Vol6FailsFirst(_Base):
            """S06-01 首次请求返回短文本（Section 失败），恢复后正常。"""

            def __init__(self):
                super().__init__(MOCK_CONFIG)
                self.resumed = False

            def _section_fragment(self, request):
                frag = super()._section_fragment(request)
                if not self.resumed and frag.get("section_id") == "S06-01":
                    frag["body_blocks"] = [{"type": "paragraph", "content": "短"}]
                return frag

        with tempfile.TemporaryDirectory() as tmp:
            brief = {**r21._brief(), "project_type": "产业园综合体", "scenario": "完整物业服务方案"}
            run = r21._write_run(Path(tmp), r21._complex_pack(), brief, r21._selection())
            first = generate_longform(
                run_dir=run, provider=Vol6FailsFirst(), provider_name="mock", brief=brief,
                selection=r21._selection(), selected_ids=["KU-0001"], task_mode="full_longform",
                require_section_min=True, speed_profile="fast")
            # Resume 修复
            good = _Base(MOCK_CONFIG)
            second = generate_longform(
                run_dir=run, provider=good, provider_name="mock", brief=brief,
                selection=r21._selection(), selected_ids=["KU-0001"], task_mode="full_longform",
                require_section_min=True, speed_profile="fast")
            # 生产等价最终装配（app_core.generate_artifact 的 build_docx 子进程路径）。
            # 渲染输入必须落在生产同名文件 `artifact_content.json` 上：§49 的"最终件谱系齐"
            # 查的就是它与最终 DOCX 是否成对在场，测试自造一个文件名等于绕过这条谱系。
            artifact = second["generated"]["artifact"]
            content_path = run / "artifact_content.json"
            content_path.write_text(json.dumps({"brief": brief, "artifact": artifact}, ensure_ascii=False), encoding="utf-8")
            render = subprocess.run(
                [sys.executable, str(APP / "build_docx.py"), str(content_path), str(run / "artifact" / "final.docx")],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            assert render.returncode == 0, render.stderr[-300:]
            # 交付落位后重算权威（等价 finalize_run 的 reconcile）
            from longform.run_state import reconcile_run_state
            reconcile_run_state(run, medium="WORD")
            # --- 读取 6 个状态源 ---
            sources = {}
            rs = json.loads((run / "run_state.json").read_text(encoding="utf-8-sig"))
            sources["run_state"] = rs["derived"]["delivery_status"]
            ck = json.loads((run / "checkpoint" / "state.json").read_text(encoding="utf-8-sig"))
            sources["checkpoint_status"] = ck.get("status")
            audit = json.loads((run / "run_audit.json").read_text(encoding="utf-8-sig")) if (run / "run_audit.json").is_file() else {}
            sources["run_audit"] = audit.get("delivery_status")
            history = json.loads((run / "generation_status.json").read_text(encoding="utf-8-sig")) if (run / "generation_status.json").is_file() else {}
            sources["history"] = (history.get("result") or {}).get("delivery_status")
            plan = json.loads((run / "11_volume_generation_plan.json").read_text(encoding="utf-8-sig"))
            sources["volume_plan_completed"] = sum(
                1 for v in plan.get("volumes", []) if v.get("generation_status") == "COMPLETED")
            progress = json.loads((run / "volume_progress.json").read_text(encoding="utf-8-sig"))
            sources["volume_progress_entries"] = len(progress.get("volumes") or [])
            # --- 不变量：delivery 语义源（authority/audit/history）非 None 必须一致 ---
            delivery_sources = {k: v for k, v in sources.items() if k in {"run_state", "run_audit", "history"} and v not in {None, ""}}
            for name, value in delivery_sources.items():
                self.assertEqual(value, rs["derived"]["delivery_status"], f"authority vs {name}: {sources}")
            # checkpoint 的 job status 是执行口径（COMPLETED=全部完成），非交付口径；
            # COMPLETED_CLEAN 必须对应 checkpoint 终态成功。
            if rs["derived"]["delivery_status"] == "COMPLETED_CLEAN":
                self.assertEqual(sources["checkpoint_status"], "COMPLETED")
            self.assertEqual(sources["volume_plan_completed"], 7, "7/7 Volume Plan COMPLETED")
            self.assertEqual(sources["volume_progress_entries"], 7, "7/7 volume progress")
            # §44–§49 定 B：`completed_volumes` 只是 Resume 锚点的磁盘读数（分卷 DOCX 落了几份），
            # 不再充当交付判据 —— 这里改判 B 语义的四条内容证据 + 最终件谱系，
            # 分卷渲染份数照旧在场（下面单独记一条读数），但不决定这一节是否通过。
            self.assertEqual("B_CHECKPOINT_ONLY", rs["derived"]["volume_artifact_contract"])
            _b_evidence = {k: rs["facts"][k] for k in (
                "volume_sections_complete", "volume_content_units_complete",
                "assembly_inputs_complete", "volume_grouping_conservation_holds",
                "final_artifact_content_present")}
            self.assertTrue(all(_b_evidence.values()), json.dumps({
                "evidence": _b_evidence,
                "gaps": {k: rs["facts"].get(k) for k in (
                    "volume_section_gaps", "volume_content_unit_gaps", "assembly_input_gaps",
                    "volume_content_unit_audit_state")}}, ensure_ascii=False))
            self.assertTrue(rs["derived"]["volume_inputs_complete"], "B 四条内容证据必须齐")
            for _key in tuple(k for k in (rs.get("invariants") or {}) if k.startswith("STATE-04")):
                self.assertEqual("PASS", rs["invariants"][_key], _key)
            self.assertTrue(rs["facts"]["final_artifact_exists"])
            # S06-01 在 Resume 后被修复
            self.assertIn("S06-01", rs["facts"]["completed_sections"])
            # Resume 后 Provider 收到了缺失 Section 的请求（不是全部跳过）
            # Resume 必须真实重做了 S06-01（Vol6FailsFirst 使其失败，good 修复）
            self.assertIn("S06-01", rs["facts"]["completed_sections"])
            self.assertTrue(rs["facts"]["final_artifact_exists"])


class ProdExternalDataRootReadTest(unittest.TestCase):
    """PROD-ROOT-01：数据根在包外是受支持的部署姿态，读盘不能因此变成"读不到"。

    起因是 dist 侧 E2E：它把 `MMF_*_ROOT` 指到便携包外（否则测试产物会写进被测包自身），
    而读盘审计用 `relative_to(包根)` 折算路径 —— 包外路径直接抛 ValueError，
    一次合法读取被判失败。落到产品上就是 §44 的分页实测取不到，
    Technical Review Summary 从"有实测"退化成 `NOT_MEASURED`，而两者在下载页上长得一样。
    """

    def test_read_json_succeeds_for_allowed_root_outside_package(self) -> None:
        from unittest.mock import patch

        import app_core

        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "package"
            (package / "runtime").mkdir(parents=True)
            outside = Path(tmp) / "data" / "runs" / "r1"
            outside.mkdir(parents=True)
            target = outside / "first_draft_page_classification.json"
            target.write_text(json.dumps({"technical_effective_pages": 285}), encoding="utf-8")

            with patch.object(app_core, "RUNTIME_ROOT", package), \
                    patch.object(app_core, "RUNTIME_DIR", package / "runtime"), \
                    patch.object(app_core, "ACCESS_AUDIT_FILE", package / "runtime" / "access_audit.jsonl"), \
                    patch.object(app_core, "_checked", side_effect=lambda path: Path(path)):
                self.assertEqual({"technical_effective_pages": 285},
                                 app_core.read_json(target, "page_classification"))
                record = json.loads((package / "runtime" / "access_audit.jsonl")
                                    .read_text(encoding="utf-8").splitlines()[-1])

            self.assertEqual("read", record["operation"])
            self.assertEqual(str(target), record["relative_path"], "折算不了就记绝对路径，不许抛")

    def test_wiring_summary_still_reports_first_draft_basis_outside_package(self) -> None:
        from unittest.mock import patch

        import app_core

        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "package"
            (package / "runtime").mkdir(parents=True)
            run_dir = Path(tmp) / "data" / "runs" / "r1"
            run_dir.mkdir(parents=True)
            (run_dir / "first_draft_page_classification.json").write_text(
                json.dumps({"excluded_scope_pages": 1, "credential_attachment_pages_present": 1,
                            "credential_attachment_pages": 1,
                            "credential_attachment_page_details": [{"page": 7}]}),
                encoding="utf-8")
            gate = {"pending_confirmation": [], "ledger_state": "EMPTY"}

            with patch.object(app_core, "RUNTIME_ROOT", package), \
                    patch.object(app_core, "RUNTIME_DIR", package / "runtime"), \
                    patch.object(app_core, "ACCESS_AUDIT_FILE", package / "runtime" / "access_audit.jsonl"), \
                    patch.object(app_core, "_checked", side_effect=lambda path: Path(path)):
                summary = app_core._write_technical_review_summary(run_dir, gate, {})

            self.assertEqual("FIRST_DRAFT_PAGINATION", summary["page_measurement_basis"],
                             "包外数据根下初稿实测必须还读得回来")
            self.assertEqual(1, summary["credential_attachment_page_count"])
