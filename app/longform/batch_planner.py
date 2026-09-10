from __future__ import annotations

from typing import Any

from providers.capability import CAPABILITY_PROFILES, latency_budget_seconds, normalize_speed_profile, recommended_parallelism

ADVISORY_DEPENDENCY_TYPES = {"clarification_boundary"}


def estimate_section_tokens(contract: dict[str, Any]) -> tuple[int, int]:
    """Conservative token estimate. Chinese content units are treated as ~1 token each plus JSON overhead."""
    target = contract.get("target_words") or {}
    words_max = int(target.get("max") or target.get("min") or 800)
    must = len(contract.get("must_cover") or [])
    output_tokens = max(400, int(words_max * 1.05) + must * 30 + 120)
    input_tokens = 1600 + must * 50 + len(contract.get("source_requirements") or []) * 30 + estimate_context_overhead(contract)
    return input_tokens, output_tokens


def estimate_context_overhead(contract: dict[str, Any]) -> int:
    blob = str(contract.get("must_cover") or "") + str(contract.get("section_title") or "")
    return max(80, len(blob) // 2)


def _module_key(section_id: str) -> str:
    return str(section_id or "")[:3]


def is_hard_dependency(dep: dict[str, Any]) -> bool:
    kind = str(dep.get("dependency_type") or "")
    if kind in ADVISORY_DEPENDENCY_TYPES:
        return False
    if dep.get("generation_dependency") is False:
        return False
    return bool(dep.get("stale_on_change", True))


def build_prereq_maps(section_ids: list[str], dependency_map: dict[str, Any]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    selected = set(section_ids)
    hard = {sid: [] for sid in section_ids}
    advisory = {sid: [] for sid in section_ids}
    for dep in dependency_map.get("dependencies") or []:
        source = dep.get("source_section")
        if source not in selected:
            continue
        bucket = hard if is_hard_dependency(dep) else advisory
        for target in dep.get("dependent_sections") or []:
            if target in selected and source not in bucket[target]:
                bucket[target].append(source)
    return hard, advisory


def _capacity(provider_name: str, provider_config: dict[str, Any] | None = None) -> dict[str, int]:
    profile = CAPABILITY_PROFILES.get(provider_name) or CAPABILITY_PROFILES["mock"]
    config = provider_config or {}
    extra = dict(config.get("extra_options") or {})
    max_out = int(config.get("max_tokens") or extra.get("max_tokens") or profile.get("max_output_tokens") or 4096)
    window = int(config.get("context_window") or extra.get("context_window") or profile.get("context_window") or 32000)
    return {"max_output_tokens": max_out, "context_window": window}


def _can_merge(current: dict[str, Any], nxt: dict[str, Any], hard: dict[str, list[str]], cap: dict[str, int], max_sections: int) -> bool:
    if len(current["section_ids"]) >= max_sections:
        return False
    last = current["section_ids"][-1]
    candidate = nxt["section_id"]
    if _module_key(last) != _module_key(candidate):
        return False
    current_ids = set(current["section_ids"])
    if any(src in current_ids for src in hard.get(candidate, [])):
        return False
    if any(candidate in hard.get(sid, []) for sid in current["section_ids"]):
        return False
    out = current["estimated_output_tokens"] + nxt["output_tokens"]
    inp = current["estimated_input_tokens"] + nxt["input_tokens"]
    if out > int(cap["max_output_tokens"] * 0.88):
        return False
    if inp > int(cap["context_window"] * 0.55):
        return False
    return True


def plan_generation_batches(
    contracts: list[dict[str, Any]],
    dependency_map: dict[str, Any],
    *,
    provider_name: str = "mock",
    speed_profile: str = "balanced",
    max_parallelism: int | None = None,
    provider_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ordered = sorted(contracts, key=lambda row: int(row.get("generation_order") or 0))
    section_ids = [row["section_id"] for row in ordered]
    if not section_ids:
        return {"schema_version": "generation-batches-v0.1", "batches": [], "rejected": [], "warnings": []}
    hard, advisory = build_prereq_maps(section_ids, dependency_map)
    cap = _capacity(provider_name, provider_config)
    mode = normalize_speed_profile(speed_profile)
    max_sections = {"fast": 4, "balanced": 4, "deep": 3}[mode]
    rows = []
    warnings: list[dict[str, Any]] = []
    for contract in ordered:
        inp, out = estimate_section_tokens(contract)
        over = out > cap["max_output_tokens"]
        if over:
            warnings.append({"section_id": contract["section_id"], "reason": "over_capacity_output_singleton", "estimated_output_tokens": out, "max_output_tokens": cap["max_output_tokens"]})
        rows.append({
            "section_id": contract["section_id"],
            "module": _module_key(contract["section_id"]),
            "input_tokens": inp,
            "output_tokens": min(out, cap["max_output_tokens"]) if over else out,
            "raw_output_tokens": out,
            "singleton_required": over,
            "complexity": "high" if len(contract.get("must_cover") or []) >= 4 or contract.get("required_processes") else "normal",
        })
    batches: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for row in rows:
        if row["singleton_required"]:
            if current:
                batches.append(current)
                current = None
            batches.append({
                "section_ids": [row["section_id"]],
                "estimated_input_tokens": row["input_tokens"],
                "estimated_output_tokens": row["raw_output_tokens"],
            })
            continue
        if current is None:
            current = {
                "section_ids": [row["section_id"]],
                "estimated_input_tokens": row["input_tokens"],
                "estimated_output_tokens": row["output_tokens"],
            }
            continue
        if _can_merge(current, row, hard, cap, max_sections):
            current["section_ids"].append(row["section_id"])
            current["estimated_input_tokens"] += row["input_tokens"]
            current["estimated_output_tokens"] += row["output_tokens"]
        else:
            batches.append(current)
            current = {
                "section_ids": [row["section_id"]],
                "estimated_input_tokens": row["input_tokens"],
                "estimated_output_tokens": row["output_tokens"],
            }
    if current:
        batches.append(current)

    sid_to_batch: dict[str, str] = {}
    planned = []
    latency = latency_budget_seconds(provider_name, mode, "draft", 1, config=provider_config)
    for index, batch in enumerate(batches, 1):
        batch_id = f"GB-{index:02d}"
        for sid in batch["section_ids"]:
            sid_to_batch[sid] = batch_id
        size = len(batch["section_ids"])
        batch_latency = latency_budget_seconds(provider_name, mode, "draft", size, config=provider_config)
        planned.append({
            "batch_id": batch_id,
            "section_ids": list(batch["section_ids"]),
            "estimated_input_tokens": batch["estimated_input_tokens"],
            "estimated_output_tokens": batch["estimated_output_tokens"],
            "dependency_ids": [],
            "parallel_group": 0,
            "reasoning_profile": mode,
            "latency_budget": {"soft": batch_latency["soft_latency_budget"], "hard": batch_latency["hard_timeout"]},
            "retry_policy": {"quality": "escalate", "latency": "downgrade_and_shrink", "rate_limit": "reduce_concurrency"},
        })
    for row in planned:
        dep_ids = []
        for sid in row["section_ids"]:
            for src in hard.get(sid, []):
                other = sid_to_batch.get(src)
                if other and other != row["batch_id"] and other not in dep_ids:
                    dep_ids.append(other)
        row["dependency_ids"] = dep_ids

    remaining = {row["batch_id"]: set(row["dependency_ids"]) for row in planned}
    group = 1
    assigned = {}
    while remaining:
        ready = [bid for bid, deps in remaining.items() if not deps]
        if not ready:
            break
        for bid in ready:
            assigned[bid] = group
            remaining.pop(bid, None)
        for deps in remaining.values():
            deps.difference_update(ready)
        group += 1
    for bid in remaining:
        assigned[bid] = group
        warnings.append({"batch_id": bid, "reason": "dependency_cycle_or_unresolved"})
    for row in planned:
        row["parallel_group"] = assigned.get(row["batch_id"], 1)

    parallelism = recommended_parallelism(provider_name, max_parallelism)
    return {
        "schema_version": "generation-batches-v0.1",
        "provider_name": provider_name,
        "speed_profile": mode,
        "max_parallelism": parallelism,
        "logical_section_count": len(section_ids),
        "generation_batch_count": len(planned),
        "capacity": cap,
        "default_latency_budget": {"soft": latency["soft_latency_budget"], "hard": latency["hard_timeout"]},
        "batches": planned,
        "rejected": [],
        "warnings": warnings,
        "hard_prerequisites": hard,
        "advisory_prerequisites": advisory,
        "notes": [
            "Logical sections remain the QA/coverage/budget unit.",
            "Hard generation dependencies are preserved, including reverse outline order.",
            "Over-capacity sections stay as singleton batches instead of being dropped.",
            "Capacity uses provider config when present, otherwise conservative documented limits.",
        ],
    }


def shrink_batch(batch: dict[str, Any], remaining_section_ids: list[str]) -> dict[str, Any]:
    keep = [sid for sid in batch.get("section_ids") or [] if sid in set(remaining_section_ids)]
    patched = dict(batch)
    patched["section_ids"] = keep
    if keep:
        ratio = len(keep) / max(1, len(batch.get("section_ids") or keep))
        patched["estimated_output_tokens"] = int((batch.get("estimated_output_tokens") or 0) * ratio)
        patched["estimated_input_tokens"] = int((batch.get("estimated_input_tokens") or 0) * ratio)
    return patched


def split_micro_batches(section_ids: list[str], original: dict[str, Any]) -> list[dict[str, Any]]:
    if len(section_ids) <= 1:
        row = dict(original)
        row["section_ids"] = list(section_ids)
        row["batch_id"] = str(original.get("batch_id") or "GB") + "-R"
        return [row] if section_ids else []
    mid = max(1, len(section_ids) // 2)
    rows = []
    for suffix, group in (("A", section_ids[:mid]), ("B", section_ids[mid:])):
        row = dict(original)
        row["section_ids"] = group
        row["batch_id"] = f"{original.get('batch_id')}-{suffix}"
        row["dependency_ids"] = list(original.get("dependency_ids") or [])
        rows.append(row)
    return rows
