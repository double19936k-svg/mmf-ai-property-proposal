"""MMF-007 PPT 链修复的单元验证。

背景（真实 Run tender_20260912_021837_4452d2 归因）：
- medium=PPT 时最终 QA 曾套用 Word 长文深度规则（LF-002/003/005），40 页幻灯片
  内容被误判 BLOCK，Fail-Soft 拒绝交付 → TECHNICAL_FAILED。
- 承诺门槛拦截"确保设施设备运行零故障"（正确），但本地修复规则只认"零故障运行"
  全后缀，修复不生效（门严修宽）。
修复：PPT 用专属 QA（evaluate_ppt_artifact）；修复规则与门槛口径对齐。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from governance.artifact_qa import evaluate_ppt_artifact
from governance.commitment_provenance import ABSOLUTE_PERFORMANCE, apply_local_repairs


def _slides() -> dict:
    return {"artifact": {"title": "测试项目", "subtitle": "物业服务方案汇报", "slides": [
        {"slide_id": "SL-001", "headline": "项目理解", "subheadline": "边界清晰", "content_blocks": [{"type": "info", "text": "项目位于产业园，含写字楼与商业裙楼。"}]},
        {"slide_id": "SL-002", "headline": "服务构想", "content_blocks": [{"type": "process", "text": "以巡场与驻场结合的方式组织顾问服务。"}]},
        {"slide_id": "SL-003", "headline": "服务保障", "content_blocks": [{"type": "cards", "text": "按确认频次组织环境作业并留痕。"}]},
    ]}}


class PptArtifactQaTest(unittest.TestCase):
    def test_valid_slides_pass(self) -> None:
        qa = evaluate_ppt_artifact(_slides(), expected_slides=3)
        self.assertEqual(qa["status"], "PASS", qa["findings"])
        self.assertEqual(qa["summary"]["slide_count"], 3)

    def test_missing_headline_blocks(self) -> None:
        payload = _slides()
        payload["artifact"]["slides"][1]["headline"] = ""
        qa = evaluate_ppt_artifact(payload)
        self.assertEqual(qa["status"], "BLOCK")
        self.assertTrue(any(f["rule_id"] == "PPT-003" for f in qa["findings"]))

    def test_no_slides_blocks(self) -> None:
        qa = evaluate_ppt_artifact({"artifact": {"title": "空"}})
        self.assertEqual(qa["status"], "BLOCK")
        self.assertTrue(any(f["rule_id"] == "PPT-001" for f in qa["findings"]))

    def test_slide_count_mismatch_is_warning(self) -> None:
        qa = evaluate_ppt_artifact(_slides(), expected_slides=40)
        self.assertNotEqual(qa["status"], "BLOCK")
        self.assertTrue(any(f["rule_id"] == "PPT-005" for f in qa["findings"]))


class CommitmentRepairAlignmentTest(unittest.TestCase):
    def test_zero_failure_without_suffix_gets_repaired(self) -> None:
        text = "确保设施设备运行零故障"
        repaired = apply_local_repairs({"artifact": {"slides": [{"core_message": text}]}})
        self.assertIn("保障设施设备运行稳定可靠", json_text(repaired))
        self.assertFalse(ABSOLUTE_PERFORMANCE.search(json_text(repaired)))

    def test_gate_variants_are_all_repaired(self) -> None:
        for phrase in ("承诺100%无故障运行", "绝不发生重大安全事件", "完全杜绝卫生死角", "永久确保设备完好"):
            repaired = json_text(apply_local_repairs({"artifact": {"slides": [{"core_message": phrase}]}}))
            self.assertFalse(ABSOLUTE_PERFORMANCE.search(repaired), phrase)


class PptSlideDataMappingTest(unittest.TestCase):
    """富映射：真实 payload 结构 → 多版式；评分内容被剥离。"""

    def _payload(self, blocks, visual=None, intent="", headline="标题", key_message="核心观点"):
        return {"headline": headline, "subheadline": "", "key_message": key_message,
                "content_blocks": blocks, "visual_data": visual or {}, "component_intent": intent}

    def test_architecture_maps_to_modules(self) -> None:
        from longform.final_assembly import build_ppt_slide_data
        payload = self._payload(
            [{"type": "pillar", "title": "组织架构", "description": "扁平化指挥体系，确保指令直达一线"},
             {"type": "pillar", "title": "专业服务", "description": "标准化作业流程，覆盖全业态场景需求"},
             {"type": "pillar", "title": "品质管控", "description": "三级质检机制，数据驱动持续优化"}],
            visual={"chart_type": "architecture_diagram", "structure": "hub_and_spoke", "center_node": "一体化运营系统"},
            intent="展示各服务模块通过统一管理中枢形成合力",
        )
        data = build_ppt_slide_data(payload, {"slide_role": "system"}, 1)
        self.assertEqual(data["layout"], "modules")
        self.assertEqual(len(data["modules"]), 3)
        self.assertEqual(data["center"], "一体化运营系统")
        self.assertIn("扁平化指挥体系", data["modules"][0]["body"])

    def test_process_maps_to_steps(self) -> None:
        from longform.final_assembly import build_ppt_slide_data
        payload = self._payload(
            [{"type": "step", "title": "建立全生命周期设备电子档案", "description": "一机一码，状态实时可追溯"}],
            visual={"chart_type": "process_flow", "elements": ["报修登记：工单录入系统", "故障判断：分级分类处置", "复验关闭：结果留痕可查"]},
            intent="展示故障处置全流程",
        )
        data = build_ppt_slide_data(payload, {"slide_role": "process"}, 2)
        self.assertEqual(data["layout"], "process")
        self.assertEqual(len(data["steps"]), 3)
        self.assertEqual(data["steps"][0]["title"], "报修登记")

    def test_comparison_maps_to_table(self) -> None:
        from longform.final_assembly import build_ppt_slide_data
        payload = self._payload(
            [{"type": "row", "title": "自营模式", "description": "总部直管，品质可控"},
             {"type": "row", "title": "外委模式", "description": "成本弹性，需强化考核"}],
            intent="对比两种供方模式",
        )
        data = build_ppt_slide_data(payload, {"slide_role": "comparison"}, 3)
        self.assertEqual(data["layout"], "table")
        self.assertEqual(data["table"]["rows"][0], ["自营模式", "总部直管，品质可控"])

    def test_scoring_content_is_stripped(self) -> None:
        from longform.final_assembly import build_ppt_slide_data
        payload = self._payload(
            [{"type": "point", "title": "评分", "description": "评分标准：商务及报价40分，服务方案40分"}],
            intent="响应招标要求",
        )
        data = build_ppt_slide_data(payload, {"slide_role": "detail"}, 4)
        joined = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("评分标准", joined)
        self.assertNotIn("商务及报价", joined)

    def test_ppt006_blocks_scoring_in_artifact_qa(self) -> None:
        payload = _slides()
        payload["artifact"]["slides"][0]["core_message"] = "按评分标准逐项响应，本项分值10分"
        qa = evaluate_ppt_artifact(payload)
        self.assertEqual(qa["status"], "BLOCK")
        self.assertTrue(any(f["rule_id"] == "PPT-006" for f in qa["findings"]))


def json_text(value: object) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
