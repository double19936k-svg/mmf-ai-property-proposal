"""External cleanup is validated against the human v12 EDIT_LOG, not invented rules.

The 60 pairs are the ground truth for the three compound-term groups.  The
stage must correct those spans contextually, keep legitimate uses of the same
plain words, and never re-introduce a global replacement of 字段/接口/工单号/编号.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from docx import Document
from docx.oxml.ns import qn
from lxml import etree

from governance.external_cleanup import RULES, clean_docx, cleanup_text, has_cleanup_terms

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "word_v12_edit_log.json"
PAYLOAD = json.loads(FIXTURE.read_text(encoding="utf-8"))
OPERATIONS = PAYLOAD["operations"]
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def test_edit_log_fixture_shape():
    assert len(OPERATIONS) == 60
    assert PAYLOAD["counts"] == {"g1": 27, "g2": 23, "g3": 10}
    assert {row["group"] for row in OPERATIONS} == {"g1", "g2", "g3"}


@pytest.mark.parametrize("operation", OPERATIONS, ids=[f"{row['op']:02d}-{row['group']}" for row in OPERATIONS])
def test_each_edit_log_pair_is_reproduced(operation):
    cleaned, actions = cleanup_text(operation["old"])
    assert cleaned == operation["new"], {
        "op": operation["op"], "old": operation["old"],
        "expected": operation["new"], "actual": cleaned,
    }
    assert operation["old"] not in cleaned or operation["old"] == operation["new"]
    assert actions, f"op {operation['op']} produced no mapping entry"


def test_cleanup_is_idempotent_on_every_pair():
    for operation in OPERATIONS:
        once, _ = cleanup_text(operation["old"])
        twice, second_actions = cleanup_text(once)
        assert twice == once, operation["op"]
        assert second_actions == [], operation["op"]


LEGITIMATE = (
    "记录内容包含空间、对象、时刻与经办岗位，缺记录内容当日退回补正并保留补正痕迹",  # only 缺记录内容 is a defect
    "专业负责人次日核对记录内容完整性，缺项当日退回补正并保留补正痕迹。",
    "本项目的沟通衔接有三个方向：向甲方提请审定、向外部专业单位约请到场、在各作业线之间调配人手与顺序。",
    "三类对象的沟通衔接不同，因而需要分别规定，不能合并成一句按甲方要求办理。",
    "三类情形共用同一份登记载体，避免同一事项在三处各有一份说法。",
    "相邻责任以服务事项相互索引，不在两处给出两种状态。",
    "投诉处理情况由客户服务岗位记录，月度复盘时按类别统计。",
    "本表登记每一类的受理方式与反馈去向，使任一事项在进入之初就确定它应走到哪一条线上。",
    "工单号、编号、字段、接口等技术标识与来源键保持原样。",
)


@pytest.mark.parametrize("text", LEGITIMATE)
def test_legitimate_plain_words_survive(text):
    cleaned, _ = cleanup_text(text)
    if text.startswith("记录内容包含空间"):
        assert "记录内容包含空间、对象、时刻与经办岗位" in cleaned
        assert "缺项当日退回补正" in cleaned
        return
    assert cleaned == text


def test_no_bare_global_replacement_of_core_words():
    forbidden = ("字段", "接口", "工单号", "编号", "记录内容", "沟通衔接", "处理情况", "事项")
    for rule_id, pattern, replacement in RULES:
        raw = pattern.pattern
        assert raw not in forbidden, f"{rule_id} replaces a bare core word: {raw}"
        # A bare-term rule would match the word alone; every rule must carry
        # surrounding context (lookaround, prefix or suffix).
        for word in forbidden:
            if word in raw:
                assert raw != word, rule_id
                assert len(raw) > len(word), f"{rule_id} matches {word} without context"


def test_cleanup_terms_are_detectable_before_and_after():
    for operation in OPERATIONS:
        assert has_cleanup_terms(operation["old"]), operation["op"]
        assert not has_cleanup_terms(operation["new"]), operation["op"]


def _docx_with_toc_and_heading(path: Path) -> None:
    document = Document()
    document.add_heading("第一章 项目理解", level=1)
    document.add_paragraph(
        "处置与复验沿处理情况续写，受理型岗位固定在对外沟通衔接处承担登记与引导。")
    document.add_paragraph("记录内容包含空间、对象、时刻与经办岗位，缺项当日退回。")
    # A heading that carries a target phrase is skipped as structure: its text
    # is the pagination/TOC identity, not customer prose.
    document.add_heading("处置情况与沟通衔接", level=2)
    field_paragraph = document.add_paragraph()
    run = field_paragraph.add_run()
    fld = run._r.makeelement(qn("w:fldChar"), {qn("w:fldCharType"): "begin"})
    run._r.append(fld)
    field_paragraph.add_run("记录内容")
    run = field_paragraph.add_run()
    instr = run._r.makeelement(qn("w:instrText"), {qn("xml:space"): "preserve"})
    instr.text = " TOC \\o \"1-3\" "
    run._r.append(instr)
    document.save(str(path))


def test_clean_docx_cleans_body_and_preserves_fields_and_headings(tmp_path):
    source = tmp_path / "source.docx"
    target = tmp_path / "target.docx"
    _docx_with_toc_and_heading(source)
    before = source.read_bytes()
    report = clean_docx(source, target)
    assert source.read_bytes() == before, "product-direct artifact must stay untouched"
    assert report["status"] == "CLEANED"
    assert report["changed_paragraphs"] == 1
    assert report["skipped_field_paragraphs"] >= 1
    assert report["residual_count"] == 0
    assert report["provider_calls"] == 0
    text = "\n".join(p.text for p in Document(str(target)).paragraphs)
    assert "沿同一工单续写" in text
    assert "对外接待窗口" in text
    assert "记录内容包含空间、对象、时刻与经办岗位" in text
    assert "处置情况与沟通衔接" in text, "heading text must not be rewritten"
    with zipfile.ZipFile(target) as archive:
        xml = etree.fromstring(archive.read("word/document.xml"))
    field_texts = ["".join(p.xpath('.//w:t/text()', namespaces={"w": W[1:-1]}))
                   for p in xml.iter(W + "p") if p.findall('.//' + W + "fldChar")]
    assert any("记录内容" in value for value in field_texts), field_texts
    headings = ["".join(p.xpath('.//w:t/text()', namespaces={"w": W[1:-1]}))
                for p in xml.iter(W + "p")
                if (p.find(".//" + W + "pStyle") is not None and
                    "Heading" in str(p.find(".//" + W + "pStyle").get(W + "val")))]
    assert any("第一章" in value for value in headings)
    assert report["source_sha256"] != report["target_sha256"]


def test_clean_docx_no_change_makes_identical_copy(tmp_path):
    document = Document()
    document.add_paragraph("本方案按合同约定组织实施，并按双方确认的结果复核。")
    source = tmp_path / "plain.docx"
    target = tmp_path / "plain_out.docx"
    document.save(str(source))
    report = clean_docx(source, target)
    assert report["status"] == "NO_CHANGES"
    assert report["identical_copy"] is True
    assert source.read_bytes() == target.read_bytes()
