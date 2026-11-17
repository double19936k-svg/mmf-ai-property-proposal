"""Disk-authoritative Content Unit audit reconstruction."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def rebuild_content_unit_generation_audit(run_dir: Path) -> dict[str, Any]:
    """Rebuild unique CU counts from final hierarchy, ledger and disk artifacts."""
    run_dir = Path(run_dir)
    hierarchy = _read(run_dir / "08_document_hierarchy.json")
    planned_ids = {str(row.get("content_unit_id")) for row in hierarchy.get("content_units", []) if isinstance(row, dict) and row.get("content_unit_id")}
    sent_ids: set[str] = set()
    request_attempts = 0
    ledger = run_dir / "provider_unknown_requests.jsonl"
    if ledger.is_file():
        for line in ledger.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ids = {str(x) for x in row.get("content_unit_ids") or [] if x}
            if row.get("status") in {"REQUEST_SENT", "RESPONSE_RECEIVED", "RESOLVED", "RESPONSE_UNKNOWN", "RESPONSE_REJECTED"}:
                sent_ids.update(ids)
            if row.get("status") == "REQUEST_SENT":
                request_attempts += 1

    received_ids: set[str] = set()
    returned_ids: set[str] = set()
    for path in (run_dir / "longform" / "word").glob("**/provider_audit.json"):
        row = _read(path)
        received_ids.update(str(x) for x in row.get("provider_received_content_unit_ids") or [] if x)
    for path in (run_dir / "longform" / "word").glob("**/provider_structured_output.json"):
        row = _read(path)
        def collect(value):
            if isinstance(value, dict):
                for fragment in value.get('content_unit_fragments') or []:
                    if isinstance(fragment, dict) and fragment.get('content_unit_id'):
                        returned_ids.add(str(fragment['content_unit_id']))
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        collect(row)
        if isinstance(row, dict) and row.get('cu_envelope_text'):
            import re
            from .envelope import parse_cu_envelope
            text = row['cu_envelope_text']
            ids = re.findall(r'<<<\s*CU_START\s*:\s*([^>\s]+)\s*>>>', text, re.I)
            parsed = parse_cu_envelope(text, requested_ids=ids)
            returned_ids.update(parsed['blocks'])
    # Older provider audits did not persist the request CU list. A structured
    # response is still durable adapter evidence for those returned CUs.
    received_ids.update(returned_ids)
    persisted_ids: set[str] = set()
    for path in (run_dir / "longform" / "word" / "sections").glob("*/cus/CU-*.json"):
        row = _read(path)
        if row.get("generation_origin") == "provider_content_unit" and not row.get("covered_by_section_assembly"):
            if row.get("content_unit_id"):
                persisted_ids.add(str(row["content_unit_id"]))
    # A valid provider CU artifact is the durable completion fact.  Checkpoint
    # entries are reconciled as corroborating evidence, never as a substitute.
    completed_ids = persisted_ids & planned_ids
    audit = {
        "schema_version": "content-unit-generation-audit-v0.2",
        "authority": {"planned": "08_document_hierarchy.json", "sent": "provider_unknown_requests.jsonl", "provider_received": "provider_audit.json", "returned": "provider_structured_output.json", "persisted": "longform/word/sections/*/cus/CU-*.json", "completed": "persisted_artifact_reconciled_with_checkpoint"},
        "planned": len(planned_ids),
        "sent": len(sent_ids & planned_ids),
        "provider_received": len(received_ids & planned_ids),
        "returned": len(returned_ids & planned_ids),
        "persisted": len(persisted_ids & planned_ids),
        "completed": len(completed_ids),
        "request_attempts": request_attempts,
        "unique_sent_cu": len(sent_ids & planned_ids),
        "unique_received_cu": len(received_ids & planned_ids),
        "unique_returned_cu": len(returned_ids & planned_ids),
        "planned_content_unit_ids": sorted(planned_ids),
        "sent_content_unit_ids": sorted(sent_ids & planned_ids),
        "provider_received_content_unit_ids": sorted(received_ids & planned_ids),
        "returned_content_unit_ids": sorted(returned_ids & planned_ids),
        "persisted_content_unit_ids": sorted(persisted_ids & planned_ids),
        "completed_content_unit_ids": sorted(completed_ids),
    }
    (run_dir / "content_unit_generation_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return audit
