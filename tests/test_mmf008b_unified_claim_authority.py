"""MMF-008B: claim scope and source binding must share one decision record."""
from __future__ import annotations

import pytest

from governance.commitment_provenance import (
    apply_local_repairs, canonical_claim_record, evaluate_commitments,
)
from governance.final_artifact_qa import evaluate_docx_artifact
from docx import Document


def _evaluate(text: str, brief: dict | None = None) -> dict:
    return evaluate_commitments(brief or {}, [], {"artifact": {"body": text}})


@pytest.mark.parametrize("text, expected", [
    ("前提是否成立仍待确认，屋面设备间已配备漏水报警系统。", "BLOCK"),
    ("前提是否成立尚待核验且地下机房已安装自动灭火装置。", "BLOCK"),
    ("对该假设是否合理尚待核验且地下机房已安装自动灭火装置。", "BLOCK"),
    ("假设的合理性尚待核验，冷冻机房已安装余热回收装置。", "BLOCK"),
    ("假设 A，则拟 B，此外地下机房已安装自动灭火装置。", "BLOCK"),
    ("若甲方批准方案，则拟调整巡查班次且地下机房已安装自动灭火装置。", "BLOCK"),
    ("假设夜间作业追加，则拟调整班次。冷冻机房已安装余热回收装置。", "BLOCK"),
    ("屋面设备间已配备漏水报警系统，假设甲方调整巡检时段，则另行安排班次。", "BLOCK"),
    ("前提是否成立尚待核验且屋面设备间已配备漏水报警系统。", "BLOCK"),
    ("假设合理性尚待讨论，冷冻机房已安装余热回收装置。", "BLOCK"),
    ("若甲方移交竣工图，则拟编排巡检路线，另有屋面设备间已配备漏水报警系统。", "BLOCK"),
    ("前提是甲方批准方案，则拟调整班次；配电间已安装自动灭火装置。", "BLOCK"),
    ("假设甲方移交图纸，则拟安排检查班次。", "PASS"),
    ("前提是甲方已移交图纸，方可核定抢修点位。", "PASS"),
    ("若甲方确认设备现状，则拟纳入巡检计划。", "PASS"),
])
def test_claim_scope_attack(text: str, expected: str) -> None:
    result = _evaluate(text)
    assert result["status"] == expected
    if expected == "BLOCK":
        assert any(row["canonical_claim_record"]["modality"] == "ASSERTED"
                   for row in result["violations"])


@pytest.mark.parametrize("brief, expected_binding", [
    ("投标人须配备办公用品，防爆照明装置的现状尚待现场核实。", "UNBOUND"),
    ("投标人须完成年度巡查，防爆照明装置是否安装尚待甲方确认。", "UNBOUND"),
    ("投标人须配备办公用品并核查防爆照明装置。", "UNBOUND"),
    ("投标人无需配备防爆照明装置。", "UNBOUND"),
    ("投标人须配备防爆照明装置并留存运行记录。", "VERIFIED"),
    ("投标人应当配备防爆照明装置。", "VERIFIED"),
])
def test_obligation_requires_predicate_object_binding(brief: str, expected_binding: str) -> None:
    text = "地下车库已配备防爆照明装置。"
    result = _evaluate(text, {"duty": brief})
    claim = next(row["canonical_claim_record"] for row in result["claims"]
                 if row["claim_type"] == "UNSUPPORTED_EXISTING_STATE_CLAIM")
    assert claim["obligation_binding"]["status"] == expected_binding
    repaired = apply_local_repairs({"artifact": {"body": text}}, {"duty": brief})["artifact"]["body"]
    if expected_binding == "UNBOUND":
        assert "将配备" not in repaired
    else:
        assert "将配备防爆照明装置" in repaired
        assert claim["obligation_binding"]["source_span"]["path"] == "brief.duty"


def test_no_brief_hold_and_canonical_record_fields() -> None:
    text = "地下车库已配备防爆照明装置。"
    repaired = apply_local_repairs({"artifact": {"body": text}})["artifact"]["body"]
    assert repaired == text
    claim = next(row["canonical_claim_record"] for row in _evaluate(text)["claims"]
                 if row["claim_type"] == "UNSUPPORTED_EXISTING_STATE_CLAIM")
    assert claim["decision"]["gate"] == "BLOCK"
    assert claim["decision"]["repair"] == "HOLD_BLOCK_WITHOUT_SOURCE_EVIDENCE"
    assert claim["claim_span"] and claim["subject_span"] and claim["predicate_span"]
    assert claim["object_span"] and claim["clause_span"]


def test_supported_existing_fact_keeps_source_span() -> None:
    text = "地下车库已配备防爆照明装置。"
    source = {"fact": text}
    result = _evaluate(text, source)
    assert result["status"] == "PASS"
    claim = next(row["canonical_claim_record"] for row in result["claims"]
                 if row["claim_type"] == "UNSUPPORTED_EXISTING_STATE_CLAIM")
    assert claim["source_support_span"]["path"] == "brief.fact"
    assert claim["decision"]["repair"] == "KEEP_SOURCE_SUPPORTED_FACT"
    assert apply_local_repairs({"artifact": {"body": text}}, source)["artifact"]["body"] == text


def test_unsupported_fact_reaches_final_docx_qa(tmp_path) -> None:
    path = tmp_path / "final.docx"
    document = Document()
    document.add_paragraph("地下车库已配备防爆照明装置。")
    document.save(path)
    report = evaluate_docx_artifact(path)
    findings = [item for item in report["findings"]
                if item.get("rule_id") == "UNSUPPORTED_EXISTING_STATE_CLAIM"]
    assert findings and findings[0]["severity"] == "BLOCK"
    record = findings[0]["canonical_claim_record"]
    assert record["modality"] == "ASSERTED" and record["decision"]["gate"] == "BLOCK"
