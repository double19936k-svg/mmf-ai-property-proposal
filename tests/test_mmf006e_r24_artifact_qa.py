from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from docx import Document

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from governance.artifact_qa import apply_artifact_repairs, evaluate_artifact, normalize_quotes
from governance.commitment_provenance import evaluate_commitments
from governance.final_artifact_qa import evaluate_docx_artifact
from longform.factory import LongformGenerationFactory, required_output_coverage
from longform.orchestrator import generate_longform
from planning.planner import _planning_requirements
from providers.mock import MockProvider
from tender_intake.pack_builder import _tender_content_class
from app_core import _automatic_repair_limit, _recoverable_integrity_failure

from tests.test_longform_orchestrator import _brief, _pack, _selection, _write_run


class EmptyThenHealthyProvider(MockProvider):
    def __init__(self, fail_calls: int):
        super().__init__({"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock-r24"})
        self.calls = 0
        self.fail_calls = fail_calls

    def invoke_structured(self, request, task_dir):
        self.calls += 1
        return super().invoke_structured(request, task_dir)

    def _section_fragment(self, request):
        fragment = super()._section_fragment(request)
        if self.calls <= self.fail_calls:
            fragment["body_blocks"] = [{"type": "subheading", "content": "关键岗位职责边界"}]
            fragment["content_unit_fragments"] = []
            fragment["tables"] = []
            fragment["processes"] = []
            fragment["callouts"] = []
        return fragment


