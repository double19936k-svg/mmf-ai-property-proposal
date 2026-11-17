"""One page-fit scene compiler shared by both render targets, without prose parsing."""
from __future__ import annotations

import math
import unicodedata

from .ir import validate, digest, VisualContractError, require

WIDTH, HEIGHT = 1120, 560


def wrap(text, width, size):
    lines, line, used = [], "", 0
    for char in str(text):
        if char == "\n":
            lines.append(line)
            line, used = "", 0
            continue
        weight = size if unicodedata.east_asian_width(char) in "WF" else size * 0.6
        if used + weight > width and line:
            lines.append(line)
            line, used = "", 0
        line += char
        used += weight
    lines.append(line)
    return lines


def compile_scene(ir):
    validate(ir)
    primitives = []

    def box(id, x, y, w, h, label, size=26, fill="#F3F3F3", bold=False):
        lines = wrap(label, w - 24, size)
        require(len(lines) * size * 1.3 <= h - 16, f"VISUAL_LAYOUT_REVIEW_REQUIRED: {id}; text cannot fit without loss")
        primitives.append({"type": "box", "id": id, "x": x, "y": y, "w": w, "h": h,
                           "lines": lines, "font_size": size, "fill": fill, "bold": bold})

    nodes = ir["nodes"]
    positions = ir.get("layout_hints", {}).get("positions")
    if ir["kind"] == "RESPONSIBILITY_MATRIX_VISUAL":
        roles, rows = ir["roles"], ir["responsibilities"]
        row_h = min(108, 520 / (len(rows) + 1))
        first_w = 292
        cell_w = (WIDTH - first_w) / len(roles)
        box("matrix-heading", 0, 0, first_w, row_h, "责任事项", bold=True)
        for c, role in enumerate(roles):
            box(role["id"], first_w + c * cell_w, 0, cell_w, row_h, role["label"], bold=True)
        for r, row in enumerate(rows):
            box(row["id"], 0, (r + 1) * row_h, first_w, row_h, row["label"], size=25, fill="#FFFFFF")
            for c, role in enumerate(roles):
                cell = row["cells"][role["id"]]
                box(row["id"] + ":" + role["id"], first_w + c * cell_w, (r + 1) * row_h,
                    cell_w, row_h, cell["label"], size=25, fill="#FFFFFF")
    elif ir["kind"] == "MILESTONE_GANTT":
        # Ordinal columns assert sequence, never a proportional calendar duration.
        require(ir["time_basis"] == "ORDINAL_PHASES", "confirmed-date Gantt needs separately reviewed scaled layout")
        seq = ir["sequence"]
        col_w = 155
        label_w = WIDTH - len(seq) * col_w
        require(label_w >= 300, "too many Gantt stages for page")
        row_h = min(100, 500 / (len(seq) + 1))
        box("gantt-basis", 0, 0, label_w, row_h, "阶段顺序（非日历工期）", bold=True)
        for i, nid in enumerate(seq):
            box("phase-" + nid, label_w + i * col_w, 0, col_w, row_h, f"阶段 {i + 1}", bold=True)
        by_id = {n["id"]: n for n in nodes}
        for i, nid in enumerate(seq):
            box(nid, 0, row_h * (i + 1), label_w, row_h, by_id[nid]["label"], fill="#FFFFFF")
            for j in range(len(seq)):
                box(f"gantt-{i}-{j}", label_w + j * col_w, row_h * (i + 1), col_w, row_h,
                    "◆" if i == j else "", fill="#FFFFFF")
    else:
        if positions is None:
            require(ir["kind"] in {"PROCESS_FLOW", "TIMELINE"}, "graph needs explicit positions; no inferred hierarchy")
            # Vertical pairs leave a free corridor for return paths.
            cols = 2
            rows = math.ceil(len(nodes) / cols)
            w, h = 480, min(124, 480 / rows - 25)
            positions = {node["id"]: [20 + (i % cols) * 580, 18 + (i // cols) * (h + 40), w, h]
                         for i, node in enumerate(nodes)}
        require(set(positions) == {n["id"] for n in nodes}, "positions must cover all nodes")
        for node in nodes:
            box(node["id"], *positions[node["id"]], node.get("display_text", node["label"]),
                bold=ir["kind"] == "ORG_CHART")
        for rel in ir.get("relationships", []):
            a, b = positions[rel["from"]], positions[rel["to"]]
            points = ir.get("layout_hints", {}).get("routes", {}).get(rel["id"])
            if points is None:
                ax, ay, aw, ah = a
                bx, by, bw, bh = b
                if ay == by:
                    if bx > ax:
                        points = [[ax + aw, ay + ah / 2], [bx, by + bh / 2]]
                    else:
                        points = [[ax, ay + ah / 2], [bx + bw, by + bh / 2]]
                else:
                    points = [[ax + aw / 2, ay + ah], [ax + aw / 2, (ay + ah + by) / 2],
                              [bx + bw / 2, (ay + ah + by) / 2], [bx + bw / 2, by]]
            primitives.insert(0, {"type": "edge", "id": rel["id"], "points": points,
                                  "from_node": rel["from"], "to_node": rel["to"],
                                  "label": rel["label"], "dashed": rel["relation"] == "RETURNS_TO"})
            if rel.get("label_position"):
                x, y = rel["label_position"]
                box("label:" + rel["id"], x, y, rel.get("label_width", 230), 48,
                    rel["label"], size=22, fill="#FFFFFF")
                primitives[-1]["border"] = False
    # Relations retained even when matrix/Gantt carries them as structure, plus
    # full detail/condition/source snapshots in the target's semantic attachment.
    scene = {"schema_version": "mmf-visual-scene-v1", "visual_id": ir["visual_id"],
             "width": WIDTH, "height": HEIGHT, "primitives": primitives,
             "semantic_digest": digest(ir), "semantic_ir": ir}
    audit_scene(scene)
    scene["scene_digest"] = digest({k: scene[k] for k in ("width", "height", "primitives", "semantic_digest")})
    return scene


def audit_scene(scene):
    for item in scene["primitives"]:
        if item["type"] == "box":
            require(0 <= item["x"] and 0 <= item["y"] and item["x"] + item["w"] <= WIDTH + .1
                    and item["y"] + item["h"] <= HEIGHT + .1, "visual outside printable area")
        else:
            require(all(0 <= x <= WIDTH and 0 <= y <= HEIGHT for x, y in item["points"]), "connector outside printable area")
    boxes = [p for p in scene["primitives"] if p["type"] == "box"]
    for i, a in enumerate(boxes):
        for b in boxes[i+1:]:
            overlap_x = min(a["x"]+a["w"], b["x"]+b["w"]) - max(a["x"],b["x"])
            overlap_y = min(a["y"]+a["h"], b["y"]+b["h"]) - max(a["y"],b["y"])
            require(overlap_x <= .1 or overlap_y <= .1, f"overlapping visual boxes: {a['id']} / {b['id']}")
    by_id = {p["id"]: p for p in boxes}
    def on_boundary(point, box):
        x, y = point
        return (box["x"] - .1 <= x <= box["x"] + box["w"] + .1 and
                box["y"] - .1 <= y <= box["y"] + box["h"] + .1 and
                min(abs(x-box["x"]),abs(x-box["x"]-box["w"]),
                    abs(y-box["y"]),abs(y-box["y"]-box["h"])) <= .1)
    for edge in [p for p in scene["primitives"] if p["type"] == "edge"]:
        require(on_boundary(edge["points"][0],by_id[edge["from_node"]]) and
                on_boundary(edge["points"][-1],by_id[edge["to_node"]]), "connector endpoint must touch its business node")
        for (ax,ay),(bx,by) in zip(edge["points"],edge["points"][1:]):
            require(ax == bx or ay == by, "only reviewed orthogonal connector routes supported")
            for box in boxes:
                if ax == bx:
                    crosses = box["x"]+.1 < ax < box["x"]+box["w"]-.1 and max(min(ay,by),box["y"]+.1) < min(max(ay,by),box["y"]+box["h"]-.1)
                else:
                    crosses = box["y"]+.1 < ay < box["y"]+box["h"]-.1 and max(min(ax,bx),box["x"]+.1) < min(max(ax,bx),box["x"]+box["w"]-.1)
                require(not crosses, f"connector {edge['id']} crosses label/node {box['id']}")
    return {"status": "PASS", "primitive_count": len(scene["primitives"])}
