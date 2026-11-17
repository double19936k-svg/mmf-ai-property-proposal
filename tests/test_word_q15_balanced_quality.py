import copy,json,os,subprocess,sys
from pathlib import Path
import pytest
from docx import Document
from governance.client_facing_prose import client_facing_prose,internal_narration_findings
from governance.table_fidelity import (source_contract,check_source_table,project_source_table,final_table_findings,TableFidelityError)
from governance.customer_hygiene import hygiene_findings
from longform.factory import _repair_word_artifact
from longform.orchestrator import assemble_word_artifact
from planning.planner import build_planning_bundle
from test_mmf007a_hierarchy import _pack,_SELECTION
from test_mmf007_final_closure import _cu_factory,MOCK_CONFIG
from providers.mock import MockProvider

def table(cu="CU-TEST-01"):
    t={"title":"服务期限与岗位记录","columns":["物业服务项目","本项目履约期限与确认方式"],"rows":[
        ["服务期及实际日期确认","正文表述为3年服务期，具体起讫日期以实际签订合同为准"],
        ["项目名称与项目编号在来源文件中的登记口径","项目名称与编号以双方确认的档案分别登记"],
        ["工程巡检岗位与记录","工程6人；负责人按岗位清单组织巡检并保留当班记录"]]}
    t["table_source_contract"]=source_contract(t,cu)
    return t

def fragment(t):
    return {"title":"服务安排","section_id":"S01-01","body_blocks":[{"type":"paragraph","content":"工程岗位负责检查设备运行状态，发现异常当班登记并报告负责人。"}],"tables":[t],"processes":[],"claims":[]}

def put(p,x):
    p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x,ensure_ascii=False),encoding="utf-8")

def test_cleanup_preserves_business_quantity_and_business_row_label():
    t=table();before=copy.deepcopy(t["table_source_contract"])
    from governance.final_editorial import SourceFacts
    facts=SourceFacts({"paragraphs":[{"text":"本项目服务期3年"}],"tables":[{"columns":["岗位","人数"],"rows":[["工程","6"],["前台","2"],["合计","8"]]}]})
    cleaned=_repair_word_artifact(fragment(t),facts)
    assert cleaned["tables"][0]["rows"][0][1]=="3年服务期，具体起讫日期以实际签订合同为准"
    assert cleaned["tables"][0]["rows"][1][0]==t["rows"][1][0]
    assert cleaned["tables"][0]["table_source_contract"]==before
    assert check_source_table(cleaned["tables"][0])==[]

def test_verified_role_category_total_in_team_prose_is_preserved_but_not_exercise_attendance():
    from test_word_q10_staff_pipeline import facts
    source=facts()
    business="服务保障的难点在于2类岗位13人的小团队同时覆盖门岗与保洁任务。"
    got=_repair_word_artifact({"body_blocks":[{"content":business}]},source)
    assert "2类岗位13人的小团队" in got["body_blocks"][0]["content"]
    attendance="演练按2类岗位13人的小团队安排参演。"
    lo=attendance.index("13")
    assert source.numeric_binding(attendance,lo,lo+3) is None

def test_unsafe_cleanup_becomes_review_instead_of_destroying_quantity():
    text="本节把正文分层，正文表述为3年服务期。"
    got,actions=client_facing_prose(text)
    assert "3年" in got
    assert any(not a["applied"] for a in actions)
    assert all(f["severity"]=="NEEDS_REVIEW" for f in internal_narration_findings(text))

@pytest.mark.parametrize("source,actual",[("3年","三年"),("24小时","二十四小时"),("2人","两人"),("2026年","二〇二六年")])
def test_equivalent_quantities_keep_same_cell_meaning(source,actual):
    t={"columns":["期限与数量"],"rows":[[source]]};t["table_source_contract"]=source_contract(t,"CU-Q")
    t["rows"]=[[actual]]
    assert check_source_table(t)==[]

@pytest.mark.parametrize("source,actual",[("3年","2年"),("3年","3天"),("工程6人","保洁6人"),("001号","1号"),("95%","90%")])
def test_changed_fact_unit_subject_or_identifier_remains_blocked(source,actual):
    t={"columns":["期限与数量"],"rows":[[source]]};t["table_source_contract"]=source_contract(t,"CU-Q")
    t["rows"]=[[actual]]
    assert all(f["severity"]=="BLOCK" for f in check_source_table(t))
    assert check_source_table(t)

