from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .models import TenderError, now_iso, write_json
from .pack_builder import build_requirement_pack, candidates_from_extraction


RECOGNITION_TIMEOUT_SECONDS = 600
RECOGNITION_MAX_ATTEMPTS = 2
RECOGNITION_RETRY_DELAY_SECONDS = 1.0


def _is_transient_provider_error(exc: Exception) -> bool:
    """Retry only failures that can plausibly recover without user action."""
    text = str(exc or "").lower()
    deterministic = (
        "401",
        "403",
        "unauthorized",
        "forbidden",
        "invalid_api_key",
        "invalid api-key",
        "密钥无效",
        "没有当前模型权限",
    )
    if any(token in text for token in deterministic):
        return False
    error_code = str(getattr(exc, "error_code", "") or "").upper()
    if error_code in {'CURSOR_RESPONSE_UNKNOWN','CURSOR_RECOGNITION_CONNECTION_RESET','CURSOR_RECOGNITION_CONNECTION_STALLED','CURSOR_NOT_DISPATCHED','ZCODE_RESPONSE_UNKNOWN','ZCODE_NO_AUTOMATIC_RESEND','ZCODE_REQUEST_INCOMPLETE','ZCODE_NOT_DISPATCHED','ZCODE_LOGIN_REQUIRED'}:
        return False
    if error_code in {"RATE_LIMIT", "NETWORK_SOCKET_PERMISSION_ERROR"}:
        return True
    transient = (
        "timeout",
        "timed out",
        "超时",
        "429",
        "rate limit",
        "temporarily unavailable",
        "connection reset",
        "network",
        "网络",
        "10013",
    )
    return any(token in text for token in transient)


