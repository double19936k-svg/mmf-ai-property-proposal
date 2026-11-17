"""Serialize terminal publication, cancellation and progress for each local run."""
from __future__ import annotations

import json
import threading
from pathlib import Path

TERMINAL = frozenset({"completed", "generation_completed", "generation_partial", "failed", "cancelled"})
_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()


def run_lock(run_id: str) -> threading.RLock:
    with _guard:
        return _locks.setdefault(run_id, threading.RLock())


def read_status(path: Path) -> dict:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("生成状态记录无效，未修改任务。")
    return value


def write_progress(path: Path, payload: dict, writer) -> bool:
    with run_lock(str(payload["run_id"])):
        if read_status(path).get("status") in TERMINAL:
            return False
        writer(path, payload)
        return True
