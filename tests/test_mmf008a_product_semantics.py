"""MMF008A 反例：二维评分语义 / 五类承诺来源（008A2 增加现场拓扑断言一族） / 客户可读文案。

Run F（build `MMF006E-R2.7-49aac1cc4a35-20260926161417`，HEAD `7e242cc6`）在 292 张技术页上
判出 `technical_scoring_coverage=0.1818` 与 11 条交付阻断项。本轮的前提是：那 11 条里有一批
**不是写作质量缺陷，而是产品语义错位** ——

① 评分归属只有一维（`TECHNICAL_INCLUDED → TECHNICAL_SUBSTANTIVE`），于是"证明材料在资格文件里"
   被记成技术正文欠账；
② 承诺来源门只看短语，于是"拟/如/甲方批准/定点值守的班表怎么排"这类正当设计被当成既存事实主张；
③ 客户成品里进了"本章先把可以写进正文的数与只能写成条件的做法分开"这种**内部推理**。

所以这个文件必须同时含两类条目："仍必须 BLOCK 的真实编造"与"不该再被误伤的正当技术设计"。
只有一类的测试等于没测：全是前者说明我只会加门，全是后者说明我在把门禁改绿。

语料取自 Run F 真实落盘文件（只读抄录，不重跑生成、零 Provider 调用）：
  `runF_snapshot_final/09_scoring_matrix.json`            —— 11 条 canonical 主题原文；
  `runF_snapshot_final/artifact_qa_report.json`           —— 元叙述句原文；
  `runF_snapshot_final/commitment_provenance_report.json` —— C2/C5 真实命中句；
逐条 fixture 的出处标在条目上。取证读数在
`MMF_Project_Records/MMF-007/04_Real_Run_Evidence/MMF008A_20260927/`。
"""
from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from governance.artifact_qa import evaluate_artifact  # noqa: E402
from governance.client_facing_prose import (  # noqa: E402
    CLIENT_FACING_PROSE_RULE_ID,
    INTERNAL_NARRATION_SHAPES,
    client_facing_prose,
    internal_narration_findings,
)
from governance.commitment_provenance import (  # noqa: E402
    HARD_BLOCK_PROVENANCE_CLASS,
    PROVENANCE_SEMANTIC_CLASSES,
    classify_provenance_semantics,
    evaluate_commitments,
)
from governance.customer_hygiene import apply_hygiene_tree  # noqa: E402
from governance.final_artifact_qa import RECOVERABLE_RULES  # noqa: E402
from longform.run_state import _scoring_gate_facts  # noqa: E402
from planning.hierarchy import update_scoring_fulfillment  # noqa: E402
from planning.scoring_evidence import judge_scoring_row  # noqa: E402
from planning.scoring_responsiveness import (  # noqa: E402
    CROSS_FILE_PROTECTED_EVIDENCE,
    WHOLE_DOCUMENT_RUBRIC,
    CONTRACTUAL_CONTEXT,
    HEADER_FORMULA_NON_RESPONSE,
    RESPONSIVENESS_VERSION,
    TECHNICAL_BODY_ANONYMOUS_STRUCTURE,
    TECHNICAL_BODY_DIRECT,
    record_requires_body_response,
    responsiveness_fields,
)
from planning.scoring_taxonomy import (  # noqa: E402
    COVERAGE_DENOMINATOR,
    COVERAGE_DENOMINATOR_ALL_CLASSES,
    COVERAGE_DENOMINATOR_LEGACY_LABEL,
    evidence_contract_for,
    pre_generation_scoring_mapping_gate,
)

# Run F 全部 11 条 canonical 技术评分主题（原文逐字），与本轮期望的第二维类别。
# 期望值不是"我希望的分类"，而是 `MMF008A_20260927/mmf008a_runF_semantic_replay.json`
# 里 `per_topic[]` 的实测读数：类别、命中标记词、是否进账三项一起钉住，
# 下一次有人改动词表让某条主题换类，这里会红，而且红得说得出是哪条、因为哪个词。
RUN_F_TOPICS: tuple[dict[str, object], ...] = (
    {
        "id": "SCN-001", "scoring_class": "TECHNICAL_SUBSTANTIVE",
        "label": "3 | 技术部分（40分） | 项目管理团队（8分） | 根据投标人拟投入本项目的管理团队"
                  "人员构成情况，综合考虑人员素质、人员结构及相关经验等，进行评分",
        "expected": TECHNICAL_BODY_ANONYMOUS_STRUCTURE, "expected_source": "ANONYMOUS_ORG_DESIGN_TOPIC",
        "counts": True, "marker": "人员构成",
        "why": "拟投入本项目的管理团队**怎么搭**是技术设计；个人资历才是别的文件里的事",
    },
    {
        "id": "SCN-002", "scoring_class": "TECHNICAL_SUBSTANTIVE",
        "label": "技术部分须提交项目应急预案，评分要求整体方案设计规范合理、可操作性强、"
                 "符合实际要求、项目落地容易、部署简单、工作迅速",
        "expected": TECHNICAL_BODY_DIRECT, "expected_source": "NO_MATTER_NOUN_ASSUMED_GENERATABLE",
        "counts": True, "marker": "应急预案",
        "why": "评价口径（可操作性强/落地容易）不该单独构成事项，但应急预案本身必须正文回答；"
               "名词切不出来时**默认要写**，不默认豁免",
    },
    {
        "id": "SCN-003", "scoring_class": "TECHNICAL_SUBSTANTIVE",
        "label": "组织架构中应明确列出投标人每个管理成员在项目中的角色、参与阶段、参与时间"
                 "和从事的工作内容",
        "expected": TECHNICAL_BODY_ANONYMOUS_STRUCTURE, "expected_source": "ANONYMOUS_ORG_DESIGN_TOPIC",
        "counts": True, "marker": "参与阶段",
        "why": "匿名岗位/角色/阶段/工作内容是技术正文可写结构，不要求身份材料",
    },
    {
        "id": "SCN-004", "scoring_class": "BIDDER_CREDENTIAL",
        "label": "为省级政府部门、省级银行提供办公楼物业服务项目的类似物业项目的物业服务经验",
        "expected": CROSS_FILE_PROTECTED_EVIDENCE, "expected_source": "PROOF_ONLY_TOPIC",
        "counts": False, "marker": "物业服务经验",
        "why": "这条要的是**业绩证明材料**，在资格/商务文件里；技术正文没有它不是欠账",
    },
    {
        "id": "SCN-005", "scoring_class": "TECHNICAL_SUBSTANTIVE",
        "label": "依据乙方投标书承诺的物业管理结构配置、管理模式、操作程序及规范",
        "expected": TECHNICAL_BODY_DIRECT, "expected_source": "TECHNICAL_MATTER_NOUN",
        "counts": True, "marker": "管理模式",
        "why": "管理模式与操作程序是任务书点名的可生成正文类",
    },
    {
        "id": "SCN-006", "scoring_class": "COMMERCIAL_TERMS",
        "label": "合同期内出现下列情形之一的，招标方有权终止物业合同或不再续签下一年度合同",
        "expected": CONTRACTUAL_CONTEXT, "expected_source": "CONTRACT_OR_PROCEDURE_CLAUSE",
        "counts": False, "marker": "有权终止",
        "why": "合同约束语，不是投标人能在正文里「响应」的事项",
    },
    {
        "id": "SCN-007", "scoring_class": "TECHNICAL_CONTEXTUAL",
        "label": "技术及服务部分 |",
        "expected": HEADER_FORMULA_NON_RESPONSE, "expected_source": "VOLUME_HEADING_WITHOUT_MATTER",
        "counts": False, "marker": "技术及服务部分",
        "why": "部次标题；剥掉分值后剩下的只有「部分」两个字",
    },
    {
        "id": "SCN-008", "scoring_class": "TECHNICAL_SUBSTANTIVE",
        "label": "综合考虑人员素质、人员结构及相关经验等，进行评分",
        "expected": TECHNICAL_BODY_ANONYMOUS_STRUCTURE, "expected_source": "ANONYMOUS_ORG_DESIGN_TOPIC",
        "counts": True, "marker": "人员结构",
        "why": "与 SCN-001 同一条评分因素的去重变体：仍要正文回答岗位结构",
    },
    {
        "id": "SCN-009", "scoring_class": "TECHNICAL_SUBSTANTIVE",
        "label": "根据是否覆盖所有物业需求，内容结构是否清晰详实合理",
        "expected": WHOLE_DOCUMENT_RUBRIC, "expected_source": "WHOLE_DOCUMENT_QUALITY_RUBRIC",
        "counts": False, "marker": "内容结构",
        "why": "整本质量口径由全篇 QA 量；把这句原话塞进某个内容单元就是自证式凑覆盖",
    },
    {
        "id": "SCN-010", "scoring_class": "TECHNICAL_SUBSTANTIVE",
        "label": "针对各岗位人员的保障方案和培训计划，视方案是否完备、科学、合理可行",
        "expected": TECHNICAL_BODY_DIRECT, "expected_source": "TECHNICAL_MATTER_NOUN",
        "counts": True, "marker": "培训计划",
        "why": "培训保障是任务书点名的可生成正文类",
    },
    {
        "id": "SCN-011", "scoring_class": "TECHNICAL_SUBSTANTIVE",
        "label": "针对本项目提出的管理建议，横向比较，优得",
        "expected": TECHNICAL_BODY_DIRECT, "expected_source": "TECHNICAL_MATTER_NOUN",
        "counts": True, "marker": "管理建议",
        "why": "管理建议是内容，不是口径",
    },
)

