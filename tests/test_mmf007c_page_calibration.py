"""MMF-007C Round 3 / P0-1 §49：PAGE-01..03 合成测试（页数口径、预算账本、超页门控）。

Run C 用 900 字/页估出 207 页、真实渲染 404 页，误差 20%；更糟的是那 404 页里
混着封面、目录和整页【待填写】，"404"这个数从来没被拆开看过。本文件钉住四件事：

1. 技术有效页只数真正承载技术内容的页（空白页、封面目录页、整页待填写表都要扣掉）；
2. 商务/报价/资信类页面能被识别成 EXCLUDED_SCOPE，但判据按整页文本标定，
   顺带提到"招标公告"的技术页不得被误扣；
3. 超页是缺陷不是优点：实测技术有效页超过上限要给出**定向削减计划**并禁止继续补深，
   计划里 protected 单元不得动、已生成正文不得被删（只登记）；
4. 没有分页渲染器时 basis 必须写 ESTIMATED，不得把 900 字/页的投影冒充实测。
"""
from __future__ import annotations

import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1] / "app"
for item in (APP, Path(__file__).resolve().parent):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from governance.document_qa import evaluate_document_scale
from longform.orchestrator import evaluate_page_overshoot
from planning.budget_control import (
    BUDGET_LEDGER_FILE,
    REDUCTION_PLAN_FILE,
    apply_rebalanced_targets,
    budget_of,
    judge_content_unit,
    merge_ledger,
    rebalance_pending_targets,
    targeted_reduction_plan,
    write_ledger,
)
from planning.page_calibration import calibrate_ruler, classify_pages, page_report

# 一页真实正文的量级：Run C 实测 938.6 字/技术页，这里取整页散文的样张。
_BODY = ("本单元规定保洁、绿化、秩序维护与设备设施各专业岗位的作业流程、频次与质量核查口径，"
         "并明确责任岗位、记录留痕方式与问题整改闭环时限，供现场班组按同一标准执行与考核。") * 3
# 整页待填写的表单：占位词占近三分之二，且没有承载任何技术口径。
_FILL_PAGE = "附表：项目服务记录表（本页字段由投标人按项目实际情况填写）" + "【待填写】" * 30
# 越界范围页：报价/保证金/证照/开标程序词密集，且一个技术信号词都没有。
_EXCLUDED_PAGE = ("本部分列示投标报价表、最高限价与报价方式说明，另附履约保证金、投标保证金缴纳凭证，"
                  "并提供营业执照、资质证书、信用证书与类似业绩合同复印件，供评标委员会开标时符合性审查。") * 2


def _pages(count: int, *, body: str = _BODY) -> list[str]:
    return [body for _ in range(count)]


def _scale_args(**overrides):
    args = dict(rendered_pages=404, page_target_min=250, page_target_max=320,
                effective_chars=400000, char_target_min=234000, requirement_coverage=1.0,
                scoring_coverage=1.0, cu_completion=1.0, archetype_counts={"SOP": 60, "CHECKLIST": 60},
                table_count=120)
    args.update(overrides)
    return args


@contextmanager
def _run_dir() -> Iterator[Path]:
    """沿用本仓库既有用例的 tempfile 口径：pytest 的 tmp_path 依赖共享 Temp 目录，权限不稳。"""
    with tempfile.TemporaryDirectory() as raw:
        yield Path(raw)


def _audit_fields(run_dir: Path) -> dict[str, Any]:
    """读交付审计字段。app_core 的路径护栏只认应用自己的目录，这里按既有用例口径放行临时目录。"""
    import app_core

    with patch.object(app_core, "_checked", side_effect=lambda path: Path(path)), \
            patch.object(app_core, "_audit_read", return_value=None):
        return app_core._pagination_audit_fields(run_dir)


