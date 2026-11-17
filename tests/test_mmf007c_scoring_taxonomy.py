"""MMF-007C Round 4 · P0-B 技术评分十类分类与唯一实质主题分母（§24–§37）。

全部离线：Synthetic Fixture + 纯规划函数 + 只读 tmp 目录里的矩阵产物，不发起任何
Provider 调用（§1）。分母从"评分表行数"换成"唯一技术实质评分主题"是本模块的核心断言：
Run D 实测 79 行里约 63 行是表头/档次/重复项，按行判覆盖率永远追不完。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
APP = APP_ROOT / "app"
for _path in (str(APP), str(APP_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from longform.run_state import collect_facts, derive_run_state
from planning.hierarchy import update_scoring_fulfillment
from planning.scoring_taxonomy import (
    COVERAGE_DENOMINATOR,
    COVERAGE_DENOMINATOR_LEGACY_LABEL,
    DENOMINATOR_SCORING_CLASS,
    EXCLUDED_SCORING_DISPOSITION,
    SCORING_CLASS_DISPOSITION,
    SCORING_CLASSES,
    SCORING_MAPPING_GATE_FILE,
    SCORING_STATUSES,
    TOPIC_MERGE_SIMILARITY,
    bind_planned_response_cu,
    build_scoring_taxonomy,
    canonical_status,
    classify_scoring_row,
    classify_scoring_row_with_source,
    display_topic,
    evidence_contract_for,
    is_non_response_row,
    pre_generation_scoring_mapping_gate,
    scoring_class_disposition,
    scoring_classes,
    scoring_statuses,
    strip_scoring_noise,
    topic_key,
    topic_similarity,
    unit_matches,
)

from test_mmf007c_reduction_executor import _delivery_run, _run_dir

# 一段够长、要素齐、能定位的响应：与 test_mmf007_scoring_deep 同口径的实体证据正文。
_RESPONSE = (
    "本节把投诉处理闭环机制拆成受理、判定、分派、处置、回访五个环节，逐环节写明时限、责任岗位与记录字段。"
    "受理由客服前台在投诉台账登记投诉对象、位置、诉求与联系人，两分钟内转达当班领班；"
    "判定按影响程度分级，影响运行的立即启动处置并同步甲方接口人；"
    "分派明确到岗位与人，写清可用资源与完成标准；"
    "处置记录开始与结束时刻、参与人员、临时措施与恢复条件；"
    "处置完成后二十四小时内由客户服务岗位发起回访闭环，记录客户评价与未解决问题，"
    "未解决问题继续挂在台账直至销项，回访记录每周汇总一次，作为服务质量分析的固定输入。"
    "台账字段包含编号、日期、来源、类别、责任人、完成时点、回访结果与销项标记。"
    "上述时限与岗位每周抽查验证，抽查记录与台账逐条对应；未按期销项的投诉升级至项目经理，"
    "在月度服务报告中写明原因、补做安排与完成时点。回访闭环的抽样比例不低于三成，"
    "重点回访重复投诉与超时投诉，回访发现的衍生问题重新进入受理环节。"
)


def _row(scoring_item_id: str, label: str, **extra) -> dict:
    row = {
        "scoring_item_id": scoring_item_id,
        "label": label,
        "must_respond": True,
        "requirement_ids": ["REQ-0001"],
        "primary_section_id": "S04-02",
        "section_ids": ["S04-02"],
        "response_status": "PLANNED",
        "planned_response_cu": [],
        "planned_evidence": {},
        "entity_contract": evidence_contract_for(label),
        "fulfillment": {},
    }
    row.update(extra)
    return row


def _unit(content_unit_id: str, section_id: str = "S04-02", **extra) -> dict:
    unit = {
        "content_unit_id": content_unit_id,
        "section_id": section_id,
        "title": "投诉处理闭环机制",
        "scoring_item_ids": [],
        "requirement_ids": [],
    }
    unit.update(extra)
    return unit


def _matrix_file(tmp: str, payload: dict) -> Path:
    path = Path(tmp) / "09_scoring_matrix.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class TaxonomyVocabulary(unittest.TestCase):
    """§24/§26：十类与五状态是词汇表，任何调用方都不得自己数或自己改。"""

    def test_ten_classes_and_five_statuses_are_published(self) -> None:
        self.assertEqual(len(SCORING_CLASSES), 10)
        self.assertEqual(scoring_classes(), SCORING_CLASSES)
        self.assertEqual(SCORING_STATUSES, ("FULFILLED", "PARTIAL", "MISSING", "NEARBY_ONLY", "NOT_APPLICABLE"))
        self.assertEqual(scoring_statuses(), SCORING_STATUSES)
        self.assertEqual(set(SCORING_CLASS_DISPOSITION), set(SCORING_CLASSES))
        self.assertEqual(len(SCORING_CLASSES), len(build_scoring_taxonomy([])["allowed_scoring_classes"]))

    def test_only_one_class_enters_the_denominator(self) -> None:
        denominator = {name for name, disposition in SCORING_CLASS_DISPOSITION.items()
                       if disposition == "TECHNICAL_DENOMINATOR"}
        self.assertEqual(denominator, {DENOMINATOR_SCORING_CLASS})
        # MMF008A-A：分母口径的字面量随第二维改口。旧字符串只作对照，不再授权判定；
        # 两句话都要钉住，否则"换尺子不换名字"（旧读数被新链路当成按新尺子量的）与
        # "改名把历史读数判成没量"这两种错向都会悄悄发生。
        self.assertEqual(COVERAGE_DENOMINATOR, "UNIQUE_TECHNICAL_RESPONDABLE_TOPICS")
        self.assertNotEqual(COVERAGE_DENOMINATOR, COVERAGE_DENOMINATOR_LEGACY_LABEL)
        self.assertEqual(COVERAGE_DENOMINATOR_LEGACY_LABEL, "UNIQUE_TECHNICAL_SUBSTANTIVE_TOPICS")
        self.assertEqual(set(SCORING_CLASS_DISPOSITION) - {DENOMINATOR_SCORING_CLASS},
                         set(SCORING_CLASSES) - {DENOMINATOR_SCORING_CLASS})

    def test_gate_file_name_is_a_single_constant(self) -> None:
        self.assertEqual(SCORING_MAPPING_GATE_FILE, "pre_generation_scoring_mapping_gate.json")


class RowClassification(unittest.TestCase):
    """§25：表头/档次行、非技术类条目一律离开分母；范围判定仍以范围门为准。"""

    def test_header_and_formula_rows_are_non_response(self) -> None:
        self.assertEqual(is_non_response_row("技术评分（30分）"), "SCORING_HEADER")
        self.assertEqual(is_non_response_row("档次：优（10分）"), "SCORING_FORMULA")
        self.assertEqual(classify_scoring_row({"label": "技术评分（30分）"}), "SCORING_HEADER")
        self.assertEqual(scoring_class_disposition("SCORING_FORMULA"), "NON_RESPONSE_ROW")

    def test_total_footer_leaf_does_not_inherit_the_table_class(self) -> None:
        # 银丰这一轮：评分表最后一行「合计」没被抽成事项，同表兄弟全是技术实质。
        # 卷次共识不能把表尾继承成可响应主题，否则证据合同为空，生成前门直接停。
        graph = {
            "status": "DERIVED",
            "leaves": [
                {"leaf_id": "TAB-1-R01", "topic": "秩序维护方案", "leaf_text": "秩序维护方案",
                 "weight": 8, "volume": "技术", "locator": {"table_id": "TAB-1"}},
                {"leaf_id": "TAB-1-R02", "topic": "合计", "leaf_text": "合计",
                 "weight": 8, "volume": "技术", "locator": {"table_id": "TAB-1"}},
            ],
        }
        row = _row("SCR-001", "秩序维护方案（8分）")
        row["scoring_leaf_id"] = "TAB-1-R01"
        tree = build_scoring_taxonomy([row], None, graph)
        topics = [str(item.get("topic") or "") for item in tree["canonical_scoring_records"]]
        self.assertTrue(any("秩序维护" in topic for topic in topics))
        self.assertNotIn("合计", topics)
        headers = [item for item in tree["rows"] if item.get("label") == "合计"]
        self.assertEqual([item.get("scoring_class") for item in headers], ["SCORING_HEADER"])

    def test_stored_total_row_does_not_block_the_mapping_gate(self) -> None:
        real = {
            "canonical_scoring_id": "SCN-001",
            "topic": "秩序维护方案",
            "label": "秩序维护方案",
            "scoring_class": "TECHNICAL_SUBSTANTIVE",
            "responsiveness_class": "TECHNICAL_BODY_DIRECT",
            "planned_response_section": "S04-02",
            "planned_response_sections": ["S04-02"],
            "planned_response_cu": ["CU-1"],
            "evidence_contract": {"concepts": ["巡逻"], "min_concepts": 1},
        }
        footer = {
            "canonical_scoring_id": "SCN-010",
            "topic": "合计",
            "label": "合计",
            "display_topic": "合计",
            "scoring_class": "TECHNICAL_SUBSTANTIVE",
            "responsiveness_class": "TECHNICAL_BODY_DIRECT",
            "mapping_basis": "DERIVED_TECHNICAL_RESPONSE_CU",
            "planned_response_section": "S01-01",
            "planned_response_sections": ["S01-01"],
            "planned_response_cu": ["CU-S01-01-07"],
            "evidence_contract": {"concepts": [], "min_concepts": 1},
        }
        gate = pre_generation_scoring_mapping_gate(
            {"canonical_scoring_records": [real, footer]},
            planned_unit_ids=["CU-1", "CU-S01-01-07"],
            planned_section_ids=["S04-02", "S01-01"])
        self.assertTrue(gate["may_enter_generation"], gate.get("blocked_reasons"))
        self.assertEqual(gate["unmapped_total"], 0)
        self.assertEqual(gate["respondable_topic_total"], 1)

    def test_scope_gate_classes_map_one_to_one(self) -> None:
        cases = {
            "TECHNICAL_INCLUDED": "TECHNICAL_SUBSTANTIVE",
            "TECHNICAL_REFERENCE": "TECHNICAL_CONTEXTUAL",
            "COMMERCIAL_EXCLUDED": "COMMERCIAL_TERMS",
            "PRICING_EXCLUDED": "PRICING",
            "QUALIFICATION_EXCLUDED": "QUALIFICATION_CREDENTIAL",
            "BIDDER_CREDENTIAL_EXCLUDED": "BIDDER_CREDENTIAL",
            "LEGAL_PROCEDURAL_EXCLUDED": "PROCEDURAL",
        }
        for scope_class, expected in cases.items():
            name, source = classify_scoring_row_with_source(
                {"label": "投诉处理闭环机制（10分）"}, scope_class=scope_class)
            self.assertEqual(name, expected, scope_class)
            self.assertEqual(source, "TECHNICAL_ONLY_SCOPE_GATE")
        self.assertEqual(scoring_class_disposition("PRICING"), EXCLUDED_SCORING_DISPOSITION)

    def test_absent_scope_gate_assumes_technical_instead_of_dropping_row(self) -> None:
        # 门禁缺席时把行留在分母（旧口径），否则"没跑范围门"就会伪装成"没有技术评分项"。
        name, source = classify_scoring_row_with_source({"label": "投诉处理闭环机制（10分）"})
        self.assertEqual(name, "TECHNICAL_SUBSTANTIVE")
        self.assertEqual(source, "SCOPE_GATE_ABSENT_ASSUMED_TECHNICAL")

    def test_non_response_shape_wins_over_scope_mapping(self) -> None:
        # 范围门常把"技术评分（30分）"当成 TECHNICAL_INCLUDED：形状先判，表头不能成为分母项。
        name, source = classify_scoring_row_with_source(
            {"label": "技术评分（30分）"}, scope_class="TECHNICAL_INCLUDED")
        self.assertEqual((name, source), ("SCORING_HEADER", "NON_RESPONSE_ROW_SHAPE"))

    def test_topic_stripping_removes_score_and_enum_noise(self) -> None:
        self.assertEqual(topic_key("一、投诉处理闭环机制（10分）"), "投诉处理闭环机制")
        self.assertEqual(strip_scoring_noise("【待投标人确认】消防维护方案（8分）"), "消防维护方案")
        self.assertLessEqual(len(display_topic("投诉处理闭环机制（10分）" * 40)), 60)


class TopicDedup(unittest.TestCase):
    """§31：按唯一实质主题去重，重复行的分值取最大值而非求和。"""

    def test_duplicate_topic_rows_collapse_into_one_canonical_record(self) -> None:
        rows = [_row("SCR-001", "投诉处理闭环机制（10分）", max_score=10),
                _row("SCR-002", "投诉处理闭环机制（5分）", max_score=5)]
        tree = build_scoring_taxonomy(rows)
        self.assertEqual(len(tree["canonical_scoring_records"]), 1)
        record = tree["canonical_scoring_records"][0]
        self.assertEqual(record["source_ids"], ["SCR-001", "SCR-002"])
        self.assertEqual(record["weight_if_known"], 10.0, "重复事项的分值不得相加")
        self.assertEqual(record["dedup"]["merge_basis"], "UNIQUE_SUBSTANTIVE_TOPIC")
        self.assertEqual(record["dedup"]["duplicate_source_ids"], ["SCR-002"])
        by_id = {row["scoring_item_id"]: row for row in tree["rows"]}
        self.assertEqual(by_id["SCR-002"]["scoring_class_after_dedup"], "DUPLICATE_TOPIC")
        self.assertEqual(by_id["SCR-002"]["duplicate_of"], "SCR-001")
        self.assertGreaterEqual(by_id["SCR-002"]["duplicate_similarity"], TOPIC_MERGE_SIMILARITY)
        self.assertEqual(tree["denominator"]["raw_substantive_rows"], 2)
        self.assertEqual(tree["denominator"]["merged_duplicate_rows"], 1)
        self.assertEqual(tree["denominator"]["canonical_total"], 1)

    def test_distinct_topics_stay_distinct(self) -> None:
        rows = [_row("SCR-001", "投诉处理闭环机制（10分）"), _row("SCR-002", "绿化养护季节化方案（10分）")]
        tree = build_scoring_taxonomy(rows)
        self.assertEqual(len(tree["canonical_scoring_records"]), 2)
        self.assertEqual([r["duplicate_of"] for r in tree["rows"]], [None, None])
        self.assertLess(topic_similarity(topic_key("投诉处理闭环机制"), topic_key("绿化养护季节化方案")),
                        TOPIC_MERGE_SIMILARITY)

    def test_short_topic_keys_are_never_merged(self) -> None:
        # 两字主题互相包含度高，但并掉就等于用分类把分母缩小 —— 宁可留两项。
        rows = [_row("SCR-001", "消防（3分）"), _row("SCR-002", "消防（2分）")]
        tree = build_scoring_taxonomy(rows)
        self.assertEqual(len(tree["canonical_scoring_records"]), 2)

    def test_non_technical_rows_never_become_denominator_items(self) -> None:
        rows = [_row("SCR-001", "投诉处理闭环机制（10分）", scope_class="TECHNICAL_INCLUDED"),
                _row("SCR-002", "投标报价（15分）", scope_class="PRICING_EXCLUDED"),
                _row("SCR-003", "类似项目业绩（5分）", scope_class="BIDDER_CREDENTIAL_EXCLUDED"),
                _row("SCR-004", "技术评分（30分）", scope_class="TECHNICAL_INCLUDED")]
        tree = build_scoring_taxonomy(rows)
        self.assertEqual([record["source_ids"] for record in tree["canonical_scoring_records"]], [["SCR-001"]])
        self.assertEqual(tree["class_counts"]["PRICING"], 1)
        self.assertEqual(tree["class_counts"]["BIDDER_CREDENTIAL"], 1)
        self.assertEqual(tree["class_counts"]["SCORING_HEADER"], 1)
        self.assertEqual(tree["denominator"]["rows_not_in_denominator"], 3)


class CanonicalRecords(unittest.TestCase):
    """§32：canonical 记录的字段就是后续门与复核的接口，缺一个就有人在空中楼阁。"""

    def test_record_carries_every_contracted_field(self) -> None:
        tree = build_scoring_taxonomy([_row("SCR-001", "投诉处理闭环机制（10分）", max_score=10)])
        record = tree["canonical_scoring_records"][0]
        for field in ("canonical_scoring_id", "source_ids", "topic", "weight_if_known", "requirement_refs",
                      "planned_response_section", "planned_response_cu", "evidence_contract"):
            self.assertIn(field, record)
        self.assertEqual(record["canonical_scoring_id"], "SCN-001")
        self.assertEqual(record["requirement_refs"], ["REQ-0001"])
        self.assertEqual(record["planned_response_section"], "S04-02")
        self.assertEqual(record["scoring_class"], DENOMINATOR_SCORING_CLASS)
        self.assertTrue(record["evidence_contract"]["concepts"])
        self.assertGreaterEqual(record["evidence_contract"]["min_concepts"], 1)
        self.assertTrue(record["evidence_contract"]["forbidden_shortcuts"])

    def test_rows_point_back_at_their_canonical_record(self) -> None:
        tree = build_scoring_taxonomy([_row("SCR-001", "投诉处理闭环机制（10分）")])
        by_id = {row["scoring_item_id"]: row for row in tree["rows"]}
        self.assertEqual(by_id["SCR-001"]["canonical_scoring_id"], "SCN-001")


class CuBinding(unittest.TestCase):
    """§32/§33：planned_response_cu 必须真绑生成计划，绑定依据要写下来。"""

    def _bind(self, units, sections=("S04-02",), refs=("REQ-0001",), ids=("SCR-001",)):
        index: dict[str, list[dict]] = {}
        for unit in units:
            index.setdefault(str(unit["section_id"]), []).append(unit)
        return bind_planned_response_cu(list(ids), list(refs), list(sections),
                                        topic_key("投诉处理闭环机制"), index)

    def test_explicit_scoring_ids_win_over_weaker_bases(self) -> None:
        units = [_unit("CU-A", scoring_item_ids=["SCR-001"]), _unit("CU-B", requirement_ids=["REQ-0001"])]
        cus, binding = self._bind(units)
        self.assertEqual(cus, ["CU-A"])
        self.assertEqual(binding["basis"], "EXPLICIT_SCORING_ITEM_IDS")
        self.assertEqual(binding["requirement_ref_units"], ["CU-B"], "弱口径也要留痕，不得只报最好的那条")

    def test_requirement_refs_beat_topic_match(self) -> None:
        units = [_unit("CU-A"), _unit("CU-B", requirement_ids=["REQ-0001"])]
        cus, binding = self._bind(units)
        self.assertEqual(cus, ["CU-B"])
        self.assertEqual(binding["basis"], "REQUIREMENT_REF_LINK")

    def test_topic_match_used_only_as_last_resort(self) -> None:
        cus, binding = self._bind([_unit("CU-A", requirement_ids=[], scoring_item_ids=[])])
        self.assertEqual(cus, ["CU-A"])
        self.assertEqual(binding["basis"], "TOPIC_MATCH_IN_PLANNED_SECTION")
        self.assertTrue(unit_matches(_unit("CU-A"), "投诉处理闭环机制"))

    def test_units_outside_planned_sections_are_not_bound(self) -> None:
        cus, binding = self._bind([_unit("CU-A", section_id="S09-01")])
        self.assertEqual(cus, [])
        self.assertEqual(binding["basis"], "UNBOUND")
        self.assertEqual(binding["units_in_planned_sections"], [])

    def test_taxonomy_binds_units_from_the_plan_tree(self) -> None:
        tree = build_scoring_taxonomy([_row("SCR-001", "投诉处理闭环机制（10分）")],
                                      [_unit("CU-A", scoring_item_ids=["SCR-001"])])
        record = tree["canonical_scoring_records"][0]
        self.assertEqual(record["planned_response_cu"], ["CU-A"])
        self.assertEqual(record["cu_binding"]["basis"], "EXPLICIT_SCORING_ITEM_IDS")

    def test_missing_plan_tree_is_reported_as_row_fallback(self) -> None:
        # 没有计划树时退回行自带 CU，并把口径写明白：不冒充"真绑了生成计划"。
        rows = [_row("SCR-001", "投诉处理闭环机制（10分）", planned_response_cu=["CU-X"])]
        tree = build_scoring_taxonomy(rows)
        record = tree["canonical_scoring_records"][0]
        self.assertEqual(record["planned_response_cu"], ["CU-X"])
        self.assertEqual(record["cu_binding"]["basis"], "ROW_PLANNED_CU_FALLBACK")


class EvidenceContract(unittest.TestCase):
    """§32：规划期写的证据合同与生成后验收共用同一个分词器。"""

    def test_contract_lists_concepts_and_forbids_the_shortcuts(self) -> None:
        contract = evidence_contract_for("投诉处理机制及回访闭环（10分）")
        self.assertGreaterEqual(len(contract["concepts"]), 1)
        self.assertGreaterEqual(contract["min_concepts"], 1)
        self.assertLessEqual(contract["min_concepts"], len(contract["concepts"]))
        self.assertIn("COUNT_NEARBY_TEXT_AS_FULFILLED", contract["forbidden_shortcuts"])
        self.assertIn("ADD_COMMERCIAL_CONTENT_FOR_COVERAGE", contract["forbidden_shortcuts"])
        self.assertIn("ADD_BLANK_ATTACHMENT_PAGE_FOR_COVERAGE", contract["forbidden_shortcuts"])
        self.assertEqual(contract["concept_source"], "SCORING_LABEL_TOKENS")
        self.assertEqual(sorted(contract["concepts"]), sorted(contract["concept_synonyms"]))

    def test_named_rubric_is_reused_not_reinvented(self) -> None:
        contract = evidence_contract_for("针对产业园特点提出针对性管理建议")
        self.assertEqual(contract["concept_source"], "NAMED_RUBRIC")
        self.assertNotEqual(contract["rubric_id"], "LABEL_DERIVED")

    def test_planned_structured_evidence_requires_a_structured_output(self) -> None:
        contract = evidence_contract_for("保洁作业标准", {"tables": ["作业频次表"]})
        self.assertTrue(contract["require_structured"])


class PreGenerationScoringMappingGate(unittest.TestCase):
    """§34：缺 planned section / CU / 证据合同不得进正式 300-Class；排除类落点即泄漏。"""

    def _gate(self, rows, units=None):
        tree = build_scoring_taxonomy(rows, units)
        return pre_generation_scoring_mapping_gate({
            "rows": tree["rows"], "canonical_scoring_records": tree["canonical_scoring_records"]}), tree

    def test_complete_mapping_enters_generation(self) -> None:
        gate, _ = self._gate([_row("SCR-001", "投诉处理闭环机制（10分）")],
                             [_unit("CU-A", scoring_item_ids=["SCR-001"])])
        self.assertEqual(gate["status"], "PASS")
        self.assertTrue(gate["may_enter_generation"])
        self.assertEqual((gate["substantive_topic_total"], gate["mapped_total"], gate["unmapped_total"]), (1, 1, 0))

    def test_unbound_topic_blocks_generation(self) -> None:
        gate, _ = self._gate([_row("SCR-001", "投诉处理闭环机制（10分）")],
                             [_unit("CU-A", section_id="S09-01")])
        self.assertEqual(gate["status"], "BLOCKED_SCORING_MAPPING_INCOMPLETE")
        self.assertFalse(gate["may_enter_generation"])
        self.assertIn("PLANNED_CONTENT_UNIT", gate["unmapped_records"][0]["missing"])
        self.assertTrue(gate["blocked_reasons"])

    def test_missing_planned_section_blocks_generation(self) -> None:
        row = _row("SCR-001", "投诉处理闭环机制（10分）")
        row.pop("primary_section_id")
        row["section_ids"] = []
        gate, _ = self._gate([row], [_unit("CU-A", scoring_item_ids=["SCR-001"])])
        self.assertIn("PLANNED_SECTION", gate["unmapped_records"][0]["missing"])
        self.assertFalse(gate["may_enter_generation"])

    def test_missing_evidence_contract_blocks_generation(self) -> None:
        row = _row("SCR-001", "投诉处理闭环机制（10分）")
        row["entity_contract"] = {"concepts": [], "min_concepts": 0}
        gate, _ = self._gate([row], [_unit("CU-A", scoring_item_ids=["SCR-001"])])
        self.assertIn("EVIDENCE_CONTRACT", gate["unmapped_records"][0]["missing"])

    def test_excluded_class_with_planned_landing_is_a_leak(self) -> None:
        rows = [_row("SCR-001", "投标报价（15分）", scope_class="PRICING_EXCLUDED",
                     planned_response_cu=["CU-B"])]
        gate, _ = self._gate(rows, [_unit("CU-B", scoring_item_ids=["SCR-001"])])
        self.assertEqual(gate["status"], "TECHNICAL_SCORING_LEAK")
        self.assertFalse(gate["may_enter_generation"])
        self.assertEqual(gate["excluded_class_planned_leaks"][0]["scoring_class"], "PRICING")
        self.assertIn("不得为提高覆盖率", gate["blocked_reasons"][0])

    def test_no_scoring_items_is_not_applicable_not_pass(self) -> None:
        gate, _ = self._gate([_row("SCR-001", "技术评分（30分）")])
        self.assertEqual(gate["status"], "NOT_APPLICABLE")
        self.assertTrue(gate["may_enter_generation"])
        self.assertEqual(gate["substantive_topic_total"], 0)

    def test_reference_class_scored_rows_are_watchlisted(self) -> None:
        # 范围门把带分值的条目移出分母 → 覆盖率会静悄悄变好：分类器不改判，但必须列出来。
        rows = [_row("SCR-001", "投诉处理闭环机制（10分）", scope_class="TECHNICAL_INCLUDED"),
                _row("SCR-002", "针对产业园业态的合理化建议（20分）", scope_class="TECHNICAL_REFERENCE",
                     max_score=20),
                _row("SCR-003", "现场踏勘安排", scope_class="TECHNICAL_REFERENCE", must_respond=True)]
        gate, _ = self._gate(rows, [_unit("CU-A", scoring_item_ids=["SCR-001"])])
        watch = gate["denominator_shrink_watch"]
        self.assertEqual([item["scoring_item_id"] for item in watch], ["SCR-002", "SCR-003"])
        self.assertEqual(watch[0]["scope_class"], "TECHNICAL_REFERENCE")
        # must_respond 在真实评分包里常整表为真，带分值的才是"丢了就丢分"的那几条 —— 分开数。
        self.assertEqual(gate["denominator_shrink_watch_scored_total"], 1)
        self.assertEqual(gate["substantive_topic_total"], 1)
        self.assertEqual(gate["rules"]["denominator_authority"], "TECHNICAL_ONLY_SCOPE_GATE")


class CanonicalStatusSynthesis(unittest.TestCase):
    """§26：一个主题的多个来源行合成一个状态，NEARBY_ONLY 不计 fulfilled。"""

    def _fulfilled(self, **extra):
        row = {"fulfillment": dict({"status": "FULFILLED", "response_section": "S04-02",
                                    "response_cu": ["CU-A"], "effective_chars": 900,
                                    "evidence_found": True, "entity_backed": True}, **extra)}
        return row

    def test_status_vocabulary_is_the_five_unified_names(self) -> None:
        for status in SCORING_STATUSES:
            verdict = canonical_status([{"fulfillment": {"status": status, "response_cu": []}}])
            self.assertIn(verdict["status"], SCORING_STATUSES)

    def test_nearby_only_never_becomes_fulfilled(self) -> None:
        verdict = canonical_status([
            {"fulfillment": {"status": "NEARBY_ONLY", "response_cu": ["CU-A"], "effective_chars": 800}},
            {"fulfillment": {"status": "MISSING", "response_cu": [], "effective_chars": 0}},
        ])
        self.assertEqual(verdict["status"], "NEARBY_ONLY")
        self.assertNotEqual(verdict["status"], "FULFILLED")

    def test_best_source_row_sets_the_topic_status(self) -> None:
        self.assertEqual(canonical_status([
            {"fulfillment": {"status": "PARTIAL"}}, self._fulfilled()])["status"], "FULFILLED")
        self.assertEqual(canonical_status([{"fulfillment": {"status": "MISSING"}},
                                           {"fulfillment": {"status": "PARTIAL"}}])["status"], "PARTIAL")

    def test_response_sections_and_cu_are_unioned(self) -> None:
        verdict = canonical_status([
            self._fulfilled(),
            {"fulfillment": {"status": "PARTIAL", "response_sections": ["S08-01"], "response_cu": ["CU-B"]}},
        ])
        self.assertEqual(verdict["response_sections"], ["S04-02", "S08-01"])
        self.assertEqual(verdict["response_cu"], ["CU-A", "CU-B"])
        self.assertEqual(verdict["counts_rows"], 2)
        self.assertTrue(verdict["entity_backed"])

    def test_no_source_rows_is_not_applicable(self) -> None:
        self.assertEqual(canonical_status([])["status"], "NOT_APPLICABLE")


class DenominatorSwitchEndToEnd(unittest.TestCase):
    """§33/§35/§36：分母换成主题口径，旧按行口径并列保留；覆盖率低不得 COMPLETED_CLEAN。"""

    def _write_matrix(self, tmp: str, rows, units=None):
        tree = build_scoring_taxonomy(rows, units)
        return _matrix_file(tmp, {
            "schema_version": "scoring-matrix-v0.3",
            "rows": tree["rows"],
            "canonical_scoring_records": tree["canonical_scoring_records"],
            "coverage_summary": {"scoring_total": len(tree["rows"])},
        }), tree

    def test_coverage_counts_canonical_topics_not_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rows = [_row("SCR-001", "投诉处理闭环机制（10分）"),
                    _row("SCR-002", "投诉处理闭环机制（5分）"),
                    _row("SCR-003", "绿化养护季节化方案（10分）")]
            path, _ = self._write_matrix(tmp, rows, [_unit("CU-A", scoring_item_ids=["SCR-001"]),
                                                     _unit("CU-B", requirement_ids=["REQ-0001"])])
            result = update_scoring_fulfillment(path, {"S04-02": {"body_blocks": [
                {"type": "paragraph", "content": _RESPONSE}], "tables": []}})
            matrix = json.loads(path.read_text(encoding="utf-8"))
            summary = matrix["coverage_summary"]
            self.assertEqual(summary["canonical_scoring_total"], 2, "两行同一主题 → 一个分母项")
            self.assertEqual(summary["canonical_deduped_source_rows"], 3)
            self.assertEqual(summary["technical_scoring_total"], 3, "旧按行口径必须并列可见")
            self.assertEqual(summary["coverage_denominator"], COVERAGE_DENOMINATOR)
            self.assertEqual(summary["row_coverage_denominator"], "TECHNICAL_SCORING_COVERAGE_ROWS")
            self.assertIsNotNone(result["technical_scoring_coverage"])
            self.assertEqual(result["canonical_denominator_basis"], "PLAN_OF_RECORD")
            self.assertLessEqual(summary["technical_scoring_coverage"], 1.0)
            gate = matrix["scoring_gate"]
            self.assertEqual(gate["coverage_denominator"], COVERAGE_DENOMINATOR)
            self.assertFalse(gate["nearby_only_counts_as_fulfilled"])
            self.assertTrue(gate["commercial_content_for_coverage_forbidden"])
            self.assertIsNotNone(gate["legacy_row_coverage"])

    def test_unfulfilled_topics_hold_coverage_below_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rows = [_row(f"SCR-{index:03d}", f"投诉处理闭环机制{index}号（10分）") for index in range(1, 6)]
            path, _ = self._write_matrix(tmp, rows, [_unit(f"CU-{index}", scoring_item_ids=[f"SCR-{index:03d}"])
                                                     for index in range(1, 6)])
            result = update_scoring_fulfillment(path, {"S04-02": {"body_blocks": [
                {"type": "paragraph", "content": _RESPONSE}], "tables": []}})
            self.assertLess(result["technical_scoring_coverage"], 0.80)
            self.assertFalse(result["completed_clean_allowed"], "§35：主题覆盖率低于 80% 不得 COMPLETED_CLEAN")

    def test_evidence_is_checked_against_planned_cu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rows = [_row("SCR-001", "投诉处理闭环机制（10分）")]
            path, _ = self._write_matrix(tmp, rows, [_unit("CU-A", scoring_item_ids=["SCR-001"])])
            update_scoring_fulfillment(path, {"S04-02": {"body_blocks": [
                {"type": "paragraph", "content": _RESPONSE}], "tables": []}})
            matrix = json.loads(path.read_text(encoding="utf-8"))
            verdict = matrix["canonical_scoring_records"][0]["fulfillment"]
            self.assertIn(verdict["status"], SCORING_STATUSES)
            self.assertEqual(verdict["cu_binding_basis"], "EXPLICIT_SCORING_ITEM_IDS")
            self.assertEqual(verdict["planned_response_cu"], ["CU-A"])
            # 证据落在计划外也要写出来，不能只报"覆盖了"。
            self.assertIsInstance(verdict["evidence_outside_plan"], list)


class FinalStateScoringGate(unittest.TestCase):
    """§36：覆盖率门是终态权威的一部分 —— 与页带门并列，写在同一处派生里。"""

    def _state(self, gate: dict | None, *, plan: dict | None = None):
        with _run_dir() as run:
            _delivery_run(run, pages=300)
            if gate is not None:
                (run / "09_scoring_matrix.json").write_text(json.dumps(gate), encoding="utf-8")
            if plan is not None:
                (run / "08_document_hierarchy.json").write_text(json.dumps(plan), encoding="utf-8")
            facts = collect_facts(run, medium="WORD", draft_delivery="COMPLETED_CLEAN")
            return facts, derive_run_state(facts)

    def test_coverage_above_gate_allows_clean(self) -> None:
        """覆盖率正例现在还要满足第 4 条必要条件（Round 4.3 §10）：映射按当前计划算得过来。

        原来这份夹具只写 `canonical_scoring_records: [{}]` —— 一条**没有落点**的假记录，
        在 §5.4「门没跑不等于门通过」之下它本来就不该拿到 Clean。这里把它换成真的合法形状
        （有主题、有在计划里的节与单元、有实体契约、有已评估的门结论），
        覆盖率那两根断言一个字不动：本轮改的是"能不能自称干净"，不是覆盖率那把尺。
        """
        record = {"canonical_scoring_id": "SCN-001", "topic": "投诉处理闭环机制",
                  "source_ids": ["SCR-001"], "planned_response_section": "S01-01",
                  "planned_response_sections": ["S01-01"],
                  "planned_response_cu": ["CU-S01-01-01"],
                  "cu_binding": {"basis": "EXPLICIT_SCORING_ID"},
                  "evidence_contract": {"concepts": ["闭环", "时限"], "min_concepts": 2}}
        facts, state = self._state({"canonical_scoring_records": [record],
                                    "rows": [{"scoring_item_id": "SCR-001",
                                              "label": "投诉处理闭环机制", "must_respond": True,
                                              "scope_class": "TECHNICAL_INCLUDED",
                                              "primary_section_id": "S01-01"}],
                                    "pre_generation_scoring_mapping_gate": {
                                        "status": "PASS", "may_enter_generation": True,
                                        "substantive_topic_total": 1, "mapped_total": 1,
                                        "unmapped_total": 0},
                                    "scoring_gate": {"measured": True,
                                                     "technical_scoring_coverage": 1.0,
                                                     "completed_clean_allowed": True,
                                                     "coverage_denominator": COVERAGE_DENOMINATOR,
                                                     "canonical_scoring_total": 1,
                                                     "min_technical_scoring_coverage": 0.8}},
                                   plan={"schema_version": "document-hierarchy-v0.1",
                                         "sections": [{"section_id": "S01-01"}],
                                         "content_units": [{"content_unit_id": "CU-S01-01-01",
                                                            "section_id": "S01-01"}]})
        self.assertEqual(facts["scoring_gate_state"], "COVERAGE_OK")
        self.assertEqual(state["delivery_status"], "COMPLETED_CLEAN")
        self.assertEqual(state["scoring_technical_coverage"], 1.0)
        self.assertEqual(state["scoring_authority_status"], "PASS")

    def test_coverage_below_gate_downgrades_to_review(self) -> None:
        facts, state = self._state({"canonical_scoring_records": [{}, {}, {}],
                                    "scoring_gate": {"measured": True, "technical_scoring_coverage": 0.33,
                                                     "completed_clean_allowed": False,
                                                     "coverage_denominator": COVERAGE_DENOMINATOR,
                                                     "canonical_scoring_total": 3}})
        self.assertEqual(facts["scoring_gate_state"], "BELOW_GATE")
        self.assertEqual(state["delivery_status"], "NEEDS_REVIEW")
        self.assertEqual(state["internal_phase"], "COMPLETED_WITH_REVIEW")

    def test_coverage_measured_on_the_wrong_ruler_is_not_a_measurement(self) -> None:
        # 按行口径的 0.33/0.99 都不是 §33 那把尺子：尺子不对，读数一律不算数。
        for coverage in (0.33, 0.99):
            facts, state = self._state({"canonical_scoring_records": [{}],
                                        "scoring_gate": {"measured": True,
                                                         "technical_scoring_coverage": coverage,
                                                         "completed_clean_allowed": coverage >= 0.8,
                                                         "coverage_denominator": "TECHNICAL_SCORING_COVERAGE",
                                                         "canonical_scoring_total": 1}})
            self.assertEqual(facts["scoring_gate_state"], "NOT_MEASURED")
            self.assertEqual(facts["scoring_coverage_denominator"], "TECHNICAL_SCORING_COVERAGE")
            self.assertEqual(state["delivery_status"], "NEEDS_REVIEW")

    def test_unmeasured_coverage_with_topics_never_passes_as_clean(self) -> None:
        # 有评分主题却没回填覆盖率：量不到不等于覆盖得好。
        facts, state = self._state({"canonical_scoring_records": [{}], "coverage_summary": {}})
        self.assertEqual(facts["scoring_gate_state"], "NOT_MEASURED")
        self.assertEqual(state["delivery_status"], "NEEDS_REVIEW")

    def test_run_without_scoring_matrix_is_not_applicable(self) -> None:
        facts, state = self._state(None)
        self.assertEqual(facts["scoring_gate_state"], "NOT_APPLICABLE")
        self.assertEqual(state["delivery_status"], "COMPLETED_CLEAN")


class GateWiringInPlanningAndGeneration(unittest.TestCase):
    """§34 的落点：规划阶段把门写在产物里，生成链只读不重算。"""

    def test_planning_stage_writes_the_mapping_gate_artifact(self) -> None:
        from planning.hierarchy import attach_hierarchy_artifacts
        from test_mmf007a_hierarchy import _bundle, _pack

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            pack = _pack()
            (run / "requirement_pack.json").write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
            written = attach_hierarchy_artifacts(run, _bundle(pack))
            self.assertIn("scoring_mapping_gate", written or {})
            gate = json.loads((run / SCORING_MAPPING_GATE_FILE).read_text(encoding="utf-8"))
            self.assertEqual(gate["evaluated_stage"], "PRE_GENERATION")
            self.assertEqual(gate["status"], "PASS")

    def test_generation_reads_the_plan_gate_and_never_reinvents_it(self) -> None:
        from longform.orchestrator import read_pre_generation_scoring_mapping_gate

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            unplanned = read_pre_generation_scoring_mapping_gate(run)
            self.assertEqual("SCORING_MAPPING_NOT_PLANNED", unplanned["status"],
                             "规划没跑过就没有这道门，不得在生成链里现算一份装作有")
            self.assertEqual("", unplanned["gate_status"],
                             "没有规划产物就没有门的结论可读：不得凭空有一个 verdict")
            self.assertFalse(unplanned["scoring_mapping_expected"])
            tree = build_scoring_taxonomy([_row("SCR-001", "投诉处理闭环机制（10分）")],
                                           [_unit("CU-A", section_id="S09-01")])
            (run / "09_scoring_matrix.json").write_text(json.dumps({
                "rows": tree["rows"],
                "canonical_scoring_records": tree["canonical_scoring_records"],
                "pre_generation_scoring_mapping_gate": pre_generation_scoring_mapping_gate({
                    "rows": tree["rows"],
                    "canonical_scoring_records": tree["canonical_scoring_records"]}),
            }, ensure_ascii=False), encoding="utf-8")
            gate = read_pre_generation_scoring_mapping_gate(run)
            self.assertEqual(gate["status"], "BLOCKED_SCORING_MAPPING_INCOMPLETE")
            self.assertFalse(gate["may_enter_generation"])


class OfflineOnly(unittest.TestCase):
    """§1：本轮全部离线 —— 分类器与门禁只读规划产物，不认识任何 Provider。"""

    def test_taxonomy_module_imports_no_provider(self) -> None:
        import planning.scoring_taxonomy as taxonomy

        source = Path(taxonomy.__file__).read_text(encoding="utf-8")
        for forbidden in ("providers", "requests", "httpx", "openai"):
            self.assertNotIn(forbidden, source, f"评分分类器不得依赖 {forbidden}")


if __name__ == "__main__":
    unittest.main()
