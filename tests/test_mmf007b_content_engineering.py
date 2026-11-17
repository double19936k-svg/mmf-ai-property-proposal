from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
APP = APP_ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from governance.longform_qa import evaluate_longform_depth
from governance.toc_qa import evaluate_native_toc
from planning.content_engineering import (
    CONTENT_ARCHETYPES,
    build_bidder_evidence_ledger,
    build_commercial_compliance_matrix,
    build_requirement_classification,
    content_quality_report,
    evaluate_bidder_evidence,
)
from planning.hierarchy import build_document_hierarchy
from planning.planner import build_planning_bundle
from planning.unified_budget import (LEDGER_CHARS_PER_TECHNICAL_PAGE,
                                     LEDGER_CHARS_PER_TECHNICAL_PAGE_RUN_D)

from test_mmf007a_hierarchy import _pack, _SELECTION


def _brief(scale: str = "large_technical_tender") -> dict:
    return {"project_name": "银行物业项目", "medium": "WORD", "project_type": "金融办公物业", "scenario": "技术标", "document_scale": scale}


def test_300_class_budget_and_hierarchy_are_real() -> None:
    pack = _pack()
    bundle = build_planning_bundle(pack, _brief(), _SELECTION, production=True)
    hierarchy = build_document_hierarchy(pack, bundle)
    totals = bundle["content_budget"]["totals"]
    assert bundle["content_budget"]["document_scale_code"] == "TECHNICAL_BID"
    # §4/§13：文档级字数额度由**页档推导**，不再写死 240k/260k/280k。旧的那三个数是渲染
    # 口径（1 页≈900 字），却被生成链按账本字（CU 正文 + 结构 payload）消费，两套尺子差
    # 1.18 倍 —— 即便结构成本算对了，计划也只落到 ~220 页，正是 §71 的 Under-Length 陷阱。
    #
    # Round 4.1 §7：这条换算尺还必须**钉在当前这条链的直量上**。上一轮钉的是 Run D 的
    # 467,292 ÷ 395 = 1,183.0 —— 那个数本身没量错，但 Run D 跑在"CU 结构在提交站被跨列表
    # 去重删空"的链上，修掉之后同一条链直量变成 930：
    #   R41 修复后 LEVEL-4   269,646 账本字 ÷ 290 技术页等效 = 929.8
    #   Run D 修复前（历史） 467,292 账本字 ÷ 395 真实技术页  = 1,183.0
    # 继续拿 1,183 排产，285 页档会买到 337,155 字，实测落到 352 页越 320 上沿（R41 实测）。
    assert abs(LEDGER_CHARS_PER_TECHNICAL_PAGE - 269646 / 290) < 0.5
    assert abs(LEDGER_CHARS_PER_TECHNICAL_PAGE_RUN_D - 467292 / 395) < 0.5
    assert LEDGER_CHARS_PER_TECHNICAL_PAGE < LEDGER_CHARS_PER_TECHNICAL_PAGE_RUN_D
    ruler = LEDGER_CHARS_PER_TECHNICAL_PAGE
    pages = bundle["document_target"]["word"]
    assert (totals["target_effective_chars_min"], totals["target_effective_chars_target"],
            totals["target_effective_chars_max"]) == (
        round(pages["target_pages_min"] * ruler),
        round(pages["planning_pages"] * ruler),
        round(pages["target_pages_max"] * ruler))
    assert (totals["target_effective_chars_min"], totals["target_effective_chars_target"],
            totals["target_effective_chars_max"]) == (232500, 265050, 297600)
    assert 250 <= pages["target_pages_min"] <= 320
    # §8/§10：CU 合计 + 机动额度 = 契约合计；§13：计划总量不得超过文档上限，否则不得进生成。
    reserve_words = sum(int((row.get("budget_split") or {}).get("reserve_budget") or 0)
                        for row in hierarchy["sections"])
    assert hierarchy["counts"]["planned_words"] + reserve_words == 265050
    gate = hierarchy["pre_generation_budget_gate"]
    assert gate["status"] == "PASS", gate["blocked_reasons"]
    assert gate["may_enter_generation"] is True
    assert gate["page_floor"] <= gate["projected_technical_effective_pages"] <= gate["page_ceiling"]
    assert gate["worst_case_technical_effective_pages"] <= gate["page_ceiling"]
    assert gate["planned_structure_share"] <= 0.36
    assert all(unit.get("min_budget") and unit.get("target_budget") and unit.get("max_budget") for unit in hierarchy["content_units"])
    assert all(volume.get("budget", {}).get("target", 0) > 0 for volume in hierarchy["volumes"])


