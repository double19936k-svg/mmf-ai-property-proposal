from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from governance.text_sanitize import collapse_repeated_conditionals, replace_outsourcing_phrase
from longform.batch_planner import plan_generation_batches
from longform.eta import append_latency_sample, estimate_runtime
from longform.factory import LongformGenerationFactory, _repair_customer_text
from longform.orchestrator import generate_longform
from longform.reasoning import adjust_for_failure, policy_reasoning_for_stage
from longform.scheduler import ConcurrentBatchScheduler
from planning.planner import build_adaptive_outline, build_planning_bundle
from providers.capability import resolve_profile
from providers.mock import MockProvider


def _pack() -> dict:
    requirements = [
        {"requirement_id": "REQ-0001", "normalized_requirement": "项目位于青岛市崂山区香港东路108号", "domain": "project_fact", "mandatory_level": "MUST", "confirmation_status": "CONFIRMED"},
        {"requirement_id": "REQ-0002", "normalized_requirement": "安全秩序管理含门岗巡逻与异常处置", "domain": "security", "mandatory_level": "MUST", "confirmation_status": "CONFIRMED"},
        {"requirement_id": "REQ-0003", "normalized_requirement": "工程巡检维保与故障闭环", "domain": "other", "mandatory_level": "MUST", "confirmation_status": "CONFIRMED"},
        {"requirement_id": "REQ-0004", "normalized_requirement": "客户投诉闭环与回访", "domain": "sla_kpi", "mandatory_level": "MUST", "confirmation_status": "CONFIRMED", "scoring_item_id": "SCR-01"},
    ]
    return {
        "schema_version": "tender-requirement-pack-v0.1",
        "pack_id": "PACK-TEST-LONGFORM",
        "status": "ready_for_plan",
        "project_facts": {
            "project_name": {"value": "示例滨海科技园"},
            "location": {"value": "青岛市崂山区香港东路108号"},
            "gross_area": {"value": "252567平方米"},
        },
        "service_scope": {"included": [], "excluded": [{"text": "会议会务不在本次范围"}], "deprioritized": [], "conditional": []},
        "requirements": requirements,
        "scoring_items": [{"scoring_item_id": "SCR-01", "must_respond": True, "label": "服务方案"}],
        "confirmation": {"ready_for_brief_seed": True},
    }


def _brief() -> dict:
    return {
        "project_name": "示例滨海科技园",
        "project_type": "综合体",
        "scenario": "完整物业服务方案",
        "medium": "WORD",
        "provider_name": "mock",
        "requirements": "安全、工程、客服闭环",
        "speed_profile": "balanced",
    }


def _selection() -> dict:
    return {
        "provider_name": "mock",
        "recommended_positive": [{"ku_id": "KU-0001"}],
        "applicable_guardrails": [],
        "knowledge_usage_contracts": [{"ku_id": "KU-0001", "selection_status": "SELECTED", "usable_content": "闭环方法", "language_level": "method"}],
        "selected_positive_ku_ids": ["KU-0001"],
        "auto_selected_positive_ids": ["KU-0001"],
    }


def _provider(name="mock"):
    return MockProvider({"provider_name": name, "provider_type": "mock", "enabled": True, "model": "mock-longform"})


