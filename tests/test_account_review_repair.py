import pytest
from docx import Document

import app_core
from governance.final_artifact_qa import evaluate_docx_artifact
from longform import pagination
from planning import scoring_final_evidence


@pytest.mark.parametrize(("refresh_status", "before_coverage", "after_coverage", "expected"), [
    ("REFRESHED", 0.75, 0.75, "APPLIED"),
    ("RENDER_FAILED", 0.75, 0.75, "REJECTED"),
    ("REFRESHED", 0.75, 0.50, "REJECTED"),
])
def test_review_repair_checks_copy_pagination_and_scoring(
        tmp_path, monkeypatch, refresh_status, before_coverage, after_coverage, expected):
    final = tmp_path / "draft.docx"
    doc = Document()
    doc.add_paragraph("执行 S01-02（服务巡视）流程。")
    doc.add_paragraph("根据现场确认的工作范围实施巡视。")
    doc.save(final)
    original = final.read_bytes()

    def score(coverage):
        return {"status": "VERIFIED", "fulfillment": {"technical_scoring_coverage": coverage}}

    def evaluate(path):
        qa = evaluate_docx_artifact(path)
        # Real repair and real DOCX parsing. Keep a separate unresolved fact in
        # both readings to verify that the repair targets only the format.
        texts = [p.text for p in Document(str(path)).paragraphs]
        qa["findings"] = [{"rule_id": "FACT", "severity": "NEEDS_REVIEW", "issue": "人员配置待确认"}]
        if "S01-02" in texts[0]:
            qa["findings"].append({"rule_id": "LABEL", "severity": "NEEDS_REVIEW", "issue": "内部编号"})
        qa["status"] = "NEEDS_REVIEW"
        return qa

    def preview_score(run_dir, candidate, *, expected_artifact_sha256, persist):
        assert run_dir == tmp_path and candidate != final
        assert final.read_bytes() == original and persist is False
        return score(after_coverage)

    monkeypatch.setattr(scoring_final_evidence, "refresh_final_scoring", preview_score)
    monkeypatch.setattr(pagination, "refresh_with_retry",
                        lambda path, **kwargs: {"status": refresh_status, "artifact_path": str(path)})
    baseline = {**evaluate(final), "final_scoring": score(before_coverage)}
    previous_pages = {"artifact_path": str(final), "status": "REFRESHED"}
    report, pages = app_core._attempt_account_review_repair(final, tmp_path, baseline, evaluate, previous_pages)
    assert report["status"] == expected and report["provider_calls"] == 0
    if expected == "APPLIED":
        assert final.read_bytes() != original
        assert [p.text for p in Document(str(final)).paragraphs] == [
            "执行 服务巡视流程。", "根据现场确认的工作范围实施巡视。"]
        assert [row["rule_id"] for row in evaluate(final)["findings"]] == ["FACT"]
        assert pages["artifact_path"] == str(final)
    else:
        assert final.read_bytes() == original and pages is previous_pages
    assert list(tmp_path.iterdir()) == [final]


def test_review_repair_without_local_changes_keeps_original_bytes(tmp_path, monkeypatch):
    final = tmp_path / "draft.docx"
    doc = Document()
    doc.add_paragraph("根据现场确认的工作范围实施巡视。")
    doc.save(final)
    original = final.read_bytes()

    def never_called(*args, **kwargs):
        pytest.fail("No pagination or scoring should run when the repair made no changes")

    monkeypatch.setattr(pagination, "refresh_with_retry", never_called)
    report, _ = app_core._attempt_account_review_repair(final, tmp_path, {}, never_called, {})
    assert report["status"] == "NO_CHANGE" and final.read_bytes() == original
