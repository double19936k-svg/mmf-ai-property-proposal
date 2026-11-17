"""MMF-007C Round 4.1 · P0-2（§12–§23）：唯一技术实质评分主题的自动映射与生成前门。

R4-P0-B 把分母换成了"去重后的唯一技术实质主题"，但只解决了"缺落点就挡住"，
没解决"落点从哪来"：Run D 只读回放实测 8 条主题里 4 条无落点，链路停在
`BLOCKED_SCORING_MAPPING_INCOMPLETE`。本文件验的是补上的那一段链：

    Scoring Extraction → Classification → Canonical Technical Topic
      → Requirement Link → Planned Section → Planned CU
      → Evidence Contract → Generation Plan

全部离线：Synthetic Fixture + 纯规划函数 + Run D 快照**只读**回放；
不发起任何 Provider 调用，不写任何历史 Run 目录（§1、§2）。
"""
from __future__ import annotations

import copy
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

from longform.hierarchical import build_volume_generation_plan
from planning.hierarchy import (
    _scoring_section_shapes,
    apply_scoring_mapping_units,
    attach_hierarchy_artifacts,
    build_document_hierarchy,
    build_scoring_matrix,
)
from planning.scoring_evidence import SCORING_ENTITY_RUBRICS
from planning.scoring_mapping import (
    AGGREGATE_KIND_MINIMUM,
    TECHNICAL_TOPIC_KINDS,
    detect_topic_kinds,
    elements_for,
    is_aggregate_topic,
    plan_scoring_mapping,
    rank_sections,
    topic_elements,
    topic_kinds,
)
from planning.scoring_taxonomy import (
    DENOMINATOR_SCORING_CLASS,
    SCORING_MAPPING_GATE_FILE,
    build_scoring_taxonomy,
    pre_generation_scoring_mapping_gate,
)
from planning.planner import build_planning_bundle

from test_mmf007a_hierarchy import _BRIEF, _SELECTION

# §21：4 / 8 / 12 条唯一技术实质主题。前 7 条就是 §19 点名的七个种类，
# 后 5 条故意落在词表之外（认不出种类的主题必须走合同词组，而不是凭空消失）。
TECHNICAL_TOPICS: tuple[str, ...] = (
    "技术部分-项目管理团队（人员素质、人员结构及相关经验，优6-8分/一般3-5分/较差0-2分）",
    "技术部分-项目服务方案（是否覆盖所有物业需求、结构是否清晰详实合理，优8-10分）",
    "技术部分-服务保障能力（保障体系建设、措施全面性可行性有效性与及时响应性，优5分）",
    "技术部分-人员保障方案（各岗位人员保障方案与培训计划完备科学合理可行，优5-6分）",
    "技术部分-项目退出移交方案（退出流程、资料与资产移交、权限交回、问题清单、"
    "人员撤场、过渡安排与双方确认，优3分）",
    "技术部分-项目应急预案（方案设计规范合理、可操作性强、落地部署迅速，4分/2分/0分档）",
    "技术部分-项目管理（针对本项目描述物业管理优势2分并提出管理建议2分）",
    "技术部分-智慧化管理平台应用（平台功能完备并与现有系统对接，3分）",
    "技术部分-节能与能耗管理措施（分项计量与节能改造建议，3分）",
    "技术部分-档案与资料数字化管理（归档目录与借阅交接，2分）",
    "技术部分-客户满意度回访安排（回访频次与满意度改进，2分）",
    "技术部分-多业态协同安排（写字楼与商业裙楼交叉作业协同，2分）",
)

# §21 的噪声行：表头、档次/公式、商务、报价、资信、程序 —— 都不许进分母，
# 更不许为了"提高映射完整度"被派生出正文单元。
NOISE_ROWS: tuple[tuple[str, str], ...] = (
    ("SCR-N01", "合计"),
    ("SCR-N02", "评分标准：优得6-8分，一般得3-5分，较差0-2分"),
    ("SCR-N03", "投标报价合理性（20分）"),
    ("SCR-N04", "类似项目业绩（10分）"),
    ("SCR-N05", "企业资信与财务状况（10分）"),
    ("SCR-N06", "开标投标文件签署要求（1分）"),
)

# 有需求引用的那几条：故意只连一半，剩下的必须靠自动映射找落点。
_LINKED = {0: "REQ-0101", 5: "REQ-0102"}