# ---------------------------------------------------------------- §49 PAGE-01
def test_page_01_blank_and_front_matter_are_not_technical_effective_pages():
    """PAGE-01：空白页与封面/目录页不计入 technical_effective_pages。"""
    pages = ["某某分行物业服务项目 物业服务方案", "目 录"] + _pages(8) + ["", "  \n "]
    report = classify_pages(pages, body_start_page=3)
    assert report["physical_pages"] == 12
    assert report["front_matter_pages"] == 2
    assert report["blank_pages"] == 2
    assert report["technical_effective_pages"] == 8
    # §13：非技术页比例按物理页算，分母含全部页。
    assert report["non_technical_page_ratio"] == round(4 / 12, 4)
    assert report["counts"]["NARRATIVE_TECHNICAL"] == 8


def test_page_01_scale_gate_uses_technical_pages_and_still_reports_physical():
    """同一份规模报告里两个口径必须并列，谁在门控由 page_basis 说明。"""
    pages = ["封面", "目录"] + _pages(330)
    report = classify_pages(pages, body_start_page=3)
    scale = evaluate_document_scale(
        **_scale_args(rendered_pages=report["physical_pages"],
                      technical_effective_pages=report["technical_effective_pages"],
                      page_classes=report["counts"]))
    assert scale["page_basis"] == "TECHNICAL_EFFECTIVE"
    assert scale["physical_rendered_pages"] == 332
    assert scale["technical_effective_pages"] == 330
    dimension = scale["dimensions"]["RENDERED_PAGES"]
    assert dimension["value"] == 330 and dimension["physical_value"] == 332
    assert dimension["met"] is False and scale["page_state"] == "ABOVE_TARGET"
    finding = next(row for row in scale["findings"] if row["rule_id"] == "DOCUMENT_SCALE_ABOVE_TARGET")
    # 实测口径下超页是"要复核并削减"，不是"多写点没坏处"的 Warning。
    assert finding["severity"] == "NEEDS_REVIEW"
    assert finding["reduction_required"] is True


# ---------------------------------------------------------------- §49 PAGE-02
def test_page_02_fill_in_form_pages_are_excluded_from_effective_pages():
    """PAGE-02：整页【待填写】/空表格撑篇幅的页不计技术有效页，并进门控比例。"""
    pages = ["封面", "目录"] + _pages(8) + [_FILL_PAGE, _FILL_PAGE]
    report = classify_pages(pages, body_start_page=3)
    assert report["placeholder_pages"] == 2
    assert report["technical_effective_pages"] == 8
    assert report["placeholder_page_ratio"] == round(2 / 12, 4)

    scale = evaluate_document_scale(
        **_scale_args(technical_effective_pages=report["technical_effective_pages"],
                      rendered_pages=report["physical_pages"],
                      page_classes=report["counts"],
                      placeholder_page_ratio=report["placeholder_page_ratio"]))
    assert any(row["rule_id"] == "PLACEHOLDER_PAGE_RATIO_HIGH" and row["severity"] == "NEEDS_REVIEW"
               for row in scale["findings"])
    # §42：占位页超标只复核，不得把整篇判死。
    assert scale["status"] != "BLOCK"


def test_page_02_layout_features_split_narrative_from_structured_pages():
    """有逐页版面特征时，结构化页与叙述页是看出来的，不是按字数投影出来的。"""
    pages = ["封面"] + _pages(4) + [_FILL_PAGE]
    features = {2: {"table_chars": 700, "list_chars": 0}, 3: {"table_chars": 0, "list_chars": 0}}
    report = classify_pages(pages, body_start_page=2, structure_by_page=features)
    assert report["layout_basis"] == "MEASURED_LAYOUT_FEATURES"
    assert report["structured_technical_pages"] == 1
    assert report["narrative_technical_pages"] == 3
    # 表格里那一片空格：整页过半是表格且填写词密度达标 → 占位页，不是结构化技术页。
    fill_features = {6: {"table_chars": 900, "list_chars": 0}}
    with_features = classify_pages(pages, body_start_page=2, structure_by_page=fill_features)
    assert with_features["counts"]["PLACEHOLDER"] == 1


