from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, Iterable

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from pypdf import PdfReader

from .models import (
    MEDIA_TYPES,
    SUPPORTED_EXTENSIONS,
    TenderError,
    append_jsonl,
    ensure_within,
    now_iso,
    safe_filename,
    sha256_bytes,
    validate_extraction,
    write_json,
)


MAX_FILES_PER_RUN = 3
MAX_FILE_SIZE = 40 * 1024 * 1024
MAX_TOTAL_UPLOAD = 80 * 1024 * 1024
HEADING_RE = re.compile(r"^(?:第[一二三四五六七八九十百]+[章节部分]|\d+(?:\.\d+){0,3}[、.\s]|#{1,6}\s+)(.+)$")


def _media_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise TenderError("EXTRACT_UNSUPPORTED_TYPE", f"暂不支持{ext or '无扩展名'}文件。")
    return MEDIA_TYPES[ext]


def save_uploads(app_root: Path, run_dir: Path, files: Iterable[tuple[str, bytes]]) -> list[dict[str, Any]]:
    app_root = app_root.resolve()
    run_dir = ensure_within(run_dir, app_root)
    rows = list(files)
    if not rows or len(rows) > MAX_FILES_PER_RUN:
        raise TenderError("EXTRACT_IO", f"每个任务需上传1-{MAX_FILES_PER_RUN}个文件。")
    total = sum(len(data or b"") for _, data in rows)
    if total > MAX_TOTAL_UPLOAD:
        raise TenderError("EXTRACT_IO", "一次上传总大小不能超过80MB。")
    originals = ensure_within(run_dir / "tender" / "originals", app_root)
    originals.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for index, (raw_name, data) in enumerate(rows, 1):
        name = safe_filename(raw_name)
        media_type = _media_type(name)
        if not data:
            raise TenderError("EXTRACT_EMPTY_BODY", f"{name}为空文件。")
        if len(data) > MAX_FILE_SIZE:
            raise TenderError("EXTRACT_IO", f"{name}超过40MB限制。")
        stored_name = name
        if (originals / stored_name).exists():
            stored_name = f"{Path(name).stem}_{index}{Path(name).suffix}"
        target = ensure_within(originals / stored_name, app_root)
        target.write_bytes(data)
        record = {
            "file_id": f"FILE-{index:03d}",
            "original_filename": name,
            "stored_relative_path": target.relative_to(app_root).as_posix(),
            "media_type": media_type,
            "sha256": sha256_bytes(data),
            "file_size": len(data),
            "upload_timestamp": now_iso(),
        }
        records.append(record)
        append_jsonl(run_dir / "tender" / "access_audit.jsonl", {
            "at": now_iso(), "operation": "upload", "run_id": run_dir.name,
            "file_id": record["file_id"], "stored_relative_path": record["stored_relative_path"],
        })
    write_json(run_dir / "tender" / "uploads.json", {"run_id": run_dir.name, "files": records})
    return records


def _outline_from_heading(outline: list[dict[str, Any]], file_id: str, title: str, depth: int, parent_id: str | None) -> str:
    node_id = f"OUT-{len(outline)+1:04d}"
    outline.append({"outline_node_id": node_id, "file_id": file_id, "title": title[:200], "depth": depth, "source_span": None, "parent_id": parent_id})
    return node_id


