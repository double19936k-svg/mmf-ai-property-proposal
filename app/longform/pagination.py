"""Real pagination + cached TOC page refresh (MMF-007C Round 2 / P0-3).

``build_docx.add_native_toc`` writes ``1`` as the cached result of every PAGEREF
field. Word and WPS refresh that on open, so the *printed* directory is fine,
but the file on disk never carried a page number — which means QA could not tell
a real 300-page directory from eleven identical "1"s, and the delivered artifact
looked broken to anyone who inspected it without a word processor.

This module renders the final DOCX once with a local LibreOffice and rewrites
*only* those cached result runs. The native TOC field, its bookmarks, the
hyperlinks and ``updateFields`` stay byte-identical, so WPS/Word still do their
own refresh on open: LibreOffice is used as a measuring stick, never as a
replacement for the WPS-compatible structure.

The same render is the only place in the product that knows how many pages the
document actually occupies, so it also reports per-page visible text: page
estimates elsewhere in the pipeline are arithmetic.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any
from lxml import etree
from filesystem_paths import filesystem_path, native_render_root

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DOCUMENT_PART = "word/document.xml"
PAGINATION_REPORT = "artifact_pagination.json"
# 一整页正文按 11pt 中文约 700-900 字；低于这个零头就算"这页没有内容"。
MIN_PAGE_CONTENT_CHARS = 40
TOC_ANCHOR_PREFIX = "MMF_TOC_"
PAGEREF = re.compile(r"PAGEREF\s+(\S+)", re.I)
# 装订噪声：点线前导、连字、全/半角空白，PDF 与 DOCX 两侧都先抹平再比。
_STRIP = re.compile(r"[\s\u3000．。·、,，.。\-—_/\\|]+")


def locate_renderer() -> str:
    """Local page renderer, or "" when there is none (the run must not fail here)."""
    configured = os.environ.get("MMF_PAGINATION_RENDERER") or os.environ.get("MMF_LIBREOFFICE_PATH")
    if configured and Path(configured).is_file():
        return str(Path(configured))
    candidates = [
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        str(Path.home() / "AppPrograms" / "LibreOffice" / "program" / "soffice.exe"),
    ]
    for item in candidates:
        if Path(item).is_file():
            return str(Path(item))
    return shutil.which("soffice") or shutil.which("libreoffice") or ""


def _normalized(value: str) -> str:
    return _STRIP.sub("", value or "").lower()


def _page_texts(pdf_path: Path) -> tuple[list[str], list[tuple[str, int]]]:
    """Per-page normalized text plus the outline marks LibreOffice derived."""
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    pages = [_normalized(page.extract_text() or "") for page in reader.pages]
    marks: list[tuple[str, int]] = []

    def walk(node) -> None:
        for item in node:
            if isinstance(item, list):
                walk(item)
                continue
            try:
                title = _normalized(str(getattr(item, "title", "") or ""))
                index = reader.get_destination_page_number(item)
            except Exception:
                continue
            if title and 0 <= index < len(pages):
                marks.append((title, index))

    try:
        walk(reader.outline)
    except Exception:
        marks = []
    return pages, marks


def _entry_label(paragraph) -> str:
    link = paragraph.find(f"{W}hyperlink")
    if link is None:
        return ""
    return "".join(node.text or "" for node in link.iter(f"{W}t"))


def _cached_page_node(paragraph, anchor: str):
    """The ``w:t`` holding PAGEREF's cached result, between separate and end."""
    seen_separate = False
    for run in paragraph.findall(f"{W}r"):
        field = run.find(f"{W}fldChar")
        instruction = run.find(f"{W}instrText")
        if instruction is not None and anchor.upper() in (instruction.text or "").upper():
            seen_separate = False
            continue
        if field is not None and field.get(f"{W}fldCharType") == "separate":
            seen_separate = True
            continue
        if seen_separate:
            node = run.find(f"{W}t")
            if node is not None:
                return node
            return None
    return None


def collect_toc_entries(root) -> list[dict[str, Any]]:
    """Visible directory rows in document order: anchor, printed label, cached page node."""
    entries: list[dict[str, Any]] = []
    for paragraph in root.iter(f"{W}p"):
        anchor = ""
        for instruction in paragraph.iter(f"{W}instrText"):
            match = PAGEREF.search(instruction.text or "")
            if match:
                anchor = match.group(1)
                break
        if not anchor.startswith(TOC_ANCHOR_PREFIX):
            continue
        label = _entry_label(paragraph)
        node = _cached_page_node(paragraph, anchor)
        if not label or node is None:
            continue
        entries.append({
            "anchor": anchor,
            "text": label,
            "node": node,
            "cached_page": (node.text or "").strip(),
        })
    return entries