def test_page_02_technical_page_mentioning_commercial_words_is_not_deducted():
    """反向保护：顺带提到"招标公告/资格证明"的技术页不得被扣成越界页。

    Run C 回放里第 11/36 页就是这么被误判的 —— 把技术页扣掉对报告自己"好看"，
    但数字是错的，所以这条与 PAGE-02 的正向判据同等重要。
    """
    mixed = ("本单元依据招标公告与技术需求书已确认的服务范围，形成项目概况、服务范围与排除项清单，"
             "明确各岗位责任矩阵、巡检频次、台账记录与响应时限口径，并在部位变更时增补复核。") * 3
    report = classify_pages(["封面", "目录", mixed], body_start_page=3)
    assert report["excluded_scope_pages"] == 0
    assert report["technical_effective_pages"] == 1


def test_page_02_excluded_scope_pages_are_flagged_for_review():
    """商务/报价/资信页进入正文时要给出 EXCLUDED_SCOPE 复核项（§0/§55）。"""
    pages = ["封面", "目录"] + _pages(6) + [_EXCLUDED_PAGE]
    report = classify_pages(pages, body_start_page=3)
    assert report["excluded_scope_pages"] == 1
    assert report["technical_effective_pages"] == 6
    scale = evaluate_document_scale(
        **_scale_args(technical_effective_pages=report["technical_effective_pages"],
                      rendered_pages=report["physical_pages"],
                      page_classes=report["counts"]))
    assert any(row["rule_id"] == "EXCLUDED_SCOPE_PAGE_IN_TECHNICAL_BODY" for row in scale["findings"])


# ---------------------------------------------------------------- §49 PAGE-03
def test_page_03_replay_shaped_overshoot_triggers_reduction_and_blocks_supplement():
    """PAGE-03：404 页那种体量必须当轮识别为 Overshoot，而不是跑完才发现。"""
    draft_pages = {"rendered_pages": 404, "technical_effective_pages": 402,
                   "page_basis": "MEASURED",
                   "page_ruler": {"basis": "MEASURED", "chars_per_technical_page": 938.6,
                                  "technical_effective_pages": 402}}
    contracts = {
        "CU-A": {"content_unit_id": "CU-A", "parent_section_id": "SEC-1", "archetype": "NARRATIVE",
                 "planned_words": 1200},
        "CU-B": {"content_unit_id": "CU-B", "parent_section_id": "SEC-2", "archetype": "SOP",
                 "planned_words": 1200, "source_scoring_items": ["S-01"]},
    }
    with _run_dir() as run_dir:
        gate = evaluate_page_overshoot(run_dir, draft_pages, page_targets=[250, 320], contracts=contracts)
        assert gate["status"] == "OVERSHOOT_REDUCTION"
        assert gate["depth_supplement_forbidden"] is True
        assert gate["measured_technical_effective_pages"] == 402
        assert gate["page_ceiling"] == 320
        plan = json.loads((run_dir / REDUCTION_PLAN_FILE).read_text(encoding="utf-8"))
    assert plan["trigger"] and plan["over_pages"] == 82
    assert plan["projection_basis"] == "MEASURED_RENDER"
    # §22：不是黑盒压缩器 —— 不删任何已生成单元，保护类只登记不动。
    assert plan["deleted_content_units"] == []
    assert plan["policy"] == "NEVER_DELETE_SCORING_RESPONSE_MUST_REQUIREMENT_SOP_TECHNICAL_TABLE_KEY_PROCESS"
    assert all(row["content_unit_id"] != "CU-B" for row in plan["actions"])
    assert plan["protected_units_skipped"] >= 1


