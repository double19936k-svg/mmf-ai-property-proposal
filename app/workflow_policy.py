"""Single source of truth for generation-entry and knowledge optionality."""

from __future__ import annotations

from typing import Any, Iterable

KNOWLEDGE_RECOMMENDATION_IS_OPTIONAL = True

NO_RECOMMENDATION = "NO_RECOMMENDATION"
RECOMMENDATION_AVAILABLE = "RECOMMENDATION_AVAILABLE"
USER_SELECTED_NONE = "USER_SELECTED_NONE"
USER_SELECTED_SOME = "USER_SELECTED_SOME"
PROVIDER_FAILED = "PROVIDER_FAILED"
INTERNAL_ERROR = "INTERNAL_ERROR"

BLOCKING_KNOWLEDGE_STATES = {INTERNAL_ERROR}
OPTIONAL_EMPTY_STATES = {NO_RECOMMENDATION, USER_SELECTED_NONE, PROVIDER_FAILED}

UNUSED_KNOWLEDGE_HINT = "本次未使用推荐知识，将仅基于项目资料与确认需求生成。"


def classify_knowledge_state(
    *,
    recommended_count: int,
    selected_count: int | None = None,
    provider_failed: bool = False,
    internal_error: bool = False,
    confirmed: bool = False,
) -> str:
    if internal_error:
        return INTERNAL_ERROR
    if provider_failed:
        return PROVIDER_FAILED
    if int(recommended_count or 0) <= 0:
        return NO_RECOMMENDATION
    if not confirmed:
        return RECOMMENDATION_AVAILABLE
    if int(selected_count or 0) <= 0:
        return USER_SELECTED_NONE
    return USER_SELECTED_SOME


def generation_may_proceed(knowledge_status: str) -> bool:
    if not KNOWLEDGE_RECOMMENDATION_IS_OPTIONAL and knowledge_status in OPTIONAL_EMPTY_STATES:
        return False
    return knowledge_status not in BLOCKING_KNOWLEDGE_STATES


def resolve_selected_positive_ids(
    recommended_ids: Iterable[str] | None,
    submitted_ids: Iterable[str] | None,
) -> list[str]:
    allowed = {str(item) for item in (recommended_ids or []) if str(item)}
    return list(dict.fromkeys(str(item) for item in (submitted_ids or []) if str(item) in allowed))


def knowledge_user_hint(knowledge_status: str) -> str:
    if knowledge_status in OPTIONAL_EMPTY_STATES:
        return UNUSED_KNOWLEDGE_HINT
    return ""


def knowledge_audit(
    *,
    recommended_count: int,
    selected_count: int,
    knowledge_status: str,
    provider_failed: bool = False,
    warning: str | None = None,
) -> dict[str, Any]:
    return {
        "KNOWLEDGE_RECOMMENDATION_IS_OPTIONAL": True,
        "knowledge_required": False,
        "knowledge_status": knowledge_status,
        "knowledge_used": int(selected_count or 0) > 0,
        "recommended_knowledge_count": int(recommended_count or 0),
        "selected_knowledge_count": int(selected_count or 0),
        "knowledge_provider_failed": bool(provider_failed),
        "knowledge_warning": warning or None,
        "knowledge_hint": knowledge_user_hint(knowledge_status),
    }


def attach_recommendation_state(
    selection: dict[str, Any],
    *,
    provider_failed: bool = False,
    warning: str | None = None,
    selected_ids: list[str] | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    recommended = [row for row in (selection.get("recommended_positive") or []) if isinstance(row, dict)]
    selected = list(selected_ids or [])
    status = classify_knowledge_state(
        recommended_count=len(recommended),
        selected_count=len(selected),
        provider_failed=provider_failed,
        confirmed=confirmed,
    )
    selection.update(knowledge_audit(
        recommended_count=len(recommended),
        selected_count=len(selected),
        knowledge_status=status,
        provider_failed=provider_failed,
        warning=warning,
    ))
    return selection


def assert_generation_entry_allowed(knowledge_status: str) -> None:
    if generation_may_proceed(knowledge_status):
        return
    raise ValueError(f"generation blocked by knowledge subsystem status: {knowledge_status}")
