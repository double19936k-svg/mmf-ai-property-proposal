import copy

import pytest

from governance import apply_artifact_repairs, apply_local_repairs
from governance.final_editorial import SourceFacts
from governance.table_fidelity import KEY, TableFidelityError, require_source_tables, source_contract
from longform.factory import _repair_customer_text


SOURCE = "设施设备及照明完好率保持95%以上，故障处理及时有效"


def source_facts(source=SOURCE):
    return SourceFacts({"paragraphs": [{"para_id": "P-KPI", "file_id": "verified-fixture", "text": source}]})


def table_for(text=SOURCE):
    table = {"columns": ["事项", "范围", "质量要求"], "rows": [["设备运行", "设施设备及照明", text]]}
    table[KEY] = source_contract(table, "CU-KPI")
    return table


def customer_chain(value, facts):
    return _repair_customer_text(apply_artifact_repairs(apply_local_repairs(value)), facts)


@pytest.mark.parametrize("token", ["95%", "95％", "95.5%"])
def test_source_backed_equipment_kpi_survives_customer_cleanup(token):
    source = SOURCE.replace("95%", token)
    original = table_for(source)
    before = copy.deepcopy(original)
    cleaned = customer_chain(original, source_facts(source))
    assert cleaned["rows"][0][2] == source
    assert cleaned[KEY] == before[KEY]
    assert original == before
    require_source_tables(cleaned)


def test_source_binding_exposes_the_requirement_locator():
    facts = source_facts()
    lo = SOURCE.index("95%")
    binding = facts.numeric_binding(SOURCE, lo, lo + 3)
    assert binding["locator"] == "P-KPI"
    assert binding["source_text"] == SOURCE


@pytest.mark.parametrize("unsupported", ["设施设备及照明完好率保持99%以上", "客户满意率保持95%以上"])
def test_same_number_or_self_asserted_table_contract_does_not_invent_support(unsupported):
    original = table_for(unsupported)
    cleaned = customer_chain(original, source_facts())
    assert cleaned["rows"][0][2] != unsupported
    with pytest.raises(TableFidelityError):
        require_source_tables(cleaned)


def test_unverified_percentage_still_requires_confirmation():
    cleaned = customer_chain(table_for(), SourceFacts())
    assert "95%" not in cleaned["rows"][0][2]
    with pytest.raises(TableFidelityError):
        require_source_tables(cleaned)


def test_source_fidelity_gate_still_blocks_actual_cell_drift():
    cleaned = customer_chain(table_for(), source_facts())
    cleaned["rows"][0][2] = SOURCE.replace("95%", "94%")
    with pytest.raises(TableFidelityError):
        require_source_tables(cleaned)