def test_page_03_within_targets_leaves_supplement_open():
    draft_pages = {"rendered_pages": 300, "technical_effective_pages": 298,
                   "page_basis": "MEASURED",
                   "page_ruler": {"basis": "MEASURED", "chars_per_technical_page": 938.6}}
    with _run_dir() as run_dir:
        gate = evaluate_page_overshoot(run_dir, draft_pages, page_targets=[250, 320], contracts={})
    assert gate["status"] == "WITHIN_PAGE_TARGETS"
    assert gate["depth_supplement_forbidden"] is False
    assert gate["at_ceiling_margin"] is False

    # 贴到天花板（318 > 320×0.95）时必须标出余量不足，不得当成"还有空间"继续补深。
    with _run_dir() as run_dir:
        tight_gate = evaluate_page_overshoot(
            run_dir, {**draft_pages, "technical_effective_pages": 318, "rendered_pages": 320},
            page_targets=[250, 320],
            contracts={})
    assert tight_gate["status"] == "WITHIN_PAGE_TARGETS"
    assert tight_gate["at_ceiling_margin"] is True


def test_page_03_without_measured_pages_the_gate_says_so_instead_of_guessing():
    with _run_dir() as run_dir:
        gate = evaluate_page_overshoot(run_dir, {"rendered_pages": 404, "technical_effective_pages": 0},
                                       page_targets=[250, 320], contracts={})
    assert gate["status"] == "NOT_EVALUATED"
    assert gate["reason"] == "NO_MEASURED_TECHNICAL_PAGES"
    assert gate["depth_supplement_forbidden"] is False


def test_page_03_reduction_plan_protects_key_content_and_admits_shortfall():
    """§22：削减计划不得动保护类，也不得为了凑数虚构可减字数或冒充页数口径。"""
    ruler = {"basis": "ESTIMATED", "chars_per_technical_page": 900}
    pending = [{"content_unit_id": f"CU-P{index}", "section_id": "SEC-1", "archetype": "NARRATIVE",
                "planned_words": 4000} for index in range(12)]
    pending.append({"content_unit_id": "CU-P-SOP", "section_id": "SEC-2", "archetype": "SOP",
                    "planned_words": 4000, "source_requirements": ["R-MUST-01"]})
    arrived = [
        judge_content_unit({"content_unit_id": "CU-W1", "parent_section_id": "SEC-1",
                            "archetype": "NARRATIVE", "planned_words": 4000}, chars=9000),
        judge_content_unit({"content_unit_id": "CU-W2", "parent_section_id": "SEC-2",
                            "archetype": "SOP", "planned_words": 4000,
                            "source_scoring_items": ["S-01"]}, chars=9000),
    ]
    ledger = merge_ledger({"records": []}, arrived)

    # 只有 18k 字（≈20 页）时不得凭空造出削减计划：口径写 ESTIMATED，trigger 留空。
    small = targeted_reduction_plan(ledger=ledger, content_units=pending, page_targets=(250, 320),
                                    ruler=ruler, measured_technical_pages=0)
    assert small["projection_basis"] == "ESTIMATED"
    assert small["over_pages"] == 0 and small["trigger"] is None
    assert small["status"] == "NOT_REQUIRED"

    plan = targeted_reduction_plan(ledger=ledger, content_units=pending, page_targets=(250, 320),
                                   ruler=ruler, measured_technical_pages=330)
    assert plan["projection_basis"] == "MEASURED_RENDER"
    assert plan["over_pages"] == 10 and plan["excess_chars"] == 9000
    assert plan["status"] == "TARGETED_REDUCTION_PLANNED"
    assert plan["deleted_content_units"] == []
    selected = {row["content_unit_id"] for row in plan["actions"]}
    assert "CU-W2" not in selected and "CU-P-SOP" not in selected
    assert all(row["protected"] is False for row in plan["actions"])
    assert plan["protected_units_skipped"] == 2

    # 缺口全落在保护类上时：如实报 REDUCTION_INSUFFICIENT，不得伪装成已经解决。
    tight = targeted_reduction_plan(ledger=merge_ledger({"records": []}, [arrived[1]]),
                                    content_units=[pending[-1]], page_targets=(250, 320),
                                    ruler=ruler, measured_technical_pages=400)
    assert tight["status"] == "REDUCTION_INSUFFICIENT"
    assert tight["actions"] == [] and tight["uncovered_chars"] > 0
    assert tight["deleted_content_units"] == []


