from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = "mmf006e-workflow-timing-v0.1"
TIMING_FILENAME = "workflow_timing.json"
CALCULATION_METHOD = "completed_stage_seconds_plus_current_active_stage_elapsed_seconds"

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_ACTIVE_STAGES: dict[str, dict[str, Any]] = {}
_ACTIVE_GUARD = threading.RLock()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    last_error: OSError | None = None
    for attempt in range(8):
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{attempt}.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.04 * (attempt + 1))
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    if last_error is not None:
        raise last_error


def _timing_path(run_dir: Path) -> Path:
    return Path(run_dir) / TIMING_FILENAME


def _new_payload(workflow_kind: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": f"workflow_{uuid.uuid4().hex}",
        "workflow_kind": workflow_kind or "unknown",
        "workflow_started_at": _now_iso(),
        "workflow_completed_at": None,
        "status": "processing",
        "calculation_method": CALCULATION_METHOD,
        "stage_timings": {},
        "total_processing_seconds": 0.0,
        "processing_seconds_so_far": 0.0,
        "current_active_stage": None,
        "current_active_stage_elapsed_seconds": 0.0,
        "current_total_processing_seconds": 0.0,
        "user_idle_seconds": None,
    }


def _recalculate(payload: dict[str, Any]) -> dict[str, Any]:
    stages = payload.get("stage_timings") if isinstance(payload.get("stage_timings"), dict) else {}
    total = round(sum(max(0.0, float(row.get("elapsed_seconds") or 0.0)) for row in stages.values() if isinstance(row, dict)), 3)
    payload["stage_timings"] = stages
    payload["total_processing_seconds"] = total
    payload["processing_seconds_so_far"] = total
    payload["current_total_processing_seconds"] = total
    payload["calculation_method"] = CALCULATION_METHOD
    payload["updated_at"] = _now_iso()
    return payload


def _active_key(run_dir: Path) -> str:
    return str(_timing_path(run_dir).resolve())


def set_active_stage(run_dir: Path, stage: str, *, workflow_kind: str = "unknown") -> dict[str, Any]:
    """Set the current in-process stage used by both Generation and History timers.

    The monotonic start is deliberately kept in process memory. A stopped program
    therefore cannot turn shutdown or user-idle wall time into processing time.
    """
    path = _timing_path(run_dir)
    stage_name = str(stage or "").strip()
    if not stage_name:
        return clear_active_stage(run_dir)
    with _ACTIVE_GUARD:
        current = _ACTIVE_STAGES.get(_active_key(run_dir))
        if current and current.get("stage") == stage_name:
            return load_workflow_timing(run_dir)
        active = {
            "stage": stage_name,
            "started_monotonic": time.monotonic(),
            "started_at": _now_iso(),
            "pid": os.getpid(),
        }
        _ACTIVE_STAGES[_active_key(run_dir)] = active
    with _lock(path):
        payload = _read(path) or _new_payload(workflow_kind)
        payload["status"] = "processing"
        payload["workflow_completed_at"] = None
        payload["current_active_stage"] = {
            "stage": stage_name,
            "started_at": active["started_at"],
            "pid": active["pid"],
        }
        _write(path, _recalculate(payload))
    return load_workflow_timing(run_dir)


def clear_active_stage(run_dir: Path) -> dict[str, Any]:
    path = _timing_path(run_dir)
    with _ACTIVE_GUARD:
        _ACTIVE_STAGES.pop(_active_key(run_dir), None)
    with _lock(path):
        payload = _read(path)
        if not payload:
            return {}
        payload["current_active_stage"] = None
        payload["current_active_stage_elapsed_seconds"] = 0.0
        _write(path, _recalculate(payload))
        return payload


