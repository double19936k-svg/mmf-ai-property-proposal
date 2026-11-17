"""MMF-007C Round 3 §49：Technical-Only 范围门与评分实体证据的合成测试（不含任何客户文件）。

这一组测试存在的理由不是"再证明一次代码能跑"，而是 §0 那句话里最容易做错的四件事：
招标文件**确实**写着保证金、报价表、营业执照与业绩合同，一条都不能漏读；但漏读之外还有
另一种错法——把它们当需求去响应，于是生出商务小节、资信附件页和报价表格，用它们撑页数。
所以每条排除项都必须同时满足两端：

1. SCOPE-01：商务条款在规划期就被登记为 EXCLUDED，**不生成** Section / Content Unit /
   Page Budget / Word Table；登记本身不算覆盖，也不许反过来把技术章节改名叫商务。
2. SCOPE-02：资信与报价评分项不进入技术评分分母——分母虚高会让"覆盖率 100 %"
   变成一句假话，所以既要不进分母，也要在分子里 appearing 为 EXCLUDED_FROM_TECHNICAL_BID。
3. SCOPE-03：报价信息不进 Word 正文。上游不进（不建结构），下游兜底（正文扫到就
   NEEDS_REVIEW），两件事都要量到；本地修复只改内部分类名称，**不许**把报价段落
   悄悄洗成"看起来干净"。
4. SCOPE-04：Commercial Compliance Matrix 只是内部门禁台账，不进 Artifact、不进 Provider Context。

§49 另两组的落点不在本文件：PAGE-01..03 在 `test_mmf007c_page_calibration.py`，
TABLE-01..03 在 `test_mmf007c_form_table_governance.py`，QA-EMPTY-01..02 在
`test_mmf007c_qa_precision.py`。SCORING-01/02 在本文件末尾。

红线：本文件只用合成数据，不读任何真实招标文件与产物，不发 Provider 请求。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

APP = Path(__file__).resolve().parents[1] / "app"
TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for item in (APP, TESTS, ROOT):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from governance.final_artifact_qa import evaluate_docx_artifact, repair_docx_artifact  # noqa: E402
from planning.content_engineering import (  # noqa: E402
    EXCLUDED_TECHNICAL_SCOPE_CLASSES,
    SCOPE_ENFORCEMENT,
    TECHNICAL_BODY_SCOPE_RULES,
    build_commercial_compliance_matrix,
    build_requirement_classification,
    build_technical_scope_gate,
    classify_technical_scope,
    is_excluded_technical_scope,
    scan_technical_body_scope,
)
from planning.hierarchy import build_document_hierarchy, build_scoring_matrix  # noqa: E402
from planning.planner import build_planning_bundle  # noqa: E402
from planning.scoring_evidence import (  # noqa: E402
    SUBSTANTIVE_UNIT_CHARS,
    artifact_evidence_index,
    judge_scoring_row,
)
from tests.test_mmf007c_table_density import _artifact, _build, _paragraph, _table  # noqa: E402

# 合成项目：一个产业园物业技术标。三条排除条款刻意写成招标文件里真实的句式，
# 这样"分类器把它们读成技术需求"才是可失败的断言。
_TECH_REQS = [
    {"requirement_id": "REQ-0001", "normalized_requirement": "项目位于产业园，包含写字楼与商业裙楼业态",
     "mandatory_level": "INFO", "domain": "project_fact", "confirmation_status": "CONFIRMED", "scoring_item_id": None},
    {"requirement_id": "REQ-0002", "normalized_requirement": "最低人员配置不少于45人，需明确岗位与班次安排",
     "mandatory_level": "MUST", "domain": "staffing", "confirmation_status": "CONFIRMED", "scoring_item_id": None},
    {"requirement_id": "REQ-0003", "normalized_requirement": "园区实行封闭式管理，重点区域需要定时巡逻",
     "mandatory_level": "MUST", "domain": "security", "confirmation_status": "CONFIRMED", "scoring_item_id": None},
    {"requirement_id": "REQ-0004", "normalized_requirement": "投诉需要建立台账并在约定周期内闭环处理",
     "mandatory_level": "MUST", "domain": "sla_kpi", "confirmation_status": "CONFIRMED", "scoring_item_id": "SCR-001"},
    {"requirement_id": "REQ-0005", "normalized_requirement": "针对产业园特点提出合理性管理建议",
     "mandatory_level": "INFO", "domain": "scoring", "confirmation_status": "CONFIRMED", "scoring_item_id": "SCR-002"},
]
_EXCLUDED_REQS = [
    # 商务：保证金与偏离表是商务标的动作，技术正文既不该展开也不该起小节。
    {"requirement_id": "REQ-9001", "normalized_requirement": "投标保证金应按招标文件规定的方式与时间缴纳，并接受商务偏离表核对",
     "mandatory_level": "MUST", "domain": "commercial", "confirmation_status": "CONFIRMED", "scoring_item_id": None},
    # 报价：单价/总价/最高限价一律不进技术正文。
    {"requirement_id": "REQ-9002", "normalized_requirement": "投标报价表应列明各岗位单价、月度总价与最高限价对照",
     "mandatory_level": "MUST", "domain": "pricing", "confirmation_status": "CONFIRMED", "scoring_item_id": None},
    # 资信：证书与业绩材料属资格审查文件，技术标只登记不建页。
    {"requirement_id": "REQ-9003", "normalized_requirement": "随投标提供企业营业执照、ISO9001质量管理体系认证证书与AAA信用等级证书",
     "mandatory_level": "MUST", "domain": "qualification", "confirmation_status": "CONFIRMED", "scoring_item_id": None},
]
_SCORING_ITEMS = [
    {"scoring_item_id": "SCR-001", "label": "客户服务与投诉闭环机制（10分）", "must_respond": True, "max_score": 10},
    {"scoring_item_id": "SCR-002", "label": "针对产业园业态的合理化建议（20分）", "must_respond": True, "max_score": 20},
    {"scoring_item_id": "SCR-9001", "label": "企业资信与类似项目业绩（10分）", "must_respond": True, "max_score": 10},
    {"scoring_item_id": "SCR-9002", "label": "投标报价与下浮率（40分）", "must_respond": True, "max_score": 40},
]
_BRIEF = {"project_name": "合成产业园物业项目", "medium": "WORD", "project_type": "产业园综合体",
          "scenario": "完整物业服务方案"}
_SELECTION: dict[str, Any] = {"knowledge_usage_contracts": []}


def _pack() -> dict[str, Any]:
    return {
        "schema_version": "tender-requirement-pack-v0.1",
        "pack_id": "PACK-scope0049",
        "pack_version": 1,
        "status": "ready_for_plan",
        "confirmation": {"ready_for_brief_seed": True},
        "project_facts": {"project_area": {"value": "12万平方米", "confirmation_status": "CONFIRMED"}},
        "service_scope": {"included": [], "excluded": []},
        "requirements": [*_TECH_REQS, *_EXCLUDED_REQS],
        "scoring_items": [*_SCORING_ITEMS],
    }


def _bundle(pack: dict[str, Any] | None = None) -> dict[str, Any]:
    return build_planning_bundle(pack or _pack(), _BRIEF, _SELECTION)


def _walk_strings(value: Any, path: str = "$") -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            rows.extend(_walk_strings(child, f"{path}.{key}"))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            rows.extend(_walk_strings(child, f"{path}[{index}]"))
    elif isinstance(value, str) and value.strip():
        rows.append((path, value))
    return rows


class Scope01NoStructureForExcludedRequirements(unittest.TestCase):
    """§49 SCOPE-01：商务 Requirement 不生成 CU（也不生成 Section / Budget / Table）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pack = _pack()
        cls.bundle = _bundle(cls.pack)
        cls.hierarchy = build_document_hierarchy(cls.pack, cls.bundle)
        cls.gate = cls.bundle["technical_scope_gate"]
        cls.excluded_ids = [row["requirement_id"] for row in _EXCLUDED_REQS]

    def test_classified_as_excluded_before_anything_is_planned(self) -> None:
        for row in _EXCLUDED_REQS:
            scope_class = classify_technical_scope(row)
            self.assertIn(scope_class, EXCLUDED_TECHNICAL_SCOPE_CLASSES, row["requirement_id"])
            self.assertTrue(is_excluded_technical_scope(scope_class), row["requirement_id"])

    def test_matrix_rows_cover_technical_requirements_only(self) -> None:
        matrix_ids = {row["requirement_id"] for row in self.bundle["requirement_matrix"]["matrix"]}
        self.assertTrue({row["requirement_id"] for row in _TECH_REQS}.issubset(matrix_ids))
        leaked = matrix_ids.intersection(self.excluded_ids)
        self.assertEqual(set(), leaked, "排除条款不得出现在需求—Section 矩阵里")
        # 矩阵里出现的每一行都必须是会被建正文的类别：REFERENCE 允许，EXCLUDED 不行。
        self.assertTrue(all(not is_excluded_technical_scope(row.get("scope_class"))
                            for row in self.bundle["requirement_matrix"]["matrix"]))

    def test_no_content_unit_carries_an_excluded_requirement(self) -> None:
        units = self.hierarchy["content_units"]
        self.assertTrue(units)
        carriers = [unit["content_unit_id"] for unit in units
                    if set(unit.get("requirement_ids") or []) & set(self.excluded_ids)]
        self.assertEqual([], carriers, "Content Unit 不得挂靠排除条款")
        # CU 的 must_cover / title 也不许抄排除条款原文，否则等于换了个壳继续写商务。
        for row in _EXCLUDED_REQS:
            text = row["normalized_requirement"]
            hit = [unit["content_unit_id"] for unit in units
                   if text in " ".join([str(unit.get("title")), str(unit.get("purpose")),
                                        " ".join(unit.get("must_cover") or [])])]
            self.assertEqual([], hit, f"{row['requirement_id']} 的原文进了 CU 施工单")

    def test_no_section_or_contract_is_built_for_excluded_clauses(self) -> None:
        outline_sections = [section for chapter in (self.bundle["word_plan"]["outline"] or [])
                            for section in chapter.get("sections") or []]
        for token in ("商务", "报价", "资信", "保证金", "偏离"):
            named = [row["section_id"] for row in outline_sections if token in str(row.get("section_title"))]
            self.assertEqual([], named, f"目录里出现了为排除条款起的 Section：{token}")
        contracts = self.bundle["section_contracts"]["contracts"]
        for row in _EXCLUDED_REQS:
            text = row["normalized_requirement"]
            hit = [contract["section_id"] for contract in contracts
                   if text in [str(item).strip() for item in (contract.get("must_cover") or [])]]
            self.assertEqual([], hit, f"{row['requirement_id']} 原文出现在 Section 契约 must_cover")
        budget_paths = json.dumps(self.bundle["content_budget"], ensure_ascii=False)
        for row in _EXCLUDED_REQS:
            self.assertNotIn(row["normalized_requirement"], budget_paths,
                             "Page Budget 不得为排除条款留字数")

    def test_register_records_them_and_forbids_every_structure_action(self) -> None:
        register = {row["requirement_id"]: row for row in self.gate["excluded_register"]}
        self.assertEqual(set(self.excluded_ids), set(register))
        for row in register.values():
            self.assertEqual("EXCLUDED_FROM_TECHNICAL_BID", row["disposition"])
            self.assertTrue(row["recorded_only"])
            self.assertEqual(["IDENTIFY", "CLASSIFY", "RECORD"], row["allowed_actions"])
            self.assertIn("CREATE_CONTENT_UNIT", row["forbidden_actions"])
            self.assertIn("BUILD_BLANK_ATTACHMENT_PAGE", row["forbidden_actions"])
        self.assertEqual(len(self.excluded_ids),
                         self.bundle["requirement_matrix"]["coverage_summary"]["scope_excluded_requirement_total"])
        # 登记不是失败：门控状态必须是 TECHNICAL_ONLY，而不是把排除项报成缺陷。
        self.assertEqual("TECHNICAL_ONLY", self.gate["status"])
        self.assertEqual(SCOPE_ENFORCEMENT, self.gate["enforcement"])


