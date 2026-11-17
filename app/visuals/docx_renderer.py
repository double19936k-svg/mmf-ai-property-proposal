"""Embed the shared scene as print-resolution PNG and preserve the full IR."""
from __future__ import annotations

import json
import math
from pathlib import Path
from xml.sax.saxutils import escape

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.shared import Inches, Pt

from .ir import validate, require, digest
from .scene import compile_scene
import hashlib
from filesystem_paths import filesystem_path


def render_png(scene, path, font_path, scale=3):
    # Optional visual runtime dependency; never an image-generation provider.
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise ValueError("DOCX visual rendering requires Pillow in the dedicated visual runtime") from exc
    require(Path(font_path).is_file(), "explicit Chinese font file required")
    image = Image.new("RGB", (int(scene["width"] * scale), int(scene["height"] * scale)), "white")
    draw = ImageDraw.Draw(image)
    for item in scene["primitives"]:
        if item["type"] == "edge":
            points = [(x * scale, y * scale) for x, y in item["points"]]
            if item["dashed"]:
                for a, b in zip(points, points[1:]):
                    length = math.dist(a, b)
                    for offset in range(0, int(length), int(12 * scale)):
                        t, u = offset / max(length, 1), min(offset + 7 * scale, length) / max(length, 1)
                        draw.line([(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t),
                                   (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u)], fill="#444444", width=int(2 * scale))
            else:
                draw.line(points, fill="#444444", width=int(2 * scale))
            a, b = points[-2:]
            angle = math.atan2(b[1] - a[1], b[0] - a[0])
            wings = [(b[0] - 10 * scale * math.cos(angle + d), b[1] - 10 * scale * math.sin(angle + d)) for d in (-.5, .5)]
            draw.polygon([b, *wings], fill="#444444")
        else:
            x, y, w, h = [item[k] * scale for k in ("x", "y", "w", "h")]
            draw.rectangle((x, y, x + w, y + h), fill=item["fill"],
                           outline="#444444" if item.get("border", True) else None, width=int(scale))
            font = ImageFont.truetype(str(font_path), round(item["font_size"] * scale))
            text = "\n".join(item["lines"])
            bounds = draw.multiline_textbbox((0, 0), text, font=font, spacing=round(5 * scale), align="center")
            require(bounds[2] - bounds[0] <= w - 16 * scale and bounds[3] - bounds[1] <= h - 12 * scale,
                    "actual Chinese font exceeds layout; no truncation permitted")
            draw.multiline_text((x + (w - bounds[2] + bounds[0]) / 2 - bounds[0],
                                 y + (h - bounds[3] + bounds[1]) / 2 - bounds[1]),
                                text, font=font, fill="#222222", spacing=round(5 * scale), align="center")
    if hasattr(path, 'write'):
        image.save(path, format='PNG', dpi=(288, 288))
        return path
    path = filesystem_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, dpi=(288, 288))
    return path


def embed_ir(doc, ir):
    payload = '<visual xmlns="urn:mmf:visual:v1" id="' + escape(ir["visual_id"]) + '">' + escape(json.dumps(ir, ensure_ascii=False)) + '</visual>'
    package = doc.part.package
    index = 1
    existing = {str(p.partname) for p in package.parts}
    while f"/customXml/visual{index}.xml" in existing:
        index += 1
    part = Part(PackURI(f"/customXml/visual{index}.xml"), "application/xml", payload.encode("utf-8"), package)
    doc.part.relate_to(part, RT.CUSTOM_XML)


def add_visual(doc, ir, asset_dir, font_path):
    validate(ir)
    scene = compile_scene(ir)
    section = doc.sections[-1]
    available = (section.page_width - section.left_margin - section.right_margin) / 914400
    height = (section.page_height - section.top_margin - section.bottom_margin) / 914400
    require(available >= 5.5 and available * scene["height"] / scene["width"] + 1.0 <= height,
            "VISUAL_LAYOUT_REVIEW_REQUIRED: page too small for readable visual")
    minimum_font = min(p["font_size"] for p in scene["primitives"] if p["type"] == "box") * available * 72 / scene["width"]
    require(minimum_font >= 9, "VISUAL_LAYOUT_REVIEW_REQUIRED: printed label would be smaller than 9pt")
    path = filesystem_path(Path(asset_dir) / (digest(ir) + ".png"))
    render_png(scene, path, font_path)
    caption = doc.add_paragraph(ir["title"])
    caption.paragraph_format.keep_with_next = True
    caption.paragraph_format.space_after = Pt(8)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.keep_together = True
    shape = p.add_run().add_picture(str(path), width=Inches(available))
    shape._inline.docPr.set("descr", "\n".join(n["label"] for n in ir["nodes"]))
    shape._inline.docPr.set("title", ir["title"])
    shape._inline.docPr.set("name", "MMF_VISUAL:" + ir['visual_id'])
    embed_ir(doc, ir)
    return {"type": "diagram", "asset_path": str(path.resolve()), "visual_id": ir["visual_id"],
            "ir": ir, "ir_digest": digest(ir), "media_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "font_path": str(Path(font_path).resolve()), "font_sha256": hashlib.sha256(Path(font_path).read_bytes()).hexdigest(),
            "semantic_digest": scene["semantic_digest"], "width_inches": available,
            "height_inches": available * scene["height"] / scene["width"], "minimum_font_pt": minimum_font}
