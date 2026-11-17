"""MMF-007A 增量数据契约的 Mock 验证（总纲 T5 五场景）。

场景一：分层规划——Volume -> Chapter -> Section -> Content Unit 结构、ID 规则、字数预算。
场景二：重复 ID——层级注册表出现重复 section/unit ID 时校验必须失败。
场景三：缺失章节——评分项无法映射任何 Section 时标记 MISSING；层级指向缺失章节时报错。
场景四：失败批次——批次失败不进入复用清单，失败 Section 归入待重做。
场景五：恢复路径——谱系一致全部复用；章节契约变化仅局部失效；需求或事实变化全部失效。

红线检查：本文件不修改任何既有规划/生成行为，仅验证增量模块与既有 ID 体系的兼容。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from planning.hierarchy import (
    build_document_hierarchy,
    build_scoring_matrix,
    compute_lineage,
    plan_invalidation,
    query_scoring_matrix,
    validate_hierarchy,
)
from planning.planner import build_planning_bundle


def _pack() -> dict:
    return {
        "schema_version": "tender-requirement-pack-v0.1",
        "pack_id": "PACK-test0001",
        "pack_version": 1,
        "status": "ready_for_plan",
        "confirmation": {"ready_for_brief_seed": True},
        "project_facts": {
            "project_area": {"value": "12万平方米", "confirmation_status": "CONFIRMED"},
            "staffing_min": {"value": 45, "confirmation_status": "CONFIRMED"},
        },
        "service_scope": {"included": [], "excluded": []},
        "requirements": [
            {"requirement_id": "REQ-0001", "normalized_requirement": "项目位于产业园，包含写字楼与商业裙楼业态", "mandatory_level": "INFO", "domain": "project_fact", "confirmation_status": "CONFIRMED", "scoring_item_id": None},
            {"requirement_id": "REQ-0002", "normalized_requirement": "最低人员配置不少于45人，需明确岗位与班次安排", "mandatory_level": "MUST", "domain": "staffing", "confirmation_status": "CONFIRMED", "scoring_item_id": None},
            {"requirement_id": "REQ-0003", "normalized_requirement": "园区实行封闭式管理，重点区域需要定时巡逻", "mandatory_level": "INFO", "domain": "security", "confirmation_status": "CONFIRMED", "scoring_item_id": None},
            {"requirement_id": "REQ-0004", "normalized_requirement": "投诉需要建立台账并在约定周期内闭环处理", "mandatory_level": "INFO", "domain": "sla_kpi", "confirmation_status": "CONFIRMED", "scoring_item_id": "SCR-001"},
            {"requirement_id": "REQ-0005", "normalized_requirement": "针对产业园特点提出合理性管理建议", "mandatory_level": "INFO", "domain": "scoring", "confirmation_status": "CONFIRMED", "scoring_item_id": "SCR-002"},
            {"requirement_id": "REQ-0006", "normalized_requirement": "服务范围不含住户专有部分及经营性餐饮区域", "mandatory_level": "INFO", "domain": "exclusion", "confirmation_status": "CONFIRMED", "scoring_item_id": None},
            {"requirement_id": "REQ-0007", "normalized_requirement": "历史样本中的人员规模数据仅作参考，不构成本项目配置", "mandatory_level": "INFO", "domain": "other", "confirmation_status": "REJECTED_AS_NOT_APPLICABLE", "scoring_item_id": None},
        ],
        "scoring_items": [
            {"scoring_item_id": "SCR-001", "label": "客户服务与投诉闭环机制（10分）", "must_respond": True, "max_score": 10},
            {"scoring_item_id": "SCR-002", "label": "针对产业园业态的合理化建议（20分）", "must_respond": True, "max_score": 20},
        ],
    }


_BRIEF = {"project_name": "测试产业园项目", "medium": "WORD", "project_type": "产业园综合体", "scenario": "完整物业服务方案"}
_SELECTION = {"knowledge_usage_contracts": []}


def _bundle(pack: dict | None = None) -> dict:
    return build_planning_bundle(pack or _pack(), _BRIEF, _SELECTION)


def _lineage_with_contract_change(pack: dict, bundle: dict, section_id: str) -> dict:
    lineage = compute_lineage(pack, bundle)
    contracts = (bundle.get("section_contracts") or {}).get("contracts") or []
    import copy

    patched = copy.deepcopy(contracts)
    for row in patched:
        if row.get("section_id") == section_id:
            row["must_cover"] = list(row.get("must_cover") or []) + ["新增覆盖要点"]
    import planning.planner as planner_mod

    bundle_changed = {**bundle, "section_contracts": {**bundle["section_contracts"], "contracts": patched}}
    return compute_lineage(pack, bundle_changed)


class Scenario1HierarchicalPlanning(unittest.TestCase):
    def test_document_scale_controls_budget_and_content_unit_depth(self) -> None:
        pack = _pack()
        scales = [
            ("regular_solution", (50, 70), 60),
            ("detailed_technical_solution", (120, 150), 135),
            ("large_technical_tender", (250, 320), 285),
        ]
        bundles = [build_planning_bundle(pack, {**_BRIEF, "document_scale": scale}, _SELECTION) for scale, _, _ in scales]
        hierarchies = [build_document_hierarchy(pack, bundle) for bundle in bundles]
        baseline_sections = [row["section_id"] for row in hierarchies[0]["sections"]]
        for (scale, page_range, planning_pages), bundle, hierarchy in zip(scales, bundles, hierarchies):
            target = bundle["document_target"]
            budget = bundle["content_budget"]
            self.assertEqual(target["target_scale"], scale)
            self.assertEqual((target["word"]["target_pages_min"], target["word"]["target_pages_max"]), page_range)
            self.assertEqual(target["word"]["planning_pages"], planning_pages)
            self.assertEqual(budget["totals"]["planning_pages"], planning_pages)
            self.assertEqual(target["word"]["target_effective_chars_min"], budget["totals"]["target_effective_chars_min"])
            self.assertEqual([row["section_id"] for row in hierarchy["sections"]], baseline_sections)
            self.assertEqual(hierarchy["validation"]["status"], "PASS")
        self.assertLess(hierarchies[0]["counts"]["content_units"], hierarchies[1]["counts"]["content_units"])
        self.assertLess(hierarchies[1]["counts"]["content_units"], hierarchies[2]["counts"]["content_units"])
        self.assertLess(bundles[0]["content_budget"]["totals"]["target_words_max"], bundles[1]["content_budget"]["totals"]["target_words_max"])
        self.assertLess(bundles[1]["content_budget"]["totals"]["target_words_max"], bundles[2]["content_budget"]["totals"]["target_words_max"])

    def test_hierarchy_structure_ids_and_units(self) -> None:
        pack = _pack()
        bundle = _bundle(pack)
        hierarchy = build_document_hierarchy(pack, bundle)

        self.assertEqual(hierarchy["schema_version"], "document-hierarchy-v0.2-technical-bid")
        self.assertEqual(hierarchy["validation"]["status"], "PASS", hierarchy["validation"]["errors"])
        counts = hierarchy["counts"]
        self.assertEqual(counts["sections"], len(hierarchy["sections"]))
        self.assertEqual(counts["content_units"], len(hierarchy["content_units"]))
        self.assertGreater(counts["content_units"], 0)
        # 每个 Section 至少一个内容单元，单元 ID 唯一且归属正确
        section_ids = {row["section_id"] for row in hierarchy["sections"]}
        unit_sections = {row["section_id"] for row in hierarchy["content_units"]}
        self.assertEqual(section_ids, unit_sections)
        for unit in hierarchy["content_units"]:
            self.assertTrue(unit["content_unit_id"].startswith(f"CU-{unit['section_id']}-"))
        # 章节既保留原 chapter_id，又挂上了 volume
        for chapter in hierarchy["chapters"]:
            self.assertTrue(chapter["chapter_id"].startswith("CH"))
            self.assertTrue(chapter["volume_id"].startswith("V"))
        # §8/§10：CU 额度合计 + 机动额度合计 == 契约额度合计。
        # Round 3 之前 CU 直接摊满契约、reserve 恒为 0，这条式子就退化成"同一个数加两遍"，
        # 谁都看不出字在哪一层丢了；现在两者分开记，差额必须等于该节机动额度。
        contract_words = sum(
            int((row.get("target_words") or {}).get("target") or (row.get("target_words") or {}).get("max") or 0)
            for row in bundle["section_contracts"]["contracts"]
        )
        reserve_words = sum(int((row.get("budget_split") or {}).get("reserve_budget") or 0)
                            for row in hierarchy["sections"])
        self.assertGreater(reserve_words, 0, "§10 要求每节留机动额度，reserve 为 0 说明切分没落地")
        self.assertEqual(counts["planned_words"] + reserve_words, contract_words)
        tree = hierarchy["budget_tree"]
        self.assertEqual(tree["measure"], "unified_effective_chars")
        self.assertTrue(tree["conservation"]["conserved"], tree["conservation"]["broken_levels"])
        self.assertEqual(tree["conservation"]["broken_levels"], [])
        self.assertEqual(tree["planned_total_chars"], contract_words)
        self.assertEqual(tree["allocated_total_chars"] + tree["reserve_total_chars"], contract_words)
        # §10 参考带上沿：结构额度不得吃掉超过 35% 的本节额度（Run D 实测 43.7% 就是
        # 395 页的来源，页数尺子按设计混合标定，偏出去门控就会永远判缺页）。
        for section in hierarchy["sections"]:
            split = section.get("budget_split") or {}
            self.assertLessEqual(float(split.get("structured_share") or 0), 0.36,
                                 f"{section['section_id']} 结构占比 {split.get('structured_share')} 超出 §10 参考带")

    def test_scoring_matrix_planned_rows(self) -> None:
        pack = _pack()
        bundle = _bundle(pack)
        matrix = build_scoring_matrix(pack, bundle, build_document_hierarchy(pack, bundle))
        self.assertEqual(matrix["schema_version"], "scoring-matrix-v0.3")
        summary = matrix["coverage_summary"]
        self.assertEqual(summary["scoring_total"], 2)
        self.assertEqual(summary["scoring_missing"], 0)
        self.assertEqual(summary["must_respond_planned"], 2)
        # §25/§33：覆盖率分母换成"唯一技术实质主题"，旧按行口径必须并列保留，换口径不藏数字。
        # MMF008A-A：主题口径之上还要过第二维（技术正文可响应性），字符串随之改口；
        # 全量十类口径的另一条并列字符串同表报出，两个数都看得见。
        self.assertEqual(summary["coverage_denominator"], "UNIQUE_TECHNICAL_RESPONDABLE_TOPICS")
        self.assertEqual(summary["coverage_denominator_all_response_classes"],
                         "UNIQUE_TECHNICAL_SUBSTANTIVE_TOPICS_ALL_RESPONSE_CLASSES")
        self.assertEqual(summary["row_coverage_denominator"], "TECHNICAL_SCORING_COVERAGE_ROWS")
        self.assertEqual(summary["technical_scoring_total"], 2)
        self.assertEqual(summary["canonical_scoring_total"], 1)
        row = query_scoring_matrix(matrix, "SCR-001")
        self.assertIsNotNone(row)
        self.assertEqual(row["response_status"], "PLANNED")
        self.assertTrue(row["section_ids"], "评分项应映射到至少一个 Section")
        self.assertIn("REQ-0004", row["requirement_ids"])
        # §24：规划期就要写下响应 CU 与实体证据合同。
        self.assertTrue(row["planned_response_cu"], "评分项应记录计划响应 Content Unit")
        self.assertIn("min_concepts", row["entity_contract"])
        self.assertEqual(row["fulfillment"]["status"], "NOT_GENERATED")
        self.assertIn("response_cu", row["fulfillment"])
        # §32：canonical 记录带上权重、去重依据与真实绑定的计划 CU。
        record = next(item for item in matrix["canonical_scoring_records"]
                      if item["canonical_scoring_id"] == "SCN-001")
        self.assertEqual(record["source_ids"], ["SCR-001"])
        self.assertEqual(record["weight_if_known"], 10.0)
        self.assertEqual(record["cu_binding"]["basis"], "EXPLICIT_SCORING_ITEM_IDS")
        self.assertTrue(record["planned_response_cu"])
        self.assertEqual(row["canonical_scoring_id"], record["canonical_scoring_id"])
        self.assertEqual(row["cu_binding_basis"], "EXPLICIT_SCORING_ITEM_IDS")
        # §34：规划期评分映射门（缺 planned section / CU / 契约不得进正式 300-Class）。
        gate = matrix["pre_generation_scoring_mapping_gate"]
        self.assertEqual(gate["status"], "PASS")
        self.assertTrue(gate["may_enter_generation"])
        # §33/§35：SCR-002 被范围门判成 TECHNICAL_REFERENCE，分类器不自行改判，
        # 但"离开分母却仍标着必须响应/带分值"的条目必须逐条出现在 shrink watch 里。
        self.assertEqual(query_scoring_matrix(matrix, "SCR-002")["scoring_class"], "TECHNICAL_CONTEXTUAL")
        watch = gate["denominator_shrink_watch"]
        self.assertEqual([item["scoring_item_id"] for item in watch], ["SCR-002"])
        self.assertEqual(watch[0]["scope_class"], "TECHNICAL_REFERENCE")
        # 既有 requirement-section-matrix 中的 scoring 引用不应被丢失
        self.assertEqual(matrix["matrix_only_scoring_refs"], [])


class Scenario2DuplicateIds(unittest.TestCase):
    def test_duplicate_section_and_unit_ids_fail_validation(self) -> None:
        bundle = _bundle()
        hierarchy = build_document_hierarchy(_pack(), bundle)
        self.assertEqual(hierarchy["validation"]["status"], "PASS")

        tampered = json.loads(json.dumps(hierarchy))
        tampered["sections"].append(dict(tampered["sections"][0]))
        tampered["content_units"].append({**tampered["content_units"][0], "section_id": tampered["sections"][0]["section_id"]})
        result = validate_hierarchy(tampered)
        self.assertEqual(result["status"], "FAIL")
        codes = {error["code"] for error in result["errors"]}
        self.assertIn("DUPLICATE_SECTION_ID", codes)
        self.assertIn("DUPLICATE_CONTENT_UNIT_ID", codes)

    def test_duplicate_volume_id_fails_validation(self) -> None:
        hierarchy = build_document_hierarchy(_pack(), _bundle())
        tampered = json.loads(json.dumps(hierarchy))
        tampered["volumes"].append(dict(tampered["volumes"][0]))
        result = validate_hierarchy(tampered)
        codes = {error["code"] for error in result["errors"]}
        self.assertIn("DUPLICATE_VOLUME_ID", codes)


class Scenario3MissingSections(unittest.TestCase):
    def test_unmapped_scoring_item_is_missing(self) -> None:
        pack = _pack()
        # SCR-001 标签不可路由，验证"需求引用存在但路由未命中"的真缺失分支
        pack = {**pack, "scoring_items": [{**pack["scoring_items"][0], "label": "区块链存证服务专项（10分）"}, *pack["scoring_items"][1:]]}
        bundle = _bundle(_pack())
        # 模拟映射缺失：REQ-0004 的矩阵行变为 UNMAPPED（如无可用 Section）
        matrix_rows = bundle["requirement_matrix"]["matrix"]
        patched = []
        for row in matrix_rows:
            if row.get("requirement_id") == "REQ-0004":
                patched.append({**row, "coverage_status": "UNMAPPED", "primary_section_id": None, "secondary_section_ids": []})
            else:
                patched.append(row)
        broken_bundle = {
            **bundle,
            "requirement_matrix": {**bundle["requirement_matrix"], "matrix": patched},
        }
        matrix = build_scoring_matrix(pack, broken_bundle)
        row = query_scoring_matrix(matrix, "SCR-001")
        self.assertEqual(row["response_status"], "MISSING")
        self.assertIsNotNone(row["missing_reason"])
        self.assertEqual(matrix["coverage_summary"]["scoring_missing"], 1)
        self.assertEqual(matrix["coverage_summary"]["must_respond_planned"], 1)

    def test_section_pointing_to_missing_chapter_fails_validation(self) -> None:
        hierarchy = build_document_hierarchy(_pack(), _bundle())
        tampered = json.loads(json.dumps(hierarchy))
        tampered["sections"][0]["chapter_id"] = "CH99"
        result = validate_hierarchy(tampered)
        codes = {error["code"] for error in result["errors"]}
        self.assertIn("MISSING_CHAPTER", codes)


class Scenario4FailedBatch(unittest.TestCase):
    def test_failed_sections_are_not_reusable(self) -> None:
        pack = _pack()
        bundle = _bundle(pack)
        lineage = compute_lineage(pack, bundle)
        completed = ["S01-01", "S01-02", "S02-01", "S03-01", "S03-02"]
        failed = ["S04-01", "S05-02"]

        plan = plan_invalidation(lineage, lineage, completed, failed_sections=failed)
        self.assertNotIn("S04-01", plan["reusable_sections"])
        self.assertNotIn("S05-02", plan["reusable_sections"])
        self.assertEqual(sorted(plan["pending_sections"]), failed)
        self.assertEqual(sorted(plan["reusable_sections"]), sorted(set(completed) - set(failed)))
        self.assertEqual(plan["invalidated_sections"], [])


class Scenario5ResumePaths(unittest.TestCase):
    def test_identical_lineage_reuses_everything(self) -> None:
        pack = _pack()
        bundle = _bundle(pack)
        lineage = compute_lineage(pack, bundle)
        completed = ["S01-01", "S02-01", "S03-02"]
        plan = plan_invalidation(lineage, lineage, completed)
        self.assertEqual(plan["invalidation_scope"], "NONE")
        self.assertEqual(sorted(plan["reusable_sections"]), completed)
        self.assertEqual(plan["invalidated_sections"], [])

    def test_contract_change_invalidates_only_affected_section(self) -> None:
        pack = _pack()
        bundle = _bundle(pack)
        old_lineage = compute_lineage(pack, bundle)
        new_lineage = _lineage_with_contract_change(pack, bundle, "S03-02")
        completed = ["S01-01", "S02-01", "S03-02"]
        plan = plan_invalidation(old_lineage, new_lineage, completed)
        self.assertEqual(plan["invalidation_scope"], "SECTION_CONTRACT_CHANGED")
        self.assertEqual(plan["invalidated_sections"], ["S03-02"])
        self.assertEqual(sorted(plan["reusable_sections"]), ["S01-01", "S02-01"])

    def test_requirement_change_invalidates_all(self) -> None:
        import copy

        pack = _pack()
        bundle = _bundle(pack)
        old_lineage = compute_lineage(pack, bundle)
        changed = copy.deepcopy(pack)
        for row in changed["requirements"]:
            if row["requirement_id"] == "REQ-0002":
                row["normalized_requirement"] = "最低人员配置调整为不少于60人并说明班次安排"
        new_lineage = compute_lineage(changed, bundle)
        plan = plan_invalidation(old_lineage, new_lineage, ["S01-01", "S03-02"])
        self.assertEqual(plan["invalidation_scope"], "ALL_REQUIREMENTS_CHANGED")
        self.assertEqual(plan["reusable_sections"], [])
        self.assertEqual(sorted(plan["invalidated_sections"]), ["S01-01", "S03-02"])

    def test_fact_change_invalidates_all(self) -> None:
        import copy

        pack = _pack()
        bundle = _bundle(pack)
        old_lineage = compute_lineage(pack, bundle)
        changed = copy.deepcopy(pack)
        changed["project_facts"]["staffing_min"]["value"] = 60
        new_lineage = compute_lineage(changed, bundle)
        self.assertNotEqual(old_lineage["fact_fingerprint"], new_lineage["fact_fingerprint"])
        plan = plan_invalidation(old_lineage, new_lineage, ["S03-02"])
        self.assertEqual(plan["invalidation_scope"], "ALL_FACTS_CHANGED")
        self.assertEqual(plan["reusable_sections"], [])


class ScenarioT41ScoringTriage(unittest.TestCase):
    """T4.1：评分项四路分诊（需求引用 > 商务排除 > 标签派生 > 真缺失）。"""

    def _matrix_with(self, scoring_items: list[dict]) -> dict:
        import copy

        pack = copy.deepcopy(_pack())
        for row in pack["requirements"]:
            row["scoring_item_id"] = None
        pack["scoring_items"] = scoring_items
        return build_scoring_matrix(pack, _bundle(_pack()))

    def test_commercial_label_is_out_of_scope(self) -> None:
        matrix = self._matrix_with([
            {"scoring_item_id": "SCR-001", "label": "投标人类似项目业绩情况（5分）", "must_respond": True},
        ])
        row = query_scoring_matrix(matrix, "SCR-001")
        self.assertEqual(row["response_status"], "OUT_OF_TECHNICAL_SCOPE")
        self.assertEqual(row["mapping_source"], "label_triage")
        self.assertIn("商务标", row["missing_reason"])

    def test_label_routing_derives_planned(self) -> None:
        matrix = self._matrix_with([
            {"scoring_item_id": "SCR-001", "label": "投诉受理与回访闭环机制是否健全（10分）", "must_respond": True},
        ])
        row = query_scoring_matrix(matrix, "SCR-001")
        self.assertEqual(row["response_status"], "DERIVED_PLANNED")
        self.assertEqual(row["mapping_source"], "label_keyword_routing")
        self.assertEqual(row["primary_section_id"], "S04-02")
        self.assertIsNone(row["missing_reason"])

    def test_unroutable_label_stays_missing(self) -> None:
        matrix = self._matrix_with([
            {"scoring_item_id": "SCR-001", "label": "区块链存证服务专项（10分）", "must_respond": True},
        ])
        row = query_scoring_matrix(matrix, "SCR-001")
        self.assertEqual(row["response_status"], "MISSING")
        self.assertIsNone(row["mapping_source"])

    def test_requirement_link_beats_label_triage(self) -> None:
        import copy

        pack = copy.deepcopy(_pack())
        pack["scoring_items"] = [
            {"scoring_item_id": "SCR-001", "label": "投标人类似项目业绩情况（5分）", "must_respond": True},
        ]
        bundle = _bundle(_pack())
        matrix = build_scoring_matrix(pack, bundle)
        row = query_scoring_matrix(matrix, "SCR-001")
        self.assertEqual(row["response_status"], "PLANNED")
        self.assertEqual(row["mapping_source"], "requirement_link")

    def test_coverage_summary_counts_all_statuses(self) -> None:
        matrix = self._matrix_with([
            {"scoring_item_id": "SCR-001", "label": "投诉受理与回访闭环机制（10分）", "must_respond": True},
            {"scoring_item_id": "SCR-002", "label": "投标报价（15分）", "must_respond": True},
            {"scoring_item_id": "SCR-003", "label": "区块链存证服务专项（10分）", "must_respond": False},
        ])
        summary = matrix["coverage_summary"]
        self.assertEqual(summary["scoring_derived_planned"], 1)
        self.assertEqual(summary["scoring_out_of_scope"], 1)
        self.assertEqual(summary["scoring_missing"], 1)
        self.assertEqual(summary["must_respond_planned"], 1)


class HierarchyArtifactIntegration(unittest.TestCase):
    def test_attach_writes_08_09_10_artifacts(self) -> None:
        from planning.hierarchy import attach_hierarchy_artifacts

        pack = _pack()
        bundle = _bundle(pack)
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "tender").mkdir()
            (run_dir / "tender" / "requirement_pack.json").write_text(
                json.dumps(pack, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            written = attach_hierarchy_artifacts(run_dir, bundle)
            self.assertIsNotNone(written)
            for key in ("hierarchy", "scoring_matrix", "lineage"):
                self.assertTrue(written[key].is_file(), key)
            hierarchy = json.loads(written["hierarchy"].read_text(encoding="utf-8"))
            matrix = json.loads(written["scoring_matrix"].read_text(encoding="utf-8"))
            lineage = json.loads(written["lineage"].read_text(encoding="utf-8"))
            self.assertEqual(hierarchy["validation"]["status"], "PASS")
            self.assertEqual(matrix["coverage_summary"]["scoring_total"], 2)
            self.assertIn("requirement_fingerprint", lineage)

    def test_attach_skips_without_requirement_pack(self) -> None:
        from planning.hierarchy import attach_hierarchy_artifacts

        bundle = _bundle()
        with tempfile.TemporaryDirectory() as tmp:
            result = attach_hierarchy_artifacts(Path(tmp), bundle)
            self.assertIsNone(result)

    def test_write_plan_artifacts_emits_incremental_files(self) -> None:
        from longform.orchestrator import write_plan_artifacts

        pack = _pack()
        bundle = _bundle(pack)
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "tender").mkdir()
            (run_dir / "tender" / "requirement_pack.json").write_text(
                json.dumps(pack, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            written = write_plan_artifacts(run_dir, bundle)
            for name in ("01_word_document_plan.json", "07_cross_section_dependency.json",
                         "08_document_hierarchy.json", "09_scoring_matrix.json", "10_hierarchy_lineage.json"):
                self.assertIn(name, {path.name for path in written.values()})


if __name__ == "__main__":
    unittest.main()
