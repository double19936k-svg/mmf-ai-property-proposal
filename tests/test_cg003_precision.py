from __future__ import annotations

import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from compliance import evaluate_compliance


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


if __name__ == "__main__":
    raise SystemExit(unittest.main())