def test_local_projection_recovers_deleted_text_without_changing_receipt():
    t=table();t["rows"][0][1]="具体起讫日期以实际签订合同为准";t["rows"][1][0]=""
    before=copy.deepcopy(t)
    fixed=project_source_table(t)
    assert fixed is not None and check_source_table(fixed)==[]
    assert "3年" in fixed["rows"][0][1] and fixed["rows"][1][0]
    assert t==before and fixed["table_source_contract"]==t["table_source_contract"]

def test_invalid_receipt_never_becomes_automatic_source_authority():
    t=table();t["table_source_contract"]["source_values_digest"]="wrong"
    assert check_source_table(t) and project_source_table(t) is None

def test_style_is_review_while_unverified_numeric_claim_is_block():
    tone=hygiene_findings("本方案不构成对本项目的直接商业承诺")
    assert next(f for f in tone if f["rule_id"]=="INTERNAL_GOVERNANCE_TONE")["severity"]=="NEEDS_REVIEW"
    facts=hygiene_findings("客户重复投诉超过三次，需要升级处理。")
    assert next(f for f in facts if f["rule_id"]=="UNSUPPORTED_NUMERIC_THRESHOLD")["severity"]=="BLOCK"

@pytest.mark.parametrize("rule",["TABLE_SOURCE_FIDELITY","SOURCE_SERVICE_REQUIREMENT_OMITTED","PAGE_CONTRACT_BODY_TOO_SHORT","PAGE_CONTRACT_PHYSICAL_LIMIT"])
def test_explicit_integrity_and_page_limits_are_not_demoted_by_document_summary(rule):
    from governance.document_qa import regrade_findings
    finding=regrade_findings([{"rule_id":rule,"severity":"BLOCK","issue":"explicit requirement not met"}])[0]
    assert finding["severity"]=="BLOCK" and finding["provider_repair_eligible"]

def test_only_failing_cu_is_requested_for_material_table_repair(tmp_path,monkeypatch):
    f,sid,ids=_cu_factory(tmp_path,MockProvider(MOCK_CONFIG))
    contract=f.inputs["section_contracts"]["contracts"][0]
    captured={}
    class ReachedQueue(Exception):pass
    def invoke(request,*args,**kwargs):
        captured.update(request);raise ReachedQueue()
    monkeypatch.setattr(f,"_invoke",invoke)
    job=f._job(f.word_root,"WORD",1)
    with pytest.raises(ReachedQueue):
        f._repair_section(job,contract,"TABLE_SOURCE_FIDELITY",force=True,draftable_ok=True,target_content_unit_ids=[ids[-1]])
    assert captured["content_unit_execution"]["remaining_content_unit_ids"]==[ids[-1]]
    assert [x["content_unit_id"] for x in captured["content_unit_contracts"]]==[ids[-1]]
    with pytest.raises(ValueError):
        f._repair_section(job,contract,"TABLE_SOURCE_FIDELITY",force=True,target_content_unit_ids=["CU-FOREIGN"])

def test_unrepairable_receipt_is_asked_once_across_resume_and_stays_blocked(tmp_path,monkeypatch):
    f,sid,ids=_cu_factory(tmp_path,MockProvider(MOCK_CONFIG))
    t=table(ids[-1]);t["table_source_contract"]["source_values_digest"]="wrong"
    put(f.word_root/"sections"/sid/"fragment.json",fragment(t))
    seen=[]
    monkeypatch.setattr(f,"selective_repair",lambda candidates,**kwargs:seen.extend(candidates) or {"status":"STILL_OPEN"})
    assert f.repair_table_projections([sid])["status"]=="BLOCK"
    assert [x["content_unit_ids"] for x in seen]==[[ids[-1]]]
    other,_,_=_cu_factory(tmp_path,MockProvider(MOCK_CONFIG))
    monkeypatch.setattr(other,"selective_repair",lambda *a,**k:pytest.fail("same bad receipt must not be re-asked on resume"))
    assert other.repair_table_projections([sid])["status"]=="BLOCK"

