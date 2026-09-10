from __future__ import annotations

import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from compliance import apply_numeric_commitment_repairs, evaluate_compliance
from longform.factory import _repair_customer_text


def _run(text: str) -> dict:
    generated = {"artifact": {"title": "测试", "sections": [{"heading": "正文", "paragraphs": [text]}]}}
    return evaluate_compliance({"confirmed_staffing": "", "requirements": "物业服务"}, [], [], generated)


def _cg003_blocks(report: dict) -> list[str]:
    return [row.get("evidence", "") for row in report.get("violations", []) if row.get("rule_id") == "CG-003" and row.get("severity") == "BLOCK"]


class CG003PrecisionTest(unittest.TestCase):
    def test_false_positives_pass(self):
        samples = [
            "S03-01 项目负责人组织检查",
            "S05-02 客户服务人员进行回访",
            "责任人负责记录",
            "工程专业负责人复核",
            "项目人员按流程执行",
            "培训覆盖20人次",
            "巡检投入10人次",
            {"title": "S03-01", "section_id": "S03-01", "paragraphs": ["项目负责人组织检查"]},
        ]
        for sample in samples:
            if isinstance(sample, dict):
                report = evaluate_compliance({"confirmed_staffing": ""}, [], [], {"artifact": {"sections": [sample]}})
            else:
                report = _run(sample)
            self.assertFalse(_cg003_blocks(report), f"FP blocked: {sample!r} -> {_cg003_blocks(report)}")

    def test_true_positives_block(self):
        samples = [
            "项目固定配置12人",
            "客户服务岗位配置4人",
            "工程部不少于6人",
            "项目团队共15人",
            "固定编制20人",
            "客服人员4名",
            "秩序人员不少于10人",
        ]
        for sample in samples:
            report = _run(sample)
            self.assertTrue(_cg003_blocks(report), f"TP missed: {sample!r}")
            self.assertEqual(report["status"], "BLOCK")


class CG001HistoricNumberRepairTests(unittest.TestCase):
    def _ku(self) -> dict:
        return {
            "ku_id": "KU-9018-9C2B14BD",
            "core_knowledge": "未启用会议室先通风15—20分钟，杯具及毛巾消毒不少于20分钟，茶叶会前30分钟备妥，服务人员会前10分钟到门口迎候。",
            "applicability": "适用于正式会议和重要接待的会前检查清单。",
            "non_applicable_conditions": "温度、消毒和迎候时点应按甲方制度、卫生要求及会议等级校准。",
        }

    def test_unconfirmed_30_minutes_is_rewritten_locally(self) -> None:
        text = "茶叶等物料会前30分钟备妥，服务人员会前可根据项目确认要求确定响应安排到岗迎候。"
        generated = {"artifact": {"paragraphs": [text]}}
        report = evaluate_compliance({"requirements": "会议服务"}, [self._ku()], [], generated)
        self.assertEqual(report["status"], "BLOCK")
        self.assertTrue(any(row.get("evidence", "").replace(" ", "") == "30分钟" for row in report["violations"]))
        repaired = apply_numeric_commitment_repairs(generated, report)
        follow = evaluate_compliance({"requirements": "会议服务"}, [self._ku()], [], repaired)
        self.assertNotEqual(follow["status"], "BLOCK", follow)
        self.assertNotIn("30分钟", str(repaired))

    def test_confirmed_30_minutes_is_kept(self) -> None:
        text = "按招标文件要求，会前30分钟备妥茶叶。"
        report = evaluate_compliance({"requirements": "会前30分钟备妥茶叶"}, [self._ku()], [], {"artifact": {"paragraphs": [text]}})
        self.assertFalse(any(row.get("rule_id") == "CG-001" and row.get("severity") == "BLOCK" for row in report.get("violations") or []))

    def test_minute_range_is_rewritten_as_a_unit(self) -> None:
        repaired = _repair_customer_text("未启用会议室先通风15至20分钟。")
        self.assertNotIn("15至", repaired)
        self.assertNotIn("20分钟", repaired)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