BY_ID = {str(row["id"]): row for row in RUN_F_TOPICS}


def _fields(topic_id: str, *, scoring_class: str = "") -> dict:
    # 主断言一律只看招标原话这一行文本（`scoring_class=""`）：第一维归类只是加速器，不是凭据。
    # 把凭据压在"两维组合"上，第一维改分类会把第二维一起带走。
    return responsiveness_fields(str(BY_ID[topic_id]["label"]), scoring_class=scoring_class)


class A1RunFSemanticClassification(unittest.TestCase):
    """§A：第二维"评分归属 × 技术正文可响应性"必须按语义分，且逐条可指认。"""

    def test_run_f_topics_match_measured_readings(self):
        for row in RUN_F_TOPICS:
            with self.subTest(topic=row["id"], why=row["why"]):
                fields = _fields(str(row["id"]))
                self.assertEqual(fields["responsiveness_class"], row["expected"])
                self.assertEqual(fields["responsiveness_class_source"], row["expected_source"])
                self.assertEqual(fields["counts_toward_technical_fulfillment"], row["counts"])
                self.assertEqual(fields["responsiveness_class_version"], RESPONSIVENESS_VERSION)
                evidence = json.dumps(fields["responsiveness_evidence"], ensure_ascii=False)
                self.assertIn(str(row["marker"]), evidence,
                              f"{row['id']} 换了类别却没留下命中标记词，等于无凭据改判")

    def test_first_dimension_routes_are_labelled_separately(self):
        # 第一维已经定性的行，第二维照单收下，但**来源必须写清是 DIM1**：两条路进同一类可以，
        # 混成一条路不行。走 DIM1 这条路进来的行同样要留下"保护哪些词"。
        credential = _fields("SCN-004", scoring_class="BIDDER_CREDENTIAL")
        self.assertEqual(credential["responsiveness_class"], CROSS_FILE_PROTECTED_EVIDENCE)
        self.assertEqual(credential["responsiveness_class_source"], "DIM1_CREDENTIAL_CLASS")
        self.assertIn("物业服务经验", credential["protected_evidence_terms"])
        excluded = _fields("SCN-006", scoring_class="COMMERCIAL_TERMS")
        self.assertEqual(excluded["responsiveness_class"], CONTRACTUAL_CONTEXT)
        self.assertEqual(excluded["responsiveness_class_source"], "DIM1_CONTRACT_CLASS")
        self.assertFalse(credential["may_bind_technical_response_cu"])
        self.assertFalse(excluded["counts_toward_technical_fulfillment"])

    def test_six_categories_exist_and_only_two_count(self):
        names = {str(_fields(str(row["id"]))["responsiveness_class"]) for row in RUN_F_TOPICS}
        for required in (TECHNICAL_BODY_DIRECT, TECHNICAL_BODY_ANONYMOUS_STRUCTURE,
                         CROSS_FILE_PROTECTED_EVIDENCE, WHOLE_DOCUMENT_RUBRIC,
                         CONTRACTUAL_CONTEXT, HEADER_FORMULA_NON_RESPONSE):
            self.assertIn(required, names)
        # 离开分母只允许这三类，且每一类都由正面凭据（标记词）支持，不是"看着不像技术"。
        leaving = {str(_fields(str(item))["responsiveness_class"])
                   for item in BY_ID if not BY_ID[item]["counts"]}
        self.assertEqual(
            leaving,
            {CROSS_FILE_PROTECTED_EVIDENCE, WHOLE_DOCUMENT_RUBRIC,
             CONTRACTUAL_CONTEXT, HEADER_FORMULA_NON_RESPONSE})

    def test_credential_evidence_may_not_be_pulled_into_technical_body(self):
        fields = _fields("SCN-004")
        self.assertFalse(fields["may_bind_technical_response_cu"])
        self.assertFalse(fields["counts_toward_technical_fulfillment"])
        self.assertIn("物业服务经验", fields["protected_evidence_terms"])
        contract = evidence_contract_for(
            str(BY_ID["SCN-004"]["label"]), {"tables": ["类似项目经验证明材料"]},
            responsiveness_class=CROSS_FILE_PROTECTED_EVIDENCE)
        self.assertFalse(contract["identity_material_required"])
        self.assertNotIn("姓名", contract["concepts"])
        self.assertNotIn("身份证", contract["concepts"])
        # 反向短路：往技术正文里拽证明材料这件事本身要被命名。
        self.assertIn("PULL_PROOF_MATERIAL_INTO_TECHNICAL_BODY",
                      json.dumps(contract, ensure_ascii=False))

    def test_personnel_items_are_not_blanket_excluded(self):
        # 任务书禁止"把所有人员相关项一律 EXCLUDED"：三条人员主题全部留在分母里。
        for topic_id in ("SCN-001", "SCN-003", "SCN-008"):
            with self.subTest(topic=topic_id):
                fields = _fields(topic_id)
                self.assertTrue(fields["counts_toward_technical_fulfillment"])
                self.assertTrue(fields["identity_material_not_required"])
                self.assertEqual(fields["responsiveness_class"], TECHNICAL_BODY_ANONYMOUS_STRUCTURE)

    def test_resume_only_topic_is_identity_material_not_a_body_contract(self):
        # 「人员」紧挨履历时不是人员构成。留在正文分母会把履历词从证据合同里剔除，
        # 要素表变空，生成前门以缺 EVIDENCE_CONTRACT 挡住整场写稿。
        label = "投标公司派驻青岛海信国际中心项目人员履历情况"
        fields = responsiveness_fields(label, scoring_class="TECHNICAL_SUBSTANTIVE")
        self.assertEqual(fields["responsiveness_class"], CROSS_FILE_PROTECTED_EVIDENCE)
        self.assertEqual(fields["responsiveness_class_source"], "IDENTITY_MATERIAL_ONLY_TOPIC")
        self.assertIn("履历", fields["protected_evidence_terms"])
        self.assertFalse(fields["counts_toward_technical_fulfillment"])
        record = {
            "canonical_scoring_id": "SCN-RESUME",
            "topic": label,
            "scoring_class": "TECHNICAL_SUBSTANTIVE",
            **fields,
            "evidence_contract": {"concepts": {}, "min_concepts": 0},
            "planned_response_section": "S01-01",
            "planned_response_sections": ["S01-01"],
            "planned_response_cu": ["CU-S01-01-01"],
            "mapping_basis": "REQUIREMENT_REF_LINK",
        }
        gate = pre_generation_scoring_mapping_gate(
            {"canonical_scoring_records": [record], "rows": []},
            planned_unit_ids=["CU-S01-01-01"],
            planned_section_ids=["S01-01"],
        )
        self.assertTrue(gate["may_enter_generation"])
        self.assertEqual(gate["unmapped_total"], 0)
        mixed = responsiveness_fields(
            "驻场人员配置方案及项目经理履历", scoring_class="TECHNICAL_SUBSTANTIVE")
        self.assertTrue(mixed["counts_toward_technical_fulfillment"])
        short = responsiveness_fields(
            "投标公司派驻项目人员履历情况", scoring_class="TECHNICAL_SUBSTANTIVE")
        self.assertEqual(short["responsiveness_class"], CROSS_FILE_PROTECTED_EVIDENCE)

    def test_anonymous_org_design_does_not_require_identity_materials(self):
        contract = evidence_contract_for(
            str(BY_ID["SCN-003"]["label"]), {"tables": ["组织架构与岗位职责表"]},
            responsiveness_class=TECHNICAL_BODY_ANONYMOUS_STRUCTURE)
        self.assertFalse(contract["identity_material_required"])
        # 匿名组织设计必须落在结构化载体上（岗位/角色/阶段表），一段散文不算回答。
        self.assertTrue(contract["require_structured"])
        blob = json.dumps(contract, ensure_ascii=False)
        for forbidden in ("身份证", "社保证明", "劳动合同", "学历证书"):
            self.assertNotIn(forbidden, blob)

    def test_whole_document_rubric_is_judged_by_document_qa_not_forced_into_a_cu(self):
        fields = _fields("SCN-009")
        self.assertEqual(fields["responsiveness_class"], WHOLE_DOCUMENT_RUBRIC)
        self.assertEqual(
            fields["judged_by_whole_document_dimensions"],
            ["REQUIREMENT_COVERAGE", "SCORING_COVERAGE", "STRUCTURED_SHARE", "ARTIFACT_INTEGRITY"])
        self.assertFalse(fields["may_bind_technical_response_cu"])


