"""Word v12 visual direction: title sizes, left alignment, scoped first-line indent."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

APP = Path(__file__).resolve().parents[1] / "app"


def _artifact_payload() -> dict:
    return {
        "brief": {"project_name": "某项目", "scenario": "物业服务方案", "medium": "WORD"},
        "artifact": {
            "title": "物业服务方案",
            "subtitle": "技术标正文",
            "include_toc": True,
            "toc_depth": 2,
            "lead": ["本方案依据招标文件与现场条件编制，以下按章节说明服务安排。"],
            "sections": [{
                "section_id": "S01-01", "heading": "第一章 项目理解", "level": 1,
                "content_blocks": [
                    {"type": "paragraph", "content": "本项目位于办公与营业混合区域，服务组织按区域使用窗口安排。"},
                    {"type": "bullet_group", "items": ["统一受理", "分区响应"]},
                    {"type": "numbered_steps", "items": ["核对来源", "登记事项", "回告结果"]},
                    {"type": "subheading", "content": "一、服务边界"},
                    {"type": "paragraph", "content": "边界以合同约定与现场确认结果为准。"},
                ],
                "tables": [{
                    "title": "表1 岗位与职责",
                    "columns": ["岗位", "主要职责", "备注"],
                    "rows": [["项目负责人", "统筹服务结果", "常驻"], ["工程人员", "设施设备维护", "按班次"]],
                }],
            }],
        },
    }


def _build(tmp_path: Path) -> Path:
    source = tmp_path / "artifact.json"
    target = tmp_path / "artifact.docx"
    source.write_text(json.dumps(_artifact_payload(), ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(APP / "build_docx.py"), str(source), str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={"PATH": "", "PYTHONPATH": str(APP), "PYTHONDONTWRITEBYTECODE": "1",
             "PYTHONNOUSERSITE": "1"},
        cwd=str(APP.parent),
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return target


def _indent_chars(paragraph) -> str | None:
    p_pr = paragraph._p.pPr
    node = p_pr.find(qn("w:ind")) if p_pr is not None else None
    return node.get(qn("w:firstLineChars")) if node is not None else None


def _style_indent_chars(document, name: str) -> str | None:
    p_pr = document.styles[name].element.pPr
    node = p_pr.find(qn("w:ind")) if p_pr is not None else None
    return node.get(qn("w:firstLineChars")) if node is not None else None


def test_title_sizes_and_left_aligned_body(tmp_path):
    document = Document(str(_build(tmp_path)))
    title = next(p for p in document.paragraphs if p.text == "物业服务方案")
    subtitle = next(p for p in document.paragraphs if p.text == "技术标正文")
    assert title.runs[0].font.size.pt == 22
    assert subtitle.runs[0].font.size.pt == 18
    for name in ("Normal", "Heading 1", "Heading 2", "Heading 3", "List Bullet"):
        assert str(document.styles[name].paragraph_format.alignment) in ("LEFT (0)", "0"), name
    body = next(p for p in document.paragraphs if p.text.startswith("本项目位于"))
    assert _indent_chars(body) == "200"
    assert _style_indent_chars(document, "Normal") == "200"
    heading = next(p for p in document.paragraphs if p.text == "第一章 项目理解")
    assert _indent_chars(heading) in (None, "0")
    assert heading.alignment is not None and int(heading.alignment) == 0


def test_structures_do_not_inherit_prose_indent(tmp_path):
    document = Document(str(_build(tmp_path)))
    bullet = next(p for p in document.paragraphs if p.text == "统一受理")
    numbered = next(p for p in document.paragraphs if p.text == "1. 核对来源")
    subheading = next(p for p in document.paragraphs if p.text == "一、服务边界")
    for paragraph in (bullet, numbered, subheading):
        assert _indent_chars(paragraph) in (None, "0"), paragraph.text
    table = document.tables[0]
    assert table.rows[0]._tr.trPr.find(qn("w:tblHeader")) is not None
    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                assert _indent_chars(paragraph) in (None, "0"), paragraph.text
    caption = next(p for p in document.paragraphs if p.text == "表1 岗位与职责")
    assert _indent_chars(caption) in (None, "0")
    assert caption.paragraph_format.keep_with_next is True


def test_native_toc_bookmarks_and_page_fields(tmp_path):
    target = _build(tmp_path)
    with open(target, "rb") as handle:
        assert handle.read(4) == b"PK\x03\x04"
    document = Document(str(target))
    xml = document._element.xml
    assert "TOC \\o" in xml
    assert "PAGEREF MMF_TOC_0001" in xml
    assert xml.count("w:bookmarkStart") >= 1
    toc_title = next(p for p in document.paragraphs if p.text == "目录")
    assert toc_title.alignment is not None and int(toc_title.alignment) == 0
    assert _indent_chars(toc_title) in (None, "0")