def test_archetype_contract_is_diverse_and_complete() -> None:
    pack = _pack()
    hierarchy = build_document_hierarchy(pack, build_planning_bundle(pack, _brief(), _SELECTION, production=True))
    units = hierarchy["content_units"]
    required = {"content_unit_id", "parent_section_id", "title", "intent", "archetype", "must_cover", "required_outputs", "source_requirements", "source_scoring_items", "fact_dependencies", "evidence_dependencies", "min_budget", "target_budget", "max_budget"}
    assert all(required <= set(unit) for unit in units)
    actual = {unit["archetype"] for unit in units}
    assert actual <= set(CONTENT_ARCHETYPES)
    assert {"NARRATIVE", "SOP", "STANDARD_TABLE", "CHECKLIST", "FORM_TEMPLATE"} <= actual


def test_bidder_evidence_gate_is_fail_soft_review() -> None:
    ledger = build_bidder_evidence_ledger({})
    report = evaluate_bidder_evidence({"paragraphs": ["我司已通过ISO认证并拥有AAA信用等级。"]}, ledger)
    assert report["status"] == "NEEDS_REVIEW"
    assert report["fail_soft"] is True
    assert report["unsupported_claim_count"] >= 1
    assert report["findings"][0]["recommended_placeholder"].startswith("【待投标人确认：")

    verified = build_bidder_evidence_ledger({"bidder_evidence": [{
        "evidence_id": "BE-0001", "claim": "我司已通过ISO认证", "source": "证书扫描件",
        "verified": True, "usable_for": ["technical_bid"],
    }]})
    accepted = evaluate_bidder_evidence({"paragraphs": ["我司已通过ISO认证。"]}, verified)
    assert accepted["status"] == "PASS"
    assert accepted["claim_evidence_links"][0]["evidence_ids"] == ["BE-0001"]


def test_requirement_split_compresses_commercial_but_preserves_scoring() -> None:
    pack = _pack()
    pack["requirements"].extend([
        {"requirement_id": "REQ-0100", "normalized_requirement": "投标保证金按招标文件要求提交", "scoring_item_id": None},
        {"requirement_id": "REQ-0101", "normalized_requirement": "企业信用评分项需提供AAA证明", "scoring_item_id": "SCR-099"},
    ])
    classification = build_requirement_classification(pack)
    matrix = build_commercial_compliance_matrix(classification)
    assert any(row["requirement_class"] == "COMMERCIAL" for row in classification["rows"])
    assert all(row["technical_body_policy"] == "COMPACT_REFERENCE_ONLY" for row in matrix["rows"])
    assert any(row.get("scoring_item_id") == "SCR-099" for row in matrix["rows"])


def test_depth_gate_rejects_80k_for_300_class() -> None:
    report = evaluate_longform_depth(
        task_mode="full_longform", word_plan={"outline": [{"sections": [{"section_id": "S01-01"}]}]},
        contracts={"contracts": [{"section_id": "S01-01"}]}, matrix={"matrix": []},
        fragments={"S01-01": {"body_blocks": [{"type": "paragraph", "content": "内容" * 40000}]}},
        gates=[], total_effective_chars=80000,
        content_budget={"document_scale_code": "TECHNICAL_BID", "totals": {"target_effective_chars_min": 240000, "target_effective_chars_target": 260000}},
    )
    assert report["status"] == "DEPTH_INCOMPLETE"
    assert report["targeted_continuation"]["required"] is True
    assert report["targeted_continuation"]["full_document_regeneration"] is False


def test_native_toc_and_native_tables() -> None:
    artifact = {
        "title": "测试技术标", "subtitle": "Synthetic", "include_toc": True, "toc_depth": 1,
        "lead": ["本文件用于验证目录与原生表格。"],
        "sections": [
            {"heading": "第一章 总体策划", "level": 1, "paragraphs": ["总体内容。"], "tables": []},
            {"heading": "一、实施机制", "level": 2, "paragraphs": ["实施内容。"], "tables": [{"title": "责任矩阵", "columns": ["事项", "责任"], "rows": [["执行", "项目负责人"]]}]},
        ],
    }
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        source = root / "content.json"
        output = root / "artifact.docx"
        source.write_text(json.dumps({"brief": _brief("regular_solution"), "artifact": artifact}, ensure_ascii=False), encoding="utf-8")
        result = subprocess.run([sys.executable, str(APP / "build_docx.py"), str(source), str(output)], capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert result.returncode == 0, result.stderr
        report = evaluate_native_toc(output)
        assert report["status"] == "PASS"
        assert report["toc_entry_count"] == 1
        assert report["zero_length_bookmark_count"] == 0
        from docx import Document
        assert len(Document(output).tables) == 1


def test_anti_padding_and_template_leakage() -> None:
    duplicate = "项目名称和建筑面积是基础事实，需要从多个角度反复说明其重要性。"
    report = content_quality_report({"paragraphs": [duplicate, duplicate, duplicate, "产业园特征决定服务模式。"]}, "金融办公物业")
    assert report["duplicate_paragraph_count"] == 2
    assert report["template_leakage_count"] == 1
    assert report["status"] in {"DEPTH_GATE_FAIL", "NEEDS_REVIEW"}