def _directory_pages(page_texts: list[str], needles: list[str]) -> int:
    """First page that can hold body content: everything before it is the TOC itself."""
    hits = 0
    for index, page in enumerate(page_texts):
        present = sum(1 for needle in needles if needle and needle in page)
        if present >= 3 or ("目录" in page and present >= 1):
            hits = index + 1
    return hits


def resolve_pages(entries: list[dict[str, Any]], pages: list[str],
                  marks: list[tuple[str, int]] | None = None) -> dict[str, Any]:
    """Map each directory entry to the page its heading actually lands on.

    LibreOffice turns the DOCX heading styles into PDF outline marks, and those
    carry the heading text in reading order. Page text is the fallback only: a
    justified Chinese heading comes out of most extractors re-ordered by column,
    so matching a title inside a page's characters is not trustworthy.
    """
    marks = [(_normalized(title), index) for title, index in (marks or []) if _normalized(title)]
    needles = [_normalized(row["text"]) for row in entries]
    start = _directory_pages(pages, [value for value in needles if value])
    previous_page = 0
    previous_mark = 0
    resolved = 0
    for row, needle in zip(entries, needles):
        page = 0
        if needle:
            match = _match_mark(needle, marks, previous_mark)
            if match:
                title, index = match
                page = index + 1
                previous_mark = index
            else:
                for index in range(start, len(pages)):
                    if needle in pages[index]:
                        page = index + 1
                        break
        if page and page >= previous_page:
            row["rendered_page"] = page
            previous_page = page
            resolved += 1
        else:
            row["rendered_page"] = 0
    return {
        "body_start_page": start,
        "outline_mark_count": len(marks),
        "resolved": resolved,
        "unresolved": len(entries) - resolved,
    }


def _match_mark(needle: str, marks: list[tuple[str, int]], not_before: int) -> tuple[str, int] | None:
    for title, index in marks:
        if index < not_before:
            continue
        if title == needle or (len(needle) >= 4 and (title.startswith(needle) or needle.startswith(title))):
            return title, index
    return None