class Scope02CreditAndPricingLeaveTheTechnicalDenominator(unittest.TestCase):
    """§49 SCOPE-02：资信/报价评分项不进技术评分分母。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pack = _pack()
        cls.bundle = _bundle(cls.pack)
        cls.gate = cls.bundle["technical_scope_gate"]
        cls.hierarchy = build_document_hierarchy(cls.pack, cls.bundle)
        cls.scoring_matrix = build_scoring_matrix(cls.pack, cls.bundle, cls.hierarchy)

    def test_denominator_counts_technical_scoring_items_only(self) -> None:
        self.assertEqual(["SCR-001", "SCR-002"], self.gate["technical_scoring_item_ids"])
        self.assertEqual(2, self.gate["technical_scoring_denominator"])
        self.assertEqual({"SCR-9001", "SCR-9002"}, set(self.gate["excluded_scoring_register_ids"]))
        for row in self.gate["scoring_register"]:
            if row["scoring_item_id"] in {"SCR-9001", "SCR-9002"}:
                self.assertEqual("EXCLUDED", row["disposition"], row["scoring_item_id"])

    def test_coverage_summary_shares_the_same_denominator(self) -> None:
        summary = self.bundle["requirement_matrix"]["coverage_summary"]
        self.assertEqual(2, summary["technical_scoring_total"])
        self.assertEqual(2, summary["excluded_scoring_total"])
        self.assertEqual({"SCR-9001", "SCR-9002"}, set(summary["excluded_scoring_item_ids"]))
        # §38：需求侧分母字符串与评分侧分母字符串各归一处，且都点名自己的权威。
        self.assertEqual("SCOPE_GATED_MUST_OR_SCORING_ROWS", summary["coverage_denominator"])
        self.assertEqual("TECHNICAL_ONLY_SCOPE_GATE", summary["scoring_denominator_authority"])
        self.assertEqual(
            "TECHNICAL_ONLY_SCOPE_GATE",
            self.bundle["requirement_matrix"]["requirement_denominator_authority"]["denominator_authority"],
        )
        self.assertTrue(summary["qa_denominator_conservation_holds"])
        # 分母不能被悄悄放大：scoring_total 就是技术项个数，不含资信/报价。
        self.assertEqual(summary["technical_scoring_total"], summary["scoring_total"])

    def test_excluded_rows_judge_as_excluded_even_with_plenty_of_text(self) -> None:
        """资信分再高也不许被"写满一页"顶成 FULFILLED：先分类，再判覆盖。"""
        row = next(r for r in self.scoring_matrix["rows"] if r["scoring_item_id"] == "SCR-9001")
        self.assertEqual("EXCLUDED", row["scope_disposition"])
        self.assertEqual("OUT_OF_TECHNICAL_SCOPE", row.get("response_status"))
        self.assertEqual([], row.get("section_ids"), "排除评分项不得占响应 Section")
        claim = "企业资信、类似项目业绩、ISO证书、AAA信用证书材料与履约证明齐全，响应位置见本节说明。"
        fragments = {sid: {"body_blocks": [{"type": "paragraph", "content": claim * 12} for _ in range(3)]}
                     for sid in (row.get("section_ids") or ["S01-01"])}
        unit_index, structured, tables = artifact_evidence_index(fragments)
        verdict = judge_scoring_row(row, unit_index, structured, tables)
        self.assertEqual("EXCLUDED_FROM_TECHNICAL_BID", verdict["status"])
        self.assertFalse(verdict["entity_backed"])
        self.assertEqual([], verdict["evidence_text"])
        self.assertEqual(0, verdict["effective_chars"])

    def test_technical_rows_stay_planned_into_sections(self) -> None:
        """分母里的技术评分项必须真的落到响应 Section，否则"不进分母"只是把窟窿藏起来。"""
        rows = {row["scoring_item_id"]: row for row in self.scoring_matrix["rows"]}
        for scoring_id in ("SCR-001", "SCR-002"):
            row = rows[scoring_id]
            # REFERENCE 与 INCLUDED 都算技术侧（"合理化建议"没有物业主题词，被判参考项），
            # 只有 EXCLUDED 才该被踢出分母。
            self.assertIn(row["scope_disposition"], {"INCLUDED", "REFERENCE"}, scoring_id)
            self.assertIn(row.get("response_status"), {"PLANNED", "DERIVED_PLANNED"}, scoring_id)
            self.assertTrue(row.get("section_ids"), scoring_id)


class Scope03PricingStaysOutOfTheWordBody(unittest.TestCase):
    """§49 SCOPE-03：报价信息不进 Word 正文（上游不建结构 + 下游扫描兜底）。"""

    def test_excluded_clause_text_lives_only_in_internal_registers(self) -> None:
        """排除条款的原文只许待在五个登记处，出现在别处就是越界。

        这一条刻意按**全 bundle** 扫：正文链路（word_plan / section_contracts /
        content_budget / global_state.client_requirements）只要有一天抄走原文，
        路径就会掉出白名单并被点名 —— 而不是等我先想到去查哪个字段。
        """
        bundle = _bundle()
        allowed_prefixes = (
            "$.commercial_compliance_matrix.rows",              # 内部台账（SCOPE-04）
            "$.global_state.source_trace.fixture_or_heading_markers",  # 溯源：招标原文留痕
            "$.requirement_classification.rows",                # 分类结果本身
            "$.requirement_matrix.excluded_requirements",       # 需求矩阵的排除登记
            "$.technical_scope_gate.excluded_register",         # 门控台账
        )
        for row in _EXCLUDED_REQS:
            offenders = sorted({path.split("[")[0] for path, text in _walk_strings(bundle)
                                if row["normalized_requirement"] in text
                                and not path.startswith(allowed_prefixes)})
            self.assertEqual([], offenders, f"{row['requirement_id']} 的原文出现在正文链路：{offenders}")
            # 白名单不是摆设：每一条排除条款确实被登记过。
            registered = [path for path, text in _walk_strings(bundle)
                          if row["normalized_requirement"] in text]
            self.assertTrue(registered, row["requirement_id"])
        body_surfaces = json.dumps({"word_plan": bundle["word_plan"],
                                    "section_contracts": bundle["section_contracts"],
                                    "content_budget": bundle["content_budget"]}, ensure_ascii=False)
        for row in _EXCLUDED_REQS:
            self.assertNotIn(row["normalized_requirement"], body_surfaces)

    def test_body_scan_catches_a_pricing_paragraph_and_keeps_it_under_review(self) -> None:
        artifact = _artifact([
            {"heading": "服务内容与边界", "level": 1, "paragraphs": [
                _paragraph(1),
                "本部分列示投标报价表、最高限价对照、各岗位单价与总价构成，并说明投标保证金缴纳方式。"]},
            {"heading": "岗位与班次安排", "level": 1, "paragraphs": [_paragraph(2)]},
        ])
        findings = scan_technical_body_scope(artifact)
        rules = {row["rule_id"] for row in findings}
        self.assertIn("EXCLUDED_SCOPE_BODY_LEAK", rules)
        hit = next(row for row in findings if row["rule_id"] == "EXCLUDED_SCOPE_BODY_LEAK")
        # 兜底门只到 NEEDS_REVIEW：它是"交人来判"，既不放过也不擅自升级为 BLOCK。
        self.assertEqual("NEEDS_REVIEW", hit["severity"])
        self.assertEqual("投标报价", hit["text"])
        self.assertIn("EXCLUDED_SCOPE_BODY_LEAK", {rule[0] for rule in TECHNICAL_BODY_SCOPE_RULES})

    def test_local_repair_renames_internal_terms_but_does_not_whiten_pricing(self) -> None:
        """把内部分类名改成对外用词是本地修复；把报价段落悄悄删掉不是，两者不许混。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = _artifact([
                {"heading": "响应方式说明", "level": 1, "paragraphs": [
                    _paragraph(1),
                    "本节按商务响应矩阵逐条核对技术方案的响应位置，符合性审查矩阵另附。"]},
                {"heading": "保洁作业组织", "level": 1, "paragraphs": [
                    _paragraph(2),
                    "本部分列示投标报价表与最高限价对照，供开标时核对单价与总价。"]},
            ])
            docx, _ledger = _build(root, artifact)
            before = evaluate_docx_artifact(docx)
            rules = {row["rule_id"] for row in before["findings"]}
            self.assertIn("EXCLUDED_SCOPE_INTERNAL_MATRIX_REF", rules)
            self.assertIn("EXCLUDED_SCOPE_BODY_LEAK", rules)

            repaired = repair_docx_artifact(docx)
            target = Path(repaired["artifact_path"])
            text_after = _docx_text(target)
            self.assertGreater(repaired["repairs"]["technical_scope_terms"], 0)
            self.assertNotIn("商务响应矩阵", text_after)
            self.assertNotIn("符合性审查矩阵", text_after)
            # 报价四个字仍在，且修完仍然带着 BODY_LEAK：没被洗成"看起来干净"。
            self.assertIn("投标报价表", text_after)
            after = evaluate_docx_artifact(target)
            self.assertIn("EXCLUDED_SCOPE_BODY_LEAK", {row["rule_id"] for row in after["findings"]})
            self.assertNotIn("EXCLUDED_SCOPE_INTERNAL_MATRIX_REF", {row["rule_id"] for row in after["findings"]})

    def test_rules_are_the_two_declared_ones(self) -> None:
        self.assertEqual({"EXCLUDED_SCOPE_INTERNAL_MATRIX_REF", "EXCLUDED_SCOPE_BODY_LEAK"},
                         {rule[0] for rule in TECHNICAL_BODY_SCOPE_RULES})
        self.assertFalse(SCOPE_ENFORCEMENT["commercial_compliance_matrix_in_final_word"])
        self.assertTrue(SCOPE_ENFORCEMENT["excluded_items_build_blank_attachment_page"] is False)


