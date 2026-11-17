from __future__ import annotations

import json
import os
import sys
import tempfile
import hashlib
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT, WD_TAB_LEADER
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from governance.table_qa import LOW_INFORMATION_GOVERNANCE_LABELS, plan_section_tables
from r07_table_layout import (
    apply_caption_keep_with_table,
    apply_post_table_spacer,
    apply_table_row_pagination,
)


# §24-§29：表格按"计划 / 已渲染 / 有效 / 占位"分开记账。子进程没有别的通道把这份
# 台账带回主流程（argv 不能改，output 目录是客户下载目录），所以用环境变量指定落点。
TABLE_LEDGER: list[dict] = []
RENDERED_CELL_CONTRACTS: list[dict] = []
# §63：节级治理报告按节 accumulate —— 合并与转删是"同节兄弟表一起看"才有的决定，
# 单表台账（TABLE_LEDGER）表达不了"这两张并进了一张"。
SECTION_TABLE_GOVERNANCE: list[dict] = []
TABLE_METRICS_ENV = "MMF_ARTIFACT_BUILD_METRICS"
SOURCE_INPUT_RECEIPT: dict = {}

FONT = "Microsoft YaHei"
NAVY = RGBColor(23, 54, 93)
BLUE = RGBColor(46, 117, 182)
INK = RGBColor(32, 42, 56)
MUTED = RGBColor(94, 107, 120)
LIGHT = "F2F4F7"
WHITE = "FFFFFF"


def set_run(run, size=11, bold=False, color=INK, italic=False):
    run.font.name = FONT
    run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), FONT)
    run._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), FONT)
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), FONT)
    run.font.size = Pt(size)
    run.bold = bold
    run.italic = italic
    run.font.color.rgb = color


def configure_style(style, size, color, bold, before, after, line_spacing, align=WD_ALIGN_PARAGRAPH.LEFT):
    style.font.name = FONT
    style._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), FONT)
    style._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), FONT)
    style._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), FONT)
    style.font.size = Pt(size)
    style.font.bold = bold
    style.font.color.rgb = color
    style.paragraph_format.space_before = Pt(before)
    style.paragraph_format.space_after = Pt(after)
    style.paragraph_format.line_spacing = line_spacing
    style.paragraph_format.alignment = align


def set_first_line_chars(paragraph, chars: int = 2, size: int = 11) -> None:
    """First-line indent for continuous prose, in character units.

    `firstLineChars` is the unit Word/WPS use for East Asian text; the twip
    value is the fallback for renderers that ignore the character attribute.
    """
    paragraph.paragraph_format.first_line_indent = Pt(size * chars)
    p_pr = paragraph._p.get_or_add_pPr()
    ind = p_pr.find(qn("w:ind"))
    if ind is None:
        ind = OxmlElement("w:ind")
        p_pr.append(ind)
    ind.set(qn("w:firstLineChars"), str(chars * 100))


def clear_first_line_indent(paragraph) -> None:
    paragraph.paragraph_format.first_line_indent = Pt(0)
    p_pr = paragraph._p.get_or_add_pPr()
    ind = p_pr.find(qn("w:ind"))
    if ind is None:
        ind = OxmlElement("w:ind")
        p_pr.append(ind)
    ind.set(qn("w:firstLineChars"), "0")


def set_style_first_line(style, chars: int, size: int) -> None:
    style.paragraph_format.first_line_indent = Pt(size * chars)
    p_pr = style._element.get_or_add_pPr()
    ind = p_pr.find(qn("w:ind"))
    if ind is None:
        ind = OxmlElement("w:ind")
        p_pr.append(ind)
    ind.set(qn("w:firstLineChars"), str(chars * 100))


def shade_cell(cell, fill):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def set_repeat_header(row):
    tr_pr = row._tr.get_or_add_trPr()
    tag = OxmlElement("w:tblHeader")
    tag.set(qn("w:val"), "true")
    tr_pr.append(tag)


