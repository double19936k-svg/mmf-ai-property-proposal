"""MMF-007 Phase 3：Compression Pipeline 行为测试（SYNTHETIC 口径）。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from compression.planner import (
    build_compression_plan,
    build_compression_trace,
    render_condensed_artifact,
    validate_no_core_loss,
)


def _fixture():
    hierarchy = {"sections": [
        {"section_id": "S01-01", "volume_id": "V01"},
        {"section_id": "S04-02", "volume_id": "V03"},
        {"section_id": "S08-01", "volume_id": "V06"},
    ]}
    scoring = {"rows": [
        {"scoring_item_id": "SCR-001", "primary_section_id": "S04-02", "response_status": "PLANNED", "max_score": 20, "must_respond": True},
        {"scoring_item_id": "SCR-002", "primary_section_id": "S08-01", "response_status": "DERIVED_PLANNED", "max_score": 5, "must_respond": False},
    ]}
    requirement = {"matrix": [
        {"requirement_id": "REQ-0001", "mandatory_level": "MUST", "primary_section_id": "S04-02"},
        {"requirement_id": "REQ-0002", "mandatory_level": "INFO", "primary_section_id": "S08-01"},
    ]}
    artifact = {"title": "长文", "sections": [
        {"section_id": "S01-01", "title": "项目概况", "body_blocks": [{"type": "paragraph", "content": "背景描述" * 60}], "scoring_items": []},
        {"section_id": "S04-02", "title": "投诉闭环", "body_blocks": [{"type": "paragraph", "content": "投诉处理与回访闭环机制描述" * 40}], "scoring_items": ["SCR-001"], "used_requirement_ids": ["REQ-0001"]},
        {"section_id": "S08-01", "title": "品质巡检", "body_blocks": [{"type": "paragraph", "content": "品质巡检分级整改" * 30}], "scoring_items": ["SCR-002"], "used_requirement_ids": ["REQ-0002"]},
    ]}
    return hierarchy, scoring, requirement, artifact


class CompressionPlanTest(unittest.TestCase):
    def test_must_and_scoring_sections_kept_full(self):
        hierarchy, scoring, requirement, artifact = _fixture()
        plan = build_compression_plan(artifact, hierarchy, scoring, requirement, target_pages=(1, 1))
        modes = {row["section_id"]: row["compression_mode"] for row in plan["plan_rows"]}
        self.assertEqual(modes["S04-02"], "KEEP_FULL", "MUST+高分评分项章节必须全保")
        self.assertIn(modes["S01-01"], {"KEEP_FULL", "SUMMARY_ONLY"})

    def test_compression_reduces_size_and_estimate(self):
        hierarchy, scoring, requirement, artifact = _fixture()
        plan = build_compression_plan(artifact, hierarchy, scoring, requirement, target_pages=(1, 1))
        self.assertLess(plan["summary"]["target_chars"], plan["summary"]["original_chars"])

    def test_trace_links_all_sources(self):
        hierarchy, scoring, requirement, artifact = _fixture()
        plan = build_compression_plan(artifact, hierarchy, scoring, requirement, target_pages=(1, 1))
        trace = build_compression_trace(plan, requirement)
        row = next(r for r in trace["rows"] if r["section_id"] == "S04-02")
        self.assertEqual(row["source_volume_ids"], ["V03"])
        self.assertIn("REQ-0001", row["requirement_ids"])
        self.assertIn("SCR-001", row["scoring_item_ids"])

    def test_no_core_loss_validation(self):
        hierarchy, scoring, requirement, artifact = _fixture()
        plan = build_compression_plan(artifact, hierarchy, scoring, requirement, target_pages=(1, 1))
        loss = validate_no_core_loss(plan, scoring, requirement)
        self.assertEqual(loss["CORE_SCORING_LOSS"], 0, loss)
        self.assertEqual(loss["CORE_MUST_LOSS"], 0, loss)
        self.assertEqual(loss["status"], "PASS")

    def test_summary_only_never_upgrades_conditional_language(self):
        hierarchy, scoring, requirement, artifact = _fixture()
        plan = build_compression_plan(artifact, hierarchy, scoring, requirement, target_pages=(1, 1))
        condensed = render_condensed_artifact(artifact, plan)
        for section in condensed["sections"]:
            text = json.dumps(section, ensure_ascii=False)
            self.assertNotIn("已具备", text.replace("已具备的", "已具备的"))
            self.assertNotIn("无条件", text)

    def test_m007c_r2_core_protection_reads_matrices_not_artifact_selfreport(self):
        """R2-P1-2：真实链路的 Section 不带 scoring_items/used_requirement_ids，
        旧规划器的"核心保护集"因此恒为空，KEEP_FULL 退化成按字数塞预算，评分点唯一
        应答章节会被无声降成摘要（验收器报 CORE_LOSS_DETECTED）。保护关系必须由矩阵
        推导——规划与验收同一把尺子，不能靠 Artifact 自报字段。"""
        hierarchy = {"sections": [
            {"section_id": f"S0{i}-01", "volume_id": f"V0{i}"} for i in range(1, 5)
        ]}
        scoring = {"rows": [{
            "scoring_item_id": "SCR-C12", "primary_section_id": "S04-01",
            "section_ids": ["S04-01"], "response_status": "PLANNED",
            "max_score": 5, "must_respond": True,
        }]}
        requirement = {"matrix": [{
            "requirement_id": "REQ-C12", "mandatory_level": "SHOULD",
            "primary_section_id": "S04-01",
        }]}
        # 唯一应答章节最短：按字数塞预算的旧逻辑第一个牺牲它。
        artifact = {"title": "长文", "sections": [
            {"section_id": "S01-01", "body_blocks": [{"content": "体量很大的非评分章节" * 200}]},
            {"section_id": "S02-01", "body_blocks": [{"content": "体量很大的非评分章节" * 160}]},
            {"section_id": "S03-01", "body_blocks": [{"content": "体量很大的非评分章节" * 120}]},
            {"section_id": "S04-01", "body_blocks": [{"content": "评分点唯一应答" * 20}]},
        ]}
        plan = build_compression_plan(artifact, hierarchy, scoring, requirement, target_pages=(1, 1))
        modes = {row["section_id"]: row["compression_mode"] for row in plan["plan_rows"]}
        self.assertEqual(modes["S04-01"], "KEEP_FULL", "评分点唯一应答章节不得因预算被降成摘要")
        self.assertEqual({r["section_id"]: r["scoring_item_ids"] for r in plan["plan_rows"]}["S04-01"],
                         ["SCR-C12"], "追溯链必须由矩阵补齐，不能只剩 Artifact 自报的空列表")
        loss = validate_no_core_loss(plan, scoring, requirement)
        self.assertEqual(loss["status"], "PASS", loss)
        # 核心章节装不进页数目标时如实报告超限，而不是偷偷丢核心。
        self.assertGreater(plan["summary"]["over_budget_chars"], 0)
        self.assertEqual(plan["summary"]["sections_core_protected"], 1)


if __name__ == "__main__":
    unittest.main()
