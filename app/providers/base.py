from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class ProviderError(RuntimeError):
    error_code = "PROVIDER_RUNTIME_ERROR"

    def __init__(self, message: str, *, error_code: str | None = None):
        super().__init__(message)
        self.error_code = error_code or self.error_code


class ProviderUnavailableError(ProviderError):
    error_code = "PROVIDER_UNAVAILABLE"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


FILLABLE_EMPTY_LISTS = {
    "proposed_items",
    "proposed_conflicts",
    "proposed_boilerplate",
    "proposed_scoring_items",
    "chunk_missing_facts",
    "recommended_positive",
    "applicable_guardrails",
    "missing_information",
    "citation_registry",
    "guardrail_non_use",
    "clarification_list",
}

FIELD_ALIASES = {
    "chunk_id": ("chunkId", "id", "chunk"),
    "proposed_items": ("items", "requirements", "candidates", "proposed_requirements"),
    "proposed_conflicts": ("conflicts",),
    "proposed_boilerplate": ("boilerplate",),
    "proposed_scoring_items": ("scoring_items", "scoring"),
    "chunk_missing_facts": ("missing_facts", "missing_information"),
    "recommended_positive": ("recommended", "positives", "positive_kus", "knowledge"),
    "applicable_guardrails": ("guardrails", "risk_controls", "applicableGuardrails"),
    "missing_information": ("missing", "missing_facts", "gaps"),
}


def _message_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("content", "reasoning_content", "text"):
            text = _message_text(value.get(key))
            if text.strip():
                return text
        return ""
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(_message_text(item.get("text") or item.get("content") or item))
            else:
                parts.append(str(item or ""))
        return "\n".join(parts)
    return str(value)


def _apply_aliases(row: dict[str, Any]) -> dict[str, Any]:
    filled = dict(row)
    for canonical, aliases in FIELD_ALIASES.items():
        if canonical in filled:
            continue
        for alias in aliases:
            if alias in filled:
                filled[canonical] = filled[alias]
                break
    return filled


