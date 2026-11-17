# -*- coding: utf-8 -*-
"""MMF008A4 Blocker 1 的反例集：Scoring Graph 必须是 canonical Authority 的唯一宇宙。

复核方确认的事实：前半链已经对（评分表结构图建得出来、叶子可响应、分值对得平、派生行
schema 齐、落点绑定合法），坏在最后一步——`canonical_scoring_authority()` 重新
`build_scoring_taxonomy(rows)` **不带图**，于是"行 → 图 → 可响应叶子 → 唯一主题"这条链
到最后一层被换成"行 → 按文本相似度折叠"。同一批行两套答案；且无图时折不到叶子就读不到
表格那一格的文本，范围门与第二维双双失明。

姿态与上一轮一致（也是复核方自己那把尺）：判据是**关系**（血缘、图、身份集合），不是句子、
不是编号、不是某个数。夹具因此是**没见过的题材**（高校学生公寓与教学楼物业服务），表名、
列名、行数、分值、章节名与 Run F 及上一轮夹具都不重叠；找行一律按内容线索，不按编号。

七组：
G1 正式全链跑完后，planning 宇宙与 authority 宇宙**逐 canonical 身份相等**（差集为空，
   不是个数相等）；换一套不同表/不同行的夹具仍然相等，且个数跟着夹具变（没写死）；
G2 来源语义贯通：矩阵行带 `source`，canonical 记录的 `rubric_lineage.source_chapter /
   source_locator` 到 Authority 这一跳不得变 null，派生叶子行的来源由**图**给出并具名；
G3 不该进分母的行（合同语境 / 资信举证 / 全表计分口径）在**两层**里都不在，Authority
   不得把它们重新捞回；该在的技术事项两层都必须在（反方向不许误杀）；
G4 攻击「删记录缩分母」：把矩阵自己落盘的 canonical 记录砍掉，宇宙不许跟着缩；
G5 攻击「矩阵的行与它自己的记录互不认账」：同一张图下两份身份集对不上 ⇒ 具名挡下，
   不许静默改用其中一份当分母；
G6 没有图的合法退路：老产物不带图 / 结构建不出来时照旧按文本折叠，但**具名说清用的是
   哪把尺**，并且不假装做过逐身份对账；
G7 结构哨兵：Authority 里不得再出现"不带图"的分类器调用，期望宇宙只有一处产地，
   产品码里不写复核方冻结文案，门本身不得被删。

零 Provider 调用：纯本地临时目录 + 合成夹具，不读任何凭证。
"""
from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
for path in (str(APP), str(Path(__file__).resolve().parent)):
    if path not in sys.path:
        sys.path.insert(0, path)

from longform.scoring_authority import (  # noqa: E402
    CANONICAL_EXTRACTION_FILES, CANONICAL_MATRIX_FILE, CANONICAL_PLAN_FILE,
    SCORING_AUTHORITY_CLEAN_BLOCKERS, canonical_scoring_authority, canonical_topic_coverage)
from planning.hierarchy import build_document_hierarchy, build_scoring_matrix  # noqa: E402
from planning.planner import build_planning_bundle  # noqa: E402
from planning.scoring_graph import ANCHOR_LEAF_CELL  # noqa: E402
from planning.scoring_responsiveness import (  # noqa: E402
    FULFILLMENT_REQUIRED_DIMENSIONS, record_requires_body_response)
from planning.scoring_taxonomy import DENOMINATOR_SCORING_CLASS, topic_key  # noqa: E402
from tender_intake.confirmation import apply_confirmation  # noqa: E402
from tender_intake.pack_builder import build_requirement_pack  # noqa: E402

APP_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# 夹具：高校学生公寓与教学楼物业服务。评分表在「第三章 评标办法」，合同条款在
# 「第七章 合同条款及格式」，资信要求在「第二章 投标人资格要求」。
# C1 三行三件事（"公共区域保洁与消杀""寒暑假专项保障"故意**不抽出来**，用来量漏抽叶子）；
# C2 同一父项的两个得分档写在两行里（档不是两件事，必须折成一条身份，分值不求和）；
# C4 父项真被拆成两件独立事项（5+3=8，紧凑两列表形状）；
# C3 只有"标段 → 分值"的汇总表（不得造叶子）。分值与 Run F / 上一轮夹具零重叠。
# ---------------------------------------------------------------------------
RUBRIC_CHAPTER = "第三章 评标办法"
CONTRACT_CHAPTER = "第七章 合同条款及格式"
CREDENTIAL_CHAPTER = "第二章 投标人资格要求"