def set_table_geometry(table, widths_dxa):
    table.autofit = False
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    tbl_pr = table._tbl.tblPr
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:type"), "dxa")
    tbl_w.set(qn("w:w"), str(sum(widths_dxa)))
    tbl_ind = tbl_pr.find(qn("w:tblInd"))
    if tbl_ind is None:
        tbl_ind = OxmlElement("w:tblInd")
        tbl_pr.append(tbl_ind)
    tbl_ind.set(qn("w:type"), "dxa")
    tbl_ind.set(qn("w:w"), "120")
    grid = table._tbl.tblGrid
    for child in list(grid):
        grid.remove(child)
    for width in widths_dxa:
        col = OxmlElement("w:gridCol")
        col.set(qn("w:w"), str(width))
        grid.append(col)
    for row in table.rows:
        for index, cell in enumerate(row.cells):
            cell.width = Inches(widths_dxa[index] / 1440)
            tc_w = cell._tc.get_or_add_tcPr().find(qn("w:tcW"))
            if tc_w is None:
                tc_w = OxmlElement("w:tcW")
                cell._tc.get_or_add_tcPr().append(tc_w)
            tc_w.set(qn("w:type"), "dxa")
            tc_w.set(qn("w:w"), str(widths_dxa[index]))


def add_page_number(paragraph):
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    clear_first_line_indent(paragraph)
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.extend([begin, instr, end])
    set_run(run, size=9, color=MUTED)


def add_bookmark(paragraph, name, bookmark_id):
    """Wrap the actual heading runs; WPS cannot navigate zero-length anchors."""
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), str(bookmark_id))
    start.set(qn("w:name"), name)
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(bookmark_id))
    p = paragraph._p
    runs = list(p.findall(qn("w:r")))
    if runs:
        runs[0].addprevious(start)
        runs[-1].addnext(end)
    else:
        p.append(start)
        run = paragraph.add_run(paragraph.text or " ")
        run._r.addnext(end)


def add_native_toc(doc, entries, depth=1):
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.LEFT
    clear_first_line_indent(title)
    set_run(title.add_run("目录"), size=18, bold=True, color=NAVY)
    toc = doc.add_paragraph()
    clear_first_line_indent(toc)
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    begin.set(qn("w:dirty"), "true")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = f' TOC \\o "1-{max(1, min(2, int(depth)))}" \\h \\z \\u '
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    for node in (begin, instr, separate):
        wrapper = OxmlElement("w:r")
        wrapper.append(node)
        toc._p.append(wrapper)
    visible_entries = [entry for entry in entries if int(entry["level"]) <= int(depth)]
    for entry in visible_entries:
        toc_entry = doc.add_paragraph()
        toc_entry.paragraph_format.space_after = Pt(3)
        toc_entry.paragraph_format.left_indent = Inches(0.28 if int(entry["level"]) == 2 else 0)
        clear_first_line_indent(toc_entry)
        toc_entry.paragraph_format.tab_stops.add_tab_stop(Inches(6.2), WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS)
        link = OxmlElement("w:hyperlink")
        link.set(qn("w:anchor"), entry["bookmark"])
        link.set(qn("w:history"), "1")
        run = OxmlElement("w:r")
        rpr = OxmlElement("w:rPr")
        style = OxmlElement("w:rStyle")
        style.set(qn("w:val"), "Hyperlink")
        rpr.append(style)
        run.append(rpr)
        text = OxmlElement("w:t")
        text.text = entry["text"]
        run.append(text)
        link.append(run)
        toc_entry._p.append(link)
        toc_entry.add_run("\t")
        for field_type, instruction in (
            ("begin", None),
            (None, f' PAGEREF {entry["bookmark"]} \\h '),
            ("separate", None),
        ):
            page_run = OxmlElement("w:r")
            if field_type:
                field = OxmlElement("w:fldChar")
                field.set(qn("w:fldCharType"), field_type)
                if field_type == "begin":
                    field.set(qn("w:dirty"), "true")
            else:
                field = OxmlElement("w:instrText")
                field.set(qn("xml:space"), "preserve")
                field.text = instruction
            page_run.append(field)
            toc_entry._p.append(page_run)
        cached_page = OxmlElement("w:r")
        cached_text = OxmlElement("w:t")
        cached_text.text = "1"
        cached_page.append(cached_text)
        toc_entry._p.append(cached_page)
        page_end_run = OxmlElement("w:r")
        page_end = OxmlElement("w:fldChar")
        page_end.set(qn("w:fldCharType"), "end")
        page_end_run.append(page_end)
        toc_entry._p.append(page_end_run)
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    wrapper = OxmlElement("w:r")
    wrapper.append(end)
    end_paragraph = doc.add_paragraph()
    end_paragraph._p.append(wrapper)
    clear_first_line_indent(end_paragraph)
    doc.add_page_break()