def render_document(docx_path: Path, *, timeout: int = 600, diagnostics: dict[str, Any] | None = None
                    ) -> tuple[Path, Path, list[str], list[tuple[str, int]]] | None:
    """Real layout pass: (pdf, working dir, per-page text, outline marks) or None."""
    note = diagnostics if diagnostics is not None else {}
    renderer = locate_renderer()
    note["renderer"] = renderer
    if not renderer:
        note["failure"] = "NO_RENDERER"
        return None
    work = Path(tempfile.mkdtemp(prefix="mmfpag"))
    # Render a copy under a fixed short basename. A native alias, when needed,
    # points to this same physical directory; it never relocates evidence.
    target = work / "input.pdf"
    env = os.environ.copy()
    # 开发运行时是 Anaconda：PYTHONHOME/PYTHONPATH 会让 LibreOffice 内嵌的脚本
    # 解释器找不到自己的标准库，转换直接半途而废。
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONEXECUTABLE"):
        env.pop(key, None)
    try:
        shutil.copy2(filesystem_path(docx_path), work / "input.docx")
        with native_render_root(work) as native_work:
            note["render_work_directory"] = str(work)
            note["native_path_alias_used"] = native_work != work
            # Drive letters can be reused after a render. A unique profile URI
            # prevents LibreOffice IPC from joining an earlier alias's session.
            profile_uri = (native_work / ("p_" + uuid.uuid4().hex)).as_uri()
            note["native_render_profile_uri"] = profile_uri
            command = [
                renderer, "--headless", "--norestore", "--invisible",
                f"-env:UserInstallation={profile_uri}",
                "--convert-to", "pdf", "--outdir", str(native_work),
                str(native_work / "input.docx"),
            ]
            done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout, shell=False, env=env)
        note["native_path_alias_released"] = True
        note["returncode"] = done.returncode
        note["stderr_tail"] = (done.stderr or "")[-300:]
        note["stdout_tail"] = (done.stdout or "")[-300:]
    except (OSError, subprocess.SubprocessError) as exc:
        note["failure"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        shutil.rmtree(work, ignore_errors=True)
        return None
    produced = sorted(work.glob("*.pdf"))
    if not target.is_file() and produced:
        target = produced[0]
    if done.returncode != 0 or not target.is_file():
        note["failure"] = "NO_PDF" if done.returncode == 0 else "RENDERER_EXIT"
        shutil.rmtree(work, ignore_errors=True)
        return None
    try:
        pages, marks = _page_texts(target)
    except Exception as exc:
        note["failure"] = f"PypdfRead: {str(exc)[:200]}"
        shutil.rmtree(work, ignore_errors=True)
        return None
    return target, work, pages, marks


def _rewrite_document_xml(docx_path: Path, payload: bytes) -> None:
    handle = tempfile.NamedTemporaryFile(
        "wb", dir=str(docx_path.parent), prefix=".pag", suffix=".docx", delete=False)
    temporary = Path(handle.name)
    handle.close()
    try:
        with zipfile.ZipFile(docx_path) as source, zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as sink:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename == DOCUMENT_PART:
                    data = payload
                sink.writestr(item, data)
        os.replace(filesystem_path(temporary), filesystem_path(docx_path))
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def page_density(pages: list[str]) -> dict[str, Any]:
    """Visible characters per real page — the only honest basis for page QA.

    §29：页数不能只数"翻过去了几页"。`effective_content_pages` 扣掉整页几乎没有字
    的页（多余的换页符、表格溢出到下一页只剩边框），这一层测的是"页面上有没有东西"；
    表格里那一片空格由 table_qa 逐表负责 —— 两把尺子各管一段，不互相冒充。
    """
    counts = [len(value) for value in pages]
    thin = sum(1 for value in counts if value < MIN_PAGE_CONTENT_CHARS)
    return {
        "rendered_pages": len(counts),
        "visible_chars": sum(counts),
        "page_char_counts": counts,
        "thin_page_count": thin,
        "effective_content_pages": len(counts) - thin,
        "meaningful_page_ratio": round((len(counts) - thin) / len(counts), 4) if counts else 0.0,
    }


def _technical_page_report(pages: list[str], marks: list[tuple[str, int]] | None) -> dict[str, Any]:
    """§13：交付件除了"翻过去几页"，还必须报出技术有效页与六类页数。

    与初稿门控用的是同一把尺子（planning.page_calibration），区别只是这里量的是
    最终 DOCX。分类失败只少报这一层口径，绝不让分页刷新本身失败 —— 目录页码刷新
    比页数分类更重要。
    """
    try:
        from planning.page_calibration import calibrate_ruler, classify_pages

        marked = [int(index) + 1 for _title, index in (marks or []) if int(index) > 0]
        body_start = min(marked) if marked else 3
        classification = classify_pages(pages, body_start_page=body_start)
        ruler = calibrate_ruler(classification, visible_chars=classification["visible_chars"],
                                technical_chars=classification["technical_chars"])
    except Exception as exc:
        return {"page_classification_status": "CLASSIFICATION_FAILED",
                "page_classification_error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return {
        "page_classification_status": "MEASURED",
        "physical_pages": int(classification["physical_pages"]),
        "body_start_page": body_start,
        "page_classes": classification["counts"],
        "layout_basis": classification["layout_basis"],
        "technical_page_count_bound": classification["technical_page_count_bound"],
        "technical_effective_pages": int(classification["technical_effective_pages"]),
        "narrative_technical_pages": int(classification["narrative_technical_pages"]),
        "structured_technical_pages": int(classification["structured_technical_pages"]),
        "front_matter_pages": int(classification["front_matter_pages"]),
        "blank_pages": int(classification["blank_pages"]),
        "placeholder_pages": int(classification["placeholder_pages"]),
        "placeholder_page_ratio": float(classification["placeholder_page_ratio"]),
        "excluded_scope_pages": int(classification["excluded_scope_pages"]),
        "excluded_scope_group_hits": classification["excluded_scope_group_hits"],
        "credential_attachment_pages_present": int(classification["credential_attachment_pages_present"]),
        "credential_attachment_pages": int(classification["credential_attachment_pages"]),
        "credential_attachment_page_details": list(classification["credential_attachment_page_details"]),
        "non_technical_page_ratio": float(classification["non_technical_page_ratio"]),
        "chars_per_technical_page": ruler["chars_per_technical_page"],
        "page_basis": ruler["basis"],
    }


def refresh_artifact_pagination(docx_path: Path, *, run_dir: Path | None = None,
                                timeout: int = 600) -> dict[str, Any]:
    """Render, map and rewrite. Never raises: a missing renderer is a fact to report."""
    docx_path = Path(docx_path)
    report: dict[str, Any] = {
        "schema_version": "artifact-pagination-v0.1",
        "status": "SKIPPED",
        "artifact_path": str(docx_path),
        "renderer": locate_renderer(),
        "rendered_pages": 0,
        "visible_chars": 0,
        "thin_page_count": 0,
        "effective_content_pages": 0,
        "meaningful_page_ratio": 0.0,
        "toc_entry_count": 0,
        "toc_pages_resolved": 0,
        "toc_pages_refreshed": 0,
        "toc_pages_verified": 0,
        "toc_pages_unresolved": 0,
        "toc_body_start_page": 0,
        "toc_outline_mark_count": 0,
        "document_xml_rewritten": False,
        "identical_page_share": 0.0,
        # §13：没量到就显式留 None/NOT_MEASURED，不能靠"字段不存在"被下游读成 0 页。
        "page_classification_status": "NOT_MEASURED",
        "physical_pages": 0,
        "technical_effective_pages": None,
        "placeholder_page_ratio": None,
        "excluded_scope_pages": None,
    }
    if not docx_path.is_file():
        report["status"] = "SKIPPED_NO_ARTIFACT"
        return report
    if not report["renderer"]:
        report["status"] = "SKIPPED_NO_RENDERER"
        report["note"] = "本机没有可用分页渲染器，目录页码仍是构建期的缓存值"
        return report
    rendered = render_document(docx_path, timeout=timeout, diagnostics=report)
    if rendered is None:
        report["status"] = "RENDER_FAILED"
        return report
    pdf_path, work, pages, marks = rendered
    try:
        report.update(page_density(pages))
        report.update(_technical_page_report(pages, marks))
        with zipfile.ZipFile(docx_path) as archive:
            root = etree.fromstring(archive.read(DOCUMENT_PART))
        entries = collect_toc_entries(root)
        report["toc_entry_count"] = len(entries)
        if not entries:
            report["status"] = "NO_NATIVE_TOC"
            return report
        mapping = resolve_pages(entries, pages, marks)
        # 显式命名而不是前缀拼接：resolve_pages 的 "resolved"/"unresolved" 拼上前缀
        # 会得到 toc_resolved，与本文件声明的 toc_pages_unresolved 不是同一个键，
        # 于是"没有映射到页码的行"永远读不出来。
        report.update({
            "toc_body_start_page": mapping["body_start_page"],
            "toc_outline_mark_count": mapping["outline_mark_count"],
            "toc_pages_resolved": mapping["resolved"],
            "toc_pages_unresolved": mapping["unresolved"],
        })
        refreshed = 0
        for row in entries:
            if not row["rendered_page"]:
                continue
            row["node"].text = str(row["rendered_page"])
            if str(row["rendered_page"]) != row["cached_page"]:
                refreshed += 1
        displayed = [str(row["node"].text or "").strip() for row in entries]
        report["displayed_pages"] = displayed
        report["toc_entry_pages"] = [
            {"text": row["text"], "rendered_page": row["rendered_page"],
             "basis": "RENDERED_HEADING" if row["rendered_page"] else "NOT_MEASURED"}
            for row in entries]
        report["identical_page_share"] = _share(displayed)
        if mapping["resolved"]:
            payload = etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True)
            _rewrite_document_xml(docx_path, payload)
            report["document_xml_rewritten"] = True
            # Word/WPS 打开时不再只能靠 dirty 才看得见页码；updateFields 原样保留。
            reopened = zipfile.ZipFile(docx_path)
            try:
                rewritten = collect_toc_entries(etree.fromstring(reopened.read(DOCUMENT_PART)))
            finally:
                reopened.close()
            report["toc_pages_verified"] = sum(
                1 for row, source in zip(rewritten, entries)
                if source["rendered_page"] and row["cached_page"] == str(source["rendered_page"]))
        report["toc_pages_refreshed"] = refreshed
        if refreshed and not mapping["unresolved"]:
            report["status"] = "REFRESHED"
        elif refreshed or mapping["resolved"]:
            report["status"] = "PARTIAL"
        else:
            report["status"] = "NO_PAGE_MAPPING"
        return report
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if run_dir is not None:
            try:
                from .hierarchical import atomic_write_json
                atomic_write_json(Path(run_dir) / PAGINATION_REPORT, report)
            except Exception:
                pass


def _share(values: list[str]) -> float:
    filled = [value for value in values if value]
    if not filled:
        return 0.0
    counts: dict[str, int] = {}
    for value in filled:
        counts[value] = counts.get(value, 0) + 1
    return round(max(counts.values()) / len(filled), 4)


def refresh_with_retry(docx_path: Path, *, run_dir: Path | None = None, attempts: int = 2,
                       timeout: int = 600) -> dict[str, Any]:
    """A headless renderer can lose a race with a running office suite."""
    report: dict[str, Any] = {}
    for _ in range(max(1, int(attempts))):
        report = refresh_artifact_pagination(docx_path, run_dir=run_dir, timeout=timeout)
        if report.get("status") != "RENDER_FAILED":
            return report
    return report
