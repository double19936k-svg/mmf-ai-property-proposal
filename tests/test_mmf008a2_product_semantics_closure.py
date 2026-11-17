"""MMF008A2 反例：三条业务语义 blocker 的**泛化**证明 + 临时态结算子项。

与 `test_mmf008a_product_semantics.py` 的分工：那一份钉住 Run F 的既有读数（已知回归集），
这一份钉的是**换一份标书还得成立**。终审会另抽 fresh holdout，"已知的 54/71 条全过"
不能代替泛化，所以本文件的夹具一律用与本合同标书无关的词汇（施工组织设计、绿色施工、
食堂/园区/巡检岗、业主签认、监理批复），并且每条反例都写成"关系"而不是"字符串"。

六组：
① 评分语义图从**表格单元格**长出形状（父项/叶子/权重/挂载），数量是解析结果不是常量；
② 分母按六个响应维度组成，两条尺（叶子数 + 加权分）并列，父子不重复计、举证类不欠账；
③ 临时态（PROVISIONAL）带具名原因，只有真实内容缺口挡 Clean；
④ 承诺溯源按 proposal / condition / material requirement / existing fact 四种关系判定，
   现场拓扑断言仍 BLOCK；
⑤ 客户文风按 REMOVE / REWRITE / KEEP 三态泛化：不吞业务内容、不半截拼接、不碰边界声明；
⑥ 本合同题材的词表不参与分母与身份——陌生题材的表照样闭合，要素来自合同自己的标签。

零 Provider 调用：全部离线，夹具都是本文件里手写的字典。
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / "app"))
sys.path.insert(0, str(APP))

from governance import commitment_provenance as provenance  # noqa: E402
from governance.client_facing_prose import (  # noqa: E402
    client_facing_prose, internal_narration_findings)
from governance.commitment_provenance import (  # noqa: E402
    HARD_BLOCK_PROVENANCE_CLASS, classify_provenance_semantics, evaluate_commitments,
    site_premise_claims)
from longform.provisional_settlement import (  # noqa: E402
    CLEAN_BLOCKING_PROVISIONAL_REASONS, GLOBAL_RUBRIC, NON_RESPONSE, PROTECTED_EVIDENCE,
    REAL_CONTENT_DEFICIENCY, RULE_DERIVED, settle_provisional_section,
    settle_provisional_sections)
from planning.scoring_mapping import (  # noqa: E402
    KIND_DETECT_TERMS, detect_topic_kinds, elements_for)
from planning.scoring_graph import build_scoring_graph, graph_readings, leaf_by_id  # noqa: E402
from planning.scoring_responsiveness import (  # noqa: E402
    CONTRACTUAL_CONTEXT, CROSS_FILE_PROTECTED_EVIDENCE, FULFILLMENT_REQUIRED_DIMENSIONS,
    HEADER_FORMULA_NON_RESPONSE, MAY_NOT_BIND_TECHNICAL_RESPONSE_CU, RESPONSE_DIMENSIONS,
    TECHNICAL_BODY_ANONYMOUS_STRUCTURE, TECHNICAL_BODY_DIRECT, WHOLE_DOCUMENT_RUBRIC,
    responsiveness_fields)
from planning.scoring_taxonomy import build_scoring_taxonomy  # noqa: E402

# ---------------------------------------------------------------------------
# 夹具：一份与本合同完全无关的评分表。列名、行数、分值、题材全部不同。
# ---------------------------------------------------------------------------
# A 表：分值单列（"评审项 | 分值 | 评审标准"）；"分部分项施工方案"占两行、
#      第二行是同一因素的**档次行**（没写自己的分值）——必须并成一个叶子，不能算两件事。
TABLE_A = {
    "table_id": "TAB-A", "is_scoring_table": True, "table_index": 0, "page_no": 11,
    "columns": ["评审项", "分值", "评审标准"],
    "heading_path": ["第五章 评标办法"],
    "rows": [
        ["一、施工组织设计", "25", "按施工组织设计的完整性与针对性横向比较，优得25分"],
        ["分部分项施工方案", "10", "针对危险性较大的分部分项工程单独编制施工方案的得10分"],
        ["分部分项施工方案", "", "未单独编制但给出总体控制措施的得5分"],
        ["进度计划保障", "8", "施工进度计划网络图完整、关键线路清晰的得8分"],
        ["质保体系", "7", "质量保障体系与检验批划分合理可行的得7分"],
    ],
}
# B 表：名称与标准挤在同一格（没有独立评分标准列），括号里带分值。
TABLE_B = {
    "table_id": "TAB-B", "is_scoring_table": True, "table_index": 1, "page_no": 12,
    "columns": ["评审内容与计分"],
    "heading_path": ["第五章 评标办法"],
    "rows": [
        ["安全文明施工措施（12分）：现场防护、文明施工措施布置齐全、可落地，优得12分，一般得6-11分"],
        ["绿色施工与节能降耗（6分）：有量化指标与台账记录、可核查，优得6分，一般得3-5分"],
    ],
}
# C 表：只有"标段 + 分值"的汇总表，没有任何评分标准文字 → 不得造出叶子。
TABLE_C = {
    "table_id": "TAB-C", "is_scoring_table": True, "table_index": 2, "page_no": 10,
    "columns": ["标段构成", "分值分配"],
    "rows": [["技术标", "50分"], ["商务标", "40分"], ["合计", "90分"]],
}
ITEMS = [
    {"scoring_item_id": "SCR-A1", "label": "针对危险性较大的分部分项工程单独编制施工方案的得10分",
     "source": {"table_id": "TAB-A",
                "source_excerpt": "针对危险性较大的分部分项工程单独编制施工方案的得10分"}},
    {"scoring_item_id": "SCR-A2", "label": "施工进度计划网络图完整",
     "source": {"table_id": "TAB-A", "source_excerpt": "施工进度计划网络图完整、关键线路清晰的得8分"}},
    {"scoring_item_id": "SCR-B1", "label": "现场防护与文明施工措施布置",
     "source": {"table_id": "TAB-B", "source_excerpt": "现场防护、文明施工措施布置齐全"}},
]
BRIEF = {"project_name": "某市政房建工程施工组织与绿色施工专项评审",
         "facts": ["项目为两栋独立楼栋，A 栋与 B 栋分设出入口。"]}


def _graph() -> dict:
    return build_scoring_graph({"tables": [TABLE_C, TABLE_A, TABLE_B]}, ITEMS)


class G1ScoringGraphIsParsedNotFiltered(unittest.TestCase):
    """§1：形状从**表格单元格**读出来；数量是解析结果，不是"从 11 条里滤掉 7 条"。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = _graph()
        cls.readings = graph_readings(cls.graph)

    def test_roll_up_table_yields_no_leaves_and_is_named(self):
        skipped = {str(row.get("table_id")): str(row.get("reason"))
                   for row in self.graph.get("skipped_tables") or []}
        self.assertIn("TAB-C", skipped, "汇总表没有被具名跳过，就会凭'技术标 50分'造出一个 50 分的假叶子")
        self.assertTrue(skipped["TAB-C"], "跳过必须写出原因")
        self.assertNotIn("TAB-C", {str(row.get("table_id"))
                                   for row in self.graph.get("parents") or []})

    def test_band_rows_merge_into_one_leaf(self):
        leaves = leaf_by_id(self.graph)
        merged = [row for row in leaves.values() if row.get("weight_basis") == "MERGED_WEIGHT_COLUMN"]
        self.assertEqual([str(row["topic"]) for row in merged], ["分部分项施工方案"])
        row = merged[0]
        self.assertEqual(float(row["weight"]), 10.0,
                         "同因子的档次行并成一个叶子后，权重取父项那格的 10 分，不是 10+5 也不是两叶")
        self.assertGreaterEqual(len(row.get("merged_row_positions") or []), 2,
                                "合并必须留下被并掉的行位置，否则读不出'两行成一事'")

    def test_weights_come_from_cells_and_reconcile(self):
        for parent in self.graph.get("parents") or []:
            with self.subTest(parent=parent["parent_id"]):
                self.assertTrue(parent["weights_reconcile"],
                                f"{parent['label']} 子项权重与父项对不上账")
                self.assertTrue(str(parent["weight_basis"]), "权重必须写明取自哪一格/哪种形")
        total = float(self.readings["by_volume"][""]["leaf_weight_total"])
        self.assertEqual(total, 68.0, "叶子总分应等于这张表自己各格之和（25+10+8+7+12+6）")

    def test_generality_counts_change_with_the_table(self):
        # 同一条规则、少一行的表：计数与权重必须跟着变。写死任何数字都会在这一条上红。
        smaller = {"tables": [dict(TABLE_A, rows=TABLE_A["rows"][:2], table_id="TAB-SMALL")]}
        readings = graph_readings(build_scoring_graph(smaller, []))
        self.assertEqual(int(readings["leaf_count"]), 2)
        self.assertEqual(float(readings["leaf_weight_total"]), 35.0)
        again = graph_readings(build_scoring_graph({"tables": [TABLE_B]}, []))
        self.assertEqual(int(again["leaf_count"]), 2)
        self.assertEqual(float(again["leaf_weight_total"]), 18.0)


