from __future__ import annotations

import json
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
DIST_ROOT = APP_ROOT / "dist" / "MMF-006E-portable-v0.1"
# Replay with the pre-R2.4 production gate that actually judged the historical
# runs.  The report is generated before the rebuilt dist replaces this code.
APP = DIST_ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from governance.artifact_qa import apply_artifact_repairs
from governance.commitment_provenance import apply_local_repairs
from longform.factory import _repair_customer_text, content_units, evaluate_word_fragment, visible_text


RUNS = {
    "Kimi": "tender_20260909_093443_793308",
    "Qwen": "tender_20260909_113429_4dfe43",
    "Zhipu": "tender_20260909_125744_bd82a3",
}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def attempt_fragment(run: Path, section_id: str, attempt: dict) -> dict | None:
    attempt_id = int(attempt.get("attempt_id") or 0)
    if attempt.get("mode") == "generate_batch":
        folder = run / "longform" / "word" / "batches" / str(attempt.get("batch_id")) / f"provider_attempt_{attempt_id}"
    else:
        folder = run / "longform" / "word" / "sections" / section_id / f"provider_attempt_{attempt_id}"
    path = folder / "provider_structured_output.json"
    if not path.is_file():
        return None
    payload = read_json(path)
    rows = payload.get("sections") if isinstance(payload, dict) else None
    if isinstance(rows, list):
        return next((row for row in rows if isinstance(row, dict) and row.get("section_id") == section_id), None)
    return payload if isinstance(payload, dict) else None


def qa_reasons(gate: dict) -> tuple[list[str], list[str]]:
    missing_requirements = [key for key, value in (gate.get("must_cover") or {}).items() if value == "MISSING"]
    reasons: list[str] = []
    if gate.get("missing_keys"):
        reasons.append("structured_fields_missing")
    if "meaningful_body" in gate and not gate.get("meaningful_body"):
        reasons.append("body_missing_or_empty")
    if gate.get("empty_headings"):
        reasons.append("empty_heading")
    if gate.get("length_status") == "SECTION_UNDER_LENGTH":
        reasons.append("under_length")
    if missing_requirements:
        reasons.append("must_cover_missing")
    if gate.get("required_outputs_missing"):
        reasons.append("required_outputs_missing")
    if gate.get("required_tables_missing"):
        reasons.append("required_table_missing")
    if gate.get("process_missing"):
        reasons.append("required_process_missing")
    if gate.get("fact_drift"):
        reasons.append("fact_drift")
    if gate.get("role_drift"):
        reasons.append("role_semantic_drift")
    if gate.get("commitment", {}).get("status") == "BLOCK":
        reasons.append("commitment_block")
    if gate.get("artifact_qa", {}).get("status") == "BLOCK":
        reasons.append("artifact_block")
    return missing_requirements, reasons


def main() -> int:
    dist = DIST_ROOT
    rows: list[dict] = []
    runs: list[dict] = []
    for provider, run_id in RUNS.items():
        run = dist / "runs" / run_id
        contracts_payload = read_json(run / "04_section_contracts.json")
        contracts = {row["section_id"]: row for row in contracts_payload.get("contracts") or []}
        processes = {row["process_id"]: row for row in contracts_payload.get("process_contracts") or []}
        global_state = read_json(run / "05_document_global_state_v0.json")
        artifact = next(run.glob("*.docx"), None)
        generation_status = read_json(run / "generation_status.json")
        provider_name = read_json(run / "brief.json").get("provider_name")
        affected = 0
        for unit in sorted((run / "longform" / "word" / "sections").iterdir()):
            audit_path = unit / "audit.json"
            if not unit.is_dir() or not audit_path.is_file():
                continue
            attempts = read_json(audit_path).get("attempts") or []
            repair_modes = [str(row.get("mode")) for row in attempts[1:]]
            if len(attempts) <= 1:
                continue
            affected += 1
            sid = unit.name
            contract = contracts[sid]
            original = attempt_fragment(run, sid, attempts[0])
            if original is None:
                gate = {}
                original_units = 0
                missing, reasons = [], ["batch_split_missing_section"]
            else:
                process_rows = [processes[pid] for pid in contract.get("required_processes") or [] if pid in processes]
                judged = _repair_customer_text(apply_artifact_repairs(apply_local_repairs(original)))
                gate = evaluate_word_fragment(judged, contract, global_state, process_rows)
                original_units = content_units(visible_text({
                    "body_blocks": original.get("body_blocks") or [],
                    "tables": original.get("tables") or [],
                    "processes": original.get("processes") or [],
                    "callouts": original.get("callouts") or [],
                }))
                missing, reasons = qa_reasons(gate)
            final_fragment = read_json(unit / "fragment.json") if (unit / "fragment.json").is_file() else {}
            final_status = read_json(unit / "status.json") if (unit / "status.json").is_file() else {}
            repaired_units = content_units(visible_text({
                "body_blocks": final_fragment.get("body_blocks") or [],
                "tables": final_fragment.get("tables") or [],
                "processes": final_fragment.get("processes") or [],
                "callouts": final_fragment.get("callouts") or [],
            }))
            true_defect = any(reason in reasons for reason in {
                "body_missing_or_empty", "empty_heading", "must_cover_missing", "required_outputs_missing",
                "required_table_missing", "required_process_missing", "structured_fields_missing", "batch_split_missing_section",
                "fact_drift", "commitment_block", "artifact_block",
            })
            rows.append({
                "section_id": sid,
                "provider": provider,
                "provider_name": provider_name,
                "original_chars": original_units,
                "required_min": int((contract.get("target_words") or {}).get("min") or 0),
                "missing_requirements": missing,
                "qa_reason": reasons,
                "repair_action": repair_modes,
                "repaired_chars": repaired_units,
                "final_status": final_status.get("status"),
                "diagnosis": "REAL_DEFECT" if true_defect else "LEGACY_QA_FALSE_POSITIVE",
            })
        runs.append({
            "provider": provider,
            "provider_name": provider_name,
            "run_id": run_id,
            "generation_status": generation_status.get("status"),
            "artifact": str(artifact) if artifact else None,
            "integrity_failure_sections": affected,
        })
    output = {
        "schema_version": "mmf006e-r2.4-real-run-diagnosis-v0.1",
        "test_target": "existing_production_dist_run_replay",
        "real_runs_rerun": False,
        "runs": runs,
        "summary": {
            "provider_runs": len(runs),
            "integrity_failure_section_events": len(rows),
            "real_defect_events": sum(row["diagnosis"] == "REAL_DEFECT" for row in rows),
            "legacy_qa_false_positive_events": sum(row["diagnosis"] == "LEGACY_QA_FALSE_POSITIVE" for row in rows),
        },
        "diagnostic_table": rows,
    }
    target = APP_ROOT / "runtime" / "mmf006e_r24_control" / "real_run_integrity_diagnosis.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output["summary"], ensure_ascii=False))
    for row in rows:
        if row["section_id"] in {"S05-03", "S10-01"}:
            print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
