"""CONS-E2E-01/02/03 + Scoring E2E Fixture（Round 2 Blocker 6/7 验收）。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from governance.consistency import check_cross_section_consistency
from planning.hierarchy import update_scoring_fulfillment


class ConsistencyE2ETest(unittest.TestCase):
    def test_cons_e2e_01_area_conflict_found(self):
        """两个 CU 面积 25万 vs 15万，Canonical 25万 → 必须发现冲突。"""
        gs = {"project_facts": {"gross_area_sqm": {"value": "25万平方米"}}}
        frags = {
            "CU-A": {"body_blocks": [{"type": "paragraph", "content": "项目总建筑面积25万平方米。"}]},
            "CU-B": {"body_blocks": [{"type": "paragraph", "content": "园区建筑面积15万平方米，含配套。"}]},
        }
        result = check_cross_section_consistency(frags, gs)
        conflicts = [i for i in result["issues"] if i["fact_key"] == "gross_area_sqm"]
        self.assertGreaterEqual(len(conflicts), 1, "必须发现 15万 vs 25万 冲突")
        self.assertEqual(result["status"], "NEEDS_REVIEW")

    def test_cons_e2e_02_alias_no_false_positive(self):
        """area_sqm / gross_area 同义 key 归一后不得误报。"""
        gs = {"project_facts": {"area_sqm": {"value": "86000平方米"}, "gross_area": {"value": "86000平方米"}}}
        frags = {"S01-01": {"body_blocks": [{"type": "paragraph", "content": "建筑面积86000平方米。"}]}}
        result = check_cross_section_consistency(frags, gs)
        self.assertEqual(result["summary"]["issue_count"], 0, "同义 key 不得误报")

    def test_cons_e2e_03_department_conflict(self):
        """department name 冲突必须发现：canonical 客户服务部 vs 片段 客户体验部。"""
        gs = {"project_facts": {"department_names": {"value": "客服部、工程部、秩序部"}}}
        frags = {
            "S04-01": {"body_blocks": [{"type": "paragraph", "content": "投诉由客户服务部统一受理并闭环。"}]},
            "S04-02": {"body_blocks": [{"type": "paragraph", "content": "重大投诉升级至客户体验部处置并复盘。"}]},
        }
        result = check_cross_section_consistency(frags, gs)
        dept_issues = [i for i in result["issues"] if i["fact_key"] == "department_names"]
        self.assertGreaterEqual(len(dept_issues), 1, "必须发现 客户体验部 vs 客服部 部门名漂移")
        self.assertEqual(result["status"], "NEEDS_REVIEW")


class ScoringE2EFixtureTest(unittest.TestCase):
    """Scoring A/B/C：完整/部分/未响应 → FULFILLED / PARTIAL / MISSING。"""

    def _fulfill(self, frags):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "m.json"
            rows = [
                {"scoring_item_id": "SCR-A", "label": "人员组织架构与岗位职责表（20分）", "section_ids": ["S03-01"],
                 "planned_response_words": 300, "planned_evidence": {"required_outputs": ["岗位职责表"], "tables": ["岗位职责表"]}, "fulfillment": {}},
                {"scoring_item_id": "SCR-B", "label": "投诉闭环流程与记录表单（15分）", "section_ids": ["S04-02"],
                 "planned_response_words": 300, "planned_evidence": {"required_outputs": ["投诉闭环流程图", "记录表单"]}, "fulfillment": {}},
                {"scoring_item_id": "SCR-C", "label": "创新增值服务方案（10分）", "section_ids": ["S08-02"],
                 "planned_response_words": 300, "planned_evidence": {"required_outputs": ["增值服务清单"]}, "fulfillment": {}},
            ]
            p.write_text(json.dumps({"rows": rows, "coverage_summary": {}}, ensure_ascii=False), encoding="utf-8")
            result = update_scoring_fulfillment(p, frags)
            matrix = json.loads(p.read_text(encoding="utf-8"))
            return {row["scoring_item_id"]: row["fulfillment"]["status"] for row in matrix["rows"]}

    def test_full_partial_missing(self):
        import tempfile
        frags = {
            # Scoring A：实体响应（要素齐备 + 有表格载体），不是"附近有文字"
            "S03-01": {"body_blocks": [{"type": "paragraph", "content": (
                "本项目人员组织架构分三层：项目经理室负责总体履约与对外接口，专业主管负责保洁、秩序、"
                "工程、客服四条线的日常调度，作业班组负责按标准完成现场动作。三层之间以岗位职责表为唯一"
                "口径来源，岗位说明书与职责表逐条对应，避免出现无人负责的间隙岗位。"
                "人员组织架构同时规定替岗关系：每条线设一名副职主管，班长缺位时由指定骨干顶上，"
                "顶岗人员须在到岗当日完成职责表交底并记录在交接班台账。"
                "岗位职责表按岗位列明工作事项、执行标准、检查频次与考核口径，考核结果每月汇总一次，"
                "作为培训安排与人员调整的依据。"
                "三层架构每周以调度会核验，会议纪要记录到岗人数与缺岗补位情况，"
                "缺岗超过两个工作日的岗位启动顶岗并回写岗位职责表；人员组织架构的调整须经项目经理签发，"
                "调整后五个工作日内完成交底与岗前培训。")}],
                "tables": [{"title": "岗位职责表", 'columns': ['岗位', '职责', '到岗与交底'],
                            'rows': [['项目经理', '人员组织架构与总体履约', '到岗核验与岗前培训'],
                                     ['专业主管', '岗位职责与调度', '考核、记录与交底']]}]},
            # Scoring B：部分响应（提到流程，缺记录表单）
            "S04-02": {"body_blocks": [{"type": "paragraph", "content": "投诉闭环流程覆盖受理、处置、回访。"}], "tables": []},
            # Scoring C：完全未响应
            "S08-02": {"body_blocks": []},
        }
        statuses = self._fulfill(frags)
        self.assertEqual(statuses["SCR-A"], "FULFILLED", "要素齐备且有表格载体")
        self.assertEqual(statuses["SCR-B"], "PARTIAL", "提到流程但缺记录表单证据 → PARTIAL")
        self.assertEqual(statuses["SCR-C"], "MISSING", "零内容 → MISSING")


if __name__ == "__main__":
    unittest.main()