class G2DenominatorComposition(unittest.TestCase):
    """§1/§2：六个响应维度、两条尺、父子不重复计、举证类不欠正文的账。"""

    def test_six_dimensions_and_their_dispositions(self):
        self.assertEqual(len(RESPONSE_DIMENSIONS), 6)
        self.assertEqual(set(FULFILLMENT_REQUIRED_DIMENSIONS),
                         {TECHNICAL_BODY_DIRECT, TECHNICAL_BODY_ANONYMOUS_STRUCTURE})
        self.assertEqual(set(MAY_NOT_BIND_TECHNICAL_RESPONSE_CU),
                         {CROSS_FILE_PROTECTED_EVIDENCE, WHOLE_DOCUMENT_RUBRIC,
                          CONTRACTUAL_CONTEXT, HEADER_FORMULA_NON_RESPONSE})

    def test_absent_annotation_defaults_to_required(self):
        # 漏标不能变成"少写正文"的理由：默认要响应，豁免必须有正面凭据。
        from planning.scoring_responsiveness import record_requires_body_response
        self.assertTrue(record_requires_body_response({}))
        self.assertFalse(record_requires_body_response(
            {"responsiveness_class": CROSS_FILE_PROTECTED_EVIDENCE}))

    @staticmethod
    def _rows_for(graph: dict) -> list[dict]:
        leaves = leaf_by_id(graph)
        rows = []
        for item in ITEMS:
            leaf_id = next((str(link["leaf_id"]) for link in graph["item_links"]
                            if link["scoring_item_id"] == item["scoring_item_id"]), "")
            leaf = leaves.get(leaf_id) or {}
            rows.append({
                "scoring_item_id": item["scoring_item_id"],
                "label": str(leaf.get("topic") or item["label"]),
                "planned_response_sections": ["S01-01"], "scoring_leaf_id": leaf_id,
                "scoring_leaf_text": str(leaf.get("text") or ""),
                "weight_if_known": leaf.get("weight")})
        # 同一件事被抽成两行：这是"父子/同因子重复计分"最常见的形状。
        rows.append(dict(rows[0], scoring_item_id="SCR-A1-DUP"))
        return rows

    def test_leaf_keyed_identity_folds_parent_and_children(self):
        graph = _graph()
        rows_in = self._rows_for(graph)
        taxonomy = build_scoring_taxonomy(rows_in, None, graph)
        records = taxonomy.get("canonical_scoring_records") or []
        leaf_ids = [str(row.get("scoring_leaf_id") or "") for row in records]
        self.assertEqual(len(leaf_ids), len(set(leaf_ids)),
                         "一条叶子只能有一条记录：两条抽取行落在同一叶子上必须折成一条")
        self.assertIn("TAB-A-R02", leaf_ids)
        denominator = taxonomy["denominator"]
        # 原始行数是**交进来的行数**（抽取行 + 漏抽补的行），分母是折出来的叶子数：
        # 两者不许混用，也不许由夹具写死——这份表的行数只取决于上面那几个夹具。
        self.assertEqual(int(denominator["raw_scoring_rows"]),
                         len(rows_in) + len(denominator["extraction_gap_ids"]),
                         "raw_scoring_rows 对不上「交进来的行 + 漏补的行」，分母口径就 drift 了")
        self.assertEqual(int(denominator["canonical_total"]), len(records))
        self.assertLess(int(denominator["canonical_total"]),
                        int(denominator["raw_scoring_rows"]),
                        "折完不比原始行少 ⇒ 父子/同因子根本没折")
        self.assertGreaterEqual(int(denominator["merged_duplicate_rows"]), 1,
                                "折叠必须留下被折掉的行数，否则读不出'两行成一事'")
        for row in records:
            self.assertEqual(str(row.get("identity_basis") or ""), "SCORING_TABLE_CELL",
                             "有图时身份来源必须写明是表格单元格，不是文本相似度")
        for key in ("composition_by_response_dimension", "respondable_weight_points",
                    "canonical_weight_points", "identity_basis_counts",
                    "extraction_gap_ids", "scoring_graph_status"):
            self.assertIn(key, denominator)
        composition = denominator["composition_by_response_dimension"]
        self.assertEqual(set(composition), set(RESPONSE_DIMENSIONS),
                         "六个维度都要在组成里出现，缺席的也要报 0，不能整格消失")
        self.assertEqual(sum(int(row["topics"]) for row in composition.values()),
                         len(records), "组成表的主题数与记录数必须闭合")
        self.assertEqual(
            round(sum(float(row["weight_points"]) for row in composition.values()), 2),
            round(float(denominator["canonical_weight_points"]), 2),
            "两条尺（叶子数 / 加权分）加起来必须对得上同一张表")

    def test_extraction_gap_leaf_becomes_a_record(self):
        # 图上成立、抽取里一行都没有（Run F 的"管理优势 2 分"就是这个形状）：
        # 不能因为抽不到就消失，必须具名成为一条待响应记录。
        graph = _graph()
        taxonomy = build_scoring_taxonomy(self._rows_for(graph), None, graph)
        denominator = taxonomy["denominator"]
        gaps = [row for row in taxonomy["canonical_scoring_records"] if row.get("extraction_gap")]
        self.assertTrue(gaps, "抽取覆盖不到的叶子必须仍在分母里")
        self.assertEqual(sorted(str(row["canonical_scoring_id"]) for row in gaps),
                         sorted(str(item) for item in denominator["extraction_gap_ids"]),
                         "漏抽具名 id 在记录与分母两处必须一致")
        for row in gaps:
            self.assertTrue(str(row.get("responsiveness_class_source") or ""),
                            "漏抽记录的类别也要有正面凭据（同卷次共识），不能默认")


