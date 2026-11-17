import json
import inspect
import sys
from pathlib import Path

# 这个文件只用裸 `longform.*` 导入，以前靠字母序靠前的兄弟模块顺手把 app/ 塞进 sys.path
# 才能收集；单独跑它就是 ModuleNotFoundError。把路径自己钉住，去掉对执行顺序的依赖。
APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from longform.batch_planner import content_unit_batches, plan_generation_batches
from longform.cu_audit import rebuild_content_unit_generation_audit
from longform.factory import LongformGenerationFactory
from longform.orchestrator import fragment_to_section


def _cu(cid, chars=1800):
    return {"content_unit_id": cid, "target_budget": {"effective_chars": chars}, "max_budget": {"effective_chars": chars}}


def test_capacity_01_over_capacity_section_splits_by_cu():
    rows = [_cu(f"CU-S06-02-{i:02d}", 1800) for i in range(1, 12)]
    groups = content_unit_batches("S06-02", rows, safe_budget=5898)
    assert len(groups) >= 3
    assert all(g["estimated_output_tokens"] <= 5898 for g in groups)
    assert sum(len(g["content_unit_ids"]) for g in groups) == 11


def test_capacity_02_planner_records_limit_and_split_reason():
    rows = [_cu(f"CU-S06-01-{i:02d}", 1800) for i in range(1, 12)]
    plan = plan_generation_batches([{"section_id": "S06-01", "generation_order": 1, "target_words": {"max": 11000}, "content_unit_contracts": rows}], {"dependencies": []}, provider_name="glm_zhipu", speed_profile="fast", provider_config={"max_tokens": 8192})
    assert len(plan["batches"]) >= 3
    assert all(b["estimated_output_tokens"] <= b["safe_output_budget"] for b in plan["batches"])
    assert any(b["split_reason"] == "CAPACITY_AWARE_CU_SPLIT" for b in plan["batches"])
    assert all("content_unit_ids" in b and "provider_output_limit" in b for b in plan["batches"])


def test_capacity_03_partial_cu_batch_defers_section_depth_qa():
    source = inspect.getsource(LongformGenerationFactory._run_batch)
    assert source.index("remaining_after_batch") < source.index("_assemble_section_from_cus")
    assert "partial_generation.json" in source


def test_capacity_04_section_assembly_is_the_depth_qa_boundary():
    source = inspect.getsource(LongformGenerationFactory._run_batch)
    assert source.index("_assemble_section_from_cus") < source.index("evaluate_word_fragment")


def test_type_01_numeric_process_steps_does_not_crash():
    out = fragment_to_section({"title": "工程", "body_blocks": [], "processes": [{"title": "流程", "steps": 7}]}, "S06-01")
    assert out["heading"] == "工程"
    assert out["bullets"] == []


def test_type_02_normal_structured_lists_preserved():
    out = fragment_to_section({"title": "工程", "body_blocks": [], "processes": [{"title": "流程", "steps": ["登记", "复核"]}], "tables": [{"columns": ["事项"], "rows": [["执行"]]}]}, "S06-01")
    assert out["bullets"] == ["登记", "复核"]
    assert out["tables"][0]["rows"] == [["执行"]]


def test_type_03_count_metadata_with_items_is_supported():
    out = fragment_to_section({"title": "清单", "body_blocks": [{"type": "bullet_group", "items": ["一", "二"], "count": 2}]}, "S01-01")
    assert out["bullets"] == ["一", "二"]


def test_type_04_missing_structures_do_not_crash():
    assert fragment_to_section({"title": "空", "body_blocks": None, "tables": None, "processes": None}, "S01-01")["heading"] == "空"


def test_audit_01_rebuilds_hierarchy_count_from_replay():
    root = Path(__file__).resolve().parents[1] / "runtime" / "mmf007b_real_run_replay_20260915"
    if not (root / "08_document_hierarchy.json").is_file():
        return
    audit = rebuild_content_unit_generation_audit(root)
    assert audit["planned"] == 301
    assert audit["completed"] <= audit["planned"]


def test_audit_02_unique_counts_are_not_attempt_counts():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); (root / "longform/word/sections/S01-01/cus").mkdir(parents=True)
        (root / "08_document_hierarchy.json").write_text(json.dumps({"content_units": [_cu("CU-S01-01-01")] }), encoding="utf-8")
        (root / "provider_unknown_requests.jsonl").write_text("\n".join(json.dumps({"status": s, "content_unit_ids": ["CU-S01-01-01"]}) for s in ["REQUEST_SENT", "RESPONSE_RECEIVED", "RESOLVED"]) + "\n", encoding="utf-8")
        (root / "longform/word/sections/S01-01/cus/CU-S01-01-01.json").write_text(json.dumps({"content_unit_id": "CU-S01-01-01", "generation_origin": "provider_content_unit"}), encoding="utf-8")
        audit = rebuild_content_unit_generation_audit(root)
        assert audit["unique_sent_cu"] == 1
        assert audit["request_attempts"] == 1


def test_unknown_01_successful_response_closes_request_ledger(tmp_path):
    factory = LongformGenerationFactory.__new__(LongformGenerationFactory)
    ledger = tmp_path / "provider_unknown_requests.jsonl"
    marker = {"request_id": "REQ-1", "request_fingerprint": "fp-1", "content_unit_ids": ["CU-1"]}
    factory._append_request_event(ledger, marker, "REQUEST_SENT")
    factory._append_request_event(ledger, marker, "RESPONSE_RECEIVED", provider_received_content_unit_ids=["CU-1"])
    factory._append_request_event(ledger, marker, "RESOLVED", resolution="structured_response")
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [row["status"] for row in rows] == ["REQUEST_SENT", "RESPONSE_RECEIVED", "RESOLVED"]
    assert rows[-1]["resolution"] == "structured_response"


def test_log_01_generation_diagnostics_include_error_id_and_recoverable(monkeypatch, tmp_path):
    import app_core

    monkeypatch.setattr(app_core, "_checked", lambda path: Path(path))
    run_dir = tmp_path / "run"
    error_id = app_core._write_generation_error_diagnostics(
        run_dir, "run-1", ValueError("synthetic failure"), phase="generation", provider="mock", recoverable=True
    )
    trace = json.loads((run_dir / "error_trace.json").read_text(encoding="utf-8"))
    log_text = (run_dir / "log" / "error.log").read_text(encoding="utf-8")
    assert error_id.startswith("ERR-")
    assert trace["error_id"] == error_id
    assert trace["recoverable"] is True
    assert error_id in log_text