class RecoverableIntegrityUxTest(unittest.TestCase):
    def _run(self, provider):
        holder = tempfile.TemporaryDirectory()
        run = _write_run(Path(holder.name), _pack(), _brief(), _selection())
        result = generate_longform(
            run_dir=run,
            provider=provider,
            provider_name="mock",
            brief=_brief(),
            selection=_selection(),
            selected_ids=["KU-0001"],
            section_ids=["S01-01"],
            task_mode="standard",
            require_section_min=True,
        )
        return holder, run, result

    def test_qa_ux_01_and_02_auto_repair_without_terminal_event(self):
        provider = EmptyThenHealthyProvider(fail_calls=1)
        holder, run, result = self._run(provider)
        try:
            checkpoint = json.loads((run / "checkpoint" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(result["word"]["completed_sections"], result["word"]["total_sections"])
            # CU-aware repair may require one CU contract repair plus one
            # bounded depth continuation; both remain fully automatic.
            self.assertGreaterEqual(provider.calls, 2)
            self.assertLessEqual(provider.calls, 3)
            self.assertTrue(any(row.get("result") == "PASS" for row in checkpoint.get("qa_events") or []))
            self.assertFalse(any(row.get("result") == "TERMINAL_BLOCKED" for row in checkpoint.get("qa_events") or []))
        finally:
            holder.cleanup()

    def test_qa_ux_03_terminal_only_after_two_repairs_fail(self):
        provider = EmptyThenHealthyProvider(fail_calls=99)
        holder, run, result = self._run(provider)
        try:
            checkpoint = json.loads((run / "checkpoint" / "state.json").read_text(encoding="utf-8-sig"))
            terminal = [row for row in checkpoint.get("qa_events") or [] if row.get("result") == "TERMINAL_BLOCKED"]
            # 生成阶段内允许的有限重试 + 文档级QA的 1 次有界修复；MMF-007C 之后
            # 不再有"整篇重跑"的第二、第三轮。
            self.assertGreaterEqual(provider.calls, 3, "CU契约修复与深度续写仍在生成阶段内完成")
            self.assertLessEqual(provider.calls, 4, "QA之后最多再花一次有界修复预算，不得整篇重跑")
            self.assertEqual(result["word"]["TOTAL_CALLS"], provider.calls)
            self.assertEqual(result["word"]["completed_sections"], 0)
            self.assertEqual(len(terminal), 1)
            # 失败仍然显式可见；阶段通道随后进入 ASSEMBLING，因为部分文档照常装配，
            # 终态由 qa_events + run_audit 承载，而不是把阶段卡在 AUTO_REPAIRING。
            self.assertEqual(result["word"]["TECHNICAL_FAILED_SECTIONS"], ["S01-01"])
            self.assertEqual(checkpoint.get("public_phase"), "ASSEMBLING")
        finally:
            holder.cleanup()

    def test_batch_empty_body_is_section_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            factory = object.__new__(LongformGenerationFactory)
            output = {
                "batch_id": "GB-1",
                "sections": [
                    {"section_id": "S01", "title": "正常", "body_blocks": [{"type": "paragraph", "content": "这是满足最小有效正文要求的完整内容。"}]},
                    {"section_id": "S02", "title": "空节", "body_blocks": []},
                ],
            }
            fragments, issues = factory._split_batch_output(output, ["S01", "S02"], expected_batch_id="GB-1")
            self.assertIn("S01", fragments)
            self.assertIn("S02", fragments)
            self.assertIn("empty_body:S02", issues)
            self.assertNotIn("missing:S01", issues)

    def test_app_layer_keeps_recoverable_integrity_repair_internal(self):
        compliance = {"status": "PASS"}
        commitment = {"status": "PASS"}
        artifact = {"status": "BLOCK", "findings": [{"rule_id": "LF-002", "severity": "BLOCK"}]}
        self.assertTrue(_recoverable_integrity_failure(compliance, commitment, artifact, {}))
        self.assertEqual(_automatic_repair_limit(compliance, commitment, artifact, {}), 3)

    def test_customer_ui_does_not_offer_manual_integrity_repair_button(self):
        page = (APP.parent / "static" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('id="repair-generation"', page)


class ArtifactHygieneTest(unittest.TestCase):
    def test_art_01_internal_meta_removed_and_idempotent(self):
        payload = {"artifact": {"paragraphs": [
            "本方案按统一 Document Plan 分章节编制，结构由 MMF规划层控制，模型仅撰写已确定的 Section。",
            "明确项目事实、承诺与条件性方法的区分，并形成承诺边界清单。",
        ]}}
        self.assertEqual(evaluate_artifact(payload)["status"], "AUTO_REPAIR")
        once = apply_artifact_repairs(payload)
        twice = apply_artifact_repairs(once)
        self.assertEqual(once, twice)
        self.assertEqual(evaluate_artifact(once)["status"], "PASS")

    def test_art_02_tender_template_classification_and_block(self):
        text = "本授权书声明，被授权人的姓名、职务由投标人填写。"
        self.assertEqual(_tender_content_class(text, "other", {}), "AUTHORIZATION_TEMPLATE")
        pack = _pack()
        pack["requirements"].append({
            "requirement_id": "REQ-AUTH",
            "normalized_requirement": text,
            "confirmation_status": "CONFIRMED",
            "classification": "PROJECT_SPECIFIC",
            "tender_content_class": "AUTHORIZATION_TEMPLATE",
            "excluded_from_solution_body": True,
        })
        self.assertNotIn("REQ-AUTH", {row["requirement_id"] for row in _planning_requirements(pack)})
        report = evaluate_artifact({"artifact": {"paragraphs": [text]}})
        self.assertTrue(any(row["rule_id"] == "TENDER_TEMPLATE_LEAK" for row in report["findings"]))

    def test_art_03_project_fact_conflict_blocks(self):
        report = evaluate_artifact(
            {"artifact": {"paragraphs": ["项目总建筑面积为252567平方米。", "项目总面积约15.27万平方米。"]}},
            canonical_facts={"project_facts": {"gross_area": {"value": "252567平方米"}}},
        )
        self.assertTrue(any(row["rule_id"] == "PROJECT_FACT_CONSISTENCY" for row in report["findings"]))
        self.assertEqual(report["status"], "BLOCK")

    def test_art_03b_confirmed_subbuilding_area_is_not_project_fact_drift(self):
        report = evaluate_artifact(
            {"artifact": {"paragraphs": [
                "项目总建筑面积为252567平方米。",
                "其中1#楼为公寓，总建筑面积约3.96万平方米。",
            ]}},
            canonical_facts={
                "project_facts": {"gross_area": {"value": "252567平方米"}},
                "client_requirements": [
                    {"requirement_id": "REQ-0154", "text": "1#为公寓，总建筑面积约3.96万平方米"},
                ],
            },
        )
        self.assertFalse(any(row["rule_id"] == "PROJECT_FACT_CONSISTENCY" for row in report["findings"]))

    def test_art_03c_customer_facing_output_aliases_satisfy_contract(self):
        text = "项目核心数据：总建筑面积252567平方米。服务范围与责任边界如下。"
        self.assertEqual(required_output_coverage("项目事实清单", text), "COVERED")
        self.assertEqual(required_output_coverage("服务边界清单", text), "COVERED")

    def test_art_04_unsupported_completion_claim_blocks(self):
        text = "所有拟派人员履历及类似项目经验已整理成册，作为本方案附件提交。"
        report = evaluate_commitments({}, [], {"artifact": {"paragraphs": [text]}})
        self.assertTrue(any(row["claim_type"] == "UNSUPPORTED_COMPLETION_CLAIM" for row in report["violations"]))

    def test_art_05_absolute_commitment_repaired_or_blocked(self):
        payload = {"artifact": {"paragraphs": ["确保强电、给排水、暖通等关键系统的零故障运行。"]}}
        report = evaluate_commitments({}, [], payload)
        self.assertTrue(any(row["claim_type"] == "ABSOLUTE_PERFORMANCE_COMMITMENT" for row in report["violations"]))
        repaired = apply_artifact_repairs(payload)
        self.assertNotIn("零故障", str(repaired))

    def test_art_06_broken_quotes_block_and_normalize(self):
        polluted = "从“ ”“ ”顾问指导 到 实操落地，形成专业“ ”赋能。"
        report = evaluate_artifact({"artifact": {"paragraphs": [polluted]}})
        self.assertTrue(any(row["rule_id"] == "QUOTE_BALANCE_QA" for row in report["findings"]))
        clean = normalize_quotes(polluted)
        self.assertNotIn("“ ”", clean)
        self.assertEqual(normalize_quotes(clean), clean)

    def test_art_07_final_docx_empty_heading_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty-heading.docx"
            doc = Document()
            doc.add_heading("关键岗位职责边界", level=1)
            doc.add_heading("跨专业协同与升级机制", level=1)
            doc.add_paragraph("围绕跨专业事项明确协同动作、升级路径和闭环记录。")
            doc.save(path)
            report = evaluate_docx_artifact(path)
            self.assertTrue(report["actual_docx_inspected"])
            self.assertTrue(any(row["rule_id"] == "EMPTY_HEADING_BLOCK" for row in report["findings"]))
            self.assertEqual(report["status"], "BLOCK")

    def test_clean_actual_docx_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clean.docx"
            doc = Document()
            doc.add_heading("项目服务思路", level=1)
            doc.add_paragraph("围绕项目实际需求建立职责清晰、过程可查、结果可复核的物业服务机制。")
            doc.save(path)
            report = evaluate_docx_artifact(path)
            self.assertTrue(report["actual_docx_inspected"])
            self.assertEqual(report["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
