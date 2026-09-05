from __future__ import annotations

from typing import Any


PROFILES: dict[str, dict[str, Any]] = {
    "qwen_modelstudio": {
        "thinking": False,
        "enable_thinking": False,
        "reasoning_effort": None,
        "max_output_tokens": 8192,
        "timeout": 300,
        "structured_output": True,
        "continuation": True,
        "retry": True,
    },
    "kimi_moonshot": {
        "thinking": None,
        "enable_thinking": None,
        "reasoning_effort": "high",
        "max_output_tokens": 8192,
        "timeout": 420,
        "structured_output": True,
        "continuation": True,
        "retry": True,
    },
    "grok_build": {
        "thinking": None,
        "enable_thinking": None,
        "reasoning_effort": "xhigh",
        "max_output_tokens": 16384,
        "timeout": 600,
        "structured_output": True,
        "continuation": True,
        "retry": True,
    },
    "mock": {
        "thinking": False,
        "enable_thinking": False,
        "reasoning_effort": None,
        "max_output_tokens": 4096,
        "timeout": 30,
        "structured_output": True,
        "continuation": True,
        "retry": True,
    },
}


def resolve_profile(provider_name: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    base = dict(PROFILES.get(provider_name) or PROFILES["mock"])
    extra = dict(config.get("extra_options") or {})
    requested = {
        "reasoning_mode": config.get("reasoning_mode"),
        "enable_thinking": extra.get("enable_thinking"),
        "reasoning_effort": extra.get("reasoning_effort") or config.get("reasoning_mode"),
        "timeout": config.get("timeout"),
        "max_tokens": config.get("max_tokens"),
        "model": config.get("model") or config.get("model_alias"),
    }
    effective = {
        "enable_thinking": base["enable_thinking"] if extra.get("enable_thinking") is None else extra.get("enable_thinking"),
        "reasoning_effort": base["reasoning_effort"],
        "timeout": int(base["timeout"]),
        "max_output_tokens": int(base["max_output_tokens"]),
        "model": requested["model"],
        "continuation": True,
        "retry": True,
    }
    if provider_name == "qwen_modelstudio":
        effective["enable_thinking"] = bool(extra.get("enable_thinking", False))
        effective["reasoning_mode"] = "thinking_disabled" if not effective["enable_thinking"] else "thinking_enabled"
    if provider_name == "kimi_moonshot":
        effort = str(base["reasoning_effort"] or "high")
        if effort not in {"low", "high", "max"}:
            effort = "high"
        effective["reasoning_effort"] = effort
    if provider_name == "grok_build":
        effective["reasoning_effort"] = str(config.get("reasoning_mode") or base["reasoning_effort"] or "xhigh")
    return {
        "provider_name": provider_name,
        "requested_settings": requested,
        "effective_settings": effective,
        "profile": base,
    }


def apply_to_request(request: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    payload = dict(request)
    effective = profile.get("effective_settings") or {}
    payload["generation_mode"] = payload.get("generation_mode") or "longform_section"
    if effective.get("reasoning_effort"):
        payload["reasoning_effort"] = effective["reasoning_effort"]
    if effective.get("enable_thinking") is not None:
        payload["enable_thinking"] = effective["enable_thinking"]
    payload["timeout_seconds"] = int(effective.get("timeout") or payload.get("timeout_seconds") or 300)
    payload["max_tokens"] = int(effective.get("max_output_tokens") or payload.get("max_tokens") or 8192)
    payload["capability_profile"] = {
        "requested_settings": profile.get("requested_settings"),
        "effective_settings": effective,
    }
    return payload
