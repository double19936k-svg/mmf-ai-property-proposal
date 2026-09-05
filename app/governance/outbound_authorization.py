from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


class OutboundAuthorizationManifest:
    REQUIRED = {"destination", "purpose", "allowed_scope", "forbidden_scope", "authorization_state", "task_id", "expires_with_task"}

    def __init__(self, path: Path):
        self.path = path

    def save(self, value: dict[str, Any]) -> dict[str, Any]:
        missing = self.REQUIRED - set(value)
        if missing: raise ValueError(f"manifest missing fields: {sorted(missing)}")
        payload = {**value, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(self.path)
        return payload

    def load(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def authorize(self, *, destination: str, purpose: str, requested_scope: list[str], task_id: str) -> dict[str, Any]:
        if not self.path.is_file(): return {"authorized": False, "reason": "PROJECT_AUTHORIZATION_MISSING"}
        value = self.load()
        if value.get("task_id") != task_id or value.get("destination") != destination or value.get("purpose") != purpose:
            return {"authorized": False, "reason": "NEW_AUTHORIZATION_REQUIRED"}
        if value.get("authorization_state") not in {"AUTHORIZED", "PLATFORM_FORCED_CONFIRMATION"}:
            return {"authorized": False, "reason": str(value.get("authorization_state"))}
        allowed = set(value.get("allowed_scope", []))
        expanded = [item for item in requested_scope if item not in allowed]
        if expanded: return {"authorized": False, "reason": "SCOPE_EXPANSION", "expanded_scope": expanded}
        return {"authorized": True, "reason": "TASK_SCOPED_AUTHORIZATION_REUSED", "platform_forced_confirmation": value.get("authorization_state") == "PLATFORM_FORCED_CONFIRMATION"}
