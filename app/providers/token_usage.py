from __future__ import annotations

import threading
from typing import Any


def _as_int(value: Any) -> int | None:
    if value is None or value is False:
        return None
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def _first_int(*values: Any) -> int | None:
    for value in values:
        parsed = _as_int(value)
        if parsed is not None:
            return parsed
    return None


def extract_token_usage(*sources: Any) -> dict[str, Any]:
    """Prefer actual provider usage, including explicit zero.

    Cache-read tokens are recorded separately and are not added on top of
    input_tokens. Cost is never treated as a verified price.
    """
    merged: dict[str, Any] = {}
    for source in sources:
        if isinstance(source, dict):
            merged.update(source)

    envelope = merged.get("envelope") if isinstance(merged.get("envelope"), dict) else merged
    usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
    model_usage = envelope.get("modelUsage") if isinstance(envelope.get("modelUsage"), dict) else {}
    # Bridge enforces a single expected model. Multiple modelUsage rows are not summed.
    if len(model_usage) == 1:
        first_model = next(iter(model_usage.values()), None)
        model_row = first_model if isinstance(first_model, dict) else {}
    else:
        model_row = {}
    meta = merged.get("provider_metadata") if isinstance(merged.get("provider_metadata"), dict) else merged
    openai_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}

    input_tokens = _first_int(
        meta.get("input_tokens"),
        usage.get("input_tokens"),
        usage.get("prompt_tokens"),
        model_row.get("inputTokens"),
        model_row.get("input_tokens"),
    )
    output_tokens = _first_int(
        meta.get("output_tokens"),
        usage.get("output_tokens"),
        usage.get("completion_tokens"),
        model_row.get("outputTokens"),
        model_row.get("output_tokens"),
    )
    cache_read = _first_int(
        meta.get("cache_read_tokens"),
        usage.get("cache_read_input_tokens"),
        usage.get("cached_tokens"),
        openai_details.get("cached_tokens"),
        model_row.get("cacheReadInputTokens"),
        model_row.get("cache_read_input_tokens"),
    ) or 0
    cache_creation = _first_int(
        meta.get("cache_creation_tokens"),
        usage.get("cache_creation_input_tokens"),
        model_row.get("cacheCreationInputTokens"),
    ) or 0
    reasoning_tokens = _first_int(
        meta.get("reasoning_tokens"),
        usage.get("reasoning_tokens"),
        model_row.get("reasoningTokens"),
    ) or 0

    actual_present = input_tokens is not None or output_tokens is not None
    zero_actual = actual_present and (input_tokens == 0 or output_tokens == 0) and (
        input_tokens is not None and output_tokens is not None
    )
    if actual_present:
        provenance = "provider_usage_zero" if (input_tokens == 0 and output_tokens == 0) else "provider_usage"
        if zero_actual and (input_tokens or output_tokens):
            provenance = "provider_usage"
    else:
        provenance = "unknown"

    reported_cost = _first_int(
        None,
    )
    cost_usd = None
    for raw in (model_row.get("costUSD"), usage.get("total_cost_usd"), meta.get("cost_usd"), envelope.get("total_cost_usd")):
        try:
            if raw is not None:
                cost_usd = float(raw)
                break
        except (TypeError, ValueError):
            continue

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": int(cache_read),
        "cache_creation_tokens": int(cache_creation),
        "reasoning_tokens": int(reasoning_tokens),
        "total_tokens_reported": _first_int(usage.get("total_tokens"), model_row.get("totalTokens")),
        "provenance": provenance,
        "cost_usd": cost_usd,
        "cost_provenance": "provider_reported_unverified" if cost_usd is not None else "unknown_unverified_pricing",
        "cache_excluded_from_input": True,
    }


def estimate_token_count(value: Any) -> int:
    text = value if isinstance(value, str) else str(value or "")
    return max(1, len(text) // 4)


def resolve_attempt_tokens(
    *,
    actual: dict[str, Any] | None,
    estimated_input: int,
    estimated_output: int,
) -> dict[str, Any]:
    actual = actual or {}
    input_actual = actual.get("input_tokens")
    output_actual = actual.get("output_tokens")
    input_is_actual = input_actual is not None
    output_is_actual = output_actual is not None
    if input_is_actual and output_is_actual:
        provenance = "provider_usage_zero" if input_actual == 0 and output_actual == 0 else "provider_usage"
        input_tokens = int(input_actual)
        output_tokens = int(output_actual)
    elif input_is_actual or output_is_actual:
        provenance = "mixed"
        input_tokens = int(input_actual) if input_is_actual else int(estimated_input)
        output_tokens = int(output_actual) if output_is_actual else int(estimated_output)
    else:
        provenance = "estimate"
        input_tokens = int(estimated_input)
        output_tokens = int(estimated_output)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "input_tokens_estimate": int(estimated_input),
        "output_tokens_estimate": int(estimated_output),
        "cache_read_tokens": int(actual.get("cache_read_tokens") or 0),
        "cache_creation_tokens": int(actual.get("cache_creation_tokens") or 0),
        "reasoning_tokens": int(actual.get("reasoning_tokens") or 0),
        "token_provenance": provenance,
        "cost_usd": None if actual.get("cost_provenance") == "unknown_unverified_pricing" else actual.get("cost_usd"),
        "cost_provenance": actual.get("cost_provenance") or "unknown_unverified_pricing",
    }


class TokenAccountant:
    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_creation_tokens = 0
        self.attempts = 0
        self._provenances: list[str] = []
        self._lock = threading.Lock()

    def add(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.attempts += 1
            self.input_tokens += int(row.get("input_tokens") or 0)
            self.output_tokens += int(row.get("output_tokens") or 0)
            self.cache_read_tokens += int(row.get("cache_read_tokens") or 0)
            self.cache_creation_tokens += int(row.get("cache_creation_tokens") or 0)
            self._provenances.append(str(row.get("token_provenance") or "unknown"))

    def restore(self, summary: dict[str, Any] | None) -> None:
        summary = summary or {}
        with self._lock:
            self.input_tokens = int(summary.get("input_tokens") or 0)
            self.output_tokens = int(summary.get("output_tokens") or 0)
            self.cache_read_tokens = int(summary.get("cache_read_tokens") or 0)
            self.cache_creation_tokens = int(summary.get("cache_creation_tokens") or 0)
            self.attempts = int(summary.get("attempt_count") or 0)
            provenance = str(summary.get("provenance") or "")
            self._provenances = [provenance] if provenance else []

    def summary(self) -> dict[str, Any]:
        with self._lock:
            unique = {item for item in self._provenances if item}
            if not unique:
                provenance = "unknown"
            elif unique <= {"provider_usage", "provider_usage_zero"}:
                provenance = "provider_usage"
            elif unique == {"estimate"}:
                provenance = "estimate"
            else:
                provenance = "mixed"
            return {
                "input_tokens": int(self.input_tokens),
                "output_tokens": int(self.output_tokens),
                "cache_read_tokens": int(self.cache_read_tokens),
                "cache_creation_tokens": int(self.cache_creation_tokens),
                "attempt_count": int(self.attempts),
                "provenance": provenance,
                "cost_usd": None,
                "cost_provenance": "unknown_unverified_pricing",
            }
