from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from docx import Document


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
FIXTURE = ROOT / "tests" / "fixtures" / "real_artifact_glm_hisense_20260910"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from compliance import evaluate_compliance  # noqa: E402
from governance.artifact_qa import apply_artifact_repairs, evaluate_artifact  # noqa: E402
from governance.text_sanitize import has_conditional_postprocess_pollution, rewrite_unconfirmed_durations  # noqa: E402
from longform.factory import _repair_customer_text  # noqa: E402
from recovery_orchestrator import LEVEL_RECOVERABLE, classify_reports, recover_governance  # noqa: E402


def _ku() -> dict:
    return {
        "ku_id": "KU-9018-9C2B14BD",
        "core_knowledge": "未启用会议室先通风15—20分钟，杯具及毛巾消毒不少于20分钟，茶叶会前30分钟备妥，服务人员会前10分钟到门口迎候。",
        "applicability": "适用于正式会议和重要接待的会前检查清单。",
        "non_applicable_conditions": "温度、消毒和迎候时点应按甲方制度、卫生要求及会议等级校准。",
    }


class GlmRealArtifactHygieneTests(unittest.TestCase):
    def test_fixture_word_is_frozen_copy(self) -> None:
        path = FIXTURE / "青岛海信国际中心物业顾问服务项目_完整物业服务方案_20260910_011206.docx"
        self.assertTrue(path.is_file())
        text = "\n".join(p.text for p in Document(str(path)).paragraphs)
        self.assertIn("可根据项目确认要求确定响应安排", text)

    def test_pollution_is_postprocess_not_provider_raw(self) -> None:
        raw = json.loads((FIXTURE / "S07-01_provider_raw.json").read_text(encoding="utf-8"))
        fragment = json.loads((FIXTURE / "S07-01_fragment.json").read_text(encoding="utf-8"))
        raw_text = " ".join(block.get("content", "") for block in raw.get("body_blocks") or [])
        frag_text = " ".join(block.get("content", "") for block in fragment.get("body_blocks") or [])
        self.assertIn("通风15至20分钟", raw_text)
        self.assertIn("30分钟", raw_text)
        self.assertFalse(has_conditional_postprocess_pollution(raw_text))
        self.assertTrue(has_conditional_postprocess_pollution(frag_text) or "可根据项目确认要求确定响应安排" in frag_text)

    def test_factory_rewrite_keeps_grammar(self) -> None:
        raw = (FIXTURE / "provider_raw_sentence.txt").read_text(encoding="utf-8").strip()
        repaired = _repair_customer_text(raw)
        self.assertNotIn("可根据项目确认要求确定响应安排", repaired)
        self.assertNotIn("通风15至", repaired)
        self.assertNotIn("15至可根据", repaired)
        self.assertIn("按项目确认", repaired)
        self.assertNotRegex(repaired, r"通风按项目确认的安排标准")

    def test_polluted_sentence_is_repaired_to_readable_chinese(self) -> None:
        polluted = (FIXTURE / "polluted_sentence.txt").read_text(encoding="utf-8").strip()
        repaired = rewrite_unconfirmed_durations(polluted)
        self.assertFalse(has_conditional_postprocess_pollution(repaired), repaired)
        self.assertNotIn("可根据项目确认要求确定响应安排", repaired)
        self.assertIn("通风", repaired)
        self.assertIn("到岗迎候", repaired)

    def test_cg001_is_recoverable_not_terminal(self) -> None:
        generated = {"artifact": {"paragraphs": [(FIXTURE / "provider_raw_sentence.txt").read_text(encoding="utf-8").strip()]}}
        result = recover_governance(
            generated,
            brief={"requirements": "会议服务"},
            positives=[_ku()],
            guardrails=[],
            contracts=[],
        )
        self.assertFalse(result["terminal"], result)
        self.assertNotEqual(result["compliance"].get("status"), "BLOCK")
        self.assertGreaterEqual(result["repair_count"], 1)
        self.assertNotIn("30分钟", json.dumps(result["generated"], ensure_ascii=False))

    def test_artifact_qa_marks_pollution_as_auto_repair(self) -> None:
        polluted = (FIXTURE / "polluted_sentence.txt").read_text(encoding="utf-8").strip()
        report = evaluate_artifact({"artifact": {"paragraphs": [polluted]}})
        self.assertTrue(any(row.get("rule_id") == "CONDITIONAL_POSTPROCESS_POLLUTION" for row in report["findings"]))
        cleaned = apply_artifact_repairs({"artifact": {"paragraphs": [polluted]}})
        follow = evaluate_artifact(cleaned)
        self.assertFalse(any(row.get("rule_id") == "CONDITIONAL_POSTPROCESS_POLLUTION" for row in follow["findings"]))

    def test_missing_section_stays_recoverable_until_exhausted(self) -> None:
        level = classify_reports(
            {"status": "PASS", "violations": []},
            {"status": "PASS", "violations": []},
            {"status": "BLOCK", "findings": [{"rule_id": "LF-002", "severity": "BLOCK"}]},
            {},
        )
        self.assertEqual(level, LEVEL_RECOVERABLE)