class A2DenominatorHonesty(unittest.TestCase):
    """§A 的"不得硬改分母"：漏标算要写、旧字面量算没量、离开分母必须逐条留账。"""

    def test_missing_annotation_defaults_to_required(self):
        # 没有第二维字段的旧产物/手写夹具不能靠"没标"整批掉出分母。
        self.assertTrue(record_requires_body_response({}))
        self.assertTrue(record_requires_body_response({"topic": "任意主题"}))
        self.assertFalse(record_requires_body_response(
            {"responsiveness_class": CROSS_FILE_PROTECTED_EVIDENCE,
             "counts_toward_technical_fulfillment": False}))
        # 只标类别、不标布尔值时也必须按类别算，两处口径不能互相矛盾。
        self.assertFalse(record_requires_body_response(
            {"responsiveness_class": CROSS_FILE_PROTECTED_EVIDENCE}))
        self.assertTrue(record_requires_body_response(
            {"responsiveness_class": TECHNICAL_BODY_DIRECT}))

    def test_three_ruler_literals_are_distinct(self):
        # 换分母必须让"用旧尺子量的读数"变成 NOT_MEASURED，而不是继续沿用。
        self.assertEqual(COVERAGE_DENOMINATOR, "UNIQUE_TECHNICAL_RESPONDABLE_TOPICS")
        self.assertEqual(COVERAGE_DENOMINATOR_LEGACY_LABEL, "UNIQUE_TECHNICAL_SUBSTANTIVE_TOPICS")
        self.assertEqual(COVERAGE_DENOMINATOR_ALL_CLASSES,
                         "UNIQUE_TECHNICAL_SUBSTANTIVE_TOPICS_ALL_RESPONSE_CLASSES")
        self.assertEqual(len({COVERAGE_DENOMINATOR, COVERAGE_DENOMINATOR_LEGACY_LABEL,
                              COVERAGE_DENOMINATOR_ALL_CLASSES}), 3,
                         "三把尺必须各有名字：共用名字等于并列 reporting 出来的两个假数")


