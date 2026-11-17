from __future__ import annotations

from collections import Counter
from typing import Any

from providers.capability import CAPABILITY_PROFILES, latency_budget_seconds, normalize_speed_profile, recommended_parallelism, safe_output_budget

from .cu_calibration import (
    PLANNING_BASIS,
    batch_output_ceiling_tokens,
    calibration_status,
    call_tail_amplification,
    delivery_ratio,
    predicted_cu_output_tokens,
    worst_case_cu_output_tokens,
)

ADVISORY_DEPENDENCY_TYPES = {"clarification_boundary"}


def estimate_section_tokens(contract: dict[str, Any]) -> tuple[int, int]:
    """Conservative token estimate. Chinese content units are treated as ~1 token each plus JSON overhead."""
    target = contract.get("target_words") or {}
    words_max = int(target.get("max") or target.get("min") or 800)
    must = len(contract.get("must_cover") or [])
    # 同一把尺：Section 级预估也不能再用"预算上限 ×1.05"，那是上限不是交付量。
    output_tokens = max(400, int(words_max * delivery_ratio(contract.get("archetype"))) + must * 30 + 120)
    input_tokens = 1600 + must * 50 + len(contract.get("source_requirements") or []) * 30 + estimate_context_overhead(contract)
    return input_tokens, output_tokens


def estimate_worst_case_section_tokens(contract: dict[str, Any]) -> int:
    """同一份预算在实测 p95 超发情形下的输出量：合并批次时用来自卫，不用来装批。"""
    target = contract.get("target_words") or {}
    words_max = int(target.get("max") or target.get("min") or 800)
    must = len(contract.get("must_cover") or [])
    return max(400, int(words_max * delivery_ratio(contract.get("archetype"), "p95")) + must * 30 + 120)


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


def _cu_output_tokens(contract: dict[str, Any]) -> int:
    """这个 CU 预计吃掉的输出量。尺在 `cu_calibration`，这里只做转接。"""
    return predicted_cu_output_tokens(contract)


def content_unit_batches(section_id: str, content_units: list[dict[str, Any]], *, safe_budget: int) -> list[dict[str, Any]]:
    """Return provider-safe groups without ever falling back to a whole section.

    每组同时带回逐 CU 的点估计与尾部估计（与 `content_unit_ids` 同序），后续
    "拿后一批的开头补前一批的尾巴"才不用重新估一遍——重估就是第二把尺。
    """
    rows = [row for row in content_units if isinstance(row, dict) and row.get("content_unit_id")]
    if not rows:
        return [{"content_unit_ids": [], "content_unit_estimates": [], "content_unit_worst_estimates": [],
                 "estimated_output_tokens": 0, "worst_case_output_tokens": 0, "split_reason": None}]
    groups: list[dict[str, Any]] = []
    current: list[str] = []
    current_estimates: list[int] = []
    current_worst_estimates: list[int] = []
    current_worst = 0
    estimate = 0

    def _close(reason: str, tokens: int) -> None:
        groups.append({
            "content_unit_ids": current,
            "content_unit_estimates": current_estimates,
            "content_unit_worst_estimates": current_worst_estimates,
            "estimated_output_tokens": tokens,
            "worst_case_output_tokens": current_worst,
            "split_reason": reason,
        })

    for row in rows:
        cu_id = str(row["content_unit_id"])
        cu_estimate = _cu_output_tokens(row)
        cu_tail = worst_case_cu_output_tokens(row)
        if current and estimate + cu_estimate > safe_budget:
            _close("CAPACITY_AWARE_CU_SPLIT", estimate)
            current, current_estimates, current_worst_estimates = [], [], []
            current_worst, estimate = 0, 0
        current.append(cu_id)
        current_estimates.append(min(cu_estimate, safe_budget))
        current_worst_estimates.append(cu_tail)
        estimate += min(cu_estimate, safe_budget)
        current_worst += cu_tail
        if cu_estimate > safe_budget:
            _close("CU_CONTINUATION_REQUIRED", safe_budget)
            current, current_estimates, current_worst_estimates = [], [], []
            current_worst, estimate = 0, 0
    if current:
        _close("CAPACITY_AWARE_CU_SPLIT" if groups else "", min(estimate, safe_budget))
        groups[-1]["split_reason"] = groups[-1]["split_reason"] or None
    return groups