# ---------------------------------------------------------------- §19 记账不删字
def test_budget_ledger_records_overshoot_without_touching_the_text():
    """§19：Provider 返回即记账；超上限只登记 OVER_BUDGET，不当场删字。"""
    contract = {"content_unit_id": "CU-1", "parent_section_id": "SEC-1", "archetype": "NARRATIVE",
                "target_budget": {"measure": "effective_chars", "effective_chars": 1000},
                "min_budget": {"measure": "effective_chars", "effective_chars": 800},
                "max_budget": {"measure": "effective_chars", "effective_chars": 1200}}
    row = {"content_unit_id": "CU-1", "content": "字" * 1800, "payload": {}}
    judged = judge_content_unit(contract, content=row)
    assert judged["budget_status"] == "OVER_BUDGET"
    assert judged["actual_chars"] == 1800
    assert judged["overshoot_chars"] == 600
    assert len(row["content"]) == 1800  # 一个字都没被删

    with _run_dir() as run_dir:
        ledger = merge_ledger({"records": []}, [judged])
        write_ledger(run_dir / BUDGET_LEDGER_FILE, ledger)
        assert ledger["totals"]["over_budget_units"] == 1
        assert ledger["totals"]["overshoot_chars"] == 600
        # 同一单元重生成后以最新一次为准，历史次数保留。
        again = merge_ledger(ledger, [{**judged, "actual_chars": 900, "budget_status": "WITHIN_BUDGET",
                                      "overshoot_chars": 0}])
    assert again["totals"]["content_units_recorded"] == 1
    assert again["records"][0]["attempt_count"] == 2
    assert again["totals"]["overshoot_chars"] == 0


def test_persisted_content_units_are_merged_into_the_run_ledger():
    """§19 落盘面：Section 持久化后必须能在运行目录读到预算账本（超写只登记，不改正文）。

    Round 4 §14 之后，落账与"生成中预算账"是同一条生产路径，所以这里用真实工厂实例
    而不是把方法绑到裸 stub 上 —— stub 会跳过记账后半段，测到的是一个不存在的形状。
    """
    import test_mmf006e_r21_q01_q04 as r21

    with _run_dir() as run_dir:
        cus_dir = run_dir / "longform" / "word" / "sections" / "S01-01" / "cus"
        cus_dir.mkdir(parents=True)
        record = judge_content_unit({"content_unit_id": "CU-S01-01-01", "parent_section_id": "S01-01",
                                     "archetype": "NARRATIVE", "planned_words": 1000}, chars=1700)
        (cus_dir / "CU-S01-01-01.json").write_text(
            json.dumps({"content_unit_id": "CU-S01-01-01", "budget": record}, ensure_ascii=False),
            encoding="utf-8")
        r21._minimal_factory(run_dir)._record_content_unit_budget("S01-01")
        ledger = json.loads((run_dir / BUDGET_LEDGER_FILE).read_text(encoding="utf-8"))

    assert ledger["records"][0]["content_unit_id"] == "CU-S01-01-01"
    assert ledger["records"][0]["budget_status"] == "OVER_BUDGET"
    assert ledger["totals"]["overshoot_chars"] == 500


