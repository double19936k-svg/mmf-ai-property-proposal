"""MMF-007C Round 3 §40-§44：P0-5 Bidder Evidence 交付边界。

Run C 那一轮没有提供投标人真实资信材料，所以台账为空**是正常输入**（§40），不是证据链坏了；
真正会被做错的两件事是：① 为了"看起来完整"生成企业证书/资质/AAA/ISO/类似业绩附件页来凑页数
（§41 明令禁止），② 把缺证据这件事升级成 TECHNICAL_FAILED（§43 只允许 NEEDS_REVIEW）。
本文件钉住五件事：

1. §40 空台账要自己讲清楚"空是正常"，并保留已核验行数，免得下游把 [] 读成数据缺陷去补假证据；
2. §41 资信附件页在**逐页实测**里必须是单独一类（越界页只报一个总数时，"2 页报价"和
   "2 页证书附件"长得一样），且顺带提到"营业执照"的技术页不得被误扣；
3. §42 唯一允许的形状是行内【待投标人确认：X】；整页只剩提示词的页不算技术有效页，
   整页空白的履历表必须被装配端删掉，而不是缩成"更小的空白表"；
4. §43 本门对 Delivery 的作用上限就是待复核，多少条都不构成 TECHNICAL_FAILED；
5. §44 随件写**内部** Technical Review Summary，只列待确认技术事实，且 §41 的页数一律取
   实测：没量过时留 None，绝不写 0 冒充"确认没生成资信页"。
"""
from __future__ import annotations

import json
import re
import shutil
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

from delivery_state import DELIVERY_REVIEW, DELIVERY_TECHNICAL, classify_fail_soft
from governance.artifact_qa import (PLACEHOLDER as PLACEHOLDER_TOKEN_QA, TENDER_TEMPLATE,
                                    TRANSPORT_RESIDUE)
from governance.table_qa import classify_table, plan_table, table_metrics
from longform.factory import LongformGenerationFactory
from longform.final_assembly import FORBIDDEN_VISIBLE
from longform.orchestrator import FIRST_DRAFT_PAGE_CLASSIFICATION_FILE
from planning.content_engineering import (build_bidder_evidence_ledger,
                                          build_technical_review_summary,
                                          evaluate_bidder_evidence, repair_template_leakage,
                                          scan_scope_text)
from planning.page_calibration import (EXCLUDED_SCOPE_GROUP_NAMES, EXCLUDED_SCOPE_PATTERNS,
                                       classify_pages, page_report)
from tests.test_mmf006e_r21_q01_q04 import _minimal_factory
from tests.test_mmf007c_table_density import _artifact, _build, _paragraph, _table

_BODY = ("本单元规定保洁、绿化、秩序维护与设备设施各专业岗位的作业流程、频次与质量核查口径，"
         "并明确责任岗位、记录留痕方式与问题整改闭环时限，供现场班组按同一标准执行与考核。") * 3
# §42 允许的唯一形状：行内一句提示，不扩表格、不扩附件页。
_HINT_INLINE = "项目经理到岗安排见本节排班表，【待投标人确认：项目经理姓名】后由品质主管复核。"
# §42 禁止的形状之一：整页只剩提示词，看着像一页内容，其实一个字都没定。
_HINT_PAGE = "本章人员与设备清单如下：" + "【待投标人确认：岗位人员配置】" * 18
# §41 点名的形状：证书/资质/业绩/履历附件页，整页只有附件目录，没有任何技术口径。
_CREDENTIAL_PAGE = ("附：企业证书附件汇总，包含ISO证书、AAA信用证书、质量管理体系认证证书与荣誉证书扫描件；"
                    "另附资质证书附件、类似项目业绩附件、用户评价书、合同复印件以及项目经理人员履历表。") * 2
# 报价类越界页：同样该单独数，不能和资信页混成一个数。
_PRICING_PAGE = ("本部分列示投标报价表、最高限价与报价方式说明，另附履约保证金、投标保证金缴纳凭证，"
                 "供评标委员会开标时符合性审查。") * 2