def _pack(topic_total: int) -> dict:
    labels = TECHNICAL_TOPICS[:topic_total]
    requirements = [{
        "requirement_id": "REQ-0001",
        "normalized_requirement": "项目位于产业园，包含写字楼与商业裙楼业态",
        "mandatory_level": "INFO", "domain": "project_fact",
        "confirmation_status": "CONFIRMED", "scoring_item_id": None,
    }]
    scoring_items = []
    for index, label in enumerate(labels):
        scoring_id = f"SCR-{index + 1:03d}"
        linked = _LINKED.get(index)
        if linked:
            requirements.append({
                "requirement_id": linked,
                "normalized_requirement": label[:24],
                "mandatory_level": "MUST", "domain": "service",
                "confirmation_status": "CONFIRMED", "scoring_item_id": scoring_id,
            })
        scoring_items.append({"scoring_item_id": scoring_id, "label": label,
                              "must_respond": True, "max_score": 8})
        # 同一条事项在评分表里出现两次是常态（评分因素 + 评分细则各写一行）。
        if index == 1:
            scoring_items.append({"scoring_item_id": f"SCR-{index + 1:03d}B",
                                  "label": label.replace("技术部分-", "技术标评分要求："),
                                  "must_respond": True, "max_score": 8})
    for scoring_id, label in NOISE_ROWS:
        scoring_items.append({"scoring_item_id": scoring_id, "label": label,
                              "must_respond": False, "max_score": None})
    return {
        "schema_version": "tender-requirement-pack-v0.1",
        "pack_id": f"PACK-r41-{topic_total}",
        "pack_version": 1, "status": "ready_for_plan",
        "confirmation": {"ready_for_brief_seed": True},
        "project_facts": {"project_area": {"value": "12万平方米", "confirmation_status": "CONFIRMED"}},
        "service_scope": {"included": [], "excluded": []},
        "requirements": requirements,
        "scoring_items": scoring_items,
    }


def _plan(topic_total: int, scale: str = "large_technical_tender") -> dict:
    """跑一遍真实规划链：分层树 → 评分矩阵（内含自动映射）→ 派生单元并入 → 门。

    默认按 300 页级排产：派生一个内容单元要吃该节的机动额度，小计划一节只剩一百多字，
    走的是"没有额度就不新建单元"那条分支（另测）。
    """
    pack = _pack(topic_total)
    bundle = build_planning_bundle(pack, {**_BRIEF, "document_scale": scale}, _SELECTION)
    hierarchy = build_document_hierarchy(pack, bundle)
    matrix = build_scoring_matrix(pack, bundle, hierarchy)
    apply_scoring_mapping_units(hierarchy, matrix, bundle)
    matrix["pre_generation_scoring_mapping_gate"] = pre_generation_scoring_mapping_gate(
        matrix, planned_unit_ids=sorted(
            {str(unit.get("content_unit_id")) for unit in hierarchy["content_units"]}))
    return {"pack": pack, "bundle": bundle, "hierarchy": hierarchy, "matrix": matrix}


class ChainClosesForEveryTopicCount(unittest.TestCase):
    """§16/§17/§21：4 / 8 / 12 条主题都要走完链并带齐七项。"""

    def test_every_topic_ends_with_a_real_home(self) -> None:
        totals = set()
        for topic_total in (4, 8, 12):
            with self.subTest(topics=topic_total):
                plan = _plan(topic_total)
                records = plan["matrix"]["canonical_scoring_records"]
                summary = plan["matrix"]["scoring_mapping"]["summary"]
                self.assertGreaterEqual(len(records), 1)
                # 分母条数由分类与去重决定，不要求等于夹具的主题条数 ——
                # 但三档夹具必须真的拉开差距（否则"同一套代码"只测了一种形状）。
                self.assertEqual(summary["topic_total"], len(records))
                totals.add(len(records))
                self.assertEqual(summary["unmapped_total"], 0, summary["unmapped_ids"])
                self.assertEqual(summary["mapped_total"], summary["topic_total"])
                gate = plan["matrix"]["pre_generation_scoring_mapping_gate"]
                self.assertEqual(gate["status"], "PASS", gate["blocked_reasons"])
                self.assertTrue(gate["may_enter_generation"])
        self.assertEqual(len(totals), 3, f"三档夹具的分母没有拉开：{sorted(totals)}")

    def test_each_topic_carries_the_seven_contract_fields(self) -> None:
        records = _plan(12)["matrix"]["canonical_scoring_records"]
        for record in records:
            with self.subTest(topic=record.get("canonical_scoring_id")):
                self.assertTrue(record.get("canonical_scoring_id"))
                self.assertTrue(record.get("topic"))
                self.assertTrue(record.get("source_ids"))
                self.assertTrue(record.get("planned_response_section"))
                self.assertTrue(record.get("planned_response_cu"))
                self.assertTrue((record.get("evidence_contract") or {}).get("concepts"))
                self.assertTrue(record.get("minimum_substantive_requirements"),
                                "§17 第七项：最低实质要求必须逐条列出，不能只有合同词组")

    def test_mapping_lands_on_units_that_exist_in_the_plan(self) -> None:
        plan = _plan(12)
        known = {str(unit.get("content_unit_id")) for unit in plan["hierarchy"]["content_units"]}
        for record in plan["matrix"]["canonical_scoring_records"]:
            bound = {str(cu) for cu in (record.get("planned_response_cu") or [])}
            self.assertTrue(bound, str(record.get("canonical_scoring_id")))
            self.assertTrue(bound <= known,
                            f"{record.get('canonical_scoring_id')} 绑到了计划里不存在的单元 "
                            f"{sorted(bound - known)}")