# ---------------------------------------------------------------- §20 动态再平衡
def test_rebalancing_shrinks_pending_targets_and_never_touches_written_text():
    """§20：再平衡只摊给未生成单元；压到字数下限时残余超写必须如实报出来。"""
    units = [{"content_unit_id": f"CU-{index}", "section_id": "SEC-1", "archetype": "NARRATIVE",
              "planned_words": 1200} for index in range(10)]
    written = [dict(judge_content_unit(unit, chars=1980)) for unit in units[:4]]  # 实测超写 1.65 倍
    ledger = merge_ledger({"records": []}, written)
    rebalance = rebalance_pending_targets(units, ledger, budget_chars_target=12000)

    assert rebalance["observed_inflation_factor"] == 1.65
    assert rebalance["pending_units"] == 6
    assert rebalance["written_units_untouched"] == 4
    assert rebalance["shrink_factor"] < 1.0
    assert all(row["target_after"] < row["target_before"] for row in rebalance["adjustments"])
    # 单元还有 min_budget 下限：压到下限之后仍然超预算时，缺口如实登记，
    # 只能靠"减少单元数（定向削减）"补，不得假装已经摊回目标。
    assert rebalance["floor_limited_units"] == 6
    assert rebalance["pending_target_chars_after"] == sum(budget_of(unit)["min"] for unit in units[4:])
    assert rebalance["residual_overrun_chars"] > 0

    applied = apply_rebalanced_targets({unit["content_unit_id"]: unit for unit in units}, rebalance)
    assert applied["applied_units"] == 6
    assert applied["contracts_available"] == 10
    assert units[0]["planned_words"] == 1200          # 已生成单元不改契约
    assert "budget_rebalanced" not in units[0]
    assert units[9]["planned_words"] == budget_of(units[9])["target"] == 960
    assert units[9]["target_budget"]["effective_chars"] == 960
    assert units[9]["budget_rebalanced"]["target_before"] == 1200


def test_rebalancing_does_not_raise_targets_when_the_budget_is_already_spent():
    """反向用例：剩余预算为 0 时只能把未生成单元压到下限，不得"再平衡"出更多字。"""
    units = [{"content_unit_id": f"CU-{index}", "section_id": "SEC-1", "planned_words": 1200}
             for index in range(4)]
    ledger = merge_ledger({"records": []}, [dict(judge_content_unit(unit, chars=6000)) for unit in units[:2]])
    rebalance = rebalance_pending_targets(units, ledger, budget_chars_target=6000)
    assert rebalance["remaining_budget_chars"] == 0
    assert rebalance["shrink_factor"] == 0.0
    assert all(row["target_after"] <= row["target_before"] for row in rebalance["adjustments"])
    assert rebalance["pending_target_chars_after"] == sum(budget_of(unit)["min"] for unit in units[2:])


# ---------------------------------------------------------------- §15/§16 尺子与六类页数
def test_ruler_is_calibrated_from_real_pages_and_reports_its_basis():
    pages = ["封面", "目录"] + _pages(10)
    report = classify_pages(pages, body_start_page=3)
    ruler = calibrate_ruler(report, visible_chars=report["visible_chars"],
                            technical_chars=report["technical_chars"])
    assert ruler["basis"] == "MEASURED"
    assert ruler["chars_per_technical_page"] == round(report["technical_chars"] / 10, 1)
    empty = calibrate_ruler({"technical_effective_pages": 0}, visible_chars=0)
    assert empty["basis"] == "ESTIMATED"
    assert empty["chars_per_technical_page"] == 900


def test_page_report_states_all_six_categories_and_never_passes_estimate_as_measurement():
    pages = ["封面", "目录"] + _pages(6) + [_FILL_PAGE, _EXCLUDED_PAGE]
    report = classify_pages(pages, body_start_page=3)
    stored = {
        "schema_version": "first-draft-page-classification-v0.1",
        "physical_pages": report["physical_pages"],
        "page_classes": report["counts"],
        "technical_effective_pages": report["technical_effective_pages"],
        "narrative_technical_pages": report["narrative_technical_pages"],
        "structured_technical_pages": report["structured_technical_pages"],
        "visible_chars": report["visible_chars"],
        "technical_chars": report["technical_chars"],
        "placeholder_page_ratio": report["placeholder_page_ratio"],
        "non_technical_page_ratio": report["non_technical_page_ratio"],
    }
    doc = page_report(stored, effective_chars=report["technical_chars"])
    assert doc["measured"] is True and doc["basis"] == "MEASURED"
    for key in ("physical_pages", "technical_effective_pages", "narrative_technical_pages",
                "structured_technical_pages", "placeholder_pages", "front_matter_pages",
                "blank_pages", "excluded_scope_pages"):
        assert key in doc
    assert doc["physical_pages"] == 10 and doc["technical_effective_pages"] == 6
    assert doc["placeholder_pages"] == 1 and doc["excluded_scope_pages"] == 1
    # 没有逐页版面特征时，叙述/结构化切分要如实写明是字符归属，不是逐页观测。
    assert doc["page_split_basis"] == "CHAR_ATTRIBUTION"
    unmeasured = page_report({}, effective_chars=90000)
    assert unmeasured["measured"] is False and unmeasured["basis"] == "ESTIMATED"
    assert unmeasured["physical_pages"] == 0


