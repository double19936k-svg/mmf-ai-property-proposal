"""离线性能规划器：把"这轮要花多少次调用、被哪几道闸限住"说清楚。

本轮是 OFFLINE ENGINEERING ROUND，不允许发起真实长文生成，也不允许把猜测写成秒数。
所以这个模块的输出被硬性分成两类，字段名就写着它们的来历：

* ``planned_*`` / ``capacity_*`` —— 由批次计划本身推导，离线可算、可复现。
* ``observed_*`` —— 只有当调用方把某个历史 Run 的第一方台账（``bridge_timing`` /
  ``concurrency_ledger``）交进来时才会出现；没有历史就老实写 ``NOT_MEASURED``。

墙钟时间一律不预测：Run D 里 queue_wait 与 worker_latency 的占比（见证据文件）说明
瓶颈在队列而不是模型，队列等待属于外部系统状态，离线轮没有资格报数字。
"""
from __future__ import annotations

from collections import Counter
from typing import Any

SCHEMA_VERSION = "mmf007c-r4-performance-plan-v0.2"
NOT_MEASURED = "NOT_MEASURED_OFFLINE"

# 重复限流排查结论：并发被四处 clamp，依赖被三层闸拦。这里是"谁在限流"的唯一台账，
# 报告里说"并发是 3 不是 1"必须能指到这里，而不是靠回忆。
THROTTLE_GATES = (
    {
        "gate": "provider_capability_parallelism",
        "authority": "providers/capability.py::recommended_parallelism",
        "rule": "min(3, requested, provider_max)",
        "kind": "concurrency",
        "note": "唯一的 Provider 侧上限；三处硬编码的 3 都应收敛到这里。",
    },
    {
        "gate": "scheduler_worker_pool",
        "authority": "longform/scheduler.py::ConcurrentBatchScheduler.run",
        "rule": "max(1, min(3, current_parallelism)) - in_flight",
        "kind": "concurrency",
        "note": "第二批 clamp：与 Provider 上限重复，但会在软超时后单独下调。",
    },
    {
        "gate": "app_core_brief_clamp",
        "authority": "app_core.py::brief.max_parallelism",
        "rule": "max(1, min(3, brief.max_parallelism))",
        "kind": "concurrency",
        "note": "入口侧第三、四处 clamp（读 brief 与写 brief 各一次）。",
    },
    {
        "gate": "planner_dependency_wave",
        "authority": "longform/batch_planner.py::plan_generation_batches",
        "rule": "parallel_group = 拓扑分层",
        "kind": "dependency",
        "note": "静态分层，只用于展示与诊断；真正的放行由下面两层决定。",
    },
    {
        "gate": "scheduler_batch_dependency",
        "authority": "longform/scheduler.py::_ready",
        "rule": "dependency_ids 全部 SUCCESS 才 ready",
        "kind": "dependency",
        "note": "批次级；同批部分失败时可由 dependency_section_ids 精确解封。",
    },
    {
        "gate": "factory_section_prerequisite",
        "authority": "longform/factory.py::_execute",
        "rule": "逐 Section 再查 prerequisites",
        "kind": "dependency",
        "note": "Section 级；与上一层语义重叠，是「依赖被三层闸拦」的来源。",
    },
)


def _distribution(values: list[int]) -> dict[str, int]:
    return {str(key): count for key, count in sorted(Counter(values).items())}


def _wave_count(batches: list[dict[str, Any]]) -> int:
    return len({int(row.get("parallel_group") or 1) for row in batches})


