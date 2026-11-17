"""MMF-007C Round 3 / P1 §45–§47：Generic / Padding 的度量、削减顺序与"不靠扩写冲页数"。

Run C 404 页真实交付件把一件事暴露得很清楚：§47 的反注水门四项重复指标全为 0
（`r3_p1_generic_padding_runC.json`），但抽样里仍有成片的"任何项目都能照抄"的句子 ——
一句话都不重复，重复检测天然看不见。本文件钉住：

1. §45 低增量按**两个正交轴**判：无可核查载体 **且** 无技术主题词才算注水，
   套话密度只负责把 HIGH_VALUE 降为 USABLE，不参与判注水（否则硬技术句会被误伤）；
2. §45 的比例过高只到 NEEDS_REVIEW，不得把整篇判死（§38 的口径），也不得改 §47 那四项；
3. §22/§45 超页削减按"信息增量最低"排序，不是按字数最大排序，保护类照旧不动；
4. §46 没量到正文与量到但干净是两件事，账本必须分得开。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
for item in (APP, Path(__file__).resolve().parent):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from planning.budget_control import judge_content_unit, merge_ledger, targeted_reduction_plan
from planning.content_engineering import content_quality_report
from planning.content_padding import (GENERIC_PADDING, HIGH_VALUE, PADDING_ITEM_SHARE_REVIEW,
                                      PADDING_LEXICON_VERSION, USABLE, classify_text,
                                      content_padding_report, measure_payload)

# ── 真句子：可核查载体在词里，不在形容词里 ──────────────────────────────────────
CONCRETE = "秩序维护领班每两小时巡查一次地下车库与电梯机房，异常当场记入巡更台账并拍照留痕。"
CONCRETE_TWO = "客户接口人在交接班前清点当日发放的临时证件，未核销的逐张写明原因与责任岗位。"
# 一句都不重复，但换个项目照样能抄 —— 这就是 §45 要抓、§47 抓不到的那一类。
GENERIC = ("我们将持续加强管理，不断完善相关制度，努力提高服务水平，确保各项要求得到切实落实。")
GENERIC_TWO = "相关人员会积极配合有关部门做好相应的沟通与协调工作，确保事项得到妥善处置。"
GENERIC_THREE = "相关事项按照有关规定执行，必要时视情况适当调整，以保证整体效果的稳定。"
FACT_ONLY = "项目名称：中国进出口银行山东省分行物业服务项目，建筑面积以招标文件载明的总建筑面积为准。"
BID_PROCESS = "投标保证金、资格证明与报价表的响应口径，本章不作展开，由商务文件承载并另行递交。"
HEDGEY_BUT_HARD = "配电室与水泵房每两小时巡检一次，责任人须签字确认，确保记录连续可追溯。"


def _artifact(paragraphs: list[str], bullets: list[str] | None = None) -> dict:
    return {"title": "物业服务方案",
            "sections": [{"section_id": "CH01", "heading": "秩序维护专项方案",
                          "level": 1, "paragraphs": list(paragraphs),
                          "bullets": list(bullets or []), "tables": []}]}


# ------------------------------------------------------------------ §45 单条判据
def test_p1_45_repetition_free_boilerplate_is_still_measured() -> None:
    """§45：不重复不等于有信息。§47 四项全 0 的文本，这一维必须还能量出注水。"""
    artifact = _artifact([GENERIC, GENERIC_TWO, GENERIC_THREE])
    assert content_quality_report(artifact)["duplicate_paragraph_count"] == 0
    assert content_quality_report(artifact)["semantic_near_duplicate_count"] == 0
    gate = content_padding_report(artifact, "物业服务项目")
    assert gate["items"] == 3
    assert gate["items_by_bucket"][GENERIC_PADDING] == 3
    assert gate["low_gain_chars"] == sum(len(text) for text in (GENERIC, GENERIC_TWO, GENERIC_THREE))
    assert gate["status"] == "NEEDS_REVIEW"


def test_p1_45_two_axes_are_both_required() -> None:
    """§45：载体为 0 但有技术主题词 → 只是 USABLE；两轴同时为 0 才算注水。"""
    assert classify_text(CONCRETE)["bucket"] == HIGH_VALUE
    padded_but_technical = classify_text("会务与进退场的衔接工作将认真对待，积极配合有关部门做好沟通协调。")
    assert padded_but_technical["anchors"] == []          # A 轴为空
    assert padded_but_technical["technical_term_hits"] > 0   # B 轴有
    assert padded_but_technical["bucket"] == USABLE
    assert classify_text(GENERIC)["bucket"] == GENERIC_PADDING
    assert classify_text(GENERIC)["anchors"] == []
    assert classify_text(GENERIC)["technical_term_hits"] == 0


def test_p1_45_accountability_wording_is_not_called_padding() -> None:
    """§45 已量到的判据边界：只写"责任岗位"而没有具体岗位名的条目，A 轴按 role 记账。

    Run C 只读复算有 32 条这样的正文挂在"删掉『岗位』就会被判注水"那条线上
    （`r3_p1_generic_padding_probe.py` 的 `lexicon_tightening_test`，探针实测后否决了收紧）。
    这条断言钉住"不再往那个方向收紧"，同时留下可反查的样本口径。
    """
    for text in ("未关闭事项继续挂账，写明未完成原因、责任岗位与调整后的完成条件。",
                 "异常处置后由非处置岗位复查，复查结论注明现场实测值。",
                 "数字出处核对：正文出现的每一处数值可指向合同、技术需求书或甲方书面答复原文。"):
        row = classify_text(text)
        assert row["bucket"] != GENERIC_PADDING, text
        assert row["anchors"], text


def test_p1_45_hedge_density_only_demotes_never_pads() -> None:
    """§45：套话密度是降级器。硬句子里出现"确保"不得被判成注水。"""
    row = classify_text(HEDGEY_BUT_HARD)
    assert row["hedge_per_1000"] > 0
    assert row["bucket"] in {HIGH_VALUE, USABLE}
    assert row["bucket"] != GENERIC_PADDING


def test_p1_45_chinese_numerals_and_cadence_words_are_checkable() -> None:
    """§45：中文数词与"按X/逐X"频次写法同样是可核查量，漏了就会把硬条目误判成注水。"""
    for text in ("秩序维护员指派一人专职疏导并恢复双线分离，异常报项目主管。",
                 "保洁作业按月统计重复部位与整改闭环率，未达标项转入下期交底。",
                 "复训定课只针对偏差事项设课，逐条写明正确做法与判定标准。"):
        assert classify_text(text)["anchors"], text
        assert classify_text(text)["bucket"] != GENERIC_PADDING, text


def test_p1_45_canonical_fact_restatement_is_low_gain() -> None:
    """§45：整句一半以上在复述同一批 Canonical Fact，就是没有信息增量。"""
    row = classify_text(FACT_ONLY)
    assert row["bucket"] == GENERIC_PADDING
    assert "CANONICAL_FACT_RESTATEMENT" in row["reasons"]
    assert row["fact_char_share"] >= 0.5


def test_p1_45_bid_process_boilerplate_is_named_separately() -> None:
    """§45：投标流程话术与技术注水分开记 —— 它同时是 §28–§30 的范围问题，不能混成一个数。"""
    row = classify_text(BID_PROCESS)
    assert row["bucket"] == GENERIC_PADDING
    assert "BID_PROCESS_BOILERPLATE" in row["reasons"]
    assert row["excluded_scope_hit_total"] > 0


# ------------------------------------------------------------------ §45 门与报告
def test_p1_45_ceiling_is_needs_review_and_never_blocks_the_document() -> None:
    """§45/§38：比例过高要复核，但不得因为一把度量尺子把整篇判死。"""
    gate = content_padding_report(_artifact([GENERIC] * 6 + [CONCRETE, CONCRETE_TWO]), "物业服务项目")
    assert gate["status"] == "NEEDS_REVIEW"
    assert gate["item_share"][GENERIC_PADDING] > PADDING_ITEM_SHARE_REVIEW
    assert not any(token in str(gate["status"]) for token in ("BLOCK", "FAIL", "TECHNICAL_FAILED"))
    clean = content_padding_report(_artifact([CONCRETE, CONCRETE_TWO]), "物业服务项目")
    assert clean["status"] == "PASS"
    assert clean["items_by_bucket"][GENERIC_PADDING] == 0


def test_p1_45_report_locates_without_copying_customer_text() -> None:
    """§45：报告给定位路径与占比，不把客户原文抄进运行报告；节级候选按低增量字数排序。"""
    artifact = _artifact([CONCRETE, GENERIC, GENERIC_TWO, GENERIC_THREE, FACT_ONLY,
                          CONCRETE_TWO, BID_PROCESS, HEDGEY_BUT_HARD])
    gate = content_padding_report(artifact, "物业服务项目")
    serialized = json.dumps(gate, ensure_ascii=False)
    assert "text_head" not in serialized
    assert "artifact.sections[0].paragraphs[1]" in serialized   # 路径足够翻到那一条
    assert all("customer_text" not in row and "text" not in row for row in gate["worst_surfaces"])
    assert gate["worst_surfaces"], "没有节级候选，削减就无从下手"
    shares = [row["low_gain_chars"] for row in gate["worst_surfaces"]]
    assert shares == sorted(shares, reverse=True)
    assert all({"path", "items", "low_gain_items", "low_gain_chars"} <= set(row)
               for row in gate["worst_surfaces"])


def test_p1_45_surfaces_are_reported_apart() -> None:
    """§45：正文与条目分开量 —— Run C 的注水在 bullet 面，混在一个总数里就看不出该改哪。"""
    gate = content_padding_report(_artifact([CONCRETE, GENERIC], bullets=[GENERIC_TWO, CONCRETE_TWO]),
                                  "物业服务项目")
    assert gate["narrative"]["items"] == 2
    assert gate["structured"]["items"] == 2
    assert gate["narrative"]["items_by_bucket"][GENERIC_PADDING] == 1
    assert gate["structured"]["items_by_bucket"][GENERIC_PADDING] == 1


def test_p1_47_anti_padding_gate_signals_are_untouched() -> None:
    """§47：Semantic Novelty / Fact Repetition / Paragraph Similarity 继续保留，
    新增这一维不得顶替或削弱原有四项。"""
    repeated = _artifact([CONCRETE] * 4)
    gate = content_quality_report(repeated, "物业服务项目")
    for key in ("duplicate_paragraph_count", "semantic_near_duplicate_count",
                "structured_low_gain_repetition_count", "canonical_fact_low_gain_repetition_count",
                "template_leakage_count", "broken_serialization_count", "semantic_novelty_policy"):
        assert key in gate, key
    # §47 的原有口径照旧：小篇允许 2 条重复，超过 max(2, 正文数//20) 就 DEPTH_GATE_FAIL。
    assert gate["duplicate_paragraph_count"] == 3
    assert gate["status"] == "DEPTH_GATE_FAIL"
    # 同一条重复正文，§45 只报低增量占比，不去抢 §47 的 FAIL 判定。
    padding = content_padding_report(repeated, "物业服务项目")
    assert padding["status"] == "PASS"
    assert padding["items_by_bucket"][GENERIC_PADDING] == 0


def test_p1_45_short_headings_are_not_counted_as_sentences() -> None:
    """§45：短于一条正文判据的标题、列名不参与占比，否则分母会被"责任岗位"这类词撑爆。"""
    assert measure_payload({"paragraphs": ["电梯", "台账"], "tables": []})["items"] == 0
    assert measure_payload({"paragraphs": [CONCRETE]})["items"] == 1


# ------------------------------------------------------- §22/§45 削减顺序（产品侧）
def _contract(cu_id: str, *, archetype: str = "NARRATIVE", **extra) -> dict:
    return {"content_unit_id": cu_id, "parent_section_id": "SEC-1", "archetype": archetype,
            "planned_words": 900, **extra}


def test_p1_46_reduction_prefers_lowest_information_increment() -> None:
    """§22/§45：超页时先删"写出来但没信息"的，再删字数超得多的。"""
    generic_unit = judge_content_unit(_contract("CU-GENERIC"), content={"content": GENERIC * 12})
    concrete_unit = judge_content_unit(_contract("CU-CONCRETE"), content={"content": CONCRETE * 12})
    assert generic_unit["padding_item_share"] > concrete_unit["padding_item_share"]
    ledger = merge_ledger(None, [
        # 故意把"字多但有效"的那条排在前面：只看字数它会先被削。
        {**concrete_unit, "overshoot_chars": 9000},
        {**generic_unit, "overshoot_chars": 1200},
    ])
    plan = targeted_reduction_plan(ledger=ledger, content_units=[], page_targets=(250, 320),
                                   ruler={"chars_per_technical_page": 938.6},
                                   measured_technical_pages=404)
    assert plan["reduction_order_basis"] == "LOWEST_INFORMATION_INCREMENT_FIRST_THEN_LARGEST_OVERSHOOT"
    assert [row["content_unit_id"] for row in plan["actions"]][:1] == ["CU-GENERIC"]
    assert plan["low_gain_candidates"] >= 1
    assert plan["actions"][0]["padding_item_share"] > 0


def test_p1_46_protected_units_stay_out_even_when_pure_boilerplate() -> None:
    """§22 优先于 §45：评分应答与 SOP 即使话很通用也不进削减候选，本轮不静默删正文。"""
    unit = judge_content_unit(_contract("CU-SOP", archetype="SOP", source_scoring_items=["S-01"]),
                              content={"content": GENERIC * 10})
    assert unit["protected"] is True
    ledger = merge_ledger(None, [{**unit, "overshoot_chars": 5000}])
    plan = targeted_reduction_plan(ledger=ledger, content_units=[], page_targets=(250, 320),
                                   ruler={"chars_per_technical_page": 938.6},
                                   measured_technical_pages=404)
    assert plan["actions"] == []
    assert plan["protected_units_skipped"] == 1
    assert plan["deleted_content_units"] == []


def test_p1_46_ledger_separates_not_measured_from_clean() -> None:
    """§46：没有正文可量时记 items=0，不得被读成"量过了，没有注水"。"""
    unmeasured = judge_content_unit(_contract("CU-PENDING"))
    assert unmeasured["padding_measured_items"] == 0
    assert unmeasured["padding_item_share"] == 0.0
    clean = judge_content_unit(_contract("CU-OK"), content={"content": CONCRETE})
    assert clean["padding_measured_items"] == 1
    assert clean["low_gain_items"] == 0
    totals = merge_ledger(None, [unmeasured, clean])["totals"]
    assert totals["padding_measured_items"] == 1
    assert totals["low_gain_chars"] == 0


def test_p1_46_supplement_shape_names_a_carrier_not_a_paragraph() -> None:
    """§46：补页的载体词表与削减/复核用的是同一套，"再写一段话"不在候选里。"""
    from longform.capability_gaps import _archetype_gap_shapes
    from planning.content_padding import STRUCTURED_CARRIER_PATTERNS

    assert set(STRUCTURED_CARRIER_PATTERNS) == {
        "SOP_PROCESS", "CHECKLIST", "RISK_REGISTER", "KPI", "RESPONSIBILITY_MATRIX",
        "STANDARD", "FORM", "SCHEDULE"}
    assert set(_archetype_gap_shapes(["NARRATIVE"])) == {"FORM_TEMPLATE", "CHECKLIST", "MATRIX", "SOP"}
    assert _archetype_gap_shapes(["SOP", "PROCESS", "CHECKLIST", "FORM_TEMPLATE", "KPI_TABLE"]) == []


def test_p1_45_device_phenomena_and_record_fields_are_substance() -> None:
    """§45：全量定标（第四轮）量出的两类现场名词必须算实质。

    `r3_p1_padding_sample_audit4_census_runC.json`：v0.8 在 55 条命中上的注水命中率只有 75 %，
    假阳性集中在"设备运行观察现象"与"服务与交付记录字段"两类，词表里没有这些名词。
    这条断言钉住补进来的那两类，免得下次精简词表时又被剪掉。
    """
    for text in (
            "识别范围覆盖温升、异响振动、渗漏与参数漂移，异常按同一测点复测并设观察期。",
            "回访人、反馈时间与提出人核验意见记入回访记录，未闭环的保持挂账。",
            "观感面按动线顺序检查，深处与背向面不得被门口打转掩盖。",
            "成果目录与实际报送件按份数、密级与编号一一对应，缺件不得签发。",
            "需法定检验处置的事项，同时向甲方书面提示，不替代甲方决策。"):
        row = classify_text(text)
        assert row["bucket"] != GENERIC_PADDING, text
        assert "object" in row["anchors"], text


def test_p1_45_widening_never_rescues_document_selfcheck() -> None:
    """§45：反向门 —— 取证动词配上"文件本身"这个对象时仍然算注水。

    词表越补越大，风险是把"表述一致性校核"这类文件自查一起放掉。
    全量定标里 40 条判为注水的条目没有一条被 v0.8/v0.9 的放宽放过（FN = 0），
    这几句是那条结论的可执行版本。
    """
    for text in (
            "表述一致性校核：本卷回引句式与专章内容逐句比对后登记检查人。",
            "正文中涉及品牌与数量的写法统一为一种表述，不重复列举。",
            "全册机构名称的写法与招标文件载明内容保持一致，不出现简称或旧称。"):
        row = classify_text(text)
        assert row["bucket"] == GENERIC_PADDING, text
        assert "DOCUMENT_SELFCHECK" in row["reasons"], text


def test_p1_45_report_carries_the_lexicon_version() -> None:
    """§45：报告要写明这把尺子是第几版词表 —— 占比不能跨版本对照。"""
    report = content_padding_report(_artifact([CONCRETE, GENERIC]), "large_technical_tender")
    assert report["lexicon_version"] == PADDING_LEXICON_VERSION
    assert report["schema_version"] != report["lexicon_version"]
