"""Delivery copying must remeasure final scoring against the landed carrier."""
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import paths
from run_layout import finalize_run
from planning.scoring_final_evidence import refresh_final_scoring, final_scoring_binding_matches
from longform.run_state import _scoring_gate_facts
from test_run_k_scoring_integration import setup_run, write_json

def test_finalization_remeasures_landed_path_and_preserves_source(tmp_path):
    root=tmp_path/"runs"; root.mkdir()
    run=root/"r05_score";run.mkdir()
    docx,matrix,_=setup_run(run)
    original=docx.read_bytes()
    initial=refresh_final_scoring(run,docx)
    assert initial["status"]=="VERIFIED"
    write_json(run/"final_artifact_qa.json",{"status":"PASS","sha256":initial["artifact_sha256"],
                "artifact_path":str(docx),"actual_docx_inspected":True,"findings":[]})
    roots=replace(paths.current(),runs_dir=root,output_root=tmp_path/"output")
    with patch.object(paths,"current",return_value=roots):
        finalize_run(run.name,{"status":"generation_completed","medium":"WORD"})
    landed=run/"artifact"/docx.name
    measured=json.loads(matrix.read_text(encoding="utf-8"))
    assert landed.read_bytes()==original==docx.read_bytes()
    assert final_scoring_binding_matches(measured,landed)
    assert not final_scoring_binding_matches(measured,docx)
    assert _scoring_gate_facts(run)["scoring_artifact_binding_state"]=="BOUND"

def test_same_name_wrong_bytes_cannot_inherit_final_scoring(tmp_path):
    run=tmp_path/"runs"/"r05_wrong";run.mkdir(parents=True)
    docx,matrix,_=setup_run(run)
    reading=refresh_final_scoring(run,docx)
    write_json(run/"final_artifact_qa.json",{"status":"PASS","sha256":reading["artifact_sha256"],
               "artifact_path":str(docx),"actual_docx_inspected":True,"findings":[]})
    (run/"artifact").mkdir()
    from docx import Document
    damaged=Document();damaged.add_paragraph("未回答原评分要求");damaged.save(run/"artifact"/docx.name)
    roots=replace(paths.current(),runs_dir=run.parent,output_root=tmp_path/"output")
    with patch.object(paths,"current",return_value=roots):
        finalize_run(run.name,{"status":"generation_completed","medium":"WORD"})
    report=json.loads((run/"final_scoring_report.json").read_text(encoding="utf-8"))
    assert report["status"]=="FAILED"
    assert report["reason_code"]=="FINAL_SCORING_ARTIFACT_IDENTITY_MISMATCH"
    assert _scoring_gate_facts(run)["scoring_artifact_binding_state"]=="FAILED_OR_STALE"