class NoiseNeverBecomesContent(unittest.TestCase):
    """§18：不许为提高映射完整度建出商务/报价/资质/资信类内容。"""

    def test_noise_rows_stay_out_of_the_denominator(self) -> None:
        plan = _plan(12)
        by_id = {str(row.get("scoring_item_id")): row for row in plan["matrix"]["rows"]}
        for scoring_id, label in NOISE_ROWS:
            row = by_id.get(scoring_id)
            with self.subTest(label=label):
                self.assertIsNotNone(row)
                self.assertNotEqual(row.get("scoring_class"), DENOMINATOR_SCORING_CLASS,
                                    f"{label} 被当成技术实质项进了分母")
                self.assertEqual(row.get("section_ids") or [], [],
                                 "非技术噪声行不得因为自动映射获得落点")

    def test_derived_units_are_technical_response_units_only(self) -> None:
        plan = _plan(12)
        records = {str(record["canonical_scoring_id"]): record
                   for record in plan["matrix"]["canonical_scoring_records"]}
        for unit in plan["matrix"]["scoring_mapping"]["derived_units"]:
            self.assertEqual(unit.get("provenance"), "SCORING_TECHNICAL_RESPONSE_UNIT")
            record = records.get(str(unit.get("canonical_scoring_id")))
            self.assertIsNotNone(record, "派生单元必须对应一条分母内的主题")
            self.assertEqual(record.get("scoring_class"), DENOMINATOR_SCORING_CLASS)
            self.assertNotIn(str(unit.get("archetype")), ("BIDDER_EVIDENCE_PAGE",))
        gate = plan["matrix"]["pre_generation_scoring_mapping_gate"]
        self.assertEqual(gate["excluded_class_planned_leaks"], [],
                         "范围排除类一旦被规划出落点就是 Technical-Only 泄漏")


class AmbiguityIsNamedNotSilentlyUnmapped(unittest.TestCase):
    """§20：识别不了的种类、两个候选难分高下，都要留下依据与备选，且仍然有落点。"""

    def test_topics_outside_the_kind_vocabulary_still_get_a_home(self) -> None:
        records = {str(record["topic"]): record
                   for record in _plan(12)["matrix"]["canonical_scoring_records"]}
        odd = [topic for topic, record in records.items() if not detect_topic_kinds(record)]
        self.assertTrue(odd, "夹具里 8-12 号主题本就不在 §19 的七个种类内")
        for topic in odd:
            record = records[topic]
            self.assertTrue(record.get("planned_response_cu"), topic)
            self.assertIn(record.get("mapping_basis"),
                          {"TECHNICAL_TOPIC_KIND_ROUTE/UNIT_INTENT_MATCH",
                           "TECHNICAL_TOPIC_KIND_ROUTE/SECTION_FIRST_UNIT_FALLBACK",
                           "TECHNICAL_TOPIC_KIND_ROUTE/HOST_SECTION_HAS_NO_RESERVE",
                           "DERIVED_TECHNICAL_RESPONSE_CU",
                           "AGGREGATE_COVERAGE_OF_MEMBER_TOPICS",
                           "REQUIREMENT_REF_LINK", "EXPLICIT_SCORING_ITEM_IDS"},
                          f"{topic} 的落点依据不在已命名的枚举里")
            self.assertTrue(record.get("mapping_reason"))

    def test_alternatives_are_recorded_and_low_confidence_asks_for_review(self) -> None:
        plan = _plan(8)
        reviewed = [record for record in plan["matrix"]["canonical_scoring_records"]
                    if record.get("mapping_review_required")]
        self.assertTrue(reviewed, "八条主题里必然有落点举证不足的，全都自动通过就是没在看")
        for record in reviewed:
            self.assertTrue(record.get("planned_response_cu"),
                            "低可信度要落成待复核，不是悄悄判未映射（§20）")
            self.assertIsNotNone(record.get("mapping_confidence"))
        ranked = rank_sections(elements_for(plan["matrix"]["canonical_scoring_records"][1]),
                               _shapes(plan))
        self.assertGreaterEqual(len(ranked), 2)
        self.assertTrue(all(row["section_id"] for row in ranked))