class A3MappingGateLeaks(unittest.TestCase):
    """§A：非响应主题一旦被派生成技术正文落点，就是"为了覆盖率塞材料"的泄漏。"""

    @staticmethod
    def _matrix(records: list[dict], rows: list[dict] | None = None) -> dict:
        return {"schema_version": "scoring-matrix-v0.3",
                "rows": rows if rows is not None else [],
                "canonical_scoring_records": records,
                "coverage_summary": {"canonical_scoring_total": len(records),
                                     "coverage_denominator": COVERAGE_DENOMINATOR}}

    @staticmethod
    def _record(topic_id: str, **over) -> dict:
        record = {"canonical_scoring_id": topic_id,
                  "topic": str(BY_ID[topic_id]["label"]),
                  "scoring_class": "TECHNICAL_SUBSTANTIVE",
                  "source_ids": [f"SCR-{topic_id[-3:]}"],
                  "planned_response_section": None, "planned_response_sections": [],
                  "planned_response_cu": [], "cu_binding": {"basis": "NOT_APPLICABLE"},
                  "evidence_contract": {"concepts": [str(BY_ID[topic_id]["marker"])],
                                        "min_concepts": 1},
                  **_fields(topic_id)}
        record.update(over)
        return record

    def test_derived_cu_for_non_respondable_topic_blocks(self):
        leak = self._record(
            "SCN-004", planned_response_section="S08-01",
            planned_response_sections=["S08-01"], planned_response_cu=["CU-0801"],
            cu_binding={"basis": "DERIVED_TECHNICAL_RESPONSE_CU"},
            mapping_basis="DERIVED_TECHNICAL_RESPONSE_CU")
        gate = pre_generation_scoring_mapping_gate(self._matrix([leak]))
        self.assertEqual(gate["status"], "TECHNICAL_SCORING_LEAK")
        self.assertFalse(gate["may_enter_generation"])
        kinds = [str(row.get("leak_kind") or "") for row in gate["excluded_class_planned_leaks"]]
        self.assertIn("TECHNICAL_BODY_RESPONSE_DERIVED_FOR_NON_RESPONDABLE_TOPIC", kinds)
        # 泄漏的那一条不能同时从"谁离开了分母"的账上消失。
        self.assertIn("SCN-004", json.dumps(gate["non_respondable_registered"], ensure_ascii=False))

    def test_registered_non_respondable_topic_is_named_with_evidence(self):
        # 第二维把一条主题请出"正文实体响应"的账，必须逐条点名交人工复核：
        # 类别、命中标记词、由谁判、招标有没有写着必须响应 —— 少掉一条主题可以，少掉一条账不行。
        record = self._record("SCN-009")
        rows = [{"scoring_item_id": "SCR-009", "label": str(BY_ID["SCN-009"]["label"]),
                 "must_respond": True, "weight_if_known": 6,
                 "scoring_class": "TECHNICAL_SUBSTANTIVE", "scope_class": "TECHNICAL_INCLUDED"}]
        gate = pre_generation_scoring_mapping_gate(self._matrix([record], rows))
        registered = json.dumps(gate["non_respondable_registered"], ensure_ascii=False)
        self.assertIn("SCN-009", registered)
        self.assertIn("REQUIREMENT_COVERAGE", registered)
        watch = [row for row in gate["denominator_shrink_watch"]
                 if str(row.get("watch_axis") or "") == "RESPONSIVENESS_CLASS_SECOND_DIMENSION"]
        self.assertEqual([row["canonical_scoring_id"] for row in watch], ["SCN-009"])
        # 命中标记词整批留账，不只留一个：复核的人要能看见是"哪几个词"把这条请出分母的。
        self.assertEqual(watch[0]["matched_markers"]["whole_document"],
                         ["覆盖所有", "所有物业需求", "内容结构"])
        self.assertTrue(watch[0]["must_respond"])
        self.assertEqual(gate["denominator_shrink_watch_second_dimension_total"], 1)
        self.assertEqual(gate["rules"]["dimensions_forbidden_technical_response_cu"],
                         [CROSS_FILE_PROTECTED_EVIDENCE, WHOLE_DOCUMENT_RUBRIC,
                          CONTRACTUAL_CONTEXT, HEADER_FORMULA_NON_RESPONSE])