def _shares_chapter(current: dict[str, Any], candidate: str) -> bool:
    return _module_key(str(current["section_ids"][-1])) == _module_key(candidate)


def _dependency_conflict(current: dict[str, Any], nxt: dict[str, Any], hard: dict[str, list[str]]) -> bool:
    """两批能不能同批生成：任一方向存在硬依赖就不行。

    CU 分支以前只查容量，不查这一条——那等于给"跨节合批"留了个绕过硬依赖的口子：
    S11-01 硬依赖 S09-01 时，如果两节的 CU 组恰好都只剩尾巴，就会被塞进同一次调用，
    下游那半批于是读不到上游状态。合批可以省调用，不能省前置条件。
    """
    current_ids = set(current["section_ids"])
    candidate = nxt["section_id"]
    if any(src in current_ids for src in hard.get(candidate, [])):
        return True
    return any(candidate in hard.get(sid, []) for sid in current["section_ids"])


def _cu_safe_limit(cap: dict[str, int], safe_budget: int) -> int:
    """CU 批次允许的**点估计**上限。

    这里不再写死 0.72：那一层保守已经由 `providers.capability.safe_output_budget` 打过一次，
    在规划器里重抄一遍数字，等于同一把尺有两个刻度不一致的家。
    实测批次尾部（`batch_output_ceiling_tokens`）只作为第二道天花板，两者取小。
    """
    return min(int(safe_budget or 0), batch_output_ceiling_tokens(int(cap["max_output_tokens"])))


def _merge_block_reason(current: dict[str, Any], nxt: dict[str, Any], hard: dict[str, list[str]], cap: dict[str, int], max_sections: int, safe_budget: int) -> str | None:
    """为什么这两行不能整组合并（None = 能合并）。

    判决与理由同在一处：报告"这轮为什么是 60 次不是 49 次"必须能指到具体的门，
    而不是靠人重读 packing 循环猜。门的次序就是判决的次序，二者不可能走偏。

    这里**不**再放一道"两条分支共用的实测天花板预检"：`batch_output_ceiling_tokens` 折算出来是
    Provider 上限的 0.9018，而下面两条分支各自的上限分别是 0.72（CU 组）与 0.88（整节），
    都比它紧 —— 预检在数值上永不绑定，却会抢先把理由写成它，让报告把 42 次合并失败
    记到一道根本没起作用的门上。判决不变，理由必须变：起作用的门才有资格署名。
    """
    # CU-aware batches are already capacity-bounded; never merge them back into
    # a section-sized provider call.
    if current.get("content_unit_ids") or nxt.get("content_unit_ids"):
        # CU groups from the same Section must remain separate; groups from
        # adjacent Sections may share a provider call when the safe budget holds.
        if current["section_ids"][-1] == nxt["section_id"]:
            return "cu_groups_of_same_section"
        if not _shares_chapter(current, nxt["section_id"]):
            return "cu_cross_chapter"
        if _dependency_conflict(current, nxt, hard):
            return "cu_hard_dependency"
        if current["estimated_output_tokens"] + nxt["output_tokens"] > _cu_safe_limit(cap, safe_budget):
            return "cu_over_safe_budget"
        return None
    if len(current["section_ids"]) >= max_sections:
        return "max_sections_per_call"
    if not _shares_chapter(current, nxt["section_id"]):
        return "cross_chapter"
    if _dependency_conflict(current, nxt, hard):
        return "hard_dependency"
    out = current["estimated_output_tokens"] + nxt["output_tokens"]
    inp = current["estimated_input_tokens"] + nxt["input_tokens"]
    if out > int(cap["max_output_tokens"] * 0.88):
        return "output_over_88pct_of_hard_limit"
    if inp > int(cap["context_window"] * 0.55):
        return "input_over_55pct_of_context_window"
    return None