class AggregateRowMapsToMemberUnion(unittest.TestCase):
    """§16：一条列举多类的总纲行不是一篇新正文，它是成员主题落点的并集。"""

    def test_aggregate_basis_and_member_union(self) -> None:
        pack = _pack(4)
        umbrella = "本项目技术标须覆盖并响应技术部分评分内容：项目管理团队、项目服务方案、服务保障能力、人员保障方案。"
        pack["scoring_items"].insert(0, {"scoring_item_id": "SCR-0900", "label": umbrella,
                                         "must_respond": True, "max_score": 10})
        bundle = build_planning_bundle(pack, _BRIEF, _SELECTION)
        hierarchy = build_document_hierarchy(pack, bundle)
        matrix = build_scoring_matrix(pack, bundle, hierarchy)
        record = next(row for row in matrix["canonical_scoring_records"]
                      if "SCR-0900" in (row.get("source_ids") or []))
        self.assertTrue(is_aggregate_topic(record))
        self.assertGreaterEqual(len(detect_topic_kinds(record)), AGGREGATE_KIND_MINIMUM)
        self.assertEqual(record["mapping_basis"], "AGGREGATE_COVERAGE_OF_MEMBER_TOPICS")
        self.assertTrue(record["planned_response_cu"])
        self.assertTrue(record["planned_response_sections"])
        members = [row for row in matrix["canonical_scoring_records"]
                   if row is not record and not is_aggregate_topic(row)]
        member_units = {str(cu) for row in members for cu in (row.get("planned_response_cu") or [])}
        self.assertTrue(member_units & {str(cu) for cu in record["planned_response_cu"]})


class TopicKindVocabularyIsWhatSpec19Names(unittest.TestCase):
    """§19：七个种类是词表；退出移交的八项要素必须真的写进合同。"""

    def test_seven_kinds_are_the_published_vocabulary(self) -> None:
        self.assertEqual(tuple(topic_kinds()), TECHNICAL_TOPIC_KINDS)
        self.assertEqual(len(TECHNICAL_TOPIC_KINDS), 7)

    def test_named_topics_are_recognised_from_the_raw_label(self) -> None:
        for label, kind in ((TECHNICAL_TOPICS[0], "MANAGEMENT_TEAM"),
                            (TECHNICAL_TOPICS[1], "SERVICE_PLAN"),
                            (TECHNICAL_TOPICS[2], "SERVICE_GUARANTEE"),
                            (TECHNICAL_TOPICS[3], "STAFF_GUARANTEE"),
                            (TECHNICAL_TOPICS[4], "EXIT_HANDOVER"),
                            (TECHNICAL_TOPICS[5], "EMERGENCY_RESPONSE"),
                            (TECHNICAL_TOPICS[6], "PROJECT_STRENGTH_OR_SUGGESTION")):
            with self.subTest(kind=kind):
                self.assertIn(kind, detect_topic_kinds({"label": label, "topic": ""}))

    def test_exit_handover_requires_the_eight_named_elements(self) -> None:
        elements = topic_elements("EXIT_HANDOVER")
        for required in ("退出流程", "资料移交", "资产设备", "权限交回",
                         "问题清单", "人员撤场", "过渡安排", "双方确认"):
            self.assertIn(required, elements, "§19 点名的退出移交要素缺一项")
        rubric = next(row for row in SCORING_ENTITY_RUBRICS if row["rubric_id"] == "EXIT_HANDOVER")
        self.assertEqual(int(rubric["min_concepts"]), len(rubric["concepts"]),
                         "合同列了八项却只要求举证四项，这张合同就是装饰")

    def test_kind_recognition_survives_score_noise_stripping(self) -> None:
        # Run D 的 SCR-041：去掉分值噪声后 topic 只剩"技术部分-项目管理"，
        # "优势/管理建议"整段被剥掉 —— 只看 topic 就永远认不出这一类。
        record = {"label": TECHNICAL_TOPICS[6], "topic": "技术部分-项目管理"}
        self.assertIn("PROJECT_STRENGTH_OR_SUGGESTION", detect_topic_kinds(record))


