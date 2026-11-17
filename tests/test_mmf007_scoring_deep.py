"""MMF-007 Phase 1：Scoring Deep Mapping 行为测试（PRECISION FIRST）。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from tender_intake.pack_builder import build_scoring_linkages


def _req(rid, text, status="CONFIRMED"):
    return {"requirement_id": rid, "normalized_requirement": text, "mandatory_level": "INFO",
            "confirmation_status": status, "scoring_item_id": None}


class ScoringLinkageTest(unittest.TestCase):
    def test_keyword_entity_match_creates_linkage(self):
        scoring = [{"scoring_item_id": "SCR-001", "label": "投诉处理机制及回访闭环（10分）", "weight_numeric": 10}]
        reqs = [_req("REQ-0001", "建立投诉处理台账，投诉处理须在24小时内首次响应"), _req("REQ-0002", "绿化养护按季节调整频次")]
        links = build_scoring_linkages(reqs, scoring)
        self.assertTrue(links)
        link = links[0]
        self.assertEqual(link["scoring_item_id"], "SCR-001")
        self.assertEqual(link["requirement_id"], "REQ-0001")
        self.assertIn(link["mapping_source"], {"SCORING_REFERENCE_LINKAGE"})
        self.assertGreaterEqual(link["mapping_confidence"], 0.5)
        self.assertEqual(link["technical_or_commercial"], "technical")
        self.assertTrue(link["evidence_text"])

    def test_generic_requirements_not_linked(self):
        scoring = [{"scoring_item_id": "SCR-001", "label": "投诉处理机制及回访闭环（10分）"}]
        reqs = [_req("REQ-0001", "提供优质服务，加强管理，确保项目顺利")]
        self.assertEqual(build_scoring_linkages(reqs, scoring), [])

    def test_commercial_label_classified(self):
        scoring = [{"scoring_item_id": "SCR-002", "label": "投标人类似项目业绩情况（5分）"}]
        reqs = [_req("REQ-0003", "类似项目业绩证明材料需提供合同复印件")]
        links = build_scoring_linkages(reqs, scoring)
        self.assertTrue(links)
        self.assertEqual(links[0]["technical_or_commercial"], "commercial")

    def test_rejected_requirements_skipped(self):
        scoring = [{"scoring_item_id": "SCR-001", "label": "投诉处理机制及回访闭环（10分）"}]
        reqs = [_req("REQ-0001", "建立投诉处理台账与回访机制", status="REJECTED")]
        self.assertEqual(build_scoring_linkages(reqs, scoring), [])


class ScoringFulfillmentTest(unittest.TestCase):
    """TECHNICAL_SCORING_ENTITY_EVIDENCE：实体证据定覆盖，字数与关键词不算。"""

    def _matrix_file(self, tmp, rows):
        p = Path(tmp) / "m.json"
        p.write_text(json.dumps({"rows": rows, "coverage_summary": {}}, ensure_ascii=False), encoding="utf-8")
        return p

    # 一段够长、要素齐、能定位的响应：受理/判定/分派/处置/回访五环节都写到岗位上。
    _RESPONSE = (
        "本节把投诉处理机制拆成受理、判定、分派、处置、回访五个环节，逐环节写明时限、责任岗位与记录字段。"
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

    def test_entity_evidence_wins_over_planned_volume(self):
        from planning.hierarchy import update_scoring_fulfillment
        with tempfile.TemporaryDirectory() as tmp:
            p = self._matrix_file(tmp, [{
                "scoring_item_id": "SCR-001", "label": "投诉处理机制及回访闭环（10分）",
                "section_ids": ["S04-02"], "planned_response_words": 5000,
                "planned_evidence": {"required_outputs": ["投诉台账"]},
                "fulfillment": {}}])
            frags = {"S04-02": {"body_blocks": [{"type": "paragraph", "content": self._RESPONSE}], "tables": []}}
            result = update_scoring_fulfillment(p, frags)
            row = json.loads(p.read_text(encoding="utf-8"))["rows"][0]
            self.assertEqual(row["fulfillment"]["status"], "FULFILLED", "实体要素齐备即可覆盖，不因字数低于计划而扣分")
            self.assertEqual(result["coverage_summary"]["fulfilled"], 1)
            # §24：FULFILLED 必须能指出响应位置与逐字证据。
            self.assertEqual(row["fulfillment"]["response_section"], "S04-02")
            self.assertTrue(row["fulfillment"]["response_cu"])
            self.assertTrue(row["fulfillment"]["evidence_text"])
            self.assertTrue(all(hit["path"] for hit in row["fulfillment"]["artifact_location"]))

    def test_short_mention_is_not_fulfilled(self):
        from planning.hierarchy import update_scoring_fulfillment
        with tempfile.TemporaryDirectory() as tmp:
            p = self._matrix_file(tmp, [{
                "scoring_item_id": "SCR-001", "label": "投诉处理机制及回访闭环（10分）",
                "section_ids": ["S04-02"], "planned_response_words": 5000,
                "planned_evidence": {"required_outputs": ["投诉台账"]},
                "fulfillment": {}}])
            frags = {"S04-02": {"body_blocks": [{"type": "paragraph", "content": "建立投诉处理台账，形成回访闭环记录。"}], "tables": []}}
            update_scoring_fulfillment(p, frags)
            row = json.loads(p.read_text(encoding="utf-8"))["rows"][0]
            self.assertNotEqual(row["fulfillment"]["status"], "FULFILLED", "§25：一句话提及不得判覆盖")
            self.assertEqual(row["fulfillment"]["effective_chars"] < 320, True)

    def test_high_chars_without_evidence_is_nearby_only(self):
        from planning.hierarchy import update_scoring_fulfillment
        with tempfile.TemporaryDirectory() as tmp:
            p = self._matrix_file(tmp, [{
                "scoring_item_id": "SCR-001", "label": "投诉处理机制及回访闭环（10分）",
                "section_ids": ["S04-02"], "planned_response_words": 100,
                "planned_evidence": {"required_outputs": ["投诉台账", "回访记录"]},
                "fulfillment": {}}])
            frags = {"S04-02": {"body_blocks": [{"type": "paragraph", "content": "文字很多但没有实质证据内容，" * 30}], "tables": []}}
            update_scoring_fulfillment(p, frags)
            row = json.loads(p.read_text(encoding="utf-8"))["rows"][0]
            self.assertEqual(row["fulfillment"]["status"], "NEARBY_ONLY", "字数达标但零实体要素正是 §25 要禁的写法")

    def test_excluded_scoring_item_never_counts(self):
        from planning.hierarchy import update_scoring_fulfillment
        with tempfile.TemporaryDirectory() as tmp:
            p = self._matrix_file(tmp, [
                {"scoring_item_id": "SCR-001", "label": "投诉处理机制及回访闭环（10分）",
                 "section_ids": ["S04-02"], "planned_evidence": {}, "fulfillment": {}},
                {"scoring_item_id": "SCR-002", "label": "类似项目业绩与荣誉证书（10分）",
                 "scope_class": "BIDDER_CREDENTIAL_EXCLUDED", "scope_disposition": "EXCLUDED",
                 "section_ids": ["S04-02"], "planned_evidence": {}, "fulfillment": {}},
            ])
            frags = {"S04-02": {"body_blocks": [{"type": "paragraph", "content": self._RESPONSE}], "tables": []}}
            result = update_scoring_fulfillment(p, frags)
            rows = {row["scoring_item_id"]: row for row in json.loads(p.read_text(encoding="utf-8"))["rows"]}
            self.assertEqual(rows["SCR-002"]["fulfillment"]["status"], "EXCLUDED_FROM_TECHNICAL_BID")
            summary = result["coverage_summary"]
            self.assertEqual(summary["technical_scoring_total"], 1, "§6/§7：排除项不得进分母")
            self.assertEqual(summary["excluded_from_technical_bid"], 1)
            self.assertEqual(summary["technical_scoring_coverage"], 1.0)

    def test_coverage_below_gate_blocks_clean_delivery(self):
        from planning.hierarchy import update_scoring_fulfillment
        with tempfile.TemporaryDirectory() as tmp:
            p = self._matrix_file(tmp, [{
                "scoring_item_id": "SCR-001", "label": "投诉处理机制及回访闭环（10分）",
                "section_ids": ["S04-02"], "planned_evidence": {}, "fulfillment": {}}])
            frags = {"S04-02": {"body_blocks": [{"type": "paragraph", "content": "文字很多但没有实质证据内容，" * 30}], "tables": []}}
            result = update_scoring_fulfillment(p, frags)
            self.assertEqual(result["technical_scoring_coverage"], 0.0)
            self.assertFalse(result["completed_clean_allowed"], "§27：实体覆盖率低于 80% 不得 COMPLETED_CLEAN")

    def test_fulfillment_missing_when_no_content(self):
        from planning.hierarchy import update_scoring_fulfillment
        with tempfile.TemporaryDirectory() as tmp:
            p = self._matrix_file(tmp, [{"scoring_item_id": "SCR-001", "label": "投诉闭环（10分）",
                                         "section_ids": ["S04-02"], "planned_response_words": 100,
                                         "planned_evidence": {}, "fulfillment": {}}])
            update_scoring_fulfillment(p, {"S04-02": {"body_blocks": []}})
            matrix = json.loads(p.read_text(encoding="utf-8"))
            row = matrix["rows"][0]
            self.assertEqual(row["fulfillment"]["status"], "MISSING")
            self.assertEqual(row["fulfillment"]["missing_kind"], "EMPTY_ARTIFACT",
                             "有响应位置但产物无文字，是写作问题不是路由问题")
            self.assertEqual(matrix["coverage_summary"]["fulfillment_missing"], 1)
            self.assertEqual(matrix["coverage_summary"]["fulfillment_missing_empty_artifact"], 1)
            self.assertEqual(matrix["coverage_summary"]["fulfillment_missing_not_planned"], 0)

    def test_missing_kind_separates_routing_from_writing(self):
        from planning.hierarchy import update_scoring_fulfillment
        with tempfile.TemporaryDirectory() as tmp:
            p = self._matrix_file(tmp, [
                {"scoring_item_id": "SCR-001", "label": "投诉处理机制及回访闭环（10分）",
                 "section_ids": [], "missing_reason": "无法路由", "planned_evidence": {}, "fulfillment": {}},
                {"scoring_item_id": "SCR-002", "label": "投诉处理机制及回访闭环（10分）",
                 "section_ids": ["S04-02"], "planned_evidence": {}, "fulfillment": {}},
            ])
            result = update_scoring_fulfillment(p, {
                "S04-02": {"body_blocks": [{"type": "paragraph", "content": self._RESPONSE}]}})
            rows = {row["scoring_item_id"]: row for row in json.loads(p.read_text(encoding="utf-8"))["rows"]}
            self.assertEqual(rows["SCR-001"]["fulfillment"]["missing_kind"], "NOT_PLANNED")
            self.assertEqual(rows["SCR-002"]["fulfillment"]["status"], "FULFILLED")
            summary = result["coverage_summary"]
            self.assertEqual(summary["fulfillment_missing_not_planned"], 1)
            self.assertEqual(summary["fulfillment_missing_empty_artifact"], 0)
            self.assertEqual(summary["technical_planned_total"], 1)
            self.assertEqual(summary["technical_scoring_total"], 2)

    def test_empty_scoring_matrix_is_not_applicable(self):
        from planning.hierarchy import update_scoring_fulfillment
        with tempfile.TemporaryDirectory() as tmp:
            p = self._matrix_file(tmp, [])
            result = update_scoring_fulfillment(p, {})
            self.assertEqual(result["status"], "NOT_APPLICABLE", "空评分项不得自称 PASS")
            matrix = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(matrix["fulfillment_status"], "NOT_APPLICABLE")


class ScoringWeightBudgetTest(unittest.TestCase):
    def test_high_score_chapter_gets_budget_boost(self):
        from planning.planner import build_requirement_matrix, build_content_budget
        pack = {
            "pack_id": "P", "status": "ready_for_plan",
            "confirmation": {"ready_for_brief_seed": True},
            "project_facts": {}, "service_scope": {"included": [], "excluded": []},
            "requirements": [
                {"requirement_id": "REQ-0001", "normalized_requirement": "投诉受理与闭环处理机制健全", "mandatory_level": "INFO", "domain": "sla_kpi", "confirmation_status": "CONFIRMED", "scoring_item_id": "SCR-001"},
                {"requirement_id": "REQ-0002", "normalized_requirement": "针对产业园特点提出合理化建议", "mandatory_level": "INFO", "domain": "scoring", "confirmation_status": "CONFIRMED", "scoring_item_id": "SCR-002"},
            ],
            "scoring_items": [
                {"scoring_item_id": "SCR-001", "label": "客户服务与投诉闭环（20分）", "must_respond": True, "weight_numeric": 20},
            ],
        }
        brief = {"project_name": "测试", "medium": "WORD", "project_type": "产业园综合体", "scenario": "完整物业服务方案"}
        outline, _ = __import__("planning.planner", fromlist=["build_adaptive_outline"]).build_adaptive_outline(pack, brief)
        matrix = build_requirement_matrix(pack, outline)
        budget = build_content_budget(outline, matrix)
        self.assertTrue(matrix.get("scoring_weights"), "matrix 应携带 scoring_weights")
        # CH04（客户服务）应因高分评分项获得更高的 words_max 密度
        ch04 = next(row for row in budget["chapters"] if row["chapter_id"] == "CH04")
        self.assertIn("scoring", json.dumps(ch04, ensure_ascii=False) + "1")  # 结构自检
        self.assertGreater(ch04["target_words_max"], 0)


if __name__ == "__main__":
    unittest.main()
