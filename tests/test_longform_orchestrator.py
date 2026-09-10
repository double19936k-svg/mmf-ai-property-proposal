from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from governance.longform_qa import evaluate_longform_depth
from longform.orchestrator import generate_longform
from planning.canonical import resolve_task_mode
from planning.planner import build_planning_bundle
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


class LongformOrchestratorTest(unittest.TestCase):
    def test_full_proposal_defaults_to_full_longform(self):
        self.assertEqual(resolve_task_mode({"scenario": "完整物业服务方案"}), "full_longform")

    def test_planner_is_provider_independent(self):
        pack, brief = _pack(), _brief()
        left = build_planning_bundle(pack, brief, _selection(), production=True)
        right = build_planning_bundle(pack, brief, {**_selection(), "knowledge_usage_contracts": []}, production=True)
        self.assertEqual(left["validation"]["status"], "PASS")
        self.assertEqual([c["chapter_title"] for c in left["word_plan"]["outline"]], [c["chapter_title"] for c in right["word_plan"]["outline"]])
        self.assertEqual(len(left["section_contracts"]["contracts"]), len(right["section_contracts"]["contracts"]))
        self.assertEqual(left["content_budget"]["totals"]["target_words_min"], right["content_budget"]["totals"]["target_words_min"])

    def test_offline_factory_writes_plan_sections_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(Path(tmp), _pack(), _brief(), _selection())
            result = generate_longform(
                run_dir=run,
                provider=_provider(),
                provider_name="mock",
                brief=_brief(),
                selection=_selection(),
                selected_ids=["KU-0001"],
                section_ids=["S01-01", "S05-02", "S06-02"],
                task_mode="standard",
            )
            for name in ("01_word_document_plan.json", "02_requirement_section_matrix.json", "03_word_content_budget.json", "04_section_contracts.json", "canonical_tender_analysis.json", "canonical_requirement_map.json", "canonical_project_brief.json", "checkpoint/state.json"):
                self.assertTrue((run / name).is_file(), name)
            self.assertFalse(result["generated"]["longform"]["ONE_SHOT_FULL_DOCUMENT_GENERATION"])
            self.assertTrue(result["generated"]["longform"]["SECTION_LEVEL_GENERATION"])
            self.assertEqual(result["word"]["completed_sections"], 3)
            self.assertTrue((run / "longform" / "word" / "sections" / "S01-01" / "fragment.json").is_file())
            self.assertTrue((run / "longform" / "word" / "sections" / "S01-01" / "generation.json").is_file())
            self.assertTrue((run / "longform" / "word" / "sections" / "S01-01" / "qa.json").is_file())
            first_calls = len(list((run / "longform" / "word" / "sections" / "S01-01").glob("provider_attempt_*")))
            generate_longform(run_dir=run, provider=_provider("mock2"), provider_name="mock2", brief=_brief(), selection=_selection(), selected_ids=["KU-0001"], section_ids=["S01-01", "S05-02", "S06-02"], task_mode="standard")
            second_calls = len(list((run / "longform" / "word" / "sections" / "S01-01").glob("provider_attempt_*")))
            self.assertEqual(first_calls, second_calls)

    def test_short_section_triggers_continuation(self):
        class ShortThenLong:
            def __init__(self):
                self.calls = 0
                self.inner = _provider()
                self.config = self.inner.config

            def get_metadata(self):
                return self.inner.get_metadata()

            def invoke_structured(self, request, task_dir):
                self.calls += 1
                payload = dict(request)
                if self.calls == 1:
                    payload["mock_short"] = True
                return self.inner.invoke_structured(payload, task_dir)

        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(Path(tmp), _pack(), _brief(), _selection())
            provider = ShortThenLong()
            generate_longform(run_dir=run, provider=provider, provider_name="mock", brief=_brief(), selection=_selection(), selected_ids=["KU-0001"], section_ids=["S01-01"], task_mode="standard")
            generation = json.loads((run / "longform" / "word" / "sections" / "S01-01" / "generation.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(provider.calls, 2)
            self.assertTrue(generation.get("continuation") or generation.get("retry"))

    def test_depth_gate_rejects_tiny_document(self):
        report = evaluate_longform_depth(
            task_mode="full_longform",
            word_plan={"outline": [{"sections": [{"section_id": "S01-01"}]}]},
            contracts={"contracts": [{"section_id": "S01-01"}]},
            matrix={"matrix": []},
            fragments={"S01-01": {"title": "x", "body_blocks": [{"type": "paragraph", "content": "短"}]}},
            gates=[{"length_status": "SECTION_UNDER_LENGTH"}],
            total_effective_chars=2503,
        )
        self.assertEqual(report["status"], "BLOCK")
        self.assertEqual(report["LONGFORM_DEPTH_GATE"], "BLOCK")

    def test_mini_parity_same_plan_different_bodies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack, brief, selection = _pack(), _brief(), _selection()
            run_a = _write_run(root / "a", pack, brief, selection)
            result_a = generate_longform(run_dir=run_a, provider=_provider("mock_a"), provider_name="mock_a", brief=brief, selection=selection, selected_ids=["KU-0001"], section_ids=["S01-01", "S05-02"], task_mode="standard")
            run_b = _write_run(root / "b", pack, brief, selection)
            for name in (
                "01_word_document_plan.json", "02_requirement_section_matrix.json", "03_word_content_budget.json",
                "04_section_contracts.json", "05_document_global_state_v0.json", "06_ppt_presentation_plan.json",
                "07_cross_section_dependency.json", "canonical_tender_analysis.json", "canonical_requirement_map.json",
                "canonical_project_brief.json",
            ):
                (run_b / name).write_bytes((run_a / name).read_bytes())
            result_b = generate_longform(run_dir=run_b, provider=_provider("mock_b"), provider_name="mock_b", brief=brief, selection=selection, selected_ids=["KU-0001"], section_ids=["S01-01", "S05-02"], task_mode="standard")
            plan_a = json.loads((run_a / "01_word_document_plan.json").read_text(encoding="utf-8"))
            plan_b = json.loads((run_b / "01_word_document_plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan_a["outline"], plan_b["outline"])
            self.assertEqual(len(result_a["generated"]["artifact"]["sections"]), len(result_b["generated"]["artifact"]["sections"]))
            self.assertEqual(result_a["capability"]["effective_settings"]["continuation"], result_b["capability"]["effective_settings"]["continuation"])

    def test_bid_process_text_is_not_dumped_into_must_cover(self):
        from planning.planner import is_bid_process_text
        self.assertTrue(is_bid_process_text("投标单位应认真阅读招标文件中所有的事项、格式、条款和技术规范等"))
        self.assertTrue(is_bid_process_text("外装信封上应清楚注明招标编号且密封处加盖公章"))
        self.assertFalse(is_bid_process_text("产业园生产经营连续性对物业服务的影响"))
        pack = _pack()
        pack["requirements"].append({
            "requirement_id": "REQ-BID-1",
            "normalized_requirement": "投标单位应认真阅读招标文件中所有的事项、格式、条款和技术规范等",
            "domain": "other",
            "mandatory_level": "MUST",
            "confirmation_status": "CONFIRMED",
        })
        bundle = build_planning_bundle(pack, _brief(), _selection(), production=True)
        s01 = next(row for row in bundle["section_contracts"]["contracts"] if row["section_id"] == "S01-02")
        self.assertFalse(any("招标文件" in str(item) for item in s01["must_cover"]))
        self.assertFalse(any(row["requirement_id"] == "REQ-BID-1" for row in bundle["requirement_matrix"]["matrix"]))

    def test_nested_bullet_group_is_not_json_debris(self):
        from governance.artifact_qa import apply_artifact_repairs, evaluate_artifact
        from longform.orchestrator import fragment_to_section
        fragment = {
            "section_id": "S01-01",
            "title": "项目概况、服务范围与排除项",
            "body_blocks": [
                {"type": "", "content": "本方案适用于示例滨海科技园。"},
                {"type": "bullet_group", "content": [
                    {"title": "项目事实清单", "items": ["项目名称：示例滨海科技园物业顾问服务项目", "日常管理顾问内容涵盖：保洁服务管理、绿化服务管理、客户服务管理"]},
                ]},
            ],
        }
        section = fragment_to_section(fragment, "项目概况")
        blob = "\n".join(section["paragraphs"] + section["bullets"])
        self.assertIn("项目事实清单", blob)
        self.assertIn("保洁服务管理", blob)
        self.assertNotIn("{'title'", blob)
        artifact = apply_artifact_repairs({"artifact": {"sections": [section]}})
        qa = evaluate_artifact(artifact)
        self.assertFalse(any(item.get("issue") == "JSON debris" for item in qa.get("findings") or []))

    def test_repair_flattens_python_list_debris(self):
        from governance.artifact_qa import apply_artifact_repairs, evaluate_artifact
        debris = "[{'title': '待澄清事项', 'items': ['项目封闭/开放管理模式尚未确认', '人员配置规模未确认']}]"
        generated = {"artifact": {"sections": [{"heading": "概况", "paragraphs": [debris], "bullets": []}]}}
        repaired = apply_artifact_repairs(generated)
        text = "\n".join(repaired["artifact"]["sections"][0]["paragraphs"])
        self.assertIn("待澄清事项", text)
        self.assertNotIn("{'title'", text)
        qa = evaluate_artifact(repaired)
        self.assertFalse(any(item.get("issue") == "JSON debris" for item in qa.get("findings") or []))


if __name__ == "__main__":
    raise SystemExit(unittest.main())
