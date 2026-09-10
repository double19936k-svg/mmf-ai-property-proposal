from __future__ import annotations

import json
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROBE_TTL_SECONDS = 3600
UNVERIFIED_VENDOR_FIELDS = ("max_context", "tpm", "rpm", "native_max_level")
OBSERVATION_ALLOWLIST = {
    "provider_name",
    "observed_model",
    "configured_model",
    "listed_models",
    "native_reasoning_acceptance",
    "structured_response",
    "token_usage_availability",
    "rate_limit_metadata",
    "provenance",
    "status",
    "reason",
    "observed_at",
    "observed_epoch",
    "ttl_seconds",
    "unverified_fields",
    "inferred_from_model_prefix",
    "auth_state",
    "probe_kind",
    "fresh",
}
EXACT_SECRET_KEYS = {
    "api_key",
    "authorization",
    "secret",
    "password",
    "access_token",
    "refresh_token",
    "credential",
    "credentials",
    "token",
}
_SECRET_VALUE = re.compile(r"(sk-[A-Za-z0-9]{8,}|bearer\s+[A-Za-z0-9._\-]+|eyJ[A-Za-z0-9_\-]{20,})", re.I)

_LOCK = threading.RLock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_secret_key(key: str) -> bool:
    lowered = str(key or "").strip().lower()
    return lowered in EXACT_SECRET_KEYS


def _sanitize_reason(value: Any) -> str:
    text = str(value or "")
    text = _SECRET_VALUE.sub("[redacted]", text)
    return text[:180]


def public_observation(row: dict[str, Any] | None) -> dict[str, Any]:
    raw = row if isinstance(row, dict) else {}
    cleaned: dict[str, Any] = {}
    for key, item in raw.items():
        if key not in OBSERVATION_ALLOWLIST or _is_secret_key(key):
            continue
        if key == "reason":
            cleaned[key] = _sanitize_reason(item)
        elif key == "rate_limit_metadata" and isinstance(item, dict):
            cleaned[key] = {
                "headers_present": bool(item.get("headers_present")),
                "rpm": item.get("rpm") if item.get("rpm") in {None, "unknown"} or isinstance(item.get("rpm"), (int, float, str)) else "unknown",
                "tpm": item.get("tpm") if item.get("tpm") in {None, "unknown"} or isinstance(item.get("tpm"), (int, float, str)) else "unknown",
            }
        else:
            cleaned[key] = item
    return cleaned


def observation_store_path(runtime_dir: Path | None = None) -> Path:
    if runtime_dir is not None:
        return Path(runtime_dir) / "capability_observations.json"
    try:
        import paths as mmf_paths
        return mmf_paths.current().runtime_dir / "capability_observations.json"
    except Exception:
        return Path("runtime") / "capability_observations.json"


def load_observations(path: Path | None = None) -> dict[str, Any]:
    target = path or observation_store_path()
    if not target.is_file():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): public_observation(value) for key, value in raw.items() if isinstance(value, dict)}


def save_observations(rows: dict[str, Any], path: Path | None = None) -> None:
    target = path or observation_store_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {str(key): public_observation(value) for key, value in (rows or {}).items() if isinstance(value, dict)}
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)


def observation_key(provider_name: str, model: str = "") -> str:
    return f"{provider_name or 'unknown'}|{model or 'unknown'}"


def _fresh(row: dict[str, Any], now_ts: float | None = None) -> bool:
    ttl = int(row.get("ttl_seconds") or PROBE_TTL_SECONDS)
    observed_epoch = row.get("observed_epoch")
    try:
        observed_epoch = float(observed_epoch)
    except (TypeError, ValueError):
        return False
    import time
    now = time.time() if now_ts is None else now_ts
    return (now - observed_epoch) <= ttl


def classify_probe_failure(exc: BaseException | None, message: str = "", status_code: int | None = None) -> str:
    text = f"{type(exc).__name__ if exc else ''} {message}".lower()
    code = int(status_code) if status_code is not None else None
    if code in {401} or any(token in text for token in ("not authenticated", "login required", "invalid api key", "incorrect api key")):
        return "AUTH_REQUIRED"
    if code in {403} or any(token in text for token in ("permission", "forbidden", "access is denied", "access denied")):
        return "PERMISSION_RESTRICTED"
    if "timed out" in text or "timeout" in text:
        return "TIMEOUT"
    if any(token in text for token in ("network", "connection", "proxy", "unreachable", "name or service not known")):
        return "NETWORK_ERROR"
    return "UNAVAILABLE"


