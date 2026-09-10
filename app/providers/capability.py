from __future__ import annotations

from typing import Any


def normalize_speed_profile(value: Any) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "快速": "fast", "fast": "fast", "quick": "fast",
        "均衡": "balanced", "balanced": "balanced", "standard": "balanced", "推荐": "balanced",
        "深度": "deep", "deep": "deep", "thorough": "deep",
    }
    return aliases.get(raw, "balanced")


def product_speed_catalog() -> list[dict[str, Any]]:
    return [
        {"id": "fast", "label": "快速", "summary": "适合内部初稿、快速验证和普通方案，优先控制等待时间和成本。", "recommended": False},
        {"id": "balanced", "label": "均衡", "summary": "质量、时间和成本平衡，默认推荐。", "recommended": True},
        {"id": "deep", "label": "深度", "summary": "适合复杂项目、重要方案和高风险章节，允许更高推理成本。", "recommended": False},
    ]


def policy_reasoning_for_stage(speed_profile: str, stage: str) -> str:
    draft = {"fast": "low", "balanced": "medium", "deep": "high"}[normalize_speed_profile(speed_profile)]
    stage_name = str(stage or "draft").lower()
    if stage_name in {"planning", "plan", "complex_repair", "commitment_repair"}:
        return "high"
    return draft


def _shift_level(level: str, delta: int, allowed: list[str]) -> str:
    if not allowed:
        return level
    order = ["low", "medium", "high", "xhigh"]
    if level not in allowed:
        idx = order.index(level) if level in order else 0
        level = sorted(allowed, key=lambda item: abs((order.index(item) if item in order else 99) - idx))[0]
    idx = allowed.index(level)
    return allowed[max(0, min(len(allowed) - 1, idx + delta))]


QUALITY_FAILURE_KINDS = {
    "QUALITY_FAILURE",
    "REQUIREMENT_MISS",
    "CONTRADICTION",
    "COMMITMENT_PROBLEM",
    "OUTPUT_CONTRACT_ERROR",
    "UNDER_LENGTH",
}
LATENCY_FAILURE_KINDS = {"LATENCY_FAILURE", "TIMEOUT", "SOFT_LATENCY_EXCEEDED"}

# Provider-native sequences are independent of product speed profiles (fast/balanced/deep).
NATIVE_REASONING_SEQUENCES = {
    "grok_build": ["low", "medium", "high", "xhigh"],
    "kimi_moonshot": ["low", "high", "max"],
    "glm_zhipu": ["low", "high", "max"],
    "qwen_modelstudio": ["disabled"],
    "mock": ["low", "medium", "high"],
}
CEILING_REASONING_LEVELS = {"xhigh", "max", "thinking_enabled"}
REPAIR_REASONING_STAGES = {
    "repair",
    "quality_retry",
    "complex_repair",
    "commitment_repair",
    "planning",
}
_NATIVE_ALIASES = {
    "medium": "high",
    "xhigh": "max",
    "fast": "low",
    "thinking_disabled": "disabled",
    "standard": "high",
}


# Model capability probing is deferred. A model-name prefix is not evidence of
# max support: only an explicit deployment capability configuration opts in.


def _kimi_native_sequence(config: dict[str, Any] | None) -> list[str]:
    config = config or {}
    extra = dict(config.get("extra_options") or {})
    explicit = extra.get("supported_reasoning_levels") or config.get("supported_reasoning_levels")
    if isinstance(explicit, (list, tuple)) and explicit:
        allowed = [str(item).strip().lower() for item in explicit if str(item).strip()]
        sequence = [level for level in ("low", "high", "max") if level in allowed]
        return sequence or ["low", "high"]
    return ["low", "high"]


