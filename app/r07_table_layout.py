"""R07 表格分页：行级 cantSplit / 题注 keepNext，不改单元格正文、字号或来源契约。

F-28 根因：装配只写了 w:tblHeader，从未写 w:cantSplit，短行可在单元格内被拆到下一页。
F-30 的标题/表分离：题注 keepNext 必须停在表头单元格，不能顺着表后空段把整表串成一块。

超高出行（折行数超过保守余量，不是精确页高）不强制 cantSplit：渲染器仍可能在行内换页，OOXML 正文不得截断、不得缩字。
不能声称「所有行绝不跨页」。
"""
from __future__ import annotations

from typing import Any, Iterable

from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from docx.table import _Cell

FONT_PT = 9.5
TABLE_TOTAL_DXA = 9360
PAGE_WIDTH_IN = 8.5
PAGE_HEIGHT_IN = 11.0
PAGE_MARGIN_IN = 1.0
BODY_LINE_SPACING = 1.15
# 50 是本产品正文表的保守折行余量，不是渲染器实测页高。
# 页 11in、边距各 1in、表宽 9360 dxa、字号 9.5pt、行距 1.15，再扣题注和表头。
# 超过该余量的行允许拆；不得把 50 当成精确页面高度，也不得因此永不拆行。
OVERLONG_LINE_BUDGET = 50
CAPTION_SIZE_PT = 10.5
BODY_SIZE_PT = 9.5
HEADER_SIZE_PT = 9.5


