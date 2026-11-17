"""Source-bound provider suggestions, retained separately from accepted facts."""
from __future__ import annotations
from copy import deepcopy
from typing import Any

CHANNELS = ("proposed_conflicts", "proposed_boilerplate", "chunk_missing_facts")

def normalize_channels(payload: dict[str, Any], chunk: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    from .understanding import _locator
    allowed = {str(block["source_id"]): block for block in chunk["blocks"]}
    result = {}
    for channel in CHANNELS:
        rows = []
        for index, candidate in enumerate(payload[channel], 1):
            proposal = deepcopy(candidate)
            row = {"proposal_id": f"{chunk['chunk_id']}:{channel}:{index}",
                   "channel": channel, "chunk_id": chunk["chunk_id"], "proposal": proposal,
                   "scope": "EXTRACTION_CHUNK", "sources": [], "source_errors": []}
            if channel == "proposed_conflicts":
                refs = candidate.get("candidates", candidate.get("options", [])) if isinstance(candidate, dict) else []
                if not isinstance(refs, list): refs = []
            else:
                refs = [candidate] if isinstance(candidate, dict) else []
            for ref in refs:
                if not isinstance(ref, dict):
                    row["source_errors"].append("SOURCE_OBJECT_REQUIRED"); continue
                source_id = str(ref.get("source_id") or "")
                excerpt = str(ref.get("source_excerpt") or "").strip()
                block = allowed.get(source_id)
                if block is None or not excerpt or excerpt not in block["text"]:
                    row["source_errors"].append({"source_id": source_id, "reason": "SOURCE_ID_OR_EXCERPT_NOT_IN_CHUNK"}); continue
                row["sources"].append({"source_id": source_id, "source": _locator(block, excerpt),
                                       "proposed_value": deepcopy(ref.get("value"))})
            minimum = 2 if channel == "proposed_conflicts" else 1
            distinct = {(s["source_id"], s["source"]["source_excerpt"]) for s in row["sources"]}
            valid = len(distinct) >= minimum and not row["source_errors"]
            # Missing facts concern what a chunk did not show, never prove global absence.
            row["validation_status"] = "SOURCE_BOUND_PROPOSAL" if valid else "UNVERIFIED_PROPOSAL"
            row["value_authority"] = "PROVIDER_PROPOSAL_NOT_CONFIRMED"
            rows.append(row)
        result[channel] = rows
    return result

def attach_proposals(requirements, conflicts, boilerplate, proposals):
    def linked(sources):
        found = []
        for row in requirements:
            for evidence in sources:
                pointer = evidence["source"]
                quote = pointer["source_excerpt"]
                if any(source.get("file_id") == pointer.get("file_id")
                       and source.get("para_id") == pointer.get("para_id")
                       and source.get("table_id") == pointer.get("table_id")
                       and (quote in source.get("source_excerpt", "") or source.get("source_excerpt", "") in quote)
                       for source in row["sources"]):
                    found.append(row["requirement_id"]); break
        return list(dict.fromkeys(found))
    by_id = {row["requirement_id"]: row for row in requirements}
    for item in proposals:
        item["review_status"] = "UNREVIEWED"
        if item["validation_status"] != "SOURCE_BOUND_PROPOSAL": continue
        raw = item["proposal"]
        ids = linked(item["sources"])
        if item["channel"] == "proposed_conflicts":
            cid = f"CFG-{len(conflicts)+1:03d}"
            conflicts.append({"conflict_group_id": cid, "requirement_ids": ids,
                "issue": str(raw.get("description") or raw.get("issue") or raw.get("field") or "来源存在待确认冲突"),
                "options": [{"requirement_id": None, "excerpt": s["source"]["source_excerpt"],
                             "source": s["source"], "value": s["proposed_value"]} for s in item["sources"]],
                "resolution": "UNRESOLVED", "custom_value": None, "blocker_for_plan": True,
                "provenance": "SOURCE_BOUND_PROVIDER_PROPOSAL", "proposal_id": item["proposal_id"]})
            for rid in ids:
                row = by_id[rid];row["classification"] = "CONFLICT_OR_AMBIGUOUS"
                row["conflict_group"] = cid;row["must_not_use_as_project_fact"] = True
        elif item["channel"] == "proposed_boilerplate":
            bid = f"BPL-{len(boilerplate)+1:03d}"
            boilerplate.append({"boilerplate_id": bid, "requirement_ids": ids,
                "suspicion_reasons": [str(raw.get("reason") or "Provider提出疑似模板，待审阅")],
                "evidence": item["sources"][0]["source"]["source_excerpt"],
                "sources": [s["source"] for s in item["sources"]],
                "action_allowed": "flag_and_confirm_only", "todd_decision": "UNCONFIRMED",
                "provenance": "SOURCE_BOUND_PROVIDER_PROPOSAL", "proposal_id": item["proposal_id"]})
            for rid in ids:
                row = by_id[rid];row["boilerplate_flag_id"] = bid
                if not row.get("conflict_group"):row["classification"] = "POTENTIAL_BOILERPLATE"
                row["must_not_use_as_project_fact"] = True
    return proposals
