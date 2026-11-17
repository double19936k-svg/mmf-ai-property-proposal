"""Case-specific Word v12 scope/expression rules: prose only, never form labels."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from docx import Document

from governance.final_artifact_qa import audit_docx_layout, evaluate_docx_artifact
from planning.content_engineering import scan_scope_text

APP = Path(__file__).resolve().parents[1] / "app"


def _rule_ids(text: str) -> list[str]:
    return [row["rule_id"] for row in scan_scope_text([("artifact.paragraphs[0]", text)])]


POSITIVE = (
    ("该事项不属于其职责，应由甲方另行安排。", "TECHNICAL_SCOPE_REJECTED_EXPRESSION"),
    ("投诉升级的触发条件为三次未闭环。", "TECHNICAL_SCOPE_REJECTED_EXPRESSION"),
    ("双方应明确接口方式后再执行。", "TECHNICAL_SCOPE_REJECTED_EXPRESSION"),
    ("本项目由招标代理机构负责组织现场踏勘。", "TECHNICAL_SCOPE_AGENCY_FOREGROUND"),
    ("投标文件递交地址：某市某区某路88号。", "TECHNICAL_SCOPE_ADDRESS_LISTING"),
    ("水泵编号：SB-2024-01。", "TECHNICAL_SCOPE_EQUIPMENT_NUMBER"),
)

NEGATIVE = (
    "触发条件。责任岗位。处理。验证。台账。关闭。",
    "表单字段包括：关键字段、填写要求、责任岗位、触发条件、保存位置。",
    "事件达到升级条件时，由项目负责人组织处置。",
    "按招标代理机构要求提交资料。",
    "项目位于某市某区，服务范围以合同约定为准。",
    "设备编号管理台账由工程岗位维护。",
    "接口方式这一列按招标文件填写。",
)


def test_case_scope_rules_fire_on_prose():
    for text, expected in POSITIVE:
        assert expected in _rule_ids(text), (text, _rule_ids(text))


def test_case_scope_rules_do_not_fire_on_labels_or_plain_facts():
    for text in NEGATIVE:
        assert _rule_ids(text) == [], (text, _rule_ids(text))


def _build(tmp_path: Path, prose: str) -> Path:
    source = tmp_path / "a.json"
    target = tmp_path / "a.docx"
    payload = {
        "brief": {"project_name": "某项目", "scenario": "物业服务方案", "medium": "WORD"},
        "artifact": {
            "title": "物业服务方案", "subtitle": "技术标正文", "include_toc": True,
            "sections": [{"section_id": "S01-01", "heading": "第一章 项目理解", "level": 1,
                          "content_blocks": [{"type": "paragraph", "content": prose}]}],
        },
    }
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(APP / "build_docx.py"), str(source), str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={"PATH": "", "PYTHONPATH": str(APP), "PYTHONDONTWRITEBYTECODE": "1",
             "PYTHONNOUSERSITE": "1"}, cwd=str(APP.parent))
    assert completed.returncode == 0, completed.stderr[-2000:]
    return target


def test_product_build_passes_layout_audit(tmp_path):
    target = _build(tmp_path, "本项目按区域使用窗口组织服务力量，并按合同约定履约。")
    layout = audit_docx_layout(target)
    assert layout["native_toc_present"] is True
    assert layout["status"] == "PASS", layout
    assert layout["main_title_22pt"] and layout["subtitle_18pt"]
    report = evaluate_docx_artifact(target, brief={"medium": "WORD"})
    assert not [row for row in report["findings"] if row["rule_id"] == "WORD_V12_LAYOUT"]


def test_compound_residue_is_located_in_product_copy(tmp_path):
    target = _build(tmp_path, "处置与复验沿处理情况续写，受理型岗位固定在对外沟通衔接处。")
    report = evaluate_docx_artifact(target, brief={"medium": "WORD"})
    rows = [row for row in report["findings"] if row["rule_id"] == "CONTEXTUAL_TERM_RESIDUE"]
    assert rows, report["findings"]
    assert rows[0]["occurrence_count"] >= 2


def _context() -> dict:
    return {"section_contract": {
        "section_id": "S01-01", "section_title": "项目理解", "must_cover": ["服务范围"],
        "target_words": {"min": 800, "max": 1200}, "source_requirements": ["REQ-0001"],
        "required_processes": [],
    }}


def test_v12_writing_policy_reaches_single_and_batch_prompts():
    from longform.factory import WORD_V12_WRITING_POLICY, LongformGenerationFactory

    context = _context()
    single = LongformGenerationFactory._word_prompt(context)["prompt"]
    factory = LongformGenerationFactory.__new__(LongformGenerationFactory)
    batch = factory._batch_prompt([context], batch_id="GB-01")["prompt"]
    for rule in WORD_V12_WRITING_POLICY:
        assert rule in single, rule
        assert rule in batch, rule


def test_v12_writing_policy_keeps_traceability_not_a_blanket_ban():
    from longform.factory import WORD_V12_WRITING_POLICY

    policy = "\n".join(WORD_V12_WRITING_POLICY)
    assert "需改派时保留原记录并登记交接" in policy
    assert "业务规则不足以判定时列为待决事项" in policy
    assert "一律不得改派" not in policy
    assert "不得写成已经具备或已经承诺" in policy
    assert "不突出招标代理机构" in policy
    assert "登记要素" in policy and "答复口径" in policy and "工单记录" in policy