def enable_field_update(doc):
    settings = doc.settings._element
    node = settings.find(qn("w:updateFields"))
    if node is None:
        node = OxmlElement("w:updateFields")
        settings.append(node)
    node.set(qn("w:val"), "true")


def add_title_block(doc, artifact, brief):
    # Built-in title styles can carry a decorative border and stale metrics.
    # Configure the actual styles, rather than hiding them with Normal.
    for name,size in (("Title",22),("Subtitle",18)):
        style=doc.styles[name]
        configure_style(style,size,RGBColor(0,0,0),name=="Title",0,5,1.0,WD_ALIGN_PARAGRAPH.CENTER)
        set_style_first_line(style,0,size)
        style.paragraph_format.left_indent=Pt(0)
        style.paragraph_format.right_indent=Pt(0)
        rpr=style._element.get_or_add_rPr()
        for inherited in rpr.xpath('./w:spacing|./w:kern|./w:position|./w:szCs'):
            inherited.getparent().remove(inherited)
        complex_size=OxmlElement('w:szCs');complex_size.set(qn('w:val'),str(size*2));rpr.append(complex_size)
        for border in style._element.xpath('./w:pPr/w:pBdr'):
            border.getparent().remove(border)
    kicker = doc.add_paragraph()
    kicker.alignment = WD_ALIGN_PARAGRAPH.CENTER
    kicker.paragraph_format.space_before = Pt(8)
    kicker.paragraph_format.space_after = Pt(8)
    clear_first_line_indent(kicker)
    set_run(kicker.add_run(brief.get("project_name", "物业项目")), size=11, bold=True, color=INK)

    title = doc.add_paragraph(style="Title")
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.space_after = Pt(5)
    title.paragraph_format.keep_with_next = True
    clear_first_line_indent(title)
    title_text=str(artifact.get("title", brief.get("scenario", "物业方案章节")))
    if len(title_text)>16 and title_text.endswith("物业服务方案"):
        title_text=title_text[:-6]+"\n物业服务方案"
    set_run(title.add_run(title_text), size=22, bold=True, color=RGBColor(0,0,0))

    subtitle = doc.add_paragraph(style="Subtitle")
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    subtitle.paragraph_format.space_after = Pt(24)
    clear_first_line_indent(subtitle)
    set_run(subtitle.add_run(artifact.get("subtitle", f"{brief.get('project_name', '')}｜{brief.get('scenario', '')}")), size=18, color=INK)


