from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any


SUCCESS = "SUCCESS"
ERROR_TAXONOMY = {
    "PLATFORM_CONFIRMATION_REQUIRED",
    "PROVIDER_AUTH_REQUIRED",
    "AUTH_STATE_UNKNOWN",
    "NETWORK_ERROR",
    "CLI_MAX_TURNS",
    "OUTPUT_CONTRACT_ERROR",
    "PROVIDER_RUNTIME_ERROR",
    "PROVIDER_UNAVAILABLE",
    SUCCESS,
}

DEFERRED_BY_ERROR = {
    "PLATFORM_CONFIRMATION_REQUIRED": "DEFERRED_PLATFORM_CONFIRMATION",
    "PROVIDER_AUTH_REQUIRED": "DEFERRED_PROVIDER_AUTH",
    "AUTH_STATE_UNKNOWN": "DEFERRED_AUTH_STATE_UNKNOWN",
    "NETWORK_ERROR": "DEFERRED_NETWORK",
    "CLI_MAX_TURNS": "DEFERRED_CLI_MAX_TURNS",
    "OUTPUT_CONTRACT_ERROR": "DEFERRED_OUTPUT_CONTRACT",
    "PROVIDER_RUNTIME_ERROR": "DEFERRED_PROVIDER_RUNTIME",
    "PROVIDER_UNAVAILABLE": "DEFERRED_PROVIDER_UNAVAILABLE",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def classify_provider_failure(
    code: str | None,
    message: str = "",
    *,
    auth_probe: dict[str, Any] | None = None,
    platform_confirmation_required: bool = False,
) -> str:
    if platform_confirmation_required:
        return "PLATFORM_CONFIRMATION_REQUIRED"
    raw_code = str(code or "").upper()
    text = str(message or "").lower()
    if "max turns reached" in text or raw_code == "CLI_MAX_TURNS":
        return "CLI_MAX_TURNS"
    if raw_code in {"INVALID_OUTPUT", "OUTPUT_CONTRACT_ERROR"} or "structured output" in text or "结构化" in text:
        return "OUTPUT_CONTRACT_ERROR"
    if raw_code in {"NETWORK_ERROR", "TIMEOUT", "PROXY_ERROR"} or any(token in text for token in ("timed out", "timeout", "connection reset", "network error")):
        return "NETWORK_ERROR"
    if raw_code in {"AUTH_REQUIRED", "PROVIDER_AUTH_REQUIRED"} or any(token in text for token in ("http 401", "unauthorized", "login required", "not authenticated")):
        if auth_probe and (auth_probe.get("authenticated") is False or auth_probe.get("auth_state") in {"UNAUTHENTICATED", "PROVIDER_AUTH_REQUIRED"}):
            return "PROVIDER_AUTH_REQUIRED"
        return "AUTH_STATE_UNKNOWN"
    if raw_code == "AUTH_STATE_UNKNOWN":
        return "AUTH_STATE_UNKNOWN"
    if raw_code in {"CLI_NOT_FOUND", "MODEL_UNAVAILABLE", "PROVIDER_UNAVAILABLE"}:
        return "PROVIDER_UNAVAILABLE"
    if raw_code in {"SUCCESS", "PASS"}:
        return SUCCESS
    return "PROVIDER_RUNTIME_ERROR"


def deferred_status(error_code: str) -> str:
    return DEFERRED_BY_ERROR.get(error_code, "DEFERRED_PROVIDER_RUNTIME")


def make_attempt(
    run_id: str,
    phase_id: str,
    attempt_id: int,
    status: str,
    *,
    completed: bool,
    finished_at: str | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "phase_id": phase_id,
        "attempt_id": int(attempt_id),
        "status": status,
        "completed": bool(completed),
        "finished_at": finished_at or now_iso(),
        "detail": detail or {},
    }


def select_authoritative_result(attempts: list[dict[str, Any]], run_id: str, phase_id: str) -> dict[str, Any] | None:
    matched = [row for row in attempts if row.get("run_id") == run_id and row.get("phase_id") == phase_id]
    if not matched:
        return None
    completed = [row for row in matched if row.get("completed")]
    source = completed or matched
    return deepcopy(max(source, key=lambda row: (int(row.get("attempt_id", 0)), str(row.get("finished_at", "")))))


def update_execution_state(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(state)
    history = list(result.get("history_events", []))
    history.append(deepcopy(event))
    result["history_events"] = history
    result["deferred_events"] = [row for row in history if str(row.get("status", "")).startswith("DEFERRED_")]
    current = select_authoritative_result(history, str(event["run_id"]), str(event["phase_id"]))
    result["current_status"] = current
    return result


def unattended_stage_result(
    *,
    local_engineering_pass: bool,
    offline_tests_pass: bool,
    artifacts_pass: bool,
    acceptance_ui_pass: bool,
    provider_status: str,
) -> dict[str, Any]:
    local_pass = all((local_engineering_pass, offline_tests_pass, artifacts_pass, acceptance_ui_pass))
    if not local_pass:
        return {"stage_status": "STAGE_FAILED_LOCAL_ENGINEERING", "external_status": provider_status, "blocking": True}
    if provider_status == SUCCESS or provider_status in {"PASS", "WARNING"}:
        return {"stage_status": "STAGE_COMPLETED", "external_status": provider_status, "blocking": False}
    if provider_status.startswith("DEFERRED_"):
        return {"stage_status": "STAGE_COMPLETED_WITH_DEFERRED_EXTERNAL", "external_status": provider_status, "blocking": False}
    if provider_status in ERROR_TAXONOMY:
        deferred = deferred_status(provider_status)
        return {"stage_status": "STAGE_COMPLETED_WITH_DEFERRED_EXTERNAL", "external_status": deferred, "blocking": False}
    return {"stage_status": "STAGE_COMPLETED_WITH_DEFERRED_EXTERNAL", "external_status": "DEFERRED_PROVIDER_RUNTIME", "blocking": False}
