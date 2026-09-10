from __future__ import annotations

import re
from typing import Any


_REPEAT_PATTERNS = (
    (re.compile(r"(如){2,}采用"), "如采用"),
    (re.compile(r"(如){2,}经确认"), "如经确认"),
    (re.compile(r"如采用如采用"), "如采用"),
    (re.compile(r"如经确认使用如经确认使用"), "如经确认使用"),
    (re.compile(r"(按){2,}(?:当前)?项目确认"), "按项目确认"),
    (re.compile(r"按按项目确认"), "按项目确认"),
    (re.compile(r"外委方式方式"), "外委方式"),
    (re.compile(r"方式方式"), "方式"),
)

_WAWEI_ALREADY = re.compile(r"(?:如采用|如经确认使用)外委方式")


def collapse_repeated_conditionals(text: str) -> str:
    value = str(text or "")
    previous = None
    while previous != value:
        previous = value
        for pattern, repl in _REPEAT_PATTERNS:
            value = pattern.sub(repl, value)
    return value


def replace_once(text: str, old: str, new: str) -> str:
    if not old:
        return text
    if new and new in text:
        return text
    return text.replace(old, new)


def replace_outsourcing_phrase(text: str) -> str:
    value = str(text or "")
    if _WAWEI_ALREADY.search(value):
        return value
    return re.sub(r"(?<!如采用)(?<!如经确认使用)外委(?!方式)", "如采用外委方式", value)


def apply_replacements_idempotent(text: str, replacements: dict[str, str]) -> str:
    value = str(text or "")
    for old, new in replacements.items():
        value = replace_once(value, old, new)
    return collapse_repeated_conditionals(value)


_DURATION_PHRASES = (
    (re.compile(r"执行通风\d+(?:\.\d+)?\s*[至到—–\-]\s*\d+(?:\.\d+)?\s*分钟(?:标准)?"), "按项目确认的时长通风"),
    (re.compile(r"通风\d+(?:\.\d+)?\s*[至到—–\-]\s*\d+(?:\.\d+)?\s*分钟(?:标准)?"), "按项目确认的时长通风"),
    (re.compile(r"执行通风\d+(?:\.\d+)?\s*分钟(?:标准)?"), "按项目确认的时长通风"),
    (re.compile(r"通风\d+(?:\.\d+)?\s*分钟(?:标准)?"), "按项目确认的时长通风"),
    (re.compile(r"消毒不少于\d+(?:\.\d+)?\s*分钟"), "消毒时长按项目确认"),
    (re.compile(r"不少于\d+(?:\.\d+)?\s*分钟"), "时长按项目确认"),
    (re.compile(r"会前\d+(?:\.\d+)?\s*分钟"), "会前按项目确认的时点"),
    (re.compile(r"\d+(?:\.\d+)?\s*[至到—–\-]\s*\d+(?:\.\d+)?\s*分钟"), "按项目确认的时长"),
)

_POLLUTION_PHRASES = (
    (re.compile(r"执行通风(?:\d+\s*[至到—–\-]\s*)?可根据项目确认要求确定响应安排(?:标准)?"), "按项目确认的时长通风"),
    (re.compile(r"通风(?:\d+\s*[至到—–\-]\s*)?可根据项目确认要求确定响应安排(?:标准)?"), "按项目确认的时长通风"),
    (re.compile(r"执行通风按项目确认的安排标准"), "按项目确认的时长通风"),
    (re.compile(r"通风按项目确认的安排标准"), "按项目确认的时长通风"),
    (re.compile(r"执行按项目确认的时长通风"), "按项目确认的时长通风"),
    (re.compile(r"消毒不少于可根据项目确认要求确定响应安排"), "消毒时长按项目确认"),
    (re.compile(r"不少于可根据项目确认要求确定响应安排"), "时长按项目确认"),
    (re.compile(r"会前可根据项目确认要求确定响应安排"), "会前按项目确认的时点"),
    (re.compile(r"可根据项目确认要求确定响应安排"), "按项目确认的安排"),
    (re.compile(r"按项目确认的安排标准"), "按项目确认的安排"),
)

_BARE_MINUTES = re.compile(r"(?<!\d)\d+(?:\.\d+)?\s*分钟")

CONDITIONAL_POLLUTION_MARKERS = (
    "可根据项目确认要求确定响应安排",
    "通风按项目确认的安排标准",
    "不少于可根据项目确认",
    "会前可根据项目确认要求确定响应安排",
)


def has_conditional_postprocess_pollution(text: str) -> bool:
    value = str(text or "")
    return any(marker in value for marker in CONDITIONAL_POLLUTION_MARKERS)


def rewrite_unconfirmed_durations(text: str, *, include_bare_minutes: bool = False) -> str:
    """Replace historic duration phrases with grammatical Chinese. Never splice mid-clause."""
    value = str(text or "")
    previous = None
    while previous != value:
        previous = value
        for pattern, repl in _DURATION_PHRASES + _POLLUTION_PHRASES:
            value = pattern.sub(repl, value)
    if include_bare_minutes:
        value = _BARE_MINUTES.sub("按项目确认的时长", value)
    return collapse_repeated_conditionals(re.sub(r"\s{2,}", " ", value).strip())


def sanitize_public_text(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: sanitize_public_text(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_public_text(item) for item in value]
    if isinstance(value, str):
        return rewrite_unconfirmed_durations(collapse_repeated_conditionals(value))
    return value