class Scope04CommercialComplianceMatrixStaysInternal(unittest.TestCase):
    """§49 SCOPE-04：Commercial Compliance Matrix 不进 Artifact、不进 Provider Context。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pack = _pack()
        cls.bundle = _bundle(cls.pack)
        cls.matrix = cls.bundle["commercial_compliance_matrix"]

    def test_matrix_declares_itself_internal_record_only(self) -> None:
        self.assertEqual("INTERNAL_ONLY", self.matrix["audience"])
        self.assertFalse(self.matrix["final_word_included"])
        self.assertFalse(self.matrix["provider_context_included"])
        self.assertFalse(self.matrix["commercial_body_expansion"])
        self.assertEqual("INTERNAL_RECORD_ONLY", SCOPE_ENFORCEMENT["commercial_compliance_matrix_role"])
        for row in self.matrix["rows"]:
            self.assertEqual("PENDING_BIDDER_CONFIRMATION", row["response_status"])
            self.assertEqual("COMPACT_REFERENCE_ONLY", row["technical_body_policy"])
            self.assertIn("【待投标人确认", row["evidence_or_filling_location"])

    def test_matrix_is_the_builder_output_not_a_rewritten_copy(self) -> None:
        """Bundle 里那份就是 builder 的输出：中途"顺手过滤掉几行"要能被看见。"""
        rebuilt = build_commercial_compliance_matrix(build_requirement_classification(self.pack))
        self.assertEqual(rebuilt["rows"], self.matrix["rows"])
        self.assertEqual(rebuilt["audience"], self.matrix["audience"])

    def test_matrix_rows_are_the_excluded_clauses_and_nothing_technical(self) -> None:
        requirement_ids = {row["requirement_id"] for row in self.matrix["rows"]}
        self.assertTrue(requirement_ids)
        self.assertTrue(requirement_ids <= {"REQ-9001", "REQ-9002", "REQ-9003"},
                        "台账里出现了技术条款，等于把技术需求改判成商务响应")
        for row in _TECH_REQS:
            self.assertNotIn(row["requirement_id"], requirement_ids)

    def test_matrix_never_reaches_the_provider_prompt_or_the_word_plan(self) -> None:
        contract = self.bundle["section_contracts"]["contracts"][0]
        prompt = json.dumps(_prompt_for(contract), ensure_ascii=False)
        for row in self.matrix["rows"]:
            self.assertNotIn(str(row["clause"]), prompt)
        self.assertNotIn("商务响应矩阵", prompt)
        self.assertNotIn("Commercial Compliance", prompt)
        word_plan = json.dumps(self.bundle["word_plan"], ensure_ascii=False)
        self.assertNotIn("commercial", word_plan.lower())
        # 台账是规划阶段的内部产物文件，不挂在 word_plan / artifact 结构上。
        self.assertNotIn("commercial_compliance_matrix", self.bundle["word_plan"])

    def test_a_built_docx_carries_no_matrix_name_and_is_flagged_if_it_does(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = _artifact([
                {"heading": "巡检机制", "level": 1,
                 "paragraphs": [_paragraph(1), _paragraph(2)],
                 "tables": [_table("重点区域巡查频次与责任岗位表",
                                   ["时段", "频次", "责任岗位", "记录载体"],
                                   [["07:00-19:00", "每2小时1次", "秩序维护员", "电子巡更记录"],
                                    ["19:00-次日07:00", "每3小时1次", "秩序维护领班", "纸质签到表"]])]},
            ])
            docx, _ledger = _build(root, artifact)
            text = _docx_text(docx)
            self.assertNotIn("商务响应矩阵", text)
            self.assertIn("重点区域巡查频次与责任岗位表", text)


class Scoring01EntityEvidenceRequiredAndLocatable(unittest.TestCase):
    """§49 SCORING-01：技术评分项必须靠产物里的实体证据判 FULFILLED，且逐字可定位。"""

    def _row(self, **overrides: Any) -> dict[str, Any]:
        row = {
            "scoring_item_id": "SCR-001",
            "label": "客户服务与投诉闭环机制（10分）",
            "max_score": 10,
            "must_respond": True,
            "scope_class": "TECHNICAL_INCLUDED",
            "scope_disposition": "INCLUDED",
            "requirement_ids": ["REQ-0004"],
            "section_ids": ["S04-02"],
            "primary_section_id": "S04-02",
            "planned_evidence": {"tables": 1, "processes": 0, "required_outputs": ["投诉台账"]},
        }
        row.update(overrides)
        return row

    def test_fulfilled_requires_locatable_verbatim_evidence(self) -> None:
        narrative = (
            "客户服务按投诉闭环机制分级：普通投诉由客服主管当日派单，责任班组在约定时限内到场处置，"
            "工程类投诉由工程主管复核并回填处理结果；每条投诉建立台账，登记编号、提出时间、部位、责任岗位与"
            "完成时限，处置完成后由品质主管按周抽查回访，回访结果回写台账并对重复投诉发起整改，"
            "整改验证通过后方可销项。台账按月汇总，形成投诉分布与响应时长分析，作为岗位考核与服务改进输入。"
        ) * 3
        fragments = {"S04-02": {
            "paragraphs": [narrative],
            "content_unit_fragments": [{
                "content_unit_id": "CU-S04-02-01", "archetype": "NARRATIVE", "content": narrative}],
            "tables": [_table("客户服务与投诉闭环机制台账表",
                              ["投诉编号", "提出时间与部位", "责任岗位", "处置时限", "回访结论", "销项日期"],
                              [["TS-2026-001", "3月5日 B座大堂", "客服主管", "24小时", "已回访满意", "3月7日"],
                               ["TS-2026-002", "3月11日 地下车库", "工程主管", "48小时", "已整改验证", "3月14日"]])],
        }}
        unit_index, structured, tables = artifact_evidence_index(fragments)
        verdict = judge_scoring_row(self._row(), unit_index, structured, tables)
        self.assertEqual("FULFILLED", verdict["status"])
        self.assertTrue(verdict["entity_backed"])
        self.assertGreaterEqual(len(verdict["concepts_found"]), verdict["concepts_required"])
        self.assertEqual("S04-02", verdict["response_section"])
        self.assertTrue(verdict["response_cu"])
        self.assertTrue(verdict["artifact_location"])
        self.assertGreater(verdict["effective_chars"], SUBSTANTIVE_UNIT_CHARS)
        # 证据必须能在产物原文里逐字找到——这是"实体证据"而不是"我说过我写了"。
        haystack = "".join(str(narrative).split()) + structured["S04-02"]
        self.assertTrue(verdict["evidence_text"])
        for row in verdict["evidence_text"]:
            self.assertIn(row["concept"], verdict["concepts_found"])
            self.assertIn(row["text"], haystack)
        for row in verdict["artifact_location"]:
            self.assertEqual("S04-02", row["section_id"])
            self.assertIn("fragment.json#", row["path"])

    def test_technical_row_without_a_planned_section_is_not_planned(self) -> None:
        unit_index, structured, tables = artifact_evidence_index({})
        verdict = judge_scoring_row(self._row(section_ids=[], primary_section_id=None),
                                    unit_index, structured, tables)
        self.assertEqual("MISSING", verdict["status"])
        self.assertEqual("NOT_PLANNED", verdict["missing_kind"])


class Scoring02NoEntityContentCannotBeFulfilled(unittest.TestCase):
    """§49 SCORING-02：无实体内容不得判 FULFILLED（有字数、有口号都不算）。"""

    def _row(self) -> dict[str, Any]:
        return {
            "scoring_item_id": "SCR-002", "label": "针对产业园业态的合理化建议（20分）",
            "max_score": 20, "must_respond": True,
            "scope_class": "TECHNICAL_INCLUDED", "scope_disposition": "INCLUDED",
            "requirement_ids": ["REQ-0005"], "section_ids": ["S02-02"], "primary_section_id": "S02-02",
            "planned_evidence": {"tables": 1, "processes": 0, "required_outputs": []},
        }

    def test_chars_without_any_rubric_concept_is_nearby_only(self) -> None:
        # 刻意不含任何评分要素词：这段字再多，也只是"这节有文字"。
        filler = "我们始终坚持以客户为中心的服务理念，注重队伍面貌与服务口碑的持续改善，努力营造更好的现场秩序与环境。" * 12
        fragments = {"S02-02": {"paragraphs": [filler], "content_unit_fragments": [
            {"content_unit_id": "CU-S02-02-01", "archetype": "NARRATIVE", "content": filler}]}}
        unit_index, structured, tables = artifact_evidence_index(fragments)
        verdict = judge_scoring_row(self._row(), unit_index, structured, tables)
        self.assertNotEqual("FULFILLED", verdict["status"])
        self.assertFalse(verdict["entity_backed"])
        self.assertEqual("NEARBY_ONLY", verdict["status"])
        self.assertEqual([], verdict["response_cu"])

    def test_concepts_without_a_substantive_unit_cannot_be_fulfilled(self) -> None:
        """要素齐全但每段都短到不像一节内容：最多 PARTIAL，不能算实体。"""
        thin = "本项目风险点在于人流密集，建议优化巡查安排并分阶段落地，由责任岗位按时限考核验证成效。"
        fragments = {"S02-02": {"paragraphs": [thin], "content_unit_fragments": [
            {"content_unit_id": "CU-S02-02-01", "archetype": "NARRATIVE", "content": thin}]}}
        unit_index, structured, tables = artifact_evidence_index(fragments)
        verdict = judge_scoring_row(self._row(), unit_index, structured, tables)
        self.assertNotEqual("FULFILLED", verdict["status"])
        self.assertFalse(verdict["entity_backed"])
        self.assertIn(verdict["status"], {"PARTIAL", "NEARBY_ONLY"})

    def test_narrative_only_concepts_fail_the_structured_requirement(self) -> None:
        """planned_evidence 要求表格，要素却只出现在叙述里：结构化载体缺席就不算实体。"""
        narrative = (
            "本项目为产业园综合体，难点在于人流与货流交叉、企业作息不一与装修施工频繁。针对这些风险，"
            "建议将秩序维护的重点时段前移至早晚高峰，并按季度形成实施计划：第一阶段完成动线梳理与标识补强，"
            "第二阶段完成岗位与频次表调整，第三阶段进入效果衡量。每项建议由责任岗位在既定时限内落地，"
            "品质主管以月度考核与现场验证衡量成效，对未达标的改进措施回炉重做，直至形成可复核的成果。"
        ) * 2
        fragments = {"S02-02": {"paragraphs": [narrative], "content_unit_fragments": [
            {"content_unit_id": "CU-S02-02-01", "archetype": "NARRATIVE", "content": narrative}]}}
        unit_index, structured, tables = artifact_evidence_index(fragments)
        verdict = judge_scoring_row(self._row(), unit_index, structured, tables)
        self.assertTrue(verdict["concepts_found"], "用例前提：叙述里确实写了要素")
        self.assertNotEqual("FULFILLED", verdict["status"])
        self.assertFalse(verdict["entity_backed"])
        # 同一份文字加上结构化载体之后必须翻正：判据是"有没有载体"，不是"喜不喜欢这段文字"。
        fragments["S02-02"]["tables"] = [_table("管理建议实施与效果衡量表",
                                                ["建议事项", "实施阶段", "责任岗位", "完成时限", "衡量指标"],
                                                [["早晚高峰动线梳理", "第一阶段", "秩序维护领班", "30日内", "高峰拥堵时长"],
                                                 ["岗位与频次表调整", "第二阶段", "项目经理", "60日内", "巡查覆盖率"]])]
        unit_index, structured, tables = artifact_evidence_index(fragments)
        flipped = judge_scoring_row(self._row(), unit_index, structured, tables)
        self.assertEqual("FULFILLED", flipped["status"])


def _docx_text(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    parts = [paragraph.text or "" for paragraph in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.extend(cell.text or "" for cell in row.cells)
    return "\n".join(parts)


def _prompt_for(contract: dict[str, Any]) -> dict[str, Any]:
    from longform.factory import LongformGenerationFactory

    context = {"section_contract": {
        "section_id": contract.get("section_id"),
        "section_title": contract.get("section_title"),
        "must_cover": contract.get("must_cover") or [],
        "required_outputs": contract.get("required_outputs") or [],
        "target_words": contract.get("target_words") or {"min": 800, "max": 1200},
        "source_requirements": contract.get("source_requirements") or [],
    }, "content_unit_contracts": [], "content_unit_execution": {}}
    return LongformGenerationFactory._word_prompt(context)


if __name__ == "__main__":
    unittest.main(verbosity=2)
