from __future__ import annotations

import json
import os
import statistics
import threading
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "latency-history-v2"
_APPEND_LOCK = threading.Lock()

# Conservative initial ranges in minutes. Documented assumptions, not live SLAs.
INITIAL_ESTIMATES_MINUTES = {
    ("qwen_modelstudio", "fast", "draft"): (8, 16),
    ("qwen_modelstudio", "balanced", "draft"): (12, 22),
    ("qwen_modelstudio", "deep", "draft"): (18, 32),
    ("kimi_moonshot", "fast", "draft"): (18, 32),
    ("kimi_moonshot", "balanced", "draft"): (25, 45),
    ("kimi_moonshot", "deep", "draft"): (35, 60),
    ("grok_build", "fast", "draft"): (25, 45),
    ("grok_build", "balanced", "draft"): (40, 80),
    ("grok_build", "deep", "draft"): (55, 110),
    ("mock", "fast", "draft"): (1, 2),
    ("mock", "balanced", "draft"): (1, 3),
    ("mock", "deep", "draft"): (1, 4),
}


def size_bucket(section_count: int | None) -> str:
    if section_count is None:
        return "unknown_size"
    count = int(section_count)
    if count <= 1:
        return "single_1"
    if count <= 8:
        return "small_2_8"
    if count <= 19:
        return "medium_9_19"
    return "full_20_plus"


def history_key(provider: str, model: str, mode: str, stage: str, section_count: int | None = None) -> dict[str, str]:
    return {
        "provider": str(provider or "unknown"),
        "model": str(model or "unknown"),
        "mode": str(mode or "balanced"),
        "stage": str(stage or "draft"),
        "size_bucket": size_bucket(section_count),
    }


def terminal_history_id(sample: dict[str, Any]) -> str:
    if sample.get("history_id"):
        return str(sample["history_id"])
    run_id = str(sample.get("run_id") or "")
    outcome = str(sample.get("final_outcome") or sample.get("success") or "")
    calls = str(sample.get("this_run_calls") if sample.get("this_run_calls") is not None else sample.get("provider_calls") or "")
    started = str(sample.get("generation_started_at") or sample.get("build_id") or "")
    invocation = str(sample.get("invocation_id") or sample.get("attempt_id") or "")
    return f"{run_id}|{outcome}|{calls}|{started}|{invocation}"