class G3ProvisionalSettlementReasons(unittest.TestCase):
    """§1 子项：临时态带具名原因；只有真实内容缺口挡 Clean，门本身一个字都不删。"""

    def test_rule_and_obligation_reasons_do_not_block_clean(self):
        cases = (
            ("纯超长规则效应", {"status": "BLOCK", "length_status": "SECTION_OVER_LENGTH"},
             {}, [], RULE_DERIVED, "SECTION_OVER_LENGTH_RULE"),
            ("门 BLOCK 但读不出内容缺口", {"status": "BLOCK"}, {}, [],
             RULE_DERIVED, "SECTION_GATE_BLOCK_WITHOUT_CONTENT_GAP"),
            ("举证在别的文件里", {"status": "PASS"}, {},
             [{"responsiveness_class": CROSS_FILE_PROTECTED_EVIDENCE,
               "response_status": "MISSING", "canonical_scoring_id": "SCN-004",
               "display_topic": "类似项目经验"}],
             PROTECTED_EVIDENCE, "OBLIGATION_CROSS_FILE_PROTECTED_EVIDENCE"),
            ("整本质量口径", {"status": "PASS"}, {},
             [{"responsiveness_class": WHOLE_DOCUMENT_RUBRIC, "response_status": "MISSING",
               "canonical_scoring_id": "SCN-009", "display_topic": "内容结构是否清晰"}],
             GLOBAL_RUBRIC, "OBLIGATION_WHOLE_DOCUMENT_RUBRIC"),
            ("部次标题不要求正文响应", {"status": "PASS"}, {},
             [{"responsiveness_class": HEADER_FORMULA_NON_RESPONSE, "response_status": "MISSING",
               "canonical_scoring_id": "SCN-007", "display_topic": "技术及服务部分"}],
             NON_RESPONSE, "OBLIGATION_HEADER_FORMULA_NON_RESPONSE"),
        )
        for why, gate, provisional, obligations, code, basis in cases:
            with self.subTest(case=why):
                row = settle_provisional_section("S02-01", gate=gate,
                                                 provisional=provisional, obligations=obligations)
                self.assertIn(code, [str(item["code"]) for item in row["reasons"]])
                self.assertIn(basis, [str(item["basis"]) for item in row["reasons"]])
                self.assertFalse(row["blocks_clean"],
                                 f"{why} 是规则/举证效应，不该把整节按内容缺口拦下来")

    def test_real_deficiency_blocks_and_is_named(self):
        for why, gate, provisional, basis in (
                ("深度确实差一块", {"status": "PASS"}, {"depth_deficit": "MATERIAL"},
                 "DEPTH_DEFICIT_MATERIAL"),
                ("正文里没有可读内容", {"status": "PASS", "meaningful_body": False}, {},
                 "MEANINGFUL_BODY_ABSENT"),
                ("流程项没写", {"status": "BLOCK", "process_missing": ["complaint_closure"]}, {},
                 "PROCESS_MISSING"),
                ("必覆盖项缺", {"status": "BLOCK",
                               "must_cover": {"电梯机房巡查": "MISSING"}}, {},
                 "MUST_COVER_MISSING")):
            with self.subTest(case=why):
                row = settle_provisional_section("S04-02", gate=gate, provisional=provisional,
                                                 obligations=[])
                self.assertTrue(row["blocks_clean"], f"{why} 必须仍然挡 Clean")
                self.assertEqual(row["blocking_reasons"], [REAL_CONTENT_DEFICIENCY])
                self.assertIn(basis, [str(item["basis"]) for item in row["reasons"]],
                              "拦下来的那一节要说得出缺的是哪一项，不能只报'provisional 非空'")

    def test_unattributed_provisional_fails_closed(self):
        row = settle_provisional_section("S05-01", gate={"status": "PASS"},
                                         provisional={}, obligations=[])
        self.assertTrue(row["blocks_clean"], "认不出原因的临时态按真实缺口处理，不默认放行")
        self.assertEqual([item["basis"] for item in row["reasons"]], ["UNATTRIBUTED_PROVISIONAL"])

    def test_gate_itself_is_not_removed(self):
        self.assertEqual(set(CLEAN_BLOCKING_PROVISIONAL_REASONS), {REAL_CONTENT_DEFICIENCY})
        settled = settle_provisional_sections([
            {"section_id": "A", "gate": {"status": "BLOCK", "length_status": "SECTION_OVER_LENGTH"},
             "provisional": {}},
            {"section_id": "B", "gate": {"status": "BLOCK", "process_missing": ["p1"]},
             "provisional": {}},
        ], [])
        self.assertEqual(settled["blocking_section_ids"], ["B"])
        self.assertEqual(settled["non_blocking_section_ids"], ["A"],
                         "PROVISIONAL_SECTIONS 门没有删，只是不再让纯规则效应冒充内容缺口")
        self.assertEqual(settled["reason_code_counts"][REAL_CONTENT_DEFICIENCY], 1)
        self.assertEqual(settled["reason_code_counts"][RULE_DERIVED], 1)