def _cu_slice_fit(current: dict[str, Any], nxt: dict[str, Any], hard: dict[str, list[str]], cap: dict[str, int], safe_budget: int) -> int:
    """当前批还剩容量、下一批的 CU 组整组装不下时，能补进来的开头 CU 个数。

    这是 Run D 72 次调用里真正白扔掉的那一段：每节的最后一组常常只剩 1～2 个 CU，
    而下一节的第一组已经装满，整组合并不了，于是各花一次调用。按开头切片补齐
    既保住全局顺序，也保住"每个 CU 恰好被请求一次"的守恒。
    """
    if not (current.get("content_unit_ids") and nxt.get("content_unit_ids")):
        return 0
    if current["section_ids"][-1] == nxt["section_id"]:
        return 0
    if not _shares_chapter(current, nxt["section_id"]):
        return 0
    if _dependency_conflict(current, nxt, hard):
        return 0
    ids = list(nxt.get("content_unit_ids") or [])
    estimates = list(nxt.get("content_unit_estimates") or [])
    # 判决只依赖点估计，所以只要求它对齐；`content_unit_worst_estimates` 缺失时
    # 报告用的最坏口径按 0 记账，不再因为它缺位就多花一次调用。
    if len(ids) < 2 or len(estimates) != len(ids):
        return 0
    limit = _cu_safe_limit(cap, safe_budget)
    used = int(current.get("estimated_output_tokens") or 0)
    k = 0
    # 只按点估计补位：`worst_case_output_tokens`（逐 CU p95 之和）在这里是**报告口径**，
    # 不是判决口径。分位数不可加，p95 之和是"每个 CU 同时跑到自己第 95 百分位"那种
    # 实测中不存在的情形；而 Provider 侧 0.72 已经是唯一一次尾部保护（放大系数 1.3889，
    # 大于任何 archetype 的单件 p95），再叠一层就是 Run D 把 44/64 次合并挡在门外的老路。
    while k < len(ids) - 1 and used + estimates[k] <= limit:
        used += estimates[k]
        k += 1
    return k


def _apply_cu_slice(current: dict[str, Any], nxt: dict[str, Any], k: int) -> dict[str, Any]:
    """把 nxt 开头 k 个 CU 并入 current，返回只剩余下 CU 的新行。"""
    ids = list(nxt["content_unit_ids"])
    estimates = list(nxt["content_unit_estimates"])
    tails = list(nxt["content_unit_worst_estimates"])
    moved_ratio = k / max(1, len(ids))
    moved_input = max(0, int(nxt["input_tokens"] * moved_ratio))
    # section_ids 必须一起记账：factory._run_batch 是照 batch["section_ids"] 逐节取
    # content_unit_ids_by_section 的，只登记 CU 不登记 Section 等于把补进来的半截
    # CU 变成了永远不会被请求的那一份——省下一次调用，丢掉一段正文。
    current["section_ids"].append(nxt["section_id"])
    current["estimated_input_tokens"] += moved_input
    current["content_unit_ids"].extend(ids[:k])
    current.setdefault("content_unit_ids_by_section", {}).setdefault(nxt["section_id"], []).extend(ids[:k])
    current["estimated_output_tokens"] += sum(estimates[:k])
    current["worst_case_output_tokens"] = int(current.get("worst_case_output_tokens") or 0) + sum(tails[:k])
    remainder = dict(nxt)
    remainder["content_unit_ids"] = ids[k:]
    remainder["content_unit_estimates"] = estimates[k:]
    remainder["content_unit_worst_estimates"] = tails[k:]
    remainder["output_tokens"] = sum(estimates[k:])
    remainder["worst_case_tokens"] = sum(tails[k:])
    remainder["input_tokens"] = max(500, int(nxt["input_tokens"] * (1 - moved_ratio)))
    remainder["split_reason"] = nxt.get("split_reason") or "CAPACITY_AWARE_CU_SPLIT"
    return remainder