def _pdf_extract(path: Path, file_meta: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise TenderError("EXTRACT_IO", f"PDF读取失败：{path.name}") from exc
    if reader.is_encrypted:
        raise TenderError("EXTRACT_ENCRYPTED_PDF", f"PDF已加密：{path.name}")
    char_total = 0
    scan_pages: list[int] = []
    current_outline: str | None = None
    for page_index, page in enumerate(reader.pages, 1):
        try:
            text = (page.extract_text() or "").replace("\x00", "").strip()
        except Exception as exc:
            raise TenderError("EXTRACT_IO", f"PDF第{page_index}页读取失败。") from exc
        char_count = len(text)
        char_total += char_count
        likely_scan = char_count < 40 and page_index != 1
        if likely_scan:
            scan_pages.append(page_index)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in lines:
            match = HEADING_RE.match(line)
            if match and len(line) <= 100:
                current_outline = _outline_from_heading(output["outline"], file_meta["file_id"], line, 1, None)
            para_index = len(output["paragraphs"])
            output["paragraphs"].append({
                "para_id": f"PAR-{para_index+1:06d}", "file_id": file_meta["file_id"], "page_no": page_index,
                "outline_node_id": current_outline, "heading_level": 1 if match else None,
                "heading_path": [line] if match else [], "text": line, "paragraph_index": para_index,
            })
        output["pages"].append({
            "file_id": file_meta["file_id"], "page_no": page_index, "text": text,
            "char_count": char_count, "likely_scan": likely_scan, "outline_node_id": current_outline,
        })
    if char_total == 0:
        raise TenderError("EXTRACT_NO_TEXT_LAYER", f"PDF没有可用文本层：{path.name}")
    return {"page_count": len(reader.pages), "char_count": char_total, "likely_scan_pages": scan_pages, "text_layer_ratio": round(1 - len(scan_pages) / max(1, len(reader.pages)), 4)}


def _iter_docx_blocks(document: Document):
    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, document)
        elif isinstance(child, CT_Tbl):
            yield Table(child, document)


