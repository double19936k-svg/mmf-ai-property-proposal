"""MMF-007C Round 4 · P0-C1 范围门后的唯一需求分母 + 承诺/元数据两条泄漏（§38–§43）。

全部离线：Synthetic Fixture + 纯规划/QA 函数 + tmp 目录，不发起任何 Provider 调用（§1）。
本模块盯的是"分母只有一个来源"和"检出口与修复口同口径"两件事：
Round 3 的 QR-001 吃的是范围门**之前**的行集，而元数据泄漏是"门报了、修没实现"，
两者都会让交付判定建立在错误的集合或缺失的修复上。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
APP = APP_ROOT / "app"
for _path in (str(APP), str(APP_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from governance.artifact_qa import (  # noqa: E402
    _is_structural_key, evaluate_artifact,
)
from governance.commitment_provenance import (  # noqa: E402
    _repair_completion_claims, _repair_text, evaluate_commitments,
)
from governance.customer_hygiene import (  # noqa: E402
    apply_hygiene_tree, hygiene_findings, rewrite_customer_hygiene,
)
from longform.orchestrator import evaluate_requirement_coverage_regression  # noqa: E402
from planning.content_engineering import (  # noqa: E402
    classify_technical_scope, is_excluded_technical_scope,
)
from planning.scope_gated_requirements import (  # noqa: E402
    NON_RESPONSE_SCORING_CLASSES, REQUIREMENT_DENOMINATOR, ROW_FALLBACK_AUTHORITY,
    evidence_alias_hits, scope_gated_denominator, split_requirement_evidence,
)


def _row(req_id: str, *, must: bool = False, scope: str = "TECHNICAL_INCLUDED",
         scoring: str | None = None, section: str | None = "S01-01",
         text: str = "", status: str = "MAPPED") -> dict:
    return {
        "requirement_id": req_id,
        "mandatory_level": "MUST" if must else "INFO",
        "scoring_item_id": scoring,
        "scope_class": scope,
        "primary_section_id": section,
        "secondary_section_ids": [],
        "coverage_status": status,
        "requirement_text": text or f"落实{req_id}的服务安排与核验载体",
    }


def _gate(*, excluded: tuple[str, ...] = (), technical_scoring: tuple[str, ...] = ()) -> dict:
    return {
        "schema_version": "technical-only-scope-gate-v0.1",
        "status": "TECHNICAL_ONLY",
        "excluded_register": [{"requirement_id": rid} for rid in excluded],
        "technical_scoring_item_ids": list(technical_scoring),
    }


def _fragment(*lines: str) -> dict:
    return {"body_blocks": [{"type": "paragraph", "content": line} for line in lines]}


class AuthorityVocabulary(unittest.TestCase):
    def test_denominator_string_is_defined_once(self) -> None:
        self.assertEqual("SCOPE_GATED_MUST_OR_SCORING_ROWS", REQUIREMENT_DENOMINATOR)
        import planning.planner as planner
        self.assertEqual(planner.REQUIREMENT_DENOMINATOR, REQUIREMENT_DENOMINATOR)

    def test_non_response_classes_cover_header_and_formula_rows(self) -> None:
        self.assertIn("SCORING_HEADER", NON_RESPONSE_SCORING_CLASSES)
        self.assertIn("SCORING_FORMULA", NON_RESPONSE_SCORING_CLASSES)


class RowRemoval(unittest.TestCase):
    def test_final_gate_excluded_row_leaves_the_denominator(self) -> None:
        matrix = {"matrix": [_row("REQ-0001", must=True), _row("REQ-0002", must=True)]}
        authority = scope_gated_denominator(matrix, scope_gate=_gate(excluded=("REQ-0002",)))
        self.assertEqual(["REQ-0001"], [r["requirement_id"] for r in authority["must_or_scoring_rows"]])
        removed = authority["excluded"][0]
        self.assertEqual("REQ-0002", removed["requirement_id"])
        self.assertEqual(["FINAL_GATE_EXCLUDED"], removed["removal_reasons"])
        self.assertEqual(1, authority["removal_counts"]["FINAL_GATE_EXCLUDED"])

    def test_row_scope_excluded_when_gate_absent_and_authority_says_so(self) -> None:
        matrix = {"matrix": [_row("REQ-0001", must=True, scope="PRICING_EXCLUDED")]}
        authority = scope_gated_denominator(matrix, scope_gate=None)
        self.assertEqual(ROW_FALLBACK_AUTHORITY, authority["denominator_authority"])
        self.assertFalse(authority["final_scope_gate_present"])
        self.assertEqual(["ROW_SCOPE_EXCLUDED"], authority["excluded"][0]["removal_reasons"])

    def test_never_recomputes_scope_from_text_when_row_has_no_class(self) -> None:
        """兜底口径只认行上已登记的 scope_class：不在此处另立一处范围真值。"""
        row = _row("REQ-0001", must=True)
        row.pop("scope_class")
        row["requirement_text"] = "服务费报价单按月度结算"
        authority = scope_gated_denominator({"matrix": [row]}, scope_gate=None)
        self.assertEqual([], authority["excluded"])
        self.assertEqual(1, authority["denominator_rows_total"])

    def test_scoring_row_outside_gate_denominator_is_removed(self) -> None:
        matrix = {"matrix": [_row("REQ-0001", scoring="SCR-0001"), _row("REQ-0002", scoring="SCR-9001")]}
        authority = scope_gated_denominator(
            matrix, scope_gate=_gate(technical_scoring=("SCR-0001",)))
        self.assertEqual(["SCORING_NOT_IN_TECHNICAL_DENOMINATOR"], authority["excluded"][0]["removal_reasons"])
        self.assertEqual(1, authority["must_or_scoring_total"])

    def test_scoring_header_row_is_not_a_must_item(self) -> None:
        """表头/公式类评分行不要求正文响应（§24 判 NON_RESPONSE_ROW）。"""
        matrix = {"matrix": [_row("REQ-0273", text="详细评分因素", scoring="SCR-028"),
                            _row("REQ-0002", must=True)]}
        authority = scope_gated_denominator(
            matrix, scope_gate=_gate(technical_scoring=("SCR-028",)),
            scoring_class_by_requirement={"REQ-0273": "SCORING_HEADER"})
        self.assertEqual(["NON_RESPONSE_SCORING_ROW"], authority["excluded"][0]["removal_reasons"])
        self.assertEqual(["REQ-0002"], [r["requirement_id"] for r in authority["must_or_scoring_rows"]])

    def test_contextual_scoring_row_leaves_denominator_with_the_scoring_side(self) -> None:
        """Run D 实测形状：REQ-0271…0274 ↔ SCR-026…029 判 TECHNICAL_CONTEXTUAL
        （处置 TECHNICAL_NOT_SCORED，本就不在 §24 技术评分分母里），需求侧必须同步离开。"""
        matrix = {"matrix": [_row("REQ-0271", text="各项评分计算结果四舍五入保留两位小数",
                                  scoring="SCR-026"),
                            _row("REQ-0002", must=True)]}
        authority = scope_gated_denominator(
            matrix, scope_gate=_gate(technical_scoring=("SCR-026",)),
            scoring_class_by_requirement={"REQ-0271": "TECHNICAL_CONTEXTUAL"})
        self.assertEqual(["SCORING_NOT_IN_TECHNICAL_DENOMINATOR"], authority["excluded"][0]["removal_reasons"])
        self.assertEqual("TECHNICAL_CONTEXTUAL", authority["excluded"][0]["scoring_class"])
        self.assertEqual(["REQ-0002"], [r["requirement_id"] for r in authority["must_or_scoring_rows"]])
        self.assertTrue(authority["conservation_holds"])

    def test_must_clause_never_exits_because_of_scoring_class(self) -> None:
        """守卫：MUST 是招标强制项，与"这条挂不挂分"无关，一律留在分母。"""
        matrix = {"matrix": [_row("REQ-0001", must=True, text="服务人员须持证上岗",
                                  scoring="SCR-0001")]}
        authority = scope_gated_denominator(
            matrix, scope_gate=_gate(technical_scoring=("SCR-0001",)),
            scoring_class_by_requirement={"REQ-0001": "TECHNICAL_CONTEXTUAL"})
        self.assertEqual([], authority["excluded"])
        self.assertEqual(["REQ-0001"], [r["requirement_id"] for r in authority["must_or_scoring_rows"]])

    def test_unrouted_row_is_removed(self) -> None:
        matrix = {"matrix": [_row("REQ-0001", must=True, section=None, status="UNMAPPED")]}
        authority = scope_gated_denominator(matrix, scope_gate=_gate())
        self.assertEqual(["NOT_ROUTED"], authority["excluded"][0]["removal_reasons"])

    def test_conservation_holds_over_mixed_matrix(self) -> None:
        matrix = {"matrix": [
            _row("REQ-0001", must=True),
            _row("REQ-0002", must=True, scope="COMMERCIAL_EXCLUDED"),
            _row("REQ-0003", must=True, section=None, status="UNMAPPED"),
            _row("REQ-0004"),
        ]}
        authority = scope_gated_denominator(matrix, scope_gate=_gate(excluded=("REQ-0004",)))
        self.assertTrue(authority["conservation_holds"])
        self.assertEqual(4, authority["matrix_rows_total"])
        self.assertEqual(
            authority["matrix_rows_total"],
            authority["denominator_rows_total"] + authority["removed_total"])

    def test_legacy_quoting_keeps_the_old_number_visible(self) -> None:
        """旧口径不是删掉而是并列可见，否则报告里无法说明分母为什么变小。"""
        matrix = {"matrix": [_row("REQ-0001", must=True), _row("REQ-0002", must=True)]}
        authority = scope_gated_denominator(matrix, scope_gate=_gate(excluded=("REQ-0002",)))
        self.assertEqual(2, authority["legacy_quoting"]["planned_must_before_authority"])
        self.assertEqual(1, authority["must_or_scoring_total"])


class EvidenceEquivalence(unittest.TestCase):
    def test_pricing_note_inside_parentheses_is_registered_not_required(self) -> None:
        """Run D REQ-0282：技术实质是"服务期限为3年"，价格上限按 §3 不得写进正文。"""
        result = split_requirement_evidence("服务期限为3年（费用预算不超人民币570万）。")
        self.assertEqual("服务期限为3年。", result["technical_text"])
        self.assertEqual(["PRICING_FRAGMENT"], [f["reason"] for f in result["registered_fragments"]])
        self.assertEqual("EXPLICIT_FORBIDDEN_BODY_FRAGMENT", result["equivalence_basis"])

    def test_alias_is_rejected_when_it_would_empty_the_requirement(self) -> None:
        """§42：整条都是资信声明时不能靠"免答"算成已覆盖。"""
        result = split_requirement_evidence("必须未被国家安全监管总局列入安全生产不良记录“黑名单”")
        self.assertEqual("REJECTED_ALIAS_WOULD_EMPTY_REQUIREMENT", result["equivalence_basis"])
        self.assertTrue(result["technical_text"])

    def test_plain_technical_text_is_untouched(self) -> None:
        result = split_requirement_evidence("每日两次公共区域巡回保洁")
        self.assertEqual("FULL_TEXT", result["equivalence_basis"])
        self.assertEqual([], result["registered_fragments"])

    def test_empty_input(self) -> None:
        self.assertEqual("NO_TEXT", split_requirement_evidence("")["equivalence_basis"])

    def test_alias_only_fires_for_declared_narrow_pairs(self) -> None:
        hits = evidence_alias_hits("服务期限为3年", "本合同服务期三年，逐年考核")
        self.assertEqual(1, len(hits))
        self.assertEqual("服务期", hits[0]["matched_alias"])
        self.assertEqual([], evidence_alias_hits("投诉闭环时限", "本项目设置门卫岗"))

    def test_alias_never_invents_unrelated_synonyms(self) -> None:
        self.assertEqual([], evidence_alias_hits("绿化养护频次", "本项目绿化面积9163平方米"))


class Qr001UsesOnlyTheAuthority(unittest.TestCase):
    def test_excluded_rows_leave_both_denominator_and_missing_list(self) -> None:
        matrix = {"matrix": [
            _row("REQ-0001", must=True, text="设置秩序维护岗位与交接班记录"),
            _row("REQ-0002", must=True, text="营业执照与资质证书齐全", scope="QUALIFICATION_EXCLUDED"),
        ]}
        gate = _gate(excluded=("REQ-0002",))
        fragments = {"S01-01": _fragment("设置秩序维护岗位与交接班记录，按班次签字确认。")}
        result = evaluate_requirement_coverage_regression(matrix, fragments, scope_gate=gate)
        self.assertEqual("PASS", result["status"])
        self.assertEqual(1, result["planned_must"])
        self.assertEqual([], result["missing"])
        self.assertEqual(REQUIREMENT_DENOMINATOR, result["denominator"])
        self.assertEqual("TECHNICAL_ONLY_SCOPE_GATE", result["denominator_authority"])
        self.assertEqual(1, result["removed_from_denominator_total"])
        self.assertEqual(["REQ-0002"], [r["requirement_id"] for r in result["removed_from_denominator"]])
        self.assertTrue(result["conservation_holds"])
        self.assertEqual(2, result["legacy_planned_must_before_scope_gate"])

    def test_technical_substance_still_fails_when_absent(self) -> None:
        """移除排除项不等于放宽：剩下的技术实质没写出来仍然 FAIL。"""
        matrix = {"matrix": [_row("REQ-0001", must=True, text="按日巡检地下车库并留存记录")]}
        result = evaluate_requirement_coverage_regression(
            matrix, {"S01-01": _fragment("本项目设置客户服务前台。")}, scope_gate=_gate())
        self.assertEqual("FAIL", result["status"])
        self.assertEqual(["REQ-0001"], result["missing"])

    def test_narrow_alias_can_carry_coverage_and_is_reported(self) -> None:
        matrix = {"matrix": [_row("REQ-0282", must=True, text="服务期限为3年")]}
        fragments = {"S01-01": _fragment("本项目合同期三年，期满按招标人最终要求移交。")}
        result = evaluate_requirement_coverage_regression(matrix, fragments, scope_gate=_gate())
        self.assertEqual(1, len(result["requirement_equivalences"]))
        self.assertEqual("DECLARED_NARROW_ALIAS", result["requirement_equivalences"][0]["basis"])
        self.assertEqual(["REQ-0282"], [e["requirement_id"] for e in result["requirement_equivalences"]])
        self.assertEqual("PASS", result["status"])

    def test_header_scoring_row_cannot_fail_the_run(self) -> None:
        matrix = {"matrix": [
            _row("REQ-0273", text="详细评分因素", scoring="SCR-028"),
            _row("REQ-0001", must=True, text="设置项目经理总责与分线负责人"),
        ]}
        fragments = {"S01-01": _fragment("设置项目经理总责与分线负责人，分线签字确认。")}
        strict = evaluate_requirement_coverage_regression(
            matrix, fragments, scope_gate=_gate(technical_scoring=("SCR-028",)),
            scoring_class_by_requirement={"REQ-0273": "SCORING_HEADER"})
        loose = evaluate_requirement_coverage_regression(
            matrix, {"S01-01": _fragment("与本页无关的内容")},
            scope_gate=_gate(technical_scoring=("SCR-028",)))
        self.assertEqual(1, strict["planned_must"])
        self.assertEqual("PASS", strict["status"])
        self.assertEqual(2, loose["planned_must"], "没有分类信息时表头行仍在分母里，不静默放宽")

    def test_no_fragments_and_no_rows_still_passes_cleanly(self) -> None:
        result = evaluate_requirement_coverage_regression({"matrix": []}, {}, scope_gate=_gate())
        self.assertEqual("PASS", result["status"])
        self.assertEqual(0, result["planned_must"])

    def test_gate_reports_which_scoring_class_map_it_read(self) -> None:
        """分类图是谁算出来的必须写进 QR-001：历史矩阵里根本没有 scoring_class 字段。"""
        matrix = {"matrix": [_row("REQ-0001", must=True, text="设置项目经理总责")] }
        stats = {"status": "OK", "source_file": "09_scoring_matrix.json", "persisted": 0,
                 "derived_from_taxonomy": 41, "rows_total": 41, "unclassified_rows": 0,
                 "requirement_ids_mapped": 67}
        result = evaluate_requirement_coverage_regression(
            matrix, {"S01-01": _fragment("设置项目经理总责与分线负责人。")},
            scope_gate=_gate(), scoring_class_map=stats)
        self.assertEqual(stats, result["scoring_class_map"])
        untouched = evaluate_requirement_coverage_regression(
            matrix, {"S01-01": _fragment("设置项目经理总责与分线负责人。")}, scope_gate=_gate())
        self.assertEqual("NOT_PASSED", untouched["scoring_class_map"]["status"])


class GenerationRequirementPackCarriesTheAuthority(unittest.TestCase):
    """§39：被排除的 Requirement 要一路走到生成需求包里，不能只停在 QA 门。"""

    def test_requirement_map_keeps_the_authority_and_excluded_rows(self) -> None:
        from planning.canonical import build_canonical_requirement_map
        from planning.planner import reconcile_requirement_denominator
        matrix = {"matrix": [
            _row("REQ-0001", must=True),
            _row("REQ-0002", must=True, scope="COMMERCIAL_EXCLUDED", text="提供营业执照与财务年报"),
        ]}
        reconcile_requirement_denominator(matrix, _gate(excluded=("REQ-0002",)))
        pack = build_canonical_requirement_map({"pack_id": "PACK-1"}, matrix)
        authority = pack["requirement_denominator_authority"]
        self.assertEqual(REQUIREMENT_DENOMINATOR, authority["denominator"])
        self.assertEqual(["REQ-0002"], authority["excluded_requirement_ids"])
        self.assertNotIn("rows", authority, "分母行集本身不进需求包，避免下游再拿它当全量")
        by_id = {row["requirement_id"]: row for row in pack["matrix"]}
        self.assertFalse(by_id["REQ-0002"]["in_qa_denominator"])
        # 门已排除时行级 scope 不再重复计因，但行级结论必须仍在场，否则读不出两处是否一致。
        self.assertEqual(["FINAL_GATE_EXCLUDED"], by_id["REQ-0002"]["qa_denominator_removal_reasons"])
        removed = authority["excluded"][0]
        self.assertEqual("COMMERCIAL_EXCLUDED", removed["scope_class"])
        self.assertEqual("EXCLUDED", removed["scope_disposition"])
        self.assertNotIn("in_qa_denominator", by_id["REQ-0001"], "留下的行不需要自证在场")

    def test_absent_authority_is_reported_as_empty_not_invented(self) -> None:
        from planning.canonical import build_canonical_requirement_map
        pack = build_canonical_requirement_map({"pack_id": "PACK-1"}, {"matrix": []})
        self.assertEqual({}, pack["requirement_denominator_authority"])


class PlannerDenominatorIsBackfilledFromTheGate(unittest.TestCase):
    def test_reconcile_replaces_the_planned_value_with_the_gate_value(self) -> None:
        from planning.planner import reconcile_requirement_denominator
        matrix = {"matrix": [_row("REQ-0001", must=True), _row("REQ-0002", must=True)],
                  "coverage_summary": {"technical_scoring_total": 99}}
        reconcile_requirement_denominator(matrix, _gate(excluded=("REQ-0002",), technical_scoring=("SCR-0001",)))
        summary = matrix["coverage_summary"]
        self.assertEqual(1, summary["technical_scoring_total"], "规划期的 99 只是估计值，门一出来就要被真值替换")
        self.assertEqual("TECHNICAL_ONLY_SCOPE_GATE", summary["scoring_denominator_authority"])
        self.assertEqual(REQUIREMENT_DENOMINATOR, summary["coverage_denominator"])
        self.assertEqual(1, summary["must_total_after_scope_gate"])
        self.assertEqual(1, summary["qa_denominator_removed_total"])
        self.assertTrue(summary["qa_denominator_conservation_holds"])
        flagged = next(r for r in matrix["matrix"] if r["requirement_id"] == "REQ-0002")
        self.assertFalse(flagged["in_qa_denominator"])
        self.assertEqual(["FINAL_GATE_EXCLUDED"], flagged["qa_denominator_removal_reasons"])
        self.assertNotIn("rows", matrix["requirement_denominator_authority"])

    def test_declared_count_disagreeing_with_the_gate_list_is_flagged_not_zeroed(self) -> None:
        """门上的声明计数不可信时以清单为准，并把冲突留在账上——静默取 0 会让覆盖门永久放行。"""
        from planning.planner import reconcile_requirement_denominator
        gate = _gate(technical_scoring=("SCR-0001", "SCR-0002"))
        gate["technical_scoring_denominator"] = 0
        matrix = {"matrix": [_row("REQ-0001", scoring="SCR-0001")], "coverage_summary": {}}
        reconcile_requirement_denominator(matrix, gate)
        summary = matrix["coverage_summary"]
        self.assertEqual(2, summary["technical_scoring_total"])
        self.assertEqual({"declared_on_gate": 0, "from_gate_item_ids": 2}, summary["scoring_denominator_conflict"])

    def test_agreeing_gate_records_no_conflict(self) -> None:
        from planning.planner import reconcile_requirement_denominator
        gate = _gate(technical_scoring=("SCR-0001",))
        gate["technical_scoring_denominator"] = 1
        matrix = {"matrix": [_row("REQ-0001", scoring="SCR-0001")], "coverage_summary": {}}
        reconcile_requirement_denominator(matrix, gate)
        self.assertNotIn("scoring_denominator_conflict", matrix["coverage_summary"])


class BidderStatusDeclarationIsScopeExcluded(unittest.TestCase):
    def test_blacklist_and_lawful_record_declarations_are_credentials(self) -> None:
        for text in ("必须未被国家安全监管总局列入安全生产不良记录“黑名单”",
                     "我单位近三年在经营活动中无重大违法记录"):
            scope = classify_technical_scope({"normalized_requirement": text})
            self.assertEqual("BIDDER_CREDENTIAL_EXCLUDED", scope, text)
            self.assertTrue(is_excluded_technical_scope(scope), text)

    def test_vendor_blacklist_management_stays_technical(self) -> None:
        """守卫：技术标里的"黑名单管理制度"不能被误当成投标人资信声明而排除。"""
        for text in ("对分包单位实行黑名单管理制度", "建立供应商黑名单清退机制与台账"):
            self.assertFalse(is_excluded_technical_scope(classify_technical_scope(
                {"normalized_requirement": text})), text)


class CommitmentDescriptiveUse(unittest.TestCase):
    """§40：Round 3 遗留的 6 条 TRUE_BLOCK 里，真实件形状都是描述性用法。"""

    def test_client_side_approval_role_is_not_a_bidder_commitment(self) -> None:
        report = evaluate_commitments({}, [], {"artifact": {"paragraphs": [
            "甲方归口联系人负责审批类事项确认与验收意见出具。"]}})
        self.assertEqual([], report["violations"])
        self.assertEqual("NEEDS_REVIEW", report["status"])
        self.assertEqual("C5", report["review_items"][0]["claim_type"])

    def test_interrogative_condition_is_not_a_commitment(self) -> None:
        report = evaluate_commitments({}, [], {"artifact": {"paragraphs": [
            "分级的目的是决定由哪一层负责，以及在什么条件下必须上报。"]}})
        self.assertEqual([], report["violations"])

    def test_assertive_commitment_still_blocks(self) -> None:
        """反向守卫：真正的无依据承诺绝不因这次的收窄而放过。"""
        report = evaluate_commitments({}, [], {"artifact": {"paragraphs": [
            "本公司承诺必须上报每一件事并由项目经理批示。"]}})
        self.assertTrue(report["violations"])
        self.assertEqual("BLOCK", report["status"])

    def test_completion_used_as_modifier_is_suppressed_with_reason(self) -> None:
        report = evaluate_commitments({}, [], {"artifact": {"paragraphs": [
            "移交包由编号清单、整改记录路径和本节已经完成本次闭环的时间说明构成。"]}})
        self.assertEqual([], report["violations"])
        self.assertEqual(1, report["suppressed_count"])
        self.assertIn("定语", report["suppressed"][0]["classification_reason"])

    def test_assertive_completion_claim_still_blocks(self) -> None:
        report = evaluate_commitments({}, [], {"artifact": {"paragraphs": [
            "本项目所有制度文件均已整理成册。"]}})
        self.assertTrue(report["violations"])
        self.assertEqual("RP-FUTURE-STATE", report["violations"][0]["repair_action"])


class FutureStateRepairExists(unittest.TestCase):
    """§40 + MMF008A6-B2：门宣布 RP-FUTURE-STATE 必须有执行端，但执行端**不许造义务**。

    这一组原来考的是"完工主张要被改写成将来承诺"（Round 3 的教训：RP-FUTURE-STATE 只有
    名字没有实现）。Codex A5 Gate 指出那一版实现把无来源的事实主张换成了无来源的**新义务**
    （"将按合同要求 / 将按合同约定"这几个字不是证据），所以本轮把同一组反例改成考
    三条安全性质：没有底稿时不动文本、有底稿无对应义务时只做现状核验边界、
    只有底稿里逐字存在对应义务时将来语态才允许出现。
    断言没有放松：改完仍然要求这一句不再被门槛当作既存事实主张放行。
    """

    def test_completion_claim_without_source_never_becomes_a_new_obligation(self) -> None:
        sentence = "本项目所有服务台账已经建立。"
        # 没有底稿 = 没有证据可言：文本一个字不改，这条主张原样留给门槛。
        self.assertEqual(sentence, _repair_text(sentence))
        self.assertTrue(evaluate_commitments({}, [], {"artifact": {"paragraphs": [sentence]}})
                        ["violations"], "不动文本不等于放行")
        # 有底稿、但底稿里没有"建立台账"这一条现行义务：同样不动文本。
        # 这一族的断言落点在整截短语开头（查在"所有"上），把 是否 挂在 已经 前面
        # 改不了那一头的语义档，所以契约给的处置是继续挡，而不是生产一句
        # "看着已处理、其实照样挡"的话。承诺式措辞一个都不许出现。
        neutral_brief = {"scope": "负责园区秩序维护与保洁作业"}
        repaired = _repair_text(sentence, "负责园区秩序维护与保洁作业")
        self.assertEqual(sentence, repaired)
        for banned in ("将按合同约定建立", "将按合同要求建立", "将建立", "是否已经建立"):
            self.assertNotIn(banned, repaired, "无依据的事实被换成了无依据的承诺")
        self.assertTrue(evaluate_commitments(
            neutral_brief, [], {"artifact": {"paragraphs": [repaired]}})["violations"],
            "契约 HOLD 的这一条必须仍然被门槛挡住")

    def test_completion_claim_reaches_future_register_only_with_bound_obligation(self) -> None:
        """底稿里逐字写着这项义务时，将来语态才是有据的改写。"""
        sentence = "本项目所有服务台账已经建立。"
        brief = {"duty": "投标人须建立服务台账并按月提交甲方确认"}
        repaired = _repair_text(sentence, "投标人须建立服务台账并按月提交甲方确认")
        self.assertNotIn("已经建立", repaired)
        self.assertIn("将", repaired)
        self.assertEqual([], evaluate_commitments(
            brief, [], {"artifact": {"paragraphs": [repaired]}})["violations"])

    def test_repair_is_idempotent(self) -> None:
        once = _repair_text("本项目所有服务台账已经建立。")
        self.assertEqual(once, _repair_text(once))
        neutral = _repair_text("本项目所有服务台账已经建立。", "负责园区秩序维护与保洁作业")
        self.assertEqual(neutral, _repair_text(neutral, "负责园区秩序维护与保洁作业"),
                         "处置不能越跑越长")

    def test_descriptive_completion_is_not_rewritten(self) -> None:
        sentence = "移交包含本节已经完成本次闭环的时间说明。"
        self.assertEqual(sentence, _repair_completion_claims(sentence))

    def test_credential_material_is_left_to_the_evidence_boundary(self) -> None:
        """资信页由 Bidder Evidence 边界另行提供，不能被改写成"中标后整理成册"。"""
        sentence = "所有人员资历证明已整理成册。"
        self.assertEqual(sentence, _repair_completion_claims(sentence))

    def test_every_declared_repair_action_has_an_effect(self) -> None:
        """每个宣告的 repair_action 都有执行端——但执行端的效果受 §B2 安全契约约束。

        两件事分开考：①承诺语汇（"采用外包模式"）必须真的被改，这与底稿无关；
        ②完工主张在拿不到底稿、或中和落不到断言那一头上时**必须保持 BLOCK**，
        而不是被一句新承诺遮掉——Round 3 要的"有执行端"不等于"必须把门改绿"。
        """
        generated = {"artifact": {"paragraphs": ["本项目采用外包模式并承担全部风险。",
                                                "所有制度文件均已整理成册。"]}}
        report = evaluate_commitments({}, [], generated)
        from governance.commitment_provenance import apply_local_repairs
        after = evaluate_commitments({}, [], apply_local_repairs(generated))
        for action in report["repair_actions"]:
            self.assertIn(action, {"RP-DOWNGRADE-RECOMMENDATION", "RP-CONDITIONALIZE",
                                   "RP-FUTURE-STATE", "RP-DOWNGRADE-ABSOLUTE"})
        self.assertEqual([], [v for v in after["violations"] if v["text"] == "采用外包模式"])
        # 没有底稿：完工主张原样留着，门槛照旧挡（不许用一句承诺遮过去）。
        held = [v for v in after["violations"] if v["claim_type"] == "UNSUPPORTED_COMPLETION_CLAIM"]
        self.assertEqual(1, len(held), "无底稿时不得动文本，也不得偷偷放行")
        # 有底稿、但底稿里没写这项义务：同一条主张仍由门槛挡，产物里不许出现承诺式措辞。
        brief = {"scope": "负责园区秩序维护与保洁作业"}
        repaired_with_brief = apply_local_repairs(generated, brief)
        self.assertEqual("所有制度文件均已整理成册。",
                         repaired_with_brief["artifact"]["paragraphs"][1])
        self.assertNotIn("将按合同要求整理成册",
                         repaired_with_brief["artifact"]["paragraphs"][1])
        self.assertTrue([v for v in evaluate_commitments(
            brief, [], repaired_with_brief)["violations"]
            if v["claim_type"] == "UNSUPPORTED_COMPLETION_CLAIM"])


class MetadataLeakGateAndRepair(unittest.TestCase):
    """§43：内部 Section/CU/Requirement ID、门名、Prompt 语言不得进客户成品。"""

    def test_body_identifier_leak_is_detected_with_tokens(self) -> None:
        findings = evaluate_artifact({"artifact": {"paragraphs": [
            "本条按 REQ-0273 与 CU-0012 响应，详见 S05-02 章节。"]}})["findings"]
        leak = [f for f in findings if f.get("leaked_tokens")]
        self.assertEqual(1, len(leak))
        self.assertEqual("CLIENT_ARTIFACT_INTERNAL_META", leak[0]["rule_id"])
        self.assertEqual(["CU-0012", "REQ-0273", "S05-02"], leak[0]["leaked_tokens"])
        self.assertEqual("STRIP_INTERNAL_META", leak[0]["repair_action"])

    def test_structural_id_fields_are_not_body_text(self) -> None:
        self.assertTrue(_is_structural_key("artifact.sections[0].section_id"))
        self.assertFalse(_is_structural_key("artifact.sections[0].paragraphs[3]"))
        report = evaluate_artifact({"artifact": {"sections": [{
            "section_id": "CH01", "paragraphs": ["本项目设置客户服务前台岗位。"]}]}})
        self.assertEqual([], [f for f in report["findings"] if f.get("leaked_tokens")])

    def test_detector_and_repair_agree_on_internal_tone(self) -> None:
        """Round 3 的真实缺陷：QA 认得这句，hygiene 既不报也不改，于是它进了客户 DOCX。"""
        sentence = ("对于甲方内部组织架构等未在本项目招标文本中明确的事实，"
                    "本方案按未确认处理，不作为承诺前提写入正文，需要引用时列入待澄清事项确认。")
        self.assertEqual(["INTERNAL_GOVERNANCE_TONE"],
                         [f["rule_id"] for f in hygiene_findings(sentence)])
        repaired = rewrite_customer_hygiene(sentence)
        self.assertNotIn("不作为承诺前提写入正文", repaired)
        self.assertIn("将在中标后按合同约定确认", repaired)
        self.assertEqual([], hygiene_findings(repaired))
        self.assertEqual(repaired, rewrite_customer_hygiene(repaired))

    def test_tree_repair_clears_every_internal_marker_it_detects(self) -> None:
        artifact = {"sections": [{"section_id": "S01-01", "paragraphs": [
            "见 CH03 与 CU-0012 的说明。", "本方案按未确认处理，不作为承诺前提写入正文。"]}]}
        repaired = apply_hygiene_tree(artifact)
        self.assertEqual([], [f for f in evaluate_artifact(repaired)["findings"]
                              if f.get("rule_id") == "CLIENT_ARTIFACT_INTERNAL_META"
                              and f.get("path", "").endswith("paragraphs[0]")])
        self.assertNotIn("不作为承诺前提写入正文", repaired["sections"][0]["paragraphs"][1])
        self.assertEqual(repaired, apply_hygiene_tree(repaired))

    def test_tree_repair_does_not_rewrite_structural_addressing(self) -> None:
        """Run D 实证缺陷：修复端把 section_id 本身改成了中文，寻址键随之失效。

        检测端按路径豁免这些字段，修复端必须同口径；否则 CH01…CH11 全塌成"本章"，
        覆盖映射、一致性核查与装配谱系再也对不回规划层。
        """
        artifact = {"sections": [
            {"section_id": "CH01", "content_unit_id": "CU-0012", "reference_sections": ["S03-02"],
             "paragraphs": ["本节按 CH03 与 CU-0012 的口径执行。"]},
            {"section_id": "S08-01", "paragraphs": ["巡检记录按频次归档。"]}]}
        repaired = apply_hygiene_tree(artifact)
        self.assertEqual("CH01", repaired["sections"][0]["section_id"])
        self.assertEqual("CU-0012", repaired["sections"][0]["content_unit_id"])
        self.assertEqual(["S03-02"], repaired["sections"][0]["reference_sections"])
        self.assertEqual("S08-01", repaired["sections"][1]["section_id"])
        self.assertEqual("本节按 本章 与 本项内容 的口径执行。", repaired["sections"][0]["paragraphs"][0])

    def test_structural_fields_do_not_produce_hygiene_findings(self) -> None:
        self.assertEqual([], hygiene_findings("CH03", "artifact.sections[3].section_id"))
        self.assertEqual([], hygiene_findings("S03-02", "artifact.matrix[0].primary_section_id"))
        self.assertIn("BROKEN_CROSS_REFERENCE", [f["rule_id"] for f in hygiene_findings("详见 S03-02。")])

    def test_clean_customer_text_is_not_flagged(self) -> None:
        report = evaluate_artifact({"artifact": {"paragraphs": [
            "本项目设置秩序维护岗位，按日巡检公共区域并留存台账。"]}})
        self.assertEqual([], [f for f in report["findings"]
                              if f["rule_id"] == "CLIENT_ARTIFACT_INTERNAL_META"])


class OfflineBoundary(unittest.TestCase):
    def test_authority_module_imports_no_network_or_provider(self) -> None:
        source = (APP / "planning" / "scope_gated_requirements.py").read_text(encoding="utf-8")
        for token in ("providers", "requests", "httpx", "openai", "api_key"):
            self.assertNotIn(token, source.lower())

    def test_authority_lives_in_planning_and_does_not_import_longform(self) -> None:
        source = (APP / "planning" / "scope_gated_requirements.py").read_text(encoding="utf-8")
        self.assertNotIn("longform", source)


if __name__ == "__main__":
    unittest.main()