def _live_view(run_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    completed = float(payload.get("total_processing_seconds") or 0.0)
    active_elapsed = 0.0
    current_stage = None
    with _ACTIVE_GUARD:
        active = _ACTIVE_STAGES.get(_active_key(run_dir))
        if active and int(active.get("pid") or -1) == os.getpid():
            active_elapsed = max(0.0, time.monotonic() - float(active["started_monotonic"]))
            current_stage = {
                "stage": active.get("stage"),
                "started_at": active.get("started_at"),
                "pid": active.get("pid"),
            }
    payload["current_active_stage"] = current_stage
    payload["current_active_stage_elapsed_seconds"] = round(active_elapsed, 3)
    payload["current_total_processing_seconds"] = round(completed + active_elapsed, 3)
    payload["processing_seconds_so_far"] = payload["current_total_processing_seconds"]
    return payload


def ensure_workflow(run_dir: Path, workflow_kind: str = "unknown") -> dict[str, Any]:
    path = _timing_path(run_dir)
    with _lock(path):
        payload = _read(path)
        if not payload.get("workflow_id"):
            payload = _new_payload(workflow_kind)
        elif workflow_kind and payload.get("workflow_kind") in {None, "", "unknown"}:
            payload["workflow_kind"] = workflow_kind
        payload["status"] = "processing"
        payload["workflow_completed_at"] = None
        _write(path, _recalculate(payload))
        return payload


def record_stage_duration(
    run_dir: Path,
    stage: str,
    elapsed_seconds: float,
    *,
    workflow_kind: str = "unknown",
    outcome: str = "completed",
) -> dict[str, Any]:
    path = _timing_path(run_dir)
    with _lock(path):
        payload = _read(path)
        if not payload.get("workflow_id"):
            payload = _new_payload(workflow_kind)
        payload["status"] = "processing"
        payload["workflow_completed_at"] = None
        stages = payload.setdefault("stage_timings", {})
        row = stages.setdefault(stage, {"elapsed_seconds": 0.0, "attempt_count": 0, "segments": []})
        elapsed = round(max(0.0, float(elapsed_seconds or 0.0)), 3)
        row["elapsed_seconds"] = round(float(row.get("elapsed_seconds") or 0.0) + elapsed, 3)
        row["attempt_count"] = int(row.get("attempt_count") or 0) + 1
        row["last_outcome"] = outcome
        row["last_completed_at"] = _now_iso()
        segments = row.setdefault("segments", [])
        segments.append({"elapsed_seconds": elapsed, "outcome": outcome, "completed_at": row["last_completed_at"]})
        # Keep the audit useful without allowing unbounded growth across many resumes.
        if len(segments) > 50:
            row["segments"] = segments[-50:]
        _write(path, _recalculate(payload))
        return payload


@contextmanager
def track_stage(run_dir: Path, stage: str, *, workflow_kind: str = "unknown") -> Iterator[None]:
    ensure_workflow(run_dir, workflow_kind)
    set_active_stage(run_dir, stage, workflow_kind=workflow_kind)
    started = time.monotonic()
    outcome = "completed"
    try:
        yield
    except BaseException:
        outcome = "failed"
        raise
    finally:
        clear_active_stage(run_dir)
        record_stage_duration(run_dir, stage, time.monotonic() - started, workflow_kind=workflow_kind, outcome=outcome)


def record_longform_attempt(
    run_dir: Path,
    wall_seconds: float,
    *,
    planning_seconds: float = 0.0,
    repair_seconds: float = 0.0,
    repair_attempt: bool = False,
    outcome: str = "completed",
) -> dict[str, Any]:
    wall = max(0.0, float(wall_seconds or 0.0))
    if repair_attempt:
        return record_stage_duration(run_dir, "section_repair", wall, outcome=outcome)
    planning = min(wall, max(0.0, float(planning_seconds or 0.0)))
    remaining = max(0.0, wall - planning)
    repair = min(remaining, max(0.0, float(repair_seconds or 0.0)))
    generation = max(0.0, remaining - repair)
    payload = record_stage_duration(run_dir, "document_planning", planning, outcome=outcome)
    payload = record_stage_duration(run_dir, "longform_generation", generation, outcome=outcome)
    if repair > 0:
        payload = record_stage_duration(run_dir, "section_repair", repair, outcome=outcome)
    return payload


def finish_workflow(run_dir: Path, status: str) -> dict[str, Any]:
    path = _timing_path(run_dir)
    with _ACTIVE_GUARD:
        _ACTIVE_STAGES.pop(_active_key(run_dir), None)
    with _lock(path):
        payload = _read(path)
        if not payload.get("workflow_id"):
            payload = _new_payload("unknown")
        payload["status"] = status
        payload["workflow_completed_at"] = _now_iso()
        payload["current_active_stage"] = None
        payload["current_active_stage_elapsed_seconds"] = 0.0
        _write(path, _recalculate(payload))
        return payload


def load_workflow_timing(run_dir: Path) -> dict[str, Any]:
    path = _timing_path(run_dir)
    with _lock(path):
        payload = _read(path)
        return _live_view(run_dir, _recalculate(payload)) if payload else {}


def workflow_audit_fields(run_dir: Path) -> dict[str, Any]:
    payload = load_workflow_timing(run_dir)
    stages = payload.get("stage_timings") or {}
    elapsed = lambda name: round(float((stages.get(name) or {}).get("elapsed_seconds") or 0.0), 3)
    qa_seconds = elapsed("content_and_compliance_qa") + elapsed("final_artifact_qa")
    return {
        "workflow_id": payload.get("workflow_id"),
        "workflow_started_at": payload.get("workflow_started_at"),
        "workflow_completed_at": payload.get("workflow_completed_at"),
        "total_processing_seconds": payload.get("current_total_processing_seconds", payload.get("total_processing_seconds", 0.0)),
        "processing_seconds_so_far": payload.get("processing_seconds_so_far", 0.0),
        "current_active_stage": payload.get("current_active_stage"),
        "current_active_stage_elapsed_seconds": payload.get("current_active_stage_elapsed_seconds", 0.0),
        "current_total_processing_seconds": payload.get("current_total_processing_seconds", payload.get("total_processing_seconds", 0.0)),
        "stage_timings": stages,
        "user_idle_seconds": payload.get("user_idle_seconds"),
        "generation_seconds": elapsed("longform_generation"),
        "repair_seconds": elapsed("section_repair"),
        "artifact_seconds": elapsed("artifact_assembly"),
        "qa_seconds": round(qa_seconds, 3),
        "workflow_qa_seconds": round(qa_seconds, 3),
        "workflow_timing_method": payload.get("calculation_method"),
    }


def history_duration(run_dir: Path, legacy_generation_seconds: float | int | None = None) -> dict[str, Any]:
    payload = load_workflow_timing(run_dir)
    if payload.get("workflow_id") and payload.get("stage_timings"):
        return {
            "total_processing_seconds": int(round(float(payload.get("current_total_processing_seconds") or payload.get("total_processing_seconds") or 0.0))),
            "current_total_processing_seconds": int(round(float(payload.get("current_total_processing_seconds") or payload.get("total_processing_seconds") or 0.0))),
            "current_active_stage": payload.get("current_active_stage"),
            "current_active_stage_elapsed_seconds": payload.get("current_active_stage_elapsed_seconds", 0.0),
            "generation_stage_seconds": None,
            "duration_scope": "complete_workflow_processing",
            "duration_label": "总处理耗时",
            "workflow_id": payload.get("workflow_id"),
        }
    try:
        legacy = max(0, int(float(legacy_generation_seconds or 0)))
    except (TypeError, ValueError):
        legacy = 0
    return {
        "total_processing_seconds": None,
        "generation_stage_seconds": legacy,
        "duration_scope": "legacy_generation_stage_only" if legacy else "unavailable",
        "duration_label": "生成阶段耗时" if legacy else "",
        "workflow_id": None,
    }


def union_interval_seconds(intervals: list[tuple[float, float]]) -> float:
    valid = sorted((float(start), float(end)) for start, end in intervals if float(end) > float(start))
    if not valid:
        return 0.0
    total = 0.0
    current_start, current_end = valid[0]
    for start, end in valid[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    total += current_end - current_start
    return round(total, 3)