C1 = {
    "table_id": "TAB-C1", "is_scoring_table": True, "table_index": 0, "page_no": 21,
    "columns": ["卷次", "评标因素", "满分", "评标标准"],
    "heading_path": [RUBRIC_CHAPTER],
    "rows": [
        ["技术部分（24分）", "公寓值守与门禁组织", "10",
         "根据投标人提交的公寓夜间值守与门禁组织方案，按值守班次、门禁台账与突发情况处置衔接的完备性评分，优得10分"],
        ["技术部分（24分）", "公共区域保洁与消杀", "7",
         "按卫生间所配比、消杀频次与用药台账登记的完整性横向比较评分，优得7分"],
        ["技术部分（24分）", "寒暑假专项保障", "7",
         "按空置期巡查安排、水电断供预案与留校师生服务响应的合理性横向比较评分，优得7分"],
    ],
}
C2 = {
    "table_id": "TAB-C2", "is_scoring_table": True, "table_index": 1, "page_no": 22,
    "columns": ["卷次", "评标因素", "满分", "评标标准"],
    "heading_path": [RUBRIC_CHAPTER],
    "rows": [
        ["技术部分", "教学楼与实验室保洁", "8",
         "大面积日常保洁的班次与频次安排合理，按巡检记录评分，优得8分"],
        ["技术部分", "教学楼与实验室保洁", "8",
         "夜间专项深度保洁与假期集中作业安排的合理性，按台账评分，一般得4分"],
    ],
}
C4 = {
    "table_id": "TAB-C4", "is_scoring_table": True, "table_index": 3, "page_no": 23,
    "columns": ["评标因素", "评标标准"],
    "heading_path": [RUBRIC_CHAPTER],
    "rows": [
        ["食堂与商业街管理（8分）",
         "食堂后厨油烟与餐厨垃圾分类清运的组织安排，按作业指导书完备性评分，优得5分"],
        ["食堂与商业街管理（8分）",
         "商业街铺位装修、营业时间与噪声控制的巡查约束安排，按台账完备性评分，优得3分"],
    ],
}
C3 = {
    "table_id": "TAB-C3", "is_scoring_table": True, "table_index": 2, "page_no": 20,
    "columns": ["标段", "分值"],
    "rows": [["技术部分", "24分"], ["商务部分", "16分"], ["合计", "40分"]],
}
EXTRACTION = {
    "run_id": "RUN-T-008A4", "processing_mode": "FULL",
    "files": [{"file_id": "FILE-1", "original_filename": "高校学生公寓物业服务招标.pdf",
               "media_type": "application/pdf", "sha256": "1" * 64, "page_count": 38}],
    "tables": [C3, C1, C2, C4], "paragraphs": [], "document_outline": [],
}


def _extraction(tables: list[dict]) -> dict:
    return dict(EXTRACTION, tables=list(tables))


def _item(label: str, chapter: str, *, table_id: str | None = None,
          pointer: str = "paragraph:31", excerpt: str | None = None) -> dict:
    """一条抽取出的评分项：`source` 就是它在招标文件里的落点（表格行 / 段落）。

    建包链按需求行自己编号（SCR-001…），夹具不预设编号——找行一律按内容线索。
    `excerpt` 是抽取器记下的原文片段：叶子归属只按"格子里含这段摘录"配，
    所以摘录必须是表格里真出现过的那段。
    """
    return {"label": label,
            "source": {"file_id": "FILE-1", "source_section": chapter,
                       "source_paragraph_or_table": (f"table:{table_id}" if table_id
                                                     else pointer),
                       "table_id": table_id, "heading_path": [chapter],
                       "source_excerpt": (excerpt or label)[:28]}}


DUTY = _item("根据投标人提交的公寓夜间值守与门禁组织方案，按值守班次、门禁台账与"
             "突发情况处置衔接的完备性评分，优得10分", RUBRIC_CHAPTER,
             table_id="TAB-C1", excerpt="根据投标人提交的公寓夜间值守与门禁组织方案")
CLEANING = _item("教学楼与实验室保洁评分：大面积日常保洁与夜间专项深度保洁两档，优得8分",
                 RUBRIC_CHAPTER, table_id="TAB-C2",
                 excerpt="大面积日常保洁的班次与频次安排合理")
CONTRACT_CLAUSE = _item("依据乙方投标书承诺的门禁值守班次、消杀作业流程及突发情况处置程序"
                        "组织服务并评分", CONTRACT_CHAPTER)
CREDENTIAL_EVIDENCE = _item("投标人须提供ISO9001质量管理体系认证证书复印件及近三年三个"
                            "同类项目合同复印件加盖公章作为评分依据", CREDENTIAL_CHAPTER)