class RecoveryMatrixTests(unittest.TestCase):
    def test_recovery_01_unsupported_commitment_is_rewritten(self) -> None:
        generated = {"artifact": {"paragraphs": ["所有人员资历证明已整理成册。"]}}
        result = recover_governance(generated, brief={"requirements": "物业服务"}, positives=[], guardrails=[], contracts=[])
        self.assertFalse(result["terminal"])
        text = json.dumps(result["generated"], ensure_ascii=False)
        self.assertIn("应按招标要求另行提供", text)

    def test_recovery_02_unsupported_number_is_rewritten(self) -> None:
        generated = {"artifact": {"paragraphs": ["茶叶会前30分钟备妥。"]}}
        result = recover_governance(generated, brief={"requirements": "会议服务"}, positives=[_ku()], guardrails=[], contracts=[])
        self.assertNotEqual(result["compliance"].get("status"), "BLOCK")
        self.assertNotIn("30分钟", json.dumps(result["generated"], ensure_ascii=False))

    def test_recovery_03_broken_cross_reference(self) -> None:
        from governance.customer_hygiene import rewrite_customer_hygiene
        text = "上述事项将直接影响相关章节、相关章节及相关章节等章节的最终落地。"
        repaired = rewrite_customer_hygiene(text)
        self.assertNotIn("相关章节、相关章节", repaired)
        self.assertIn("后续专项安排", repaired)

    def test_recovery_04_conditional_rewrite_grammar(self) -> None:
        polluted = (FIXTURE / "polluted_sentence.txt").read_text(encoding="utf-8").strip()
        from governance.customer_hygiene import hygiene_findings, rewrite_customer_hygiene
        self.assertTrue(any(row["rule_id"] == "CONDITIONAL_REWRITE_GRAMMAR" for row in hygiene_findings(polluted)))
        repaired = rewrite_customer_hygiene(polluted)
        self.assertFalse(any(row["rule_id"] == "CONDITIONAL_REWRITE_GRAMMAR" for row in hygiene_findings(repaired)), repaired)

    def test_recovery_05_two_repairs_still_fail_is_terminal(self) -> None:
        generated = {"artifact": {"paragraphs": ["项目总建筑面积为252567平方米。", "项目总面积约15.27万平方米。"]}}
        facts = {"project_facts": {"gross_area": {"value": "252567平方米"}}}
        result = recover_governance(generated, brief={"requirements": "物业"}, positives=[], guardrails=[], contracts=[], canonical_facts=facts)
        self.assertTrue(result["terminal"] or result["artifact_qa"].get("status") == "BLOCK")

    def test_recovery_06_local_repair_does_not_call_provider(self) -> None:
        generated = {"artifact": {"paragraphs": ["茶叶会前30分钟备妥。"]}}
        result = recover_governance(generated, brief={"requirements": "会议服务"}, positives=[_ku()], guardrails=[], contracts=[])
        self.assertGreaterEqual(result["repair_count"], 1)
        self.assertNotIn("provider", json.dumps(result.get("audit"), ensure_ascii=False))

    def test_rfid_access_is_conditionalized_not_terminal(self) -> None:
        from governance.customer_hygiene import hygiene_findings, rewrite_customer_hygiene
        text = "常驻人员与车辆：采用生物识别或RFID技术实现无感通行，定期更新授权名单，离职或退租后立即注销权限。"
        self.assertIn("UNSUPPORTED_SYSTEM_CAPABILITY", {row["rule_id"] for row in hygiene_findings(text)})
        repaired = rewrite_customer_hygiene(text)
        self.assertNotIn("无感通行", repaired)
        self.assertIn("如项目已配置", repaired)
        self.assertFalse(any(row["rule_id"] == "UNSUPPORTED_SYSTEM_CAPABILITY" for row in hygiene_findings(repaired)), repaired)

    def test_configured_access_is_not_unsupported_completion(self) -> None:
        from governance.commitment_provenance import evaluate_commitments
        text = "常驻人员与车辆：如项目已配置相应通行系统，可采用生物识别或RFID等方式核验通行。"
        report = evaluate_commitments({}, [], {"artifact": {"paragraphs": [text]}})
        self.assertFalse(
            any(row["claim_type"] == "UNSUPPORTED_COMPLETION_CLAIM" for row in report["violations"]),
            report,
        )

    def test_real_attachment_claim_still_detected(self) -> None:
        from governance.commitment_provenance import evaluate_commitments
        text = "所有拟派人员履历及类似项目经验已整理成册，作为本方案附件提交。"
        report = evaluate_commitments({}, [], {"artifact": {"paragraphs": [text]}})
        self.assertTrue(any(row["claim_type"] == "UNSUPPORTED_COMPLETION_CLAIM" for row in report["violations"]))

    def test_commitment_leftover_does_not_withhold_delivery(self) -> None:
        from recovery_orchestrator import accept_leftover_findings, should_quarantine_artifact, withhold_delivery
        self.assertFalse(withhold_delivery(
            {"status": "PASS", "violations": []},
            {"status": "BLOCK", "violations": [{
                "claim_type": "UNSUPPORTED_COMPLETION_CLAIM",
                "severity": "BLOCK",
                "text": "常驻人员与车辆：如项目已配置",
            }]},
            {"status": "PASS", "findings": []},
            {},
        ))
        leftover = {"status": "BLOCK", "findings": [{"rule_id": "UNSUPPORTED_COMPLETION_CLAIM", "severity": "BLOCK"}]}
        self.assertFalse(should_quarantine_artifact(leftover))
        accepted = accept_leftover_findings(leftover)
        self.assertEqual(accepted["status"], "WARNING")
        self.assertTrue(accepted["leftover_accepted"])

    def test_missing_section_and_project_fact_still_withhold(self) -> None:
        from recovery_orchestrator import should_quarantine_artifact, withhold_delivery
        self.assertTrue(withhold_delivery(
            {"status": "PASS", "violations": []},
            {"status": "PASS", "violations": []},
            {"status": "BLOCK", "findings": [{"rule_id": "LF-002", "severity": "BLOCK"}], "longform_depth": {"metrics": {"planned_sections": 30, "generated_sections": 5, "missing_sections": [f"S{i:02d}-01" for i in range(1, 26)]}}},
            {},
        ))
        self.assertFalse(withhold_delivery(
            {"status": "PASS", "violations": []},
            {"status": "PASS", "violations": []},
            {"status": "BLOCK", "findings": [{"rule_id": "PROJECT_FACT_CONSISTENCY", "severity": "BLOCK"}]},
            {},
        ))
        self.assertFalse(should_quarantine_artifact({"findings": [{"rule_id": "PROJECT_FACT_CONSISTENCY", "severity": "BLOCK"}]}))
        self.assertTrue(should_quarantine_artifact({"findings": [{"rule_id": "FINAL_ARTIFACT_MISSING", "severity": "BLOCK"}]}))

    def test_glm_01_to_06_detectors(self) -> None:
        from governance.customer_hygiene import hygiene_findings
        cases = [
            ("杯具及毛巾消毒不少于可根据项目确认要求确定响应安排", "CONDITIONAL_REWRITE_GRAMMAR"),
            ("相关章节、相关章节及相关章节等章节", "BROKEN_CROSS_REFERENCE"),
            ("数据记录— — — — — —趋势分析", "BROKEN_FLOW_SERIALIZATION"),
            ("通过人脸识别实现无感通行", "UNSUPPORTED_SYSTEM_CAPABILITY"),
            ("客户重复投诉超过三次", "UNSUPPORTED_NUMERIC_THRESHOLD"),
            ("值守责任位编制", "INTERNAL_GOVERNANCE_TONE"),
        ]
        for text, rule in cases:
            rules = {row["rule_id"] for row in hygiene_findings(text)}
            self.assertIn(rule, rules, f"{rule} missed for {text}")

    def test_mechanical_project_confirm_is_rewritten_in_context(self) -> None:
        from governance.customer_hygiene import rewrite_customer_hygiene
        monthly = rewrite_customer_hygiene("按项目确认的周期复盘主要聚焦于当月发生的典型问题、未关闭的遗留事项及关键指标波动情况")
        self.assertIn("双方确认的周期开展复盘", monthly)
        self.assertNotIn("按项目确认的周期复盘主要聚焦于当月", monthly)
        plan = rewrite_customer_hygiene("阶段成果包括《按项目确认的周期设施设备维护保养计划》")
        self.assertIn("设施设备周期维护保养计划", plan)
        self.assertNotIn("按项目确认的周期设施设备", plan)

    def test_fast_mode_does_not_shrink_full_proposal_budget(self) -> None:
        from planning.planner import build_content_budget
        from tests.test_longform_orchestrator import _brief, _pack, _selection
        from planning.planner import build_planning_bundle
        pack, brief, selection = _pack(), _brief(), _selection()
        left = build_planning_bundle(pack, brief, selection, production=True)
        right = build_content_budget(left["word_plan"]["outline"], left["requirement_matrix"], speed_profile="fast")
        self.assertEqual(left["content_budget"]["totals"]["target_words_min"], right["totals"]["target_words_min"])
        from longform.orchestrator import resolve_task_mode
        from planning.canonical import resolve_task_mode as resolve_mode
        self.assertEqual(resolve_mode({"scenario": "完整物业服务方案"}), "full_longform")

    def test_real_artifact_replay_old_docx_fails_then_repaired_copy_passes(self) -> None:
        import shutil
        import tempfile
        from governance.final_artifact_qa import evaluate_docx_artifact, repair_docx_artifact
        source = FIXTURE / "青岛海信国际中心物业顾问服务项目_完整物业服务方案_20260910_011206.docx"
        before = evaluate_docx_artifact(source)
        rules = {row.get("rule_id") for row in before.get("findings") or []}
        excerpt = (FIXTURE / "broken_flow_excerpt.txt").read_text(encoding="utf-8")
        from governance.customer_hygiene import hygiene_findings
        excerpt_rules = {row["rule_id"] for row in hygiene_findings(excerpt)}
        combined = rules | excerpt_rules
        for needed in (
            "CONDITIONAL_REWRITE_GRAMMAR",
            "BROKEN_CROSS_REFERENCE",
            "BROKEN_FLOW_SERIALIZATION",
            "UNSUPPORTED_SYSTEM_CAPABILITY",
            "UNSUPPORTED_NUMERIC_THRESHOLD",
            "INTERNAL_GOVERNANCE_TONE",
        ):
            self.assertIn(needed, combined, f"missing {needed} in {sorted(combined)}")
        self.assertEqual(before["status"], "BLOCK")
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / source.name
            shutil.copy2(source, copy)
            repair_docx_artifact(copy)
            after = evaluate_docx_artifact(copy)
            remaining = {
                row.get("rule_id") for row in after.get("findings") or []
                if row.get("severity") == "BLOCK" and row.get("rule_id") in {
                    "CONDITIONAL_REWRITE_GRAMMAR", "BROKEN_CROSS_REFERENCE", "BROKEN_FLOW_SERIALIZATION",
                    "UNSUPPORTED_SYSTEM_CAPABILITY", "UNSUPPORTED_NUMERIC_THRESHOLD", "INTERNAL_GOVERNANCE_TONE",
                }
            }
            self.assertFalse(remaining, remaining)
            text = "\n".join(p.text for p in Document(str(copy)).paragraphs)
            self.assertNotIn("可根据项目确认要求确定响应安排", text)
            self.assertNotIn("相关章节、相关章节", text)


if __name__ == "__main__":
    unittest.main()