def profile_batch_plan(
    plan: dict[str, Any],
    *,
    concurrency_ledger: list[dict[str, Any]] | None = None,
    attempt_history: list[dict[str, Any]] | None = None,
    scope_note: str = "",
) -> dict[str, Any]:
    """离线画像：不预测墙钟，只把容量、调用数、限流闸与历史实测分开陈述。"""
    batches = [row for row in (plan.get("batches") or []) if row.get("section_ids")]
    capacity = plan.get("capacity") or {}
    limit = int(capacity.get("max_output_tokens") or 0)
    safe_budget = int(plan.get("capacity_summary", {}).get("safe_output_budget") or 0)
    cu_counts = [len(row.get("content_unit_ids") or []) for row in batches]
    section_counts = [len(row.get("section_ids") or []) for row in batches]
    utilisation = [
        round(int(row.get("estimated_output_tokens") or 0) / safe_budget, 4)
        for row in batches
        if safe_budget
    ]
    profile = {
        "schema_version": SCHEMA_VERSION,
        "scope_note": scope_note or "未声明作用域",
        "planned_provider_calls": len(batches),
        "planned_logical_sections": sum(section_counts),
        "planned_content_units": int((plan.get("capacity_summary") or {}).get("planned_content_units") or sum(cu_counts)),
        "content_unit_conservation": dict(
            (plan.get("capacity_summary") or {}).get("content_unit_conservation") or {}),
        "sections_per_call_distribution": _distribution(section_counts),
        "content_units_per_call_distribution": _distribution(cu_counts),
        "dependency_wave_count": _wave_count(batches),
        "planned_max_parallelism": int(plan.get("max_parallelism") or 1),
        "capacity": {
            "provider_output_limit": limit,
            "safe_output_budget": safe_budget,
            "output_calibration": (plan.get("output_calibration") or {}).get("provenance") or {},
            "batches_over_measured_batch_ceiling": list(
                (plan.get("capacity_summary") or {}).get("batches_over_measured_batch_ceiling") or []),
            "mean_safe_budget_utilisation": round(sum(utilisation) / len(utilisation), 4) if utilisation else None,
            "min_safe_budget_utilisation": min(utilisation) if utilisation else None,
            "max_safe_budget_utilisation": max(utilisation) if utilisation else None,
        },
        "call_count_explanation": {
            # 下界在 batch_planner 里算（那里看得见合批前的每一行）；画像只转接，
            # 自己再算一遍就是第二把尺——而"还剩多少空间"恰恰是要被对质的数字。
            "lower_bound": dict(
                (plan.get("capacity_summary") or {}).get("call_count_lower_bound") or {
                    "status": "NOT_REPORTED_BY_PLANNER"}),
            "merge_block_reason_distribution": dict(
                (plan.get("capacity_summary") or {}).get("merge_block_reason_distribution") or {
                    "status": "NOT_REPORTED_BY_PLANNER"}),
            "leading_cu_slice_recoveries": sum(
                1 for row in (plan.get("merge_decision_log") or [])
                if row.get("leading_cu_sliced_into_previous_batch")),
        },
        "throttle_gates": [dict(row) for row in THROTTLE_GATES],
        "throttle_gate_counts": {
            "concurrency": sum(1 for row in THROTTLE_GATES if row["kind"] == "concurrency"),
            "dependency": sum(1 for row in THROTTLE_GATES if row["kind"] == "dependency"),
        },
        # 重复限流排查的否定结论也要落账：Round 3 报告里"信号量把并发压成 1"是无从对质的猜测，
        # 全仓扫描（app/ 内 Semaphore / BoundedSemaphore / acquire()）0 命中才是证据。
        "semaphore_present": False,
        "semaphore_scan_scope": "app/**/*.py",
        "semaphore_note": (
            "2026-09-23 扫描 app/ 无信号量、无 acquire()：并发只由上表三处 clamp 决定，"
            "实测并发掉到 1 的原因只能到 observed_concurrency 与 Provider 台账里找。"
        ),
        "wall_clock": {
            "planned_seconds": NOT_MEASURED,
            "reason": (
                "离线工程轮不预测真实墙钟；队列等待是外部系统状态，"
                "只有历史 Run 的第一方台账才有资格给数字。"
            ),
        },
    }

    ledger = [row for row in (concurrency_ledger or []) if isinstance(row, dict)]
    if ledger:
        observed_concurrency = [int(row.get("max_observed_concurrency") or 0) for row in ledger]
        distinct_sections = [int(row.get("max_observed_distinct_sections") or 0) for row in ledger]
        profile["observed_concurrency"] = {
            "source": "concurrency_ledger (first-party, per volume)",
            "max_observed_concurrency": max(observed_concurrency) if observed_concurrency else 0,
            "max_observed_distinct_sections": max(distinct_sections) if distinct_sections else 0,
            "planned_vs_observed_gap": (profile["planned_max_parallelism"] - max(observed_concurrency)) if observed_concurrency else None,
            "soft_latency_events": sum(int(row.get("soft_latency_events") or 0) for row in ledger),
            "rate_limit_events": sum(int(row.get("rate_limit_events") or 0) for row in ledger),
        }
    rows = [row for row in (attempt_history or []) if isinstance(row, dict)]
    if rows:
        bridge = [row.get("bridge_timing") for row in rows if isinstance(row.get("bridge_timing"), dict)]
        waits = [float(row.get("queue_wait_seconds") or 0) for row in bridge]
        work = [float(row.get("worker_latency_seconds") or 0) for row in bridge]
        spans = [float(row.get("total_seconds") or 0) for row in bridge]
        profile["observed_latency"] = {
            "source": "historical run attempt ledger (read-only replay)",
            "attempt_count": len(rows),
            "measured_queue_wait_seconds_total": round(sum(waits), 3),
            "measured_worker_latency_seconds_total": round(sum(work), 3),
            "measured_call_span_seconds_total": round(sum(spans), 3),
            "queue_wait_share": round(sum(waits) / sum(spans), 4) if sum(spans) else None,
            "finish_reason_distribution": _distribution([str(row.get("finish_reason") or "") for row in rows]),
        }
    return profile