class A4FulfillmentRulers(unittest.TestCase):
    """§A 落到覆盖率上：三把尺并列 + 按维度读数 + 缺证明材料不阻断、缺技术正文阻断。

    这一组的关键是"谁进账由**正文里有没有**决定，不由类别名字决定"：夹具里
    `written` 是要素能在结构化载体上定位到的那几条，其余一律算没写。
    """

    @staticmethod
    def _marker(topic_id: str) -> str:
        return str(BY_ID[topic_id]["marker"])

    def _row(self, topic_id: str) -> dict:
        return {"scoring_item_id": f"SCR-{topic_id[-3:]}", "label": str(BY_ID[topic_id]["label"]),
                "must_respond": True, "scoring_class": "TECHNICAL_SUBSTANTIVE",
                "scope_class": "TECHNICAL_INCLUDED", "scope_disposition": "INCLUDED",
                "section_ids": ["S08-01"], "planned_response_cu": [f"CU-{topic_id[-3:]}"],
                "entity_contract": {"concepts": [self._marker(topic_id)], "min_concepts": 1,
                                    "require_structured": True},
                **_fields(topic_id)}

    def _record(self, topic_id: str) -> dict:
        return {"canonical_scoring_id": topic_id, "topic": str(BY_ID[topic_id]["label"]),
                "source_ids": [f"SCR-{topic_id[-3:]}"],
                "planned_response_section": "S08-01", "planned_response_sections": ["S08-01"],
                "planned_response_cu": [f"CU-{topic_id[-3:]}"],
                "cu_binding": {"basis": "EXPLICIT_SCORING_ID"},
                "evidence_contract": {"concepts": [self._marker(topic_id)], "min_concepts": 1,
                                      "require_structured": True},
                **_fields(topic_id)}

    def _terms(self, topic_id: str) -> list[str]:
        """这条主题"要在正文里看得见"的词。

        有具名 rubric 时按 rubric 给的那批同义词写 —— 判定端 `judge_scoring_row` 第一眼看
        的就是 rubric，夹具只写一个标记词就会得到 PARTIAL，那不是产品在挡，是夹具没写全。
        """
        from planning.scoring_evidence import rubric_for
        rubric = rubric_for(str(BY_ID[topic_id]["label"]))
        if rubric:
            return [str(term) for terms in rubric["concepts"].values() for term in terms]
        return [self._marker(topic_id)]

    def _read(self, topics: list[str], written: list[str]) -> dict:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "09_scoring_matrix.json"
            path.write_text(json.dumps({
                "schema_version": "scoring-matrix-v0.3",
                "rows": [self._row(topic) for topic in topics],
                "canonical_scoring_records": [self._record(topic) for topic in topics],
                "coverage_summary": {"canonical_scoring_total": len(topics),
                                     "coverage_denominator": COVERAGE_DENOMINATOR},
            }, ensure_ascii=False), encoding="utf-8")
            filler = "岗位与班次安排按现场条件执行，异常路径、记录与检查方法逐条列明，避免只写结论。"
            def body(topic_id: str) -> str:
                return "；".join(self._terms(topic_id)) + "：" + filler * 12
            fragments = {"S08-01": {
                # 一条 CU 要 320 字以上才算"够长的单元"（§25 的 NEARBY_ONLY 就是为此而设）：
                # 夹具如果短于这个门槛，测出来的"没覆盖"是夹具自己造的，不是产品判的。
                "content_unit_fragments": [
                    {"content_unit_id": f"CU-{topic[-3:]}", "archetype": "TABLE",
                     "content": body(topic)} for topic in written],
                "tables": [{"title": "岗位与职责安排表", "columns": ["事项", "安排"],
                            "rows": [[term, "按现场条件执行"] for term in self._terms(topic)]}
                           for topic in written]}}
            update_scoring_fulfillment(path, fragments)
            return json.loads(path.read_text(encoding="utf-8"))

    def test_three_rulers_are_written_by_the_same_pass(self):
        matrix = self._read(["SCN-001", "SCN-002", "SCN-004"], ["SCN-001"])
        gate, summary = matrix["scoring_gate"], matrix["coverage_summary"]
        self.assertEqual(gate["coverage_denominator"], COVERAGE_DENOMINATOR)
        self.assertEqual(summary["coverage_denominator_all_response_classes"],
                         COVERAGE_DENOMINATOR_ALL_CLASSES)
        self.assertEqual(gate["respondable_scoring_total"], 2)
        self.assertEqual(gate["technical_scoring_coverage"], 0.5)
        # 把主题移出"可响应口径"只会让那一个数变好，所以另两把必须并列报出来。
        self.assertLess(gate["all_response_classes_coverage"], 0.5)
        self.assertIsInstance(gate["legacy_row_coverage"], float)
        self.assertEqual(sorted(gate["coverage_by_response_dimension"]),
                         sorted([TECHNICAL_BODY_DIRECT, TECHNICAL_BODY_ANONYMOUS_STRUCTURE,
                                 CROSS_FILE_PROTECTED_EVIDENCE, WHOLE_DOCUMENT_RUBRIC,
                                 CONTRACTUAL_CONTEXT, HEADER_FORMULA_NON_RESPONSE]))

    def test_protected_evidence_topic_absent_from_body_does_not_block_delivery(self):
        # SCN-004 是证明材料主题：正文里没有它，不算技术成品欠账。
        gate = self._read(["SCN-001", "SCN-004"], ["SCN-001"])["scoring_gate"]
        self.assertTrue(gate["completed_clean_allowed"],
                        "唯一进账主题已满覆盖时，缺证明材料不得阻断交付")
        by_dimension = gate["coverage_by_response_dimension"]
        self.assertFalse(by_dimension[CROSS_FILE_PROTECTED_EVIDENCE]["counts_toward_gate"])
        self.assertTrue(by_dimension[TECHNICAL_BODY_ANONYMOUS_STRUCTURE]["counts_toward_gate"])
        self.assertEqual(by_dimension[CROSS_FILE_PROTECTED_EVIDENCE]["fulfilled"], 0)
        self.assertTrue(gate["proof_material_may_not_be_required_in_technical_body"])

    def test_genuine_technical_topic_still_required_to_be_fulfilled(self):
        # 反过来：可响应主题没写满就是没达标，不许借"第二维"放行。
        gate = self._read(["SCN-001", "SCN-002"], ["SCN-001"])["scoring_gate"]
        self.assertFalse(gate["completed_clean_allowed"])
        self.assertLess(gate["technical_scoring_coverage"], 1.0)

    def test_all_respondable_topics_fulfilled_is_clean(self):
        gate = self._read(["SCN-002", "SCN-010", "SCN-011"],
                          ["SCN-002", "SCN-010", "SCN-011"])["scoring_gate"]
        self.assertTrue(gate["completed_clean_allowed"])
        self.assertEqual(gate["technical_scoring_coverage"], 1.0)

    def test_no_respondable_topic_is_not_self_declared_pass(self):
        gate = self._read(["SCN-004", "SCN-009", "SCN-006"], [])["scoring_gate"]
        self.assertFalse(gate["completed_clean_allowed"])
        # Global QA remains a technical acceptance obligation even without a body CU.
        self.assertFalse(gate["no_respondable_technical_topic"])
        self.assertTrue(gate['no_body_response_topic'])
        self.assertEqual(gate['respondable_scoring_total'], 1)