def line_budget_basis() -> dict[str, Any]:
    """本产品页宽/字号/行距下的行数余量。budget_is_exact_page_height 恒为 False。"""
    content_pt = (PAGE_HEIGHT_IN - 2 * PAGE_MARGIN_IN) * 72.0
    line_pt = FONT_PT * BODY_LINE_SPACING
    return {
        "kind": "heuristic_not_exact_page_height",
        "budget_is_exact_page_height": False,
        "page_width_in": PAGE_WIDTH_IN,
        "page_height_in": PAGE_HEIGHT_IN,
        "margin_in": PAGE_MARGIN_IN,
        "content_width_in": PAGE_WIDTH_IN - 2 * PAGE_MARGIN_IN,
        "table_width_dxa": TABLE_TOTAL_DXA,
        "font_pt": FONT_PT,
        "line_spacing": BODY_LINE_SPACING,
        "caption_pt": CAPTION_SIZE_PT,
        "cell_space_before_pt": 2,
        "cell_space_after_pt": 2,
        "raw_content_lines_at_font": int(content_pt // line_pt),
        "budget_lines": OVERLONG_LINE_BUDGET,
        "scope": (
            "本产品 Word 正文表：8.5in 页宽、四边 1in 边距、表宽 9360 dxa、"
            "单元格 9.5pt、行距 1.15。50 行是扣题注和表头之后的保守余量，"
            "不是 LibreOffice 或 Word 实测页高。超过余量的行允许拆，"
            "正文不截断、不缩字。"
        ),
    }


def _on_off(element, enabled: bool) -> None:
    if enabled:
        element.set(qn("w:val"), "true")
    else:
        element.set(qn("w:val"), "false")


def _first(parent, tag: str):
    return parent.find(qn(tag))


def _ensure_child(parent, tag: str):
    node = _first(parent, tag)
    if node is None:
        node = OxmlElement(tag)
        parent.append(node)
    return node


def _bool_val(node) -> bool:
    if node is None:
        return False
    value = node.get(qn("w:val"))
    if value is None:
        return True
    return str(value).lower() not in {"0", "false", "off"}


def set_row_cant_split(row, enabled: bool) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    existing = tr_pr.findall(qn("w:cantSplit"))
    if not enabled:
        for node in existing:
            tr_pr.remove(node)
        return
    if existing:
        for node in existing:
            _on_off(node, True)
        return
    node = OxmlElement("w:cantSplit")
    _on_off(node, True)
    tr_pr.append(node)


def row_has_cant_split(row) -> bool:
    tr_pr = row._tr.find(qn("w:trPr"))
    if tr_pr is None:
        return False
    return any(_bool_val(node) for node in tr_pr.findall(qn("w:cantSplit")))


def ensure_repeat_header(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    existing = [node for node in tr_pr.findall(qn("w:tblHeader")) if _bool_val(node)]
    if existing:
        return
    node = OxmlElement("w:tblHeader")
    _on_off(node, True)
    tr_pr.append(node)


def row_is_repeat_header(row) -> bool:
    tr_pr = row._tr.find(qn("w:trPr"))
    if tr_pr is None:
        return False
    return any(_bool_val(node) for node in tr_pr.findall(qn("w:tblHeader")))


def set_paragraph_keep_with_next(paragraph, enabled: bool) -> None:
    paragraph.paragraph_format.keep_with_next = bool(enabled)


def paragraph_has_keep_with_next(paragraph) -> bool:
    ppr = paragraph._p.find(qn("w:pPr"))
    if ppr is None:
        return False
    return _bool_val(_first(ppr, "w:keepNext"))


def apply_caption_keep_with_table(paragraph) -> None:
    """题注与下一段落（通常是表头单元格）同页；keepLines 只约束题注自身。"""
    paragraph.paragraph_format.keep_with_next = True
    paragraph.paragraph_format.keep_together = True


def apply_post_table_spacer(paragraph) -> None:
    """表后间隔段必须关掉 keepNext，否则题注 keepNext 会把整表串到这段上。"""
    paragraph.paragraph_format.keep_with_next = False
    paragraph.paragraph_format.space_after = Pt(1)


def _style_keep_with_next(paragraph) -> bool | None:
    """样式链上最近的显式 keepNext。段落自身没有该属性时，None 不等于 False。"""
    try:
        style = paragraph.style
    except (AttributeError, KeyError, ValueError):
        return None
    seen: set[int] = set()
    while style is not None and id(style) not in seen:
        seen.add(id(style))
        fmt = getattr(style, "paragraph_format", None)
        if fmt is not None and fmt.keep_with_next is not None:
            return bool(fmt.keep_with_next)
        style = getattr(style, "base_style", None)
    return None


def clear_cell_keep_with_next(table) -> int:
    """单元格段落不得 keepNext：否则会沿网格把整表焊成一块。

    直接属性为 None 时仍可能从段落样式继承 True。这种单元格要写成显式 False。
    已经是显式 False 的段落保持不变。不修改 Normal 或任何样式定义。
    """
    cleared = 0
    for tr in table._tbl.findall(qn("w:tr")):
        for tc in tr.findall(qn("w:tc")):
            for paragraph in _Cell(tc, table).paragraphs:
                direct = paragraph.paragraph_format.keep_with_next
                if direct is False:
                    continue
                inherited = direct is None and _style_keep_with_next(paragraph) is True
                if direct is True or paragraph_has_keep_with_next(paragraph) or inherited:
                    paragraph.paragraph_format.keep_with_next = False
                    cleared += 1
    return cleared


def table_grid_widths(table) -> list[int]:
    grid = table._tbl.tblGrid
    widths: list[int] = []
    for col in grid.findall(qn("w:gridCol")):
        raw = col.get(qn("w:w")) or "0"
        try:
            widths.append(int(raw))
        except ValueError:
            widths.append(0)
    return widths or [TABLE_TOTAL_DXA]


def estimate_wrapped_lines(text: str, width_dxa: int, *, font_pt: float = FONT_PT) -> int:
    width_in = max(0.4, float(width_dxa) / 1440.0)
    chars_per_line = max(4, int(width_in * 72.0 / max(1.0, font_pt)))
    chunks = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not chunks:
        return 1
    lines = 0
    for chunk in chunks:
        n = max(1, len(chunk))
        lines += max(1, (n + chars_per_line - 1) // chars_per_line)
    return max(1, lines)


def row_should_cant_split(values: Iterable[Any], widths_dxa: list[int], *, header: bool = False) -> bool:
    if header:
        return True
    widths = list(widths_dxa) or [TABLE_TOTAL_DXA]
    tallest = 1
    for index, value in enumerate(values):
        width = widths[index] if index < len(widths) else widths[-1]
        tallest = max(tallest, estimate_wrapped_lines(str(value), width))
    return tallest <= OVERLONG_LINE_BUDGET


def _tc_vmerge(tc) -> str | None:
    tc_pr = tc.find(qn("w:tcPr"))
    if tc_pr is None:
        return None
    node = tc_pr.find(qn("w:vMerge"))
    if node is None:
        return None
    value = node.get(qn("w:val"))
    if value in (None, "continue"):
        return "continue"
    return "restart"


def _tc_grid_span(tc) -> int:
    tc_pr = tc.find(qn("w:tcPr"))
    if tc_pr is None:
        return 1
    grid = tc_pr.find(qn("w:gridSpan"))
    if grid is None:
        return 1
    try:
        return max(1, int(grid.get(qn("w:val")) or "1"))
    except ValueError:
        return 1


def vmerge_row_groups(table) -> list[list[int]]:
    """同一列里彼此独立的 vMerge 链。无合并时为空。

    普通单元格，以及同一列上新的 restart，都会结束上一条链。
    因此 [1,2] 与 [4,5] 不会被收成 [1,2,4,5]。
    gridSpan 只覆盖它声明的列，不把相邻列并进同一条链。
    """
    rows = list(table._tbl.findall(qn("w:tr")))
    if not rows:
        return []
    active: dict[int, list[int]] = {}
    finished: list[list[int]] = []

    def close(col: int) -> None:
        chain = active.pop(col, None)
        if not chain:
            return
        ordered = list(dict.fromkeys(chain))
        if len(ordered) >= 2:
            finished.append(ordered)

    for row_index, tr in enumerate(rows):
        col = 0
        seen_cols: set[int] = set()
        for tc in tr.findall(qn("w:tc")):
            span = _tc_grid_span(tc)
            state = _tc_vmerge(tc)
            covered = range(col, col + span)
            if state == "restart":
                for covered_col in covered:
                    close(covered_col)
                    active[covered_col] = [row_index]
                    seen_cols.add(covered_col)
            elif state == "continue":
                for covered_col in covered:
                    chain = active.get(covered_col)
                    if chain is not None:
                        chain.append(row_index)
                    seen_cols.add(covered_col)
            else:
                for covered_col in covered:
                    close(covered_col)
                    seen_cols.add(covered_col)
            col += span
        for covered_col in [key for key in active if key not in seen_cols]:
            close(covered_col)
    for covered_col in list(active):
        close(covered_col)
    groups: list[list[int]] = []
    seen: set[tuple[int, ...]] = set()
    for chain in finished:
        key = tuple(chain)
        if key in seen:
            continue
        seen.add(key)
        groups.append(chain)
    return groups


def physical_row_texts(row) -> list[str]:
    """本行 OOXML 里实际存在的单元格文本。不展开合并格，不把缺行写成空串。"""
    texts: list[str] = []
    for tc in row._tr.findall(qn("w:tc")):
        paragraphs: list[str] = []
        for paragraph in tc.findall(qn("w:p")):
            chunks: list[str] = []
            for node in paragraph.iter(qn("w:t")):
                if node.text:
                    chunks.append(node.text)
            paragraphs.append("".join(chunks))
        texts.append("\n".join(paragraphs))
    return texts


def row_values_for_layout(row, supplied: list[Any] | None) -> list[Any]:
    """显式矩阵行优先。矩阵没有这一行时读取真实单元格。

    未传入 values、显式矩阵缺行，或宽度列表比列数短，都不能把未知真实行换成空行。
    """
    if supplied is None:
        physical = physical_row_texts(row)
        return physical if physical else [""]
    return list(supplied)


def apply_table_row_pagination(table, *, values: list[list[Any]] | None = None,
                               widths_dxa: list[int] | None = None, header_rows: int = 1) -> dict[str, Any]:
    """按行写入 cantSplit，保留已有 tblHeader；不改单元格文本。"""
    widths = list(widths_dxa) if widths_dxa else table_grid_widths(table)
    matrix = list(values) if values is not None else None
    header_n = max(0, int(header_rows))
    decisions: list[dict[str, Any]] = []
    for index, row in enumerate(table.rows):
        header = index < header_n
        if header:
            ensure_repeat_header(row)
        supplied = matrix[index] if matrix is not None and index < len(matrix) else None
        row_values = row_values_for_layout(row, supplied)
        can_split_off = row_should_cant_split(row_values, widths, header=header)
        decisions.append({
            "row": index,
            "header": header,
            "cant_split": can_split_off,
            "estimated_lines": max(
                (estimate_wrapped_lines(str(value), widths[col] if col < len(widths) else widths[-1])
                 for col, value in enumerate(row_values)),
                default=1,
            ),
        })
    for group in vmerge_row_groups(table):
        if not all(decisions[i]["cant_split"] for i in group if i < len(decisions)):
            for i in group:
                if i < len(decisions):
                    decisions[i]["cant_split"] = False
                    decisions[i]["overlong_merge_group"] = True
    for index, row in enumerate(table.rows):
        set_row_cant_split(row, bool(decisions[index]["cant_split"]))
    cleared = clear_cell_keep_with_next(table)
    return {
        "header_rows": header_n,
        "row_count": len(decisions),
        "cant_split_rows": sum(1 for row in decisions if row["cant_split"]),
        "overlong_rows": [row["row"] for row in decisions if not row["cant_split"]],
        "vmerge_groups": vmerge_row_groups(table),
        "cell_keep_next_cleared": cleared,
        "rows": decisions,
        "line_budget_kind": "heuristic_not_exact_page_height",
        "line_budget_lines": OVERLONG_LINE_BUDGET,
    }


def inspect_table_layout(table) -> dict[str, Any]:
    cell_keep = 0
    cell_paras = 0
    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                cell_paras += 1
                if paragraph_has_keep_with_next(paragraph):
                    cell_keep += 1
    return {
        "row_count": len(table.rows),
        "cant_split_rows": [index for index, row in enumerate(table.rows) if row_has_cant_split(row)],
        "header_rows": [index for index, row in enumerate(table.rows) if row_is_repeat_header(row)],
        "cell_paragraphs": cell_paras,
        "cell_keep_next": cell_keep,
        "vmerge_groups": vmerge_row_groups(table),
        "widths_dxa": table_grid_widths(table),
    }