def table_ledger_entry(plan: dict) -> dict:
    """一张表的治理决定怎么写进装配台账 —— 渲染出来的和转删的都走同一个形状。

    转删的表也必须留条目：装配台账里 `tables_planned - tables_rendered` 就是"治理拿掉了
    多少张"，只记渲染成功的账会把这件事算成 0。
    """
    if plan.get("action") == "review" or plan.get("needs_review"):
        rows = plan.get("rows")
        return {
            "title": str(plan.get("title") or ""),
            "action": "review",
            "verdict": "needs_review",
            "reason": str(plan.get("table_review_reason") or plan.get("reason") or ""),
            "planned_rows": len(rows) if isinstance(rows, (list, tuple)) else 0,
            "planned_cells": 0,
            "planned_empty_cells": 0,
            "rendered_rows": 0,
            "rendered_cells": 0,
            "rendered_empty_cells": 0,
            "planned_placeholder_cells": 0,
            "rendered_placeholder_cells": 0,
            "meaningful_chars": 0,
            "bidder_fill": False,
            "archetype": plan.get("archetype") or "",
            "is_form": bool(plan.get("is_form")),
            "form_elements_missing": [],
            "fields_overflow": 0,
            "low_information": False,
            "placeholder_prose_heavy": False,
            "notes": plan.get("notes") or "",
            "governance": "needs_review",
            "source_tables": 1,
            "merged_from": [],
            "planned_columns": list(plan.get("columns")) if isinstance(plan.get("columns"), (list, tuple)) else [],
            "rendered_columns": [],
        }
    return {
        "title": plan["title"],
        "action": plan["action"],
        "verdict": plan["verdict"],
        "reason": plan["reason"],
        "planned_rows": plan["metrics_before"]["rows"],
        "planned_cells": plan["metrics_before"]["body_cells"],
        "planned_empty_cells": plan["metrics_before"]["empty_cells"],
        "rendered_rows": len(plan["rows"]),
        "rendered_cells": plan["metrics"]["body_cells"],
        "rendered_empty_cells": plan["metrics"]["empty_cells"],
        "planned_placeholder_cells": plan["metrics_before"]["placeholder_cells"],
        "rendered_placeholder_cells": plan["metrics"]["placeholder_cells"],
        "meaningful_chars": plan["metrics"]["meaningful_chars"],
        "bidder_fill": plan["bidder_fill"],
        # §38：装配端也出一份同口径的表格质量账，交付前就能看到表单缺哪几项要素。
        "archetype": plan.get("archetype") or "",
        "is_form": bool(plan.get("is_form")),
        "form_elements_missing": list(plan.get("form_elements_missing") or []),
        "fields_overflow": plan.get("fields_overflow", 0),
        "low_information": bool(plan["metrics"].get("low_information")),
        "placeholder_prose_heavy": bool(plan["metrics"].get("placeholder_prose_heavy")),
        "notes": plan.get("notes") or "",
        # §63：合并与转删是节级决定，台账要说得出这张表的形状是谁给的、并进了哪一张。
        "governance": plan.get("governance") or "",
        "source_tables": int(plan.get("source_tables") or 1),
        "merged_from": list(plan.get("merged_from") or []),
        "planned_columns": list(plan.get("planned_columns") or []),
        "rendered_columns": list(plan.get("columns") or []),
    }


def add_table(doc, plan):
    """渲染一张**已经过节级治理**的表。治理决定在 `add_section_tables` 里做，这里只画。

    入参必须是 `plan_section_tables`/`plan_table` 的输出：一张表要不要合并、压行、转删，
    在同节其它表一起看时才能决定，所以不允许再从别处以"单张表"为单位绕过节级治理。
    """
    if plan.get("action") == "review" or plan.get("needs_review"):
        reason = str(plan.get("table_review_reason") or "表格结构待复核，已保留原值。")
        original = json.dumps({"title": plan.get("title"), "columns": plan.get("columns"), "rows": plan.get("rows")}, ensure_ascii=False)
        p = doc.add_paragraph()
        clear_first_line_indent(p)
        set_run(p.add_run(reason + " " + original), size=10.5, color=INK)
        return
    if plan["action"] == "drop" or not plan["columns"] or not plan["rows"]:
        return
    columns = plan["columns"]
    rows = plan["rows"]
    if len(columns) < 2:
        for row in rows:
            text = "；".join(str(cell) for cell in row if str(cell).strip())
            if text:
                bullet = doc.add_paragraph(style="List Bullet")
                set_run(bullet.add_run(text), size=10.5, color=INK)
        return
    title = plan["title"]
    if title:
        p = doc.add_paragraph(style="Caption")
        apply_caption_keep_with_table(p)
        clear_first_line_indent(p)
        set_run(p.add_run(title), size=10.5, bold=True, color=INK)
    table = doc.add_table(rows=1, cols=len(columns))
    table.style = "Table Grid"
    for _ in rows:
        table.add_row()
    total = 9360
    base = total // len(columns)
    widths = [base] * len(columns)
    widths[-1] += total - sum(widths)
    set_table_geometry(table, widths)
    set_repeat_header(table.rows[0])
    values = [columns, *rows]
    from governance.table_fidelity import digest
    RENDERED_CELL_CONTRACTS.append({'values': values, 'values_digest': digest(values)})
    for r_index, row_values in enumerate(values):
        for c_index, value in enumerate(row_values):
            cell = table.rows[r_index].cells[c_index]
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            shade_cell(cell, "17365D" if r_index == 0 else (LIGHT if r_index % 2 else WHITE))
            p = cell.paragraphs[0]
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.line_spacing = 1.15
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER if r_index == 0 else WD_ALIGN_PARAGRAPH.LEFT
            clear_first_line_indent(p)
            from governance.proposal_fidelity import display_cell
            column_name = str(columns[c_index]) if c_index < len(columns) else ""
            row_label = "" if r_index == 0 or c_index == 0 else str(row_values[0] if row_values else "")
            set_run(p.add_run(display_cell(value, header=r_index == 0, column_name="" if r_index == 0 else column_name, row_label=row_label)), size=9.5, bold=r_index == 0, color=RGBColor(255, 255, 255) if r_index == 0 else INK)
    apply_table_row_pagination(table, values=values, widths_dxa=widths)
    spacer = doc.add_paragraph()
    apply_post_table_spacer(spacer)
    clear_first_line_indent(spacer)
    from governance.proposal_fidelity import safe_unwrap_to_text
    notes = safe_unwrap_to_text(plan.get("notes")).strip()
    if notes.startswith("表注："):
        notes = notes[3:].strip()
    if notes:
        # §36/§37/§61：表下的字段说明（适用场景、保存位置、保存期限、复核要求、溢出字段名）
        # 必须落到 Word 里。装配端过去把 notes 整个丢掉，于是表单只剩半张空表；
        # 交付端也读不到九要素里的那四项 —— 要素在装配端"齐了"，在交付件里其实不在。
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(2)
        p.paragraph_format.space_after = Pt(6)
        clear_first_line_indent(p)
        set_run(p.add_run(("字段说明：" if plan.get("is_form") else "表注：") + notes),
                size=9, color=RGBColor(0x40, 0x40, 0x40))


