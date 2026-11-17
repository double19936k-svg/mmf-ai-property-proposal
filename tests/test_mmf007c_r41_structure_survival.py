"""Round 4.1 P0-1 的回归：装配出来的 CU 结构必须活着走到产物。

这一份钉的缺陷（R41 实测，零 Provider）：
`governance.artifact_qa.apply_artifact_repairs` 过去让**整棵 fragment 树共用一个
"已见字符串"集合**去重（`len ≥ 18` 的字符串只要见过就删）。而 `apply_content_units`
会把同一批 CU payload 同时写进两处 —— 渲染槽位（`tables`/`processes`）和审计镜像
（`content_unit_fragments`）。镜像在键序里排在前面，于是**后走到的渲染槽位被判成重复删空**：
提交站实测每节 4/5 张表 0 行、3/4 个流程 0 步（证据 `r41_structure_chain_replay.py`），
文档级整篇再过一遍时还会跨节互删。这不是"内容不够"，是内容到了又被静默丢掉 ——
和 Round 4 收掉的那七条同一类。

去重域收窄到"每个列表各自"之后，仍然保留原意：同一个列表里的相邻同句照删。
所以这一份两头都钉：
  1. 结构 payload 穿过三条本地清洗链后行数/步骤数不减，出处键不被抹；
  2. 同一列表内的重复长句仍然被去掉（不许把保护一起删了）；
  3. 跨节同句不再互删，重复由 QA 规则判（`LF-ANTI-PADDING`），不靠删。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from governance.artifact_qa import apply_artifact_repairs
from governance.commitment_provenance import apply_local_repairs
from longform.cu_assembly import apply_content_units
from longform.factory import _repair_customer_text
from providers.mock import MockProvider

# 一条"足够长所以会被去重规则盯上"的句子长度门槛（artifact_qa 用的是 ≥18 个非空白字符）
LONG_SENTENCE = "门岗进出登记与访客核验必须当班完成复核并由品质主管在巡查记录单上签认闭环"


def _cus(count: int = 8) -> list[dict]:
    kinds = ("NARRATIVE", "STANDARD_TABLE", "SOP", "CHECKLIST")
    rows = []
    for index in range(count):
        archetype = kinds[index % len(kinds)]
        budget = {"effective_chars": 900}
        if archetype == "STANDARD_TABLE":
            budget["rows"] = 5
        elif archetype == "SOP":
            budget["steps"] = 5
        elif archetype == "CHECKLIST":
            budget["items"] = 5
        rows.append({"content_unit_id": f"CU-S01-01-{index + 1:02d}",
                     "parent_section_id": "S01-01", "archetype": archetype,
                     "intent": f"门区通行组织要点{index + 1}",
                     "title": f"门区通行组织要点{index + 1}",
                     "must_cover": [f"通行核验口径{index + 1}", "记录与检查"],
                     "target_budget": budget})
    return rows


def _provider_fragment(contracts: list[dict]) -> tuple[dict, list[dict]]:
    """走真实入口拿 base：`_assemble_section_from_cus` 用的就是 Provider 交回的那份片段。

    base 的**键序**决定这场碰撞谁先被走到 —— Provider 片段里 `content_unit_fragments`
    排在 `tables`/`processes` 之前，所以旧代码删掉的是后面的渲染槽位。测试若自己造一份
    空 base，键序就反过来，删的是镜像侧，缺陷就被放过了。
    """
    provider = MockProvider({"provider_name": "mock", "provider_type": "mock",
                             "enabled": True, "model": "mock-r41"})
    context = {"section_contract": {"section_id": "S01-01", "section_title": "门区通行组织",
                                    "must_cover": ["门区通行管理原则", "访客核验接口"],
                                    "required_outputs": ["通行组织记录"],
                                    "source_requirements": ["REQ-0001"]},
               "relevant_process_contracts": []}
    fragment = provider._section_fragment(
        {"prompt": "Context Pack：\n" + json.dumps(context, ensure_ascii=False),
         "content_unit_contracts": contracts})
    rows = []
    for row in fragment.get("content_unit_fragments") or []:
        row = dict(row)
        row.setdefault("budget", {"effective_chars": 900})
        row.setdefault("generation_origin", "provider_content_unit")
        rows.append(row)
    return fragment, rows


def _shape(doc: dict) -> tuple[list[int], list[int]]:
    rows = [len(table.get("rows") or []) for table in doc.get("tables") or []
            if isinstance(table, dict)]
    steps = [len(process.get("steps") or []) for process in doc.get("processes") or []
             if isinstance(process, dict)]
    return rows, steps


class CuStructuresSurviveTheCommitChain(unittest.TestCase):
    def test_assembled_tables_and_processes_survive_local_repair_chain(self):
        base, rows = _provider_fragment(_cus())
        assembled = apply_content_units(rows, base_fragment=base, section_id="S01-01",
                                        planned_ids=[row["content_unit_id"] for row in rows],
                                        missing_ids=[])
        rows_before, steps_before = _shape(assembled)
        self.assertTrue(rows_before, "夹具没交出表格结构，这条回归就什么都没钉住")
        self.assertTrue(steps_before, "夹具没交出流程结构，这条回归就什么都没钉住")
        cleaned = _repair_customer_text(apply_artifact_repairs(apply_local_repairs(assembled)))
        rows_after, steps_after = _shape(cleaned)
        self.assertEqual(rows_after, rows_before,
                         f"表格行数在提交站被改动 {rows_before} → {rows_after}："
                         "整棵树共享去重集会删掉渲染槽位里的单元格")
        self.assertEqual(steps_after, steps_before,
                         f"流程步骤数在提交站被改动 {steps_before} → {steps_after}")
        # 出处键是削减执行器认"哪几张表是 CU 加的"的唯一依据，抹掉就会重复加表
        for table in cleaned.get("tables") or []:
            if table.get("source_content_unit_id"):
                self.assertTrue(table.get("rows"), "带出处的表被清空了行")

    def test_duplicate_sentences_inside_one_list_are_still_removed(self):
        """收窄去重域不等于取消去重：同一个列表里重复出现的长句仍然要被删。"""
        unique = "另一句正常正文内容，独立成句不与上面重复。"
        generated = {"artifact": {"sections": [{"paragraphs": [LONG_SENTENCE,
                                                              unique,
                                                              LONG_SENTENCE]}]}}
        once = apply_artifact_repairs(generated)
        paragraphs = once["artifact"]["sections"][0]["paragraphs"]
        self.assertEqual(paragraphs.count(LONG_SENTENCE), 1,
                         f"同列表内的重复长句没被去掉 —— 反 padding 能力被一起删了：{paragraphs}")
        self.assertIn(unique, paragraphs)
        self.assertEqual(apply_artifact_repairs(once), once, "清洗必须幂等")

    def test_same_sentence_in_two_sections_is_not_deleted_across_sections(self):
        """跨节同句不再互删：那是整篇级共享集合造成的静默丢失，重复由 QA 判。"""
        generated = {"artifact": {"sections": [
            {"paragraphs": [LONG_SENTENCE]},
            {"paragraphs": [LONG_SENTENCE]},
        ]}}
        repaired = apply_artifact_repairs(generated)
        kept = [row["paragraphs"] for row in repaired["artifact"]["sections"]]
        self.assertTrue(all(kept), f"整篇清洗把后一节的正文删空了：{kept}")

    def test_audit_mirror_does_not_empty_the_render_slot(self):
        """镜像与渲染槽位并存时，被删的必须是镜像侧而不是渲染侧。"""
        base, rows = _provider_fragment(_cus(count=4))
        assembled = apply_content_units(rows, base_fragment=base, section_id="S01-01",
                                        planned_ids=[row["content_unit_id"] for row in rows],
                                        missing_ids=[])
        mirror_structured = [row for row in assembled.get("content_unit_fragments") or []
                             if isinstance((row.get("payload") or {}).get("rows"), list)
                             or isinstance((row.get("payload") or {}).get("steps"), list)]
        self.assertTrue(mirror_structured, "夹具没产生审计镜像，这条钉不住")
        cleaned = apply_artifact_repairs(assembled)
        rendered_rows = sum(len(table.get("rows") or []) for table in cleaned.get("tables") or []
                            if isinstance(table, dict))
        rendered_steps = sum(len(process.get("steps") or [])
                             for process in cleaned.get("processes") or [] if isinstance(process, dict))
        self.assertGreater(rendered_rows, 0, "渲染槽位的表格全被删空")
        self.assertGreater(rendered_steps, 0, "渲染槽位的流程全被删空")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
