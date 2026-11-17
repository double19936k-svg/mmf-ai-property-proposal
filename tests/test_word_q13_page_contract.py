from copy import deepcopy
import pytest
from planning.planner import build_document_target, build_planning_bundle
from planning.hierarchy import build_document_hierarchy
from planning.page_contract import evaluate_page_contract
from test_mmf007a_hierarchy import _pack, _SELECTION

LIMITS={"technical_pages_min":200,"physical_pages_max":300,"measurement_required":True}

def test_explicit_profile_expands_real_budgets_and_hierarchy():
    brief={"project_name":"银行物业项目","medium":"WORD","project_type":"金融办公物业","scenario":"技术标","document_scale":"bounded_technical_tender","speed_profile":"fast"}
    pack=_pack()
    bundle=build_planning_bundle(pack,brief,_SELECTION,production=True)
    regular=build_planning_bundle(pack,{**brief,"document_scale":"regular_solution"},_SELECTION,production=True)
    target=bundle["document_target"]["word"]
    assert target["page_constraints"]["technical_pages_min"]==200
    assert target["page_constraints"]["physical_pages_max"]==300
    assert target["planning_pages"]==240
    totals=bundle["content_budget"]["totals"]
    assert (totals["target_effective_chars_min"],totals["target_effective_chars_target"],totals["target_effective_chars_max"])==(186000,223200,269700)
    assert totals["target_words_target"]==regular["content_budget"]["totals"]["target_words_target"]*4
    hierarchy=build_document_hierarchy(pack,bundle)
    assert hierarchy["pre_generation_budget_gate"]["may_enter_generation"]
    assert hierarchy["counts"]["planned_words"]>regular["content_budget"]["totals"]["target_words_target"]*2
    assert len(hierarchy["content_units"])>len(build_document_hierarchy(pack,regular)["content_units"])
    assert len(bundle["requirement_matrix"]["matrix"])==len(regular["requirement_matrix"]["matrix"])

@pytest.mark.parametrize("physical,technical,expected",[(69,66,"BLOCK"),(202,199,"BLOCK"),(203,200,"PASS"),(245,240,"PASS"),(300,295,"PASS"),(301,290,"BLOCK"),(301,199,"BLOCK"),(206,0,"BLOCK")])
def test_hard_limits_use_body_minimum_and_whole_document_maximum(physical,technical,expected):
    report=evaluate_page_contract({"physical_pages":physical,"technical_effective_pages":technical,"layout_basis":"NO_LAYOUT_FEATURES","technical_page_count_bound":"UPPER_BOUND_TEXT_ONLY"},LIMITS)
    assert report["status"]==expected
    assert report["technical_page_count_bound"]=="UPPER_BOUND_TEXT_ONLY"
    assert report["manual_acceptance"]=="PENDING"

@pytest.mark.parametrize("measurement",[None,{},{"physical_pages":250},{"physical_pages":None,"technical_effective_pages":240},{"physical_pages":200,"technical_effective_pages":210}])
def test_estimates_or_missing_body_measurement_never_pass(measurement):
    assert evaluate_page_contract(measurement,LIMITS)["status"]=="NOT_MEASURED"

def test_legacy_scale_is_unchanged_and_profile_constraints_are_not_shared():
    regular=build_document_target(target_scale="regular_solution")["word"]
    assert (regular["target_pages_min"],regular["target_pages_max"])==(50,70)
    assert "page_constraints" not in regular
    target=build_document_target(target_scale="bounded_technical_tender")["word"]
    target["page_constraints"]["physical_pages_max"]=999
    assert build_document_target(target_scale="bounded_technical_tender")["word"]["page_constraints"]["physical_pages_max"]==300
    assert evaluate_page_contract({},None)["status"]=="NOT_APPLICABLE"

def test_actual_document_scale_report_enforces_hard_physical_limit(monkeypatch,tmp_path):
    from longform import orchestrator
    brief={"project_name":"银行物业项目","medium":"WORD","project_type":"金融办公物业","scenario":"技术标","document_scale":"bounded_technical_tender"}
    bundle=build_planning_bundle(_pack(),brief,_SELECTION,production=True)
    monkeypatch.setattr(orchestrator,"_read_page_classification",lambda run:{"physical_pages":301,"technical_effective_pages":240})
    report,_=orchestrator.build_document_scale_report(bundle,{"sections":[{"title":"工程服务","body_blocks":[{"content":"工程巡检与保养"}]}]}, {},total_chars=180000,coverage_gate={"covered_must":10,"planned_must":10},hierarchy_metrics={"content_units":2,"content_units_by_archetype":{"STANDARD_TABLE":2}},completed_content_units=["a","b"],semantic_repetition=0,run_dir=tmp_path)
    assert report["status"]=="BLOCK"
    assert any(row["rule_id"]=="PAGE_CONTRACT_PHYSICAL_LIMIT" for row in report["findings"])
    assert report["page_contract"]["artifact_stage"]=="FIRST_DRAFT_MEASUREMENT"