def add_section_tables(doc, tables, *, section_id: str = "") -> dict:
    """一节的全部表格：先过 §63 的节级治理（同列低信息表合并、治理后仍低信息转删），
    再逐张渲染。节级动作必须在这里做，因为"能不能合并"要看同节的兄弟表。

    台账记**每一个**决定（含转删与被合并掉的），渲染只走 `governed["tables"]`：
    装配台账与交付端测量对得上，靠的就是"计划数"里包含被拿掉的那些。
    """
    governed = plan_section_tables(tables)
    governed["report"]["section_id"] = str(section_id or "")
    SECTION_TABLE_GOVERNANCE.append(governed["report"])
    for plan in governed["ledger"]:
        TABLE_LEDGER.append(table_ledger_entry(plan))
    from governance.table_fidelity import digest
    seen = set()
    for plan in governed["tables"]:
        columns = plan.get("columns")
        rows = plan.get("rows") if isinstance(plan.get("rows"), list) else []
        body = [list(columns) if isinstance(columns, (list, tuple)) else [str(columns)]]
        for row in rows:
            body.append(list(row) if isinstance(row, (list, tuple)) else [row])
        fingerprint = (str(plan.get("title") or ""), str(plan.get("purpose") or plan.get("notes") or ""), digest(body))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        add_table(doc, plan)
    return governed["report"]


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: build_docx.py input.json output.docx")
    source = Path(sys.argv[1])
    output = Path(sys.argv[2])
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    metrics_file = os.environ.get(TABLE_METRICS_ENV)
    if metrics_file:
        boundary = Path(metrics_file).parent.resolve()
        if source.resolve().is_relative_to(boundary):
            SOURCE_INPUT_RECEIPT.update(file=source.resolve().relative_to(boundary).as_posix(),
                                        sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    brief = payload["brief"]
    artifact = payload["artifact"]
    output.parent.mkdir(parents=True, exist_ok=True)

    doc = Document()
    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(1.0)
    section.right_margin = Inches(1.0)
    section.bottom_margin = Inches(1.0)
    section.left_margin = Inches(1.0)
    section.header_distance = Inches(0.492)
    section.footer_distance = Inches(0.492)

    configure_style(doc.styles["Normal"], 11, INK, False, 0, 8, 1.333, WD_ALIGN_PARAGRAPH.LEFT)
    configure_style(doc.styles["Heading 1"], 16, RGBColor(0,0,0), True, 18, 10, 1.0)
    configure_style(doc.styles["Heading 2"], 13, RGBColor(0,0,0), True, 12, 6, 1.0)
    configure_style(doc.styles["Heading 3"], 12, RGBColor(0,0,0), True, 8, 4, 1.0)
    configure_style(doc.styles["List Bullet"], 11, INK, False, 0, 4, 1.208)
    # 普通散文首行 2 字符；标题、列表、表格单元格等结构在各自创建处清零。
    set_style_first_line(doc.styles["Normal"], 2, 11)
    for style_name in ("Heading 1", "Heading 2", "Heading 3", "List Bullet", "List Number"):
        try:
            set_style_first_line(doc.styles[style_name], 0, 11)
        except KeyError:
            continue

    header_p = section.header.paragraphs[0]
    header_p.paragraph_format.space_after = Pt(0)
    clear_first_line_indent(header_p)
    set_run(header_p.add_run(f"{brief.get('project_name', '')}｜{brief.get('scenario', '')}"), size=9, color=RGBColor(0,0,0))
    add_page_number(section.footer.paragraphs[0])

    add_title_block(doc, artifact, brief)
    doc.add_page_break()
    toc_depth = max(1, min(2, int(artifact.get("toc_depth") or 1)))
    from governance.proposal_fidelity import normalize_heading, prepare_paragraph
    heading_entries = []
    chapter_no = 0
    section_no = 0
    for index, section_data in enumerate(artifact.get("sections", []), 1):
        heading = str(section_data.get("heading", "")).strip()
        level = max(1, min(3, int(section_data.get("level") or 1)))
        if not heading:
            continue
        if level == 1:
            chapter_no += 1
            section_no = 0
        elif level == 2:
            section_no += 1
        _kind, heading = normalize_heading(heading, level, chapter_no, section_no)
        section_data["heading"] = heading
        heading_entries.append({"text": heading, "level": level, "bookmark": f"MMF_TOC_{index:04d}"})
    if artifact.get("include_toc", True):
        add_native_toc(doc, heading_entries, toc_depth)
    for lead in artifact.get("lead", []):
        p = doc.add_paragraph(str(lead))
        p.paragraph_format.space_after = Pt(10)
        set_first_line_chars(p)
        for run in p.runs:
            set_run(run, size=11, color=INK)

    heading_index = 0
    visual_registry = []
    chapter_no = 0
    section_no = 0
    for section_data in artifact.get("sections", []):
        heading = str(section_data.get("heading", "")).strip()
        if not heading:
            continue
        level = max(1, min(3, int(section_data.get("level") or 1)))
        if level == 1:
            chapter_no += 1
            section_no = 0
        elif level == 2:
            section_no += 1
        heading_paragraph = doc.add_heading(heading, level=level)
        heading_paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
        clear_first_line_indent(heading_paragraph)
        entry = heading_entries[heading_index]
        add_bookmark(heading_paragraph, entry["bookmark"], heading_index + 1)
        heading_index += 1
        blocks = section_data.get('content_blocks')
        if blocks is None:
            blocks = ([{'type':'paragraph','content':text} for text in section_data.get('paragraphs', [])]
                      + [{'type':'bullet_group','items':section_data.get('bullets', [])}])
        inline_tables = []
        has_visual = bool(section_data.get("visuals"))
        for block in blocks:
            kind = block.get('type')
            if kind == 'table' and isinstance(block.get('table'), dict):
                inline_tables.append(block['table'])
                add_section_tables(doc, [block['table']], section_id=str(section_data.get("section_id") or heading))
                continue
            if kind in {'bullet_group','numbered_steps'}:
                from governance.proposal_fidelity import filter_and_renumber_steps, safe_unwrap_to_text
                step_items = filter_and_renumber_steps(block.get('items') or []) if kind == 'numbered_steps' else [safe_unwrap_to_text(text) for text in (block.get('items') or [])]
                for number, text in enumerate(step_items, 1):
                    if not str(text).strip():
                        continue
                    p = doc.add_paragraph(style='List Number' if kind == 'numbered_steps' else 'List Bullet')
                    clear_first_line_indent(p)
                    # Explicit local step numbers prevent numbering from running
                    # across unrelated processes in native Word implementations.
                    shown = prepare_paragraph(text if kind == 'numbered_steps' else str(text), has_visual=has_visual)
                    if kind == 'numbered_steps':
                        p.style = 'Normal'
                        p.paragraph_format.left_indent = Inches(.2)
                        clear_first_line_indent(p)
                    set_run(p.add_run(shown), size=11, color=INK)
            elif kind == 'subheading':
                sub_kind, sub_title = normalize_heading(str(block.get('content') or ''), 3, chapter_no, section_no)
                if sub_kind == 'lead':
                    p = doc.add_paragraph()
                    clear_first_line_indent(p)
                    set_run(p.add_run(sub_title), size=11, bold=True, color=INK)
                else:
                    p = doc.add_heading(sub_title, level=3)
                    p.paragraph_format.keep_with_next = True
                    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
                    clear_first_line_indent(p)
            else:
                p = doc.add_paragraph(prepare_paragraph(str(block.get('content') or ''), has_visual=has_visual))
                set_first_line_chars(p)
                for run in p.runs:
                    set_run(run, size=11, color=INK)
        if not inline_tables:
            tables = [row for row in (section_data.get("tables") or []) if isinstance(row, dict)]
            if isinstance(section_data.get("table"), dict):
                tables.append(section_data["table"])
            # §63：整节一起过治理，再逐张渲染。逐张各自决定 = 低信息表永远合不起来。
            # 块序列里已经按导语插好的表不再整节后置，避免“先写完全部段落、再把表堆到节末”。
            add_section_tables(doc, tables, section_id=str(section_data.get("section_id") or heading))
        # Optional visual rendering only. Existing table/editorial/QA decisions
        # are untouched; the factory does not create or infer these IR objects.
        for visual in section_data.get("visuals", []):
            from visuals.docx_renderer import add_visual
            visual_registry.append(add_visual(doc, visual, output.parent / "visual_assets",
                       os.environ.get("MMF_VISUAL_FONT_PATH", "C:/Windows/Fonts/msyh.ttc")))

    doc.core_properties.title = artifact.get("title", "物业方案章节初稿")
    doc.core_properties.subject = brief.get("scenario", "物业方案")
    doc.core_properties.author = "物业服务方案编制团队"
    enable_field_update(doc)
    pack_path = output.parent / "tender" / "requirement_pack.json"
    corpus = pack_path.read_text(encoding="utf-8") if pack_path.is_file() else ""
    from governance.closing_patch import finish_document
    finish_document(doc, corpus)
    doc.save(output)
    metrics_target = os.environ.get(TABLE_METRICS_ENV)
    if metrics_target:
        registry_path = Path(metrics_target).parent / 'visual_render_registry.json'
        registry_path.write_text(json.dumps({'schema_version':'visual-render-register-v1',
                                'visuals':visual_registry}, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    write_table_metrics()


def write_table_metrics():
    target = os.environ.get(TABLE_METRICS_ENV)
    if not target:
        return
    rendered = [row for row in TABLE_LEDGER if row["action"] != "drop"]
    payload = {
        "schema_version": "artifact-table-build-v0.2",
        "rendered_cell_contracts": RENDERED_CELL_CONTRACTS,
        "source_input_receipt": SOURCE_INPUT_RECEIPT,
        # §63：合并之后"台账行数"不再等于"装配进来的表数"。Planned 必须按**来源表**数，
        # 否则两张表并成一张会让 Planned 自己少一张，Planned vs Rendered 就看不出是治理动的。
        "tables_planned": sum(max(1, int(row.get("source_tables") or 1)) for row in TABLE_LEDGER),
        "tables_rendered": len(rendered),
        "tables_dropped": len(TABLE_LEDGER) - len(rendered),
        "tables_compacted": sum(1 for row in TABLE_LEDGER if row["action"] == "compact"),
        "tables_bidder_fill": sum(1 for row in rendered if row["bidder_fill"]),
        # §38：两端的桶必须同一定义。交付端 `audit_docx_tables` 把"格子被套话填满的表单"算进
        # Placeholder（形态仍是 bidder_fill，质量是缺陷），装配端此前只按 verdict 数，于是同一份
        # 文件会同时报 placeholder=3 与 placeholder=34；Meaningful 同样不能把待填写表单算成技术内容。
        "tables_placeholder": sum(1 for row in rendered
                                  if row["verdict"] in {"empty_scaffold", "placeholder_heavy"}
                                  or row["placeholder_prose_heavy"]),
        "tables_meaningful": sum(1 for row in rendered if row["verdict"] == "meaningful"),
        # §38：装配端与交付端同一套口径分开数 —— 只报"多少张表"会让一张 6 行紧凑表单和
        # 一张 24 行空格子的表在报告里长得一模一样。
        "tables_form_templates": sum(1 for row in rendered if row["is_form"]),
        "tables_low_information": sum(1 for row in rendered
                                      if row["verdict"] in {"no_cells", "duplicate_rows"}
                                      or (row["verdict"] == "meaningful" and row["low_information"])),
        "tables_form_elements_incomplete": sum(1 for row in rendered
                                               if row["is_form"] and row["form_elements_missing"]),
        "placeholder_table_share": round(sum(1 for row in rendered
                                             if row["verdict"] in {"empty_scaffold", "placeholder_heavy"}
                                             or row["placeholder_prose_heavy"])
                                         / max(1, len(rendered)), 4),
        "planned_cells": sum(row["planned_cells"] for row in TABLE_LEDGER),
        "rendered_cells": sum(row["rendered_cells"] for row in rendered),
        "planned_empty_cells": sum(row["planned_empty_cells"] for row in TABLE_LEDGER),
        "rendered_empty_cells": sum(row["rendered_empty_cells"] for row in rendered),
        "planned_placeholder_cells": sum(row["planned_placeholder_cells"] for row in TABLE_LEDGER),
        "rendered_placeholder_cells": sum(row["rendered_placeholder_cells"] for row in rendered),
        "meaningful_table_chars": sum(row["meaningful_chars"] for row in rendered),
        "dropped_tables": [row for row in TABLE_LEDGER if row["action"] == "drop"],
        # §63：交付件里"低信息表 = 0"不等于"这一卷没有低信息表"，而是治理把它们合并或
        # 转删了。两个数分开报，否则这份账会把动作读成"天生干净"。
        # 按**来源表**数而不是台账行数：两张表并一张只留一行台账，数行数会把"并掉两张"
        # 读成"并掉一张"，和上面的 Planned 也不是同一个分母。标签取自 table_qa 那一份，
        # 不在这里另列一组字符串（"无有效内容"是 Round 2 单表那条腿的账，不算 §63 的动作）。
        "tables_low_information_governed_away": sum(
            max(1, int(row.get("source_tables") or 1)) for row in TABLE_LEDGER
            if row["governance"] in LOW_INFORMATION_GOVERNANCE_LABELS),
        "section_table_governance": {
            "sections_with_tables": len(SECTION_TABLE_GOVERNANCE),
            "tables_planned": sum(int(row.get("tables_planned") or 0) for row in SECTION_TABLE_GOVERNANCE),
            "tables_rendered": sum(int(row.get("tables_rendered") or 0) for row in SECTION_TABLE_GOVERNANCE),
            "tables_dropped": sum(int(row.get("tables_dropped") or 0) for row in SECTION_TABLE_GOVERNANCE),
            "merge_groups": sum(int(row.get("tables_merged_groups") or 0) for row in SECTION_TABLE_GOVERNANCE),
            "merged_source_tables": sum(int(row.get("tables_merged_sources") or 0)
                                        for row in SECTION_TABLE_GOVERNANCE),
            "low_information_dropped": sum(int(row.get("low_information_dropped") or 0)
                                           for row in SECTION_TABLE_GOVERNANCE),
            "low_information_retained_protected": sum(
                int(row.get("low_information_retained_protected") or 0)
                for row in SECTION_TABLE_GOVERNANCE),
            "per_section": SECTION_TABLE_GOVERNANCE,
        },
        "ledger": TABLE_LEDGER,
    }
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    # MAX_PATH=260：临时名不能比目标更长，否则深层 Run 目录里直接写不出去。
    # mkstemp 会把文件描述符留在打开状态 —— Windows 上打开的文件既不能 os.replace
    # 也不能 unlink，先关掉再写，否则收尾记账把已经成功的装配整个带崩。
    descriptor, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tables.", suffix=".tmp")
    os.close(descriptor)
    handle = Path(temp_name)
    try:
        handle.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(handle, path)
    except OSError:
        # 记账写不出去只是少一份审计，不能牵连客户要下载的 Word。
        pass
    finally:
        try:
            if handle.exists():
                handle.unlink()
        except OSError:
            pass



if __name__ == "__main__":
    main()