def _call_count_lower_bound(rows: list[dict[str, Any]], safe_budget: int, achieved_calls: int) -> dict[str, Any]:
    """这一轮**至少**要打多少次，以及还剩多少次不是预测尺能省下来的。

    两条界都要给，取大的那个才叫下界：
    * `capacity_token_floor` —— 只问"点估计装得下就得花多少次"，最乐观；
    * `same_section_split_floor` —— 一节的 CU 组按构造就不能回到同一次调用
      （它就是"这一节超过安全容量"才被拆开又合不回去的），所以一节拆成几组，
      至少要占几次调用。只看 token 会把这一项当成可省的空间，那是虚报。
    顺序、硬依赖、同章约束都不在这里，因此本值仍然只是界，不是目标。
    """
    total = sum(int(row.get("output_tokens") or 0) for row in rows)
    token_floor = -(-total // safe_budget) if safe_budget and rows else 0
    per_section = Counter(str(row["section_id"]) for row in rows)
    split_floor = max(per_section.values()) if per_section else 0
    binding = max(token_floor, split_floor)
    return {
        "capacity_token_floor": token_floor,
        "same_section_split_floor": split_floor,
        "binding_bound": "capacity_token_floor" if token_floor >= split_floor else "same_section_split_floor",
        "lower_bound_calls": binding,
        "planned_calls": achieved_calls,
        "gap_over_lower_bound": achieved_calls - binding,
        "is_not_an_achievable_target": True,
        "note": (
            "下界不含生成顺序、硬依赖与同章约束，所以真计划的调用数一定 ≥ 它；"
            "报告只能用它说明『剩下的调用有多少不在预测尺上』，不得当成计划数或承诺数。"
        ),
    }


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
        return {"schema_version": "generation-batches-v0.2", "batches": [], "rejected": [], "warnings": []}
    hard, advisory = build_prereq_maps(section_ids, dependency_map)
    cap = _capacity(provider_name, provider_config)
    mode = normalize_speed_profile(speed_profile)
    max_sections = {"fast": 4, "balanced": 4, "deep": 3}[mode]
    safe_budget = safe_output_budget(cap["max_output_tokens"], mode)
    rows = []
    warnings: list[dict[str, Any]] = []
    section_cu_counts: dict[str, int] = {}
    for contract in ordered:
        inp, out = estimate_section_tokens(contract)
        worst = estimate_worst_case_section_tokens(contract)
        cu_contracts = contract.get("content_unit_contracts") if isinstance(contract.get("content_unit_contracts"), list) else []
        section_cu_counts[contract["section_id"]] = len([
            row for row in cu_contracts if isinstance(row, dict) and row.get("content_unit_id")
        ])
        # Small sections can remain one logical provider call; CU splitting is
        # required only when the section estimate would exceed safe capacity.
        if cu_contracts and out > safe_budget:
            groups = content_unit_batches(contract["section_id"], cu_contracts, safe_budget=safe_budget)
            if len(groups) > 1:
                warnings.append({"section_id": contract["section_id"], "reason": "capacity_aware_cu_split", "batch_count": len(groups), "safe_output_budget": safe_budget})
            for group in groups:
                ids = list(group["content_unit_ids"])
                group_worst = int(group.get("worst_case_output_tokens") or 0)
                group_estimate = int(group["estimated_output_tokens"] or 0)
                if group_estimate > batch_output_ceiling_tokens(cap["max_output_tokens"]):
                    # CU 已经是能切的最小粒度，再切就是拆 Section；这一条只能报出来，
                    # 让运营者在真实跑之前知道哪一批的点估计已经超出实测能安全交付的批次上限。
                    warnings.append({"section_id": contract["section_id"], "reason": "cu_group_over_measured_batch_ceiling", "estimated_output_tokens": group_estimate, "batch_output_ceiling_tokens": batch_output_ceiling_tokens(cap["max_output_tokens"]), "worst_case_output_tokens": group_worst, "content_unit_ids": ids})
                rows.append({"section_id": contract["section_id"], "module": _module_key(contract["section_id"]), "input_tokens": max(500, int(inp * len(ids) / max(1, len(cu_contracts)))), "output_tokens": group["estimated_output_tokens"] or min(out, safe_budget), "worst_case_tokens": group_worst, "raw_output_tokens": out, "singleton_required": False, "content_unit_ids": ids, "content_unit_estimates": list(group.get("content_unit_estimates") or []), "content_unit_worst_estimates": list(group.get("content_unit_worst_estimates") or []), "split_reason": group.get("split_reason"), "complexity": "high" if len(contract.get("must_cover") or []) >= 4 or contract.get("required_processes") else "normal"})
            continue
        over = out > cap["max_output_tokens"]
        if over:
            warnings.append({"section_id": contract["section_id"], "reason": "over_capacity_output_singleton", "estimated_output_tokens": out, "max_output_tokens": cap["max_output_tokens"]})
        rows.append({
            "section_id": contract["section_id"],
            "module": _module_key(contract["section_id"]),
            "input_tokens": inp,
            "output_tokens": min(out, cap["max_output_tokens"]) if over else out,
            "worst_case_tokens": worst,
            "raw_output_tokens": out,
            "singleton_required": over,
            "complexity": "high" if len(contract.get("must_cover") or []) >= 4 or contract.get("required_processes") else "normal",
            "content_unit_ids": [],
            "split_reason": None,
        })

    def _open_batch(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "section_ids": [row["section_id"]],
            "estimated_input_tokens": row["input_tokens"],
            "estimated_output_tokens": row["output_tokens"],
            "worst_case_output_tokens": int(row.get("worst_case_tokens") or 0),
            "content_unit_ids": list(row.get("content_unit_ids") or []),
            "content_unit_estimates": list(row.get("content_unit_estimates") or []),
            "content_unit_worst_estimates": list(row.get("content_unit_worst_estimates") or []),
            "content_unit_ids_by_section": {row["section_id"]: list(row.get("content_unit_ids") or [])},
            "split_reason": row.get("split_reason"),
        }

    batches: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    merge_log: list[dict[str, Any]] = []
    for row in rows:
        if row["singleton_required"]:
            if current:
                batches.append(current)
                current = None
            merge_log.append({
                "candidate_section_id": row["section_id"],
                "reason": "over_capacity_singleton_not_packed",
                "carried_section_ids": list(batches[-1]["section_ids"]) if batches else [],
            })
            batches.append(_open_batch(row))
            continue
        if current is None:
            current = _open_batch(row)
            continue
        block_reason = _merge_block_reason(current, row, hard, cap, max_sections, safe_budget)
        if block_reason is None:
            current["section_ids"].append(row["section_id"])
            current["estimated_input_tokens"] += row["input_tokens"]
            current["estimated_output_tokens"] += row["output_tokens"]
            current["worst_case_output_tokens"] = int(current.get("worst_case_output_tokens") or 0) + int(row.get("worst_case_tokens") or 0)
            current.setdefault("content_unit_ids_by_section", {}).setdefault(row["section_id"], []).extend(row.get("content_unit_ids") or [])
        else:
            # 整组合并不等于一个都补不进来：先按开头切片补齐当前批的尾巴，
            # 剩下的 CU 自己开一批。顺序不变，守恒不变，只是少花一次调用。
            take = _cu_slice_fit(current, row, hard, cap, safe_budget)
            if take:
                merge_log.append({
                    "candidate_section_id": row["section_id"],
                    "reason": block_reason,
                    "leading_cu_sliced_into_previous_batch": take,
                    "carried_section_ids": list(current["section_ids"]),
                })
                row = _apply_cu_slice(current, row, take)
            else:
                merge_log.append({
                    "candidate_section_id": row["section_id"],
                    "reason": block_reason,
                    "carried_section_ids": list(current["section_ids"]),
                })
            batches.append(current)
            current = _open_batch(row)
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
            "worst_case_output_tokens": int(batch.get("worst_case_output_tokens") or 0),
            "provider_output_limit": cap["max_output_tokens"],
            "safe_output_budget": safe_budget,
            "capacity_basis": f"calibrated_{PLANNING_BASIS}_point_estimate",
            "content_unit_ids": list(batch.get("content_unit_ids") or []),
            "content_unit_ids_by_section": dict(batch.get("content_unit_ids_by_section") or ({batch["section_ids"][0]: list(batch.get("content_unit_ids") or [])} if len(batch["section_ids"]) == 1 else {})),
            "split_reason": batch.get("split_reason"),
            "dependency_ids": [],
            "parallel_group": 0,
            "reasoning_profile": mode,
            "latency_budget": {"soft": batch_latency["soft_latency_budget"], "hard": batch_latency["hard_timeout"]},
            "retry_policy": {"quality": "escalate", "latency": "downgrade_and_shrink", "rate_limit": "reduce_concurrency"},
        })
    for row in planned:
        dep_ids = []
        dep_sections = []
        for sid in row["section_ids"]:
            for src in hard.get(sid, []):
                other = sid_to_batch.get(src)
                if other and other != row["batch_id"] and other not in dep_ids:
                    dep_ids.append(other)
                if other and other != row["batch_id"] and src not in dep_sections:
                    dep_sections.append(src)
        row["dependency_ids"] = dep_ids
        # Batch可能部分成功。保留真正的Section级前置条件，避免同批无关
        # Section失败后把已经满足前置条件的下游Section一并阻断。
        row["dependency_section_ids"] = dep_sections

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
    mapped_cus: dict[str, list[str]] = {}
    for row in planned:
        for sid, ids in (row.get("content_unit_ids_by_section") or {}).items():
            mapped_cus.setdefault(sid, []).extend(ids)
    grouped = sum(len(ids) for ids in mapped_cus.values())
    whole_section_cus = sum(section_cu_counts.get(sid, 0) for sid in section_ids if not mapped_cus.get(sid))
    planned_cu_total = sum(section_cu_counts.values())
    duplicated = {sid: ids for sid, ids in mapped_cus.items() if len(ids) != len(set(ids))}
    return {
        "schema_version": "generation-batches-v0.2",
        "provider_name": provider_name,
        "speed_profile": mode,
        "max_parallelism": parallelism,
        "logical_section_count": len(section_ids),
        "generation_batch_count": len(planned),
        "capacity": cap,
        "capacity_summary": {
            "planned_provider_calls": len(planned),
            "planned_content_units": planned_cu_total,
            "content_units_per_call": round(grouped / len(planned), 3) if planned else 0,
            "safe_output_budget": safe_budget,
            "provider_output_limit": cap["max_output_tokens"],
            "measured_batch_output_ceiling_tokens": batch_output_ceiling_tokens(cap["max_output_tokens"]),
            "call_tail_amplification": call_tail_amplification(),
            "batches_over_measured_batch_ceiling": [
                row["batch_id"] for row in planned
                if row["estimated_output_tokens"] > batch_output_ceiling_tokens(cap["max_output_tokens"])],
            "content_unit_conservation": {
                "grouped_content_units": grouped,
                "carried_by_whole_section_calls": whole_section_cus,
                "planned_content_units_total": planned_cu_total,
                "conservation_holds": grouped + whole_section_cus == planned_cu_total,
                "duplicated_within_section": sorted(duplicated),
            },
            "merge_block_reason_distribution": dict(Counter(
                str(row["reason"]) for row in merge_log)),
            "call_count_lower_bound": _call_count_lower_bound(rows, safe_budget, len(planned)),
            "wall_clock_projection": "NOT_MEASURED_OFFLINE",
        },
        "merge_decision_log": merge_log,
        "output_calibration": calibration_status(),
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
            "Batch packing is sized on measured CU delivery (ratio-of-sums); the provider 0.72 factor is the only tail protection.",
            "worst_case_output_tokens is a per-CU p95 envelope for reporting, not a merge decision: quantiles are not additive.",
            "wall_clock_projection stays NOT_MEASURED_OFFLINE: an offline round may not invent seconds.",
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
        patched["worst_case_output_tokens"] = int((batch.get("worst_case_output_tokens") or 0) * ratio)
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
