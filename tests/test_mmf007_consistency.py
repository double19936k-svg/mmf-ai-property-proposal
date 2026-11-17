"""Cross-Section Consistency Engine 行为测试。"""
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


def _frag(sid, text):
    return {sid: {"body_blocks": [{"type": "paragraph", "content": text}]}}


class ConsistencyTest(unittest.TestCase):
    def test_consistent_area_passes(self):
        gs = {"project_facts": {"gross_area": {"value": "25.2万平方米"}}}
        frags = {}
        frags.update(_frag("S01-01", "项目总建筑面积25.2万平方米，含写字楼与商业。"))
        frags.update(_frag("S06-02", "针对25.2万平方米的设施设备开展巡检维保。"))
        result = check_cross_section_consistency(frags, gs)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["summary"]["issue_count"], 0)

    def test_conflicting_headcount_needs_review(self):
        gs = {"project_facts": {"team_headcount": {"value": 45}}}
        frags = {}
        frags.update(_frag("S03-02", "本项目人员配置不少于45人，实行三班运转。"))
        frags.update(_frag("S03-03", "后段章节表述：本项目人员配置不少于60人，培训上岗。"))
        result = check_cross_section_consistency(frags, gs)
        self.assertGreaterEqual(result["summary"]["issue_count"], 1)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        issue = result["issues"][0]
        self.assertEqual(issue["fact_key"], "team_headcount")
        self.assertEqual(issue["severity"], "NEEDS_REVIEW")

    def test_registry_files_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            gs = {"project_facts": {"gross_area": {"value": "12万平方米"}}}
            frags = _frag("S01-01", "总建筑面积12万平方米。")
            check_cross_section_consistency(frags, gs, Path(tmp))
            self.assertTrue((Path(tmp) / "consistency_registry.json").is_file())
            self.assertTrue((Path(tmp) / "consistency_issues.json").is_file())
            registry = json.loads((Path(tmp) / "consistency_registry.json").read_text(encoding="utf-8"))
            self.assertIn("gross_area_sqm", registry["observed"], "alias gross_area 必须归一到 canonical key")


if __name__ == "__main__":
    unittest.main()