def empty_observation(provider_name: str, model: str = "", *, status: str, reason: str) -> dict[str, Any]:
    import time
    return public_observation({
        "provider_name": provider_name,
        "observed_model": model or "unknown",
        "configured_model": model or "unknown",
        "listed_models": [],
        "native_reasoning_acceptance": "unknown",
        "structured_response": "unknown",
        "token_usage_availability": "unknown",
        "rate_limit_metadata": "unknown",
        "provenance": "unavailable",
        "status": status,
        "reason": reason,
        "observed_at": utc_now(),
        "observed_epoch": time.time(),
        "ttl_seconds": PROBE_TTL_SECONDS,
        "unverified_fields": list(UNVERIFIED_VENDOR_FIELDS),
        "inferred_from_model_prefix": False,
        "probe_kind": "none",
    })


def _synthetic_probe(provider: Any) -> dict[str, Any]:
    """Minimal connection-test structured ping. Never uses customer data."""
    result = {
        "structured_response": "unknown",
        "token_usage_availability": "unknown",
        "native_reasoning_acceptance": "unknown",
        "probe_kind": "synthetic_structured_ping",
    }
    if provider is None or not hasattr(provider, "invoke_structured"):
        return result
    config = getattr(provider, "config", {}) if provider is not None else {}
    if not isinstance(config, dict) or not config.get("enabled", False):
        return result
    try:
        from pathlib import Path as _Path
        request = {
            "task_id": "mmf-capability-probe",
            "system_prompt": "Return one JSON object. Do not use tools.",
            "prompt": 'Return exactly this JSON object: {"probe":true,"ok":true}',
            "purpose": "capability_probe",
            "generation_mode": "capability_probe",
            "mock_response": {"probe": True, "ok": True},
            "reasoning_effort": "low",
            "enable_thinking": False,
            "max_tokens": 128,
            "timeout_seconds": 30,
            "agent_max_turns": 1,
            "tools": [],
            "required_keys": ["probe", "ok"],
            "json_schema": {"type": "object", "required": ["probe", "ok"], "properties": {"probe": {"const": True}, "ok": {"const": True}}},
        }
        with tempfile.TemporaryDirectory(prefix="mmf-cap-probe-") as tmp:
            output = provider.invoke_structured(request, _Path(tmp) / "probe")
        meta = output.get("provider_metadata") if isinstance(output, dict) else {}
        if isinstance(output, dict) and output.get("probe") is True and output.get("ok") is True:
            result["structured_response"] = "accepted"
        else:
            result["structured_response"] = "rejected"
        if isinstance(meta, dict) and (meta.get("input_tokens") is not None or meta.get("output_tokens") is not None):
            result["token_usage_availability"] = "present"
        else:
            result["token_usage_availability"] = "absent"
        # A single ping does not prove native reasoning ceilings.
        result["native_reasoning_acceptance"] = "unknown"
    except Exception as exc:
        result["structured_response"] = "rejected"
        result["probe_error"] = type(exc).__name__
        result["status"] = classify_probe_failure(exc, str(exc))
    return result


