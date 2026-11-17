"""MMF-007C Round 2 / P1-1：吞吐与并发（R2-P1-1 ①..⑧）。

钉住的行为：并发治理不得因为"一次比预算慢"就把队列桥锁在单并发；
队列桥的每一次调用都要留下第一方时间戳，让下一轮不需要靠文件 mtime 考古。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
APP = PACKAGE_ROOT / "app"
TESTS = PACKAGE_ROOT / "tests"
for item in (APP, TESTS):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from longform.factory import LongformGenerationFactory  # noqa: E402
from longform.scheduler import CONSECUTIVE_BREACHES_REQUIRED, ConcurrentBatchScheduler  # noqa: E402
from providers.capability import (  # noqa: E402
    CAPABILITY_PROFILES,
    PROVIDER_LATENCY_BUDGETS,
    latency_budget_seconds,
    recommended_parallelism,
    resolve_profile,
)
from providers.mock import MockProvider  # noqa: E402
from providers.qoder_internal import QoderInternalProvider  # noqa: E402
import test_mmf007_final_closure as closure  # noqa: E402

# MMF-007C Round 1 Run B 的第一方实测（73 次准点调用，剔除 4 小时休眠造成的离群值）：
# client_total p50 184.1 / p90 407.3 / p95 525.5 秒。见
# 04_Real_Run_Evidence/MMF007C_R2_20260920/throughput_ledger_runB_round1.json。
MEASURED_P50 = 184.1
MEASURED_P95 = 525.5
BRIDGE_WAIT_SECONDS = 3600.0
def _scratch_root() -> Path:
    """Round 2 §4.1 卫生项：临时根被显式重定向时（dist 侧 E2E）scratch 跟着落到包外，
    便携包内保持逐文件不变；未重定向时仍是包内 `runtime/tmp`。"""
    override = os.environ.get("MMF_TEMP_ROOT")
    base = Path(override).expanduser() if override else PACKAGE_ROOT / "runtime" / "tmp"
    return base / "p11throughput"


SCRATCH_ROOT = _scratch_root()


def _provider(root: Path, **overrides) -> QoderInternalProvider:
    config = {
        "provider_name": "qoder_internal",
        "provider_type": "qoder_internal",
        "enabled": True,
        "test_mode": True,
        "queue_dir": str(root / "queue"),
        "wait_timeout": 30,
        "poll_interval": 0.02,
    }
    config.update(overrides)
    return QoderInternalProvider(config, credential_store=object())


class QoderInternalCapabilityProfile(unittest.TestCase):
    """R2-P1-1①：队列桥必须有自己的人才知道慢的档案，不能继承 mock 的 120 秒。"""

    def test_r2_p1_01_profile_exists_and_allows_three_workers(self):
        self.assertIn("qoder_internal", CAPABILITY_PROFILES)
        self.assertEqual(recommended_parallelism("qoder_internal", 3), 3)
        self.assertEqual(CAPABILITY_PROFILES["qoder_internal"]["recommended_parallelism"], 3)

    def test_r2_p1_02_latency_budget_sits_above_measured_tail_and_below_the_wait(self):
        budget = latency_budget_seconds("qoder_internal", "fast", "draft", 1)
        soft = float(budget["soft_latency_budget"])
        hard = float(budget["hard_timeout"])
        self.assertGreaterEqual(soft, MEASURED_P95, "软预算低于实测 p95 会把正常调用判成超时")
        self.assertLess(soft, hard)
        self.assertLess(hard, BRIDGE_WAIT_SECONDS, "硬预算必须早于队列等待上限，否则只是装饰")
        self.assertEqual(PROVIDER_LATENCY_BUDGETS["qoder_internal"]["fast"][0], soft)

    def test_r2_p1_03_generic_fallback_would_have_flagged_most_of_round_one(self):
        """旧口径（无档案 → 120/300）在这条链上一定会误判：把它钉成断言而不是叙述。"""
        legacy_soft = 120.0
        self.assertLess(legacy_soft, MEASURED_P50)
        self.assertEqual(latency_budget_seconds("kimi_moonshot", "fast", "draft", 1)["soft_latency_budget"], 180)


class SoftLatencyGovernor(unittest.TestCase):
    """R2-P1-1④⑤：降并发要连着两次超预算；一次慢不算证据。"""

    def _scheduler(self) -> ConcurrentBatchScheduler:
        return ConcurrentBatchScheduler(provider_name="qoder_internal", max_parallelism=3)

    def test_r2_p1_04_single_breach_does_not_downgrade(self):
        scheduler = self._scheduler()
        policy = scheduler.observe_soft_latency()
        self.assertEqual(policy["current_parallelism"], 3)
        self.assertEqual(policy["policy"], "SOFT_BREACH_WATCHING")
        self.assertEqual(scheduler.concurrency_events, [])

    def test_r2_p1_05_consecutive_breaches_downgrade_one_step_each_time(self):
        scheduler = self._scheduler()
        steps = [scheduler.observe_soft_latency()["current_parallelism"] for _ in range(5)]
        self.assertEqual(steps, [3, 2, 2, 1, 1])
        self.assertEqual(scheduler.current_parallelism, 1)
        self.assertEqual(
            [(row["action"], row["from"], row["to"]) for row in scheduler.concurrency_events][:2],
            [("DOWNGRADE", 3, 2), ("DOWNGRADE", 2, 1)])
        self.assertEqual(scheduler.concurrency_events[-1]["action"], "RECORD_ONLY",
                         "已经降到 1 之后只能如实记录，不能假装还能降")
        self.assertEqual(CONSECUTIVE_BREACHES_REQUIRED, 2)

    def test_r2_p1_06_alternating_latency_never_clamps_to_one(self):
        """上一轮的真实形态：慢-快-慢-快。旧逻辑两步锁死，新逻辑一步不降。"""
        scheduler = self._scheduler()
        for _ in range(6):
            scheduler.observe_soft_latency()
            scheduler.observe_within_budget()
        self.assertEqual(scheduler.current_parallelism, 3)
        self.assertEqual(scheduler.concurrency_events, [])

    def test_r2_p1_07_bounded_recovery_still_works(self):
        scheduler = self._scheduler()
        for _ in range(2 * CONSECUTIVE_BREACHES_REQUIRED):
            scheduler.observe_soft_latency()
        self.assertEqual(scheduler.current_parallelism, 1)
        scheduler.observe_within_budget()
        self.assertEqual(scheduler.current_parallelism, 1)
        recovered = scheduler.observe_within_budget()
        self.assertEqual(recovered["current_parallelism"], 2)
        self.assertIn(recovered["policy"], {"BOUNDED_RECOVERY", "RECOVERED"})
        self.assertIn("RECOVER", [row["action"] for row in scheduler.concurrency_events])


class ConcurrentDispatchIsReal(unittest.TestCase):
    """R2-P1-1⑥：并发度要按"不同 Section 同时在飞"计，批次数不算并发。"""

    def setUp(self):
        self.root = SCRATCH_ROOT / f"dispatch-{os_pid()}"
        self.root.mkdir(parents=True, exist_ok=True)

    def test_r2_p1_08_independent_sections_run_three_at_a_time(self):
        batches = [{
            "batch_id": f"GB-{index:02d}",
            "section_ids": [f"S0{index}-01"],
            "dependency_ids": [],
            "content_unit_ids": [],
        } for index in range(1, 7)]
        live = {"now": 0, "max": 0}
        guard = threading.Lock()

        def execute(batch):
            with guard:
                live["now"] += 1
                live["max"] = max(live["max"], live["now"])
            time.sleep(0.12)
            with guard:
                live["now"] -= 1
            return {"status": "SUCCESS", "section_ids": batch["section_ids"], "provider_calls": 1}

        scheduler = ConcurrentBatchScheduler(provider_name="qoder_internal", max_parallelism=3)
        result = scheduler.run(batches, execute)
        self.assertEqual(sorted(result["completed_batches"]), sorted(row["batch_id"] for row in batches),
                         "完成顺序本身就是并行的证据，不做全序断言")
        self.assertEqual(result["max_observed_concurrency"], 3)
        self.assertEqual(result["max_observed_distinct_sections"], 3)
        self.assertGreaterEqual(live["max"], 2, "线程池没有真的并行")

    def test_r2_p1_09_same_section_batches_are_not_cross_section_concurrency(self):
        batches = [{
            "batch_id": f"GB-{index:02d}",
            "section_ids": ["S06-01"],
            "dependency_ids": [],
            "content_unit_ids": [],
        } for index in range(1, 4)]
        scheduler = ConcurrentBatchScheduler(provider_name="qoder_internal", max_parallelism=3)
        result = scheduler.run(batches, lambda batch: {"status": "SUCCESS", "provider_calls": 1})
        self.assertGreaterEqual(result["max_observed_concurrency"], 1)
        self.assertEqual(result["max_observed_distinct_sections"], 1)


class BridgeFirstPartyTiming(unittest.TestCase):
    """R2-P1-1⑦⑧：账本与 Run 目录都要留下绝对时间戳；已答响应不许被删掉。"""

    JOB_ID = "RUN-S06-01-GB-01-timing"

    def setUp(self):
        import shutil
        self.root = SCRATCH_ROOT / f"timing-{os_pid()}-{int(time.monotonic() * 10000)}"
        for folder in ("queue/out", "queue/in", "queue/ledger", "queue/claims",
                       "queue/answers", "task"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.provider = _provider(self.root)
        self.request = {
            "task_id": "RUN-S06-01-GB-01",
            "job_id": self.JOB_ID,
            "system_prompt": "你是物业投标方案撰写引擎。",
            "prompt": "请按 CU 契约产出内容单元正文。",
            "generation_mode": "longform_batch",
            "response_protocol": "cu_envelope",
            "required_keys": ["cu_envelope_text"],
            "attempt_mode": "generate_batch",
            "batch_id": "GB-01",
            "section_ids": ["S06-01"],
            "content_unit_contracts": [{"content_unit_id": "CU-S06-01-01", "archetype": "NARRATIVE"}],
        }

    def _serve_as_worker(self) -> None:
        """按 WORKER_BRIEF 的三步复现：claim → answer → response（顺序即时间序）。"""
        queue = self.root / "queue"
        request_path = queue / "out" / f"{self.JOB_ID}.request.json"
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and not request_path.is_file():
            time.sleep(0.02)
        if not request_path.is_file():
            return
        (queue / "claims" / f"{self.JOB_ID}.claim.json").write_text(
            json.dumps({"job_id": self.JOB_ID, "claimed_at": time.time(), "worker": "qoder-test-t1"},
                       ensure_ascii=False), encoding="utf-8")
        time.sleep(0.06)
        (queue / "answers" / f"{self.JOB_ID}.answer").write_text("正文", encoding="utf-8")
        time.sleep(0.06)
        (queue / "in" / f"{self.JOB_ID}.response.json").write_text(json.dumps({
            "job_id": self.JOB_ID, "worker": "qoder-test-t1", "status": "OK",
            "body": "<<<CU_START:CU-S06-01-01>>>桥头岗交接记录齐全，异常十分钟内上报。<<<CU_END>>>",
            "submitted_at": time.time(),
        }, ensure_ascii=False), encoding="utf-8")

    def _events(self) -> list[dict]:
        path = self.root / "queue" / "ledger" / "calls.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]

    def _call(self) -> dict:
        thread = threading.Thread(target=self._serve_as_worker, daemon=True)
        thread.start()
        output = self.provider.invoke_structured(self.request, self.root / "task" / "call1")
        thread.join(timeout=60)
        return output

    def test_r2_p1_10_enqueue_and_receive_events_carry_absolute_timestamps(self):
        output = self._call()
        enqueued = next(row for row in self._events() if row["event"] == "REQUEST_ENQUEUED")
        received = next(row for row in self._events() if row["event"] == "RESPONSE_RECEIVED")
        self.assertIn("CU_START", output["cu_envelope_text"])
        self.assertRegex(enqueued["queued_at_iso"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertTrue(enqueued.get("wait_budget_seconds"), "等待上限要留在账本里")
        self.assertGreater(float(received["received_at"]), float(enqueued["queued_at"]))
        self.assertEqual(received["content_unit_count"], 1)
        for key in ("claimed_at", "answered_at", "submitted_at"):
            self.assertIsNotNone(received.get(key), f"{key} 必须第一方记录，不能靠 mtime 考古")
        self.assertGreater(received["queue_wait_seconds"], 0)
        self.assertGreater(received["worker_latency_seconds"], 0)
        self.assertGreater(received["handoff_seconds"], 0)
        timing = output["provider_metadata"]["bridge_timing"]
        self.assertEqual(timing["total_seconds"], received["wait_seconds"])
        self.assertEqual(timing["worker_latency_seconds"], received["worker_latency_seconds"])

    def test_r2_p1_11_consumed_response_is_archived_not_deleted(self):
        self._call()
        queue = self.root / "queue"
        self.assertFalse((queue / "in" / f"{self.JOB_ID}.response.json").exists(),
                         "in/ 必须清空，否则晚到扫描会把同一份内容再算一次")
        archived = queue / "ledger" / f"{self.JOB_ID}.response.json"
        self.assertTrue(archived.is_file(), "已消费的响应是吞吐证据，不能删")
        self.assertEqual(json.loads(archived.read_text(encoding="utf-8"))["worker"], "qoder-test-t1")

    def test_r2_p1_12_timeout_event_still_leaves_a_recovery_timestamp(self):
        self.provider.config["wait_timeout"] = 0.4
        with self.assertRaises(Exception) as caught:
            self.provider.invoke_structured(self.request, self.root / "task" / "call2")
        self.assertEqual(getattr(caught.exception, "error_code", ""), "TIMEOUT")
        timed_out = next(row for row in self._events() if row["event"] == "REQUEST_TIMED_OUT")
        self.assertTrue(timed_out["timed_out_at_iso"])
        self.assertEqual(timed_out["content_unit_ids"], ["CU-S06-01-01"],
                         "超时记录要点名是哪几个 CU 悬空，晚到回收才知道找回什么")
        self.assertTrue(timed_out["late_response_recoverable"])


class ConcurrencyLedgerIsReported(unittest.TestCase):
    """R2-P1-1⑬：并发结论必须进产物，不能只活在内存里的 scheduler 对象上。

    Round 1 的 generation_raw.json 只有自报的 MAX_OBSERVED_CONCURRENCY=3，
    而实测跨节并发是 1——报告里无从对质。这一条钉住"台账随摘要落盘"。
    """

    def test_r2_p1_13_word_summary_carries_the_concurrency_ledger(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            factory, sid, _cus = closure._cu_factory(Path(tmp), MockProvider(closure.MOCK_CONFIG))
            summary = factory.generate_word([sid])
        self.assertIn("CONCURRENCY_LEDGER", summary)
        self.assertIn("MAX_OBSERVED_DISTINCT_SECTIONS", summary)
        ledger = summary["CONCURRENCY_LEDGER"]
        self.assertEqual(len(ledger), 1, "一次 generate_word 要留下一条分卷台账")
        row = ledger[0]
        self.assertIn(sid, row["scope_section_ids"])
        self.assertGreaterEqual(row["scope_section_count"], 1)
        self.assertGreaterEqual(row["batch_count"], 1)
        self.assertEqual(row["max_observed_distinct_sections"], 1)
        self.assertGreaterEqual(row["initial_parallelism"], 1)
        self.assertLessEqual(row["final_parallelism"], row["initial_parallelism"])
        self.assertLessEqual(row["max_observed_concurrency"], row["initial_parallelism"])
        self.assertEqual(row["soft_latency_events"], 0)
        self.assertEqual(row["events"], [])
        self.assertEqual(summary["MAX_OBSERVED_DISTINCT_SECTIONS"], row["max_observed_distinct_sections"])


def os_pid() -> int:
    import os
    return os.getpid()


if __name__ == "__main__":
    unittest.main()