GLOBAL_RUBRIC = _item("技术部分各评标因素均采用横向比较分档评分，优、一般、较差三档",
                      RUBRIC_CHAPTER)
CHAIN_ITEMS = [DUTY, CLEANING, CONTRACT_CLAUSE, CREDENTIAL_EVIDENCE, GLOBAL_RUBRIC]
CLUES = {"duty": "公寓夜间值守", "cleaning": "教学楼与实验室保洁",
         "contract": "门禁值守班次、消杀作业流程", "credential": "ISO9001",
         "global": "横向比较分档"}
BRIEF = {"project_name": "高校学生公寓与教学楼物业服务采购项目", "medium": "WORD",
         "project_type": "高校物业", "scenario": "技术标",
         "document_scale": "large_technical_tender"}
SELECTION = {"knowledge_usage_contracts": []}


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _pack_of(items: list[dict], extraction: dict | None = None) -> dict:
    """走**正式**建包链（评分图与叶子归属在 `pack_builder` 里现算），不手工拼产物。"""
    rows = [{"normalized_requirement": str(item.get("label") or ""),
             "source_locator": item.get("source") or {},
             "requirement_type": "scoring", "mandatory_level_guess": "MUST",
             "confidence": 0.8} for item in items]
    return apply_confirmation(build_requirement_pack(extraction or EXTRACTION, rows), {})


def _formal_run(root: Path, items: list[dict], extraction: dict | None = None) -> tuple:
    """raw rows → 评分表结构图 → 可响应叶子 → 矩阵 → 映射，全部走正式入口并落盘。"""
    pack = _pack_of(items, extraction)
    bundle = build_planning_bundle(pack, BRIEF, SELECTION, production=True)
    hierarchy = build_document_hierarchy(pack, bundle)
    matrix = build_scoring_matrix(pack, bundle, hierarchy)
    run = root / "run"
    _write(run / CANONICAL_MATRIX_FILE, matrix)
    _write(run / CANONICAL_PLAN_FILE, hierarchy)
    _write(run / CANONICAL_EXTRACTION_FILES[0], pack)
    return run, matrix, pack


def _matrix_rows(matrix: dict) -> list[dict]:
    return [row for row in matrix.get("rows") or [] if isinstance(row, dict)]


def _row(matrix: dict, clue: str) -> dict:
    hits = [row for row in _matrix_rows(matrix) if clue in str(row.get("label") or "")]
    if len(hits) != 1:
        raise AssertionError(f"线索 {clue!r} 在矩阵行里命中 {len(hits)} 行，需要恰好 1 行")
    return hits[0]


def _identity(row: dict) -> str:
    return str(row.get("topic_key") or row.get("scoring_topic_key")
               or topic_key(row.get("topic") or row.get("label") or ""))


def _planning_universe(matrix: dict) -> set[str]:
    return {_identity(row) for row in (matrix.get("canonical_scoring_records") or [])
            if _identity(row)}


def _gate_self_consistent(matrix: dict, records: list[dict]) -> None:
    """把嵌在矩阵里的那份门结论改成与"砍过之后的记录"自洽。

    这样挡下来的原因只可能是本轮新增的**宇宙一致性**，不是上一轮已经关掉的门形状。
    """
    gate = dict(matrix.get("pre_generation_scoring_mapping_gate") or {})
    gate.update({"substantive_topic_total": len(records), "mapped_total": len(records),
                 "unmapped_total": 0, "status": "PASS", "may_enter_generation": True})
    matrix["pre_generation_scoring_mapping_gate"] = gate