class NothingHardcodesTheTopicCount(unittest.TestCase):
    """§17：不得出现 `if project == Run D: total = 8` 这类判定。

    扫的是**代码**不是散文：模块文档里引用 Run D 的实测读数是这轮的要求，
    把它当违规就会逼人把证据从注释里删掉 —— 那是反向激励。
    """

    def test_mapping_module_has_no_project_branch_or_count_comparison(self) -> None:
        import ast
        import re

        source = (APP / "planning" / "scoring_mapping.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        # 文档字符串里引用 Run D 是这轮**要求**的（说清判据从哪次实测量出来）；
        # 违规的是代码里出现具体项目分支，所以先把各层 docstring 从扫描范围里剔掉。
        documented = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", None) or []
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    documented.add(id(body[0].value))
        literals = [node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in documented]
        for value in literals:
            self.assertIsNone(
                re.search(r"Run D|runD|tender_2026|SCN-\d|SCR-\d|济南|产业园", value),
                f"映射模块的代码里出现了具体项目/Run 标识：{value!r}")
        names = ("topic_total", "canonical_total", "mapped_total", "unmapped_total",
                 "elements", "element_total")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or not isinstance(node.ops[0], ast.Eq):
                continue
            rendered = ast.unparse(node)
            if any(re.search(rf"\b{name}\b", rendered) for name in names):
                self.assertIsNone(
                    re.search(r"==\s*\d{1,3}\b", rendered),
                    f"主题条数/要素数被写成了常量比较：{rendered}")

    def test_the_same_code_handles_every_topic_count(self) -> None:
        totals = set()
        for topic_total in (4, 8, 12):
            summary = _plan(topic_total)["matrix"]["scoring_mapping"]["summary"]
            totals.add(summary["topic_total"])
            self.assertEqual(summary["mapped_total"], summary["topic_total"])
        self.assertGreater(len(totals), 1, "三档夹具的分母条数没拉开，等于只测了一种形状")


class DerivedUnitReachesTheGenerationPlan(unittest.TestCase):
    """§16 链尾：派生的落点必须真的进生成计划，否则"映射"只是矩阵里的一句话。"""

    def test_volume_plan_contains_the_derived_unit(self) -> None:
        plan = _plan(12)
        applied = plan["matrix"]["scoring_mapping"]["application"]
        self.assertEqual(applied["unfunded_total"], 0, applied["unfunded"])
        self.assertTrue(applied["applied_unit_ids"],
                        "12 条主题里应有至少一条既有节举证不足 → 派生单元，否则这条链没被走到")
        volume_plan = build_volume_generation_plan(plan["hierarchy"], plan["bundle"], None)
        planned = {cu for volume in volume_plan["volumes"] for cu in volume["content_unit_ids"]}
        for unit_id in applied["applied_unit_ids"]:
            self.assertIn(unit_id, planned,
                          "派生单元没进生成计划 —— 矩阵写着有落点，生成时没人写它")

    def test_budget_is_taken_from_section_reserve_not_newly_created(self) -> None:
        pack = _pack(12)
        bundle = build_planning_bundle(pack, {**_BRIEF, "document_scale": "large_technical_tender"},
                                       _SELECTION)
        before = build_document_hierarchy(pack, bundle)
        document_before = before["budget_tree"]["planned_total_chars"]
        units_before = len(before["content_units"])
        matrix = build_scoring_matrix(pack, bundle, before)
        applied = apply_scoring_mapping_units(before, matrix, bundle)
        self.assertTrue(applied["applied_total"], "先确认这条链真的派生了单元，否则下面的守恒检查是空的")
        tree = before["budget_tree"]
        self.assertTrue(tree["conservation"]["conserved"], tree["conservation"]["broken_levels"])
        self.assertEqual(tree["planned_total_chars"], document_before,
                         "派生单元吃的是该节机动额度，文档总额不得因为补落点而变大")
        self.assertEqual(
            tree["allocated_total_chars"] + tree["reserve_total_chars"], document_before)
        self.assertEqual(len(before["content_units"]), units_before + applied["applied_total"])
        self.assertEqual(before["validation"]["status"], "PASS", before["validation"]["errors"])

    def test_section_without_reserve_borrows_nothing_and_asks_for_review(self) -> None:
        """机动额度不够时不造单元：让该节既有单元承载，并把缺口写成具名待复核。"""
        pack = _pack(12)
        bundle = build_planning_bundle(pack, {**_BRIEF, "document_scale": "large_technical_tender"},
                                       _SELECTION)
        hierarchy = build_document_hierarchy(pack, bundle)
        for section in hierarchy["sections"]:
            section["budget_split"]["reserve_budget"] = 0
        matrix = build_scoring_matrix(pack, bundle, hierarchy)
        bases = [str(record.get("mapping_basis")) for record in matrix["canonical_scoring_records"]]
        units_before = len(hierarchy["content_units"])
        applied = apply_scoring_mapping_units(hierarchy, matrix, bundle)
        self.assertEqual(applied["applied_total"], 0, bases)
        self.assertEqual(len(hierarchy["content_units"]), units_before,
                         "没有额度就不该多出一个内容单元")
        self.assertEqual(matrix["scoring_mapping"]["summary"]["unmapped_total"], 0)
        self.assertTrue(all(record.get("mapping_review_required")
                            for record in matrix["canonical_scoring_records"]
                            if record.get("mapping_basis")
                            == "TECHNICAL_TOPIC_KIND_ROUTE/HOST_SECTION_HAS_NO_RESERVE"))

    def test_forced_unfunded_unit_does_not_fake_a_home(self) -> None:
        """兜底：派生清单里真出现没额度的单元时，门必须挡住而不是放行。"""
        plan = _plan(12)
        hierarchy, matrix = plan["hierarchy"], plan["matrix"]
        matrix["scoring_mapping"]["derived_units"] = [{
            "content_unit_id": "CU-S01-01-99", "section_id": "S01-01",
            "canonical_scoring_id": "SCN-999", "archetype": "NARRATIVE",
            "title": "伪造的派生单元", "intent": "伪造的派生单元", "must_cover": [],
        }]
        for record in matrix["canonical_scoring_records"]:
            if str(record.get("canonical_scoring_id")) == "SCN-999":
                record["planned_response_cu"] = ["CU-S01-01-99"]
        matrix["canonical_scoring_records"].append({
            "canonical_scoring_id": "SCN-999", "topic": "伪造的派生单元",
            "source_ids": ["SCR-999"], "planned_response_section": "S01-01",
            "planned_response_cu": ["CU-S01-01-99"],
            "evidence_contract": {"concepts": ["要素"], "min_concepts": 1},
            "mapping_basis": "DERIVED_TECHNICAL_RESPONSE_CU",
        })
        for section in hierarchy["sections"]:
            section["budget_split"]["reserve_budget"] = 0
        applied = apply_scoring_mapping_units(hierarchy, matrix, plan["bundle"])
        self.assertIn("UNFUNDED_DERIVED_UNIT", applied["unfunded"][0]["reason"])
        gate = pre_generation_scoring_mapping_gate(
            matrix, planned_unit_ids=[str(unit.get("content_unit_id"))
                                      for unit in hierarchy["content_units"]])
        self.assertEqual(gate["status"], "BLOCKED_SCORING_MAPPING_INCOMPLETE",
                         "没有额度的落点是假落点，必须挡住而不是放行")


class BrokenMappingStillBlocks(unittest.TestCase):
    """§23：自动映射不许把门变成橡皮图章。"""

    def test_hand_broken_binding_is_blocked_even_with_all_fields_filled(self) -> None:
        plan = _plan(8)
        matrix = plan["matrix"]
        known = sorted({str(unit.get("content_unit_id")) for unit in plan["hierarchy"]["content_units"]})
        self.assertEqual(pre_generation_scoring_mapping_gate(
            matrix, planned_unit_ids=known)["status"], "PASS")
        broken = copy.deepcopy(matrix)
        record = broken["canonical_scoring_records"][0]
        record["planned_response_cu"] = ["CU-S99-99-99"]
        record["planned_response_section"] = "S99-99"
        record["evidence_contract"] = {"concepts": ["有"], "min_concepts": 1}
        gate = pre_generation_scoring_mapping_gate(broken, planned_unit_ids=known)
        self.assertEqual(gate["status"], "BLOCKED_SCORING_MAPPING_INCOMPLETE")
        self.assertFalse(gate["may_enter_generation"])
        self.assertIn("PLANNED_CONTENT_UNIT_NOT_IN_PLAN",
                      gate["unmapped_records"][0]["missing"])

    def test_unbound_topic_still_blocks(self) -> None:
        broken = copy.deepcopy(_plan(4)["matrix"])
        broken["canonical_scoring_records"][0]["planned_response_cu"] = []
        gate = pre_generation_scoring_mapping_gate(
            broken, planned_unit_ids=["CU-S01-01-01"])
        self.assertEqual(gate["status"], "BLOCKED_SCORING_MAPPING_INCOMPLETE")
        self.assertIn("PLANNED_CONTENT_UNIT", gate["unmapped_records"][0]["missing"])

    def test_planning_writes_the_gate_artifact_the_generation_side_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            pack = _pack(12)
            (run / "requirement_pack.json").write_text(json.dumps(pack, ensure_ascii=False),
                                                        encoding="utf-8")
            bundle = build_planning_bundle(pack, _BRIEF, _SELECTION)
            written = attach_hierarchy_artifacts(run, bundle)
            self.assertIn("scoring_mapping_gate", written or {})
            gate = json.loads((run / SCORING_MAPPING_GATE_FILE).read_text(encoding="utf-8-sig"))
            self.assertEqual(gate["status"], "PASS", gate["blocked_reasons"])
            # 生成链读的是 09 里内嵌的那一份门（§34 的落点约定），并按盘上的计划清单**重算**
            # 一遍引用（Final Gate §5.3）；把它换成"有主题没落点"，读出来必须是不放行。
            matrix = json.loads((run / "09_scoring_matrix.json").read_text(encoding="utf-8-sig"))
            self.assertEqual(matrix["pre_generation_scoring_mapping_gate"]["status"], "PASS")
            total = len(matrix["canonical_scoring_records"])
            matrix["pre_generation_scoring_mapping_gate"] = {
                "status": "BLOCKED_SCORING_MAPPING_INCOMPLETE",
                "may_enter_generation": False,
                "substantive_topic_total": total, "mapped_total": total - 1, "unmapped_total": 1,
                "unmapped_records": [{"canonical_scoring_id": "SCN-001", "topic": "被破坏的主题",
                                      "missing": ["PLANNED_CONTENT_UNIT"]}],
                "blocked_reasons": ["1/1 个唯一技术实质评分主题缺 PLANNED_CONTENT_UNIT"],
            }
            (run / "09_scoring_matrix.json").write_text(json.dumps(matrix, ensure_ascii=False),
                                                         encoding="utf-8")
            from longform.orchestrator import read_pre_generation_scoring_mapping_gate
            read = read_pre_generation_scoring_mapping_gate(run)
            self.assertEqual(read["status"], "BLOCKED_SCORING_MAPPING_INCOMPLETE")
            self.assertFalse(read["may_enter_generation"])

    def test_embedded_pass_over_ghost_references_is_recomputed_not_trusted(self) -> None:
        """Final Gate §5.3：嵌在 09 里的 PASS 只是一句声称 —— 生成侧必须按清单重算。

        旧读取器拿到 embedded 门就放行，于是"门结论改成 PASS + 落点写成查无此节"
        这一对矛盾能直接进正式生成。
        """
        from longform.orchestrator import read_pre_generation_scoring_mapping_gate
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            pack = _pack(12)
            (run / "requirement_pack.json").write_text(json.dumps(pack, ensure_ascii=False),
                                                        encoding="utf-8")
            attach_hierarchy_artifacts(run, build_planning_bundle(pack, _BRIEF, _SELECTION))
            matrix = json.loads((run / "09_scoring_matrix.json").read_text(encoding="utf-8-sig"))
            self.assertEqual(read_pre_generation_scoring_mapping_gate(run)["status"], "PASS")
            matrix["canonical_scoring_records"][0]["planned_response_section"] = "S99-99"
            matrix["canonical_scoring_records"][0]["planned_response_sections"] = ["S99-99"]
            matrix["pre_generation_scoring_mapping_gate"] = {
                "status": "PASS", "may_enter_generation": True,
                "substantive_topic_total": len(matrix["canonical_scoring_records"]),
                "mapped_total": len(matrix["canonical_scoring_records"]), "unmapped_total": 0,
                "evaluated_stage": "PRE_GENERATION"}
            (run / "09_scoring_matrix.json").write_text(json.dumps(matrix, ensure_ascii=False),
                                                         encoding="utf-8")
            reading = read_pre_generation_scoring_mapping_gate(run)
            self.assertFalse(reading["may_enter_generation"],
                             "查无此节的幽灵落点带着 embedded PASS 进了生成入口")
            self.assertEqual("BLOCKED_SCORING_MAPPING_INCOMPLETE", reading["status"])


class RunDReadOnlyReplay(unittest.TestCase):
    """§22：Run D 只读回放必须从 Mapped 4 走到 8、Unmapped 归零。"""

    EVIDENCE_ROOT = APP_ROOT.parent / "MMF_Project_Records" / "MMF-007" / "04_Real_Run_Evidence"

    def _run_dir(self) -> Path | None:
        for candidate in sorted((self.EVIDENCE_ROOT / "MMF007C_R3_20260921"
                                 / "runD_snapshot_final").glob("*/09_scoring_matrix.json")):
            if candidate.is_file():
                return candidate.parent
        return None

    def test_mapped_goes_from_four_to_eight(self) -> None:
        run = self._run_dir()
        if run is None:
            self.skipTest("源码树之外才有项目记录树，production_dist 不带 Run D 快照")
        names = ("09_scoring_matrix.json", "08_document_hierarchy.json", "04_section_contracts.json")
        stamps = {name: (run / name).stat().st_mtime_ns for name in names}
        matrix = json.loads((run / names[0]).read_text(encoding="utf-8-sig"))
        hierarchy = json.loads((run / names[1]).read_text(encoding="utf-8-sig"))
        contracts = {str(row.get("section_id")): row for row in json.loads(
            (run / names[2]).read_text(encoding="utf-8-sig")).get("contracts") or []}
        units = hierarchy.get("content_units") or []
        units_by_section: dict[str, list[dict]] = {}
        for unit in units:
            units_by_section.setdefault(str(unit.get("section_id")), []).append(unit)
        tree = build_scoring_taxonomy(matrix.get("rows") or [], units)
        records = tree["canonical_scoring_records"]
        before_mapped = sum(1 for row in records
                            if row.get("planned_response_section") and row.get("planned_response_cu"))
        self.assertEqual(before_mapped, 4, "Run D 的起点必须是 R4 实测的 4/8，否则这条回放不是在说同一件事")
        # 回放必须走产品自己的取面函数（含该节机动额度），否则这里 PASS 不代表线上 PASS。
        shapes = _scoring_section_shapes(
            hierarchy, {"section_contracts": {"contracts": list(contracts.values())}})
        result = plan_scoring_mapping(copy.deepcopy(records), shapes, units_by_section=units_by_section)
        summary = result["summary"]
        self.assertEqual(summary["topic_total"], 8)
        self.assertEqual(summary["unmapped_total"], 0, summary["unmapped_ids"])
        self.assertEqual(summary["mapped_total"], 8)
        for record in result["records"]:
            self.assertTrue(record.get("minimum_substantive_requirements"))
            self.assertTrue(record.get("mapping_basis"))
        gate = pre_generation_scoring_mapping_gate(
            {"rows": tree["rows"], "canonical_scoring_records": result["records"]},
            planned_unit_ids=sorted({str(unit.get("content_unit_id")) for unit in units}
                                    | {str(unit["content_unit_id"])
                                       for unit in result["derived_units"]}))
        self.assertEqual(gate["status"], "PASS", gate["blocked_reasons"])
        self.assertTrue(gate["may_enter_generation"])
        self.assertTrue(all((run / name).stat().st_mtime_ns == stamp for name, stamp in stamps.items()),
                        "只读回放不得改动 Run D 快照")


def _shapes(plan: dict) -> list[dict]:
    return _scoring_section_shapes(plan["hierarchy"], plan["bundle"])


if __name__ == "__main__":
    unittest.main()
