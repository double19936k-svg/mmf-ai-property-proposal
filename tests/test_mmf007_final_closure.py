"""Acceptance tests for MMF-007 Blocker 3 and Blocker 4."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1] / "app"
TESTS = Path(__file__).resolve().parent
for item in (APP, TESTS):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

import paths
import test_mmf006e_r21_q01_q04 as r21
from test_mmf007c_r4_final_state_contract import _write_final_artifact_qa
from longform.factory import LongformGenerationFactory
from planning.hierarchy import build_document_hierarchy
from planning.planner import build_planning_bundle
from providers.mock import MockProvider
from run_layout import finalize_run

MOCK_CONFIG = {"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock-final-closure"}


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _authority_fixture(root: Path, run_id: str = "authority-run") -> tuple[Path, SimpleNamespace]:
    runs = root / "runs"
    output = root / "output"
    run = runs / run_id
    (run / "volumes").mkdir(parents=True)
    (run / "checkpoint").mkdir()
    (run / "artifact").mkdir()
    (run / "V01.docx").write_bytes(b"PK\x03\x04fixture")
    (run / "volumes" / "V01.docx").write_bytes(b"PK\x03\x04volume")
    (run / "11_volume_generation_plan.json").write_text(json.dumps({"volumes": [{"volume_id": "V01", "generation_status": "PLANNED", "section_ids": ["S01-01"], "content_unit_ids": ["CU-S01-01-01"]}]}), encoding="utf-8")
    section = run / "longform" / "word" / "sections" / "S01-01"
    section.mkdir(parents=True)
    (section / "status.json").write_text(json.dumps({"status": "COMPLETED"}), encoding="utf-8")
    # §44–§49 定 B：一次"该判干净"的现场必须有四条内容证据（正文 / CU 落盘 / 装配输入 /
    # 最终件谱系），而不是靠 `volumes/V01.docx` 那份 Resume 锚点在场来放行。
    (section / "fragment.json").write_text(json.dumps(
        {"section_id": "S01-01", "body_blocks": [{"type": "paragraph", "content": "技术实质内容"}]}),
        encoding="utf-8")
    (run / "content_unit_generation_audit.json").write_text(json.dumps(
        {"persisted_content_unit_ids": ["CU-S01-01-01"]}), encoding="utf-8")
    (run / "longform" / "word" / "first_draft_artifact.json").write_text(json.dumps(
        {"schema_version": "first-draft-artifact-v0.1",
         "section_states": {"S01-01": "COMPLETED"}}), encoding="utf-8")
    (run / "artifact_content.json").write_text(json.dumps(
        {"artifact": {"sections": [{"section_id": "S01-01"}]}}), encoding="utf-8")
    (run / "checkpoint" / "state.json").write_text(json.dumps({"schema_version": "longform-checkpoint-v0.3-hierarchical", "status": "COMPLETED", "completed_sections": ["S01-01"], "failed_units": [], "pending_batches": [], "failed_batches": [], "retry_queue": []}), encoding="utf-8")
    (run / "artifact_qa_report.json").write_text(json.dumps({"status": "PASS"}), encoding="utf-8")
    # Round 4.7：终态的 QA 凭据是"对那份 DOCX 量的"那一份，而且必须绑当前产物。
    # 这份夹具的 DOCX 是先写在 Run 根目录、由 `run_layout.finalize_run` 拷进 `artifact/`
    # 的（同名字节相同），所以绑的是根目录那一份真的在场的字节 —— 不是那个还不存在的
    # `artifact/V01.docx`（写法人复用共享夹具，它现在会拒绝绑一份不在场的交付件）。
    _write_final_artifact_qa(run, status="PASS", artifact=run / "V01.docx")
    # Final Gate §4.2：250–320 的**实测**技术有效页是 Clean 的必要条件之一。这份"全门齐"
    # 正向对照要的就是"所有条件都满足时 Clean 仍然可达"，所以它必须自带实测页 ——
    # 不写这两份文件，量到的会是旧口径（页带不参与判定）。
    (run / "document_scale_report.json").write_text(json.dumps(
        {"page_targets": [250, 320],
         "page_report": {"technical_effective_pages": 286, "measured": True}}), encoding="utf-8")
    (run / "artifact_pagination.json").write_text(
        json.dumps({"technical_effective_pages": 286}), encoding="utf-8")
    (run / "run_audit.json").write_text(json.dumps({"status": "generation_completed", "candidate_delivery_status": "COMPLETED_CLEAN"}), encoding="utf-8")
    (run / "generation_status.json").write_text(json.dumps({"status": "generation_completed", "result": {"delivery_status": "COMPLETED_CLEAN"}}), encoding="utf-8")
    (run / "volume_progress.json").write_text(json.dumps({"volumes": [{"volume_id": "V01"}]}), encoding="utf-8")
    roots = SimpleNamespace(runs_dir=runs, output_root=output)
    return run, roots


class RunStateSingleWriterTest(unittest.TestCase):
    def test_normal_finalization_projects_one_clean_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, roots = _authority_fixture(Path(tmp))
            with patch.object(paths, "current", return_value=roots):
                result = finalize_run(run.name, {"medium": "WORD", "delivery_status": "COMPLETED_CLEAN"})
            self.assertEqual(result["delivery_status"], "COMPLETED_CLEAN")
            state = _read(run / "run_state.json")
            self.assertEqual(state["derived"]["delivery_status"], "COMPLETED_CLEAN")
            self.assertTrue(all(value == "PASS" for value in state["invariants"].values()))
            for path in (run / "run_audit.json", run / "checkpoint" / "state.json", run / "generation_status.json"):
                self.assertEqual(_read(path)["delivery_status"], "COMPLETED_CLEAN")
            self.assertEqual(_read(run / "11_volume_generation_plan.json")["authority_delivery_status"], "COMPLETED_CLEAN")
            self.assertEqual(_read(run / "volume_progress.json")["authority_delivery_status"], "COMPLETED_CLEAN")

    def test_reconcile_failure_cannot_be_overwritten_by_clean_qa(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, roots = _authority_fixture(Path(tmp))
            with patch.object(paths, "current", return_value=roots), patch("longform.run_state.reconcile_run_state", side_effect=RuntimeError("injected authority failure")):
                result = finalize_run(run.name, {"medium": "WORD", "delivery_status": "COMPLETED_CLEAN"})
            self.assertEqual(result["delivery_status"], "NEEDS_REVIEW")
            self.assertTrue((run / "run_state_reconcile_failed.json").is_file())
            state = _read(run / "run_state.json")
            self.assertEqual(state["derived"]["delivery_status"], "NEEDS_REVIEW")
            for path in (run / "run_audit.json", run / "checkpoint" / "state.json", run / "generation_status.json"):
                self.assertEqual(_read(path)["delivery_status"], "NEEDS_REVIEW")
            self.assertEqual(_read(run / "11_volume_generation_plan.json")["authority_delivery_status"], "NEEDS_REVIEW")
            self.assertEqual(_read(run / "volume_progress.json")["authority_delivery_status"], "NEEDS_REVIEW")


def _cu_factory(run: Path, provider: MockProvider) -> tuple[LongformGenerationFactory, str, list[str]]:
    brief = {**r21._brief(), "project_type": "产业园综合体", "scenario": "完整物业服务方案"}
    selection = r21._selection()
    pack = r21._complex_pack()
    bundle = build_planning_bundle(pack, brief, selection)
    hierarchy = build_document_hierarchy(pack, bundle)
    sid = next(section["section_id"] for section in hierarchy["sections"] if sum(unit["section_id"] == section["section_id"] for unit in hierarchy["content_units"]) == 3)
    units = [unit for unit in hierarchy["content_units"] if unit["section_id"] == sid]
    contracts = {
        unit["content_unit_id"]: {
            "content_unit_id": unit["content_unit_id"],
            "parent_section_id": unit["section_id"],
            "title": unit.get("title"),
            "intent": unit.get("intent"),
            "must_cover": unit.get("must_cover") or [],
            "required_outputs": unit.get("required_outputs") or [],
            "target_budget": unit.get("target_budget"),
            "scoring_refs": unit.get("scoring_refs") or [],
            "dependency_refs": unit.get("dependency_refs") or [],
        }
        for unit in units
    }
    selected_contract = next(row for row in bundle["section_contracts"]["contracts"] if row["section_id"] == sid)
    inputs = {
        **bundle,
        "section_contracts": {**bundle["section_contracts"], "contracts": [copy.deepcopy(selected_contract)]},
        "knowledge_selection": selection,
        "cu_contracts": contracts,
        "require_section_min": False,
        "speed_profile": "fast",
    }
    return LongformGenerationFactory(run_root=run, provider=provider, provider_name="mock", inputs=inputs), sid, list(contracts)


class ContentUnitGenerationTest(unittest.TestCase):
    def test_one_section_three_cus_are_provider_generated_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = MockProvider(MOCK_CONFIG)
            factory, sid, cu_ids = _cu_factory(Path(tmp), provider)
            result = factory.generate_word([sid])
            audit = result["content_unit_generation"]
            self.assertEqual({key: audit[key] for key in ("planned", "sent", "provider_received", "returned", "completed", "legacy_fallback")}, {"planned": 3, "sent": 3, "provider_received": 3, "returned": 3, "completed": 3, "legacy_fallback": 0})
            self.assertEqual(set(provider.received_requests[0]["content_unit_ids"]), set(cu_ids))
            required_contract_fields = {
                "content_unit_id", "parent_section_id", "title", "intent",
                "must_cover", "required_outputs", "target_budget", "scoring_refs",
                "dependency_refs",
            }
            for contract in provider.received_requests[0]["content_unit_contracts"]:
                self.assertTrue(required_contract_fields <= set(contract))
            for cu_id in cu_ids:
                row = _read(Path(tmp) / "longform" / "word" / "sections" / sid / "cus" / f"{cu_id}.json")
                self.assertEqual(row["generation_origin"], "provider_content_unit")
                self.assertFalse(row["covered_by_section_assembly"])

    def test_partial_cu_resume_requests_only_missing_cu(self):
        class PartialThenDrop(MockProvider):
            def __init__(self):
                super().__init__(MOCK_CONFIG)
                self.calls = 0

            def invoke_structured(self, request, task_dir):
                self.calls += 1
                if self.calls == 1:
                    output = super().invoke_structured(request, task_dir)
                    output["content_unit_fragments"] = output["content_unit_fragments"][:2]
                    return output
                raise ConnectionError("stop after two CU fragments")

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            first = PartialThenDrop()
            factory, sid, cu_ids = _cu_factory(run, first)
            factory.generate_word([sid])
            persisted = sorted(path.stem for path in (run / "longform" / "word" / "sections" / sid / "cus").glob("CU-*.json"))
            self.assertEqual(persisted, cu_ids[:2])
            resume = MockProvider(MOCK_CONFIG)
            resumed_factory, _, _ = _cu_factory(run, resume)
            result = resumed_factory.generate_word([sid])
            resume_payload = [cu for row in resume.received_requests for cu in row["content_unit_ids"]]
            self.assertEqual(resume_payload, [cu_ids[2]])
            audit = result["content_unit_generation"]
            self.assertEqual(audit["completed"], 3)
            self.assertEqual(audit["legacy_fallback"], 0)


if __name__ == "__main__":
    unittest.main()