class G1FormalChainSingleUniverse(unittest.TestCase):
    """§Blocker 1 主断言：正式全链上 planning 宇宙 == authority 宇宙，**逐身份**相等。"""

    def test_g1_authority_expected_set_equals_planning_identity_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, matrix, pack = _formal_run(Path(tmp), CHAIN_ITEMS)
            self.assertEqual("DERIVED", (pack.get("scoring_graph") or {}).get("status"),
                             "夹具没建出评分表结构图，这一条就没量到带图的尺")
            planning = _planning_universe(matrix)
            self.assertTrue(planning, "正式链一条 canonical 主题都没有")
            reading = canonical_scoring_authority(run)
            coverage = canonical_topic_coverage(run, matrix,
                                                matrix["canonical_scoring_records"])
        consistency = reading["topic_universe_planning_consistency"]
        self.assertTrue(consistency["applies"], "逐身份对账没被启用，等于没核")
        self.assertTrue(consistency["identities_equal"])
        self.assertEqual([], consistency["only_in_authority"])
        self.assertEqual([], consistency["only_in_planning"])
        self.assertEqual(planning, set(coverage["expected"]))
        self.assertEqual(len(planning), reading["topic_expected_count"])
        # 尺的名字要说清是哪一把：按表格单元格折叠，不是按文本相似度。
        self.assertEqual("CANONICAL_SCORING_GRAPH_LEAF_FOLDING", reading["topic_universe_ruler"])
        self.assertEqual("DERIVED", reading["topic_scoring_graph_state"])
        # 覆盖率宇宙与 N/A 分母必须是同一个数（修前是 43/39/35，三把尺各说各话）。
        sources = reading["canonical_topic_sources"]
        self.assertEqual(reading["topic_expected_count"], sources["CANONICAL_MATRIX_ROWS"])
        self.assertEqual(reading["topic_expected_count"], sources["CANONICAL_MATRIX_RECORDS"])

    def test_g1b_every_authority_identity_has_a_named_planning_topic(self):
        """逐身份点名，且能认出是**哪一条**主题（个数相等也可能是两批不同的主题）。"""
        with tempfile.TemporaryDirectory() as tmp:
            run, matrix, pack = _formal_run(Path(tmp), CHAIN_ITEMS)
            coverage = canonical_topic_coverage(run, matrix,
                                                matrix["canonical_scoring_records"])
            admitted = {_identity(row): str(row.get("topic") or "") for row
                        in matrix["canonical_scoring_records"]}
        self.assertEqual([], sorted(set(coverage["expected"]) - set(admitted)))
        for identity in coverage["expected"]:
            self.assertTrue(admitted[identity].strip(), f"{identity} 的身份没有主题文本")

    def test_g1c_universe_is_computed_not_hardcoded(self):
        """换夹具（少一张表、少抽几行）⇒ 宇宙个数**跟着变**，且相等关系仍成立。"""
        with tempfile.TemporaryDirectory() as tmp:
            full_run, full, _ = _formal_run(Path(tmp) / "full", CHAIN_ITEMS)
            full_reading = canonical_scoring_authority(full_run)
            lean_run, lean, _ = _formal_run(Path(tmp) / "lean", [DUTY], _extraction([C3, C1]))
            lean_reading = canonical_scoring_authority(lean_run)
        self.assertEqual(len(_planning_universe(full)), full_reading["topic_expected_count"])
        self.assertEqual(len(_planning_universe(lean)), lean_reading["topic_expected_count"])
        self.assertLess(lean_reading["topic_expected_count"],
                        full_reading["topic_expected_count"],
                        "少了一张评分表而宇宙没跟着变，说明分母是从别处抄来的")
        for reading in (full_reading, lean_reading):
            consistency = reading["topic_universe_planning_consistency"]
            self.assertTrue(consistency["identities_equal"])
            self.assertEqual([], consistency["only_in_authority"])
            self.assertEqual([], consistency["only_in_planning"])

    def test_g1d_band_rows_of_one_parent_are_one_identity(self):
        """同一父项的两个得分档写在两行里：折成**一条**身份，分值不求和。"""
        with tempfile.TemporaryDirectory() as tmp:
            run, matrix, pack = _formal_run(Path(tmp), [DUTY, CLEANING])
            reading = canonical_scoring_authority(run)
        leaves = [row for row in (pack["scoring_graph"].get("leaves") or [])
                  if str((row.get("locator") or {}).get("table_id")) == "TAB-C2"]
        self.assertEqual(1, len(leaves), "两档被拆成两件事：分母会乘以档数")
        self.assertEqual(8.0, float(leaves[0].get("weight")), "两档求和把 8 分刷成 16 分")
        self.assertTrue(reading["topic_universe_planning_consistency"]["identities_equal"])

    def test_g1e_split_parent_is_two_identities_with_reconciled_points(self):
        """父项真被拆成两件独立事项（5+3=8）：两条身份、分值对得平，两边仍相等。"""
        with tempfile.TemporaryDirectory() as tmp:
            run, matrix, pack = _formal_run(Path(tmp), CHAIN_ITEMS)
            reading = canonical_scoring_authority(run)
        graph = pack["scoring_graph"]
        leaves = [row for row in (graph.get("leaves") or [])
                  if str((row.get("locator") or {}).get("table_id")) == "TAB-C4"]
        self.assertEqual(2, len(leaves), "拆分的两件事被并成一条：少一笔正文欠账")
        self.assertEqual({5.0, 3.0}, {float(row.get("weight")) for row in leaves})
        parents = [row for row in (graph.get("parents") or [])
                   if str(row.get("table_id")) == "TAB-C4"]
        self.assertTrue(parents and all(row.get("weights_reconcile") for row in parents))
        self.assertTrue(reading["topic_universe_planning_consistency"]["identities_equal"])

    def test_g1f_rollup_only_table_makes_no_leaves(self):
        """只有"标段→分值"的汇总表不得造叶子；造了就是凭空多一笔正文欠账。"""
        with tempfile.TemporaryDirectory() as tmp:
            _run, _matrix, pack = _formal_run(Path(tmp), CHAIN_ITEMS)
        graph = pack["scoring_graph"]
        self.assertIn("TAB-C3", [str(row.get("table_id"))
                                 for row in graph.get("skipped_tables") or []])
        self.assertEqual([], [row for row in graph.get("leaves") or []
                              if str((row.get("locator") or {}).get("table_id")) == "TAB-C3"])

    def test_g1g_extraction_gap_is_in_both_universes(self):
        """表上有、抽取里没有的叶子是**漏抽**：两层都必须在，且具名 extraction_gap。"""
        with tempfile.TemporaryDirectory() as tmp:
            run, matrix, pack = _formal_run(Path(tmp), [DUTY])
            reading = canonical_scoring_authority(run)
        gaps = [row for row in matrix["canonical_scoring_records"] if row.get("extraction_gap")]
        self.assertTrue(gaps, "表上其他事项没被抽出来，却没有一条漏抽记录")
        self.assertTrue(all(record_requires_body_response(row) for row in gaps))
        expected = set(reading["topic_universe_planning_consistency"]["only_in_authority"]
                       + reading["topic_universe_planning_consistency"]["only_in_planning"])
        for record in gaps:
            self.assertIn(_identity(record), set(canonical_topic_coverage(
                run, matrix, matrix["canonical_scoring_records"])["expected"]))
            self.assertNotIn(_identity(record), expected)