def _write_run(root: Path, pack: dict, brief: dict, selection: dict) -> Path:
    run = root / "run"
    (run / "tender").mkdir(parents=True)
    (run / "tender" / "requirement_pack.json").write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
    (run / "brief.json").write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
    (run / "knowledge_selection.json").write_text(json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8")
    return run


def _complex_pack() -> dict:
    pack = _pack()
    domains = [
        "project_fact", "security", "staffing", "service_hours", "sla_kpi",
        "exclusion", "scoring", "quality", "environment", "emergency",
    ]
    extra = []
    for index in range(1, 22):
        domain = domains[index % len(domains)]
        extra.append({
            "requirement_id": f"REQ-C{index:02d}",
            "normalized_requirement": f"复杂项目必须覆盖{domain}专项要求{index}并形成可检查记录",
            "domain": domain,
            "mandatory_level": "MUST",
            "confirmation_status": "CONFIRMED",
            "scoring_item_id": f"SCR-C{index:02d}" if index % 3 == 0 else None,
        })
    pack["requirements"] = list(pack["requirements"]) + extra
    pack["scoring_items"] = list(pack.get("scoring_items") or []) + [
        {"scoring_item_id": f"SCR-C{index:02d}", "must_respond": True, "label": f"评分{index}"}
        for index in range(3, 22, 3)
    ]
    pack["project_facts"]["project_type"] = {"value": "产业园综合体"}
    return pack


class CountingProvider(MockProvider):
    def __init__(self, config=None):
        super().__init__(config or {"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock-r2"})
        self.calls = 0
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.seen = []

    def invoke_structured(self, request, task_dir):
        with self.lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.seen.append({"mode": request.get("generation_mode"), "stage": request.get("stage"), "effort": request.get("reasoning_effort"), "thinking": request.get("enable_thinking")})
        try:
            return super().invoke_structured(request, task_dir)
        finally:
            with self.lock:
                self.active -= 1


class AdaptiveAndBatchTest(unittest.TestCase):
    def test_adaptive_section_count_not_fixed_27(self):
        simple, simple_decision = build_adaptive_outline(_pack(), _brief())
        complex_outline, complex_decision = build_adaptive_outline(_complex_pack(), {**_brief(), "project_type": "产业园综合体", "scenario": "投标全套服务方案"})
        simple_n = sum(len(ch["sections"]) for ch in simple)
        complex_n = sum(len(ch["sections"]) for ch in complex_outline)
        self.assertNotEqual(simple_n, 27)
        self.assertNotEqual(simple_n, complex_n)
        self.assertGreaterEqual(simple_n, 12)
        self.assertLessEqual(simple_n, 18)
        self.assertGreaterEqual(complex_n, 25)
        self.assertTrue(simple_decision["decision_reason"])
        self.assertFalse(simple_decision["fixed_section_count"])
        self.assertFalse(simple_decision["planning_provider_invoked"])
        simple_bundle = build_planning_bundle(_pack(), _brief(), _selection(), production=True)
        complex_bundle = build_planning_bundle(_complex_pack(), {**_brief(), "project_type": "产业园综合体"}, _selection(), production=True)
        self.assertEqual(simple_bundle["requirement_matrix"]["coverage_summary"]["must_mapped"], simple_bundle["requirement_matrix"]["coverage_summary"]["must_total"])
        self.assertEqual(complex_bundle["requirement_matrix"]["coverage_summary"]["must_mapped"], complex_bundle["requirement_matrix"]["coverage_summary"]["must_total"])
        self.assertGreater(complex_bundle["adaptive_section_decision"]["selected_section_count"], simple_bundle["adaptive_section_decision"]["selected_section_count"])

    def test_batch_planner_compresses_calls_and_keeps_sections(self):
        bundle = build_planning_bundle(_complex_pack(), {**_brief(), "project_type": "产业园综合体"}, _selection(), production=True)
        contracts = bundle["section_contracts"]["contracts"]
        self.assertGreaterEqual(len(contracts), 20)
        plan = plan_generation_batches(contracts, bundle["dependency_map"], provider_name="mock", speed_profile="balanced")
        self.assertLess(plan["generation_batch_count"], plan["logical_section_count"])
        self.assertLessEqual(plan["generation_batch_count"], 20)
        self.assertTrue(plan["batches"])
        over = [row for row in plan["batches"] if row["estimated_output_tokens"] > 8192]
        self.assertFalse(over)

    def test_batch_rejects_duplicate_and_unrequested(self):
        factory = LongformGenerationFactory(
            run_root=Path(tempfile.mkdtemp()),
            provider=_provider(),
            provider_name="mock",
            inputs={"word_plan": {}, "requirement_matrix": {}, "section_contracts": {"contracts": [], "process_contracts": []}, "global_state": {"canonical_terms": {}, "client_requirements": [], "project_facts": {}, "staffing": {}, "service_hours": {}, "sla_kpi": {}, "service_scope": {}, "excluded_scope": [], "commitment_registry": [], "topic_ownership": [], "canonical_roles": {}}, "ppt_plan": {}, "dependency_map": {"dependencies": []}, "knowledge_selection": {"knowledge_usage_contracts": []}, "require_section_min": False},
        )
        fragments, issues = factory._split_batch_output(
            {"sections": [{"section_id": "S01-01", "title": "a"}, {"section_id": "S01-01", "title": "b"}, {"section_id": "S99-99", "title": "x"}]},
            ["S01-01", "S01-02"],
        )
        self.assertIn("duplicate:S01-01", issues)
        self.assertIn("unrequested:S99-99", issues)
        self.assertIn("missing:S01-02", issues)
        self.assertNotIn("S01-01", fragments)

    def test_stage_reasoning_maps_and_retries(self):
        self.assertEqual(policy_reasoning_for_stage("balanced", "planning"), "high")
        self.assertNotEqual(policy_reasoning_for_stage("fast", "draft"), "xhigh")
        grok_draft = resolve_profile("grok_build", {"model": "grok"}, speed_profile="balanced", stage="draft")
        self.assertEqual(grok_draft["effective_settings"]["reasoning_effort"], "medium")
        grok_fast = resolve_profile("grok_build", {"model": "grok"}, speed_profile="fast", stage="draft")
        self.assertEqual(grok_fast["effective_settings"]["reasoning_effort"], "low")
        grok_deep = resolve_profile("grok_build", {"model": "grok"}, speed_profile="deep", stage="draft")
        self.assertEqual(grok_deep["effective_settings"]["reasoning_effort"], "high")
        grok_plan = resolve_profile("grok_build", {"model": "grok"}, speed_profile="fast", stage="planning")
        self.assertEqual(grok_plan["requested_settings"]["product_reasoning_level"], "high")
        qwen = resolve_profile("qwen_modelstudio", {"model": "qwen-flash"}, speed_profile="deep", stage="draft")
        self.assertFalse(qwen["effective_settings"]["ui_thinking_enabled"])
        self.assertEqual(qwen["effective_settings"]["effective_reasoning"], "disabled")
        qwen_fast = resolve_profile("qwen_modelstudio", {}, speed_profile="fast", stage="draft")
        self.assertFalse(qwen_fast["effective_settings"]["ui_thinking_enabled"])
        kimi_fast = resolve_profile("kimi_moonshot", {}, speed_profile="fast", stage="draft")
        self.assertEqual(kimi_fast["effective_settings"]["reasoning_effort"], "low")
        kimi_bal = resolve_profile("kimi_moonshot", {}, speed_profile="balanced", stage="draft")
        self.assertEqual(kimi_bal["effective_settings"]["reasoning_effort"], "high")
        up, action = adjust_for_failure("low", "QUALITY_FAILURE", ["low", "medium", "high"])
        self.assertEqual(action, "QUALITY_ESCALATION")
        self.assertEqual(up, "medium")
        down, action = adjust_for_failure("high", "LATENCY_FAILURE", ["low", "medium", "high"])
        self.assertEqual(action, "LATENCY_DOWNGRADE")
        self.assertEqual(down, "medium")

    def test_concurrency_and_rate_limit(self):
        hits = []
        lock = threading.Lock()

        rate_hits = {"GB-02": 0}

        def work(batch):
            with lock:
                hits.append(("start", batch["batch_id"], time.time()))
            time.sleep(0.12)
            if batch["batch_id"] == "GB-02":
                rate_hits["GB-02"] += 1
                if rate_hits["GB-02"] == 1:
                    return {"status": "RATE_LIMIT", "error_code": "RATE_LIMIT"}
            with lock:
                hits.append(("end", batch["batch_id"], time.time()))
            return {"status": "SUCCESS"}

        batches = [
            {"batch_id": "GB-01", "section_ids": ["A"], "dependency_ids": [], "parallel_group": 1},
            {"batch_id": "GB-02", "section_ids": ["B"], "dependency_ids": [], "parallel_group": 1},
            {"batch_id": "GB-03", "section_ids": ["C"], "dependency_ids": [], "parallel_group": 1},
        ]
        scheduler = ConcurrentBatchScheduler(provider_name="mock", max_parallelism=3)
        out = scheduler.run(batches, work)
        self.assertGreaterEqual(scheduler.rate_limit_events, 1)
        self.assertLessEqual(out["parallelism"], 3)
        self.assertFalse(out["cancelled_healthy_workers"])
        starts = [row for row in hits if row[0] == "start"]
        self.assertGreaterEqual(len(starts), 3)

    def test_eta_unknown_without_history_and_range_with_history(self):
        unknown = estimate_runtime(provider="unknown_engine", mode="balanced", stage="draft")
        self.assertEqual(unknown["display"], "暂无足够历史数据")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latency.jsonl"
            for seconds in (600, 720, 900, 960):
                append_latency_sample(path, {
                    "schema_version": "latency-history-v2",
                    "provider": "mock",
                    "model": "mock",
                    "mode": "balanced",
                    "stage": "draft",
                    "elapsed_seconds": seconds,
                    "success": True,
                    "eligible_for_eta": True,
                    "size_bucket": "single_1",
                    "section_count": 1,
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "token_provenance": "estimate",
                    "run_id": f"hist-{seconds}",
                    "this_run_calls": 1,
                })
            eta = estimate_runtime(provider="mock", model="mock", mode="balanced", stage="draft", history_path=path, section_count=1)
            self.assertEqual(eta["status"], "range")
            self.assertIn("约", eta["display"])
            self.assertNotIn("18分32秒", eta["display"])

    def test_post_processing_idempotency(self):
        text = "外委单位按月度安排"
        once = _repair_customer_text(text)
        twice = _repair_customer_text(once)
        self.assertEqual(once, twice)
        polluted = "如如经确认使用外委方式方式，按按项目确认"
        cleaned = collapse_repeated_conditionals(polluted)
        self.assertNotIn("如如", cleaned)
        self.assertNotIn("方式方式", cleaned)
        self.assertNotIn("按按项目确认", cleaned)
        self.assertEqual(replace_outsourcing_phrase("如采用外委方式"), "如采用外委方式")

    def test_offline_generation_batches_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = _complex_pack()
            brief = {**_brief(), "project_type": "产业园综合体", "scenario": "完整物业服务方案", "speed_profile": "balanced"}
            run = _write_run(Path(tmp), pack, brief, _selection())
            provider = CountingProvider()
            result = generate_longform(
                run_dir=run,
                provider=provider,
                provider_name="mock",
                brief=brief,
                selection=_selection(),
                selected_ids=["KU-0001"],
                task_mode="full_longform",
                require_section_min=False,
                speed_profile="balanced",
                history_path=Path(tmp) / "latency.jsonl",
            )
            word = result["word"]
            self.assertGreaterEqual(word["logical_section_count"], 20)
            self.assertLess(word["provider_call_count"], word["logical_section_count"])
            self.assertGreaterEqual(word["logical_section_count"], 20)
            self.assertEqual(provider.calls, word["provider_call_count"])
            self.assertTrue((run / "generation_batches.json").is_file())
            self.assertTrue((run / "adaptive_section_decision.json").is_file())
            self.assertTrue((run / "checkpoint" / "state.json").is_file())
            decision = json.loads((run / "adaptive_section_decision.json").read_text(encoding="utf-8"))
            self.assertFalse(decision["planning_provider_invoked"])
            self.assertEqual(decision["planning_execution"], "local_deterministic")
            self.assertEqual(result["planning"]["provider_invoked"], False)
            first_section = decision["selected_section_ids"][0]
            self.assertTrue((run / "longform" / "word" / "sections" / first_section / "fragment.json").is_file())
            checkpoint = json.loads((run / "checkpoint" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["schema_version"], "longform-checkpoint-v0.2")
            calls_before = provider.calls
            generate_longform(
                run_dir=run,
                provider=provider,
                provider_name="mock",
                brief=brief,
                selection=_selection(),
                selected_ids=["KU-0001"],
                task_mode="full_longform",
                require_section_min=False,
                speed_profile="balanced",
                history_path=Path(tmp) / "latency.jsonl",
            )
            self.assertEqual(provider.calls, calls_before)
            self.assertEqual(result["quality_regression"]["status"], "PASS")
            self.assertIn("requested_settings", result["capability"])
            self.assertIn("effective_settings", result["capability"])

    def test_partial_batch_resume_skips_pass_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(Path(tmp), _pack(), _brief(), _selection())
            provider = CountingProvider()
            generate_longform(run_dir=run, provider=provider, provider_name="mock", brief=_brief(), selection=_selection(), selected_ids=["KU-0001"], section_ids=["S03-01", "S03-02"], task_mode="standard", require_section_min=False)
            s01 = run / "longform" / "word" / "sections" / "S03-01"
            self.assertTrue((s01 / "fragment.json").is_file())
            before_attempts = list(s01.glob("provider_attempt_*"))
            status = json.loads((run / "longform" / "word" / "sections" / "S03-02" / "status.json").read_text(encoding="utf-8"))
            status["status"] = "FAILED_SECTION"
            (run / "longform" / "word" / "sections" / "S03-02" / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
            for name in ("fragment.json", "provider_raw.json", "qa.json", "governance.json"):
                (run / "longform" / "word" / "sections" / "S03-02" / name).unlink(missing_ok=True)
            before = provider.calls
            generate_longform(run_dir=run, provider=provider, provider_name="mock", brief=_brief(), selection=_selection(), selected_ids=["KU-0001"], section_ids=["S03-01", "S03-02"], task_mode="standard", require_section_min=False)
            self.assertGreater(provider.calls, before)
            self.assertTrue((s01 / "fragment.json").is_file())
            after_attempts = list(s01.glob("provider_attempt_*"))
            self.assertEqual(len(before_attempts), len(after_attempts))


if __name__ == "__main__":
    raise SystemExit(unittest.main())