class B1ProvenanceSemantics(unittest.TestCase):
    """§B：五类语义，只有"缺少当前项目来源的既存事实主张"仍然硬阻断。"""

    def test_five_classes_are_the_declared_vocabulary(self):
        self.assertEqual(
            set(PROVENANCE_SEMANTIC_CLASSES),
            {"EXISTING_FACT", "PROPOSED_SERVICE_METHOD", "CONDITIONAL_CONTROL",
             "FUTURE_COMMITMENT", "UNSUPPORTED_EXISTING_CLAIM"})
        self.assertEqual(HARD_BLOCK_PROVENANCE_CLASS, "UNSUPPORTED_EXISTING_CLAIM")

    def test_run_f_service_design_sentences_are_not_fact_claims(self):
        # 出处：Run F commitment_provenance_report.json → C2 真实命中句。
        cases = (
            ("岗位按定点值守、流动巡查、盯屏接单三种任务状态布置，三种状态共用同一份班表，"
             "交接时按状态整批移交", "C2", "PROPOSED_SERVICE_METHOD"),
            ("空间上按三类区别对待：主出入口与营业大厅按营业时间内重点部位布置定点值守，"
             "同步做好人流疏导", "C2", "PROPOSED_SERVICE_METHOD"),
            ("秩序维护七人编制下定点值守、流动巡查、盯屏接单三态班次的岗位移交与状态整批交接",
             "C2", "PROPOSED_SERVICE_METHOD"),
        )
        for text, claim_type, expected in cases:
            with self.subTest(text=text[:24]):
                semantics, basis, evidence = classify_provenance_semantics(
                    text, supported=False, claim_type=claim_type)
                self.assertEqual(semantics, expected)
                self.assertTrue(basis and evidence)

    def test_approval_control_sentences_are_not_fact_claims(self):
        # 出处：Run F commitment_provenance_report.json → C5 真实命中句。
        cases = (
            "甲方批准人与批准时间按甲方经办人签字的书面材料填写，未获批准不安排进场",
            "作业超出甲方批准范围或附加安全条件未确认的，当场暂停并撤离作业面",
            "以甲方批准的委托计划与许可为准",
        )
        for text in cases:
            with self.subTest(text=text[:24]):
                semantics, _basis, _evidence = classify_provenance_semantics(
                    text, supported=False, claim_type="C5")
                self.assertEqual(semantics, "CONDITIONAL_CONTROL")

    def test_real_fabrication_still_blocks(self):
        # 既存状态断言（"已建立…/均已…"）+ 缺少当前项目来源：这一类仍然是硬阻断。
        text = "本项目已建立封闭式管理体系，人员均已持证上岗"
        semantics, basis, _evidence = classify_provenance_semantics(
            text, supported=False, claim_type="C4")
        self.assertEqual(semantics, HARD_BLOCK_PROVENANCE_CLASS)
        self.assertEqual(basis, "ASSERTED_WITHOUT_CURRENT_PROJECT_SOURCE")

    def test_clause_without_provenance_evidence_defaults_to_block(self):
        # 分类拿不出正面凭据时**默认挡**，不默认放行：五类语义是把"看得懂是设计"的那批放走，
        # 不是给"看不懂的"发免检牌。
        semantics, basis, _evidence = classify_provenance_semantics(
            "本项目早已实行封闭式管理，门禁卡由我方统一制发", supported=False, claim_type="C4")
        self.assertEqual(semantics, HARD_BLOCK_PROVENANCE_CLASS)
        self.assertEqual(basis, "NO_PROVENANCE_SEMANTICS_EVIDENCE_IN_CLAUSE")

    def test_fabricated_existing_state_still_blocks_a_real_paragraph(self):
        # 检出口仍然认这一族关键词：把"封闭管理/门禁卡"写成这里**已经有**的安排、
        # 又拿不出当前项目来源，就仍然 BLOCK —— 五类语义放宽的是"怎么写"，不是"编造"。
        report = evaluate_commitments(
            {"project_name": "某某分行物业服务项目"}, [],
            {"artifact": {"paragraphs": [
                "本项目早已实行封闭式管理，门禁卡由我方统一制发，外来人员一律不得进入。"]}})
        violations = report.get("violations") or []
        self.assertTrue(violations, "编造的既存状态必须留在 violations")
        self.assertIn("封闭式管理", json.dumps(violations, ensure_ascii=False))
        self.assertEqual(report["status"], "BLOCK")

    def test_obligation_and_absolute_wording_block_first(self):
        # 两根阻断标尺（既存状态断言 / 绝对化义务）**排在未来语态之前**：
        # "确保零故障"虽然写的是今后的事，也不因为"拟/将"就变成安全。
        for text, claim_type in (("确保全年零故障运行", "C8"),
                                 ("所有隐患绝无可能再次发生", "C8"),
                                 ("巡查记录一律不得缺失", "C6")):
            with self.subTest(text=text):
                semantics, basis, _ = classify_provenance_semantics(
                    text, supported=False, claim_type=claim_type)
                self.assertEqual(semantics, HARD_BLOCK_PROVENANCE_CLASS)
                self.assertEqual(basis, "ASSERTED_WITHOUT_CURRENT_PROJECT_SOURCE")

    def test_completion_shape_blocks_by_definition(self):
        # 没有既存状态词、也没有绝对化副词，但形状本身是"完成/绝对"主张 → 按形状挡。
        semantics, basis, _ = classify_provenance_semantics(
            "全部记录将整理成册备查", supported=False, claim_type="C1",
            claim_family="COMPLETION_OR_ABSOLUTE")
        self.assertEqual(semantics, HARD_BLOCK_PROVENANCE_CLASS)
        self.assertEqual(basis, "COMPLETION_OR_ABSOLUTE_CLAIM_BY_DEFINITION")

    def test_prefix_alone_does_not_decide(self):
        # 「拟」字头 + 既存状态断言：一个拟字不能把已经说成事实的那半句洗白。
        text = "拟派管理团队已经全部到岗，人员社保与劳动合同均已核验完毕"
        semantics, basis, _ = classify_provenance_semantics(text, supported=False, claim_type="C2")
        self.assertEqual(semantics, HARD_BLOCK_PROVENANCE_CLASS)
        self.assertEqual(basis, "ASSERTED_WITHOUT_CURRENT_PROJECT_SOURCE")

    def test_supported_by_brief_is_existing_fact_not_a_violation(self):
        text = "本项目两处面积读数合计约3.6万平方米，实行封闭式管理"
        semantics, basis, _ = classify_provenance_semantics(
            text, supported=True, claim_type="C4")
        self.assertEqual(semantics, "EXISTING_FACT")
        self.assertEqual(basis, "BRIEF_SUPPORTS_PHRASE")


class B2GateStillHasTeeth(unittest.TestCase):
    """§B 不弱化：关键词不删、非阻断类逐条留痕、severity 不整体降档。"""

    BRIEF = {"project_name": "某某分行物业服务项目",
             "service_area": "两处面积读数合计约3.6万平方米"}
    FABRICATION = ("本项目早已实行封闭式管理，门禁卡由我方统一制发，"
                   "外来人员一律不得进入。")
    PARAGRAPHS = [
        "岗位按定点值守、流动巡查、盯屏接单三种任务状态布置，交接时按状态整批移交。",
        "甲方批准人与批准时间按甲方经办人签字的书面材料填写，未获批准不安排进场。",
        FABRICATION,
    ]

    def _report(self) -> dict:
        return evaluate_commitments(self.BRIEF, [], {"artifact": {"paragraphs": list(self.PARAGRAPHS)}})

    def test_keywords_are_still_detected(self):
        report = self._report()
        detected = {str(row.get("text")) for row in report.get("claims") or []}
        self.assertIn("定点值守", detected)
        self.assertIn("甲方批准", detected)
        # 关键词一个都没删：删掉关键词就等于把检出口一起关掉。

    def test_only_the_hard_class_blocks_delivery(self):
        report = self._report()
        violations = report.get("violations") or []
        self.assertTrue(violations, "真实编造句必须留在 violations，不能被语义分类吃掉")
        for row in violations:
            self.assertEqual(row["provenance_semantics"], HARD_BLOCK_PROVENANCE_CLASS)
            self.assertEqual(row["severity"], "BLOCK")
        self.assertEqual(report["delivery_blocking_only_for_class"], HARD_BLOCK_PROVENANCE_CLASS)
        self.assertIn("封闭式管理", json.dumps(violations, ensure_ascii=False),
                      "挡下来的必须就是那句编造，而不是别的什么")
        self.assertEqual(report["schema_version"], "commitment-provenance-gate-v0.3")

    def test_non_blocking_classes_are_recorded_not_deleted(self):
        report = self._report()
        suppressed = report.get("suppressed") or []
        semantics = {str(row.get("provenance_semantics")) for row in suppressed}
        self.assertIn("PROPOSED_SERVICE_METHOD", semantics)
        self.assertIn("CONDITIONAL_CONTROL", semantics)
        for row in suppressed:
            self.assertTrue(row.get("classification_reason"))
            self.assertTrue(row.get("provenance_semantics_basis"))
        counts = report["provenance_semantics_counts"]
        self.assertEqual(sum(counts.values()), len(report.get("claims") or []))

    def test_severity_is_not_globally_lowered(self):
        # 同一段文字里，编造句仍然 BLOCK：整体降档会在这一条上红。
        report = self._report()
        self.assertEqual(report["status"], "BLOCK")


