"""Canonical Visual IR v1; validate structure and exact source bindings only.

This is not a claim/provenance authority. Reviewed business mappings remain
reviewable source assertions, never inferred by a renderer.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

VERSION = "mmf-visual-ir-v1"
KINDS = {"ORG_CHART", "PROCESS_FLOW", "RELATIONSHIP_DIAGRAM",
         "RESPONSIBILITY_MATRIX_VISUAL", "TIMELINE", "MILESTONE_GANTT"}
SOURCE_KINDS = {"TENDER", "ACCEPTED_FACT", "GENERATED_PROPOSED_METHOD", "APPROVED_SERVICE_DESIGN"}


class VisualContractError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise VisualContractError(message)


def digest(value):
    def normalized(value):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, dict):
            return {k: normalized(v) for k, v in value.items()}
        if isinstance(value, list):
            return [normalized(v) for v in value]
        return value
    return hashlib.sha256(json.dumps(normalized(value), ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def pointer(value, path):
    for token in path.strip("/").split("/") if path else []:
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def validate(ir, source_root=None):
    """Reject missing evidence, changed sources and lossy/ambiguous semantics.

source_root verifies external original CU files. Portable evidence snapshots
are always checked. A digest is integrity evidence, not business approval.
"""
    require(ir.get("schema_version") == VERSION, "unsupported Visual IR version")
    require(ir.get("kind") in KINDS, "unsupported visual kind")
    require(bool(ir.get("visual_id") and ir.get("title") and ir.get("section_id")), "missing identity")
    sources = ir.get("sources", [])
    require(bool(sources), "missing source/CU identity")
    source_map = {s["source_id"]: s for s in sources}
    require(len(source_map) == len(sources), "duplicate source identity")
    for source in sources:
        require(source.get("source_kind") in SOURCE_KINDS, "unsupported source kind")
        require(bool(source.get("cu_id") and source.get("file") and source.get("sha256")), "missing source locator")
        require(source.get("snapshot_digest") == digest(source.get("snapshot")), "changed evidence snapshot")
        if source_root is not None:
            root = Path(source_root).resolve()
            path = (root / source["file"]).resolve()
            require(path.is_relative_to(root), "source escapes frozen snapshot")
            require(path.is_file(), "missing source file")
            raw = path.read_bytes()
            require(hashlib.sha256(raw).hexdigest() == source["sha256"], "changed source file")
            original = json.loads(raw.decode("utf-8-sig"))
            if "content_unit_id" in original:
                require(original["content_unit_id"] == source["cu_id"], "CU identity mismatch")
            if source.get("identity_pointer"):
                require(pointer(original,source["identity_pointer"]) == source["source_id"], "source identity mismatch")
            require(original == source["snapshot"], "snapshot differs from source")

    def evidence(item):
        require(bool(item.get("evidence")), f"missing evidence: {item.get('id', item)}")
        for ref in item["evidence"]:
            require(ref.get("source_id") in source_map, "unknown source")
            try:
                raw = pointer(source_map[ref["source_id"]]["snapshot"], ref["pointer"])
            except (KeyError, IndexError, ValueError, TypeError) as exc:
                raise VisualContractError("invalid source pointer") from exc
            require(isinstance(raw, str) and bool(ref.get("quote")), "evidence must bind source text")
            start, end = ref.get("span", [0, len(raw)])
            require(0 <= start < end <= len(raw) and raw[start:end] == ref["quote"], "source quote mismatch")
        for field in ("label", "title", "caption", "display_text", "condition", "detail", "role", "responsibility"):
            if item.get(field):
                literal = any(str(item[field]) in ref["quote"] for ref in item["evidence"])
                require(literal or item.get("reviewed_projection") is True,
                        f"nonliteral {field} needs explicit reviewed projection")

    evidence(ir)
    nodes = ir.get("nodes", [])
    require(bool(nodes), "no visual nodes")
    ids = {n["id"] for n in nodes}
    require(len(ids) == len(nodes), "duplicate node")
    for node in nodes:
        require(bool(node.get("label")), "empty node label")
        evidence(node)
    for rel in ir.get("relationships", []):
        require(rel.get("from") in ids and rel.get("to") in ids, "dangling relationship")
        require(rel["from"] != rel["to"], "self relationship")
        require(rel.get("relation") in {"REPORTS_TO", "DIRECTS", "NEXT", "COORDINATES", "RETURNS_TO", "DEPENDS_ON"}, "unknown relation")
        evidence(rel)
        require(bool(rel.get("id") and rel.get("label")), "relationship identity/label required")
    relationship_ids = [r["id"] for r in ir.get("relationships", [])]
    require(len(relationship_ids) == len(set(relationship_ids)), "duplicate relationship")
    seq = ir.get("sequence", [])
    if ir["kind"] in {"PROCESS_FLOW", "TIMELINE", "MILESTONE_GANTT"}:
        require(bool(seq) and len(seq) == len(set(seq)) and set(seq) <= ids, "ordered visual sequence must contain unique known nodes")
        if ir["kind"] != "PROCESS_FLOW":
            require(set(seq) == ids, "ordered visual must cover every node once")
        else:
            require(all(n.get("branch") is True for n in nodes if n["id"] not in seq),
                    "nonsequence process node must be explicitly marked as a branch")
            reached = set(seq)
            while True:
                expanded = reached | {r["to"] for r in ir.get("relationships", []) if r["from"] in reached}
                if expanded == reached:
                    break
                reached = expanded
            require(reached == ids, "process branch node must be reachable from main sequence")
        for a, b in zip(seq, seq[1:]):
            require(any(r["from"] == a and r["to"] == b and r["relation"] in {"NEXT", "DEPENDS_ON"}
                        for r in ir.get("relationships", [])), "sequence requires evidenced relationship")
    if ir["kind"] == "ORG_CHART":
        parent = {}
        for rel in ir.get("relationships", []):
            require(rel["relation"] == "REPORTS_TO", "organization tree requires reporting semantics")
            require(rel["from"] not in parent, "multiple administrative parents")
            parent[rel["from"]] = rel["to"]
        require(len(parent) == len(nodes) - 1, "organization chart must have one root")
        for node in ids:
            seen = set()
            while node in parent:
                require(node not in seen, "organization cycle")
                seen.add(node)
                node = parent[node]
    contract = ir.get('relationship_contract')
    if contract is not None:
        snapshot = contract.get('snapshot')
        require(isinstance(snapshot, dict) and contract.get('snapshot_digest') == digest(snapshot),
                'changed relationship contract snapshot')
        require(snapshot.get('scope') == 'GENERATED_PROPOSED_METHOD', 'invalid relationship contract scope')
        if source_root is not None:
            path = (Path(source_root).resolve() / contract.get('file', '')).resolve()
            require(path.is_relative_to(Path(source_root).resolve()) and path.is_file(),
                    'missing relationship contract file')
            raw = path.read_bytes()
            require(hashlib.sha256(raw).hexdigest() == contract.get('sha256'), 'changed relationship contract file')
            require(json.loads(raw.decode('utf-8-sig')) == snapshot, 'relationship contract differs from source')
        expected = snapshot.get('relationships')
        actual = [{key: r.get(key) for key in ('id', 'from', 'to', 'relation')} for r in ir.get('relationships', [])]
        require(expected == actual, 'relationship direction differs from reviewed source contract')
        require(snapshot.get('nodes') == {n['id']: n['label'] for n in nodes}, 'relationship node projection changed')
        for ref in snapshot.get('evidence', []):
            evidence({'id':'relationship-contract', 'evidence':[ref]})
        require(bool(snapshot.get('evidence')), 'relationship contract requires source evidence')
    # Explicit connector geometry is part of the directional assertion.
    positions = ir.get('layout_hints', {}).get('positions', {})
    routes = ir.get('layout_hints', {}).get('routes', {})
    def boundary(point, box):
        x,y=point; bx,by,bw,bh=box
        return bx-.1 <= x <= bx+bw+.1 and by-.1 <= y <= by+bh+.1 and min(
            abs(x-bx),abs(x-bx-bw),abs(y-by),abs(y-by-bh)) <= .1
    for rel in ir.get('relationships', []):
        route = routes.get(rel['id'])
        if route is not None:
            require(len(route) >= 2 and rel['from'] in positions and rel['to'] in positions,
                    'connector geometry missing business endpoints')
            require(boundary(route[0], positions[rel['from']]) and boundary(route[-1], positions[rel['to']]),
                    'connector direction does not touch its source and target nodes')
    roles = ir.get("roles", [])
    require(len({r["id"] for r in roles}) == len(roles), "duplicate role")
    for role in roles:
        evidence(role)
    for group in ir.get("groups", []):
        require(set(group.get("members", [])) <= ids, "unknown group member")
        evidence(group)
    responsibilities = ir.get("responsibilities", [])
    if ir["kind"] == "RESPONSIBILITY_MATRIX_VISUAL":
        require(bool(roles and responsibilities), "empty responsibility matrix")
        require({r["id"] for r in responsibilities} == ids and len(responsibilities) == len(nodes),
                "matrix nodes must correspond exactly to responsibility rows")
        require(all(next(n for n in nodes if n["id"] == r["id"])["label"] == r["label"] for r in responsibilities),
                "matrix row/node label mismatch")
    if ir["kind"] == "RELATIONSHIP_DIAGRAM":
        require(len(nodes) >= 2 and bool(ir.get("relationships")), "relationship diagram needs actual relationships")
    for row in responsibilities:
        evidence(row)
        require(set(row.get("cells", {})) == {r["id"] for r in roles}, "matrix cells must cover every role")
        for cell in row["cells"].values():
            evidence(cell)
    if ir["kind"] == "MILESTONE_GANTT":
        require(ir.get("time_basis") in {"ORDINAL_PHASES", "CONFIRMED_DATES"}, "time basis required")
        for node in nodes:
            require(bool(node.get("condition")), "milestone condition required")
            if ir["time_basis"] == "CONFIRMED_DATES":
                require(bool(node.get("start") and node.get("end")), "confirmed date range required")
                require(node["start"] <= node["end"], "invalid date range")
                require(all(any(str(node[k]) in r["quote"] for r in node["evidence"]) for k in ("start", "end")), "dates must be literal source values")
    return {"status": "PASS", "visual_id": ir["visual_id"], "semantic_digest": digest(ir),
            "nodes": len(nodes), "relationships": len(ir.get("relationships", [])),
            "source_verification": "ORIGINAL_FILES" if source_root is not None else "PORTABLE_SNAPSHOTS"}


def portable_copy(ir):
    validate(ir)
    return copy.deepcopy(ir)
