"""Persisted, bounded execution policy for the optional <=60-page PPT path."""
from __future__ import annotations
import time
import json
from pathlib import Path
from providers.base import write_json

PROFILE = "bounded_90min_v1"
PAGE_RESPONSE_SECONDS = 180
LATE_RECONCILE_SECONDS = 120
RECOMMEND_SECONDS = 120

def completion_state(run_root: Path) -> dict | None:
    """Only the bounded PPT path opts in; artifact existence is not completion."""
    plan_path = run_root / "06_ppt_presentation_plan.json"
    if not plan_path.is_file():
        return None
    plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    if (plan.get("execution_policy") or {}).get("profile") != PROFILE:
        return None
    planned = [s["slide_id"] for s in plan.get("slide_storyboard", [])]
    completed = []
    for sid in planned:
        unit = run_root / "longform/ppt/slides" / sid
        try:
            status = json.loads((unit / "status.json").read_text(encoding="utf-8-sig"))
            payload = json.loads((unit / "payload.json").read_text(encoding="utf-8-sig"))
            gate = json.loads((unit / "governance.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if status.get("status") in {"COMPLETED", "COMPLETED_WITH_WARNING"} and payload.get("slide_id") == sid and gate.get("status") in {"PASS", "WARNING"}:
            completed.append(sid)
    pending = [sid for sid in planned if sid not in completed]
    return {"complete": bool(planned) and not pending, "planned_body_pages": len(planned),
            "completed_body_pages": len(completed), "pending_slide_ids": pending,
            "resume_available": bool(pending)}

def execution_budget(root:Path, *, started_epoch:float|None=None)->dict:
    path=root/"execution_budget.json"
    if path.exists():
        data=json.loads(path.read_text(encoding="utf-8"))
        if data.get("profile")!=PROFILE or data.get("wall_seconds")!=5400:
            raise ValueError("PPT execution budget identity mismatch; do not reset a prior budget")
        if data["deadline_epoch"]!=data["started_epoch"]+5400 or data["generation_deadline_epoch"]!=data["started_epoch"]+4200:
            raise ValueError("PPT execution deadline was altered")
        return data
    start=time.time() if started_epoch is None else float(started_epoch)
    data={"profile":PROFILE,"started_epoch":start,"wall_seconds":5400,
          "deadline_epoch":start+5400,"generation_deadline_epoch":start+4200,
          "generation_reserve_seconds":1200,"parallelism_cap":2,
          "clock_basis":"instance preflight through export/review; auto-start at first generation only if preflight was omitted"}
    write_json(path,data)
    return data

def generation_seconds_left(budget:dict, *, now:float|None=None)->float:
    return max(0.0,budget["generation_deadline_epoch"]-(time.time() if now is None else now))

def estimated_seconds(body_pages:int, page_p90_seconds:float, *, workers:int=2, retry_pages:int=6, fixed_seconds:int=1500)->float:
    """Capacity estimate only; actual model/transport time still needs measurement."""
    import math
    if body_pages<1 or page_p90_seconds<=0 or workers not in (1,2):raise ValueError("invalid capacity inputs")
    return math.ceil((body_pages+retry_pages)/workers)*page_p90_seconds+fixed_seconds