# 资信词命中已够 4 次、但整页仍是技术口径的页：词表加宽之后最容易误伤的形状。
# 它必须靠"页内有技术信号词"这一维守住，否则技术页会被凭空扣掉。
_TECH_PAGE_MENTIONING_CREDENTIAL = ("本节的岗位职责、培训安排、巡检频次与台账记录表填写规则以现场实施为准；"
                                    "企业营业执照、资格证明与类似业绩材料以资格审查文件为准，本方案不重复展开。")
# Run C 第 28 页的形状：整页讲的是商务/报价口径的响应组织方式，顺带出现一次"合同复印件"。
# 它是越界页（该扣），但**不是** §41 禁的资信附件页 —— 资信词在这页不占多数。
_CREDENTIAL_MENTION_IN_COMMERCIAL_PAGE = (
    "本部分说明商务部分的响应口径：商务条款、商务偏离与商务响应的对应关系，价格部分与评标基准价不在本节展开，"
    "投标报价表由商务文件承载；需要合同复印件等有效证明材料的事项，本节只登记材料种类与所在文件位置，"
    "代理服务费另计，本节不自设具体时点。") * 2


def _ledger(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return build_bidder_evidence_ledger({"bidder_evidence": rows or []})


class Be40EmptyLedgerIsNormalInput(unittest.TestCase):
    """§40：本轮没有资信材料 → 台账为空是正常状态，报告必须自己说明这一点。"""

    def test_be40_empty_ledger_is_labelled_and_not_a_failure(self) -> None:
        gate = evaluate_bidder_evidence({"sections": [{"body": _BODY}]}, _ledger())
        self.assertEqual(0, gate["ledger_rows"])
        self.assertEqual("EMPTY_AS_SUPPLIED", gate["ledger_state"])
        self.assertTrue(gate["empty_ledger_is_normal"])
        self.assertEqual("PASS", gate["status"], "空台账 + 无主张，不得凭空造出缺陷")
        self.assertIn("不得编造", gate["empty_ledger_note"])

    def test_be40_empty_ledger_never_invents_evidence_links(self) -> None:
        ledger = _ledger()
        self.assertEqual([], ledger["verified_evidence_ids"])
        gate = evaluate_bidder_evidence(
            {"sections": [{"body": "我司已通过ISO认证并拥有AAA信用等级。"}]}, ledger)
        self.assertEqual([], gate["claim_evidence_links"], "没有台账就没有可链接的证据，更不能编")
        self.assertEqual(0, gate["ledger_verified_rows"])
        # 缺证据只以 NEEDS_REVIEW 主张的形式出现，且给出的是行内提示建议。
        self.assertTrue(gate["findings"])
        self.assertTrue(all(row["recommended_placeholder"].startswith("【待投标人确认：")
                            for row in gate["findings"]))

    def test_be40_supplied_ledger_still_reports_the_real_state(self) -> None:
        supplied = _ledger([{"evidence_id": "BE-0001", "claim": "我司已通过ISO认证",
                             "verified": True, "source": "证书扫描件"}])
        gate = evaluate_bidder_evidence({"sections": [{"body": "我司已通过ISO认证。"}]}, supplied)
        self.assertEqual("SUPPLIED", gate["ledger_state"])
        self.assertFalse(gate["empty_ledger_is_normal"])
        self.assertEqual("PASS", gate["status"])
        self.assertEqual(["BE-0001"], gate["claim_evidence_links"][0]["evidence_ids"])


class Be41CredentialPagesAreMeasuredNotAssumed(unittest.TestCase):
    """§41：不得自动生成资信附件页凑页数 —— 前提是这种页的数量量得出来。"""

    def test_be41_excluded_pages_are_split_by_boundary(self) -> None:
        report = classify_pages([_BODY, _TECH_PAGE_MENTIONING_CREDENTIAL, _CREDENTIAL_PAGE,
                                 _PRICING_PAGE, _BODY])
        # 样张本身必须真的命中够 3 次，否则这条测的是别的东西而不是这条守卫。
        self.assertGreaterEqual(sum(len(pattern.findall(_TECH_PAGE_MENTIONING_CREDENTIAL))
                                    for pattern in EXCLUDED_SCOPE_PATTERNS), 3)
        self.assertEqual(2, report["excluded_scope_pages"])
        self.assertEqual(1, report["credential_attachment_pages_present"])
        self.assertEqual(1, report["credential_attachment_pages"])
        details = report["credential_attachment_page_details"]
        self.assertEqual([3], [row['page'] for row in details],
                         "附件页要报得出页号，只有总数就没法复核这一页到底是不是附件页")
        self.assertTrue(details[0]['dominant'] and details[0]['credential_hits'] > 0)
        hits = report["excluded_scope_group_hits"]
        self.assertEqual({"PRICING", "COMMERCIAL", "CREDENTIAL", "PROCEDURAL"}, set(hits))
        self.assertGreater(hits["CREDENTIAL"], 0)
        self.assertGreater(hits["PRICING"], 0)
        # 分组命中只统计**已判越界**的页：讲技术的页即使提到"营业执照"也不进这里，
        # 它仍然是技术有效页（3 页），这是词表加宽之后最容易被扣错的一侧。
        self.assertEqual(3, report["technical_effective_pages"])

    def test_be41_technical_page_mentioning_credentials_is_not_deducted(self) -> None:
        report = classify_pages([_BODY, _TECH_PAGE_MENTIONING_CREDENTIAL, _BODY])
        self.assertEqual(0, report["excluded_scope_pages"])
        self.assertEqual(0, report["credential_attachment_pages_present"])
        self.assertEqual(3, report["technical_effective_pages"])
        self.assertEqual({name: 0 for name in EXCLUDED_SCOPE_GROUP_NAMES},
                         report["excluded_scope_group_hits"])

    def test_be41_credential_mention_is_not_counted_as_an_attachment_page(self) -> None:
        """Run C 第 28 页那一类：越界页里只提了一次"合同复印件"，不能报成生成了资信附件页。"""
        report = classify_pages([_BODY, _CREDENTIAL_MENTION_IN_COMMERCIAL_PAGE])
        self.assertEqual(1, report["excluded_scope_pages"])
        self.assertEqual(1, report["credential_attachment_pages_present"], "宽口径要点名")
        self.assertEqual(0, report["credential_attachment_pages"], "资信词在这页不占多数，就不是附件页")
        self.assertFalse(report["credential_attachment_page_details"][0]["dominant"])
        self.assertGreater(report["credential_attachment_page_details"][0]["other_group_hits"],
                           report["credential_attachment_page_details"][0]["credential_hits"])

    def test_be41_page_report_and_pagination_share_the_same_fields(self) -> None:
        classification = classify_pages([_BODY, _CREDENTIAL_PAGE])
        doc = page_report(classification, effective_chars=len(_BODY))
        self.assertEqual(1, doc["credential_attachment_pages_present"])
        self.assertEqual(classification["credential_attachment_pages"], doc["credential_attachment_pages"])
        self.assertEqual(classification["credential_attachment_page_details"],
                         doc["credential_attachment_page_details"])
        self.assertEqual(classification["excluded_scope_group_hits"], doc["excluded_scope_group_hits"])
        # 没有实测时不得凭空报"确认没有资信页"：measured/basis 要自己承认没量过。
        empty = page_report({}, effective_chars=9000)
        self.assertEqual({}, empty["excluded_scope_group_hits"])
        self.assertFalse(empty["measured"])

    def test_be41_body_scope_scan_names_attachment_pages(self) -> None:
        """正文级（非整页级）判据：附件目录字样要能被范围扫描点名。"""
        hits = scan_scope_text([("artifact.sections[0].body", _CREDENTIAL_PAGE)])
        self.assertTrue(hits, "证书/资质/业绩附件页的标题写法必须落在正文范围扫描里")
        self.assertTrue(all(row["severity"] in {"NEEDS_REVIEW", "AUTO_REPAIR"} for row in hits))


class Be42LightweightHintOnly(unittest.TestCase):
    """§42：只允许行内轻量提示，不得扩成整页空白履历表。"""

    def test_be42_inline_hint_is_counted_and_not_a_defect(self) -> None:
        gate = evaluate_bidder_evidence({"sections": [{"body": _HINT_INLINE}]}, _ledger())
        self.assertEqual(1, gate["confirmation_hint_count"])
        self.assertIn("【待投标人确认：项目经理姓名】", gate["confirmation_hints"][0]["hint"])
        self.assertEqual([], gate["findings"], "提示本身不是无证据主张")
        self.assertEqual("PASS", gate["status"])

    def test_be42_hint_only_page_is_not_a_technical_page(self) -> None:
        report = classify_pages([_BODY, _HINT_PAGE, _BODY])
        self.assertGreaterEqual(report["placeholder_pages"], 1)
        self.assertEqual(2, report["technical_effective_pages"], "整页提示词不承载技术内容")

    def test_be42_blank_resume_table_is_dropped_not_rendered(self) -> None:
        resume = _table("项目经理履历表", ["姓名", "职称", "执业年限", "类似项目业绩", "证明附件"],
                        [["", "", "", "", ""] for _ in range(12)])
        self.assertEqual("empty_scaffold",
                         classify_table(table_metrics(resume["columns"], resume["rows"],
                                                      title=resume["title"])))
        plan = plan_table(resume)
        self.assertEqual("drop", plan["action"])
        artifact = _artifact([{"heading": "人员与岗位设置", "level": 1,
                               "paragraphs": [_paragraph(3)], "tables": [resume]}])
        with tempfile.TemporaryDirectory() as tmp:
            _docx, ledger = _build(Path(tmp), artifact)
            self.assertEqual(1, ledger["tables_planned"])
            self.assertEqual(1, ledger["tables_dropped"])
            self.assertEqual(0, ledger["tables_rendered"], "空白履历表不得占纸页")

    def test_be42_leaked_double_marker_title_is_not_a_content_page(self) -> None:
        """表题里漏出的双重占位标记会被判成待填写页，不进入技术有效页。"""
        report = classify_pages([_BODY, "附表【待投标人确认：格式与编号】【待投标人填写】"
                                        + "【待填写】" * 20])
        self.assertEqual(1, report["technical_effective_pages"])
        self.assertGreaterEqual(report["placeholder_pages"], 1)

    def test_be42_hint_survives_every_leakage_gate(self) -> None:
        """§42 允许的行内提示，不得被任何"泄漏残留"门当成待清理内容删掉。

        这四道门都是 BLOCK 级的文本清洗，误伤一条提示等于把 §42 唯一的合法形状删干净，
        剩下投标人只能凭空编造事实。
        """
        self.assertFalse(TENDER_TEMPLATE.search(_HINT_INLINE) or TENDER_TEMPLATE.search(_HINT_PAGE))
        self.assertFalse(TRANSPORT_RESIDUE.search(_HINT_INLINE))
        self.assertFalse(PLACEHOLDER_TOKEN_QA.search(_HINT_INLINE) or PLACEHOLDER_TOKEN_QA.search(_HINT_PAGE))
        self.assertEqual([], [word for word in FORBIDDEN_VISIBLE
                              if word in _HINT_INLINE or word in _HINT_PAGE])
        repaired, changed = repair_template_leakage(
            {"sections": [{"body": _HINT_INLINE}, {"body": _HINT_PAGE}]}, "")
        self.assertEqual(0, changed)
        self.assertEqual(_HINT_INLINE, repaired["sections"][0]["body"])

    def test_be42_placeholder_ruler_counts_the_whole_bracketed_marker(self) -> None:
        """尺子修正：占位段按整段计，5 字标记与 15 字标记不能都只算 2 个字。

        旧口径只数到"【待"，于是"整页都是待填写标记"的页永远够不到 0.30 那条线，
        会被读成技术有效页。样张特意落在"窄口径看不见、宽口径看得见"的那一段：
        每个标记按 2 字算都 <0.30，按整段算都 ≥0.30。
        Run C 404 页只读复算：两口径只差 1 页（392 → 391 技术有效页），
        见 r3_placeholder_token_probe_runC.json，阈值因此没有重标。
        """
        for marker, count in (("【待填写】", 40), ("【待投标人确认：岗位人员配置】", 15)):
            page = _BODY + marker * count
            text = re.sub(r"\s+", "", page)
            wide_share = len(marker) * count / len(text)
            narrow_share = 2 * count / len(text)
            self.assertGreaterEqual(wide_share, 0.30, marker)
            self.assertLess(narrow_share, 0.30, marker)
            report = classify_pages([page])
            self.assertEqual(["PLACEHOLDER"], report["page_classes"], marker)
            self.assertEqual(0, report["technical_effective_pages"], "整页待填不承载技术内容")


class Be43DeliveryEffectIsReviewOnly(unittest.TestCase):
    """§43：Bidder Evidence 影响 Delivery 的上限就是 NEEDS_REVIEW。"""

    def _docx(self, tmp: Path) -> Path:
        artifact = _artifact([{"heading": "服务总体思路", "level": 1,
                               "paragraphs": [_paragraph(1), _paragraph(2)]}])
        docx, _ledger_row = _build(tmp, artifact)
        return docx

    def test_be43_unsupported_claims_never_make_technical_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docx = self._docx(Path(tmp))
            gate = evaluate_bidder_evidence({"sections": [
                {"body": "我司已通过ISO认证并拥有AAA信用等级，自有呼叫中心与400电话平台。"}]}, _ledger())
            self.assertEqual("NEEDS_REVIEW", gate["status"])
            decision = classify_fail_soft(artifact_path=docx, findings=list(gate["findings"]),
                                          artifact_qa={"status": "PASS", "findings": []})
            self.assertNotEqual(DELIVERY_TECHNICAL, decision["delivery_status"])
            self.assertEqual(DELIVERY_REVIEW, decision["delivery_status"])
            self.assertTrue(decision["artifact_available"])

    def test_be43_volume_of_findings_does_not_change_the_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docx = self._docx(Path(tmp))
            findings = [{"rule_id": "UNSUPPORTED_BIDDER_CLAIM", "severity": "NEEDS_REVIEW",
                         "path": f"artifact.sections[{index}]", "claim": f"我司已具备资质{index}"}
                        for index in range(20)]
            decision = classify_fail_soft(artifact_path=docx, findings=findings,
                                         artifact_qa={"status": "PASS", "findings": []})
            self.assertEqual(DELIVERY_REVIEW, decision["delivery_status"])
            self.assertEqual(20, decision["review_count"])

    def test_be43_gate_declares_its_own_ceiling(self) -> None:
        gate = evaluate_bidder_evidence({"sections": [{"body": "我司已建立自有平台。"}]}, _ledger())
        self.assertTrue(gate["fail_soft"])
        self.assertIn("TECHNICAL_FAILED", gate["delivery_effect"])
        self.assertIn("不构成", gate["delivery_effect"])


class Be44TechnicalReviewSummary(unittest.TestCase):
    """§44：随件附内部 Technical Review Summary，不输出完整商务/资信清单。"""

    def setUp(self) -> None:
        self.gate = evaluate_bidder_evidence({"sections": [
            {"body": "我司已通过ISO认证并拥有AAA信用等级。"}, {"body": _HINT_INLINE}]}, _ledger())

    def _summary(self, **kwargs: Any) -> dict[str, Any]:
        return build_technical_review_summary(self.gate, **kwargs)

    def test_be44_lists_pending_technical_facts_only(self) -> None:
        summary = self._summary(excluded_scope_pages=2, credential_attachment_pages=0,
                                page_basis="FINAL_DOCX_PAGINATION")
        self.assertEqual("INTERNAL_ONLY", summary["audience"])
        self.assertEqual(self.gate["status"], summary["gate_status"])
        self.assertEqual("EMPTY_AS_SUPPLIED", summary["ledger_state"])
        self.assertTrue(summary["ledger_empty_is_normal"])
        self.assertEqual(1, summary["unsupported_claim_count"])
        self.assertEqual(2, summary["pending_confirmation_count"], "一条主张 + 一条行内提示")
        self.assertEqual({"PENDING_BIDDER_CONFIRMATION"},
                         {row["state"] for row in summary["pending_confirmation_items"]})
        self.assertTrue(all(row["hint"].startswith("【待投标人确认：")
                            for row in summary["pending_confirmation_items"]))
        self.assertEqual(2, summary["excluded_scope_page_count"])
        self.assertEqual(0, summary["credential_attachment_page_count"])
        self.assertEqual("FINAL_DOCX_PAGINATION", summary["page_measurement_basis"])

    def test_be44_keeps_both_credential_layers_separate(self) -> None:
        """§41 的两层口径要原样进汇总：附件页数与"提到过"的点名数不得互相顶替。"""
        summary = self._summary(credential_attachment_pages=0, credential_attachment_pages_mentioned=1,
                                credential_attachment_page_details=[
                                    {"page": 28, "credential_hits": 1, "other_group_hits": 8,
                                     "dominant": False}])
        self.assertEqual(0, summary["credential_attachment_page_count"])
        self.assertEqual(1, summary["credential_attachment_pages_mentioned"])
        self.assertEqual([28], [row["page"] for row in summary["credential_attachment_page_details"]])
        self.assertIn("占多数", summary["credential_attachment_page_rule"])

    def test_be44_is_not_a_commercial_or_qualification_checklist(self) -> None:
        summary = self._summary()
        self.assertFalse(summary["commercial_or_qualification_checklist_included"])
        self.assertTrue(summary["technical_body_only"])
        blob = json.dumps(summary, ensure_ascii=False)
        for needle in ("投标保证金", "履约保证金", "营业执照", "开标", "报价表", "商务偏离"):
            self.assertNotIn(needle, blob, f"内部汇总不得抄进{needle}这类商务/资信条目")
        self.assertLessEqual(summary["pending_confirmation_count"], 3)

    def test_be44_unmeasured_pages_stay_none_instead_of_zero(self) -> None:
        summary = self._summary()
        self.assertIsNone(summary["excluded_scope_page_count"])
        self.assertIsNone(summary["credential_attachment_page_count"])
        self.assertEqual("NOT_MEASURED", summary["page_measurement_basis"])
        self.assertIn("None 表示", summary["credential_attachment_page_rule"])

    def test_be44_deduplicates_repeated_claims_and_hints(self) -> None:
        gate = evaluate_bidder_evidence({"sections": [{"body": _HINT_INLINE}, {"body": _HINT_INLINE}]},
                                        _ledger())
        self.assertEqual(2, gate["confirmation_hint_count"])
        summary = build_technical_review_summary(gate)
        self.assertEqual(1, summary["pending_confirmation_count"])
        self.assertEqual(2, summary["confirmation_hint_count"], "提示条数按出现次数，条目按去重后")


class BeWiringSummaryLandsBesideDelivery(unittest.TestCase):
    """§44 落盘：交付阶段写真文件，页数优先取最终 DOCX，退一步才取初稿实测。

    写盘走 app_core 的 `write_json`，它带工作范围守卫，所以试炼目录用应用自己的
    `runtime/tmp`（同一个 allowed root），不绕过守卫、也不落到系统临时目录去。
    """

    def setUp(self) -> None:
        import app_core
        from paths import current as current_roots

        self.app_core = app_core
        self.gate = evaluate_bidder_evidence({"sections": [{"body": _HINT_INLINE}]}, _ledger())
        scratch = current_roots().temp_root / f"r3p05_wiring_{self.id().split('.')[-1]}"
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, scratch, True)
        self.scratch = scratch

    def _run_dir(self, classification: dict[str, Any] | None) -> Path:
        run_dir = self.scratch / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        if classification is not None:
            (run_dir / FIRST_DRAFT_PAGE_CLASSIFICATION_FILE).write_text(
                json.dumps(classification), encoding="utf-8")
        return run_dir

    def test_wiring_prefers_final_docx_measurement(self) -> None:
        run_dir = self._run_dir({
            "excluded_scope_pages": 1, "credential_attachment_pages_present": 1,
            "credential_attachment_pages": 1, "credential_attachment_page_details": [{"page": 2}]})
        summary = self.app_core._write_technical_review_summary(run_dir, self.gate, {
            "page_classification_status": "MEASURED", "excluded_scope_pages": 3,
            "credential_attachment_pages_present": 1, "credential_attachment_pages": 0,
            "credential_attachment_page_details": [{"page": 28, "credential_hits": 1,
                                                    "other_group_hits": 8, "dominant": False}]})
        self.assertEqual("FINAL_DOCX_PAGINATION", summary["page_measurement_basis"])
        self.assertEqual(3, summary["excluded_scope_page_count"])
        self.assertEqual(0, summary["credential_attachment_page_count"], "取的是最终 DOCX，不是初稿那份")
        self.assertEqual(1, summary["credential_attachment_pages_mentioned"])
        self.assertEqual(summary, json.loads(
            (run_dir / "technical_review_summary.json").read_text(encoding="utf-8")))

    def test_wiring_falls_back_to_first_draft_and_says_so(self) -> None:
        run_dir = self._run_dir({
            "excluded_scope_pages": 1, "credential_attachment_pages_present": 1,
            "credential_attachment_pages": 1, "credential_attachment_page_details": [{"page": 7}]})
        summary = self.app_core._write_technical_review_summary(run_dir, self.gate, {})
        self.assertEqual("FIRST_DRAFT_PAGINATION", summary["page_measurement_basis"])
        self.assertEqual(1, summary["credential_attachment_page_count"])
        self.assertEqual([7], [row["page"] for row in summary["credential_attachment_page_details"]])

    def test_wiring_without_any_measurement_reports_none_not_zero(self) -> None:
        run_dir = self._run_dir(None)
        summary = self.app_core._write_technical_review_summary(run_dir, self.gate, {})
        self.assertEqual("NOT_MEASURED", summary["page_measurement_basis"])
        self.assertIsNone(summary["credential_attachment_page_count"])
        self.assertIsNone(summary["excluded_scope_page_count"])

    def test_wiring_survives_an_unreadable_classification_file(self) -> None:
        run_dir = self._run_dir(None)
        (run_dir / FIRST_DRAFT_PAGE_CLASSIFICATION_FILE).write_text("{", encoding="utf-8")
        summary = self.app_core._write_technical_review_summary(run_dir, self.gate, {})
        self.assertEqual("NOT_MEASURED", summary["page_measurement_basis"])