def _lock_handle(handle) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        except OSError:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_handle(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def append_latency_sample(path: Path, sample: dict[str, Any]) -> bool:
    """Append one terminal sample. Returns False when the same terminal attempt already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    history_id = terminal_history_id(sample)
    payload = dict(sample)
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload["history_id"] = history_id
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _APPEND_LOCK:
        existing_ids = {str(row.get("history_id") or terminal_history_id(row)) for row in load_samples(path)}
        if history_id in existing_ids:
            return False
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            if lock_handle.tell() == 0:
                lock_handle.write("0")
                lock_handle.flush()
            try:
                _lock_handle(lock_handle)
                existing_ids = {str(row.get("history_id") or terminal_history_id(row)) for row in load_samples(path)}
                if history_id in existing_ids:
                    return False
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
            finally:
                _unlock_handle(lock_handle)
    return True


def load_samples(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def sample_eligible_for_eta(row: dict[str, Any], key: dict[str, str]) -> bool:
    if str(row.get("schema_version") or "") != SCHEMA_VERSION:
        return False
    if row.get("eligible_for_eta") is not True:
        return False
    if row.get("zero_generation"):
        return False
    if row.get("success") is not True:
        return False
    if str(row.get("input_tokens")) == "estimate" or str(row.get("output_tokens")) == "estimate":
        return False
    if str(row.get("provider")) != key["provider"]:
        return False
    if str(row.get("mode") or "balanced") != key["mode"]:
        return False
    if str(row.get("stage") or "draft") != key["stage"]:
        return False
    query_model = str(key.get("model") or "unknown")
    row_model = str(row.get("model") or "unknown")
    if query_model in {"", "unknown"}:
        if row_model not in {"", "unknown"}:
            return False
    elif row_model != query_model:
        return False
    row_bucket = str(row.get("size_bucket") or size_bucket(row.get("section_count") if "section_count" in row else row.get("logical_section_count")))
    if row_bucket != key["size_bucket"]:
        return False
    if row.get("partial_resume") or row.get("workload_scope") == "partial_resume":
        return False
    if str(row.get("sample_scope") or "") in {"partial_resume", "provider_attempt"}:
        return False
    return True


def load_budget_history_samples(
    path: Path | None,
    *,
    provider: str,
    model: str,
    mode: str,
    stage: str,
    batch_size: int = 1,
) -> list[dict[str, Any]]:
    """Per-attempt samples only. Full-document wall times never become batch timeouts."""
    if path is None:
        return []
    rows = []
    for row in load_samples(path):
        if str(row.get("schema_version") or "") != SCHEMA_VERSION:
            continue
        if row.get("eligible_for_eta") is not True or row.get("success") is not True:
            continue
        if str(row.get("provider")) != str(provider or "unknown"):
            continue
        if str(row.get("model") or "unknown") != str(model or "unknown"):
            continue
        if str(row.get("mode") or "balanced") != str(mode or "balanced"):
            continue
        if str(row.get("stage") or "draft") != str(stage or "draft"):
            continue
        scope = str(row.get("sample_scope") or "")
        if scope != "provider_attempt" or int(row.get("batch_size") or 0) != int(batch_size):
            continue
        if row.get("partial_resume"):
            continue
        try:
            elapsed = float(row.get("elapsed_seconds") or 0)
        except (TypeError, ValueError):
            continue
        if elapsed <= 0:
            continue
        rows.append(row)
    return rows[-20:]


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    idx = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return float(ordered[idx])


def estimate_runtime(
    *,
    provider: str,
    model: str = "",
    mode: str = "balanced",
    stage: str = "draft",
    history_path: Path | None = None,
    min_samples: int = 3,
    section_count: int | None = None,
) -> dict[str, Any]:
    key = history_key(provider, model, mode, stage, section_count)
    samples: list[float] = []
    excluded_legacy = 0
    if history_path is not None:
        for row in load_samples(history_path):
            if sample_eligible_for_eta(row, key):
                try:
                    samples.append(float(row.get("elapsed_seconds") or 0))
                except (TypeError, ValueError):
                    continue
            elif row.get("success"):
                excluded_legacy += 1
    samples = [value for value in samples if value > 0][-20:]
    if len(samples) >= min_samples:
        p50 = _percentile(samples, 0.50)
        p80 = _percentile(samples, 0.80)
        lo = max(1, int(round(p50 / 60)))
        hi = max(lo, int(round(p80 / 60)))
        if hi == lo:
            hi = lo + 1
        return {
            **key,
            "status": "range",
            "sample_count": len(samples),
            "p50_seconds": int(p50),
            "p80_seconds": int(p80),
            "median_seconds": int(statistics.median(samples)),
            "display": f"约{lo}～{hi}分钟",
            "range_minutes": [lo, hi],
            "source": "recent_success_history",
            "excluded_legacy_or_unverified": excluded_legacy,
        }
    guess = INITIAL_ESTIMATES_MINUTES.get((key["provider"], key["mode"], key["stage"]))
    if guess:
        return {
            **key,
            "status": "unknown_with_assumption",
            "sample_count": len(samples),
            "display": "暂无足够历史数据",
            "assumption_display": f"按引擎经验约{guess[0]}～{guess[1]}分钟",
            "range_minutes": list(guess),
            "source": "provider_initial_assumption",
            "assumption_note": "初始区间为保守文档化假设，不是精确承诺，也不是现场实测。",
            "excluded_legacy_or_unverified": excluded_legacy,
        }
    return {
        **key,
        "status": "unknown",
        "sample_count": len(samples),
        "display": "暂无足够历史数据",
        "range_minutes": None,
        "source": "none",
        "excluded_legacy_or_unverified": excluded_legacy,
    }


def build_terminal_latency_sample(
    *,
    provider: str,
    model: str,
    mode: str,
    stage: str,
    section_count: int,
    batch_count: int | None,
    elapsed_seconds: float,
    qa_seconds: float,
    provider_wait_seconds: float,
    success: bool,
    eligible_for_eta: bool,
    final_outcome: str,
    token_accounting: dict[str, Any] | None,
    build_id: str,
    run_id: str,
    this_run_calls: int,
    zero_generation: bool,
    token_provenance: str,
    generation_started_at: str | None = None,
    invocation_id: str | None = None,
    sample_scope: str = "full_document",
    partial_resume: bool = False,
) -> dict[str, Any]:
    tokens = token_accounting or {}
    import uuid
    invocation = invocation_id or str(uuid.uuid4())
    eligible = bool(eligible_for_eta and success and not zero_generation and not partial_resume and sample_scope != "partial_resume")
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": provider,
        "model": model or "unknown",
        "mode": mode,
        "stage": stage,
        "section_count": None if section_count is None else int(section_count),
        "logical_section_count": None if section_count is None else int(section_count),
        "generation_batch_count": batch_count,
        "size_bucket": size_bucket(section_count),
        "elapsed_seconds": float(elapsed_seconds),
        "qa_seconds": float(qa_seconds),
        "provider_wait_seconds": float(provider_wait_seconds),
        "success": bool(success),
        "eligible_for_eta": eligible,
        "final_outcome": final_outcome,
        "input_tokens": int(tokens["input_tokens"]) if tokens.get("input_tokens") is not None else 0,
        "output_tokens": int(tokens["output_tokens"]) if tokens.get("output_tokens") is not None else 0,
        "cache_read_tokens": int(tokens.get("cache_read_tokens") or 0),
        "token_provenance": token_provenance,
        "build_id": build_id,
        "run_id": run_id,
        "this_run_calls": int(this_run_calls or 0),
        "zero_generation": bool(zero_generation),
        "generation_started_at": generation_started_at,
        "invocation_id": invocation,
        "sample_scope": sample_scope,
        "partial_resume": bool(partial_resume),
        "workload_scope": "partial_resume" if partial_resume else sample_scope,
        "cost_usd": None,
        "cost_provenance": "unknown_unverified_pricing",
    }
