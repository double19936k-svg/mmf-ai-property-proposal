from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Callable

from providers.capability import recommended_parallelism

SUCCESS_STATUSES = {"SUCCESS", "COMPLETED", "SKIPPED_RESUME"}
MAX_RATE_ATTEMPTS = 4
# One slow call is normal on a queue transport; only a streak is evidence.
CONSECUTIVE_BREACHES_REQUIRED = 2


class ConcurrentBatchScheduler:
    """Run independent generation batches with 1-3 workers.

    Failed or gapped batches never satisfy downstream dependencies. Rate limits
    only reduce future concurrency and never cancel healthy in-flight workers.
    """

    def __init__(self, *, provider_name: str, max_parallelism: int | None = None):
        self.provider_name = provider_name
        self.max_parallelism = recommended_parallelism(provider_name, max_parallelism)
        self.current_parallelism = self.max_parallelism
        self.lock = threading.RLock()
        self.completed_batches: list[str] = []
        self.failed_batches: list[str] = []
        self.blocked_batches: list[str] = []
        self.retry_queue: list[dict[str, Any]] = []
        self.rate_limit_events = 0
        self.cancelled_healthy_workers = False
        self._rate_attempts: dict[str, int] = {}
        self.unknown_dependencies: list[str] = []
        self.cycle_batches: list[str] = []
        self.soft_latency_events = 0
        self.soft_latency_policy = "NONE"
        self.soft_latency_recoveries = 0
        self._consecutive_breaches = 0
        self.concurrency_events: list[dict[str, Any]] = []
        self._within_budget_after_soft = 0
        self.future_batch_split = False
        self.allow_future_batch_split = True
        self.latency_reasoning_downgrade_pending = False
        self.max_observed_concurrency = 0
        self.max_observed_distinct_sections = 0
        self._concurrency_samples: list[int] = []
        self.dependency_recovered_batches: list[str] = []

    @property
    def avg_observed_concurrency(self) -> float:
        if not self._concurrency_samples:
            return 0.0
        return round(sum(self._concurrency_samples) / len(self._concurrency_samples), 3)

    def observe_soft_latency(self, *, quality_blocked: bool = False) -> dict[str, Any]:
        """Reduce future concurrency. Never cancel healthy in-flight work.

        Quality-blocked results do not trigger latency reasoning downgrade.
        Prefer concurrency reduction; split future large batches only after
        parallelism is already 1.

        MMF-007C Round 1 measured why one strike is wrong for a queue transport:
        60 of 73 on-time calls exceeded the then-generic 120 s soft budget, so
        3 -> 2 -> 1 happened inside the first two batches and never recovered
        (recovery needs two *consecutive* within-budget calls). The real maximum
        cross-section concurrency of that run was therefore 1, not the reported 3.
        A downgrade now requires ``CONSECUTIVE_BREACHES_REQUIRED`` breaches in a
        row, and every decision is journaled so the next round is audited from
        first-party records instead of being argued about.
        """
        with self.lock:
            self.soft_latency_events += 1
            self._consecutive_breaches += 1
            previous = self.current_parallelism
            downgrade = (
                previous > 1
                and self._consecutive_breaches >= CONSECUTIVE_BREACHES_REQUIRED
            )
            if downgrade:
                self.current_parallelism = max(1, previous - 1)
                self.soft_latency_policy = "REDUCE_FUTURE_CONCURRENCY"
                self.future_batch_split = False
                self.latency_reasoning_downgrade_pending = False
                self._consecutive_breaches = 0
                self.concurrency_events.append({
                    "at": time.time(), "action": "DOWNGRADE",
                    "from": previous, "to": self.current_parallelism,
                    "consecutive_breaches": CONSECUTIVE_BREACHES_REQUIRED,
                })
            elif previous > 1:
                self.soft_latency_policy = "SOFT_BREACH_WATCHING"
            else:
                if self.allow_future_batch_split:
                    self.future_batch_split = True
                    self.soft_latency_policy = "SPLIT_FUTURE_LARGE_BATCH"
                else:
                    self.future_batch_split = False
                    self.soft_latency_policy = "RECORD_ONLY"
                self.latency_reasoning_downgrade_pending = False
                self.concurrency_events.append({
                    "at": time.time(), "action": "RECORD_ONLY",
                    "from": previous, "to": self.current_parallelism,
                    "consecutive_breaches": self._consecutive_breaches,
                })
            self._within_budget_after_soft = 0
            return {
                "policy": self.soft_latency_policy,
                "previous_parallelism": previous,
                "current_parallelism": self.current_parallelism,
                "future_batch_split": self.future_batch_split,
                "latency_reasoning_downgrade_pending": self.latency_reasoning_downgrade_pending,
                "cancelled_healthy_workers": self.cancelled_healthy_workers,
                "consecutive_breaches": self._consecutive_breaches,
            }

    def observe_within_budget(self) -> dict[str, Any]:
        """Bounded recovery: two consecutive within-budget successes restore +1."""
        with self.lock:
            self._consecutive_breaches = 0
            if self.soft_latency_events <= 0:
                return {"policy": self.soft_latency_policy, "current_parallelism": self.current_parallelism}
            self._within_budget_after_soft += 1
            if self._within_budget_after_soft >= 2 and self.current_parallelism < self.max_parallelism:
                self.current_parallelism += 1
                self.soft_latency_recoveries += 1
                self._within_budget_after_soft = 0
                self.concurrency_events.append({
                    "at": time.time(), "action": "RECOVER",
                    "from": self.current_parallelism - 1, "to": self.current_parallelism,
                })
                if self.current_parallelism > 1:
                    self.future_batch_split = False
                    self.latency_reasoning_downgrade_pending = False
                self.soft_latency_policy = (
                    "RECOVERED" if self.current_parallelism >= self.max_parallelism else "BOUNDED_RECOVERY"
                )
            return {
                "policy": self.soft_latency_policy,
                "current_parallelism": self.current_parallelism,
                "recoveries": self.soft_latency_recoveries,
            }

    def _validate_graph(self, batches: list[dict[str, Any]]) -> None:
        known = {row["batch_id"] for row in batches}
        remaining = {row["batch_id"]: set(row.get("dependency_ids") or []) for row in batches}
        for bid, deps in list(remaining.items()):
            unknown = [dep for dep in deps if dep not in known]
            if unknown:
                self.unknown_dependencies.extend(f"{bid}->{dep}" for dep in unknown)
                remaining[bid] = {dep for dep in deps if dep in known}
        progressed = True
        seen: list[str] = []
        while remaining and progressed:
            ready = [bid for bid, deps in remaining.items() if not deps]
            progressed = bool(ready)
            for bid in ready:
                seen.append(bid)
                remaining.pop(bid, None)
            for deps in remaining.values():
                deps.difference_update(ready)
        self.cycle_batches = list(remaining)

    @staticmethod
    def _dependency_satisfied(
        batch: dict[str, Any],
        succeeded: set[str],
        failed: set[str],
        is_section_complete: Callable[[str], bool] | None,
    ) -> bool:
        deps = set(batch.get("dependency_ids") or [])
        unresolved = deps - succeeded
        if not unresolved:
            return True
        section_deps = [str(value) for value in (batch.get("dependency_section_ids") or []) if str(value)]
        # 只有规划器提供了精确Section依赖、且这些Section都已真实完成时，
        # 才允许跨越“同批其他Section失败”的批次级失败。
        return bool(
            unresolved <= failed
            and section_deps
            and is_section_complete
            and all(is_section_complete(section_id) for section_id in section_deps)
        )

    def _ready(
        self,
        batches: list[dict[str, Any]],
        succeeded: set[str],
        in_flight: set[str],
        failed: set[str],
        is_section_complete: Callable[[str], bool] | None = None,
    ) -> list[dict[str, Any]]:
        ready = []
        for batch in batches:
            bid = batch["batch_id"]
            if bid in succeeded or bid in in_flight or bid in failed:
                continue
            if self._dependency_satisfied(batch, succeeded, failed, is_section_complete):
                ready.append(batch)
        return ready

    def run(
        self,
        batches: list[dict[str, Any]],
        execute: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        is_section_complete: Callable[[str], bool] | None = None,
        is_section_settled: Callable[[str], bool] | None = None,
    ) -> dict[str, Any]:
        """`is_section_complete` 只管依赖是否可用；跳过判据另有 `is_section_settled`。

        两者可以差在 CU 缺口上：正文已到达但计划内 Content Unit 还没全部落盘的 Section，
        依赖可以放行，但 Resume 不能把它判成"不用再跑" —— 否则缺失的 CU 永远等不到请求。
        """
        pending = [dict(row) for row in batches if row.get("section_ids")]
        self._validate_graph(pending)
        cyclic = set(self.cycle_batches)
        succeeded: set[str] = set()
        failed: set[str] = set()
        in_flight: dict[str, Any] = {}
        results: dict[str, Any] = {}
        skipped: list[str] = []

        settled = is_section_settled or is_section_complete
        if settled:
            still = []
            for batch in pending:
                remaining = [sid for sid in batch["section_ids"] if not settled(sid)]
                if not remaining:
                    succeeded.add(batch["batch_id"])
                    skipped.append(batch["batch_id"])
                    results[batch["batch_id"]] = {"status": "SKIPPED_RESUME", "section_ids": batch["section_ids"], "provider_calls": 0}
                    self.completed_batches.append(batch["batch_id"])
                    continue
                row = dict(batch)
                row["section_ids"] = remaining
                still.append(row)
            pending = still

        for row in list(pending):
            if row["batch_id"] in cyclic:
                results[row["batch_id"]] = {"status": "BLOCKED_BY_CYCLE", "section_ids": row.get("section_ids")}
                failed.add(row["batch_id"])
                self.blocked_batches.append(row["batch_id"])
                pending = [item for item in pending if item["batch_id"] != row["batch_id"]]

        workers = max(1, min(3, self.current_parallelism))
        with ThreadPoolExecutor(max_workers=3) as pool:
            while pending or in_flight:
                with self.lock:
                    capacity = max(1, min(3, self.current_parallelism)) - len(in_flight)
                    ready = self._ready(pending, succeeded, set(in_flight), failed, is_section_complete)
                    launch = ready[: max(0, capacity)]
                    for batch in launch:
                        if set(batch.get("dependency_ids") or []) & failed and batch["batch_id"] not in self.dependency_recovered_batches:
                            self.dependency_recovered_batches.append(batch["batch_id"])
                        pending = [row for row in pending if row["batch_id"] != batch["batch_id"]]
                        in_flight[batch["batch_id"]] = pool.submit(execute, batch)
                    live = len(in_flight)
                    if live:
                        self.max_observed_concurrency = max(self.max_observed_concurrency, live)
                        self._concurrency_samples.append(live)
                        # 批次数不等于并发度：同一 Section 的多个批次同时挂起，
                        # 对吞吐没有意义（Round 1 就是这样把"3"报成了 1 的实际并行）。
                        outstanding_sections: set[str] = set()
                        for running_id in in_flight:
                            outstanding_sections.update(str(x) for x in (
                                next((row for row in batches if row["batch_id"] == running_id), {})
                                .get("section_ids") or []))
                        self.max_observed_distinct_sections = max(
                            self.max_observed_distinct_sections, len(outstanding_sections))
                if not in_flight:
                    known = {row["batch_id"] for row in batches}
                    pending_ids = {row["batch_id"] for row in pending}
                    still_pending = []
                    for batch in pending:
                        deps = set(batch.get("dependency_ids") or [])
                        missing = deps - succeeded
                        dependency_satisfied = self._dependency_satisfied(batch, succeeded, failed, is_section_complete)
                        if not dependency_satisfied or any(dep not in known for dep in deps) or (missing <= pending_ids and bool(missing)):
                            results[batch["batch_id"]] = {
                                "status": "BLOCKED_BY_DEPENDENCY",
                                "section_ids": batch.get("section_ids"),
                                "dependencies": sorted(missing),
                            }
                            failed.add(batch["batch_id"])
                            self.blocked_batches.append(batch["batch_id"])
                        else:
                            still_pending.append(batch)
                    pending = still_pending
                    break
                done, _ = wait(list(in_flight.values()), return_when=FIRST_COMPLETED)
                finished_ids = [bid for bid, fut in list(in_flight.items()) if fut in done]
                for bid in finished_ids:
                    fut = in_flight.pop(bid)
                    try:
                        result = fut.result()
                    except Exception as exc:  # noqa: BLE001 - worker isolation
                        result = {"status": "FAILED_BATCH", "error": str(exc), "error_code": "PROVIDER_RUNTIME_ERROR"}
                    results[bid] = result
                    code = str(result.get("error_code") or "")
                    status = str(result.get("status") or "")
                    if code == "RATE_LIMIT":
                        with self.lock:
                            self.rate_limit_events += 1
                            self.current_parallelism = max(1, self.current_parallelism - 1)
                            self._rate_attempts[bid] = int(self._rate_attempts.get(bid) or 0) + 1
                            attempt_no = self._rate_attempts[bid]
                        if attempt_no < MAX_RATE_ATTEMPTS:
                            time.sleep(min(0.05 * attempt_no, 0.2))
                            original = next((row for row in batches if row["batch_id"] == bid), {"batch_id": bid, "section_ids": result.get("section_ids") or []})
                            row = dict(original)
                            pending.append(row)
                            self.retry_queue.append({"batch_id": bid, "reason": "RATE_LIMIT", "attempt": attempt_no})
                            continue
                        failed.add(bid)
                        self.failed_batches.append(bid)
                        continue
                    if status in SUCCESS_STATUSES:
                        succeeded.add(bid)
                        if bid not in self.completed_batches:
                            self.completed_batches.append(bid)
                    else:
                        failed.add(bid)
                        if status == "BLOCKED_BY_DEPENDENCY":
                            self.blocked_batches.append(bid)
                        else:
                            self.failed_batches.append(bid)
        succeeded_ids = set(self.completed_batches)
        retry_queue = [
            item for item in self.retry_queue
            if (item.get("batch_id") if isinstance(item, dict) else item) not in succeeded_ids
        ]
        self.retry_queue = retry_queue
        pending_ids = [row["batch_id"] for row in pending if row.get("batch_id") not in succeeded_ids and row.get("batch_id") not in set(self.failed_batches) and row.get("batch_id") not in set(self.blocked_batches)]
        return {
            "status": "COMPLETED" if not self.failed_batches and not self.blocked_batches else "COMPLETED_WITH_GAPS",
            "results": results,
            "completed_batches": list(self.completed_batches),
            "failed_batches": list(self.failed_batches),
            "blocked_batches": list(self.blocked_batches),
            "pending_batches": pending_ids,
            "skipped_batches": skipped,
            "retry_queue": list(self.retry_queue),
            "parallelism": self.current_parallelism,
            "initial_parallelism": workers,
            "rate_limit_events": self.rate_limit_events,
            "cancelled_healthy_workers": self.cancelled_healthy_workers,
            "unknown_dependencies": list(self.unknown_dependencies),
            "cycle_batches": list(self.cycle_batches),
            "soft_latency_events": self.soft_latency_events,
            "soft_latency_policy": self.soft_latency_policy,
            "soft_latency_recoveries": self.soft_latency_recoveries,
            "future_batch_split": self.future_batch_split,
            "max_observed_concurrency": self.max_observed_concurrency,
            "max_observed_distinct_sections": self.max_observed_distinct_sections,
            "avg_observed_concurrency": self.avg_observed_concurrency,
            "concurrency_events": list(self.concurrency_events),
            "consecutive_breaches_required": CONSECUTIVE_BREACHES_REQUIRED,
            "dependency_recovered_batches": list(self.dependency_recovered_batches),
        }