def observe_from_health(
    provider: Any,
    health: dict[str, Any] | None,
    *,
    store_path: Path | None = None,
    force: bool = False,
    run_synthetic: bool | None = None,
) -> dict[str, Any]:
    """Record what a live health/connection probe actually exposed.

    A single /models or CLI health success does not prove max context, TPM,
    RPM, or native reasoning ceilings.
    """
    config = getattr(provider, "config", {}) if provider is not None else {}
    if not isinstance(config, dict):
        config = {}
    provider_name = str(config.get("provider_name") or "unknown")
    model = str(config.get("model") or config.get("model_alias") or "")
    key = observation_key(provider_name, model)
    health = health or {}
    incoming_negative = (
        health.get("available") is False
        or health.get("status") in {"not_configured", "not_installed", "unavailable", "AUTH_REQUIRED", "UNAVAILABLE"}
        or not config.get("enabled", False)
        or health.get("configured") is False
    )
    with _LOCK:
        store = load_observations(store_path)
        cached = store.get(key) if isinstance(store.get(key), dict) else None
        if cached and _fresh(cached) and not force and not incoming_negative and cached.get("status") == "PASS":
            return {**cached, "fresh": True, "provenance": "cached"}

        if not config.get("enabled", False):
            row = empty_observation(provider_name, model, status="UNAVAILABLE", reason="provider_not_enabled")
            store[key] = row
            save_observations(store, store_path)
            return {**row, "fresh": False}

        if health.get("status") in {"not_configured", "not_installed"} or health.get("configured") is False:
            row = empty_observation(
                provider_name,
                model,
                status="UNAVAILABLE",
                reason=str(health.get("status") or "not_configured"),
            )
            store[key] = row
            save_observations(store, store_path)
            return {**row, "fresh": False}

        import time
        configured_model = model or "unknown"
        models_listed = []
        if isinstance(health.get("models"), list):
            models_listed = [str(item) for item in health["models"] if item]
        observed_model = configured_model
        token_usage = "unknown"
        structured = "unknown"
        reasoning_acceptance = "unknown"
        rate_meta: Any = "unknown"
        headers = health.get("rate_limit_headers")
        if isinstance(headers, dict) and headers:
            rate_meta = {
                "headers_present": True,
                "rpm": headers.get("x-ratelimit-limit-requests") or "unknown",
                "tpm": headers.get("x-ratelimit-limit-tokens") or "unknown",
            }
        probe_kind = "health_only"
        if health.get("available"):
            status = "PASS"
            provenance = "live_health_or_models_list"
            if str(config.get("provider_type") or "") == "mock" or config.get("test_mode"):
                provenance = "offline_mock_health"
            # A passive page/status read must never start a generation request.
            should_probe = bool(force if run_synthetic is None else run_synthetic)
            synthetic = _synthetic_probe(provider) if should_probe else {"probe_kind": "health_only"}
            structured = synthetic.get("structured_response") or structured
            token_usage = synthetic.get("token_usage_availability") or token_usage
            reasoning_acceptance = synthetic.get("native_reasoning_acceptance") or reasoning_acceptance
            probe_kind = str(synthetic.get("probe_kind") or "synthetic_structured_ping")
            if synthetic.get("structured_response") == "rejected":
                provenance = "health_pass_structured_probe_rejected"
                status = str(synthetic.get("status") or "OUTPUT_CONTRACT_ERROR")
        else:
            status = classify_probe_failure(None, str(health.get("message") or health.get("status") or ""))
            if status == "UNAVAILABLE" and health.get("status") in {"unavailable", "connection_failed"}:
                status = "UNAVAILABLE"
            provenance = "live_health_or_models_list"

        row = public_observation({
            "provider_name": provider_name,
            "observed_model": observed_model,
            "configured_model": configured_model,
            "listed_models": models_listed,
            "native_reasoning_acceptance": reasoning_acceptance,
            "structured_response": structured,
            "token_usage_availability": token_usage,
            "rate_limit_metadata": rate_meta,
            "provenance": provenance,
            "status": status,
            "reason": _sanitize_reason(health.get("message") or health.get("status") or ""),
            "observed_at": utc_now(),
            "observed_epoch": time.time(),
            "ttl_seconds": PROBE_TTL_SECONDS,
            "unverified_fields": list(UNVERIFIED_VENDOR_FIELDS),
            "inferred_from_model_prefix": False,
            "auth_state": health.get("authenticated") if "authenticated" in health else health.get("auth_state"),
            "probe_kind": probe_kind,
        })
        store[key] = row
        save_observations(store, store_path)
        return {**row, "fresh": False}


def get_cached_observation(provider_name: str, model: str = "", path: Path | None = None) -> dict[str, Any] | None:
    store = load_observations(path)
    row = store.get(observation_key(provider_name, model))
    if isinstance(row, dict) and _fresh(row):
        return {**row, "fresh": True}
    return None