class C1MetaNarration(unittest.TestCase):
    """§C：内部推理 → 自然招标语言。整句才动笔，风控内容一律放过。"""

    # 出处：Run F artifact_content.json / artifact_qa_report.json 逐字抄录。
    MUST_BE_NATURALIZED: tuple[tuple[str, str], ...] = (
        ("BODY_WRITABILITY_TUTORIAL",
         "本章先把\u201c可以写进正文的数\u201d与\u201c只能写成条件的做法\u201d分开。"),
        ("EVERY_FIGURE_HAS_A_SOURCE",
         "三者分列之后，正文中出现的每一个数字都有出处，每一条做法都有前提，"
         "评审与后续核查都能追溯到同一条依据。"),
        ("LAYERING_RULE_ANNOUNCEMENT",
         "据此本节把内容分三层并给出各自处置规则：已确认事实层收录项目名称、服务地点、"
         "标的类型、两处面积读数、合同期限与人员配置合计等文本载明项，直接引用不再讨论；"),
        ("EXCLUSION_REGISTER",
         "排除项与边界登记：本章只界定技术服务范围与规模事实，商务、资格、资信与投标程序类条款"
         "不在技术正文展开，包括报价表与价格构成、保证金与代理服务费、营业执照与资质信用认证证明、"
         "业绩与荣誉证明材料、授权委托书等格式文件、商务偏离表。"),
        ("NOT_LISTED_WITH_REASON",
         "因该部分属技术标正文排除的证明材料范围，且上下文包中尚未提供可核验的证据条目，"
         "本章不列具体项目名称、服务年限、面积规模与合同金额，也不以任何形式改写为业绩描述页或履历表。"),
        ("CATEGORY_BELONGING_EXPLAINED",
         "超出部分按范围政策只登记不展开，属于商务、报价、资格资信与投标程序性质的事项"
         "不进入技术标正文，也不为其生成说明段、清单或空白附件页。"),
        ("DEVELOPER_GOVERNANCE_VOCABULARY",
         "是否出现未经确认的历史数字、固定岗位承诺或真实案例描述，出现即整句删除"
         "并在登记单注明删除理由与所在章节"),
        ("TEMPLATE_AND_HISTORY_EXCLUSION",
         "边界之外还有两个明确排除项：与本项目业态不符的其他项目模板场景不写进本方案，"
         "未经确认的历史数字与固定岗位承诺不作为服务依据。"),
    )

    def test_each_shape_is_applied_and_output_is_client_language(self):
        replacements = {row["shape_id"]: str(row.get("replacement") or "")
                        for row in INTERNAL_NARRATION_SHAPES}
        for shape_id, sentence in self.MUST_BE_NATURALIZED:
            with self.subTest(shape=shape_id):
                value, actions = client_facing_prose(sentence)
                applied = [row for row in actions if row["applied"]]
                self.assertEqual(len(applied), 1, f"{shape_id} 没有动手，元叙述会原样进 DOCX")
                self.assertEqual(applied[0]["shape_id"], shape_id)
                self.assertTrue(applied[0]["matched_text"])
                # 反"残句拼接"：产物只允许整句不出现 / 整条替代文本 / 原句里连续的一段。
                if value.strip():
                    head = value.strip().rstrip("。；;！!？?")
                    allowed = {replacements[shape_id].rstrip("。；"),
                               *[sentence.strip()[:len(head)] for _ in [0]]}
                    self.assertTrue(head in sentence or head == replacements[shape_id].rstrip("。；"),
                                    f"{shape_id} 产物不是原句子串也不是整条替代文本：{head} / {allowed}")
                self.assertEqual(client_facing_prose(value)[1], [], "第二遍必须零动作")

    def test_detection_and_repair_share_one_ruler(self):
        for _shape_id, sentence in self.MUST_BE_NATURALIZED:
            with self.subTest(text=sentence[:18]):
                findings = internal_narration_findings(sentence, "artifact.paragraphs[0]")
                self.assertTrue(findings)
                self.assertTrue(all(row["repair_action"] == "NATURALIZE_INTERNAL_NARRATION"
                                    for row in findings))
                self.assertTrue(all(row["severity"] == "AUTO_REPAIR" and row["auto_repair_applied"]
                                    for row in findings))
                self.assertEqual(internal_narration_findings(client_facing_prose(sentence)[0]), [])

    def test_mixed_sentence_keeps_its_business_clause(self):
        # 整子句收口：前半是真作业安排，只有尾下那句"不进入技术标正文"是写法说明。
        text = ("对应的预防行动同样只有三类，编入当期维护计划的专项预防检查、调整当期记录"
                "与检查重点、报甲方裁定，不以此为由新增设备、人手或改造投入，"
                "涉及费用的口径不进入技术标正文。")
        value, actions = client_facing_prose(text)
        self.assertTrue([row for row in actions if row["applied"]])
        self.assertIn("对应的预防行动同样只有三类", value)
        self.assertIn("不以此为由新增设备、人手或改造投入", value)
        self.assertNotIn("不进入技术标正文", value)
        self.assertTrue(value.endswith("。"))

    def test_substantive_methodology_sentences_are_reported_not_deleted(self):
        # 任务书要的"不误伤"：这些句子有业务内容，形状只在句中夹了一截内部口径。
        keep = (
            "统计不引入未经确认的历史数字，也不把个案表述为整体结论，样本量不足时如实标注为观察值。",
            "二是接口与口径管理：涉及对外经办区域的引导与解释严格以甲方授权为限，"
            "服务岗位只传递事实、不作承诺性答复，避免越权表述引发争议。",
            "以甲方授权口径为限，服务岗位只传递事实不作承诺，会议与接待按清单双向交接",
        )
        for sentence in keep:
            with self.subTest(text=sentence[:18]):
                value, actions = client_facing_prose(sentence)
                self.assertEqual(value, sentence, "风控与服务规则内容被洗掉了")
                for row in actions:
                    self.assertFalse(row["applied"])
                findings = internal_narration_findings(sentence)
                self.assertTrue(all(row["severity"] == "NEEDS_REVIEW" for row in findings),
                                "够不上整句的命中只能报人工确认，不能自动改写")