class G2SourceSemanticsSurviveTheMatrix(unittest.TestCase):
    """§Blocker 1 后半：`rubric_lineage.source_chapter/source_locator` 不得在下游变 null。"""

    def test_g2_matrix_rows_carry_their_own_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            _run, matrix, pack = _formal_run(Path(tmp), CHAIN_ITEMS)
        rows = _matrix_rows(matrix)
        self.assertTrue(rows)
        self.assertTrue(all(row.get("source") for row in rows),
                        "矩阵行没带来源，下游任何重算都只能把章节/定位读成 null")

    def test_g2b_lineage_chapter_and_locator_survive_authority_recompute(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, matrix, pack = _formal_run(Path(tmp), CHAIN_ITEMS)
            coverage = canonical_topic_coverage(run, matrix,
                                                matrix["canonical_scoring_records"])
        lost = []
        for record in matrix["canonical_scoring_records"]:
            lineage = record.get("rubric_lineage") or {}
            if not (lineage.get("source_chapter") and lineage.get("source_locator")):
                lost.append((str(record.get("canonical_scoring_id")),
                             {key: lineage.get(key) for key in
                              ("source_chapter", "source_locator", "source_table_id",
                               "anchor_basis")}))
        self.assertEqual([], lost, "来源语义在矩阵→Authority 这一跳变 null")
        self.assertEqual(_planning_universe(matrix), set(coverage["expected"]))
        self.assertEqual(0, coverage["planning_consistency"]["lineage_source_fields_null"])

    def test_g2c_derived_leaf_row_source_is_graph_located_and_named(self):
        """漏抽叶子没有抽取行可抄：它的来源只能由**图**给，且要具名，不冒充抽取记录。"""
        with tempfile.TemporaryDirectory() as tmp:
            _run, matrix, pack = _formal_run(Path(tmp), CHAIN_ITEMS)
        leaves = {str(row.get("leaf_id")): row for row in pack["scoring_graph"].get("leaves") or []}
        gap_rows = [row for row in _matrix_rows(matrix)
                    if str(row.get("scoring_item_id") or "").startswith("LEAF-")]
        self.assertTrue(gap_rows, "夹具没产生漏抽叶子行")
        for row in gap_rows:
            source = row.get("source") or {}
            leaf = leaves.get(str(row.get("scoring_leaf_id"))) or {}
            self.assertEqual("SCORING_TABLE_LEAF_LOCATOR", source.get("source_derivation"))
            self.assertEqual(str((leaf.get("locator") or {}).get("table_id")),
                             str(source.get("table_id")))
            self.assertEqual(RUBRIC_CHAPTER, source.get("source_section"))
            self.assertTrue(source.get("source_paragraph_or_table"))
            lineage = row.get("rubric_lineage") or {}
            self.assertEqual(ANCHOR_LEAF_CELL, lineage.get("anchor_basis"))
            self.assertTrue(lineage.get("source_chapter"))
            self.assertTrue(lineage.get("source_locator"))

    def test_g2d_paragraph_rows_keep_their_own_chapter(self):
        """非表格行（合同章 / 资信章 / 口径段）的来源也要随行落盘：那是它们被降类的**证据**。"""
        with tempfile.TemporaryDirectory() as tmp:
            _run, matrix, pack = _formal_run(Path(tmp), CHAIN_ITEMS)
        for clue, chapter in (("contract", CONTRACT_CHAPTER),
                              ("credential", CREDENTIAL_CHAPTER),
                              ("global", RUBRIC_CHAPTER)):
            row = _row(matrix, CLUES[clue])
            self.assertEqual(chapter, str((row.get("source") or {}).get("source_section")),
                             f"{clue} 行的来源章节在矩阵里丢了")
            self.assertEqual(chapter, (row.get("rubric_lineage") or {}).get("source_chapter"))
            self.assertIsNone(row.get("scoring_leaf_id"), f"{clue} 行不该落到任何评分叶子")


class G3OutOfDenominatorStaysOut(unittest.TestCase):
    """§Authority 必须连**响应语义**一起消费：规划链判成"不由技术正文回答"的主题不得被捞回。"""

    def test_g3_demoted_topics_are_out_of_the_respondable_set_in_both_layers(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, matrix, pack = _formal_run(Path(tmp), CHAIN_ITEMS)
            reading = canonical_scoring_authority(run)
        records = matrix["canonical_scoring_records"]
        planning_respondable = {_identity(row) for row in records
                                if record_requires_body_response(row)}
        self.assertTrue(planning_respondable)
        self.assertEqual(len(planning_respondable), reading["topic_respondable_total"],
                         "占技术正文覆盖率的主题数两层不一致")
        self.assertEqual(
            round(sum(float(row.get("weight_if_known") or 0.0) for row in records
                      if record_requires_body_response(row)), 2),
            reading["topic_respondable_weight_points"],
            "分值的两层读数不一致（条数尺与分值尺必须同时闭合）")
        for clue in ("contract", "credential", "global"):
            row = _row(matrix, CLUES[clue])
            self.assertFalse(row.get("counts_toward_technical_fulfillment"),
                             f"{clue} 被当成技术正文欠账：{row.get('scoring_class')}/"
                             f"{row.get('responsiveness_class')}")
            self.assertNotIn(_identity(row), planning_respondable)
            # 宇宙里有这条身份（它是评分表上真实存在的一项），但它不占覆盖率——
            # "在宇宙里"与"欠一篇正文"是两件事，Authority 不许把后者从前者的名字里推出来。
            self.assertIn(str(row.get("responsiveness_class")),
                          reading["topic_universe_composition"])

    def test_g3b_technical_topics_that_count_are_in_both_layers(self):
        """反方向不许误杀：真有表格血缘的技术事项两层都算它占覆盖率。"""
        with tempfile.TemporaryDirectory() as tmp:
            run, matrix, pack = _formal_run(Path(tmp), CHAIN_ITEMS)
            reading = canonical_scoring_authority(run)
            coverage = canonical_topic_coverage(run, matrix,
                                                matrix["canonical_scoring_records"])
        duty = _row(matrix, CLUES["duty"])
        self.assertEqual(ANCHOR_LEAF_CELL, (duty.get("rubric_lineage") or {})["anchor_basis"])
        self.assertTrue(duty.get("counts_toward_technical_fulfillment"))
        self.assertIn(_identity(duty), set(coverage["expected"]))
        counted = {name for name, cell in reading["topic_universe_composition"].items()
                   if cell["counts_toward_technical_fulfillment"]}
        from planning.scoring_responsiveness import TECHNICAL_ACCEPTANCE_DIMENSIONS
        self.assertEqual(set(TECHNICAL_ACCEPTANCE_DIMENSIONS), counted,
                         '技术验收含全篇 QA；正文 CU 义务仍只保留原来的两类')
        self.assertEqual(len(FULFILLMENT_REQUIRED_DIMENSIONS), 2)

    def test_g3c_no_dimension_outside_the_canonical_vocabulary(self):
        """组成里只准出现那六类响应维度，第二类尺不许在 Authority 里另立词表。"""
        from planning.scoring_responsiveness import RESPONSE_DIMENSIONS
        with tempfile.TemporaryDirectory() as tmp:
            run, matrix, pack = _formal_run(Path(tmp), CHAIN_ITEMS)
            reading = canonical_scoring_authority(run)
        self.assertTrue(set(reading["topic_universe_composition"]) <= set(RESPONSE_DIMENSIONS))
        self.assertEqual(sum(cell["topics"] for cell in
                             reading["topic_universe_composition"].values()),
                         reading["topic_expected_count"])


class G4ShrinkingTheRecordsCannotShrinkTheUniverse(unittest.TestCase):
    """§不得删除 identity/completeness gate：砍记录不许把分母一起砍掉。"""

    def test_g4_dropped_record_is_still_demanded_by_the_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, matrix, pack = _formal_run(Path(tmp), CHAIN_ITEMS)
            records = list(matrix["canonical_scoring_records"])
            self.assertGreater(len(records), 2)
            kept, dropped = records[:-1], records[-1]
            matrix["canonical_scoring_records"] = kept
            _gate_self_consistent(matrix, kept)
            _write(run / CANONICAL_MATRIX_FILE, matrix)
            reading = canonical_scoring_authority(run)
        self.assertFalse(reading["may_enter_generation"])
        self.assertEqual("SCORING_MAPPING_TOPIC_UNIVERSE_PLANNING_MISMATCH", reading["status"],
                         "删记录把宇宙一起缩掉了：逐身份对账没起作用")
        self.assertEqual("SCORING_AUTHORITY_TOPIC_UNIVERSE_PLANNING_MISMATCH",
                         SCORING_AUTHORITY_CLEAN_BLOCKERS[reading["status"]])
        self.assertIn(_identity(dropped),
                      reading["topic_universe_planning_consistency"]["only_in_authority"])
        self.assertGreater(reading["topic_expected_count"], len(kept),
                           "期望宇宙跟着记录条数缩了")


class G5RowsAndRecordsMustRecogniseEachOther(unittest.TestCase):
    """§同一张图下两份身份集对不上 = 矩阵自己互不认账，具名挡下而不是挑一份用。"""

    def test_g5_record_no_row_of_its_own_matrix_produces_is_named(self):
        foreign_topic = "校园机动车停车场收费口径说明"
        with tempfile.TemporaryDirectory() as tmp:
            run, matrix, pack = _formal_run(Path(tmp), CHAIN_ITEMS)
            head = matrix["canonical_scoring_records"][0]
            foreign = {"canonical_scoring_id": "SCN-999", "source_ids": ["SCR-999"],
                       "topic": foreign_topic, "display_topic": foreign_topic,
                       "label": foreign_topic, "topic_key": topic_key(foreign_topic),
                       "scoring_class": DENOMINATOR_SCORING_CLASS,
                       "responsiveness_class": head.get("responsiveness_class"),
                       "counts_toward_technical_fulfillment": True,
                       "weight_if_known": 2.0, "weight_source": "fixture",
                       "requirement_refs": [],
                       "planned_response_section": head.get("planned_response_section"),
                       "planned_response_sections": [], "planned_response_cu": [],
                       "cu_binding": {"basis": "EXPLICIT_SCORING_ID"},
                       "evidence_contract": {"concepts": ["口径"], "min_concepts": 1},
                       "dedup": {"rows_in_topic": 1, "merge_basis": "SINGLE_ROW"},
                       "identity_basis": "SCORING_TABLE_CELL"}
            records = list(matrix["canonical_scoring_records"]) + [foreign]
            matrix["canonical_scoring_records"] = records
            _gate_self_consistent(matrix, records)
            _write(run / CANONICAL_MATRIX_FILE, matrix)
            reading = canonical_scoring_authority(run)
        self.assertFalse(reading["may_enter_generation"])
        consistency = reading["topic_universe_planning_consistency"]
        self.assertTrue(consistency["applies"])
        self.assertFalse(consistency["identities_equal"])
        self.assertIn(foreign["topic_key"], consistency["only_in_planning"])
        self.assertEqual("SCORING_MAPPING_TOPIC_UNIVERSE_PLANNING_MISMATCH", reading["status"])
        # 原因要说清是**哪一边**多出来：只报个数就看不出谁在撒谎。
        self.assertTrue(consistency["only_in_planning"] and not consistency["only_in_authority"])


class G6NoGraphFallbackStaysVisible(unittest.TestCase):
    """不带图时按文本折叠是**合法退路**，但必须具名，不得冒充带图读数。"""

    def test_g6_pack_without_scoring_graph_reports_the_text_ruler(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, matrix, pack = _formal_run(Path(tmp), CHAIN_ITEMS)
            _write(run / CANONICAL_EXTRACTION_FILES[0],
                   {key: value for key, value in pack.items() if key != "scoring_graph"})
            reading = canonical_scoring_authority(run)
        self.assertEqual("ROW_TEXT_SIMILARITY_NO_SCORING_GRAPH", reading["topic_universe_ruler"])
        self.assertEqual("ABSENT_FROM_PACK", reading["topic_scoring_graph_state"])
        consistency = reading["topic_universe_planning_consistency"]
        self.assertFalse(consistency["applies"])
        self.assertEqual("NO_SCORING_GRAPH_IN_CANONICAL_INPUTS", consistency["reason"])

    def test_g6b_not_derivable_graph_is_reported_as_such(self):
        """表都在但结构建不出来：图状态要写成 NOT_DERIVABLE，不能留空当"核过了"。"""
        with tempfile.TemporaryDirectory() as tmp:
            run, matrix, pack = _formal_run(Path(tmp), CHAIN_ITEMS, _extraction([C3]))
            self.assertEqual("NOT_DERIVABLE", pack["scoring_graph"].get("status"))
            reading = canonical_scoring_authority(run)
        self.assertEqual("NOT_DERIVABLE", reading["topic_scoring_graph_state"])
        self.assertEqual("ROW_TEXT_SIMILARITY_NO_SCORING_GRAPH", reading["topic_universe_ruler"])
        self.assertFalse(reading["topic_universe_planning_consistency"]["applies"])

    def test_g6c_unreadable_pack_names_the_gap_instead_of_claiming_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, matrix, pack = _formal_run(Path(tmp), CHAIN_ITEMS)
            (run / CANONICAL_EXTRACTION_FILES[0]).write_text("{", encoding="utf-8")
            reading = canonical_scoring_authority(run)
        self.assertTrue(reading["topic_scoring_graph_state"].startswith("PACK_"))
        self.assertIn("CANONICAL_EXTRACTION_UNREADABLE", reading["canonical_topic_gaps"])


class G7StructureSentinels(unittest.TestCase):
    """§11/§9：期望宇宙只有一处产地；Authority 不带第二套分类，也不写死句子。"""

    AUTHORITY = APP_ROOT / "app" / "longform" / "scoring_authority.py"

    def test_g7_every_taxonomy_call_in_the_authority_carries_a_graph(self):
        tree = ast.parse(self.AUTHORITY.read_text(encoding="utf-8-sig"))
        bare = [node.lineno for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "build_scoring_taxonomy"
                and len(node.args) < 3
                and "graph" not in {str(key.arg) for key in node.keywords}]
        self.assertEqual([], bare,
                         "又出现不带评分表结构图的分类器调用（期望宇宙的第二处产地）")

    def test_g7b_the_gate_is_still_there(self):
        """不得删门：对账的每个格子都必须在读数里，缺一格就核不了。"""
        with tempfile.TemporaryDirectory() as tmp:
            run, matrix, pack = _formal_run(Path(tmp), CHAIN_ITEMS)
            reading = canonical_scoring_authority(run)
        for key in ("applies", "reason", "planning_identity_total", "authority_identity_total",
                    "identities_equal", "only_in_authority", "only_in_planning",
                    "lineage_source_fields_null", "record_total"):
            self.assertIn(key, reading["topic_universe_planning_consistency"])
        self.assertIn("SCORING_MAPPING_TOPIC_UNIVERSE_PLANNING_MISMATCH",
                      SCORING_AUTHORITY_CLEAN_BLOCKERS)

    def test_g7c_no_reviewer_case_text_baked_into_the_authority(self):
        """复核方冻结文案不进产品码：Run F 那几条幽灵主题的原文一格都不许出现。"""
        text = self.AUTHORITY.read_text(encoding="utf-8-sig")
        for fragment in ("一般项目经验", "体系认证证书", "横向比较分档", "评标基准价",
                         "同一建筑", "单一地块"):
            self.assertNotIn(fragment, text,
                             f"宇宙一致性被写成了针对具体句子的特判：{fragment}")

    def test_g7d_no_topic_total_is_hardcoded_in_the_authority(self):
        """分母是**算**出来的：Authority 里不许出现任何"主题条数"字面量。"""
        tree = ast.parse(self.AUTHORITY.read_text(encoding="utf-8-sig"))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            left, right = node.left, node.comparators[0]
            for side, other in ((left, right), (right, left)):
                if (isinstance(side, ast.Constant) and isinstance(side.value, int)
                        and 1 < side.value < 100
                        and "topic" in ast.dump(other).lower()):
                    offenders.append(f"{node.lineno}:{side.value}")
        self.assertEqual([], offenders, "主题条数被写成字面量：那是硬编码的期望宇宙")


if __name__ == "__main__":
    unittest.main()
