"""MMF008A3 终审返修的反例集：三个已确认 Run G 语义 blocker 的泛化证明。

姿态（与复核方同一把尺）：
- 已知集（Run F、复核方基线、上一轮 fresh holdout）只作**回归**，本文件里不断言它们，
  断言全部落在**没见过的项目内容**上：产业园区/冷链/数据中心三套题材，列名、行数、分值、
  章节名与 Run F 完全不同。规则必须是关系（血缘、作用域、子句角色），不是句子。
- 产品码里绝不出现复核方冻结文案；本文件也不把它们的原文当判据，只用同一类**结构**。

五组：
A1 合同语境不得凭"文字像技术要求"进入技术响应分母（靠评分血缘，不靠编号）；
A2 派生评分叶子的 schema 契约：下游必读字段在生产端一次配齐，正式链全程跑通；
B  否定只作用于它真正覆盖的那一截（span 作用域），真否定仍然可以安全 suppress；
C  同句里业务与编写元叙述的区分 + 改后语法清理（不产出 `，。`、悬空标点、半句话）；
D  陌生题材的泛化对照：以上四类在没见过的内容上仍然成立，且不放松 unsupported fact 门。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest import mock

import planning.scoring_taxonomy as scoring_taxonomy
from governance.client_facing_prose import (
    _clause_roles, _tidy_after_edit, client_facing_prose)
from governance.commitment_provenance import (
    HARD_BLOCK_PROVENANCE_CLASS, evaluate_commitments)
from planning.hierarchy import build_document_hierarchy, build_scoring_matrix
from planning.planner import build_planning_bundle
from planning.scoring_graph import (
    ANCHOR_GRAPH_ABSENT, ANCHOR_LEAF_CELL, ANCHOR_RUBRIC_CHAPTER_WEIGHT, ANCHOR_TEXT_ONLY,
    build_scoring_graph, leaf_by_id, rubric_lineage, rubric_lineage_index)
from planning.scoring_responsiveness import (
    CONTRACTUAL_CONTEXT, HEADER_FORMULA_NON_RESPONSE, TECHNICAL_BODY_DIRECT,
    responsiveness_fields)
from planning.scoring_taxonomy import (
    SCORING_ROW_REQUIRED_FIELDS, build_scoring_taxonomy, derived_scoring_leaf_row,
    must_respond_of, scoring_row_schema_violations)
from qa_severity import is_conditioned, is_denied, is_mention_not_use, semantic_status
from tender_intake.confirmation import apply_confirmation
from tender_intake.pack_builder import build_requirement_pack

APP_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# 夹具一：产业园区设施服务。评分表在「第三章 评审办法」，合同条款在「第六章 合同条款」。
# T1 是带独立分值列的父项/子项表；T2 是名称与标准同格的紧凑表；
# T3 只有卷次汇总（不得造叶子）。分值与 Run F 无任何重叠。
# ---------------------------------------------------------------------------
T1 = {
    "table_id": "TAB-P1", "is_scoring_table": True, "table_index": 0, "page_no": 18,
    "columns": ["卷次", "评审因素", "分值", "评审标准"],
    "heading_path": ["第三章 评审办法"],
    "rows": [
        ["技术部分（30分）", "供配电与制冷运维组织", "12",
         "根据投标人提交的供配电与制冷系统运维组织方案，按巡检点位、值守班次与应急处置衔接的完备性评分，优得12分"],
        ["技术部分（30分）", "绿色节能管理", "9",
         "按能耗计量、分项节流目标与节能技改安排的合理性横向比较，优得9分"],
        ["技术部分（30分）", "安防与通行管理", "9",
         "按出入口分级管控、访客流转与周界告警联动的可执行性横向比较，优得9分"],
    ],
}
T2 = {
    "table_id": "TAB-P2", "is_scoring_table": True, "table_index": 1, "page_no": 19,
    "columns": ["评审内容与计分"],
    "heading_path": ["第三章 评审办法"],
    "rows": [
        ["垃圾收运与分类管理（7分）：按分类投放点布置、收运频次与台账登记的完整性横向比较，优得7分，一般得4-6分"],
        ["入驻单位服务界面（3分）：按报修受理、公共区域交付与投诉回访的安排横向比较，优得3分"],
    ],
}
T3 = {
    "table_id": "TAB-P3", "is_scoring_table": True, "table_index": 2, "page_no": 17,
    "columns": ["标段", "分值"],
    "rows": [["技术部分", "30分"], ["商务部分", "20分"], ["合计", "50分"]],
}
RUBRIC_CHAPTER = "第三章 评审办法"
CONTRACT_CHAPTER = "第六章 合同条款及格式"

# 两条**文字都像技术要求**的行，只有一条真有评分血缘：
#   HAS_LINEAGE 来自评分表的格子里；
#   NO_LINEAGE  来自合同章的一个段落，引用的是投标人已经承诺过的内容。
HAS_LINEAGE = {
    "scoring_item_id": "SCR-P1", "category": "tender_scoring",
    "label": "根据投标人提交的供配电与制冷系统运维组织方案，按巡检点位、值守班次与应急处置衔接的完备性评分，优得12分",
    "weight_numeric": 12.0, "must_respond": True,
    "source": {"file_id": "FILE-1", "source_section": RUBRIC_CHAPTER,
               "source_paragraph_or_table": "table:0", "table_id": "TAB-P1",
               "heading_path": [RUBRIC_CHAPTER],
               "source_excerpt": "根据投标人提交的供配电与制冷系统运维组织方案"},
}
NO_LINEAGE = {
    "scoring_item_id": "SCR-P2", "category": "tender_scoring",
    # 题材与 Run F 的 SCR-053 完全不同，但形状一致：合同章段落、引用已承诺内容、无分值。
    "label": "依据乙方投标书承诺的供配电值守班次、巡检作业流程及应急处置程序",
    "weight_numeric": None, "must_respond": True,
    "source": {"file_id": "FILE-1", "source_section": CONTRACT_CHAPTER,
               "source_paragraph_or_table": "paragraph:88", "table_id": None,
               "paragraph_index": 88, "heading_path": [CONTRACT_CHAPTER],
               "source_excerpt": "依据乙方投标书承诺的供配电值守班次、巡检作业流程及应急处置程序"},
}
# 评分章里的一行，但既不是表格行也没有分值：那是卷次/计分口径的话，不是一件可响应的事。
RUBRIC_CHAPTER_NO_CELL = {
    "scoring_item_id": "SCR-P3", "category": "tender_scoring",
    "label": "技术部分评审采用横向比较分档，优、良、一般三档",
    "weight_numeric": None, "must_respond": True,
    "source": {"file_id": "FILE-1", "source_section": RUBRIC_CHAPTER,
               "source_paragraph_or_table": "paragraph:12", "table_id": None,
               "heading_path": [RUBRIC_CHAPTER],
               "source_excerpt": "技术部分评审采用横向比较分档"},
}
BRIEF = {"project_name": "园区设施服务采购项目", "medium": "WORD",
         "project_type": "产业园区综合体", "scenario": "技术标",
         "document_scale": "large_technical_tender"}
SELECTION = {"knowledge_usage_contracts": []}
EXTRACTION = {    "run_id": "RUN-T-008A3", "processing_mode": "FULL",
    "files": [{"file_id": "FILE-1", "original_filename": "产业园区服务招标.pdf",
               "media_type": "application/pdf", "sha256": "0" * 64, "page_count": 42}],
    "tables": [T3, T1, T2], "paragraphs": [], "document_outline": [],
}


def _pack(items: list[dict], requirements: list[dict] | None = None) -> dict:
    """走**正式**建包链（评分图在 pack_builder 里现算并落叶子 id），不手工拼产物。"""
    rows = requirements if requirements is not None else [
        {"normalized_requirement": str(item.get("label") or ""),
         "source_locator": item.get("source") or {},
         "requirement_type": "scoring", "mandatory_level_guess": "MUST",
         "confidence": 0.8}
        for item in items]
    return build_requirement_pack(EXTRACTION, rows)


def _stamped(items: list[dict], graph: dict) -> list[dict]:
    """把抽取行落到叶子上——**用图自己给出的 `item_links`**，与 `pack_builder` 同一步。

    叶子归属不是夹具可以凭感觉填的：填错一格，血缘门就是在给一个产品永远不会产生的形状
    判分。这里只搬动产品算出来的那条对应关系。
    """
    linked = {str(row.get("scoring_item_id")): str(row.get("leaf_id"))
              for row in graph.get("item_links") or []}
    leaves = leaf_by_id(graph)
    stamped: list[dict] = []
    for item in items:
        row = dict(item)
        leaf_id = linked.get(str(row.get("scoring_item_id") or ""))
        if leaf_id:
            row["scoring_leaf_id"] = leaf_id
            row["scoring_leaf_text"] = str((leaves.get(leaf_id) or {}).get("leaf_text") or "") or None
        stamped.append(row)
    return stamped


def _taxonomy(items: list[dict]) -> dict:
    graph = build_scoring_graph(EXTRACTION, items)
    return build_scoring_taxonomy(_stamped(items, graph), None, graph)


def _roles_of(clause: str) -> dict:
    """子句角色的读法与生产端一致：`_generic_internal_move` 只剥子句边界标点、**留着冒号**。

    `第一步转译：` 的判据就是"以冒号收尾、后面没有谓语"，先过一遍 `_core` 会把这格证据擦掉。
    """
    return _clause_roles(clause.rstrip("。；;，、!！?？ "))


def _record_of(taxonomy: dict, scoring_id: str) -> dict:
    for record in taxonomy["canonical_scoring_records"]:
        if scoring_id in (record.get("source_ids") or []):
            return record
    return {}


def _row_of(taxonomy: dict, scoring_id: str) -> dict:
    for row in taxonomy["rows"]:
        if str(row.get("scoring_item_id")) == scoring_id:
            return row
    return {}


def _gate(text: str, brief: dict | None = None) -> dict:
    """一条句子在真实承诺溯源链路上的效力：任一命中进 violations 才算挡。"""
    return evaluate_commitments(brief or {}, [],
                                {"artifact": {"cases": [{"text": text}]}})


class A1ContractualContextCannotEnterDenominatorAlone(unittest.TestCase):
    """§A1：分母要**评分血缘**，不是"文字像技术要求"。判据只用 locator/表格血缘/父项/分值。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.items = [dict(HAS_LINEAGE), dict(NO_LINEAGE), dict(RUBRIC_CHAPTER_NO_CELL)]
        cls.taxonomy = _taxonomy(cls.items)

    def test_clause_row_without_rubric_lineage_is_contractual_context(self):
        row = _row_of(self.taxonomy, "SCR-P2")
        self.assertEqual(row["responsiveness_class"], CONTRACTUAL_CONTEXT)
        self.assertFalse(row["counts_toward_technical_fulfillment"])
        self.assertEqual(row["responsiveness_class_source"],
                         "NOT_ANCHORED_CONTRACT_CLAUSE_LINEAGE")

    def test_text_only_verdict_stays_visible_after_the_gate(self):
        """降类不能把前一步的文本判断抹掉：复核的人要能看见"文字确实像技术要求"。"""
        row = _row_of(self.taxonomy, "SCR-P2")
        self.assertEqual(row["responsiveness_text_only_class"], TECHNICAL_BODY_DIRECT)
        self.assertEqual(row["responsiveness_text_only_rule"], "TECHNICAL_MATTER_NOUN")
        self.assertIsNotNone(row["rubric_lineage"])

    def test_lineage_evidence_names_locator_and_absent_weight(self):
        lineage = _row_of(self.taxonomy, "SCR-P2")["rubric_lineage"]
        self.assertIsNone(lineage["leaf_id"], "合同段落的行不该落到任何评分叶子")
        self.assertIsNone(lineage["source_table_id"], "来源不是表格行")
        self.assertEqual(lineage["source_chapter"], CONTRACT_CHAPTER)
        self.assertIsNone(lineage["own_weight"], "这一项自己没有分值")
        self.assertFalse(lineage["anchored"])

    def test_table_cell_row_is_not_demoted(self):
        """同一套词表下，真有表格血缘的那一行不受影响——否则这就是把技术项也赶走。"""
        row = _row_of(self.taxonomy, "SCR-P1")
        self.assertTrue(row["counts_toward_technical_fulfillment"])
        self.assertEqual(row["rubric_lineage"]["anchor_basis"], ANCHOR_LEAF_CELL)

    def test_rubric_chapter_non_cell_row_is_non_response_header_not_contract(self):
        """来源就在评分章、但不是表格行也没有分值 → 计分口径的话，另一条具名理由。"""
        row = _row_of(self.taxonomy, "SCR-P3")
        self.assertEqual(row["responsiveness_class"], HEADER_FORMULA_NON_RESPONSE)
        self.assertFalse(row["counts_toward_technical_fulfillment"])
        self.assertEqual(row["rubric_lineage"]["anchor_basis"], ANCHOR_TEXT_ONLY)

    def test_missing_scoring_table_never_demands_lineage(self):
        """没有可解析评分表是**文件形态**问题：此时文本判定原样成立，不拿缺表当降类理由。"""
        item = dict(NO_LINEAGE)
        item["source"] = dict(item["source"], heading_path=[RUBRIC_CHAPTER],
                              source_section=RUBRIC_CHAPTER)
        fields = responsiveness_fields(item["label"], scoring_class="TECHNICAL_SUBSTANTIVE",
                                       lineage=rubric_lineage(item, {"status": "NOT_DERIVABLE"}))
        self.assertEqual(fields["rubric_lineage"]["anchor_basis"], ANCHOR_GRAPH_ABSENT)
        self.assertTrue(fields["counts_toward_technical_fulfillment"])

    def test_rubric_chapter_with_own_weight_is_anchored(self):
        """同一评分章里、自带分值但没配到格子的行 → 保守留在分母（抽取失败≠不用写正文）。"""
        item = {"scoring_item_id": "SCR-P4", "label": "入驻单位报修受理与回访安排",
                "weight_numeric": 3.0,
                "source": {"source_section": RUBRIC_CHAPTER, "table_id": None,
                           "heading_path": [RUBRIC_CHAPTER]}}
        lineage = rubric_lineage(item, build_scoring_graph(EXTRACTION, [dict(HAS_LINEAGE)]))
        self.assertEqual(lineage["anchor_basis"], ANCHOR_RUBRIC_CHAPTER_WEIGHT)
        self.assertTrue(lineage["anchored"])

    def test_no_denominator_topic_without_a_leaf_or_rubric_chapter(self):
        for record in self.taxonomy["canonical_scoring_records"]:
            if not record.get("counts_toward_technical_fulfillment"):
                continue
            self.assertTrue(record.get("scoring_leaf_id") or record.get("volume"),
                            "进分母的主题必须有表格落点：" + str(record.get("topic")))

    def test_rule_is_not_keyed_to_a_specific_item_id(self):
        """血缘判定只读结构与 locator，不读评分项编号——编号在两次建包之间本来就会漂。"""
        for name in ("SCR-P2", "SCR-P1", "SCR-P3"):
            self.assertTrue(_row_of(self.taxonomy, name))
        # 编号整批改掉（各自不同，撞号的夹具等于在测另一件事），按标签找回同一批行。
        renamed = [dict(item, scoring_item_id="ZZZ-{:03d}".format(index))
                   for index, item in enumerate(self.items)]
        again = _taxonomy(renamed)
        by_label = {str(row.get("label") or "")[:12]: row for row in again["rows"]}
        for item, expected, counts in ((NO_LINEAGE, CONTRACTUAL_CONTEXT, False),
                                       (HAS_LINEAGE, TECHNICAL_BODY_DIRECT, True),
                                       (RUBRIC_CHAPTER_NO_CELL, HEADER_FORMULA_NON_RESPONSE, False)):
            row = by_label[str(item["label"])[:12]]
            self.assertEqual(row["responsiveness_class"], expected, item["scoring_item_id"])
            self.assertEqual(bool(row["counts_toward_technical_fulfillment"]), counts,
                             item["scoring_item_id"])

    def test_product_source_does_not_hardcode_the_reviewer_item(self):
        """产品码里不许出现复核方点名的编号或原句（关系规则，不是句子）。"""
        for rel in ("app/planning/scoring_responsiveness.py", "app/planning/scoring_graph.py",
                    "app/planning/scoring_taxonomy.py", "app/planning/hierarchy.py"):
            source = (APP_ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("SCR-053", source, rel)
            self.assertNotIn("依据乙方投标书承诺的物业管理结构配置", source, rel)
            self.assertNotIn("合同主要条款及格式", source, rel)


class A2DerivedLeafSchemaClosesProductionChain(unittest.TestCase):
    """§A2：派生叶子的下游必读字段必须在**生产端**一次配齐。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.items = [dict(HAS_LINEAGE)]          # 其余叶子一行都没抽出来 → 全部走派生
        cls.graph = build_scoring_graph(EXTRACTION, cls.items)
        cls.taxonomy = _taxonomy(cls.items)
        cls.derived = [row for row in cls.taxonomy["rows"]
                       if str(row.get("scoring_item_id", "")).startswith("LEAF-")]
        cls.plain = [row for row in cls.taxonomy["rows"]
                     if not str(row.get("scoring_item_id", "")).startswith("LEAF-")]

    def test_every_derived_leaf_row_carries_the_contract(self):
        self.assertTrue(self.derived, "评分表上有、抽取里没有的叶子必须补出派生行")
        for row in self.derived:
            missing = [key for key in SCORING_ROW_REQUIRED_FIELDS if key not in row]
            self.assertEqual(missing, [], "{} 缺 {}".format(row["scoring_item_id"], missing))

    def test_derived_row_field_set_covers_sibling_rows(self):
        """派生行的键集合必须覆盖兄弟行的键集合——否则消费端就得为它另写一套宽容读法。"""
        union: set[str] = set()
        for row in self.plain:
            union |= set(row)
        for row in self.derived:
            self.assertEqual(sorted(union - set(row)), [])

    def test_neutral_fill_is_declared_not_invented(self):
        row = self.derived[0]
        fill = row["derived_schema_fill"]
        self.assertIn("section_ids", fill["neutral"])
        self.assertTrue(row["section_ids"] == [], "中性值必须是可指认的中性值，不是别条的取值")
        self.assertEqual(row["must_respond_basis"], "DERIVED_SCORING_TABLE_LEAF")

    def test_producer_refuses_an_incomplete_derived_row(self):
        """缺字段要在生产端炸，而不是让下游 KeyError 或 `.get` 兜成 False。"""
        broken = dict(self.derived[0])
        broken.pop("must_respond")
        violations = scoring_row_schema_violations([broken])
        self.assertEqual(violations[0]["missing_fields"], ["must_respond"])
        # 守门是活的：把中性字段表里的一格抹掉，正式 taxonomy 构造必须拒绝而不是静默放行。
        with mock.patch.object(scoring_taxonomy, "SCORING_ROW_REQUIRED_FIELDS",
                               tuple(scoring_taxonomy.SCORING_ROW_REQUIRED_FIELDS)
                               + ("planned_response_words",)):
            scoring_taxonomy._DERIVED_NEUTRAL_FIELDS.pop("planned_response_words")
            try:
                with self.assertRaises(ValueError) as caught:
                    _taxonomy([dict(HAS_LINEAGE)])
                self.assertIn("planned_response_words", str(caught.exception))
            finally:
                scoring_taxonomy._DERIVED_NEUTRAL_FIELDS["planned_response_words"] = 0

    def test_formal_chain_builds_matrix_end_to_end(self):
        """raw rows → parent rubric → leaves → pack → matrix → canonical：正式链全程成功。"""
        matrix, hierarchy, pack = _formal_chain()
        self.assertEqual(hierarchy["validation"]["status"], "PASS",
                         hierarchy["validation"].get("errors"))
        self.assertTrue(matrix.get("rows"))
        for row in matrix["rows"]:
            self.assertIn("must_respond", row, row.get("scoring_item_id"))
        summary = matrix["coverage_summary"]
        self.assertEqual(summary["must_respond_total"], len(matrix["rows"]))
        self.assertGreater(summary["canonical_scoring_total"], 0)
        # A2 的落点：正式链**交出去**的每一行都带着全份契约，消费端不必为任何一种行写宽容读法。
        self.assertEqual(scoring_row_schema_violations(matrix["rows"]), [],
                         "正式链交出的行缺下游必读字段")
        # 表上每一条带分值的叶子都要在正式链里有个落点：漏一条就是"漏抽反而把覆盖率刷高"。
        table_leaf_ids = {str(row["leaf_id"]) for row in _scoring_table_leaves()}
        matrix_leaf_ids = {str(row.get("scoring_leaf_id") or "") for row in matrix["rows"]}
        self.assertEqual(sorted(table_leaf_ids - matrix_leaf_ids), [],
                         "评分表上的叶子必须全部进正式矩阵")

    def test_matrix_rows_report_derived_leaves_as_extraction_gap(self):
        matrix, _hierarchy, _pack = _formal_chain()
        gaps = [row for row in matrix["rows"] if row.get("extraction_gap")]
        self.assertTrue(gaps, "漏抽叶子必须进分母，不能因为没抽到就当这条不存在")
        self.assertTrue(all(row["must_respond"] for row in gaps))
        # 派生行与抽取行同构：兄弟行有的必读字段它一格不缺（否则消费端就得为它写宽容读法）
        for row in gaps:
            self.assertEqual([key for key in SCORING_ROW_REQUIRED_FIELDS if key not in row], [])
        self.assertEqual(sum(float(row.get("weight_if_known") or 0) for row in matrix["rows"]),
                         sum(float(row["weight"] or 0) for row in _scoring_table_leaves()))

    def test_weight_comes_from_the_table_cell_not_from_label_text(self):
        """卷次总分写在标签里也不能盖过这一格的分值：8/40 那种误读必须靠表格层级消掉。"""
        item = {"scoring_item_id": "SCR-W1",
                "label": "2 | 技术部分（30分） | 安防与通行管理（9分） | 按出入口分级管控横向比较，优得9分",
                "weight_numeric": 30.0,          # 抽取器从标签里抓到的第一个"N分"是卷次总分
                "must_respond": True,
                "scoring_leaf_id": _leaf_id_of("安防与通行管理"),
                "source": {"source_section": RUBRIC_CHAPTER, "table_id": "TAB-P1",
                           "heading_path": [RUBRIC_CHAPTER]}}
        taxonomy = _taxonomy([item])
        row = _row_of(taxonomy, "SCR-W1")
        self.assertEqual(row["weight_if_known"], 9.0)
        self.assertEqual(row["weight_scraped_from_text"], 30.0, "抓取值必须原样看得见")
        record = _record_of(taxonomy, "SCR-W1")
        self.assertEqual(record["weight_if_known"], 9.0)

    def test_weight_rule_is_not_keyed_to_the_observed_numbers(self):
        """换一套分值（不是 8/40）仍然按表格那一格走，证明没有把数写死。"""
        table = {"table_id": "TAB-W9", "is_scoring_table": True, "table_index": 0, "page_no": 3,
                 "columns": ["评审项", "分值", "评审标准"],
                 "heading_path": [RUBRIC_CHAPTER],
                 "rows": [["冷链设备运行监测", "5", "按监测点布置与告警处置闭环横向比较，优得5分"]]}
        graph = build_scoring_graph({"tables": [table]}, [])
        leaf = next(row["leaf_id"] for row in graph["leaves"]
                    if row["topic"] == "冷链设备运行监测")
        item = {"scoring_item_id": "SCR-W9", "label": "冷链（40分）| 监测（5分）",
                "weight_numeric": 40.0, "scoring_leaf_id": leaf,
                "source": {"source_section": RUBRIC_CHAPTER, "table_id": "TAB-W9"}}
        row = _row_of(build_scoring_taxonomy([item], None, graph), "SCR-W9")
        self.assertEqual(row["weight_if_known"], 5.0)

    def test_rows_outside_the_denominator_carry_the_contract_too(self):
        """不进分母的行（资信/资格/价格类）也带着"折叠后类别"这一格。

        折叠逻辑天生只走分母里的行，所以这一格必须由**生产端**给不在分母的行配平
        （中性值 = 没被折叠，仍是自己）。留白的话，消费端为了躲 KeyError 写的
        `.get("scoring_class_after_dedup", …)` 就是把缺 schema 洗成判定结果。
        这里只要求 taxonomy 自己拥有生产权的那几格；落点类字段（章节/证据/需求引用）
        由 `build_scoring_matrix` 在建行时配齐，那份契约在正式链两条测试里证。
        """
        item = {"scoring_item_id": "SCR-B1", "label": "提供有效的ISO9001认证证书复印件得3分",
                "weight_numeric": 3.0, "must_respond": True,
                "source": {"source_section": CONTRACT_CHAPTER, "table_id": None,
                           "heading_path": [CONTRACT_CHAPTER]}}
        row = _row_of(_taxonomy([dict(HAS_LINEAGE), item]), "SCR-B1")
        for key in ("scoring_class_after_dedup", "duplicate_of", "must_respond",
                    "must_respond_basis"):
            self.assertIn(key, row, "行 schema 缺一格：" + key)
        self.assertEqual(row["scoring_class_after_dedup"], row["scoring_class"])
        self.assertIsNone(row["duplicate_of"])
        self.assertEqual(row["must_respond_basis"], "PACK_SCORING_ITEM")

    def test_must_respond_reader_names_its_basis(self):
        value, basis = must_respond_of({"must_respond": False})
        self.assertEqual((value, basis), (False, "PACK_SCORING_ITEM"))
        value, basis = must_respond_of({"label": "没有这一格的旧包"})
        self.assertEqual((value, basis), (True, "INFERRED_SCORING_ITEM_DEFAULT"))


class BNegationScopesToItsOwnSpan(unittest.TestCase):
    """§B：否定只管它真正覆盖的那一截，不是"看见不/非/未/无就 suppress 全句"。"""

    # 陌生题材：数据中心。前半句否定的是**论述依据**，后半句是现场既存构型的肯定断言
    # （①数量词+空间构件 ②共位谓语，两个成分同句共现才是这一类断言的形状）。
    FRAMING_THEN_FACTS = (
        "定位依据来自现场踏勘记录而非通用模板："
        "服务标的位于园区西侧，为单一地块，"
        "柴发机房、UPS 与电池房在同一建筑内共用一条检修通道；")
    PLAIN_FACTS = ("服务标的位于园区西侧，为单一地块，"
                   "柴发机房、UPS 与电池房在同一建筑内共用一条检修通道；")
    TRUE_DENIAL = ("服务标的并非位于单一地块，"
                   "柴发机房与电池房也不共用一条检修通道。")
    CONDITION_BEFORE = "若服务标的为单一地块，则巡检动线按一个闭环组织。"
    CONDITION_AFTER = "服务标的为单一地块，若甲方另有分期交付要求则按两期组织。"

    def test_negation_in_another_span_does_not_suppress_the_facts(self):
        for token in ("单一地块", "同一建筑"):
            index = self.FRAMING_THEN_FACTS.index(token)
            status = semantic_status(self.FRAMING_THEN_FACTS, index, index + len(token))
            self.assertEqual(status, "ASSERTED", token)
            self.assertFalse(is_mention_not_use(self.FRAMING_THEN_FACTS, index))
            self.assertFalse(is_denied(self.FRAMING_THEN_FACTS, index))

    def test_the_asserted_facts_are_blocked(self):
        gate = _gate(self.FRAMING_THEN_FACTS)
        self.assertEqual(gate["status"], "BLOCK")
        self.assertTrue(gate["violations"])

    def test_dropping_the_framing_gives_the_same_verdict(self):
        """去掉论述前缀后判定不变：证明不是靠前缀、也不是把语气当判据。"""
        self.assertEqual(_gate(self.PLAIN_FACTS)["status"], "BLOCK")

    def test_genuine_negation_is_still_suppressed(self):
        gate = _gate(self.TRUE_DENIAL)
        self.assertEqual(gate["status"], "PASS", "真否定必须仍然可以安全放行")
        self.assertTrue(gate["suppressed"], "要看得见这一截被判成非主张，不能只是没报警")
        index = self.TRUE_DENIAL.index("单一地块")
        self.assertIn(semantic_status(self.TRUE_DENIAL, index, index + 4),
                      ("DENIED", "MENTION"))

    def test_conditionals_still_work_but_only_forward(self):
        index = self.CONDITION_BEFORE.index("单一地块")
        self.assertTrue(is_conditioned(self.CONDITION_BEFORE, index))
        after = self.CONDITION_AFTER.index("单一地块")
        self.assertFalse(is_conditioned(self.CONDITION_AFTER, after),
                         "写在命中之后的条件词不能回头洗前面的主张")

    def test_scope_rule_is_a_construction_not_this_projects_words(self):
        """同一形状换个题材（校园后勤）照样成立：判据是冒号/逗号划出的作用域。"""
        sentence = ("口径来自委托范围而非行业惯例：食堂与操作间位于同一楼层，"
                    "粗加工与备餐共用一条传菜通道；")
        index = sentence.index("同一楼层")
        self.assertEqual(semantic_status(sentence, index, index + 4), "ASSERTED")
        self.assertEqual(_gate(sentence)["status"], "BLOCK")

    def test_no_hardcoded_counterexample(self):
        source = (APP_ROOT / "app/qa_severity.py").read_text(encoding="utf-8")
        for phrase in ("单一院落", "同一建筑", "竖向叠加", "通用表述"):
            self.assertNotIn(phrase, source)


class CClauseLevelSanitizerAndCleanup(unittest.TestCase):
    """§C：同句里业务与元叙述分开处置；删过之后必须还是一句能读的话。"""

    def test_pure_writing_instruction_removed(self):
        # 陌生：主语是语言单位（写法/前半句），没有任何服务动作。
        sentence = ("分级台账的写法须成对出现，前半句写明核对口径，"
                    "后半句写明核对不成立时退回哪一步，禁止只写结论不写依据。")
        output, actions = client_facing_prose(sentence)
        self.assertEqual(output.strip(), "")
        self.assertTrue([row for row in actions if row.get("applied")])

    def test_pure_generation_boundary_removed(self):
        sentence = "此类事项在本章仅登记为不纳入技术正文，不为其生成说明段、清单或空白附件页。"
        output, _actions = client_facing_prose(sentence)
        self.assertEqual(output.strip(), "")

    def test_mixed_sentence_keeps_business_and_cuts_meta(self):
        sentence = ("报修受理须在 30 分钟内响应、2 小时内到场处置，"
                    "因此本节把编制过程本身固化为可重复执行的程序。")
        output, actions = client_facing_prose(sentence)
        self.assertTrue(output.strip(), "业务那一截不能一起洗掉")
        self.assertIn("30 分钟内响应", output)
        self.assertIn("2 小时内到场", output)
        self.assertNotIn("编制过程", output)
        self.assertEqual(output, f"{output}")
        applied = [row for row in actions if row.get("applied")]
        self.assertTrue(applied)
        self.assertEqual(applied[0]["shape_id"], "GENERIC_INTERNAL_WRITING_NARRATION")

    def test_client_facing_statement_is_kept_verbatim(self):
        """合法的客户可读范围陈述不能因为出现"本节/本章"就被删。"""
        for sentence in (
                "本节仅界定上述难点、管理原则与验证口径，具体交接程序由退出移交专章展开；",
                "核验中发现短缺或损坏的，当场形成书面确认单并三方签认，"
                "责任认定与后续处理按合同既有约定执行，本节不作条款性表述。"):
            output, _actions = client_facing_prose(sentence)
            self.assertEqual(output.strip(), sentence.strip())

    def test_cleanup_leaves_no_punctuation_artifact(self):
        """上一版的真实坏形状：删掉尾段留下 `，。`，删掉中间一截留下悬空逗号/分号。"""
        sentence = ("夜间巡检记录逐条登记为编制约束，评分口径一律不进入正文，"
                    "正文中也不出现对自身分档的描述。")
        output, _actions = client_facing_prose(sentence)
        for artifact in ("，。", "；。", "、。", "。，", "：。", "，，", "；；"):
            self.assertNotIn(artifact, output, output)
        self.assertFalse(re.search(r"[，、：]$", output.strip()), output)

    def test_cleanup_is_a_fixed_point(self):
        for text in ("口径以台账为准，；月度汇总另行报送。", "…执行，因此", "因此，"):
            once = _tidy_after_edit(text)
            self.assertEqual(once, _tidy_after_edit(once))

    def test_cleanup_never_invents_content(self):
        """清理只做减法：长度只能不变或变短，不能出现原文没有的实词。"""
        text = "本节仅登记为编制约束，"
        output = _tidy_after_edit(text)
        self.assertLessEqual(len(output), len(text))
        self.assertTrue(all(ch in text for ch in output))

    def test_action_choice_is_idempotent(self):
        for sentence in (
                "分级台账的写法须成对出现，前半句写明核对口径。",
                "报修受理须在 30 分钟内响应，因此本节把编制过程本身固化为可重复执行的程序。",
                "本节仅界定上述难点、管理原则与验证口径；"):
            once, _a = client_facing_prose(sentence)
            twice, _b = client_facing_prose(once)
            self.assertEqual(once, twice)

    def test_roles_are_computed_from_syntax_not_from_the_known_sentences(self):
        for sentence in ("评分口径一律不进入正文", "写法须成对出现", "第一步转译：",
                         "把各项评审因素逐条改写成需求条目"):
            self.assertTrue(_roles_of(sentence)["internal"], sentence)
        for sentence in ("责任认定与后续处理按合同既有约定执行", "须在进场后与甲方逐项核定"):
            self.assertFalse(_roles_of(sentence)["internal"], sentence)


class DGeneralizationOnUnfamiliarContent(unittest.TestCase):
    """§D：任务书点名的七类陌生内容，逐类都有独立反例，且没有一条来自已知集。"""

    def test_scoring_table_requirement_vs_contractual_context(self):
        taxonomy = _taxonomy([dict(HAS_LINEAGE), dict(NO_LINEAGE)])
        self.assertTrue(_record_of(taxonomy, "SCR-P1")["counts_toward_technical_fulfillment"])
        self.assertFalse(_record_of(taxonomy, "SCR-P2")["counts_toward_technical_fulfillment"])

    def test_parent_and_child_scoring_stay_one_topic(self):
        """形状相近的两张表，结果必须相反：两档=一件事，两笔独立分值=两件事。"""
        band_table = {"table_id": "TAB-K1", "is_scoring_table": True, "table_index": 0,
                      "page_no": 5, "columns": ["卷次", "评审因素", "分值", "评审标准"],
                      "heading_path": [RUBRIC_CHAPTER],
                      "rows": [
                          ["技术部分（25分）", "设备设施管理", "10",
                           "管理架构清晰、台账与巡检动线齐备且有第三方复核记录的得10分"],
                          ["技术部分（25分）", "设备设施管理", "10",
                           "架构不完整但给出补位安排与临时台账的得5分"],
                          ["技术部分（25分）", "能耗与碳排放管理", "15",
                           "分项计量装置覆盖全部主要用能系统且给出年度节能目标的得15分"],
                      ]}
        graph = build_scoring_graph({"tables": [band_table]}, [])
        leaves = [row for row in graph["leaves"] if row["topic"] == "设备设施管理"]
        self.assertEqual(len(leaves), 1, "同一因素的档次行不是两件事")
        self.assertEqual(leaves[0]["weight"], 10.0)
        self.assertEqual(leaves[0]["volume"], "技术部分")
        self.assertEqual(leaves[0]["merged_row_positions"], [1, 2], "两档行仍要点名是哪两行")
        self.assertEqual(graph["counts"]["leaf_weight_total"], 25.0, "父项合计不等于卷次总分就是漏")

        split_table = {"table_id": "TAB-K2", "is_scoring_table": True, "table_index": 1,
                       "page_no": 6, "columns": ["卷次", "评审因素及权重", "评分标准"],
                       "heading_path": [RUBRIC_CHAPTER],
                       "rows": [
                           ["技术部分（25分）", "冷链与能耗专项（14分）",
                            "冷链机组运行监测方案完备、告警处置闭环可核查的得8分"],
                           ["技术部分（25分）", "冷链与能耗专项（14分）",
                            "能耗分项计量与年度节能目标齐备可核查的得6分"],
                           ["技术部分（25分）", "消毒与病媒防治（11分）",
                            "消杀频次、药品台账与抗性监测安排完备可核查的得11分"],
                       ]}
        again = build_scoring_graph({"tables": [split_table]}, [])
        children = [row for row in again["leaves"]
                    if row.get("parent_group") == "冷链与能耗专项"]
        self.assertEqual(len(children), 2, "各行分值加起来正好等于父项括号里的总分=两件事")
        self.assertEqual(sorted(row["weight"] for row in children), [6.0, 8.0])
        parent = next(row for row in again["parents"] if row["weight"] == 14.0)
        self.assertEqual(parent["split_into"], 2)
        self.assertTrue(parent["weights_reconcile"])

    def test_nested_irrelevant_negation_keeps_the_claim(self):
        sentence = ("说明：以下口径不是照抄通用范本，而是按委托范围逐条核对——"
                    "周转库房位于地下一层，与装卸区在同一楼层相邻。")
        index = sentence.index("同一楼层")
        self.assertEqual(semantic_status(sentence, index, index + 4), "ASSERTED")

    def test_real_unsupported_existing_fact_still_blocks(self):
        gate = _gate("园区已建成三座立体车库并全部投入运营，车位引导系统已完成联调验收。")
        self.assertEqual(gate["status"], "BLOCK")

    def test_mixed_business_and_meta_sentence(self):
        sentence = ("供配电巡检按日执行并留存记录，因此本节把编制过程本身固化为可重复执行的程序。")
        output, _actions = client_facing_prose(sentence)
        self.assertIn("供配电巡检按日执行", output)
        self.assertNotIn("编制过程", output)

    def test_pure_internal_instruction_removed(self):
        output, _actions = client_facing_prose(
            "本章只登记为编制约束，不为其生成说明段、清单或空白附件页。")
        self.assertEqual(output.strip(), "")

    def test_legitimate_ben_jie_statement_survives(self):
        sentence = "本节的任务是把四个评价维度从形容词转成可核查的判定动作。"
        output, _actions = client_facing_prose(sentence)
        self.assertEqual(output.strip(), sentence.strip())

    def test_unfamiliar_domain_does_not_need_domain_words(self):
        """陌生题材的评分表照样长结构与分母：不靠任何本项目专有词。"""
        graph = build_scoring_graph(EXTRACTION, [dict(HAS_LINEAGE)])
        readings = graph.get("counts") or {}
        self.assertGreaterEqual(readings.get("leaves") or 0, 4)
        self.assertEqual(readings.get("leaf_weight_total"), 40.0)


def _leaf_id_of(topic: str) -> str:
    graph = build_scoring_graph(EXTRACTION, [])
    for row in graph["leaves"]:
        if row.get("topic") == topic:
            return str(row["leaf_id"])
    raise AssertionError("夹具里找不到该主题：" + topic)


def _scoring_table_leaves() -> list[dict]:
    """这张评分表结构上应有的可响应叶子（正式链应当一条不落全部接住）。"""
    return list(build_scoring_graph(EXTRACTION, [dict(HAS_LINEAGE)])["leaves"])


def _formal_chain() -> tuple[dict, dict, dict]:
    """抽取行 → 建包 → Todd 确认 → 规划包 → 层级 → 评分矩阵，全部走**正式**入口。

    A2 要证的是"正式链全程成功"，所以这里不手工拼 bundle：手拼的层级缺 `chapter_id`
    之类的生产字段，测出来的是夹具能不能跑，不是产品能不能跑。
    """
    pack = apply_confirmation(_pack([dict(HAS_LINEAGE)]), {})
    bundle = build_planning_bundle(pack, BRIEF, SELECTION, production=True)
    hierarchy = build_document_hierarchy(pack, bundle)
    return build_scoring_matrix(pack, bundle, hierarchy), hierarchy, pack


if __name__ == "__main__":
    unittest.main()
