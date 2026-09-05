from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .models import TenderError, now_iso, write_json
from .pack_builder import build_requirement_pack, candidates_from_extraction


PROVIDER_REQUIRED_KEYS = {
    "chunk_id",
    "proposed_items",
    "proposed_conflicts",
    "proposed_boilerplate",
    "proposed_scoring_items",
    "chunk_missing_facts",
}
PROVIDER_SCHEMA = {
    "type": "object",
    "required": sorted(PROVIDER_REQUIRED_KEYS),
    "properties": {
        "chunk_id": {"type": "string"},
        "proposed_items": {"type": "array", "items": {"type": "object"}},
        "proposed_conflicts": {"type": "array", "items": {"type": "object"}},
        "proposed_boilerplate": {"type": "array", "items": {"type": "object"}},
        "proposed_scoring_items": {"type": "array", "items": {"type": "object"}},
        "chunk_missing_facts": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}


def _page_scan_set(extraction: dict[str, Any]) -> set[tuple[str, int]]:
    return {(row["file_id"], int(row["page_no"])) for row in extraction.get("pages", []) if row.get("likely_scan")}


def _source_id(row: dict[str, Any]) -> str:
    return str(row.get("para_id") or row.get("table_id") or f"SRC-{row.get('file_id')}-{row.get('paragraph_index')}-{row.get('table_index')}")


def _blocks(extraction: dict[str, Any]) -> list[dict[str, Any]]:
    scan = _page_scan_set(extraction)
    files = {row["file_id"]: row for row in extraction["files"]}
    blocks: list[dict[str, Any]] = []
    for row in extraction.get("paragraphs", []):
        if row.get("page_no") is not None and (row["file_id"], int(row["page_no"])) in scan:
            continue
        blocks.append({
            "source_id": _source_id(row), "file_id": row["file_id"], "source_file": files[row["file_id"]]["original_filename"],
            "source_page": row.get("page_no"), "heading_path": row.get("heading_path") or [],
            "paragraph_index": row.get("paragraph_index"), "table_index": None, "para_id": row.get("para_id"),
            "table_id": None, "source_section": " / ".join(row.get("heading_path") or []) or None,
            "source_paragraph_or_table": f"paragraph:{row.get('paragraph_index')}", "outline_node_id": row.get("outline_node_id"),
            "text": row.get("text", ""), "kind": "paragraph",
        })
    for row in extraction.get("tables", []):
        text = "\n".join(" | ".join(str(cell) for cell in table_row) for table_row in ([row.get("columns", [])] + row.get("rows", [])))
        blocks.append({
            "source_id": _source_id(row), "file_id": row["file_id"], "source_file": files[row["file_id"]]["original_filename"],
            "source_page": row.get("page_no"), "heading_path": row.get("heading_path") or [],
            "paragraph_index": None, "table_index": row.get("table_index"), "para_id": None,
            "table_id": row.get("table_id"), "source_section": " / ".join(row.get("heading_path") or []) or None,
            "source_paragraph_or_table": f"table:{row.get('table_index')}", "outline_node_id": row.get("outline_node_id"),
            "text": text, "kind": "table", "is_scoring_table": bool(row.get("is_scoring_table")),
        })
    return blocks


def build_chunks(extraction: dict[str, Any], max_chars: int = 12000) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    current_file = None
    first_page = None
    last_page = None
    for block in _blocks(extraction):
        size = len(block["text"])
        file_changed = current_file is not None and block["file_id"] != current_file
        page_span = block.get("source_page") is not None and first_page is not None and int(block["source_page"]) - int(first_page) >= 12
        if current and (current_chars + size > max_chars or file_changed or page_span):
            chunks.append({"chunk_id": f"CHK-{len(chunks)+1:04d}", "blocks": current, "char_count": current_chars, "page_start": first_page, "page_end": last_page})
            current, current_chars, first_page, last_page = [], 0, None, None
        current.append(block)
        current_chars += size
        current_file = block["file_id"]
        if block.get("source_page") is not None:
            first_page = block["source_page"] if first_page is None else min(first_page, block["source_page"])
            last_page = block["source_page"] if last_page is None else max(last_page, block["source_page"])
    if current:
        chunks.append({"chunk_id": f"CHK-{len(chunks)+1:04d}", "blocks": current, "char_count": current_chars, "page_start": first_page, "page_end": last_page})
    return chunks


def _locator(block: dict[str, Any], excerpt: str | None = None) -> dict[str, Any]:
    return {
        "file_id": block["file_id"], "source_file": block["source_file"], "source_page": block.get("source_page"),
        "source_section": block.get("source_section"), "source_paragraph_or_table": block["source_paragraph_or_table"],
        "outline_node_id": block.get("outline_node_id"), "para_id": block.get("para_id"), "table_id": block.get("table_id"),
        "paragraph_index": block.get("paragraph_index"), "table_index": block.get("table_index"),
        "heading_path": block.get("heading_path") or [], "source_excerpt": (excerpt or block["text"])[:500],
    }


def _mock_payload(extraction: dict[str, Any], chunk: dict[str, Any]) -> dict[str, Any]:
    allowed = {block["source_id"]: block for block in chunk["blocks"]}
    candidates = []
    all_candidates = candidates_from_extraction(extraction)
    for row in all_candidates:
        locator = row.get("source_locator", {})
        source_id = str(locator.get("para_id") or locator.get("table_id") or "")
        if source_id not in allowed:
            continue
        candidates.append({
            "normalized_requirement": row["normalized_requirement"], "requirement_type": row["requirement_type"],
            "mandatory_level_guess": row["mandatory_level_guess"], "classification_guess": row["classification_guess"],
            "source_id": source_id, "source_excerpt": row["source_excerpt"], "confidence": row.get("confidence", 0.85),
        })
    return {"chunk_id": chunk["chunk_id"], "proposed_items": candidates, "proposed_conflicts": [], "proposed_boilerplate": [], "proposed_scoring_items": [], "chunk_missing_facts": []}


def _prompt(chunk: dict[str, Any]) -> str:
    public_blocks = [{"id": row["source_id"], "kind": row["kind"], "text": row["text"]} for row in chunk["blocks"]]
    return f"""你是招标/服务需求文件的Requirement Understanding引擎，只提出候选，不拥有最终Pack权威。

任务要求：
1. 逐块识别项目事实、服务范围、MUST/SHOULD/INFO要求、人员、服务时间、SLA/KPI、评分项、排除范围和疑似模板。
2. 每个proposed_item必须引用给定id作为source_id；不得编造文件名、页码、项目事实或来源。
3. 数字不是PROJECT_SPECIFIC充分证据；只有明确当前项目实体、指代、合同范围或稳定项目上下文时才可建议PROJECT_SPECIFIC。
4. 明确“必须/不得/应至少/否则否决”不得降为INFO。
5. 不删除重复项、不自动解决冲突、不自动删除疑似模板；本地Pack Builder将负责ID、去重、冲突与最终分类。
6. 只返回一个JSON对象，根字段必须且只能为chunk_id、proposed_items、proposed_conflicts、proposed_boilerplate、proposed_scoring_items、chunk_missing_facts。
7. proposed_items每项至少含normalized_requirement、requirement_type、mandatory_level_guess、classification_guess、source_id、source_excerpt、confidence。

chunk_id={chunk['chunk_id']}
blocks={json.dumps(public_blocks, ensure_ascii=False, separators=(",", ":"))}
"""


def _normalize_payload(payload: dict[str, Any], chunk: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not PROVIDER_REQUIRED_KEYS <= set(payload):
        raise TenderError("UNDERSTAND_SCHEMA_INVALID", "Provider Understanding结果结构缺失。")
    for key in ("proposed_items", "proposed_conflicts", "proposed_boilerplate", "proposed_scoring_items", "chunk_missing_facts"):
        if not isinstance(payload[key], list):
            raise TenderError("UNDERSTAND_SCHEMA_INVALID", f"Provider字段{key}必须是数组。")
    allowed = {row["source_id"]: row for row in chunk["blocks"]}
    items: list[dict[str, Any]] = []
    for candidate in payload["proposed_items"]:
        if not isinstance(candidate, dict):
            continue
        source_id = str(candidate.get("source_id", ""))
        block = allowed.get(source_id)
        text = str(candidate.get("normalized_requirement", "")).strip()
        if block is None or len(text) < 4:
            continue
        excerpt = str(candidate.get("source_excerpt", "")).strip()
        if not excerpt or excerpt not in block["text"]:
            excerpt = block["text"][:500]
        items.append({
            "normalized_requirement": text, "requirement_type": str(candidate.get("requirement_type", "other")),
            "mandatory_level_guess": str(candidate.get("mandatory_level_guess", "UNKNOWN")),
            "classification_guess": str(candidate.get("classification_guess", "GENERIC_REQUIREMENT")),
            "source_excerpt": excerpt, "source_locator": _locator(block, excerpt),
            "confidence": candidate.get("confidence", 0.7),
        })
    scoring: list[dict[str, Any]] = []
    for candidate in payload["proposed_scoring_items"]:
        if not isinstance(candidate, dict):
            continue
        source_id = str(candidate.get("source_id", ""))
        block = allowed.get(source_id)
        if block is None:
            continue
        scoring.append({**candidate, "source": _locator(block, str(candidate.get("source_excerpt") or block["text"][:500]))})
    return items, scoring


def engine_label(provider: Any) -> str:
    cfg = getattr(provider, "config", {}) or {}
    name = str(cfg.get("provider_name") or "")
    display = str(cfg.get("display_name") or "")
    blob = f"{name} {display}".lower()
    if "qwen" in blob or "千问" in display:
        return "千问"
    if "grok" in blob:
        return "Grok"
    if "kimi" in blob:
        return "Kimi"
    if "mock" in blob:
        return "测试引擎"
    short = display.split("（")[0].strip()
    return short or "当前引擎"


def understand_run(run_dir: Path, extraction: dict[str, Any], provider: Any, resume: bool = True) -> dict[str, Any]:
    tender_dir = run_dir / "tender"
    chunks_dir = tender_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    chunks = build_chunks(extraction)
    status_path = tender_dir / "status.json"
    status = {
        "schema_version": "mmf006a-status-v0.1", "run_id": run_dir.name, "upload_completed": True,
        "extraction_completed": True, "chunks_total": len(chunks), "chunks_completed": 0, "failed_chunk": None,
        "pack_status": "understanding", "stage": "A4_PROVIDER_UNDERSTANDING", "updated_at": now_iso(),
    }
    write_json(status_path, status)
    all_items: list[dict[str, Any]] = []
    all_scoring: list[dict[str, Any]] = []
    for chunk in chunks:
        chunk_dir = chunks_dir / chunk["chunk_id"]
        normalized_path = chunk_dir / "normalized.json"
        if resume and normalized_path.is_file():
            normalized = json.loads(normalized_path.read_text(encoding="utf-8-sig"))
            all_items.extend(normalized.get("proposed_items", []))
            all_scoring.extend(normalized.get("proposed_scoring_items", []))
            status["chunks_completed"] += 1
            continue
        chunk_dir.mkdir(parents=True, exist_ok=True)
        write_json(chunk_dir / "input.json", {"chunk_id": chunk["chunk_id"], "blocks": chunk["blocks"]})
        request = {
            "task_id": f"{run_dir.name}-tender-{chunk['chunk_id']}", "purpose": "MMF-006A Tender Requirement Understanding",
            "system_prompt": "你是只读的招标需求理解引擎。只处理提示中的Extraction Chunk，只输出规定JSON，不读文件、不联网、不调用工具。",
            "prompt": _prompt(chunk), "required_keys": sorted(PROVIDER_REQUIRED_KEYS), "json_schema": PROVIDER_SCHEMA,
            "chunk_id": chunk["chunk_id"],
            "mock_response": _mock_payload(extraction, chunk), "agent_max_turns": 1,
            "reasoning_effort": "low",
        }
        started_at = now_iso()
        engine = engine_label(provider)
        status["understanding_started_at"] = status.get("understanding_started_at") or started_at
        status["engine_label"] = engine
        status["message"] = f"正在调用{engine}识别需求。大文件可能需要几分钟，请勿刷新。"
        status["updated_at"] = started_at
        write_json(status_path, status)
        write_json(chunk_dir / "status.json", {"chunk_id": chunk["chunk_id"], "status": "RUNNING", "started_at": started_at})
        stop = threading.Event()
        invoke_started = time.monotonic()

        def _heartbeat() -> None:
            while not stop.wait(5):
                elapsed = int(time.monotonic() - invoke_started)
                status["elapsed_seconds"] = elapsed
                status["updated_at"] = now_iso()
                status["engine_label"] = engine
                status["message"] = f"正在调用{engine}识别需求，已用时{elapsed // 60}分{elapsed % 60}秒。请继续等待。"
                write_json(status_path, status)

        heartbeat = threading.Thread(target=_heartbeat, daemon=True)
        heartbeat.start()
        try:
            payload = provider.invoke_structured(request, chunk_dir / "provider_call")
            provider_metadata = payload.pop("provider_metadata", {})
            write_json(chunk_dir / "raw.json", {"payload": payload, "provider_metadata": provider_metadata})
            items, scoring = _normalize_payload(payload, chunk)
            normalized = {"chunk_id": chunk["chunk_id"], "proposed_items": items, "proposed_scoring_items": scoring, "provider_metadata": provider_metadata}
            write_json(normalized_path, normalized)
            write_json(chunk_dir / "status.json", {"chunk_id": chunk["chunk_id"], "status": "PASS", "finished_at": now_iso()})
            all_items.extend(items)
            all_scoring.extend(scoring)
            status["chunks_completed"] += 1
            status["failed_chunk"] = None
            status["message"] = ""
            status["updated_at"] = now_iso()
            write_json(status_path, status)
        except TenderError:
            raise
        except Exception as exc:
            status.update({"failed_chunk": chunk["chunk_id"], "pack_status": "understanding_failed", "updated_at": now_iso(), "error_code": "UNDERSTAND_PROVIDER_UNAVAILABLE", "error": str(exc)})
            write_json(chunk_dir / "status.json", {"chunk_id": chunk["chunk_id"], "status": "FAIL", "error_code": "UNDERSTAND_PROVIDER_UNAVAILABLE", "error": str(exc), "finished_at": now_iso()})
            write_json(status_path, status)
            raise TenderError("UNDERSTAND_PROVIDER_UNAVAILABLE", f"需求识别未完成。本地文件已解析保留，请重试识别。", {"failed_chunk": chunk["chunk_id"]}) from exc
        finally:
            stop.set()
    # Local high-recall candidates remain the deterministic safety net. Provider
    # proposals may add interpretation, but cannot make a source-backed MUST or
    # repeated occurrence disappear from the authoritative Pack.
    all_items.extend(candidates_from_extraction(extraction))
    pack = build_requirement_pack(extraction, all_items, all_scoring)
    write_json(tender_dir / "requirement_pack.json", pack)
    status.update({"chunks_completed": len(chunks), "failed_chunk": None, "pack_status": pack["status"], "stage": "A5_TODD_CONFIRMATION", "updated_at": now_iso()})
    write_json(status_path, status)
    return {"pack": pack, "status": status}