def native_reasoning_sequence(provider_name: str, config: dict[str, Any] | None = None) -> list[str]:
    name = str(provider_name or "")
    extra = dict((config or {}).get("extra_options") or {})
    if name == "qwen_modelstudio" and extra.get("enable_thinking"):
        return ["disabled", "thinking_enabled"]
    if name in {"kimi_moonshot", "glm_zhipu"}:
        return _kimi_native_sequence(config)
    return list(NATIVE_REASONING_SEQUENCES.get(name) or [])


def _snap_native_level(provider_name: str, current: str, sequence: list[str]) -> str:
    if current in sequence:
        return current
    aliased = _NATIVE_ALIASES.get(current, current)
    if aliased in sequence:
        return aliased
    if provider_name in {"kimi_moonshot", "glm_zhipu"}:
        mapped = {"low": "low", "medium": "high", "high": "high", "xhigh": "max"}.get(current)
        if mapped in sequence:
            return mapped
    return sequence[0] if sequence else current


def next_higher_reasoning(
    provider_name: str,
    current_effective: str | None,
    *,
    stage: str = "draft",
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the next *native* reasoning level. Never invent unsupported API values."""
    current = str(current_effective or "").strip()
    stage_name = str(stage or "draft").lower()
    extra = dict((config or {}).get("extra_options") or {})
    sequence = native_reasoning_sequence(provider_name, config)
    provider = str(provider_name or "")
    if provider == "qwen_modelstudio" and not extra.get("enable_thinking"):
        snapped = current if current in {"disabled", "thinking_enabled"} else "disabled"
        return {
            "current": snapped,
            "next_level": snapped,
            "applied": False,
            "unavailable_reason": "REASONING_ESCALATION_UNAVAILABLE",
            "sequence": sequence,
        }
    if not sequence:
        return {
            "current": current,
            "next_level": current,
            "applied": False,
            "unavailable_reason": "QUALITY_ESCALATION_UNAVAILABLE",
            "sequence": sequence,
        }
    if current not in sequence:
        # Unknown/changed native capability must not turn an upgrade into a
        # downgrade by snapping the current value to the bottom of the ladder.
        return {"current": current, "next_level": current, "applied": False,
                "unavailable_reason": "QUALITY_ESCALATION_UNAVAILABLE", "sequence": sequence}
    snapped = current
    idx = sequence.index(snapped)
    if idx >= len(sequence) - 1:
        return {
            "current": snapped,
            "next_level": snapped,
            "applied": False,
            "unavailable_reason": "QUALITY_ESCALATION_UNAVAILABLE",
            "sequence": sequence,
        }
    candidate = sequence[idx + 1]
    if candidate in CEILING_REASONING_LEVELS and stage_name not in REPAIR_REASONING_STAGES:
        return {
            "current": snapped,
            "next_level": snapped,
            "applied": False,
            "unavailable_reason": "QUALITY_ESCALATION_UNAVAILABLE",
            "sequence": sequence,
        }
    return {
        "current": snapped,
        "next_level": candidate,
        "applied": True,
        "unavailable_reason": None,
        "sequence": sequence,
    }


def next_lower_reasoning(
    provider_name: str,
    current_effective: str | None,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sequence = native_reasoning_sequence(provider_name, config)
    current = str(current_effective or "").strip()
    if not sequence:
        return {"current": current, "next_level": current, "applied": False, "sequence": sequence}
    snapped = _snap_native_level(provider_name, current, sequence)
    idx = sequence.index(snapped)
    if idx <= 0:
        return {"current": snapped, "next_level": snapped, "applied": False, "sequence": sequence}
    return {"current": snapped, "next_level": sequence[idx - 1], "applied": True, "sequence": sequence}


def adjust_for_failure(level: str, failure_class: str | None, allowed: list[str]) -> tuple[str, str]:
    kind = str(failure_class or "").upper()
    if kind in QUALITY_FAILURE_KINDS:
        nxt = _shift_level(level, 1, allowed)
        if allowed:
            order = ["low", "medium", "high", "xhigh"]
            baseline = level if level in allowed else sorted(
                allowed,
                key=lambda item: abs((order.index(item) if item in order else 99) - (order.index(level) if level in order else 0)),
            )[0]
            if nxt == baseline:
                return nxt, "QUALITY_ESCALATION_UNAVAILABLE"
        return nxt, "QUALITY_ESCALATION"
    if kind in LATENCY_FAILURE_KINDS:
        return _shift_level(level, -1, allowed), "LATENCY_DOWNGRADE"
    return level, "NONE"


# Context windows, rate limits and token ceilings are conservative documented
# assumptions. They are not live-verified provider SLAs.
PROVIDER_LATENCY_BUDGETS: dict[str, dict[str, tuple[int, int]]] = {
    "qwen_modelstudio": {"fast": (90, 240), "balanced": (150, 360), "deep": (240, 480)},
    "kimi_moonshot": {"fast": (180, 420), "balanced": (300, 600), "deep": (420, 780)},
    "glm_zhipu": {"fast": (150, 360), "balanced": (240, 480), "deep": (360, 720)},
    "grok_build": {"fast": (240, 540), "balanced": (420, 900), "deep": (600, 1200)},
    "mock": {"fast": (8, 20), "balanced": (12, 30), "deep": (16, 40)},
}
MODEL_LATENCY_BUDGETS: dict[tuple[str, str], dict[str, tuple[int, int]]] = {
    ("qwen_modelstudio", "qwen-flash"): {"fast": (60, 180), "balanced": (90, 240), "deep": (150, 360)},
    ("qwen_modelstudio", "qwen3.7-plus"): {"fast": (90, 240), "balanced": (150, 360), "deep": (240, 480)},
    ("kimi_moonshot", "kimi-k3"): {"fast": (180, 420), "balanced": (300, 600), "deep": (420, 780)},
    ("glm_zhipu", "glm-4.7"): {"fast": (150, 360), "balanced": (240, 480), "deep": (360, 720)},
    ("glm_zhipu", "glm-5"): {"fast": (150, 360), "balanced": (240, 480), "deep": (360, 720)},
    ("glm_zhipu", "glm-5.3"): {"fast": (180, 420), "balanced": (300, 600), "deep": (420, 780)},
    ("grok_build", "grok-4.6"): {"fast": (240, 540), "balanced": (420, 900), "deep": (600, 1200)},
    ("grok_build", "grok-4.6-build"): {"fast": (240, 540), "balanced": (420, 900), "deep": (600, 1200)},
    ("mock", "mock-fast"): {"fast": (4, 10), "balanced": (6, 14), "deep": (8, 18)},
    ("mock", "mock-slow"): {"fast": (16, 40), "balanced": (24, 60), "deep": (32, 80)},
    ("mock", "deterministic-mock-v0.1"): {"fast": (8, 20), "balanced": (12, 30), "deep": (16, 40)},
}


def _config_model(config: dict[str, Any] | None) -> str:
    config = config or {}
    extra = dict(config.get("extra_options") or {})
    return str(config.get("model") or config.get("model_alias") or extra.get("model") or "").strip()


def latency_budget_seconds(
    provider_name: str,
    speed_profile: str,
    stage: str,
    batch_size: int = 1,
    model: str | None = None,
    config: dict[str, Any] | None = None,
    history_samples: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    mode = normalize_speed_profile(speed_profile)
    stage_name = str(stage or "draft").lower()
    size = max(1, int(batch_size or 1))
    config = config or {}
    extra = dict(config.get("extra_options") or {})
    exact_model = str(model or _config_model(config) or "").strip()
    override = extra.get("latency_budget") or config.get("latency_budget")
    provenance = "provider_family_assumed"
    assumed_unknown = False
    if isinstance(override, dict) and (override.get("soft") or override.get("soft_latency_budget") or override.get("hard") or override.get("hard_timeout")):
        soft = int(override.get("soft") or override.get("soft_latency_budget") or 0)
        hard = int(override.get("hard") or override.get("hard_timeout") or max(soft + 30, 1))
        provenance = "deployment_override"
    else:
        model_table = MODEL_LATENCY_BUDGETS.get((provider_name, exact_model)) if exact_model else None
        family = PROVIDER_LATENCY_BUDGETS.get(provider_name) or {"fast": (120, 300), "balanced": (180, 420), "deep": (300, 600)}
        if model_table:
            soft, hard = model_table.get(mode, model_table.get("balanced", (180, 420)))
            provenance = "model_specific_table"
        else:
            soft, hard = family.get(mode, family.get("balanced", (180, 420)))
            if exact_model:
                provenance = "unknown_model_assumed"
                assumed_unknown = True
            else:
                provenance = "provider_family_assumed"
        if extra.get("soft_latency_budget") is not None or config.get("soft_latency_budget") is not None:
            soft = float(extra.get("soft_latency_budget") if extra.get("soft_latency_budget") is not None else config.get("soft_latency_budget"))
            provenance = "deployment_override"
        if extra.get("hard_timeout") is not None or config.get("hard_timeout") is not None:
            hard = float(extra.get("hard_timeout") if extra.get("hard_timeout") is not None else config.get("hard_timeout"))
            provenance = "deployment_override"
        if stage_name in {"planning", "complex_repair", "commitment_repair"}:
            soft = soft * 1.6
            hard = hard * 1.8
        if extra.get("soft_latency_budget") is None and config.get("soft_latency_budget") is None:
            soft = soft * (1 + 0.25 * (size - 1))
            hard = hard * (1 + 0.2 * (size - 1))
            if float(soft).is_integer():
                soft = int(soft)
            if float(hard).is_integer():
                hard = int(hard)
    history_adjusted = False
    if history_samples and provenance != "deployment_override":
        elapsed = []
        for row in history_samples:
            if row.get("eligible_for_eta") is not True or row.get("success") is not True:
                continue
            if str(row.get("provider") or "") and str(row.get("provider")) != str(provider_name):
                continue
            row_model = str(row.get("model") or "unknown")
            want_model = exact_model or "unknown"
            if row_model != want_model:
                continue
            if str(row.get("mode") or "balanced") != mode:
                continue
            if str(row.get("stage") or "draft") != stage_name:
                continue
            if str(row.get("sample_scope") or "") != "provider_attempt":
                continue
            try:
                value = float(row.get("elapsed_seconds") or 0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                elapsed.append(value)
        elapsed = elapsed[-20:]
        if len(elapsed) >= 3:
            ordered = sorted(elapsed)
            p50 = ordered[min(len(ordered) - 1, max(0, round((len(ordered) - 1) * 0.50)))]
            p80 = ordered[min(len(ordered) - 1, max(0, round((len(ordered) - 1) * 0.80)))]
            soft = max(1, int(round(p50)))
            hard = max(soft + 30, int(round(p80 * 1.2)))
            provenance = "measured_recent_history_adjusted"
            history_adjusted = True
            assumed_unknown = False
    hard_value = max(float(soft) + 30, float(hard))
    soft_value = float(soft)
    if soft_value.is_integer():
        soft_value = int(soft_value)
    if float(hard_value).is_integer():
        hard_value = int(hard_value)
    return {
        "soft_latency_budget": soft_value,
        "hard_timeout": hard_value,
        "model": exact_model or "unknown",
        "provider_name": provider_name,
        "speed_profile": mode,
        "stage": stage_name,
        "batch_size": size,
        "provenance": provenance,
        "assumed_unknown_model": assumed_unknown,
        "history_adjusted": history_adjusted,
        "note": (
            "Unknown model uses provider-family assumed budgets; not invented vendor limits."
            if assumed_unknown
            else "Latency budgets are deployment/model tables or measured history, not live vendor SLAs."
        ),
    }


CAPABILITY_PROFILES: dict[str, dict[str, Any]] = {
    "qwen_modelstudio": {
        "thinking": False,
        "enable_thinking": False,
        "reasoning_effort": None,
        "supported_reasoning_levels": ["disabled", "fast"],
        "default_reasoning": "disabled",
        "max_output_tokens": 8192,
        "context_window": 128000,
        "supports_parallel": True,
        "recommended_parallelism": 2,
        "supports_cancel": True,
        "timeout_behavior": "request_timeout",
        "timeout": 300,
        "structured_output": True,
        "continuation_support": True,
        "continuation": True,
        "retry": True,
        "capability_assumption": "context/token/rate are conservative documented assumptions",
    },
    "kimi_moonshot": {
        "thinking": None,
        "enable_thinking": None,
        "reasoning_effort": "low",
        "supported_reasoning_levels": ["low", "high", "max"],
        "default_reasoning": "low",
        "max_output_tokens": 8192,
        "context_window": 128000,
        "supports_parallel": True,
        "recommended_parallelism": 2,
        "supports_cancel": True,
        "timeout_behavior": "request_timeout",
        "timeout": 420,
        "structured_output": True,
        "continuation_support": True,
        "continuation": True,
        "retry": True,
        "capability_assumption": "Kimi low/high/max are not equivalent to Grok low/medium/high/xhigh",
    },
    "glm_zhipu": {
        "thinking": None,
        "enable_thinking": None,
        "reasoning_effort": "low",
        "supported_reasoning_levels": ["low", "high", "max"],
        "default_reasoning": "low",
        "max_output_tokens": 8192,
        "context_window": 128000,
        "supports_parallel": True,
        "recommended_parallelism": 2,
        "supports_cancel": True,
        "timeout_behavior": "request_timeout",
        "timeout": 420,
        "structured_output": True,
        "continuation_support": True,
        "continuation": True,
        "retry": True,
        "capability_assumption": "GLM reasoning_effort low/high/max; thinking type enabled for high/max",
    },
    "grok_build": {
        "thinking": None,
        "enable_thinking": None,
        "reasoning_effort": "low",
        "supported_reasoning_levels": ["low", "medium", "high", "xhigh"],
        "default_reasoning": "low",
        "max_output_tokens": 16384,
        "context_window": 128000,
        "supports_parallel": True,
        "recommended_parallelism": 2,
        "supports_cancel": True,
        "timeout_behavior": "request_timeout",
        "timeout": 600,
        "structured_output": True,
        "continuation_support": True,
        "continuation": True,
        "retry": True,
        "xhigh_allowed_stages": ["planning", "complex_repair", "commitment_repair", "repair", "quality_retry"],
        "capability_assumption": "xhigh is reserved for planning/complex/quality repair, not draft writing",
    },
    "mock": {
        "thinking": False,
        "enable_thinking": False,
        "reasoning_effort": "low",
        "supported_reasoning_levels": ["low", "medium", "high"],
        "default_reasoning": "low",
        "max_output_tokens": 8192,
        "context_window": 32000,
        "supports_parallel": True,
        "recommended_parallelism": 3,
        "supports_cancel": True,
        "timeout_behavior": "request_timeout",
        "timeout": 30,
        "structured_output": True,
        "continuation_support": True,
        "continuation": True,
        "retry": True,
        "capability_assumption": "offline mock profile",
    },
}

PROFILES = CAPABILITY_PROFILES

_PRODUCT_TO_KIMI = {"low": "low", "medium": "high", "high": "high", "xhigh": "max"}
_PRODUCT_TO_QWEN_THINKING = {"low": False, "medium": False, "high": True, "xhigh": True}


def _map_provider_reasoning(provider_name: str, product_level: str, stage: str, base: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    allowed = native_reasoning_sequence(provider_name, config) or list(base.get("supported_reasoning_levels") or [])
    stage_name = str(stage or "draft").lower()
    mapped: dict[str, Any] = {
        "product_reasoning_level": product_level,
        "ui_thinking_enabled": False,
        "ui_reasoning_label": "标准写作",
    }
    if provider_name == "qwen_modelstudio":
        mapped["enable_thinking"] = False
        mapped["reasoning_effort"] = None
        mapped["effective_reasoning"] = "disabled"
        mapped["ui_thinking_enabled"] = False
        mapped["ui_reasoning_label"] = "当前模型按快速写作运行（未启用思维链）"
        return mapped
    if provider_name in {"kimi_moonshot", "glm_zhipu"}:
        effort = _PRODUCT_TO_KIMI.get(product_level, "high")
        if effort not in allowed:
            effort = "high" if "high" in allowed else (allowed[0] if allowed else "low")
        if stage_name in {"planning", "complex_repair"} and product_level == "xhigh" and "max" in allowed:
            effort = "max"
        mapped["enable_thinking"] = None
        mapped["reasoning_effort"] = effort
        mapped["effective_reasoning"] = effort
        mapped["ui_reasoning_label"] = {"low": "快速写作", "high": "标准推理", "max": "深度推理"}.get(effort, effort)
        mapped["ui_thinking_enabled"] = False
        return mapped
    if provider_name == "grok_build":
        effort = product_level
        if effort not in allowed:
            effort = "low"
        if effort == "xhigh" and stage_name not in set(base.get("xhigh_allowed_stages") or []):
            effort = "high" if "high" in allowed else "low"
        if stage_name == "draft" and effort in {"xhigh"}:
            effort = "high" if product_level == "high" else "low"
        mapped["enable_thinking"] = None
        mapped["reasoning_effort"] = effort
        mapped["effective_reasoning"] = effort
        mapped["ui_reasoning_label"] = {"low": "快速写作", "medium": "均衡写作", "high": "深度写作", "xhigh": "最高推理"}.get(effort, effort)
        mapped["ui_thinking_enabled"] = False
        return mapped
    effort = product_level if product_level in allowed else (base.get("default_reasoning") or "low")
    mapped["enable_thinking"] = False
    mapped["reasoning_effort"] = effort
    mapped["effective_reasoning"] = effort
    mapped["ui_reasoning_label"] = "测试引擎"
    mapped["ui_thinking_enabled"] = False
    return mapped


def _apply_native_to_mapped(provider_name: str, native: str, mapped: dict[str, Any]) -> dict[str, Any]:
    updated = dict(mapped)
    updated["effective_reasoning"] = native
    if provider_name == "qwen_modelstudio":
        thinking = native in {"thinking_enabled", "true"} or native is True
        updated["enable_thinking"] = bool(thinking)
        updated["reasoning_effort"] = None
        updated["ui_thinking_enabled"] = bool(thinking)
        updated["ui_reasoning_label"] = "思维链已启用" if thinking else "当前模型按快速写作运行（未启用思维链）"
        return updated
    if provider_name in {"kimi_moonshot", "glm_zhipu"}:
        updated["enable_thinking"] = None
        updated["reasoning_effort"] = native
        updated["ui_reasoning_label"] = {"low": "快速写作", "high": "标准推理", "max": "深度推理"}.get(native, native)
        updated["ui_thinking_enabled"] = False
        return updated
    if provider_name == "grok_build":
        updated["enable_thinking"] = None
        updated["reasoning_effort"] = native
        updated["ui_reasoning_label"] = {
            "low": "快速写作",
            "medium": "均衡写作",
            "high": "深度写作",
            "xhigh": "最高推理",
        }.get(native, native)
        updated["ui_thinking_enabled"] = False
        return updated
    updated["reasoning_effort"] = native
    updated["enable_thinking"] = False
    return updated


def resolve_profile(
    provider_name: str,
    config: dict[str, Any] | None = None,
    *,
    speed_profile: str | None = None,
    stage: str = "draft",
    failure_class: str | None = None,
    batch_size: int = 1,
    history_samples: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    config = config or {}
    base = dict(CAPABILITY_PROFILES.get(provider_name) or CAPABILITY_PROFILES["mock"])
    extra = dict(config.get("extra_options") or {})
    mode = normalize_speed_profile(speed_profile or config.get("speed_profile") or "balanced")
    product_level = policy_reasoning_for_stage(mode, stage)
    mapped = _map_provider_reasoning(provider_name, product_level, stage, base, config=config)
    latency = latency_budget_seconds(
        provider_name,
        mode,
        stage,
        batch_size,
        model=config.get("model") or config.get("model_alias"),
        config=config,
        history_samples=history_samples,
    )
    requested_product = policy_reasoning_for_stage(mode, stage)
    requested = {
        "reasoning_mode": config.get("reasoning_mode"),
        "enable_thinking": extra.get("enable_thinking"),
        "reasoning_effort": extra.get("reasoning_effort") or config.get("reasoning_mode"),
        "timeout": config.get("timeout"),
        "max_tokens": config.get("max_tokens"),
        "model": config.get("model") or config.get("model_alias"),
        "speed_profile": mode,
        "stage": stage,
        "product_reasoning_level": requested_product,
        "failure_class": failure_class,
        "quality_escalation_requested": False,
    }
    enable_thinking = mapped.get("enable_thinking")
    if extra.get("enable_thinking") is not None and provider_name == "qwen_modelstudio":
        enable_thinking = bool(extra.get("enable_thinking"))
        mapped["enable_thinking"] = enable_thinking
        mapped["effective_reasoning"] = "thinking_enabled" if enable_thinking else "disabled"
        mapped["ui_thinking_enabled"] = bool(enable_thinking)
        mapped["ui_reasoning_label"] = "思维链已启用" if enable_thinking else "当前模型按快速写作运行（未启用思维链）"
    current_effective = str(mapped.get("effective_reasoning") or "")
    reasoning_before = current_effective
    reasoning_after = current_effective
    action = "NONE"
    escalation_requested = False
    escalation_applied = False
    unavailable_reason = None
    kind = str(failure_class or "").upper()
    if kind in QUALITY_FAILURE_KINDS:
        escalation_requested = True
        requested["quality_escalation_requested"] = True
        nxt = next_higher_reasoning(provider_name, current_effective, stage=stage, config=config)
        if nxt.get("applied") and nxt.get("next_level") and nxt["next_level"] != current_effective:
            escalation_applied = True
            action = "QUALITY_ESCALATION"
            reasoning_after = str(nxt["next_level"])
            mapped = _apply_native_to_mapped(provider_name, reasoning_after, mapped)
            enable_thinking = mapped.get("enable_thinking")
        else:
            action = str(nxt.get("unavailable_reason") or "QUALITY_ESCALATION_UNAVAILABLE")
            unavailable_reason = action
            reasoning_after = current_effective
            escalation_applied = False
    elif kind in LATENCY_FAILURE_KINDS:
        lower = next_lower_reasoning(provider_name, current_effective, config=config)
        if lower.get("applied") and lower.get("next_level") != current_effective:
            action = "LATENCY_DOWNGRADE"
            reasoning_after = str(lower["next_level"])
            mapped = _apply_native_to_mapped(provider_name, reasoning_after, mapped)
            enable_thinking = mapped.get("enable_thinking")
        else:
            action = "LATENCY_DOWNGRADE"
            reasoning_after = current_effective
    timeout = int(config.get("timeout") or latency["hard_timeout"] or base["timeout"])
    effective = {
        "enable_thinking": enable_thinking if enable_thinking is not None else base.get("enable_thinking"),
        "reasoning_effort": mapped.get("reasoning_effort"),
        "effective_reasoning": mapped.get("effective_reasoning"),
        "timeout": timeout,
        "max_output_tokens": int(config.get("max_tokens") or base["max_output_tokens"]),
        "model": requested["model"],
        "continuation": True,
        "retry": True,
        "speed_profile": mode,
        "stage": stage,
        "product_reasoning_level": requested_product,
        "reasoning_adjustment": action,
        "ui_thinking_enabled": bool(mapped.get("ui_thinking_enabled")),
        "ui_reasoning_label": mapped.get("ui_reasoning_label"),
        "soft_latency_budget": latency["soft_latency_budget"],
        "hard_timeout": latency["hard_timeout"],
        "latency_budget_provenance": latency.get("provenance"),
        "latency_budget_model": latency.get("model"),
        "latency_budget_assumed_unknown_model": bool(latency.get("assumed_unknown_model")),
        "context_window": int(base.get("context_window") or 32000),
        "supports_parallel": bool(base.get("supports_parallel")),
        "recommended_parallelism": int(base.get("recommended_parallelism") or 1),
        "supports_cancel": bool(base.get("supports_cancel")),
        "structured_output": True,
        "capability_assumption": base.get("capability_assumption"),
        "quality_escalation_requested": escalation_requested,
        "quality_escalation_applied": escalation_applied,
        "reasoning_before": reasoning_before,
        "reasoning_after": reasoning_after,
        "escalation_unavailable_reason": unavailable_reason,
        "native_reasoning_sequence": native_reasoning_sequence(provider_name, config),
    }
    try:
        from .capability_probe import get_cached_observation
        observation = get_cached_observation(provider_name, str(requested.get("model") or ""))
        if observation:
            effective["capability_observation"] = {
                "status": observation.get("status"),
                "observed_model": observation.get("observed_model"),
                "structured_response": observation.get("structured_response"),
                "token_usage_availability": observation.get("token_usage_availability"),
                "unverified_fields": observation.get("unverified_fields"),
                "provenance": observation.get("provenance"),
            }
            # Cached probe never auto-overrides unsupported native capabilities.
    except Exception:
        pass
    if provider_name == "qwen_modelstudio":
        effective["reasoning_mode"] = "thinking_enabled" if effective.get("enable_thinking") else "thinking_disabled"
    if provider_name in {"kimi_moonshot", "glm_zhipu"}:
        seq = effective["native_reasoning_sequence"]
        effective["native_sequence_assumption"] = (
            "explicit_deployment_native_levels_not_live_probed"
            if "max" in seq
            else "kimi_unknown_or_unlisted_model_high_ceiling_not_live_probed"
        )
    return {
        "provider_name": provider_name,
        "requested_settings": requested,
        "effective_settings": effective,
        "profile": base,
        "speed_profiles": product_speed_catalog(),
        "planning_note": "Planning uses a local deterministic planner; high reasoning is policy only unless a provider planning call is actually issued.",
    }


def apply_to_request(request: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    payload = dict(request)
    effective = profile.get("effective_settings") or {}
    payload["generation_mode"] = payload.get("generation_mode") or "longform_section"
    if effective.get("reasoning_effort"):
        payload["reasoning_effort"] = effective["reasoning_effort"]
    if effective.get("enable_thinking") is not None:
        payload["enable_thinking"] = effective["enable_thinking"]
    payload["timeout_seconds"] = int(effective.get("hard_timeout") or effective.get("timeout") or payload.get("timeout_seconds") or 300)
    payload["max_tokens"] = int(effective.get("max_output_tokens") or payload.get("max_tokens") or 8192)
    payload["speed_profile"] = effective.get("speed_profile")
    payload["stage"] = effective.get("stage") or payload.get("stage") or "draft"
    payload["capability_profile"] = {
        "requested_settings": profile.get("requested_settings"),
        "effective_settings": effective,
    }
    return payload


def recommended_parallelism(provider_name: str, requested: int | None = None) -> int:
    base = CAPABILITY_PROFILES.get(provider_name) or CAPABILITY_PROFILES["mock"]
    provider_max = int(base.get("recommended_parallelism") or 1)
    value = int(requested or 3)
    return max(1, min(3, value, provider_max if provider_name != "mock" else max(provider_max, value)))
