"""R07 table pagination: cantSplit for short rows, no whole-table keepNext chain.

Does not rewrite existing assertions. LibreOffice visual checks never default to PASS.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

import pytest
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from build_docx import add_table, set_run  # noqa: E402
from governance.table_fidelity import digest  # noqa: E402
from r07_table_layout import (  # noqa: E402
    BODY_SIZE_PT,
    FONT_PT,
    OVERLONG_LINE_BUDGET,
    apply_caption_keep_with_table,
    apply_post_table_spacer,
    apply_table_row_pagination,
    clear_cell_keep_with_next,
    ensure_repeat_header,
    estimate_wrapped_lines,
    inspect_table_layout,
    line_budget_basis,
    paragraph_has_keep_with_next,
    physical_row_texts,
    row_has_cant_split,
    row_is_repeat_header,
    row_should_cant_split,
    vmerge_row_groups,
)

BRIEF = {"project_name": "R07表格分页夹具", "scenario": "完整物业服务方案", "medium": "WORD"}
SOFFICE = Path(r"C:\Program Files\LibreOffice\program\soffice.exe")
CREATE_NO_WINDOW = 0x08000000
# Product-isolation fixtures redirect LOCALAPPDATA/TEMP into deep trees. Native
# rendering needs the current user's physical temporary directory instead.
SHORT_LO_ROOT_DEFAULT = ((Path.home() / 'AppData' / 'Local' / 'Temp')
                         if os.name == 'nt' else Path(tempfile.gettempdir())) / 'g7r07r02'
DEEP_PATH_CRASH_CODE = 3221226505
_STRIP = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _STRIP.sub("", text or "")


def _lo_work() -> Path:
    configured = os.environ.get("MMF_R07_LO_WORK", "").strip()
    if configured:
        path = Path(configured)
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(__file__).resolve().parents[2] / "t" / "lo"


def _page_setup(doc: Document) -> None:
    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(1.0)
    section.right_margin = Inches(1.0)
    section.bottom_margin = Inches(1.0)
    section.left_margin = Inches(1.0)


def _fill_cell(cell, text: str, *, header: bool = False) -> None:
    cell.text = str(text)
    color = RGBColor(255, 255, 255) if header else RGBColor(32, 42, 56)
    for paragraph in cell.paragraphs:
        paragraph.paragraph_format.space_before = Pt(2)
        paragraph.paragraph_format.space_after = Pt(2)
        paragraph.paragraph_format.line_spacing = 1.15
        paragraph.paragraph_format.keep_with_next = False
        if not paragraph.runs:
            set_run(paragraph.add_run(paragraph.text or ""), size=FONT_PT, bold=header, color=color)
            continue
        for run in paragraph.runs:
            set_run(run, size=FONT_PT, bold=header, color=color)


def _run_sz_half_points(run) -> int | None:
    rpr = run._element.find(qn("w:rPr"))
    if rpr is None:
        return None
    sz = rpr.find(qn("w:sz"))
    if sz is None:
        return None
    raw = sz.get(qn("w:val"))
    return int(raw) if raw and str(raw).isdigit() else None


def _plan(title: str, columns: list[str], rows: list[list[str]]) -> dict:
    return {
        "action": "keep",
        "title": title,
        "columns": columns,
        "rows": rows,
        "notes": "",
        "is_form": False,
        "metrics": {"body_cells": len(columns) * len(rows), "empty_cells": 0,
                    "placeholder_cells": 0, "meaningful_chars": 10, "low_information": False,
                    "placeholder_prose_heavy": False},
        "metrics_before": {"rows": len(rows), "body_cells": len(columns) * len(rows),
                           "empty_cells": 0, "placeholder_cells": 0},
        "bidder_fill": False,
        "verdict": "meaningful",
        "reason": "fixture",
    }


def _build_docx(tmp: Path, artifact: dict) -> Path:
    source = tmp / "content.json"
    output = tmp / "out.docx"
    source.write_text(json.dumps({"brief": BRIEF, "artifact": artifact}, ensure_ascii=False),
                      encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(APP)
    env.pop("PYTHONHOME", None)
    done = subprocess.run(
        [sys.executable, "-B", str(APP / "build_docx.py"), str(source), str(output)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, env=env, cwd=str(tmp),
    )
    assert done.returncode == 0 and output.is_file(), done.stderr[-600:] + done.stdout[-200:]
    return output


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _short_lo_root() -> Path:
    """soffice convert directory. Never the redirected deep TEMP, never a drive alias."""
    raw = os.environ.get("MMF_R07_LO_SHORT_ROOT", "").strip()
    root = Path(raw) if raw else SHORT_LO_ROOT_DEFAULT
    text = str(root)
    if len(text) > 80 or text.startswith("\\\\"):
        raise RuntimeError("LO_SHORT_ROOT_TOO_LONG")
    root.mkdir(parents=True, exist_ok=True)
    return root


def accept_new_pdf(returncode: int, before_names: set[str], pdf_paths: list[Path]) -> Path | None:
    """A convert succeeds only when it creates exactly one new non-empty PDF.

    A PDF that already existed in the output directory is not a new conversion.
    """
    if returncode != 0:
        return None
    fresh = [path for path in pdf_paths if path.name not in before_names]
    if len(fresh) != 1:
        return None
    chosen = fresh[0]
    if not chosen.is_file() or chosen.stat().st_size <= 0:
        return None
    return chosen


def _run_soffice(target: Path, outdir: Path, profile: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONEXECUTABLE"):
        env.pop(key, None)
    outdir.mkdir(parents=True, exist_ok=True)
    profile.mkdir(parents=True, exist_ok=True)
    command = [
        str(SOFFICE), "--headless", "--norestore", "--invisible",
        f"-env:UserInstallation={profile.resolve().as_uri()}",
        "--convert-to", "pdf", "--outdir", str(outdir.resolve()),
        str(target.resolve()),
    ]
    kwargs = dict(capture_output=True, text=True, encoding="utf-8", errors="replace",
                  timeout=180, shell=False, env=env, stdin=subprocess.DEVNULL)
    if os.name == "nt":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    return subprocess.run(command, **kwargs)


def _read_pdf_pages(pdf: Path) -> list[str]:
    from pypdf import PdfReader
    reader = PdfReader(str(pdf))
    return [page.extract_text() or "" for page in reader.pages]


def _render_pdf(docx: Path, work: Path) -> dict:
    """Headless LO convert of a byte copy in a fresh short directory.

    Deep g7 paths crash soffice with 3221226505. That failure is already recorded,
    so this helper does not launch soffice on the deep path again. An existing PDF
    is never treated as a new conversion. The input DOCX hash must be unchanged.
    """
    work.mkdir(parents=True, exist_ok=True)
    payload = docx.read_bytes()
    source_sha = _sha256(payload)
    note = {
        "status": "NOT_RENDERED",
        "renderer": str(SOFFICE),
        "renderer_exists": SOFFICE.is_file(),
        "work": str(work),
        "docx": None,
        "pdf": None,
        "pdf_sha256": None,
        "pdf_identity": None,
        "returncode": None,
        "pages": [],
        "stderr_tail": "",
        "stdout_tail": "",
        "failure": "",
        "native_path_alias_used": False,
        "docx_sha256_before": source_sha,
        "docx_sha256_after": None,
        "docx_sha_unchanged": False,
        "preexisting_pdf_accepted": False,
        "deep_path_invoked": False,
        "deep_path_known_failure": DEEP_PATH_CRASH_CODE,
        "deep_path_policy": "known_crash_not_retried",
        "outdir_empty_before": False,
        "profile_created_fresh": False,
        "attempts": [],
    }
    try:
        root = _short_lo_root()
    except RuntimeError as exc:
        note["failure"] = str(exc)
        return note
    if not SOFFICE.is_file():
        note["failure"] = "NO_RENDERER"
        return note
    slot = root / uuid.uuid4().hex[:12]
    profile = slot / "profile"
    outdir = slot / "out"
    profile.mkdir(parents=True)
    outdir.mkdir()
    before_names = {path.name for path in outdir.glob("*.pdf")}
    note["outdir_empty_before"] = not any(outdir.iterdir()) and not before_names
    note["profile_created_fresh"] = not any(profile.iterdir())
    note["short_slot"] = str(slot)
    target = slot / "in.docx"
    target.write_bytes(payload)
    sha_before = _sha256(target.read_bytes())
    note["docx"] = str(target)
    note["docx_sha256_before"] = sha_before
    try:
        done = _run_soffice(target, outdir, profile)
    except (OSError, subprocess.SubprocessError) as exc:
        note["failure"] = f"{type(exc).__name__}: {str(exc)[:240]}"
        note["attempts"].append({"label": "short_temp_copy", "failure": note["failure"]})
        return note
    sha_after = _sha256(target.read_bytes())
    note["docx_sha256_after"] = sha_after
    note["docx_sha_unchanged"] = sha_before == sha_after == source_sha
    pdfs = sorted(outdir.glob("*.pdf"))
    note["returncode"] = done.returncode
    note["stderr_tail"] = (done.stderr or "")[-400:]
    note["stdout_tail"] = (done.stdout or "")[-400:]
    note["attempts"].append({
        "label": "short_temp_copy",
        "returncode": done.returncode,
        "pdfs_before": sorted(before_names),
        "pdfs_after": [path.name for path in pdfs],
        "input": str(target),
        "profile": str(profile),
        "stderr_tail": (done.stderr or "")[-200:],
    })
    if not note["docx_sha_unchanged"]:
        note["failure"] = "DOCX_MUTATED"
        return note
    chosen = accept_new_pdf(done.returncode, before_names, pdfs)
    if chosen is None:
        note["failure"] = "NO_NEW_PDF" if done.returncode == 0 else "RENDERER_EXIT"
        return note
    pdf_bytes = chosen.read_bytes()
    pdf_sha = _sha256(pdf_bytes)
    copied = work / "t.pdf"
    copied.write_bytes(pdf_bytes)
    if _sha256(copied.read_bytes()) != pdf_sha:
        note["failure"] = "PDF_COPY_MISMATCH"
        return note
    note["pdf"] = str(copied)
    note["pdf_sha256"] = pdf_sha
    note["pdf_identity"] = f"{chosen.name}:{pdf_sha}"
    note["convert_path"] = "short_temp_copy"
    try:
        pages = _read_pdf_pages(copied)
    except Exception as exc:
        note["failure"] = f"PypdfRead: {type(exc).__name__}: {str(exc)[:200]}"
        return note
    note["pages"] = pages
    note["page_count"] = len(pages)
    note["status"] = "RENDERED"
    return note


def _pages_with(token: str, pages: list[str]) -> list[int]:
    needle = _norm(token)
    return [index + 1 for index, text in enumerate(pages) if needle and needle in _norm(text)]


def test_short_row_gets_cantsplit_overlong_row_does_not():
    short = ["事项A", "短说明"]
    long_cell = "超长行正文" * 80 + "\n".join(f"LONG{index:03d}" for index in range(60))
    widths = [4680, 4680]
    assert row_should_cant_split(short, widths, header=False) is True
    assert row_should_cant_split(["列", "头"], widths, header=True) is True
    assert row_should_cant_split(["事项B", long_cell], widths, header=False) is False
    assert estimate_wrapped_lines(long_cell, 4680) > OVERLONG_LINE_BUDGET


def test_production_add_table_emits_cantsplit_header_and_caption_keepnext(tmp_path):
    columns = ["事项", "口径"]
    rows = [["巡检", "当班岗位按楼层完成"], ["交接", "书面签字"]]
    doc = Document()
    _page_setup(doc)
    add_table(doc, _plan("表题CAP_KEEP", columns, rows))
    assert len(doc.tables) == 1
    table = doc.tables[0]
    layout = inspect_table_layout(table)
    assert layout["header_rows"] == [0]
    assert layout["cant_split_rows"] == [0, 1, 2]
    assert layout["cell_keep_next"] == 0
    assert layout["row_count"] == 3
    captions = [p for p in doc.paragraphs if "表题CAP_KEEP" in p.text]
    assert len(captions) == 1
    assert paragraph_has_keep_with_next(captions[0]) is True
    after = False
    for paragraph in doc.paragraphs:
        if after:
            assert paragraph_has_keep_with_next(paragraph) is False
            break
        if "表题CAP_KEEP" in paragraph.text:
            after = True
    for r_index, row in enumerate(table.rows):
        for cell in row.cells:
            for run in cell.paragraphs[0].runs:
                half = _run_sz_half_points(run)
                assert half == 19, half
                assert run.font.size == Pt(BODY_SIZE_PT)
    values = [columns, *rows]
    for r_index, row_values in enumerate(values):
        for c_index, value in enumerate(row_values):
            assert table.rows[r_index].cells[c_index].text.strip() == value
    path = tmp_path / "prod.docx"
    doc.save(path)
    metrics = {"values": values, "values_digest": digest(values)}
    assert metrics["values_digest"] == digest([
        [table.rows[r].cells[c].text.strip() for c in range(len(columns))]
        for r in range(len(table.rows))
    ])


def test_build_docx_preserves_source_cells_and_does_not_add_empty_rows(tmp_path):
    columns = ["编号", "内容"]
    rows = [[f"C{index:02d}", f"单元格正文{index:02d}不得丢失"] for index in range(1, 6)]
    artifact = {
        "title": "R07源单元格夹具", "include_toc": False,
        "lead": ["引导段。"],
        "sections": [{
            "heading": "第一章 表格分页", "level": 1,
            "paragraphs": ["正文段。"],
            "tables": [_plan("源绑定表", columns, rows)],
        }],
    }
    docx = _build_docx(tmp_path, artifact)
    document = Document(docx)
    assert len(document.tables) == 1
    table = document.tables[0]
    assert len(table.rows) == 1 + len(rows)
    assert row_is_repeat_header(table.rows[0])
    assert all(row_has_cant_split(row) for row in table.rows)
    layout = inspect_table_layout(table)
    assert layout["cell_keep_next"] == 0
    rendered = [[cell.text.strip() for cell in row.cells] for row in table.rows]
    assert rendered[0] == columns
    assert rendered[1:] == rows
    assert digest(rendered) == digest([columns, *rows])
    caption = next(p for p in document.paragraphs if p.text.strip() == "源绑定表")
    assert paragraph_has_keep_with_next(caption)
    body_keep = [p for p in document.paragraphs if p.text.strip() == "正文段。" and paragraph_has_keep_with_next(p)]
    assert body_keep == []


def test_merged_cells_keep_vmerge_and_source_text(tmp_path):
    doc = Document()
    _page_setup(doc)
    table = doc.add_table(rows=4, cols=3)
    grid = ["H0", "H1", "H2"]
    body = [["M_START", "a1", "a2"], ["", "b1", "b2"], ["c0", "c1", "c2"]]
    for c_index, value in enumerate(grid):
        _fill_cell(table.rows[0].cells[c_index], value, header=True)
    for r_index, row_values in enumerate(body, 1):
        for c_index, value in enumerate(row_values):
            _fill_cell(table.rows[r_index].cells[c_index], value)
    start = table.cell(1, 0)
    start.merge(table.cell(2, 0))
    _fill_cell(start, "M_START")
    table.cell(0, 1).merge(table.cell(0, 2))
    apply_table_row_pagination(
        table,
        values=[grid, ["M_START", "a1", "a2"], ["M_START", "b1", "b2"], ["c0", "c1", "c2"]],
        widths_dxa=[3120, 3120, 3120],
        header_rows=1,
    )
    layout = inspect_table_layout(table)
    assert layout["header_rows"] == [0]
    assert 0 in layout["cant_split_rows"]
    assert layout["vmerge_groups"]
    xml = table._tbl
    vmerge = xml.findall(".//" + qn("w:vMerge"))
    assert vmerge, "vMerge must remain in OOXML"
    texts = [cell.text.strip() for row in table.rows for cell in row.cells]
    assert "M_START" in texts
    assert "a1" in texts and "b2" in texts and "c1" in texts
    path = tmp_path / "merge.docx"
    doc.save(path)
    reopened = Document(path).tables[0]
    assert inspect_table_layout(reopened)["vmerge_groups"]


def test_keep_with_next_does_not_chain_through_table_cells(tmp_path):
    doc = Document()
    _page_setup(doc)
    caption = doc.add_paragraph()
    apply_caption_keep_with_table(caption)
    set_run(caption.add_run("题注CHAIN"), size=10.5, bold=True)
    table = doc.add_table(rows=4, cols=2)
    values = [["H0", "H1"], ["r1", "v1"], ["r2", "v2"], ["r3", "v3"]]
    for r_index, row_values in enumerate(values):
        for c_index, value in enumerate(row_values):
            _fill_cell(table.rows[r_index].cells[c_index], value, header=r_index == 0)
            table.rows[r_index].cells[c_index].paragraphs[0].paragraph_format.keep_with_next = True
    apply_table_row_pagination(table, values=values, widths_dxa=[4680, 4680])
    spacer = doc.add_paragraph()
    apply_post_table_spacer(spacer)
    assert paragraph_has_keep_with_next(caption) is True
    assert inspect_table_layout(table)["cell_keep_next"] == 0
    assert paragraph_has_keep_with_next(spacer) is False
    assert all(row_has_cant_split(row) for row in table.rows)


def test_overlong_row_ooxml_allows_split_and_does_not_truncate():
    marker_first = "LONG000"
    marker_last = "LONG059"
    lines = [f"LONG{index:03d} 超长行保留全文不得截断" * 3 for index in range(60)]
    values = [["项目", "说明"], ["超长", "\n".join(lines)]]
    doc = Document()
    table = doc.add_table(rows=2, cols=2)
    for r_index, row_values in enumerate(values):
        for c_index, value in enumerate(row_values):
            _fill_cell(table.rows[r_index].cells[c_index], value, header=r_index == 0)
    report = apply_table_row_pagination(table, values=values, widths_dxa=[2400, 6960])
    assert row_has_cant_split(table.rows[0]) is True
    assert report["overlong_rows"] == [1]
    assert row_has_cant_split(table.rows[1]) is False
    body = table.rows[1].cells[1].text
    assert marker_first in body and marker_last in body
    assert "不得截断" in body
    assert len(body) >= len(values[1][1])


def _lo_cases(tmp_path: Path) -> dict:
    work = _lo_work()
    evidence = {"schema": "r07-lo-cases-v1", "work": str(work), "cases": []}

    filler = ("本段只用于把短行推到页底附近，不进入表格单元格。"
              "分页夹具填充句。" * 18)

    short_doc = Document()
    _page_setup(short_doc)
    for _ in range(7):
        p = short_doc.add_paragraph(filler)
        p.paragraph_format.space_after = Pt(8)
        for run in p.runs:
            set_run(run, size=11)
    end = short_doc.add_paragraph("FILLER_END_MARK")
    set_run(end.runs[0] if end.runs else end.add_run("FILLER_END_MARK"), size=11)
    cap = short_doc.add_paragraph()
    apply_caption_keep_with_table(cap)
    set_run(cap.add_run("CAPTION_MARK 短行表"), size=10.5, bold=True)
    columns = ["列甲", "列乙", "列丙"]
    header = ["HDRCOL0", "HDRCOL1", "HDRCOL2"]
    data = [[f"R{r}C0", f"R{r}C1", f"R{r}C2短"] for r in range(1, 9)]
    table = short_doc.add_table(rows=1 + len(data), cols=3)
    values = [header, *data]
    for r_index, row_values in enumerate(values):
        for c_index, value in enumerate(row_values):
            _fill_cell(table.rows[r_index].cells[c_index], value, header=r_index == 0)
    apply_table_row_pagination(table, values=values, widths_dxa=[3120, 3120, 3120])
    spacer = short_doc.add_paragraph()
    apply_post_table_spacer(spacer)
    short_path = tmp_path / "short.docx"
    short_doc.save(short_path)
    short_note = _render_pdf(short_path, work / "s")
    short_case = {"id": "LO-SHORT-ROW", "render": {k: v for k, v in short_note.items() if k != "pages"}}
    if short_note["status"] == "RENDERED":
        pages = short_note["pages"]
        short_case["page_count"] = len(pages)
        short_case["caption_pages"] = _pages_with("CAPTION_MARK", pages)
        short_case["header_pages"] = _pages_with("HDRCOL0", pages)
        short_case["filler_pages"] = _pages_with("FILLER_END_MARK", pages)
        row_pages = {f"R{r}": sorted(set().union(*(_pages_with(f"R{r}C{c}", pages) for c in range(3))))
                     for r in range(1, 9)}
        short_case["row_pages"] = row_pages
        short_case["short_row_unsplit"] = all(len(found) <= 1 for found in row_pages.values())
        short_case["caption_with_header"] = bool(
            set(short_case["caption_pages"]) & set(short_case["header_pages"])
        )
        short_case["table_not_glued_off_page"] = bool(
            set(short_case["filler_pages"]) & set(short_case["header_pages"])
            or (short_case["filler_pages"] and short_case["header_pages"]
                and short_case["header_pages"][0] == short_case["filler_pages"][-1])
        )
    evidence["cases"].append(short_case)

    glue_doc = Document()
    _page_setup(glue_doc)
    p = glue_doc.add_paragraph("GLUE_TOP_MARK " + filler[:80])
    set_run(p.runs[0] if p.runs else p.add_run(p.text), size=11)
    cap = glue_doc.add_paragraph()
    apply_caption_keep_with_table(cap)
    set_run(cap.add_run("GLUE_CAPTION"), size=10.5, bold=True)
    glue_header = ["GH0", "GH1"]
    glue_data = [[f"G{r}A", f"G{r}B"] for r in range(1, 41)]
    glue_table = glue_doc.add_table(rows=41, cols=2)
    glue_values = [glue_header, *glue_data]
    for r_index, row_values in enumerate(glue_values):
        for c_index, value in enumerate(row_values):
            _fill_cell(glue_table.rows[r_index].cells[c_index], value, header=r_index == 0)
    apply_table_row_pagination(glue_table, values=glue_values, widths_dxa=[4680, 4680])
    apply_post_table_spacer(glue_doc.add_paragraph())
    glue_path = tmp_path / "glue.docx"
    glue_doc.save(glue_path)
    glue_note = _render_pdf(glue_path, work / "g")
    glue_case = {"id": "LO-NOT-GLUE", "render": {k: v for k, v in glue_note.items() if k != "pages"}}
    if glue_note["status"] == "RENDERED":
        pages = glue_note["pages"]
        glue_case["page_count"] = len(pages)
        glue_case["top_pages"] = _pages_with("GLUE_TOP_MARK", pages)
        glue_case["first_row_pages"] = _pages_with("G1A", pages)
        glue_case["last_row_pages"] = _pages_with("G40A", pages)
        glue_case["header_pages"] = _pages_with("GH0", pages)
        glue_case["table_starts_on_first_content_page"] = bool(
            glue_case["top_pages"] and glue_case["first_row_pages"]
            and glue_case["first_row_pages"][0] == glue_case["top_pages"][0]
        )
        glue_case["table_spans_pages"] = glue_case["page_count"] >= 2
        glue_case["header_repeats_if_spanned"] = (
            not glue_case["table_spans_pages"]
            or len(glue_case["header_pages"]) >= 2
        )
        glue_row_pages = {f"G{r}": _pages_with(f"G{r}A", pages) for r in range(1, 41)}
        glue_case["row_pages"] = glue_row_pages
        glue_case["short_row_unsplit"] = all(len(found) == 1 for found in glue_row_pages.values())
    evidence["cases"].append(glue_case)

    long_doc = Document()
    _page_setup(long_doc)
    cap = long_doc.add_paragraph()
    apply_caption_keep_with_table(cap)
    set_run(cap.add_run("LONG_CAPTION"), size=10.5, bold=True)
    long_lines = [f"LONG{index:03d}行内换页仍须可读" * 4 for index in range(60)]
    long_values = [["LH0", "LH1"], ["超长", "\n".join(long_lines)]]
    long_table = long_doc.add_table(rows=2, cols=2)
    for r_index, row_values in enumerate(long_values):
        for c_index, value in enumerate(row_values):
            _fill_cell(long_table.rows[r_index].cells[c_index], value, header=r_index == 0)
    apply_table_row_pagination(long_table, values=long_values, widths_dxa=[2400, 6960])
    long_path = tmp_path / "long.docx"
    long_doc.save(long_path)
    long_note = _render_pdf(long_path, work / "l")
    long_case = {"id": "LO-OVERLONG", "render": {k: v for k, v in long_note.items() if k != "pages"}}
    if long_note["status"] == "RENDERED":
        pages = long_note["pages"]
        blob = _norm("".join(pages))
        long_case["page_count"] = len(pages)
        long_case["has_first"] = "LONG000" in blob
        long_case["has_last"] = "LONG059" in blob
        long_case["not_truncated"] = long_case["has_first"] and long_case["has_last"]
    evidence["cases"].append(long_case)

    merge_doc = Document()
    _page_setup(merge_doc)
    cap = merge_doc.add_paragraph()
    apply_caption_keep_with_table(cap)
    set_run(cap.add_run("MERGE_CAPTION"), size=10.5, bold=True)
    merge_table = merge_doc.add_table(rows=4, cols=2)
    merge_values = [["MH0", "MH1"], ["MERGE_CELL", "x1"], ["MERGE_CELL", "x2"], ["z0", "z1"]]
    for r_index, row_values in enumerate(merge_values):
        for c_index, value in enumerate(row_values):
            _fill_cell(merge_table.rows[r_index].cells[c_index], value, header=r_index == 0)
    merge_table.cell(1, 0).merge(merge_table.cell(2, 0))
    apply_table_row_pagination(merge_table, values=merge_values, widths_dxa=[4680, 4680])
    merge_path = tmp_path / "merge_lo.docx"
    merge_doc.save(merge_path)
    merge_note = _render_pdf(merge_path, work / "m")
    merge_case = {"id": "LO-MERGE", "render": {k: v for k, v in merge_note.items() if k != "pages"}}
    if merge_note["status"] == "RENDERED":
        blob = _norm("".join(merge_note["pages"]))
        merge_case["page_count"] = len(merge_note["pages"])
        merge_case["has_merge_text"] = "MERGE_CELL" in blob
        merge_case["has_neighbors"] = "x1" in blob and "x2" in blob and "z1" in blob
    evidence["cases"].append(merge_case)
    return evidence


def _physical_snapshot(table) -> list[list[str]]:
    return [physical_row_texts(row) for row in table.rows]


def test_default_call_matches_explicit_overlong_row():
    long_text = "LONG " + "字" * 10000
    reports = []
    for use_values in (False, True):
        doc = Document()
        table = doc.add_table(rows=2, cols=1)
        table.cell(0, 0).text = "Header"
        table.cell(1, 0).text = long_text
        before = _physical_snapshot(table)
        kwargs = {"values": [["Header"], [long_text]]} if use_values else {}
        report = apply_table_row_pagination(table, **kwargs)
        assert _physical_snapshot(table) == before
        assert table.cell(1, 0).text == long_text
        assert row_has_cant_split(table.rows[1]) is False
        assert report["rows"][1]["estimated_lines"] > OVERLONG_LINE_BUDGET
        assert report["line_budget_kind"] == "heuristic_not_exact_page_height"
        reports.append(report)
    assert reports[0]["rows"][1]["cant_split"] == reports[1]["rows"][1]["cant_split"]
    assert reports[0]["rows"][1]["estimated_lines"] == reports[1]["rows"][1]["estimated_lines"]


def test_short_row_without_values_keeps_cantsplit():
    doc = Document()
    table = doc.add_table(rows=2, cols=1)
    table.cell(0, 0).text = "Header"
    table.cell(1, 0).text = "短行"
    before = table.cell(1, 0).text
    report = apply_table_row_pagination(table)
    assert row_has_cant_split(table.rows[0]) is True
    assert row_has_cant_split(table.rows[1]) is True
    assert report["rows"][1]["estimated_lines"] <= OVERLONG_LINE_BUDGET
    assert table.cell(1, 0).text == before


def test_missing_matrix_row_and_short_widths_read_real_cells():
    long_text = "宽" * 3000
    doc = Document()
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "H0"
    table.cell(0, 1).text = "H1"
    table.cell(1, 0).text = "左"
    table.cell(1, 1).text = long_text
    before = _physical_snapshot(table)
    report = apply_table_row_pagination(table, values=[["H0", "H1"]], widths_dxa=[1500])
    assert _physical_snapshot(table) == before
    assert report["rows"][1]["cant_split"] is False
    assert report["rows"][1]["estimated_lines"] > OVERLONG_LINE_BUDGET
    assert row_has_cant_split(table.rows[1]) is False
    assert table.cell(1, 1).text == long_text
    explicit = apply_table_row_pagination(
        table, values=[["H0", "H1"], ["左", long_text]], widths_dxa=[1500],
    )
    assert explicit["rows"][1]["cant_split"] is False
    assert table.cell(1, 1).text == long_text


def test_separate_vmerge_groups_do_not_share_split_decision():
    doc = Document()
    table = doc.add_table(rows=6, cols=2)
    table.cell(1, 0).merge(table.cell(2, 0))
    table.cell(4, 0).merge(table.cell(5, 0))
    assert vmerge_row_groups(table) == [[1, 2], [4, 5]]

    adjacent = Document().add_table(rows=5, cols=2)
    adjacent.cell(1, 0).merge(adjacent.cell(2, 0))
    adjacent.cell(3, 0).merge(adjacent.cell(4, 0))
    assert vmerge_row_groups(adjacent) == [[1, 2], [3, 4]]

    long_text = "组" * 4000
    for column in range(2):
        table.cell(0, column).text = f"H{column}"
    table.cell(1, 0).text = "SHORT_MERGE"
    table.cell(1, 1).text = "s1"
    table.cell(2, 1).text = "s2"
    table.cell(3, 0).text = "GAP"
    table.cell(3, 1).text = "g1"
    table.cell(4, 0).text = long_text
    table.cell(4, 1).text = "L1"
    table.cell(5, 1).text = "L2"
    before = _physical_snapshot(table)
    report = apply_table_row_pagination(table, widths_dxa=[4680, 4680])
    assert report["vmerge_groups"] == [[1, 2], [4, 5]]
    assert _physical_snapshot(table) == before
    assert row_has_cant_split(table.rows[1]) is True
    assert row_has_cant_split(table.rows[2]) is True
    assert row_has_cant_split(table.rows[4]) is False
    assert row_has_cant_split(table.rows[5]) is False
    assert "SHORT_MERGE" in table.cell(1, 0).text
    assert long_text in table.cell(4, 0).text
    assert table.cell(2, 1).text == "s2"
    assert table.cell(5, 1).text == "L2"


def test_gridspan_boundary_keeps_neighbor_vmerge_and_leaf_text():
    doc = Document()
    table = doc.add_table(rows=3, cols=3)
    table.cell(0, 0).merge(table.cell(0, 1))
    table.cell(0, 0).text = "SPAN_HEAD"
    table.cell(0, 2).text = "H2"
    table.cell(1, 2).text = "LEAF_A"
    table.cell(2, 2).text = "LEAF_B"
    table.cell(1, 0).text = "N0"
    table.cell(1, 1).text = "N1"
    table.cell(2, 0).text = "N2"
    table.cell(2, 1).text = "N3"
    table.cell(1, 2).merge(table.cell(2, 2))
    table.cell(1, 2).text = "VERT_ONLY"
    assert vmerge_row_groups(table) == [[1, 2]]
    before = _physical_snapshot(table)
    apply_table_row_pagination(table, header_rows=1)
    assert vmerge_row_groups(table) == [[1, 2]]
    assert _physical_snapshot(table) == before
    assert "SPAN_HEAD" in table.cell(0, 0).text
    assert "VERT_ONLY" in table.cell(1, 2).text
    assert table.cell(1, 1).text == "N1"


def test_inherited_keep_next_is_explicit_false_and_normal_style_stays():
    doc = Document()
    normal = doc.styles["Normal"]
    normal_before = normal.paragraph_format.keep_with_next
    style = doc.styles.add_style("R07InheritedKeep", WD_STYLE_TYPE.PARAGRAPH)
    style.paragraph_format.keep_with_next = True
    table = doc.add_table(rows=2, cols=1)
    paragraph = table.cell(1, 0).paragraphs[0]
    paragraph.style = style
    assert paragraph.paragraph_format.keep_with_next is None
    cleared = clear_cell_keep_with_next(table)
    assert cleared >= 1
    assert paragraph.paragraph_format.keep_with_next is False
    assert style.paragraph_format.keep_with_next is True
    assert normal.paragraph_format.keep_with_next == normal_before
    body = doc.add_paragraph("正文不受单元格清链影响")
    assert body.paragraph_format.keep_with_next is None
    caption = doc.add_paragraph("题注样式未被改写")
    apply_caption_keep_with_table(caption)
    assert paragraph_has_keep_with_next(caption) is True
    assert normal.paragraph_format.keep_with_next == normal_before


def test_explicit_false_keep_next_overrides_inherited_style():
    doc = Document()
    style = doc.styles.add_style("R07ExplicitFalse", WD_STYLE_TYPE.PARAGRAPH)
    style.paragraph_format.keep_with_next = True
    table = doc.add_table(rows=2, cols=1)
    paragraph = table.cell(1, 0).paragraphs[0]
    paragraph.style = style
    paragraph.paragraph_format.keep_with_next = False
    assert paragraph.paragraph_format.keep_with_next is False
    clear_cell_keep_with_next(table)
    assert paragraph.paragraph_format.keep_with_next is False
    assert style.paragraph_format.keep_with_next is True
    assert paragraph_has_keep_with_next(paragraph) is False


def test_line_budget_is_product_heuristic_and_overlong_row_may_split():
    basis = line_budget_basis()
    assert basis["budget_is_exact_page_height"] is False
    assert basis["page_width_in"] == 8.5
    assert basis["page_height_in"] == 11.0
    assert basis["margin_in"] == 1.0
    assert basis["table_width_dxa"] == 9360
    assert basis["font_pt"] == 9.5
    assert basis["line_spacing"] == 1.15
    assert basis["budget_lines"] == OVERLONG_LINE_BUDGET == 50
    assert basis["budget_lines"] < basis["raw_content_lines_at_font"]
    width = 4680

    def text_for(lines: int) -> str:
        lo, hi = 1, 1
        while estimate_wrapped_lines("界" * hi, width) < lines:
            hi *= 2
        while lo < hi:
            mid = (lo + hi) // 2
            if estimate_wrapped_lines("界" * mid, width) < lines:
                lo = mid + 1
            else:
                hi = mid
        assert estimate_wrapped_lines("界" * lo, width) == lines
        return "界" * lo

    at_budget = text_for(OVERLONG_LINE_BUDGET)
    over_budget = text_for(OVERLONG_LINE_BUDGET + 1)
    assert row_should_cant_split(["短", at_budget], [width, width], header=False) is True
    assert row_should_cant_split(["短", over_budget], [width, width], header=False) is False
    doc = Document()
    table = doc.add_table(rows=2, cols=2)
    _fill_cell(table.cell(0, 0), "项目", header=True)
    _fill_cell(table.cell(0, 1), "说明", header=True)
    _fill_cell(table.cell(1, 0), "超长", header=False)
    _fill_cell(table.cell(1, 1), over_budget, header=False)
    before = table.cell(1, 1).text
    report = apply_table_row_pagination(
        table, values=[["项目", "说明"], ["超长", over_budget]], widths_dxa=[width, width],
    )
    assert report["overlong_rows"] == [1]
    assert row_has_cant_split(table.rows[1]) is False
    assert table.cell(1, 1).text == before
    assert len(table.cell(1, 1).text) == len(over_budget)
    for run in table.cell(1, 1).paragraphs[0].runs:
        assert _run_sz_half_points(run) == 19
        assert run.font.size == Pt(9.5)


def test_repeat_header_caption_and_post_table_spacer_stay_in_contract():
    doc = Document()
    caption = doc.add_paragraph()
    apply_caption_keep_with_table(caption)
    set_run(caption.add_run("题注只贴表头"), size=10.5, bold=True)
    table = doc.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "HDR"
    table.rows[0].cells[1].text = "HDR2"
    table.rows[1].cells[0].text = "R"
    table.rows[1].cells[1].text = "C"
    ensure_repeat_header(table.rows[0])
    headers_before = table.rows[0]._tr.find(qn("w:trPr")).findall(qn("w:tblHeader"))
    apply_table_row_pagination(table, values=[["HDR", "HDR2"], ["R", "C"]], widths_dxa=[4680, 4680])
    headers_after = table.rows[0]._tr.find(qn("w:trPr")).findall(qn("w:tblHeader"))
    assert len(headers_before) == 1
    assert len(headers_after) == 1
    assert row_is_repeat_header(table.rows[0]) is True
    spacer = doc.add_paragraph()
    apply_post_table_spacer(spacer)
    assert paragraph_has_keep_with_next(caption) is True
    assert paragraph_has_keep_with_next(spacer) is False
    assert inspect_table_layout(table)["cell_keep_next"] == 0


def test_render_helper_rejects_preexisting_pdf(tmp_path):
    stale = tmp_path / "old.pdf"
    stale.write_bytes(b"%PDF-1.4 decoy")
    fresh = tmp_path / "new.pdf"
    fresh.write_bytes(b"%PDF-1.4 fresh-bytes")
    assert accept_new_pdf(0, {"old.pdf"}, [stale]) is None
    assert accept_new_pdf(0, {"old.pdf"}, [stale, fresh]) == fresh
    assert accept_new_pdf(1, set(), [fresh]) is None
    assert accept_new_pdf(0, set(), [fresh, stale]) is None
    assert accept_new_pdf(0, set(), []) is None


def test_long_convert_root_does_not_invoke_soffice(tmp_path, monkeypatch):
    monkeypatch.setenv("MMF_R07_LO_SHORT_ROOT", str(tmp_path / ("d" * 90)))
    called = {"n": 0}

    def boom(*_args, **_kwargs):
        called["n"] += 1
        raise AssertionError("soffice must not start")

    monkeypatch.setattr(f"{__name__}._run_soffice", boom)
    doc = Document()
    doc.add_paragraph("x")
    docx = tmp_path / "a.docx"
    doc.save(docx)
    note = _render_pdf(docx, tmp_path / "work")
    assert called["n"] == 0
    assert note["status"] == "NOT_RENDERED"
    assert note["deep_path_invoked"] is False
    assert note["failure"] == "LO_SHORT_ROOT_TOO_LONG"
    assert note["preexisting_pdf_accepted"] is False


def test_render_pdf_identity_requires_new_pdf_and_stable_docx(tmp_path, monkeypatch):
    monkeypatch.setenv("MMF_R07_LO_SHORT_ROOT", str(SHORT_LO_ROOT_DEFAULT))
    monkeypatch.setattr(f"{__name__}._read_pdf_pages", lambda path: ["identity-page"])
    doc = Document()
    doc.add_paragraph("identity")
    docx = tmp_path / "a.docx"
    doc.save(docx)

    def no_pdf(target, outdir, profile):
        assert not any(outdir.iterdir())
        assert not any(profile.iterdir())
        target.write_bytes(target.read_bytes() + b"mutated")

        class Done:
            returncode = 0
            stderr = ""
            stdout = ""

        return Done()

    monkeypatch.setattr(f"{__name__}._run_soffice", no_pdf)
    mutated = _render_pdf(docx, tmp_path / "mut")
    assert mutated["status"] == "NOT_RENDERED"
    assert mutated["failure"] == "DOCX_MUTATED"
    assert mutated["docx_sha_unchanged"] is False

    def empty_success(target, outdir, profile):
        class Done:
            returncode = 0
            stderr = ""
            stdout = ""

        return Done()

    monkeypatch.setattr(f"{__name__}._run_soffice", empty_success)
    missing = _render_pdf(docx, tmp_path / "miss")
    assert missing["status"] == "NOT_RENDERED"
    assert missing["failure"] == "NO_NEW_PDF"
    assert missing["docx_sha_unchanged"] is True
    assert missing["pdf_sha256"] is None

    def one_pdf(target, outdir, profile):
        (outdir / "in.pdf").write_bytes(b"%PDF-1.4\n%fixture\n")

        class Done:
            returncode = 0
            stderr = ""
            stdout = ""

        return Done()

    monkeypatch.setattr(f"{__name__}._run_soffice", one_pdf)
    note = _render_pdf(docx, tmp_path / "ok")
    assert note["status"] == "RENDERED"
    assert note["docx_sha_unchanged"] is True
    assert note["docx_sha256_before"] == note["docx_sha256_after"]
    assert note["pdf_sha256"] == _sha256(b"%PDF-1.4\n%fixture\n")
    assert note["pdf_identity"].endswith(note["pdf_sha256"])
    assert note["outdir_empty_before"] is True
    assert note["profile_created_fresh"] is True
    assert note["deep_path_invoked"] is False
    assert note["native_path_alias_used"] is False
    assert note["preexisting_pdf_accepted"] is False
    assert note["pages"] == ["identity-page"]
    assert Path(note["pdf"]).read_bytes() == b"%PDF-1.4\n%fixture\n"


def test_libreoffice_fixtures_never_default_visual_pass(tmp_path):
    evidence = _lo_cases(tmp_path)
    sink = _lo_work() / "LO_EVIDENCE.json"
    sink.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rendered = [case for case in evidence["cases"] if case.get("render", {}).get("status") == "RENDERED"]
    if not rendered:
        pytest.skip("LibreOffice fixture render did not produce PDF; visual NOT_VERIFIED, see "
                    + str(sink))
    for case in rendered:
        render = case["render"]
        assert render.get("docx_sha_unchanged") is True, case["id"]
        assert render.get("pdf_sha256"), case["id"]
        assert render.get("pdf_identity"), case["id"]
        assert render.get("preexisting_pdf_accepted") is False, case["id"]
        assert render.get("deep_path_invoked") is False, case["id"]
        assert render.get("native_path_alias_used") is False, case["id"]
        assert render.get("outdir_empty_before") is True, case["id"]
        assert render.get("profile_created_fresh") is True, case["id"]
        assert render.get("convert_path") == "short_temp_copy", case["id"]
    by_id = {case["id"]: case for case in evidence["cases"]}
    short = by_id["LO-SHORT-ROW"]
    assert short.get("short_row_unsplit") is True, short
    assert short.get("caption_with_header") is True, short
    glue = by_id["LO-NOT-GLUE"]
    assert glue.get("table_starts_on_first_content_page") is True, glue
    assert glue.get("header_repeats_if_spanned") is True, glue
    assert glue.get("short_row_unsplit") is True, glue
    overlong = by_id["LO-OVERLONG"]
    assert overlong.get("not_truncated") is True, overlong
    merged = by_id["LO-MERGE"]
    assert merged.get("has_merge_text") is True and merged.get("has_neighbors") is True, merged
