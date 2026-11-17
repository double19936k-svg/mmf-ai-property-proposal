"""Projection preserves operational leaves and immutable source receipts."""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"app"))
from governance.artifact_qa import apply_artifact_repairs
from governance.commitment_provenance import apply_local_repairs
from governance.table_fidelity import source_contract, digest
from longform.factory import _repair_customer_text


def test_table_receipt_is_not_a_customer_percentage_commitment():
    from longform.factory import public_strings, _governance_artifact
    from governance import evaluate_commitments
    receipt = {'dropped_tables': [{'reason': '低有效内容，表格有效单元格 0% < 25%。'}]}
    fragment = {'body_blocks': [{'type': 'paragraph', 'content': '按适用范围登记核查结果。'}],
                'table_governance': receipt, 'cu_structures_stripped': {'tables': 1}}
    assert public_strings(fragment) == ['按适用范围登记核查结果。']
    assert evaluate_commitments({}, [], _governance_artifact(fragment))['status'] != 'BLOCK'
    assert fragment['table_governance'] == receipt


def test_excluding_backend_receipt_does_not_hide_customer_claim():
    from longform.factory import _governance_artifact
    from governance import evaluate_commitments
    fragment = {'body_blocks': [{'type': 'paragraph', 'content': '本项目投诉处理率100%。'}],
                'table_governance': {'dropped_tables': [{'reason': '有效单元格 0% < 25%。'}]}}
    result = evaluate_commitments({}, [], _governance_artifact(fragment))
    assert result['status'] == 'BLOCK'
    assert any(c['text'] == '100%' for c in result['violations'])
    assert not any(c['text'] in ('0%', '25%') for c in result['violations'])


def project(value):
    return _repair_customer_text(apply_artifact_repairs(apply_local_repairs(value)))


@pytest.mark.parametrize("value", [
    "岗位级当日复核：当班记录在交班之前逐条过一遍",
    "项目负责人季度与年度复核：检查记录体系本身",
    "登记与分级：把发现的问题变成一条可追踪的条目",
    "判定口径与责任岗位",
    "判定口径与结论归属",
    "四、咨询解答与跨岗位转介及登记口径",
])
def test_operational_cell_is_not_deleted(value):
    assert project({"rows": [[value]]})["rows"][0][0] == value


def test_conditional_applicability_is_rewritten_without_loss():
    value = "以条件性方法表述，条件成立时适用的部分单独成节而不写成既定"
    got = project({"rows": [[value]]})["rows"][0][0]
    assert got == "相关措施仅在适用条件经确认成立后实施。"
    assert project({"rows": [[got]]})["rows"][0][0] == got


@pytest.mark.parametrize("text", [
    "按甲方书面确认结果登记适用性结论，未确认前不假定任何外委安排",
    "组织方式未确认，本方案不假定存在外委安排",
    "二、报事与投诉的首次响应时限数值",
])
def test_unconfirmed_register_is_not_rewritten_as_an_arrangement(text):
    assert project({"rows": [[text]]})["rows"][0][0] == text


def test_preexisting_conditional_does_not_exempt_another_clause():
    from governance.text_sanitize import replace_outsourcing_phrase
    assert replace_outsourcing_phrase("如采用外委方式，另行说明外委安排") == "如采用外委方式，另行说明如采用外委方式安排"


def test_internal_header_projects_to_business_limits():
    assert project({"columns": ["允许表述与禁止表述"]})["columns"] == ["适用范围与实施限制"]


def test_normalization_keeps_procedure_next_to_its_own_unit():
    from longform.local_normalize import normalize_fragment
    from longform.orchestrator import fragment_to_section
    cu = "CU-TEST-01"
    original = {"title": "日常核查", "section_id": "S-TEST",
                "body_blocks": [{"type": "paragraph", "content": "本岗位按下列流程核查。",
                                 "source_content_unit_id": cu},
                                {"type": "paragraph", "content": "另一岗位另行检查。",
                                 "source_content_unit_id": "CU-TEST-02"}],
                "processes": [{"steps": ["观察", "记录"], "source_content_unit_id": cu}]}
    normalized, _ = normalize_fragment(original)
    section = fragment_to_section(normalized, "日常核查")
    blocks = section["content_blocks"]
    assert [b["type"] for b in blocks] == ["paragraph", "subheading", "numbered_steps", "paragraph"]
    assert blocks[2]["source_content_unit_id"] == cu


def test_actual_writing_instructions_remain_removed():
    value = "本节把正文分为条件性方法与既定承诺，按评分口径删改句式。"
    assert project({"rows": [[value]]})["rows"][0][0] != value


def test_source_matrix_is_immutable_even_when_prose_changes():
    table = {"columns": ["安排"], "rows": [["如采用外委方式，供方到场后登记。"]]}
    contract = source_contract(table, "CU-TEST-01")
    original = copy.deepcopy(contract)
    result = project({**table, "table_source_contract": contract})
    assert result["table_source_contract"] == original
    assert digest(result["table_source_contract"]["source_values"]) == original["source_values_digest"]


def test_backend_governance_receipts_are_not_customer_prose():
    ledger = {"schema_version": "cu-table-governance-v0.1", "tables_rendered": 1,
              "dropped_tables": [{"reason": "same-column 100% repetition", "source_content_unit_id": "CU-TEST-01"}]}
    writing = "本节把正文分为条件性方法与既定承诺，按评分口径删改句式。"
    result = project({"table_governance": ledger,
                      "rows": [[writing]]})
    assert result["table_governance"] == ledger
    assert result["rows"][0][0] != writing
