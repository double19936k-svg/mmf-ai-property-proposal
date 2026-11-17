"""R07 supplement identity, arrival and source-attribution regressions.

These fixtures call the real persistence/assembly/repair entry points. The invoke
boundary is a local stub; no model, queue or external endpoint is exercised.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from longform.cu_assembly import apply_content_units
from longform.factory import read_json, write_json
from longform.orchestrator import fragment_to_section
from test_mmf007c_unified_budget import _factory

SID = "S-A"
BASE = "CU-S-A-01"
NEW = "CU-S-A-03"


def contract(cu_id=BASE, **changes):
    return {"content_unit_id": cu_id, "parent_section_id": SID,
            "archetype": "NARRATIVE", "source_requirements": ["REQ-0001"],
            **changes}


def setup_factory(root, *, missing=False):
    contracts = {BASE: contract()}
    if missing:
        contracts["CU-S-A-02"] = contract("CU-S-A-02")
    factory = _factory(root, cu_contracts=contracts)
    section = next(x for x in factory.inputs["section_contracts"]["contracts"]
                   if x["section_id"] == SID)
    section.update({"section_title": "现场工作安排", "must_cover": [],
                    "required_outputs": [], "source_requirements": ["REQ-0001"],
                    "target_words": {"min": 100, "target": 800, "max": 2000},
                    "required_processes": []})
    factory.incremental_state.setdefault("generation_progress", {})
    factory.incremental_state.setdefault("cross_references", [])
    unit = factory.word_root / "sections" / SID
    return factory, section, unit


def persist_base(factory, unit):
    row = {"content_unit_id": BASE, "archetype": "NARRATIVE",
           "content": "原有岗位按台账检查现场情况，发现异常先登记并报告负责人。",
           "payload": {}}
    returned, issues = factory._persist_provider_cus(SID, {"content_unit_fragments": [row]}, [BASE])
    assert returned == [BASE] and not issues
    fragment, _ = factory._assemble_section_from_cus(SID, {"title": "现场工作安排"})
    write_json(unit / "fragment.json", fragment)
    return fragment


def response(row):
    return {"section_id": SID, "title": "现场工作安排", "body_blocks": [],
            "tables": [], "processes": [], "callouts": [], "cross_references": [],
            "claims": [], "used_requirement_ids": ["REQ-0001"], "used_ku_ids": [],
            "generation_notes": [], "content_unit_fragments": [row]}


def long_payload_row(cu_id=NEW):
    # Most new information is in a structured payload, not the brief introduction.
    return {"content_unit_id": cu_id, "archetype": "STANDARD_TABLE",
            "content": "本表明确每项工作对应的检查动作和留痕方式。",
            "payload": {"title": "逐项工作核对表", "columns": ["事项", "执行与核查"],
                        "rows": [[f"对象{i}", f"针对第{i}项对象核对现场状态、记录位置、作业条件、责任岗位和异常处理路径；"
                                  f"按对象编号{i}逐项记录观察，核查记录字段后交下一岗位接续，"
                                  "未取得确认的取值继续留空，接续时保留原始观察与处理说明。"]
                                 for i in range(8)]}}


def stub_invoke(factory, output):
    factory._invoke = lambda *args, **kwargs: (copy.deepcopy(output), {"status": "SUCCESS", "invoked": False})


def test_registration_rejects_collision(tmp_path):
    factory, _, unit = setup_factory(tmp_path / "r")
    persist_base(factory, unit)
    original = (unit / "cus" / f"{BASE}.json").read_bytes()
    before = copy.deepcopy(factory.cu_contracts)
    with pytest.raises(ValueError):
        factory._register_supplement_cus(SID, [contract(BASE, title="另一项新补充")])
    assert factory.cu_contracts == before
    assert (unit / "cus" / f"{BASE}.json").read_bytes() == original


def test_registration_batch_is_atomic(tmp_path):
    factory, _, _ = setup_factory(tmp_path / "r")
    before = copy.deepcopy(factory.cu_contracts)
    with pytest.raises(ValueError):
        factory._register_supplement_cus(SID, [contract(NEW), contract(BASE)])
    assert factory.cu_contracts == before


def test_discard_cannot_delete_planned_unit(tmp_path):
    factory, _, unit = setup_factory(tmp_path / "r")
    persist_base(factory, unit)
    original = (unit / "cus" / f"{BASE}.json").read_bytes()
    factory._discard_supplement_cus(SID, [BASE])
    assert BASE in factory._planned_cus(SID)
    assert (unit / "cus" / f"{BASE}.json").read_bytes() == original


def test_registered_supplement_survives_restart(tmp_path):
    root = tmp_path / "r"
    factory, _, _ = setup_factory(root)
    factory._register_supplement_cus(SID, [contract(NEW)])
    fresh, _, _ = setup_factory(root)
    assert NEW in fresh._planned_cus(SID)
    assert fresh.cu_contracts[NEW]["source_requirements"] == ["REQ-0001"]


def test_failed_new_intents_do_not_reuse_ids(tmp_path):
    factory, section, _ = setup_factory(tmp_path / "r")
    section["target_words"] = {"min": 0}
    ids = []
    def rejected(job, section, failure, **kwargs):
        rows = kwargs["supplement_rows"]
        ids.extend(x["content_unit_id"] for x in rows)
        factory._register_supplement_cus(SID, rows)
        factory._discard_supplement_cus(SID, [x["content_unit_id"] for x in rows])
        return False
    factory._repair_section = rejected
    factory._checkpoint = lambda *args, **kwargs: None
    factory._record_qa_event = lambda *args, **kwargs: None
    factory.supplement_depth([
        {"section_id": SID, "issue_key": "A", "focus": [{"topic": "日常检查", "archetype": "CHECKLIST"}]},
        {"section_id": SID, "issue_key": "B", "focus": [{"topic": "异常报告", "archetype": "SOP"}]},
    ])
    assert len(ids) == 2
    assert ids[0] != ids[1]


def test_rejected_reservation_survives_restart(tmp_path):
    root = tmp_path / "r"
    factory, _, _ = setup_factory(root)
    factory._register_supplement_cus(SID, [contract(NEW)])
    factory._discard_supplement_cus(SID, [NEW])
    fresh, _, _ = setup_factory(root)
    assert NEW not in fresh._planned_cus(SID)
    with pytest.raises(ValueError):
        fresh._register_supplement_cus(SID, [contract(NEW)])


def test_direct_usage_is_persisted(tmp_path):
    factory, _, unit = setup_factory(tmp_path / "r")
    row = {"content_unit_id": BASE, "content": "岗位按记录核查。", "payload": {},
           "used_requirement_ids": ["REQ-0001"], "used_ku_ids": ["KU-0001"]}
    factory._persist_provider_cus(SID, {"content_unit_fragments": [row]}, [BASE])
    saved = read_json(unit / "cus" / f"{BASE}.json")
    assert saved["used_requirement_ids"] == ["REQ-0001"]
    assert saved["used_ku_ids"] == ["KU-0001"]


def test_single_unit_section_usage_is_bound(tmp_path):
    factory, _, unit = setup_factory(tmp_path / "r")
    output = response({"content_unit_id": BASE, "content": "根据检查结果登记。", "payload": {}})
    factory._persist_provider_cus(SID, output, [BASE])
    saved = read_json(unit / "cus" / f"{BASE}.json")
    assert saved["used_requirement_ids"] == ["REQ-0001"]
    assert saved["requirement_usage_kind"] == "SECTION_RESPONSE_SINGLE_CU"


def test_multi_unit_usage_remains_section_scoped(tmp_path):
    factory, _, unit = setup_factory(tmp_path / "r", missing=True)
    output = {"used_requirement_ids": ["REQ-0001"], "content_unit_fragments": [
        {"content_unit_id": cu, "content": "按该岗位记录观察。", "payload": {}}
        for cu in [BASE, "CU-S-A-02"]]}
    factory._persist_provider_cus(SID, output, [BASE, "CU-S-A-02"])
    for cu in [BASE, "CU-S-A-02"]:
        saved = read_json(unit / "cus" / f"{cu}.json")
        assert saved["used_requirement_ids"] == []
        assert saved["section_response_used_requirement_ids"] == ["REQ-0001"]


def test_assembly_preserves_declared_not_planned_usage():
    rows = [{"content_unit_id": BASE, "content": "岗位检查安排。", "payload": {},
             "used_requirement_ids": ["REQ-0001"], "used_ku_ids": ["KU-0001"],
             "planned_requirement_ids": ["REQ-9999"], "requirement_usage_kind": "CU_RESPONSE"}]
    out = apply_content_units(rows, base_fragment={}, section_id=SID, planned_ids=[BASE])
    assert out["used_requirement_ids"] == ["REQ-0001"]
    assert "REQ-9999" not in out["used_requirement_ids"]
    assert out["content_unit_fragments"][0]["planned_requirement_ids"] == ["REQ-9999"]


def test_reassembly_does_not_duplicate_cu_structures():
    row = {"content_unit_id": BASE, "archetype": "STANDARD_TABLE", "content": "检查职责。",
           "payload": {"title": "岗位工作表", "columns": ["岗位", "动作"],
                       "rows": [["甲", "记录"], ["乙", "复查"]]}}
    plain = {"title": "未绑定表", "columns": ["对象", "记录"], "rows": [["丙", "核验"]]}
    one = apply_content_units([row], base_fragment={"tables": [plain]}, section_id=SID, planned_ids=[BASE])
    two = apply_content_units([row], base_fragment=one, section_id=SID, planned_ids=[BASE])
    assert len(two["tables"]) == len(one["tables"]) == 2
    assert two["tables"] == one["tables"]


def test_checklist_keeps_its_cu_identity():
    row = {"content_unit_id": BASE, "archetype": "CHECKLIST", "content": "以下核对项。",
           "payload": {"items": ["现场观察", "记录核查"]}}
    out = apply_content_units([row], base_fragment={}, section_id=SID, planned_ids=[BASE])
    block = next(x for x in out["body_blocks"] if x["type"] == "bullet_group")
    assert block["source_content_unit_id"] == BASE
    section = fragment_to_section(out, "核查安排", section_id=SID)
    assert next(x for x in section["content_blocks"] if x["type"] == "bullet_group")["source_content_unit_id"] == BASE


def test_structured_supplement_is_measured_in_full(tmp_path):
    factory, section, unit = setup_factory(tmp_path / "r")
    persist_base(factory, unit)
    stub_invoke(factory, response(long_payload_row()))
    ok = factory._repair_section(factory._job(factory.word_root, "WORD", 1), section,
                                 "UNDER_LENGTH", supplement_rows=[contract(NEW, archetype="STANDARD_TABLE")])
    assert ok
    decision = read_json(unit / "supplement.json")
    assert decision["novelty"]["passed"]
    assert decision["novelty"]["arrived_chars"] >= 360
    assert (unit / "provider_attempt_1" / "supplement_decision.json").is_file()
    assert "对象7" in json.dumps(read_json(unit / "fragment.json")["tables"], ensure_ascii=False)


def test_copying_payload_is_still_rejected(tmp_path):
    factory, section, unit = setup_factory(tmp_path / "r")
    base = long_payload_row(BASE)
    factory._persist_provider_cus(SID, response(base), [BASE])
    previous, _ = factory._assemble_section_from_cus(SID, {"title": "现场工作安排"})
    write_json(unit / "fragment.json", previous)
    copied = {**copy.deepcopy(base), "content_unit_id": NEW}
    stub_invoke(factory, response(copied))
    assert not factory._repair_section(factory._job(factory.word_root, "WORD", 1), section,
                                       "UNDER_LENGTH", supplement_rows=[contract(NEW, archetype="STANDARD_TABLE")])
    assert read_json(unit / "supplement.json")["novelty"]["reason"] == "LOW_INFORMATION_GAIN"
    assert (unit / "cus" / f"{BASE}.json").is_file()
    assert not (unit / "cus" / f"{NEW}.json").is_file()


def test_metadata_cannot_supply_information_gain(tmp_path):
    factory, section, unit = setup_factory(tmp_path / "r")
    persist_base(factory, unit)
    row = {"content_unit_id": NEW, "archetype": "NARRATIVE", "content": "新增说明。", "payload": {},
           "planned_requirement_ids": [f"REQ-{i:04d}" for i in range(500)],
           "generation_notes": ["伪造增量"*200]}
    stub_invoke(factory, response(row))
    assert not factory._repair_section(factory._job(factory.word_root, "WORD", 1), section,
                                       "UNDER_LENGTH", supplement_rows=[contract(NEW)])
    assert read_json(unit / "supplement.json")["novelty"]["reason"] == "BELOW_MIN_SUPPLEMENT_CHARS"


@pytest.mark.parametrize("archetype,payload", [
    ("STANDARD_TABLE", {"title": "核查记录", "columns": ["事项"], "rows": [["登记"]]}),
    ("SOP", {"title": "核查顺序", "steps": ["查看", "登记"]}),
])
def test_unrendered_payload_metadata_cannot_supply_information_gain(tmp_path, archetype, payload):
    factory, section, unit = setup_factory(tmp_path / "r")
    persist_base(factory, unit)
    row = {"content_unit_id": NEW, "archetype": archetype, "content": "新增说明。",
           "payload": {**payload, "provider_diagnostics": "非正文的附带记录" * 100}}
    stub_invoke(factory, response(row))
    assert not factory._repair_section(factory._job(factory.word_root, "WORD", 1), section,
                                       "UNDER_LENGTH", supplement_rows=[contract(NEW, archetype=archetype)])
    assert read_json(unit / "supplement.json")["novelty"]["reason"] == "BELOW_MIN_SUPPLEMENT_CHARS"


def test_arrived_supplement_is_not_lost_to_old_gap(tmp_path):
    factory, section, unit = setup_factory(tmp_path / "r", missing=True)
    persist_base(factory, unit)
    stub_invoke(factory, response(long_payload_row()))
    ok = factory._repair_section(factory._job(factory.word_root, "WORD", 1), section,
                                 "UNDER_LENGTH", supplement_rows=[contract(NEW, archetype="STANDARD_TABLE")])
    assert ok
    final = read_json(unit / "fragment.json")
    assert NEW in final["content_unit_execution"]["assembled_content_unit_ids"]
    assert "CU-S-A-02" in final["content_unit_execution"]["outstanding_content_unit_ids"]
    assert "对象7" in json.dumps(final["tables"], ensure_ascii=False)
    assert not factory._section_is_settled(SID)
    assert factory._section_status(SID) == "COMPLETED_PROVISIONAL"


def test_registry_does_not_make_content_covered(tmp_path):
    factory, _, unit = setup_factory(tmp_path / "r")
    factory._register_supplement_cus(SID, [contract(NEW, source_requirements=["REQ-9999"])])
    factory._persist_provider_cus(SID, response({"content_unit_id": NEW, "content": "只描述一般方法。", "payload": {}}), [NEW])
    final, _ = factory._assemble_section_from_cus(SID, {})
    assert "REQ-9999" not in final.get("used_requirement_ids", [])
    assert read_json(unit / "cus" / f"{NEW}.json")["planned_requirement_ids"] == ["REQ-9999"]


def test_resume_accepts_same_explicit_contracts(tmp_path):
    root = tmp_path / "r"
    factory, _, _ = setup_factory(root)
    factory._register_supplement_cus(SID, [contract(NEW)])
    fresh = _factory(root, cu_contracts=copy.deepcopy(factory.cu_contracts))
    assert fresh._planned_cus(SID).count(NEW) == 1


def test_cleanup_rejects_unowned_replacement(tmp_path):
    factory, _, unit = setup_factory(tmp_path / "r")
    factory._register_supplement_cus(SID, [contract(NEW)])
    factory._persist_provider_cus(SID, response({"content_unit_id": NEW, "content": "现场核查。", "payload": {}}), [NEW])
    path = unit / "cus" / f"{NEW}.json"
    tampered = read_json(path)
    tampered["supplement_registration_id"] = "0" * 32
    write_json(path, tampered)
    original = path.read_bytes()
    factory._discard_supplement_cus(SID, [NEW])
    assert path.read_bytes() == original
    assert NEW in factory._planned_cus(SID)
    registry = read_json(unit / "supplement_cu_registry.json")
    assert registry["reservations"][NEW]["status"] == "CLEANUP_BLOCKED"
    assert NEW not in factory._completed_cus(SID)
    final, issues = factory._assemble_section_from_cus(SID, {})
    assert any(NEW in issue for issue in issues)
    assert NEW not in final.get("content_unit_execution", {}).get("assembled_content_unit_ids", [])


def test_settlement_records_landed_subset(tmp_path):
    factory, section, unit = setup_factory(tmp_path / "r")
    persist_base(factory, unit)
    stub_invoke(factory, response(long_payload_row()))
    observations = []
    def settle(audit, **kwargs):
        observations.append({"ids": kwargs["persisted_content_unit_ids"],
                             "file_present": (unit / "cus" / f"{NEW}.json").is_file()})
    factory._settle_request = settle
    missing = "CU-S-A-04"
    ok = factory._repair_section(factory._job(factory.word_root, "WORD", 1), section,
                                 "UNDER_LENGTH", supplement_rows=[contract(NEW, archetype="STANDARD_TABLE"), contract(missing)])
    assert ok
    assert observations == [{"ids": [NEW], "file_present": True}]
    assert read_json(unit / "supplement.json")["status"] == "PARTIAL_ACCEPTED"
    assert missing in read_json(unit / "fragment.json")["content_unit_execution"]["outstanding_content_unit_ids"]
    assert not factory._section_is_settled(SID)


def test_persistence_failure_is_not_settled(tmp_path):
    factory, section, unit = setup_factory(tmp_path / "r")
    persist_base(factory, unit)
    stub_invoke(factory, response(long_payload_row()))
    settlements = []
    factory._settle_request = lambda *args, **kwargs: settlements.append(kwargs)
    def failed(*args, **kwargs):
        raise OSError("injected persistence failure")
    factory._persist_provider_cus = failed
    with pytest.raises(OSError, match="injected persistence failure"):
        factory._repair_section(factory._job(factory.word_root, "WORD", 1), section,
                                "UNDER_LENGTH", supplement_rows=[contract(NEW, archetype="STANDARD_TABLE")])
    assert settlements == []


@pytest.mark.parametrize("key,value", [
    ("supplement_registration_id", "0" * 32),
    ("content_unit_id", "CU-FOREIGN-01"),
    ("parent_section_id", "S-FOREIGN"),
    ("generation_origin", "foreign_source"),
])
def test_reserved_identity_mismatch_is_not_an_arrival(tmp_path, key, value):
    factory, _, unit = setup_factory(tmp_path / "r")
    persist_base(factory, unit)
    factory._register_supplement_cus(SID, [contract(NEW)])
    factory._persist_provider_cus(SID, response({"content_unit_id": NEW,
                                  "content": "不能被借用的外来内容。", "payload": {}}), [NEW])
    path = unit / "cus" / f"{NEW}.json"
    row = read_json(path)
    row[key] = value
    write_json(path, row)
    assert NEW not in factory._completed_cus(SID)
    assert NEW in factory._missing_cus(SID)
    final, issues = factory._assemble_section_from_cus(SID, {})
    assert issues and NEW in final["content_unit_execution"]["outstanding_content_unit_ids"]
    assert "不能被借用" not in json.dumps(final["body_blocks"], ensure_ascii=False)