class C2RiskControlContentSurvives(unittest.TestCase):
    """§C 的反方向：sanitizer 不能把风控内容一起删掉。逐条要求零改写、零动作。"""

    MUST_SURVIVE: tuple[str, ...] = (
        "【待投标人确认：所需证明】",
        "待澄清事项以甲方书面澄清结果为准，澄清前不作为承诺。",
        "上述事项以双方书面确认后的责任界面为准。",
        "本章节只界定服务边界与前置条件，具体控制上限与计算基数以合同约定为准。",
        "涉及停机窗口的作业需经甲方书面确认后纳入应急响应流程。",
        "编号待填项由甲方归口部门填写，另行确认后排入计划。",
        "服务边界以招标文件给定的范围为限，超出部分由甲方另行委托并另行确认。",
    )

    def test_risk_control_text_is_untouched(self):
        for sentence in self.MUST_SURVIVE:
            with self.subTest(text=sentence[:24]):
                value, actions = client_facing_prose(sentence)
                self.assertEqual(value, sentence)
                self.assertEqual(actions, [])

    def test_bidder_evidence_anchor_survives_inside_a_real_paragraph(self):
        # 锚点是 Bidder Evidence 门在产物里唯一的可定位形状，删了就等于门查无此据。
        text = ("主入口礼仪岗的人员履历与形象照【待投标人确认：所需证明】，"
                "本方案先按岗位口径写明排班与交接安排。")
        value, _actions = client_facing_prose(text)
        self.assertIn("【待投标人确认：所需证明】", value)
        self.assertIn("排班与交接安排", value)

    def test_hygiene_tree_inherits_the_prose_pass_and_keeps_addressing_values(self):
        tree = {"sections": [{
            "section_id": "S03-02",
            "planned_content_unit_ids": ["S03-02-C01", "S03-02-C02"],
            "body_blocks": [{"type": "paragraph", "text": "本章先把\u201c可以写进正文的数\u201d"
                                                          "与\u201c只能写成条件的做法\u201d分开。"},
                            {"type": "bullet_group", "items": ["每日保洁按合同约定频次执行。"]}],
            "tables": [{"table_id": "T-0302", "rows": [["排除项与边界登记：本章只界定技术服务范围"
                                                        "与规模事实，商务与资格条款不在技术正文展开"]]},
            ],
        }]}
        repaired = apply_hygiene_tree(tree)
        blob = json.dumps(repaired, ensure_ascii=False)
        self.assertNotIn("可以写进正文的数", blob)
        self.assertNotIn("排除项与边界登记", blob)
        # 结构化寻址值必须逐字保留：上一轮把枚举值洗成中文，装配与覆盖找回全部静默落空。
        self.assertEqual(repaired["sections"][0]["section_id"], "S03-02")
        self.assertEqual(repaired["sections"][0]["planned_content_unit_ids"],
                         ["S03-02-C01", "S03-02-C02"])
        self.assertEqual(repaired["sections"][0]["body_blocks"][1]["type"], "bullet_group")
        self.assertEqual(repaired["sections"][0]["tables"][0]["table_id"], "T-0302")
        self.assertEqual(repaired["sections"][0]["body_blocks"][1]["items"],
                         ["每日保洁按合同约定频次执行。"])


class C3GateWiring(unittest.TestCase):
    """§C 落到门禁上：检得出、算可修、词表与提示词一起改。"""

    NARRATION = "本章先把\u201c可以写进正文的数\u201d与\u201c只能写成条件的做法\u201d分开。"

    def test_artifact_qa_reports_the_rule_as_auto_repair(self):
        report = evaluate_artifact({"artifact": {"paragraphs": [self.NARRATION]}})
        rows = [row for row in report["findings"]
                if row["rule_id"] == CLIENT_FACING_PROSE_RULE_ID]
        self.assertTrue(rows)
        self.assertEqual(rows[0]["severity"], "AUTO_REPAIR")
        self.assertEqual(report["status"], "AUTO_REPAIR")
        self.assertNotEqual(report["status"], "PASS", "有泄漏却自称 PASS 就是假干净")

    def test_rule_is_locally_recoverable(self):
        self.assertIn(CLIENT_FACING_PROSE_RULE_ID, RECOVERABLE_RULES)

    def test_structural_paths_are_exempt(self):
        # 寻址值不参与：那是后端寻址口径，不是客户可见文案。
        report = evaluate_artifact({"artifact": {"sections": [
            {"section_id": "S03-02", "content_unit_id": "内容单元"}]}})
        self.assertFalse([row for row in report["findings"]
                          if row["rule_id"] == CLIENT_FACING_PROSE_RULE_ID])

    def test_prompt_forbids_narration_without_quoting_the_banned_sentence(self):
        from longform.factory import LongformGenerationFactory
        source = Path(sys.modules["longform.factory"].__file__).read_text(encoding="utf-8")
        self.assertGreaterEqual(
            source.count("不得解释这份文档怎么写"), 2,
            "两条提示词入口（单节 / 批量）都要禁，只补一条就会从另一条漏出去")
        for banned in ("可以写进正文的数", "分三层并给出各自处置规则"):
            self.assertNotIn(banned, source,
                             "禁句原话进了提示词就等于把那一句交给模型抄进正文")
        self.assertTrue(callable(LongformGenerationFactory._word_prompt))

    def test_run_f_gate_facts_reject_the_previous_denominator_ruler(self):
        # 旧字面量（按"唯一实质主题"量的那份读数）必须读成 NOT_MEASURED。
        import tempfile
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw)
            payload = {"rows": [{"scoring_item_id": "SCR-01", "label": "应急预案整体方案设计"}],
                       "canonical_scoring_records": [{"canonical_scoring_id": "SCN-001"}],
                       "coverage_summary": {"canonical_scoring_total": 1,
                                            "coverage_denominator": COVERAGE_DENOMINATOR_LEGACY_LABEL},
                       "scoring_gate": {"measured": True, "technical_scoring_coverage": 1.0,
                                        "coverage_denominator": COVERAGE_DENOMINATOR_LEGACY_LABEL,
                                        "completed_clean_allowed": True,
                                        "min_technical_scoring_coverage": 0.8}}
            (run / "09_scoring_matrix.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            facts = _scoring_gate_facts(run)
            self.assertEqual(facts["scoring_gate_state"], "NOT_MEASURED")
            self.assertEqual(facts["scoring_coverage_denominator"],
                             COVERAGE_DENOMINATOR_LEGACY_LABEL)
            # 换成这一轮的尺子，同一份读数就必须被认下来。
            payload["scoring_gate"]["coverage_denominator"] = COVERAGE_DENOMINATOR
            payload["coverage_summary"]["coverage_denominator"] = COVERAGE_DENOMINATOR
            (run / "09_scoring_matrix.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(_scoring_gate_facts(run)["scoring_gate_state"], "COVERAGE_OK")


class D0NoProviderCalls(unittest.TestCase):
    """本轮全程离线：这个文件不调用任何 Provider，也不读任何凭证。"""

    def test_semantic_and_prose_layers_never_import_the_provider_stack(self):
        # 能量到的只有"这三个模块不导入 Provider/凭证栈"。零真实调用、零付费是**运行期**
        # 断言：同一进程里别的测试会 import providers，所以在这里查 `sys.modules` 只会得到
        # 一串与被测对象无关的名字。那本账由本轮的 dist E2E 多路独立见证来给（见收口报告）。
        import governance.client_facing_prose as prose
        import governance.commitment_provenance as provenance
        import planning.scoring_responsiveness as responsiveness
        for module in (prose, provenance, responsiveness):
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            imported: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.append(node.module)
            offending = [name for name in imported
                         if name.split(".")[0] in {"providers", "app"}
                         or "credential" in name.lower()]
            self.assertEqual(offending, [],
                             f"{module.__name__} 导入了 Provider/凭证层：语义层不该能发起调用")
        for topic in BY_ID:
            responsiveness_fields(str(BY_ID[topic]["label"]))


if __name__ == "__main__":
    unittest.main()