def _fill_required(row: dict[str, Any], required: set[str], defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    filled = _apply_aliases(row)
    defaults = defaults or {}
    for key in required:
        if key in filled:
            continue
        if key in FILLABLE_EMPTY_LISTS:
            filled[key] = []
        elif key in defaults:
            filled[key] = defaults[key]
    return filled


def _walk_objects(value: Any, found: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    found = found if found is not None else []
    if isinstance(value, dict):
        found.append(value)
        for child in value.values():
            _walk_objects(child, found)
    elif isinstance(value, list):
        for child in value:
            _walk_objects(child, found)
    return found


def parse_json_object(value: Any, required_keys: set[str] | None = None, defaults: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    required = set(required_keys or [])
    defaults = defaults or {}
    candidates: list[dict[str, Any]] = []
    if isinstance(value, dict):
        candidates = _walk_objects(value)
    else:
        text = _message_text(value).strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = re.sub(r"\n```\s*$", "", text)
        start = text.find("{")
        if start < 0:
            raise ProviderError("Provider未返回结构化JSON。", error_code="OUTPUT_CONTRACT_ERROR")
        decoder = json.JSONDecoder()
        cursor = start
        while cursor < len(text):
            next_start = text.find("{", cursor)
            if next_start < 0:
                break
            try:
                parsed, consumed = decoder.raw_decode(text[next_start:])
            except json.JSONDecodeError:
                cursor = next_start + 1
                continue
            if isinstance(parsed, dict):
                candidates.extend(_walk_objects(parsed))
            cursor = next_start + consumed
    if not candidates:
        raise ProviderError("Provider结构化结果不是JSON对象。", error_code="OUTPUT_CONTRACT_ERROR")

    def score(row: dict[str, Any]) -> tuple[int, int]:
        repaired = _fill_required(row, required, defaults)
        matched = sum(1 for key in required if key in repaired)
        extra = len(repaired) - matched
        # Prefer objects that contain the required keys. Nested item dicts often
        # have more keys than the root, so extra keys must not win.
        return (matched, -extra if required else len(repaired))

    ranked = sorted(candidates, key=score)
    selected = ranked[-1]
    repaired = _fill_required(selected, required, defaults)
    if required and not required <= set(repaired):
        missing = ", ".join(sorted(required - set(repaired)))
        raise ProviderError(f"模型返回格式不完整，缺少：{missing}", error_code="OUTPUT_CONTRACT_ERROR")
    repair = None
    if repaired != selected or len(candidates) > 1:
        repair = {
            "applied": True,
            "type": "structured_payload_repair",
            "candidate_count": len(candidates),
            "filled_keys": sorted(set(repaired) - set(selected)),
            "business_text_changed": False,
        }
    return repaired, repair


def normalize_recommendation(result: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    expected = {"recommended_positive", "applicable_guardrails", "missing_information"}
    repaired, _ = parse_json_object(result, expected)
    payload = {key: repaired.get(key, []) for key in expected}
    if not isinstance(payload["recommended_positive"], list):
        raise ProviderError("recommended_positive必须是数组。")
    if not isinstance(payload["applicable_guardrails"], list):
        raise ProviderError("applicable_guardrails必须是数组。")
    if not isinstance(payload["missing_information"], list):
        raise ProviderError("missing_information必须是数组。")
    return {**payload, "provider_metadata": metadata}


def _as_generation_payload(row: dict[str, Any]) -> dict[str, Any]:
    if isinstance(row.get("artifact"), dict):
        return row
    if any(key in row for key in ("title", "sections", "slides", "lead", "subtitle")):
        extras = {"citation_registry", "guardrail_non_use", "clarification_list"}
        return {
            "artifact": {key: value for key, value in row.items() if key not in extras},
            "citation_registry": row.get("citation_registry") or [],
            "guardrail_non_use": row.get("guardrail_non_use") or [],
            "clarification_list": row.get("clarification_list") or [],
        }
    return row


def normalize_generation(result: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    expected = {"artifact", "citation_registry", "guardrail_non_use", "clarification_list"}
    repaired, _ = parse_json_object(_as_generation_payload(result if isinstance(result, dict) else {}), expected)
    payload = {
        "artifact": repaired.get("artifact") if isinstance(repaired.get("artifact"), dict) else _as_generation_payload(repaired).get("artifact", {}),
        "citation_registry": repaired.get("citation_registry") if isinstance(repaired.get("citation_registry"), list) else [],
        "guardrail_non_use": repaired.get("guardrail_non_use") if isinstance(repaired.get("guardrail_non_use"), list) else [],
        "clarification_list": repaired.get("clarification_list") if isinstance(repaired.get("clarification_list"), list) else [],
    }
    if not isinstance(payload["artifact"], dict) or not payload["artifact"]:
        raise ProviderError("正式生成结果缺少方案正文。")
    return {**payload, "provider_metadata": metadata}


class AIProvider(ABC):
    provider_version = "0.1"

    def __init__(self, config: dict[str, Any], credential_store: Any = None):
        self.config = config
        self.credential_store = credential_store

    @abstractmethod
    def recommend_knowledge(self, request: dict[str, Any], task_dir: Path) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def generate_solution(self, request: dict[str, Any], task_dir: Path) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> dict[str, Any]:
        raise NotImplementedError

    def invoke_structured(self, request: dict[str, Any], task_dir: Path) -> dict[str, Any]:
        raise ProviderError(f"{self.config.get('provider_name', 'Provider')}暂不支持结构化通用调用。")

    def get_metadata(self) -> dict[str, Any]:
        return {
            "provider_name": self.config["provider_name"],
            "provider_type": self.config["provider_type"],
            "model": self.config.get("model", ""),
            "endpoint_alias": self.config.get("endpoint_alias", "local"),
            "provider_version": self.provider_version,
            "reasoning_mode": self.config.get("reasoning_mode", ""),
            "fallback_used": False,
            "fallback_reason": None,
            "test_mode": bool(self.config.get("test_mode", False)),
        }

    def _task_metadata(self, task_id: str, **extra: Any) -> dict[str, Any]:
        return {**self.get_metadata(), "task_id": task_id, **extra}