class BeRegressionPromptsAndLedgerHold(unittest.TestCase):
    """§50：本轮改动不得动掉既有的 Bidder Evidence / Technical-Only 能力。"""

    _CONTEXT = {"section_contract": {
        "section_id": "S09-01", "section_title": "人员与岗位", "must_cover": ["岗位设置"],
        "target_words": {"min": 80, "max": 200}, "source_requirements": ["REQ-0009"],
        "required_processes": []}}

    def test_regression_unverified_ledger_row_still_counts_as_missing_evidence(self) -> None:
        ledger = _ledger([{"evidence_id": "BE-0002", "claim": "我司已具备同类银行项目服务业绩",
                           "verified": False, "source": "待补"}])
        self.assertEqual(1, len(ledger["rows"]))
        self.assertEqual([], ledger["verified_evidence_ids"])
        gate = evaluate_bidder_evidence(
            {"sections": [{"body": "我司已具备同类银行项目服务业绩。"}]}, ledger)
        self.assertEqual("NEEDS_REVIEW", gate["status"], "未核验条目仍按缺证据处理")
        self.assertEqual("SUPPLIED", gate["ledger_state"])
        self.assertEqual([], gate["claim_evidence_links"])

    def test_regression_both_prompts_name_the_forbidden_attachment_pages(self) -> None:
        section = LongformGenerationFactory._word_prompt(self._CONTEXT)["prompt"]
        with tempfile.TemporaryDirectory() as tmp:
            batch = _minimal_factory(Path(tmp))._batch_prompt([self._CONTEXT], batch_id="GB-05")["prompt"]
        for blob, label in ((section, "单节请求"), (batch, "批量请求")):
            for kind in ("企业证书附件页", "资质附件页", "AAA附件页", "ISO证书附件页",
                         "类似项目业绩附件页", "人员履历附件页"):
                self.assertIn(kind, blob, f"{label}提示词必须点名禁止 {kind}")
            self.assertIn("整页空白履历表", blob, f"{label}提示词必须禁止整页空白履历表")
            self.assertIn("【待投标人确认：", blob, f"{label}提示词必须给出唯一的行内提示形状")


if __name__ == "__main__":
    unittest.main(verbosity=2)