class G4ProvenanceRelations(unittest.TestCase):
    """§2：判定建立在 proposal / condition / material requirement / existing fact 四种关系上。"""

    def _semantics(self, clause: str, **kwargs):
        return classify_provenance_semantics(clause, **kwargs)

    def test_non_assertive_label_and_material_list_are_not_fact_claims(self):
        # 复核方点名的两个形状的**同形换词版**：岗位名带括号说明、审批依赖材料的名词短语。
        for clause in ("巡检岗（流动巡查：地下车库与首层大堂）",
                       "业主签认的作业票、监理批复的进场单与第三方检测报告"):
            with self.subTest(clause=clause[:14]):
                self.assertNotEqual(self._semantics(clause)[0], HARD_BLOCK_PROVENANCE_CLASS)

    def test_duty_assignment_is_method_not_existing_fact(self):
        semantics, basis, _ = self._semantics("流动巡查承担地下车库与首层大堂的安全巡视与引导")
        self.assertEqual(semantics, "PROPOSED_SERVICE_METHOD")
        self.assertTrue(basis)

    def test_genuine_fabrication_and_site_premise_still_block(self):
        blocked = (
            ("本项目所有制度文件均已整理成册并作为本方案附件提交", "既有状态＋完工"),
            ("本项目位于单一园区，各专业共用一个出入口与一条巡视动线", "现场拓扑断言"),
        )
        for clause, why in blocked:
            with self.subTest(why=why):
                self.assertEqual(self._semantics(clause)[0], HARD_BLOCK_PROVENANCE_CLASS)
        # 一把尺只认「数量词 + 空间构件 + 共处谓语」这一种关系：同句里两处现场前提都要点出来，
        # 只报后半句等于把前半句的断言放走。
        self.assertEqual([item.group(0) for item in site_premise_claims(
            "本项目为单一楼栋，工程与保洁共用同一楼层通道")], ["单一楼栋", "同一楼层"])

    def test_site_premise_with_current_project_source_is_a_fact(self):
        generated = {"artifact": {"paragraphs": [
            "本项目为两栋独立楼栋，A 栋与 B 栋分设出入口，各专业共用同一出入口时段需错开安排。"]}}
        report = evaluate_commitments(BRIEF, [], generated)
        kinds = {str(row.get("claim_type")) for row in report.get("violations") or []}
        self.assertNotIn("UNSUPPORTED_SITE_PREMISE", kinds,
                         "底稿逐字写着'两栋独立楼栋…分设出入口'，同一条断言就不该再被拦")

    def test_gate_and_repair_share_the_site_ruler(self):
        generated = {"artifact": {"paragraphs": [
            "本项目为单一地块，各专业共用出入口，因此采用统一巡视路线。"]}}
        blocked = evaluate_commitments({"facts": ["招标文件未载现场地块构成。"]}, [], generated)
        self.assertIn("UNSUPPORTED_SITE_PREMISE",
                      {str(row.get("claim_type")) for row in blocked.get("violations") or []})
        repaired = provenance.apply_local_repairs(generated, {"facts": ["未载"]})
        blob = json.dumps(repaired, ensure_ascii=False)
        self.assertNotIn("单一地块", blob, "门拦下、修不回就是'门严修宽'")
        self.assertIn("各专业共用出入口", blob, "修复只去掉未经核验的构型限定，安排本身要留给甲方")

    def test_no_reviewer_case_text_baked_into_any_pattern(self):
        # 反硬编码守卫：把所有编译好的正则原文拿出来，复核方点名的两句原话不得在内。
        patterns = [item.pattern for item in vars(provenance).values() if isinstance(item, re.Pattern)]
        blob = "\n".join(patterns)
        for forbidden in ("甲方批准的书面材料", "门岗（定点值守", "定点值守：主出入口"):
            self.assertNotIn(forbidden, blob, f"修复被写死成复核方给的那句原文：{forbidden}")


