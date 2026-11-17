"""MMF-007A T4.1 附属：Requirement Pack 构建的自愈降级测试。

场景：本地 _classification 因"项目名称出现在文本中"给出 PROJECT_SPECIFIC，
但该文本不含强上下文标记、也无事实值佐证——validate_requirement_pack 若按原
路径会在构建期抛 TenderError 使 Run 确定性卡死。修复后：降级为
GENERIC_REQUIREMENT 并生成 PROJECT_SPECIFIC_DOWNGRADED 澄清项。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from tender_intake.pack_builder import build_requirement_pack


def _extraction() -> dict:
    return {
        "run_id": "run_test_selfheal",
        "processing_mode": "COMPLETE",
        "files": [{"file_id": "f1", "original_filename": "tender.pdf", "media_type": "pdf", "sha256": "abc", "page_count": 9}],
        "paragraphs": [{"text": "项目名称：星河湾综合服务园区物业管理服务"}],
        "tables": [],
    }


def _item(text: str, requirement_type: str = "service_requirement", classification: str = "PROJECT_SPECIFIC") -> dict:
    return {
        "requirement_type": requirement_type,
        "mandatory_level_guess": "INFO",
        "normalized_requirement": text,
        "source_excerpt": text,
        "source_locator": {"file_id": "f1", "source_page": 1, "paragraph_index": 1, "source_excerpt": text},
        "confidence": 0.8,
        "classification": classification,
    }


class PackSelfHealTest(unittest.TestCase):
    def test_unverifiable_project_specific_is_downgraded_with_clarification(self) -> None:
        # 复现真实 Run（tender_20260912_004339_9c0d15）的卡死组合：条目经"项目名匹配"
        # 获得 PROJECT_SPECIFIC，但其文本不含强标记、也无事实值佐证（事实丢失/口径差）。
        # 用 monkeypatch 将事实表清空以构造该状态；真实生产数据中该分支已降级 3 条。
        from tender_intake import pack_builder

        items = [
            _item("项目名称：星河湾综合服务园区物业管理服务"),
            _item("星河湾综合服务园区物业管理服务的绿化养护覆盖全园区公共区域并按季节调整频次"),
        ]
        original = pack_builder._canonical_facts
        pack_builder._canonical_facts = lambda rows: {}
        try:
            pack = pack_builder.build_requirement_pack(_extraction(), items)
        finally:
            pack_builder._canonical_facts = original
        self.assertEqual(pack["status"], "awaiting_todd_confirmation")
        downgraded = [row for row in pack["requirements"] if row["classification"] == "GENERIC_REQUIREMENT"]
        self.assertTrue(downgraded, "应存在被降级的条目")
        reasons = [item["reason_code"] for item in pack["clarification_items"]]
        self.assertIn("PROJECT_SPECIFIC_DOWNGRADED", reasons)
        clarified = [item for item in pack["clarification_items"] if item["reason_code"] == "PROJECT_SPECIFIC_DOWNGRADED"][0]
        self.assertFalse(clarified["blocks_plan"])
        self.assertTrue(clarified["related_requirement_ids"])

    def test_strong_context_keeps_project_specific(self) -> None:
        pack = build_requirement_pack(
            _extraction(),
            [_item("本项目服务范围包含园区全部公共区域的秩序维护与巡逻管理")],
        )
        row = pack["requirements"][0]
        self.assertEqual(row["classification"], "PROJECT_SPECIFIC")
        reasons = [item["reason_code"] for item in pack["clarification_items"]]
        self.assertNotIn("PROJECT_SPECIFIC_DOWNGRADED", reasons)


if __name__ == "__main__":
    unittest.main()
