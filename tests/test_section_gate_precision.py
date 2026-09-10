from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from governance.commitment_provenance import evaluate_commitments
from longform.factory import evaluate_word_fragment


def _fragment(paragraphs: list[str]) -> dict:
    return {
        "section_id": "S01-01",
        "title": "项目概况、服务范围与排除项",
        "body_blocks": [{"type": "paragraph", "content": text} for text in paragraphs],
        "tables": [],
        "processes": [],
        "callouts": [],
        "cross_references": [],
        "claims": [],
        "used_requirement_ids": [],
        "used_ku_ids": [],
        "generation_notes": "",
    }


class SectionGatePrecisionTest(unittest.TestCase):
    def test_conditional_closed_management_and_client_approval_pass(self):
        text = (
            "以下事项列为待澄清内容。关于封闭管理与开放管理的界限，目前尚未完全确认，"
            "安防策略需具备弹性。任何超出既定服务范围的临时性任务，需报甲方批准方可执行，严禁口头承诺。"
        )
        report = evaluate_commitments({"project_name": "测试项目"}, [], {"artifact": {"content": [text]}})
        self.assertEqual(report["status"], "PASS", report.get("violations"))

    def test_bid_form_must_cover_does_not_block_service_section(self):
        contract = {
            "must_cover": [
                "产业园生产经营连续性对物业服务的影响",
                "园区人车物流、设施运行和客户协同重难点",
                "重难点对应的管理原则与验证方式",
                "投标单位应认真阅读招标文件中所有的事项、格式、条款和技术规范等",
                "优惠承诺\n(如有)",
                "4 | 其他费用",
            ],
            "target_words": {"min": 80, "max": 400},
            "section_id": "S01-02",
        }
        fragment = _fragment([
            "示例滨海科技园作为综合体，物业顾问服务需围绕生产经营连续性与多业态协同展开。",
            "园区人车物流、设施运行和客户协同是实施重难点，需通过动线分流、低干扰养护和网格化管理验证。",
            "对应管理原则是预防优于补救，并以巡查台账和复盘会作为验证方式。",
        ])
        fragment["section_id"] = "S01-02"
        fragment["title"] = "产业园特征与物业管理重难点"
        gate = evaluate_word_fragment(fragment, contract, {"excluded_scope": [], "staffing": {}, "service_hours": {}, "sla_kpi": {}, "canonical_terms": {}}, [])
        self.assertNotEqual(gate["status"], "BLOCK", json.dumps(gate.get("must_cover"), ensure_ascii=False))
        self.assertEqual(gate["must_cover"]["投标单位应认真阅读招标文件中所有的事项、格式、条款和技术规范等"], "N/A")
        self.assertEqual(gate["must_cover"]["4 | 其他费用"], "N/A")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