class G5SanitizerThreeStates(unittest.TestCase):
    """§3：REMOVE / REWRITE / KEEP 三态在**没见过的句子**上成立，且 KEEP 永远优先。"""

    REMOVE_MUST_GO = (
        "本节按问题清单逐条对应的编排方式组织全部段落，不单独展开制度背景说明。",
        "第二条是证据分级规则，把正文句子划成已核验、待核验与不作数三种口径。",
        "第三步口径复核：正文所有数字一律换成区间表述后再进入下一节。",
        "为使内部评审能够顺着本方案逐条核对，本章采用台账式写法登记每一条依据。",
    )
    REWRITE_MIXED = (
        ("可行性的判定方法是负荷匹配：本项目用餐峰值与后厨产能须在同一时段内成立，"
         "不以后厨扩能为前提。",
         "可行性的判定方法是负荷匹配",
         "本项目用餐峰值与后厨产能须在同一时段内成立，不以后厨扩能为前提"),
        ("本节不新增时限或人员承诺；月度从台账提取受理量与办结量，作为阶段沟通的输入之一。",
         "本节不新增时限或人员承诺",
         "月度从台账提取受理量与办结量，作为阶段沟通的输入之一"),
    )
    KEEP_MUST_SURVIVE = (
        "上述建议均不构成新增人员、设备、频次或时限承诺，实施深度以甲方书面确认为准。",
        "凡涉及设备台账与型号的取值，以招标文件与业主已确认的资产清册为准，业主另有答复的从其答复。",
        "检查依据只有两项：岗位职责与本项目已明确的服务标准，不在此之外追加量化要求。",
        "监理另有要求的部位，先列入待澄清清单，取得书面答复后排入计划。",
        "本项目把偏差定性限定在三类可核查输入之内：检查记录、考核结论与使用者有指向的反映。",
    )

    def test_pure_writing_narration_never_survives(self):
        for sentence in self.REMOVE_MUST_GO:
            with self.subTest(text=sentence[:16]):
                out, actions = client_facing_prose(sentence)
                self.assertTrue([row for row in actions if row["applied"]],
                                f"整句都在讲怎么写，净化器一个字没动：{sentence[:24]}")
                self.assertNotIn("本节按问题清单", out)

    def test_mixed_sentence_loses_the_meta_and_keeps_the_business(self):
        for sentence, must_go, must_keep in self.REWRITE_MIXED:
            with self.subTest(text=sentence[:16]):
                out, actions = client_facing_prose(sentence)
                applied = [row for row in actions if row["applied"]]
                self.assertTrue(applied)
                # 泛化层必须**自己**认出这两句：Run F 那 12 条字面形状一条都没见过它们。
                self.assertIn("GENERIC_INTERNAL_WRITING_NARRATION",
                              {str(row.get("shape_id")) for row in applied},
                              "动作来自 Run F 已知形状，泛化层等于没参与")
                self.assertNotIn(must_go, out, "内部编写语没被切掉")
                self.assertIn(must_keep, out, "业务内容被一起洗掉了")

    def test_client_facing_boundary_sentences_are_untouched(self):
        for sentence in self.KEEP_MUST_SURVIVE:
            with self.subTest(text=sentence[:16]):
                out, actions = client_facing_prose(sentence)
                self.assertEqual(out, sentence, "客户可读的边界/澄清/确认机制被当成内部元叙述")
                self.assertEqual([row for row in actions if row["applied"]], [])

    def test_idempotent_and_never_splices_mid_clause(self):
        for sentence in self.REMOVE_MUST_GO + tuple(item[0] for item in self.REWRITE_MIXED) \
                + self.KEEP_MUST_SURVIVE:
            with self.subTest(text=sentence[:16]):
                once, _ = client_facing_prose(sentence)
                twice, second_actions = client_facing_prose(once)
                self.assertEqual(twice, once, "第二遍还在改 ⇒ 净化器不幂等")
                self.assertEqual([row for row in second_actions if row["applied"]], [])
                for fragment in re.split(r"[。；，、]", once):
                    fragment = fragment.strip()
                    if fragment:
                        self.assertIn(fragment, sentence,
                                      f"产物里出现了原句没有的片段（疑似半截拼接）：{fragment}")

    def test_detection_and_repair_share_one_ruler(self):
        for sentence in self.REMOVE_MUST_GO:
            with self.subTest(text=sentence[:16]):
                findings = internal_narration_findings(sentence)
                self.assertTrue(findings)
                self.assertTrue(all(row["auto_repair_applied"] for row in findings))
                self.assertEqual(internal_narration_findings(client_facing_prose(sentence)[0]), [])

    def test_anchor_survives_inside_a_meta_looking_paragraph(self):
        text = ("本节按台账式写法组织内容，岗位履历与资格证明【待投标人确认：所需证明】。")
        out, _actions = client_facing_prose(text)
        self.assertIn("【待投标人确认：所需证明】", out,
                      "锚点是 Bidder Evidence 门在产物里唯一的可定位形状，删了门就查无此据")


