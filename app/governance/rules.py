from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

RULE_DIR = Path(__file__).resolve().parents[1] / "assets" / "governance"


@lru_cache(maxsize=None)
def load_rule(name: str) -> dict[str, Any]:
    path = (RULE_DIR / name).resolve()
    if path.parent != RULE_DIR.resolve() or not path.is_file():
        raise FileNotFoundError(f"MMF-005 governance rule is missing: {name}")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or not (value.get("schema_version") or value.get("$schema")):
        raise ValueError(f"Invalid governance rule asset: {name}")
    return value