# ---------------------------------------------------------------- §13 交付件口径
def test_delivered_artifact_pagination_reports_the_six_page_categories():
    """交付审计里"404 页"必须被拆成六类页数，量不到时如实留 NOT_MEASURED。"""
    from longform.pagination import PAGINATION_REPORT, _technical_page_report

    pages = ["封面", "目录"] + _pages(6) + [_FILL_PAGE, _EXCLUDED_PAGE]
    measured = _technical_page_report(pages, [("第一章 项目理解", 2)])
    assert measured["page_classification_status"] == "MEASURED"
    assert measured["physical_pages"] == 10
    assert measured["technical_effective_pages"] == 6
    assert measured["placeholder_pages"] == 1 and measured["excluded_scope_pages"] == 1
    assert measured["front_matter_pages"] == 2 and measured["blank_pages"] == 0
    assert measured["page_basis"] == "MEASURED"
    assert measured["chars_per_technical_page"] > 0
    # 没有逐页版面特征时，文本口径的技术有效页是**上界**（看不见整页待填写表格）。
    assert measured["layout_basis"] == "NO_LAYOUT_FEATURES"
    assert measured["technical_page_count_bound"] == "UPPER_BOUND_TEXT_ONLY"

    with _run_dir() as run_dir:
        (run_dir / PAGINATION_REPORT).write_text(json.dumps({
            "schema_version": "artifact-pagination-v0.1", "status": "REFRESHED", "renderer": "soffice",
            "rendered_pages": 10, "visible_chars": 3000, "page_char_counts": [100] * 10,
            "thin_page_count": 0, "effective_content_pages": 10, "meaningful_page_ratio": 1.0,
            "toc_entry_count": 3, "toc_pages_resolved": 3, "toc_pages_refreshed": 3,
            "toc_pages_verified": 3, "toc_pages_unresolved": 0, "toc_body_start_page": 3,
            "displayed_pages": ["3", "4", "5"], "identical_page_share": 0.0,
            **measured,
        }, ensure_ascii=False), encoding="utf-8")
        fields = _audit_fields(run_dir)
    assert fields["PHYSICAL_PAGES"] == 10
    assert fields["TECHNICAL_EFFECTIVE_PAGES"] == 6
    assert fields["NARRATIVE_TECHNICAL_PAGES"] == 6
    assert fields["PLACEHOLDER_PAGES"] == 1 and fields["EXCLUDED_SCOPE_PAGES"] == 1
    assert fields["PLACEHOLDER_PAGE_RATIO"] == round(1 / 10, 4)
    assert fields["PAGE_BASIS"] == "MEASURED"
    assert fields["TECHNICAL_PAGE_COUNT_BOUND"] == "UPPER_BOUND_TEXT_ONLY"
    assert fields["PAGINATION_STATUS"] == "REFRESHED"

    with _run_dir() as bare:
        (bare / PAGINATION_REPORT).write_text(json.dumps({
            "schema_version": "artifact-pagination-v0.1", "status": "SKIPPED_NO_RENDERER",
            "renderer": None, "rendered_pages": 404,
        }, ensure_ascii=False), encoding="utf-8")
        skipped = _audit_fields(bare)
    # 没有分页器就没有技术有效页可报：留 None，不得折算成 0 冒充"没超页"。
    assert skipped["TECHNICAL_EFFECTIVE_PAGES"] is None
    assert skipped["PAGE_CLASSIFICATION_STATUS"] == "NOT_MEASURED"
    assert skipped["TECHNICAL_PAGE_COUNT_BOUND"] == "UNKNOWN"
    assert skipped["PHYSICAL_PAGES"] == 404