class G6VocabularyIsNotTheDenominator(unittest.TestCase):
    """§1/§2 的收尾：本合同题材的词表**不参与**分母与身份。

    `scoring_mapping.KIND_DETECT_TERMS` / `FALLBACK_TOPIC_ELEMENTS` 带"管理团队/服务保障/
    退出移交/优势"这类本合同词。本组证明的是：一份题材完全陌生的评分表（餐后垃圾、冷链、
    能源计量）照样长得出叶子、进得了分母、拿得到最低实质要素表——要素来自**合同自己的
    标签**，不来自那张词表。词表命中与否只影响要素写得细不细，不影响这条主题存不存在。
    """

    TABLE = {
        "table_id": "TAB-X", "is_scoring_table": True, "table_index": 0, "page_no": 7,
        "columns": ["评审项目", "分值", "评分细则"],
        "heading_path": ["第三章 评标办法"],
        "rows": [
            ["餐后垃圾收运组织", "9", "明确餐后垃圾暂存点位、收运频次与车辆动线的得9分"],
            ["冷链设备运行监测", "6", "给出温控台账与异常处置流程的得6分"],
            ["能源计量与节流", "5", "分区计量表计齐全、有量化节流目标的得5分"],
        ],
    }

    @classmethod
    def setUpClass(cls) -> None:
        items = [{"scoring_item_id": f"SCR-X{index}", "label": str(row[0]),
                  "source": {"table_id": "TAB-X", "source_excerpt": str(row[2])}}
                 for index, row in enumerate(cls.TABLE["rows"], start=1)]
        cls.graph = build_scoring_graph({"tables": [cls.TABLE]}, items)
        cls.readings = graph_readings(cls.graph)
        leaves = leaf_by_id(cls.graph)
        cls.rows = []
        for item in items:
            leaf_id = next((str(link["leaf_id"]) for link in cls.graph["item_links"]
                            if link["scoring_item_id"] == item["scoring_item_id"]), "")
            leaf = leaves.get(leaf_id) or {}
            cls.rows.append({"scoring_item_id": item["scoring_item_id"], "label": item["label"],
                             "scoring_leaf_id": leaf_id,
                             "scoring_leaf_text": str(leaf.get("text") or ""),
                             "weight_if_known": leaf.get("weight"),
                             "planned_response_sections": ["S01-01"]})
        cls.taxonomy = build_scoring_taxonomy(cls.rows, None, cls.graph)

    def test_the_familiar_vocabulary_really_does_not_cover_this_domain(self):
        # 这条是前面几条的前提：词表在这份标书上一个词都没命中，后面还能算对才叫泛化。
        vocab = [term for terms in KIND_DETECT_TERMS.values() for term in terms]
        for leaf in self.graph["leaves"]:
            with self.subTest(topic=str(leaf.get("topic"))[:12]):
                text = " ".join(str(leaf.get(key) or "") for key in ("topic", "text"))
                self.assertEqual([term for term in vocab if term in text], [],
                                 "这份夹具其实还落在本合同词表里，泛化证明不成立")
                self.assertEqual(detect_topic_kinds({"topic": leaf.get("topic")}), [])

    def test_leaves_and_denominator_close_on_an_unfamiliar_table(self):
        denominator = self.taxonomy["denominator"]
        self.assertEqual(str(denominator["scoring_graph_status"]), "DERIVED")
        self.assertEqual(int(denominator["canonical_total"]), int(self.readings["leaf_count"]))
        self.assertEqual(round(float(denominator["canonical_weight_points"]), 2),
                         round(float(self.readings["leaf_weight_total"]), 2),
                         "陌生题材上两条尺（叶子数/加权分）也要闭合")
        self.assertEqual(list(denominator["identity_basis_counts"]), ["SCORING_TABLE_CELL"],
                         "身份只能来自表格单元格；换题材不许退化成文本相似度")

    def test_minimum_substance_elements_come_from_the_contract_own_words(self):
        for record in self.taxonomy["canonical_scoring_records"]:
            with self.subTest(topic=str(record.get("topic"))[:12]):
                elements = elements_for(record)
                self.assertTrue(elements,
                                "词表认不出就不给要素 ⇒ 陌生题材的主题永远没有最低实质要求")
                concepts = {str(item) for item in
                            ((record.get("evidence_contract") or {}).get("concepts") or [])}
                own = str(record.get("topic") or "") + str(record.get("scoring_table_topic") or "")
                for name in elements:
                    self.assertTrue(name in own or name in concepts,
                                    f"要素 {name} 既不在合同原话里也不在合同概念里，是外部词表塞进来的")


if __name__ == "__main__":
    unittest.main(verbosity=2)
