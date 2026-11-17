"""Map the same validated Visual IR to the existing production slide payload."""
from copy import deepcopy

from .ir import validate
from .scene import compile_scene


def visual_slide(ir, chapter_id, slide_id=None):
    validate(ir)
    return {"slide_id": slide_id or ir["visual_id"], "chapter_id": chapter_id,
            "title": ir["title"], "core_message": ir.get("caption", ""), "layout": "visual",
            "visual_scene": compile_scene(deepcopy(ir)),
            "source_section_ids": [ir["section_id"]],
            "source_cu_ids": [s["cu_id"] for s in ir["sources"]]}
