from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from workflow_timing import (  # noqa: E402
    ensure_workflow,
    finish_workflow,
    history_duration,
    load_workflow_timing,
    record_longform_attempt,
    record_stage_duration,
    union_interval_seconds,
    workflow_audit_fields,
)


class WorkflowTimingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temp.name) / "run"
        self.run_dir.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_time_01_tender_stage_sum_is_100_seconds(self) -> None:
        ensure_workflow(self.run_dir, "tender_file")
        for name, seconds in (
            ("local_parse", 10),
            ("requirement_recognition_and_structuring", 20),
            ("brief_generation", 5),
            ("longform_generation", 60),
            ("final_artifact_qa", 5),
        ):
            record_stage_duration(self.run_dir, name, seconds)
        self.assertEqual(load_workflow_timing(self.run_dir)["total_processing_seconds"], 100.0)

    def test_time_02_user_idle_is_not_counted(self) -> None:
        ensure_workflow(self.run_dir, "tender_file")
        record_stage_duration(self.run_dir, "local_parse", 40)
        finish_workflow(self.run_dir, "waiting_for_user")
        # A 30-minute wall-clock/user gap is deliberately not represented by a stage.
        ensure_workflow(self.run_dir, "tender_file")
        record_stage_duration(self.run_dir, "longform_generation", 60)
        result = finish_workflow(self.run_dir, "completed")
        self.assertEqual(result["total_processing_seconds"], 100.0)
        self.assertIsNone(result["user_idle_seconds"])

    def test_time_03_concurrent_workers_use_interval_union(self) -> None:
        self.assertEqual(union_interval_seconds([(0, 120), (0, 100), (0, 90)]), 120.0)
        record_longform_attempt(self.run_dir, 120, planning_seconds=0, repair_seconds=0)
        self.assertEqual(load_workflow_timing(self.run_dir)["total_processing_seconds"], 120.0)

    def test_time_04_resume_accumulates_only_active_time(self) -> None:
        ensure_workflow(self.run_dir, "new_plan")
        record_stage_duration(self.run_dir, "longform_generation", 8 * 60)
        finish_workflow(self.run_dir, "failed")
        # Two hours of shutdown are not added. Resume adds only five active minutes.
        ensure_workflow(self.run_dir, "new_plan")
        record_stage_duration(self.run_dir, "longform_generation", 5 * 60)
        self.assertEqual(finish_workflow(self.run_dir, "completed")["total_processing_seconds"], 13 * 60)

    def test_time_05_repair_is_included_without_overlap(self) -> None:
        record_stage_duration(self.run_dir, "longform_generation", 10 * 60)
        record_stage_duration(self.run_dir, "section_repair", 2 * 60)
        fields = workflow_audit_fields(self.run_dir)
        self.assertEqual(fields["total_processing_seconds"], 12 * 60)
        self.assertEqual(fields["repair_seconds"], 2 * 60)

    def test_time_06_legacy_generation_is_not_claimed_as_total(self) -> None:
        legacy = history_duration(self.run_dir, 900)
        self.assertIsNone(legacy["total_processing_seconds"])
        self.assertEqual(legacy["generation_stage_seconds"], 900)
        self.assertEqual(legacy["duration_label"], "生成阶段耗时")
        self.assertEqual(legacy["duration_scope"], "legacy_generation_stage_only")

    def test_timing_file_contains_stable_workflow_id_and_audit_fields(self) -> None:
        first = ensure_workflow(self.run_dir, "new_plan")
        record_stage_duration(self.run_dir, "artifact_assembly", 3.25)
        second = ensure_workflow(self.run_dir, "new_plan")
        self.assertEqual(first["workflow_id"], second["workflow_id"])
        fields = workflow_audit_fields(self.run_dir)
        self.assertEqual(fields["artifact_seconds"], 3.25)
        self.assertIn("stage_timings", fields)
        payload = json.loads((self.run_dir / "workflow_timing.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["workflow_id"], first["workflow_id"])

    def test_new_plan_complete_workflow_uses_total_processing_label(self) -> None:
        ensure_workflow(self.run_dir, "new_plan")
        for name, seconds in (
            ("project_input_processing", 2),
            ("knowledge_recommendation", 3),
            ("generation_preparation", 1),
            ("document_planning", 4),
            ("longform_generation", 10),
            ("content_and_compliance_qa", 2),
            ("artifact_assembly", 1),
            ("final_artifact_qa", 1),
        ):
            record_stage_duration(self.run_dir, name, seconds)
        finish_workflow(self.run_dir, "completed")
        history = history_duration(self.run_dir, 10)
        self.assertEqual(history["total_processing_seconds"], 24)
        self.assertEqual(history["duration_label"], "总处理耗时")
        self.assertEqual(history["duration_scope"], "complete_workflow_processing")


if __name__ == "__main__":
    unittest.main()
