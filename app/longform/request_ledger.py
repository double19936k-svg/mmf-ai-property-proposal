"""Provider request lifecycle ledger (MMF-007C Round 2 / P0-1).

A request that timed out is not a request that failed: the answer can still
arrive on the queue afterwards. Without a durable record of *which* request is
still open, a late response has nowhere to be reconciled into and the Section
simply goes missing in the delivered artifact.

This module owns the vocabulary and the projection:

    CREATED -> REQUEST_SENT -> RESPONSE_RECEIVED -> PERSISTED -> RESOLVED
    REQUEST_SENT -> RESPONSE_UNKNOWN -> RESPONSE_RECEIVED_LATE -> PERSISTED -> RESOLVED

``RESPONSE_UNKNOWN`` therefore stays open, never terminal. ``RESPONSE_REJECTED``
is the explicit "we gave up on this request" state that Final Assembly reports
instead of pretending the content exists.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

LEDGER_FILENAME = "provider_unknown_requests.jsonl"

CREATED = "CREATED"
REQUEST_SENT = "REQUEST_SENT"
RESPONSE_RECEIVED = "RESPONSE_RECEIVED"
RESPONSE_RECEIVED_LATE = "RESPONSE_RECEIVED_LATE"
PERSISTED = "PERSISTED"
RESOLVED = "RESOLVED"
RESPONSE_UNKNOWN = "RESPONSE_UNKNOWN"
RESPONSE_REJECTED = "RESPONSE_REJECTED"
RESEND_CONTROLLED = "RESEND_CONTROLLED"

TERMINAL_STATES = {RESOLVED, RESPONSE_REJECTED}
# Everything the provider was asked for but that has not been settled yet.
OPEN_STATES = {CREATED, REQUEST_SENT, RESPONSE_UNKNOWN, RESPONSE_RECEIVED, RESPONSE_RECEIVED_LATE, PERSISTED}
# States that consumed (or may have consumed) a provider attempt.
COUNTED_STATES = {REQUEST_SENT, RESPONSE_RECEIVED, RESPONSE_UNKNOWN, RESPONSE_REJECTED, RESOLVED}


def read_events(path: Path, integrity: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """读请求账；坏行**必须留痕**而不是悄悄丢掉。

    一行读不出来通常意味着这条记录被别的写入撕裂（见 `factory._append_request_event`）。
    丢掉它会让 `open_requests`/`attempts_seen` 少算，晚到响应核对就把一次"已发出"的
    请求当成从没发生过 —— 内容与钱都已经付出去了。所以坏行照常跳过（不能拿半行去判决），
    但数量与样本必须交给调用方。
    """
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        if integrity is not None:
            integrity["ledger_readable"] = False
        return []
    rows: list[dict[str, Any]] = []
    malformed: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            malformed.append(line[:160])
            continue
        if isinstance(row, dict):
            rows.append(row)
    if integrity is not None:
        integrity["ledger_readable"] = True
        integrity["malformed_line_count"] = len(malformed)
        integrity["malformed_line_samples"] = malformed[:3]
        integrity["records_read"] = len(rows)
    return rows


def latest_status(events: list[dict[str, Any]]) -> dict[str, str]:
    """Last recorded status per ``request_id`` (ledger order is the timeline)."""
    current: dict[str, str] = {}
    for row in events:
        request_id = str(row.get("request_id") or "")
        status = str(row.get("status") or "")
        if request_id and status:
            current[request_id] = status
    return current


def open_requests(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Requests that were sent but never reached a terminal state."""
    current = latest_status(events)
    last: dict[str, dict[str, Any]] = {}
    for row in events:
        request_id = str(row.get("request_id") or "")
        if request_id:
            last[request_id] = row
    return [row for request_id, row in last.items() if current.get(request_id) not in TERMINAL_STATES]


def unresolved_for_fingerprint(events: list[dict[str, Any]], fingerprint: str) -> list[dict[str, Any]]:
    return [row for row in open_requests(events) if row.get("request_fingerprint") == fingerprint]


def attempts_seen(events: list[dict[str, Any]], fingerprint: str) -> int:
    """How many provider attempts this logical request already recorded."""
    return sum(1 for row in events if row.get("request_fingerprint") == fingerprint and row.get("status") in COUNTED_STATES)


def control_resends_seen(events: list[dict[str, Any]], fingerprint: str) -> int:
    return sum(1 for row in events if row.get("request_fingerprint") == fingerprint and row.get("status") == RESEND_CONTROLLED)


def payload_fingerprint(payload: dict[str, Any]) -> str:
    """Content hash of what was actually sent, so a resend can be proven identical."""
    basis = json.dumps({
        "system_prompt": str(payload.get("system_prompt") or ""),
        "prompt": str(payload.get("prompt") or ""),
        "response_protocol": payload.get("response_protocol"),
        "max_tokens": payload.get("max_tokens"),
        "required_keys": sorted(set(payload.get("required_keys") or [])),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def mint_identity(
    *,
    payload: dict[str, Any],
    run_id: str,
    fingerprint: str,
    provider: str,
    attempt_no: int,
    content_unit_ids: list[str],
) -> dict[str, Any]:
    """Stable request identity: the key a late response is reconciled back by.

    ``job_id`` is derived (not random) on purpose. The queue file name is what
    the executor answers into, so a timed-out request can only be recovered if
    the same name can be recomputed after the waiter is gone.
    """
    task_id = str(payload.get("task_id") or "")
    section_ids = list(payload.get("section_ids") or ([payload.get("section_id")] if payload.get("section_id") else []))
    batch_id = payload.get("batch_id")
    request_id = f"REQ-{run_id}-{fingerprint[:12]}-A{attempt_no}"
    return {
        "request_id": request_id,
        "run_id": run_id,
        "task_id": task_id,
        "job_id": f"{task_id}-A{attempt_no}-{fingerprint[:8]}",
        "batch_id": batch_id,
        "section_ids": section_ids,
        "content_unit_ids": list(content_unit_ids),
        "provider": provider,
        "attempt_no": attempt_no,
        "payload_hash": payload_fingerprint(payload),
        "request_fingerprint": fingerprint,
    }