def test_local_table_recovery_uses_no_provider_and_preserves_cu_files(tmp_path):
    provider=MockProvider(MOCK_CONFIG);f,sid,ids=_cu_factory(tmp_path,provider)
    t=table(ids[-1]);t["rows"][0][1]="具体起讫日期以实际签订合同为准";t["rows"][1][0]=""
    unit=f.word_root/"sections"/sid
    put(unit/"fragment.json",fragment(t))
    for cu in ids:put(unit/"cus"/(cu+".json"),{"content_unit_id":cu,"content":"原始模型内容保持不变"})
    original={p.name:p.read_bytes() for p in (unit/"cus").glob("*.json")}
    result=f.repair_table_projections([sid])
    assert result["status"]=="PASS" and result["local_actions"] and not provider.received_requests
    assert {p.name:p.read_bytes() for p in (unit/"cus").glob("*.json")}==original
    assert list((unit/"local_table_repair_history").glob("*.json"))
    assert f.repair_table_projections([sid])["local_actions"]==[]

def test_candidate_word_retains_other_sections_and_final_fact_block(tmp_path):
    t=table();t["rows"][0][1]="2年服务期，具体起讫日期以实际签订合同为准"
    plan={"outline":[{"chapter_id":"CH01","chapter_title":"服务组织","sections":[
        {"section_id":"S01-01","section_title":"期限与岗位"},{"section_id":"S01-02","section_title":"其他服务"}]}]}
    fragments={"S01-01":fragment(t),"S01-02":{"title":"其他服务","body_blocks":[{"type":"paragraph","content":"保洁岗位按清单检查现场环境，异常交负责人处理并记录复核结果。"}]}}
    with pytest.raises(TableFidelityError):assemble_word_artifact({},plan,fragments)
    artifact=assemble_word_artifact({},plan,fragments,allow_unresolved_tables=True)
    assert artifact["source_table_findings"]
    assert [s["section_id"] for s in artifact["sections"]]==["CH01","S01-01","S01-02"]
    source=tmp_path/"candidate.json";out=tmp_path/"candidate.docx"
    put(source,{"brief":{"project_name":"物业服务项目","scenario":"完整物业服务方案"},"artifact":artifact})
    env=dict(os.environ);env["MMF_ARTIFACT_BUILD_METRICS"]=str(tmp_path/"artifact_table_build_metrics.json")
    builder=Path(__file__).resolve().parents[1]/"app/build_docx.py"
    done=subprocess.run([sys.executable,"-B",str(builder),str(source),str(out)],env=env,capture_output=True,text=True,encoding="utf-8")
    assert done.returncode==0,done.stderr
    doc=Document(out)
    assert any("保洁岗位按清单" in p.text for p in doc.paragraphs)
    assert doc.tables and "2年" in doc.tables[0].rows[1].cells[1].text
    findings=final_table_findings(out,tmp_path)
    assert any(f["severity"]=="BLOCK" and f["rule_id"]=="TABLE_SOURCE_FIDELITY" for f in findings)

def test_reference_info_is_not_promoted_to_mandatory_prose_and_scopes_stay_registered():
    pack=copy.deepcopy(_pack())
    extras=[("X-PROC","招标代理机构：某商务公司","MUST"),("X-REVIEW","初步评审标准","MUST"),
            ("X-DATE","2021年9月","INFO"),("X-SVC","工程岗位检查设备状态并登记异常","INFO")]
    for rid,text,level in extras:pack["requirements"].append({"requirement_id":rid,"normalized_requirement":text,"domain":"other","mandatory_level":level,"confirmation_status":"CONFIRMED","tender_content_class":"SERVICE_REQUIREMENT"})
    b=build_planning_bundle(pack,{"project_name":"物业服务项目","medium":"WORD","project_type":"办公物业","scenario":"完整物业服务方案"},_SELECTION,production=True)
    prose=[v for c in b["section_contracts"]["contracts"] for v in c["must_cover"]]
    assert "2021年9月" not in prose and extras[0][1] not in prose and extras[1][1] not in prose
    assert extras[3][1] in prose
    classified={r["requirement_id"]:r for r in b["requirement_classification"]["rows"]}
    assert set(r[0] for r in extras).issubset(classified)
    assert classified["X-PROC"]["scope_disposition"]==classified["X-REVIEW"]["scope_disposition"]=="EXCLUDED"