def _docx_extract(path: Path, file_meta: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    try:
        document = Document(str(path))
    except Exception as exc:
        raise TenderError("EXTRACT_IO", f"DOCX读取失败：{path.name}") from exc
    heading_stack: list[str] = []
    current_outline: str | None = None
    paragraph_index = 0
    table_index = 0
    char_total = 0
    for block in _iter_docx_blocks(document):
        if isinstance(block, Paragraph):
            text = block.text.replace("\x00", "").strip()
            if not text:
                paragraph_index += 1
                continue
            style_name = str(block.style.name or "")
            heading_level = None
            match = re.search(r"(\d+)$", style_name) if style_name.lower().startswith("heading") else None
            if match:
                heading_level = max(1, min(6, int(match.group(1))))
                heading_stack = heading_stack[: heading_level - 1] + [text]
                current_outline = _outline_from_heading(output["outline"], file_meta["file_id"], text, heading_level, None)
            char_total += len(text)
            output["paragraphs"].append({
                "para_id": f"PAR-{len(output['paragraphs'])+1:06d}", "file_id": file_meta["file_id"],
                "page_no": None, "source_page": None, "outline_node_id": current_outline,
                "heading_level": heading_level, "heading_path": list(heading_stack), "text": text,
                "paragraph_index": paragraph_index, "table_index": None,
            })
            paragraph_index += 1
        else:
            rows = [[cell.text.strip() for cell in row.cells] for row in block.rows]
            columns = rows[0] if rows else []
            char_total += sum(len(cell) for row in rows for cell in row)
            output["tables"].append({
                "table_id": f"TAB-{len(output['tables'])+1:05d}", "file_id": file_meta["file_id"],
                "page_no": None, "source_page": None, "heading_path": list(heading_stack),
                "paragraph_index": None, "table_index": table_index, "columns": columns,
                "rows": rows[1:] if rows else [], "is_scoring_table": any(any(term in cell for term in ("评分", "分值", "权重", "得分")) for row in rows for cell in row),
                "extraction_quality": "structured",
            })
            table_index += 1
    if char_total == 0:
        raise TenderError("EXTRACT_EMPTY_BODY", f"DOCX没有可抽取正文：{path.name}")
    return {"page_count": 0, "char_count": char_total, "likely_scan_pages": [], "text_layer_ratio": 1.0}


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise TenderError("EXTRACT_IO", "文本编码无法识别。")


def _text_extract(path: Path, file_meta: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    text = _decode_text(path.read_bytes()).replace("\x00", "").strip()
    if not text:
        raise TenderError("EXTRACT_EMPTY_BODY", f"文本文件为空：{path.name}")
    heading_stack: list[str] = []
    current_outline: str | None = None
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    for index, block in enumerate(blocks):
        first = block.splitlines()[0].strip()
        match = HEADING_RE.match(first)
        if match and len(first) <= 120:
            depth = len(first) - len(first.lstrip("#")) or 1
            heading_stack = heading_stack[: depth - 1] + [first.lstrip("# ")]
            current_outline = _outline_from_heading(output["outline"], file_meta["file_id"], first.lstrip("# "), depth, None)
        output["paragraphs"].append({
            "para_id": f"PAR-{len(output['paragraphs'])+1:06d}", "file_id": file_meta["file_id"],
            "page_no": None, "source_page": None, "outline_node_id": current_outline,
            "heading_level": 1 if match else None, "heading_path": list(heading_stack), "text": block,
            "paragraph_index": index, "block_index": index, "table_index": None,
        })
    return {"page_count": 0, "char_count": len(text), "likely_scan_pages": [], "text_layer_ratio": 1.0}


def extract_run(app_root: Path, run_dir: Path) -> dict[str, Any]:
    app_root = app_root.resolve()
    run_dir = ensure_within(run_dir, app_root)
    uploads_path = run_dir / "tender" / "uploads.json"
    if not uploads_path.is_file():
        raise TenderError("EXTRACT_IO", "尚未上传文件。")
    import json
    upload_data = json.loads(uploads_path.read_text(encoding="utf-8-sig"))
    output: dict[str, Any] = {
        "schema_version": "tender-extraction-v0.1", "extraction_id": f"EXT-{uuid.uuid4().hex[:12]}",
        "run_id": run_dir.name, "files": [], "outline": [], "pages": [], "paragraphs": [], "tables": [],
        "warnings": [], "processing_mode": "NORMAL", "created_at": now_iso(),
    }
    max_pages = 0
    for file_meta in upload_data.get("files", []):
        path = ensure_within(app_root / file_meta["stored_relative_path"], app_root)
        if not path.is_file():
            raise TenderError("EXTRACT_IO", f"上传文件不存在：{file_meta['original_filename']}")
        try:
            ext = path.suffix.lower()
            if ext == ".pdf":
                stats = _pdf_extract(path, file_meta, output)
            elif ext == ".docx":
                stats = _docx_extract(path, file_meta, output)
            else:
                stats = _text_extract(path, file_meta, output)
        except TenderError:
            raise
        except OSError as exc:
            raise TenderError("EXTRACT_IO", f"文件读取失败：{path.name}") from exc
        max_pages = max(max_pages, int(stats["page_count"]))
        output["files"].append({**file_meta, **stats})
        for page_no in stats["likely_scan_pages"]:
            output["warnings"].append({"code": "SCAN_PAGE", "file_id": file_meta["file_id"], "page_no": page_no, "message": "页面疑似无文本层，本轮不执行OCR，也不送入AI理解。"})
    if max_pages > 200:
        output["processing_mode"] = "PARTIAL_DEGRADED"
        output["warnings"].append({"code": "EXTRACTION_DEGRADED", "message": "文档超过200页，继续抽取并分块理解；本轮标记为实验性降级。"})
    elif max_pages > 80:
        output["processing_mode"] = "CHUNKED"
    if any(row.get("likely_scan_pages") for row in output["files"]):
        ratio = sum(len(row.get("likely_scan_pages", [])) for row in output["files"]) / max(1, sum(row.get("page_count", 0) for row in output["files"]))
        if ratio > 0.3:
            output["processing_mode"] = "PARTIAL_DEGRADED"
    validate_extraction(output)
    write_json(run_dir / "tender" / "extraction.json", output)
    append_jsonl(run_dir / "tender" / "access_audit.jsonl", {"at": now_iso(), "operation": "extract", "run_id": run_dir.name, "files": len(output["files"]), "provider_invoked": False})
    return output