def _invoke_recognition(provider: Any, request: dict[str, Any], call_dir: Path, *, chunk=None, extraction=None, progress=None) -> dict[str, Any]:
    """One bounded recovery for a proven token-limit terminal; never replay unknown jobs."""
    if (chunk is not None and extraction is not None
            and (getattr(provider,'config',{}) or {}).get('provider_type') in {'zcode_account','cursor_account'}):
        from .zcode_segments import plan_zcode_segments
        from .recognition_segments import execute_segments
        segments=plan_zcode_segments(chunk,extraction,_prompt)
        # Previously dispatched parents retain their identities and unknown-result guard.
        legacy=any((call_dir/name).exists() for name in ('zcode_stream_progress.json','zcode_raw_response.json'))
        if len(segments)>1 and not legacy:
            return execute_segments(provider,request,chunk,extraction,call_dir,segments,_prompt,_mock_payload,progress,max_tokens=16384)
    if (chunk is not None and extraction is not None
            and (getattr(provider, 'config', {}) or {}).get('provider_type') == 'qoder_account'):
        from .recognition_segments import plan_segments, execute_segments
        from .qoder_recognition_recovery import approved_reply_split, split_dense_paragraphs
        segments = plan_segments(chunk, extraction)
        # An upgrade must not replace an already dispatched, unknown parent with
        # fresh child identities. Let the original ledger resolve/block it first.
        legacy_calls = [call_dir, call_dir / 'output_budget_retry']
        legacy_unresolved = False
        for legacy_dir in legacy_calls:
            if (legacy_dir / 'qoder_request_identity.json').is_file():
                raw_path = legacy_dir / 'qoder_raw_answer.json'
                raw = json.loads(raw_path.read_text(encoding='utf-8')) if raw_path.is_file() else {}
                if raw.get('stop_reason') != 'max_tokens':
                    legacy_unresolved = True
        split_approved = approved_reply_split(call_dir, request['task_id'])
        if len(segments) == 1 and (not legacy_unresolved or split_approved):
            segments = split_dense_paragraphs(chunk)
        if len(segments) > 1 and (not legacy_unresolved or split_approved):
            return execute_segments(provider, request, chunk, extraction, call_dir, segments, _prompt, _mock_payload, progress)
    try:
        return provider.invoke_structured(request, call_dir)
    except Exception as exc:
        if (getattr(exc, "error_code", "") != "QODER_OUTPUT_TRUNCATED"
                or request.get("generation_mode") != "tender_requirement_recognition"
                or int(getattr(exc, "max_output_tokens", 8000) or 8000) >= 32000
                or int(request.get("max_tokens") or 8000) >= 32000):
            raise
        # Reuse unchanged source text and schema. Only the failed chunk gets a new,
        # deterministic identity with a larger output budget; prior PASS caches stay.
        retry = {**request, "task_id": str(request["task_id"]) + "-output-32000", "max_tokens": 32000}
        if request.get("request_id"):
            retry["request_id"] = str(request["request_id"]) + "-output-32000"
        audit = {"trigger": "QODER_OUTPUT_TRUNCATED", "max_recovery_attempts": 1,
            "original_task_id": request["task_id"], "recovery_task_id": retry["task_id"],
            "max_output_tokens": 32000, "source_and_schema_unchanged": True, "status": "started"}
        call_dir.mkdir(parents=True, exist_ok=True)
        write_json(call_dir / "output_budget_recovery.json", audit)
        try:
            payload = provider.invoke_structured(retry, call_dir / "output_budget_retry")
        except Exception:
            write_json(call_dir / "output_budget_recovery.json", {**audit, "status": "failed"})
            raise
        metadata = dict(payload.get("provider_metadata") or {})
        payload = {**payload, "provider_metadata": {**metadata, "output_budget_recovery": {**audit, "status": "returned"}}}
        write_json(call_dir / "output_budget_recovery.json", {**audit, "status": "returned"})
        return payload


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
    public_blocks = [{"id": row["source_id"], "kind": row["kind"], "text": row["text"],
        **({"table_context": row["table_context"]} if row.get("table_context") else {})} for row in chunk["blocks"]]
    return f"""你是招标/服务需求文件的Requirement Understanding引擎，只提出候选，不拥有最终Pack权威。

任务要求：
1. 逐块识别项目事实、服务范围、MUST/SHOULD/INFO要求、人员、服务时间、SLA/KPI、评分项、排除范围和疑似模板。
2. 每个proposed_item必须引用给定id作为source_id；不得编造文件名、页码、项目事实或来源。
3. 数字不是PROJECT_SPECIFIC充分证据；只有明确当前项目实体、指代、合同范围或稳定项目上下文时才可建议PROJECT_SPECIFIC。
4. 明确“必须/不得/应至少/否则否决”不得降为INFO。
5. 不删除重复项、不自动解决冲突、不自动删除疑似模板；本地Pack Builder将负责ID、去重、冲突与最终分类。
6. 只返回一个JSON对象，根字段必须且只能为chunk_id、proposed_items、proposed_conflicts、proposed_boilerplate、proposed_scoring_items、chunk_missing_facts。
7. proposed_items每项至少含normalized_requirement、requirement_type、mandatory_level_guess、classification_guess、source_id、source_excerpt、confidence。
8. proposed_conflicts每项用description和candidates说明；至少两个candidate分别含source_id、source_excerpt、value。source_excerpt必须逐字存在于当前块；不得用本块id冒充块外另一处，只有一处证据时保留为chunk_missing_facts待核实。
9. proposed_boilerplate每项含source_id、source_excerpt、reason；chunk_missing_facts只描述当前块未显示的事项，不断言整份文件缺失，不填造事实。

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
    if "glm" in blob or "zhipu" in blob or "智谱" in display:
        return "智谱GLM"
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
        "provider_name": (getattr(provider,'config',{}) or {}).get('provider_name'),
        "extraction_completed": True, "chunks_total": len(chunks), "chunks_completed": 0, "failed_chunk": None,
        "pack_status": "understanding", "stage": "A4_PROVIDER_UNDERSTANDING", "updated_at": now_iso(),
    }
    write_json(status_path, status)
    provider_type = (getattr(provider, 'config', {}) or {}).get('provider_type')
    if provider_type in {'zcode_account', 'cursor_account'}:
        from .zcode_segments import pending_request_plan
        plan=pending_request_plan(chunks,extraction,_prompt,chunks_dir)
        prefix = 'cursor' if provider_type == 'cursor_account' else 'zcode'
        if prefix == 'cursor':
            plan['provider'] = provider.config['provider_name']
            plan['max_output_tokens'] = None
        write_json(tender_dir/f'{prefix}_recognition_plan.json',plan)
        status[f'{prefix}_request_plan']=plan
        if plan['planned_requests']>plan['max_requests']:
            raise TenderError(f'{prefix.upper()}_RECOGNITION_PLAN_BUDGET',f"文件识别预计需要{plan['planned_requests']}次请求，超过本轮{plan['max_requests']}次上限；未发送模型请求。")
    all_items: list[dict[str, Any]] = []
    all_scoring: list[dict[str, Any]] = []
    all_proposals: list[dict[str, Any]] = []
    from .proposal_channels import CHANNELS, normalize_channels
    provider_error: Exception | None = None
    for chunk in chunks:
        chunk_dir = chunks_dir / chunk["chunk_id"]
        normalized_path = chunk_dir / "normalized.json"
        if resume and normalized_path.is_file():
            normalized = json.loads(normalized_path.read_text(encoding="utf-8-sig"))
            if normalized.get("proposal_channels_version") != 1:
                raw_path = chunk_dir / "raw.json"
                if raw_path.is_file():
                    raw_payload = json.loads(raw_path.read_text(encoding="utf-8-sig"))["payload"]
                    normalized.update(normalize_channels(raw_payload, chunk), proposal_channels_version=1)
                    write_json(normalized_path, normalized)
                else:
                    # Legacy caches cannot certify that absent channels were empty.
                    all_proposals.append({"proposal_id": chunk["chunk_id"]+":legacy_channels", "channel": "LEGACY_CHANNELS_UNAVAILABLE",
                        "chunk_id": chunk["chunk_id"], "proposal": "旧缓存缺少原始提议，须审阅", "sources": [],
                        "validation_status": "UNVERIFIED_PROPOSAL", "scope": "EXTRACTION_CHUNK"})
            all_items.extend(normalized.get("proposed_items", []))
            all_scoring.extend(normalized.get("proposed_scoring_items", []))
            for key in CHANNELS:all_proposals.extend(normalized.get(key, []))
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
            "generation_mode": "tender_requirement_recognition",
            "timeout_seconds": RECOGNITION_TIMEOUT_SECONDS,
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
                part = status.get('recognition_segments') or {}
                detail = f"{part.get('parent_chunk_id')}，第{part.get('current')}/{part.get('total')}个子块，" if part else ''
                status["message"] = f"正在调用{engine}识别需求，{detail}已用时{elapsed // 60}分{elapsed % 60}秒。请继续等待。"
                write_json(status_path, status)

        heartbeat = threading.Thread(target=_heartbeat, daemon=True)
        heartbeat.start()
        try:
            attempts: list[dict[str, Any]] = []
            payload = None
            for attempt_no in range(1, RECOGNITION_MAX_ATTEMPTS + 1):
                call_dir = chunk_dir / ("provider_call" if attempt_no == 1 else f"provider_call_retry_{attempt_no:02d}")
                attempt_started = now_iso()
                try:
                    payload = _invoke_recognition(provider, request, call_dir, chunk=chunk, extraction=extraction,
                        progress=lambda part: status.update(recognition_segments=part))
                    attempts.append({
                        "attempt": attempt_no,
                        "status": "PASS",
                        "started_at": attempt_started,
                        "finished_at": now_iso(),
                        "call_dir": call_dir.name,
                    })
                    write_json(chunk_dir / "recognition_retry_audit.json", {
                        "chunk_id": chunk["chunk_id"],
                        "max_attempts": RECOGNITION_MAX_ATTEMPTS,
                        "timeout_seconds": RECOGNITION_TIMEOUT_SECONDS,
                        "attempts": attempts,
                    })
                    break
                except Exception as exc:
                    retriable = _is_transient_provider_error(exc)
                    attempts.append({
                        "attempt": attempt_no,
                        "status": "FAIL",
                        "started_at": attempt_started,
                        "finished_at": now_iso(),
                        "call_dir": call_dir.name,
                        "error_type": type(exc).__name__,
                        "error_code": getattr(exc, "error_code", None),
                        "error": str(exc)[:400],
                        "retriable": retriable,
                    })
                    write_json(chunk_dir / "recognition_retry_audit.json", {
                        "chunk_id": chunk["chunk_id"],
                        "max_attempts": RECOGNITION_MAX_ATTEMPTS,
                        "timeout_seconds": RECOGNITION_TIMEOUT_SECONDS,
                        "attempts": attempts,
                    })
                    if not retriable or attempt_no >= RECOGNITION_MAX_ATTEMPTS:
                        raise
                    status["message"] = (
                        f"{engine}本次识别遇到临时故障，系统正在自动重试"
                        f"（{attempt_no + 1}/{RECOGNITION_MAX_ATTEMPTS}）。"
                    )
                    status["updated_at"] = now_iso()
                    write_json(status_path, status)
                    time.sleep(RECOGNITION_RETRY_DELAY_SECONDS)
            if payload is None:
                raise RuntimeError("AI需求识别未返回结果")
            provider_metadata = payload.pop("provider_metadata", {})
            write_json(chunk_dir / "raw.json", {"payload": payload, "provider_metadata": provider_metadata})
            items, scoring = _normalize_payload(payload, chunk)
            normalized = {"chunk_id": chunk["chunk_id"], "proposed_items": items, "proposed_scoring_items": scoring, "provider_metadata": provider_metadata}
            channels = normalize_channels(payload, chunk)
            normalized.update(channels, proposal_channels_version=1)
            write_json(normalized_path, normalized)
            write_json(chunk_dir / "status.json", {"chunk_id": chunk["chunk_id"], "status": "PASS", "finished_at": now_iso()})
            all_items.extend(items)
            all_scoring.extend(scoring)
            for key in CHANNELS:all_proposals.extend(channels[key])
            status["chunks_completed"] += 1
            status.pop('recognition_segments', None)
            status["failed_chunk"] = None
            status["message"] = ""
            status["updated_at"] = now_iso()
            write_json(status_path, status)
        except TenderError as exc:
            provider_error = exc
            status.update({"failed_chunk": chunk["chunk_id"], "updated_at": now_iso(), "provider_error": str(exc)})
            write_json(status_path, status)
            break
        except Exception as exc:
            provider_error = exc
            status.update({"failed_chunk": chunk["chunk_id"], "updated_at": now_iso(), "error_code": "UNDERSTAND_PROVIDER_UNAVAILABLE", "provider_error": str(exc)})
            write_json(chunk_dir / "status.json", {"chunk_id": chunk["chunk_id"], "status": "FAIL", "error_code": "UNDERSTAND_PROVIDER_UNAVAILABLE", "error": str(exc), "finished_at": now_iso()})
            write_json(status_path, status)
            break
        finally:
            stop.set()
            heartbeat.join()
    # Local high-recall candidates remain the deterministic safety net. Provider
    # proposals may add interpretation, but cannot make a source-backed MUST or
    # repeated occurrence disappear from the authoritative Pack.
    all_items.extend(candidates_from_extraction(extraction))
    if not all_items:
        cause = provider_error or TenderError("UNDERSTAND_PROVIDER_UNAVAILABLE", "需求识别未完成。本地文件已解析保留，请重试识别。")
        status.update({"pack_status": "understanding_failed", "stage": "A4_PROVIDER_UNDERSTANDING_FAILED", "error": str(cause), "updated_at": now_iso()})
        write_json(status_path, status)
        raise TenderError("UNDERSTAND_PROVIDER_UNAVAILABLE", str(cause), {"failed_chunk": status.get("failed_chunk")}) from (provider_error if isinstance(provider_error, Exception) else None)
    pack = build_requirement_pack(extraction, all_items, all_scoring, provider_proposals=all_proposals)
    write_json(tender_dir / "requirement_pack.json", pack)
    status.update({"chunks_completed": status.get("chunks_completed") or 0, "pack_status": pack["status"], "stage": "A5_TODD_CONFIRMATION", "updated_at": now_iso()})
    from .cursor_recognition_recovery import CHOICE_ERRORS, WAITING
    waiting = getattr(provider_error, "error_code", None) in CHOICE_ERRORS

    if provider_error:
        cause = str(getattr(provider_error, "__cause__", None) or provider_error)
        status["recognition_outcome"] = WAITING if waiting else "AI_RECOGNITION_FAILED_WITH_LOCAL_FALLBACK"
        status["local_fallback_confirmation_required"] = True
        status["local_fallback_confirmed"] = False
        status["warning"] = (f"AI需求识别已暂停，等待选择处理方式。{cause}" if waiting else
                             f"AI需求识别未完成，当前仅展示本地解析结果。{cause}")
        status["message"] = status["warning"]
        status.pop("error", None)
    else:
        status["recognition_outcome"] = "AI_RECOGNITION_SUCCESS"
        status["local_fallback_confirmation_required"] = False
        status["local_fallback_confirmed"] = True
        status.pop("warning", None)
        status["message"] = "AI需求识别已完成。"
    write_json(tender_dir / "provider_recognition_audit.json", {
        "run_id": extraction.get("run_id"),
        "recognition_outcome": status["recognition_outcome"],
        "provider_error_type": type(provider_error).__name__ if provider_error else None,
        "provider_error_code": getattr(provider_error, "error_code", None) if provider_error else None,
        "local_fallback_used": bool(provider_error) and not waiting,
        "local_fallback_confirmation_required": status["local_fallback_confirmation_required"],
        "created_at": now_iso(),
    })
    write_json(status_path, status)
    return {"pack": pack, "status": status}
