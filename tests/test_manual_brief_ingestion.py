"""Manual form regression: lossless source and scope boundaries, Provider=0."""
import copy
import hashlib
import json

import pytest

from planning.canonical import ensure_requirement_pack, build_canonical_project_brief, build_canonical_tender_analysis
from planning.manual_brief import parse_manual_brief
from planning.planner import _planning_requirements, build_planning_bundle
from planning.content_engineering import classify_technical_scope, is_excluded_technical_scope
from longform.factory import ContextPackBuilder


def pack_from(tmp_path, text):
    brief = {"project_name": "合成园区", "project_type": "产业园区", "scenario": "完整物业服务方案",
             "medium": "PPT", "requirements": text}
    path = ensure_requirement_pack(tmp_path, brief)
    return json.loads(path.read_text(encoding="utf-8")), brief


def test_full_input_and_tail_are_retained_with_exact_spans(tmp_path):
    text = "工程设备巡检。" * 150 + "客服投诉闭环和夜间应急联络。"
    pack, _ = pack_from(tmp_path, text)
    assert pack["manual_input"]["text"] == text
    assert pack["manual_input"]["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert pack["requirements"][-1]["normalized_requirement"] == "客服投诉闭环和夜间应急联络。"
    for row in pack["requirements"]:
        span = row["source_span"]
        assert text[span["start"]:span["end"]] == row["normalized_requirement"]


def test_plain_input_not_collapsed_into_one_poisoned_requirement(tmp_path):
    text = ("制定保洁与工程服务方案。不要写待投标人填写的细碎表单。报价不可编造。没有资料就不编造资质和业绩。\n"
            "[项目简介逐页原文与页码]\n"
            "第1页：1号产业大厦，办公与配套。\n"
            "第2页：2号企业独栋，企业自用。\n"
            "第3页：3号研发中试楼，研发与中试。\n"
            "第4页：4号标准生产厂房，生产车间。\n")
    pack, brief = pack_from(tmp_path, text)
    assert len(pack["writing_constraints"]) == 3
    active = _planning_requirements(pack)
    assert [r["normalized_requirement"] for r in active] == ["制定保洁与工程服务方案。"]
    bundle = build_planning_bundle(pack, brief, {}, production=True)
    assert bundle["validation"]["status"] == "PASS"
    assert bundle["requirement_matrix"]["matrix"]
    assert len(bundle["ppt_plan"]["slide_storyboard"]) >= 30


def test_reference_and_writing_context_reaches_each_medium_without_fact_promotion(tmp_path):
    source = "第1页：开发商信用及资质简介。\n第3页：研发楼8层，规划效果图。\n第6页：标准厂房示意。"
    text = "[用户需求原文]\n制定工程与客户服务方案。报价不可编造。\n[项目简介逐页原文与页码]\n" + source
    pack, brief = pack_from(tmp_path, text)
    assert len(pack["reference_material"]) == 1
    assert pack["reference_material"][0]["text"] == source
    bundle = build_planning_bundle(pack, brief, {}, production=True)
    assert bundle["validation"]["status"] == "PASS"
    state = bundle["global_state"]
    assert state["project_facts"].keys() == {"project_name", "location", "gross_area"}
    assert not state["reference_material"][0]["verified_current_project_fact"]
    before = copy.deepcopy(state)
    builder = ContextPackBuilder(state, bundle["section_contracts"], {}, bundle["dependency_map"])
    slide = bundle["ppt_plan"]["slide_storyboard"][0]
    chapter = bundle["ppt_plan"]["chapter_blocks"][0]
    for context in (builder.ppt(slide, chapter), builder.word(bundle["section_contracts"]["contracts"][0], {})):
        assert context["project_reference_material"][0]["text"] == source
        assert context["writing_constraints"][0]["text"].strip() == "报价不可编造。"
        context["project_reference_material"].clear()
    assert state == before
    canonical = build_canonical_project_brief(pack, brief, build_canonical_tender_analysis(pack, brief))
    assert canonical["reference_material"] == pack["reference_material"]


@pytest.mark.parametrize("constraint", [
    "不铺陈设备编号/记录表/待投标人填写的细碎表单。",
    "不写招标代理机构、无关地址。", "未知人员数量、报价、设备清单不可编造。",
    "没有物业服务商资料就不编造其资质和业绩。", "标题和正文的字体字号统一。",
    "文字左对齐。", "概念图须注明示意。", "优先使用简介里的项目图片。",
    "提出的管理方法须区别于项目既成事实。",
])
def test_writing_instructions_are_not_service_commitments(tmp_path, constraint):
    pack, _ = pack_from(tmp_path, "制定客服服务方案。" + constraint)
    assert len(pack["writing_constraints"]) == 1
    assert [r["normalized_requirement"] for r in _planning_requirements(pack)] == ["制定客服服务方案。"]


@pytest.mark.parametrize("excluded", [
    "须提交营业执照和资质证书。", "报价总价为100万元。", "投标人须提交投标文件。",
    "不得编造资质且必须提供营业执照。", "报价不得超过100万元。",
])
def test_real_pricing_credential_and_procedural_clauses_still_excluded(tmp_path, excluded):
    pack, _ = pack_from(tmp_path, "制定保洁服务方案。" + excluded)
    row = pack["requirements"][-1]
    assert row["normalized_requirement"] == excluded
    assert is_excluded_technical_scope(classify_technical_scope(row)) or "投标人" in excluded
    assert [r["normalized_requirement"] for r in _planning_requirements(pack)] == ["制定保洁服务方案。"]


def test_empty_scope_is_not_fabricated_as_a_service_requirement(tmp_path):
    pack, brief = pack_from(tmp_path, "须提交营业执照。报价为100万元。")
    assert not _planning_requirements(pack)
    assert build_planning_bundle(pack, brief, {}, production=True)["validation"]["checks"]["REQUIREMENT_SECTION_MATRIX"] is False


def test_existing_tender_pack_is_returned_without_migration(tmp_path):
    path = tmp_path / "tender" / "requirement_pack.json"
    path.parent.mkdir()
    original = b'{"status":"frozen","requirements":[]}'
    path.write_bytes(original)
    assert ensure_requirement_pack(tmp_path, {"requirements": "new input"}) == path
    assert path.read_bytes() == original


def test_reference_only_input_does_not_manufacture_a_scope(tmp_path):
    pack, _ = pack_from(tmp_path, "[项目简介]\n研发楼共8层。")
    assert not _planning_requirements(pack)
    assert pack["reference_material"][0]["text"] == "研发楼共8层。"
