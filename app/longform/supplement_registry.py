"""Durable supplement reservations; rejected intents never recycle an identity.

Only the factory checkpoint lock may mutate this per-section registry. A
reservation is a planning record, not proof that content arrived or satisfied a
requirement. Cleanup requires the matching registration token in the CU file.
"""
from __future__ import annotations

import copy
import json
import re
import uuid
from pathlib import Path
from typing import Any

from filesystem_paths import filesystem_path
from providers.base import write_json

NAME = "supplement_cu_registry.json"
SCHEMA = "supplement-cu-reservations-v1"
STATES = {"REGISTERED", "REJECTED", "CLEANUP_BLOCKED"}


def owns_arrival(record: dict[str, Any], row: dict[str, Any],
                 section_id: str, cu_id: str) -> bool:
    """A filename alone cannot complete a reserved supplement."""
    return (record.get("status") == "REGISTERED"
            and row.get("supplement_registration_id") == record.get("registration_id")
            and row.get("content_unit_id") == cu_id
            and row.get("parent_section_id") == section_id
            and row.get("generation_origin") == "provider_content_unit")


def load_registry(unit: Path, section_id: str) -> dict[str, dict[str, Any]]:
    path = filesystem_path(unit / NAME)
    if not path.is_file():
        return {}
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if document.get("schema_version") != SCHEMA or document.get("section_id") != section_id:
        raise ValueError("supplement_registry_identity_mismatch")
    records = document.get("reservations")
    if not isinstance(records, dict):
        raise ValueError("invalid_supplement_reservations")
    for cu_id, record in records.items():
        valid_id = re.fullmatch(re.escape(f"CU-{section_id}-") + r"\d{2,}", cu_id)
        if (not valid_id or not isinstance(record, dict)
                or record.get("status") not in STATES
                or not re.fullmatch(r"[0-9a-f]{32}", str(record.get("registration_id") or ""))
                or not isinstance(record.get("contract"), dict)
                or record["contract"].get("content_unit_id") != cu_id
                or record["contract"].get("parent_section_id") != section_id):
            raise ValueError("invalid_supplement_reservation")
    return records


def save_registry(unit: Path, section_id: str, records: dict[str, dict[str, Any]]) -> None:
    write_json(unit / NAME, {"schema_version": SCHEMA, "section_id": section_id,
                            "reservations": records})


def reserve(unit: Path, section_id: str, rows: list[dict[str, Any]],
            *, occupied_ids: set[str]) -> dict[str, dict[str, Any]]:
    records = load_registry(unit, section_id)
    fresh: dict[str, dict[str, Any]] = {}
    for row in rows:
        cu_id = str(row.get("content_unit_id") or "")
        if (not re.fullmatch(re.escape(f"CU-{section_id}-") + r"\d{2,}", cu_id)
                or row.get("parent_section_id") not in (None, "", section_id)):
            raise ValueError("invalid_supplement_contract_identity")
        if (cu_id in occupied_ids or cu_id in records or cu_id in fresh
                or filesystem_path(unit / "cus" / f"{cu_id}.json").exists()):
            raise ValueError(f"supplement_content_unit_id_collision:{cu_id}")
        fresh[cu_id] = {"contract": {**copy.deepcopy(row), "parent_section_id": section_id},
                        "registration_id": uuid.uuid4().hex, "status": "REGISTERED"}
    # Validate the whole batch before reserving anything or changing in-memory plans.
    updated = {**records, **fresh}
    save_registry(unit, section_id, updated)
    return fresh


def discard(unit: Path, section_id: str, cu_ids: list[str]) -> list[str]:
    records = load_registry(unit, section_id)
    removed: list[str] = []
    changed = False
    for cu_id in cu_ids:
        record = records.get(cu_id)
        if not record or record["status"] == "REJECTED":
            continue
        path = filesystem_path(unit / "cus" / f"{cu_id}.json")
        if path.is_file():
            row = json.loads(path.read_text(encoding="utf-8-sig"))
            if (row.get("supplement_registration_id") != record["registration_id"]
                    or row.get("content_unit_id") != cu_id
                    or row.get("parent_section_id") != section_id):
                record["status"] = "CLEANUP_BLOCKED"
                changed = True
                continue
            path.unlink()
        record["status"] = "REJECTED"
        changed = True
        removed.append(cu_id)
    if changed:
        save_registry(unit, section_id, records)
    return removed
