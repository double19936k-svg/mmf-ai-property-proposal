"""Regression evidence for Profile-P: actual business steps and source-bound assets."""
from __future__ import annotations
import copy,io,json,sys
from pathlib import Path
import pytest
from pypdf import PdfWriter
from pypdf.generic import NameObject,DictionaryObject,NumberObject,DecodedStreamObject
APP=Path(__file__).resolve().parents[1]/"app"
sys.path.insert(0,str(APP))
sys.path.insert(0,str(APP.parent))
from planning.manual_brief import parse_manual_brief
from planning.ppt_service import VERSION,build_plan,render_data,payload_issues,issues,visible_parts
from longform.final_assembly import build_ppt_slide_data
from project_assets import save_project_pdf,attach_project_images,verified_image,relabel_project_pdf

def pack():
    p=parse_manual_brief("[用户需求原文]\n根据项目简介，生成不少于30页、图文并茂的完整物业服务方案PPT，供业主比较。\n[项目简介逐页原文与页码]\n第1页：\n产业大厦-1#\n层数：24F；电梯8部。\n第2页：\n企业独栋-2#、3#\n层数：5F；整栋销售。\n研发中试车间-4#A、4#C\n层数：9F；分层销售。\n第3页：\n标准生产车间-5#、6#\n层数：9F；分户出租。",split_commas=False)
    p["ppt_service_contract"]=VERSION
    return p

def test_ppt_sentence_not_cut_at_comma():
    p=pack()
    assert len(p["requirements"])==1
    assert p["requirements"][0]["normalized_requirement"].endswith("供业主比较。")
    r=p["requirements"][0];span=r["source_span"]
    assert p["manual_input"]["text"][span["start"]:span["end"]]==r["normalized_requirement"]

def test_legacy_manual_contract_is_unchanged():
    assert len(parse_manual_brief("根据资料，生成方案，供业主比较。")["requirements"])>1

def test_buildings_and_lifecycle_are_source_bound():
    p=pack(); plan=build_plan(p,[])
    assert {g["kind"] for g in plan["building_profiles"]}=={"tower","villa","research","production"}
    topics={s["topic_id"] for s in plan["slide_storyboard"]}
    assert {"handover","first_90_days","fitout","energy","seasonal","vacancy","digital_phases"}<=topics
    assert 30<=len(plan["slide_storyboard"])<=50
    assert not any(s["slide_role"]=="case" for s in plan["slide_storyboard"])
    assert plan["coverage_summary"]["final_coverage"]=="NOT_ASSESSED"

def process_payload():
    return {"headline":"企业入驻服务","key_message":"把使用条件向企业说明清楚","content_blocks":[],
        "visual_data":{"layout_type":"process","steps":[
            {"title":"提前沟通","body":"核对使用范围、联系人与搬运计划，说明现场条件。"},
            {"title":"联合交接","body":"陪同查验房屋及共用设施，确认已发现的问题。"},
            {"title":"跟进入驻","body":"协调通行和装修安排，回访企业使用情况。"}]}}

def test_structured_steps_survive_assembly_without_slicing():
    payload=process_payload(); row={"profile_p_contract":VERSION,"layout_intent":"process","expected_steps":3}
    data=build_ppt_slide_data(payload,row)
    assert data["steps"]==payload["visual_data"]["steps"]
    assert not payload_issues(payload,row)
    assert "bullets" not in data

@pytest.mark.parametrize("phrase",["四步横向箭头依次连接需求","四个里程碑按时间顺序排列","每个里程碑对应一份可核对的交付物"])
def test_known_meta_language_is_blocked_not_silently_deleted(phrase):
    payload=process_payload();payload["visual_data"]["steps"][0]["body"]=phrase
    result=payload_issues(payload,{"layout_intent":"process","expected_steps":3})
    assert any(f["rule_id"]=="PPT-P-META" for f in result)
    assert payload["visual_data"]["steps"][0]["body"]==phrase

def test_real_business_feedback_language_is_allowed():
    payload=process_payload();payload["visual_data"]["steps"][-1]["body"]="回访结果回流至服务负责人，未解决问题继续跟进。"
    assert not payload_issues(payload,{"layout_intent":"process","expected_steps":3})

def test_missing_business_step_cannot_be_a_success():
    assert any(f["rule_id"]=="PPT-P-STEP-COUNT" for f in payload_issues(process_payload(),{"layout_intent":"process","expected_steps":5}))

def test_overlong_title_is_reported_and_preserved():
    payload=process_payload(); title="完整业务标题需要重新拟写但绝不截掉后半句话"
    payload["visual_data"]["steps"][0]["title"]=title
    assert render_data(payload,{"layout_intent":"process"})["steps"][0]["title"]==title
    assert any(f["rule_id"]=="PPT-P-NODE-TITLE" for f in payload_issues(payload,{"layout_intent":"process"}))

def test_only_rendered_carrier_is_counted():
    s={"profile_p_contract":VERSION,"title":"服务安排","layout":"modules","modules":[{"title":"企业联络","body":"为企业说明进度。"}],"bullets":["不应重复计算的回声"]}
    assert "不应重复计算的回声" not in "\n".join(visible_parts(s))
    from governance.artifact_qa import _walk_strings
    assert "不应重复计算的回声" not in "\n".join(t for _,t in _walk_strings(s))

def test_whole_ppt_qa_counts_completed_business_text_without_crashing(tmp_path):
    from longform.orchestrator import run_document_level_qa
    slide={"profile_p_contract":VERSION,"slide_id":"SL-001","title":"服务 安排",
        "core_message":"说明\n使用条件。","layout":"modules",
        "modules":[{"title":"企业联络","body":"告知办理进度。"},{"title":"专业复核","body":"核对设备条件。"}],
        "bullets":["这段回声不应计数"],"speaker_note_optional":"备注不应计数"}
    plan={"profile_p_contract":VERSION,"slide_storyboard":[{"slide_id":"SL-001"}]}
    result=run_document_level_qa(tmp_path,brief={},bundle={"ppt_plan":plan},contracts={},mode="full",medium="PPT",chosen=[],fragments={},gates=[],artifact={"slides":[slide]})
    assert result["total_chars"]==len("服务安排说明使用条件。企业联络告知办理进度。专业复核核对设备条件。")
    assert result["depth"]["completed_content_units"]==1
    assert result["coverage_gate"]["semantic_coverage"]=="PENDING_REVIEW"
    assert result["depth"]["status"]=="NEEDS_REVIEW"

def test_whole_ppt_qa_keeps_missing_planned_page_incomplete(tmp_path):
    from longform.orchestrator import run_document_level_qa
    plan={"profile_p_contract":VERSION,"slide_storyboard":[{"slide_id":"SL-001"},{"slide_id":"SL-002"}]}
    slide={"slide_id":"SL-001","title":"服务安排","layout":"modules","modules":[{"title":"企业联络","body":"说明办理进度。"},{"title":"专业复核","body":"确认使用条件。"}]}
    result=run_document_level_qa(tmp_path,brief={},bundle={"ppt_plan":plan},contracts={},mode="full",medium="PPT",chosen=[],fragments={},gates=[],artifact={"slides":[slide]})
    assert result["depth"]["status"]=="DEPTH_INCOMPLETE"
    assert any(f["rule_id"]=="PPT-P-MISSING" and f["slide_id"]=="SL-002" for f in result["depth"]["findings"])

def test_service_table_exports_explicit_chinese_font_and_editable_cells(tmp_path):
    import os,shutil,subprocess,zipfile
    import xml.etree.ElementTree as ET
    node=shutil.which("node");assert node,"PPT renderer runtime is required for export regression"
    script=tmp_path/"service_table_font.mjs";out=tmp_path/"service_table_font.pptx"
    data={"title":"服务安排","core_message":"明确责任与反馈","layout":"table","table":{"columns":["事项","安排"],"rows":[["报修","说明办理进度。"],["回访","确认使用结果。"]]}}
    script.write_text('import {createRequire} from "node:module";import {pathToFileURL} from "node:url";\n'+
        'import {renderServiceSlide} from '+json.dumps((APP/'ppt_renderer_v2/components.mjs').as_uri())+';\n'+
        'const entry=createRequire(import.meta.url).resolve("@oai/artifact-tool",{paths:[process.env.RUNTIME_NODE_MODULES]});\n'+
        'const {Presentation,PresentationFile}=await import(pathToFileURL(entry).href);\n'+
        'const p=Presentation.create({slideSize:{width:1280,height:720}});\n'+
        'renderServiceSlide(p.slides.add(),'+json.dumps(data,ensure_ascii=False)+',1,1,{},"test");\n'+
        'await (await PresentationFile.exportPptx(p)).save('+json.dumps(str(out))+');\n',encoding='utf-8')
    env=os.environ.copy();env['RUNTIME_NODE_MODULES']=str(APP/'node_modules')
    result=subprocess.run([node,str(script)],env=env,capture_output=True,text=True,encoding='utf-8',timeout=60)
    assert result.returncode==0,result.stderr
    ns={'a':'http://schemas.openxmlformats.org/drawingml/2006/main'}
    with zipfile.ZipFile(out) as z:table=ET.fromstring(z.read('ppt/slides/slide1.xml')).find('.//a:tbl',ns)
    assert table is not None
    assert [t.text for t in table.findall('.//a:t',ns)]==['事项','安排','报修','说明办理进度。','回访','确认使用结果。']
    for run in table.findall('.//a:rPr',ns):
        assert run.find('a:latin',ns).get('typeface')=='Microsoft YaHei'
        assert run.find('a:ea',ns).get('typeface')=='Microsoft YaHei'

def test_floor_load_and_unfounded_promises_are_detected():
    s={"title":"物流组织","layout":"modules","modules":[
        {"title":"设备匹配","body":"车间载重500kg/㎡，按货物重量匹配电梯与通道。"},
        {"title":"运行保障","body":"办公生产不因小故障中断。"}]}
    ids={f["rule_id"] for f in issues(s)}
    assert {"PPT-P-PARAMETER","PPT-P-PROMISE"}<=ids

def pdf_bytes():
    writer=PdfWriter();page=writer.add_blank_page(width=200,height=200)
    stream=DecodedStreamObject();stream.set_data(bytes([30,90,150])*(100*80))
    stream.update({NameObject("/Type"):NameObject("/XObject"),NameObject("/Subtype"):NameObject("/Image"),
        NameObject("/Width"):NumberObject(100),NameObject("/Height"):NumberObject(80),
        NameObject("/ColorSpace"):NameObject("/DeviceRGB"),NameObject("/BitsPerComponent"):NumberObject(8)})
    ref=writer._add_object(stream)
    page[NameObject("/Resources")]=DictionaryObject({NameObject("/XObject"):DictionaryObject({NameObject("/Im1"):ref})})
    content=DecodedStreamObject();content.set_data(b"q 100 0 0 80 20 20 cm /Im1 Do Q")
    page[NameObject("/Contents")]=writer._add_object(content)
    buf=io.BytesIO();writer.write(buf);return buf.getvalue()

def test_pdf_image_bytes_provenance_and_reviewed_assignment(tmp_path):
    data=pdf_bytes()
    m=save_project_pdf(tmp_path,"project.pdf",data)
    assert len(m["images"])==1 and m["unclassified_count"]==1
    art={"slides":[{"slide_id":"SL-001","layout":"modules","image_topic":"aerial"}]}
    assert not attach_project_images(tmp_path,art)["assignments"]
    m=relabel_project_pdf(tmp_path,[{"page":1,"image":1,"kind":"aerial","image_class":"规划效果图"}])
    assert m["unclassified_count"]==0
    audit=attach_project_images(tmp_path,art)
    assert len(audit["assignments"])==1
    assert verified_image(tmp_path,m["images"][0]["asset_id"]).is_file()
    assert "规划效果图" in art["slides"][0]["image_caption"]
    assert m["images"][0]["verified_current_site_photo"] is False

def test_path_or_asset_tampering_fails_closed(tmp_path):
    m=save_project_pdf(tmp_path,"project.pdf",pdf_bytes(),[{"page":1,"image":1,"kind":"aerial"}])
    path=tmp_path/m["images"][0]["path"];path.write_bytes(b"changed")
    with pytest.raises(ValueError):verified_image(tmp_path,m["images"][0]["asset_id"])

def test_generation_seals_project_inputs(tmp_path):
    (tmp_path/"generation_input.json").write_text("{}")
    with pytest.raises(ValueError):save_project_pdf(tmp_path,"project.pdf",pdf_bytes())

def test_unconfirmed_sla_cannot_be_authorized_by_arbitrary_trace():
    from longform.factory import evaluate_ppt_payload,PPT_REQUIRED_KEYS
    row={"profile_p_contract":VERSION,"layout_intent":"table","topic_id":"service_response","slide_role":"detail"}
    data={k:[] if k in {"content_blocks","source_trace","generation_notes"} else "" for k in PPT_REQUIRED_KEYS}
    data.update(headline="服务响应安排",key_message="向企业反馈",source_trace=[{"note":"已确认"}],
        visual_data={"layout_type":"table","table":{"columns":["事项","安排"],"rows":[["报修","10分钟响应"]]}},
        component_intent="table")
    gate=evaluate_ppt_payload(data,row,{})
    assert any(f["rule_id"]=="PPT-P-UNCONFIRMED-SLA" for f in gate["profile_p_findings"])

def test_conflicting_pdf_does_not_write_unbound_assets(tmp_path):
    original=pdf_bytes();m=save_project_pdf(tmp_path,"project.pdf",original)
    before=set((tmp_path/"project_assets").iterdir())
    with pytest.raises(ValueError):save_project_pdf(tmp_path,"other.pdf",original+b"\n")
    assert set((tmp_path/"project_assets").iterdir())==before
    assert json.loads((tmp_path/"project_assets/manifest.json").read_text())["source_sha256"]==m["source_sha256"]

def test_native_pipeline_metadata_survives_hygiene():
    from governance.artifact_qa import apply_artifact_repairs
    s={"profile_p_contract":VERSION,"slide_id":"SL-001","topic_id":"fitout","expected_steps":4,
       "image_topic":"research","source_trace":[{"page":5}],"layout":"modules","modules":[{"title":"专业复核","body":"说明使用条件。"}]}
    repaired=apply_artifact_repairs(s)
    for k in ("profile_p_contract","topic_id","expected_steps","image_topic","source_trace"):
        assert repaired[k]==s[k]

def test_ppt_body_bands_follow_scale_not_word_pages():
    from planning.planner import build_document_target
    word=build_document_target(target_scale="regular_solution")["word"]
    assert (word["target_pages_min"], word["target_pages_max"])==(50, 70)
    assert (build_document_target(target_scale="regular_solution")["ppt"]["target_slides_min"],
            build_document_target(target_scale="regular_solution")["ppt"]["target_slides_max"])==(15, 20)
    assert build_document_target(target_scale="detailed_technical_solution")["ppt"]["target_slides_max"]==40
    assert build_document_target(target_scale="large_technical_tender")["ppt"]["target_slides_min"]==50
    bare=pack(); bare["requirements"][0]["normalized_requirement"]="生成完整物业服务方案，供业主比较。"
    assert 15<=len(build_plan(bare,[],slide_band=(15,20))["slide_storyboard"])<=20
    assert 30<=len(build_plan(bare,[],slide_band=(30,40))["slide_storyboard"])<=40
    assert 50<=len(build_plan(bare,[],slide_band=(50,70))["slide_storyboard"])<=70

def test_user_page_minimum_is_preserved():
    p=pack();p["requirements"][0]["normalized_requirement"]="生成不少于60页的完整方案。"
    assert build_plan(p,[])["output_contract"]["min_total_pages"]==60

def test_explicit_meetings_exclusion_is_not_planned_as_a_deliverable():
    p=pack();p["requirements"][0]["normalized_requirement"]+="本方案不含会务服务。"
    plan=build_plan(p,[])
    assert "meetings" not in {s["topic_id"] for s in plan["slide_storyboard"]}
    menu=next(s for s in plan["slide_storyboard"] if s["topic_id"]=="enterprise_services")
    assert "会务" not in menu["core_message"]

def synthetic_ppt(path,image=True,text="完整标题业务动作",steps=()):
    import zipfile
    from PIL import Image
    out=io.BytesIO();Image.new("RGB",(100,80),(30,90,150)).save(out,format="PNG")
    a="http://schemas.openxmlformats.org/drawingml/2006/main"
    p="http://schemas.openxmlformats.org/presentationml/2006/main"
    r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with zipfile.ZipFile(path,"w") as z:
        ids="".join('<p:sldId id="'+str(i+255)+'" r:id="r'+str(i)+'"/>' for i in range(1,31))
        z.writestr("ppt/presentation.xml",'<p:presentation xmlns:p="'+p+'" xmlns:r="'+r+'"><p:sldIdLst>'+ids+'</p:sldIdLst></p:presentation>')
        z.writestr("ppt/_rels/presentation.xml.rels",'<Relationships>'+''.join('<Relationship Id="r'+str(i)+'" Target="slides/slide'+str(i)+'.xml"/>' for i in range(1,31))+'</Relationships>')
        if image:z.writestr("ppt/media/image.png",out.getvalue())
        for i in range(1,31):
            shapes='<p:sp><p:txBody><a:p><a:r><a:t>'+text+'</a:t></a:r></a:p></p:txBody></p:sp>'
            for n,t in enumerate(steps):
                shapes+='<p:sp><p:nvSpPr><p:cNvPr name="service-step-title-'+str(n)+'"/></p:nvSpPr><p:txBody><a:p><a:r><a:t>'+t+'</a:t></a:r></a:p></p:txBody></p:sp>'
            if image and i<=10:shapes+='<p:pic><p:blipFill><a:blip r:embed="img"/></p:blipFill></p:pic>'
            z.writestr("ppt/slides/slide"+str(i)+".xml",'<p:sld xmlns:p="'+p+'" xmlns:a="'+a+'" xmlns:r="'+r+'"><p:cSld><p:spTree>'+shapes+'</p:spTree></p:cSld></p:sld>')
            if image:z.writestr("ppt/slides/_rels/slide"+str(i)+'.xml.rels','<Relationships><Relationship Id="img" Target="../media/image.png"/></Relationships>')
    return out.getvalue()

def test_final_file_counts_real_pages_and_media(tmp_path):
    from ppt_final_verify import verify_pptx
    p=tmp_path/"test.pptx";synthetic_ppt(p)
    r=verify_pptx(p,{"slides":[{"title":"完整标题","layout":"modules","modules":[{"title":"","body":"业务动作"}]}]})
    assert r["physical_slide_count"]==30 and len(r["picture_pages"])==10
    assert r["status"]=="PASS" and r["actual_pptx_inspected"]
    assert r["visual_render_review"]=="PENDING"
    assert r["semantic_requirement_acceptance"]=="PENDING"

def test_final_file_missing_text_and_wrong_source_image_fail(tmp_path):
    from ppt_final_verify import verify_pptx
    from PIL import Image
    p=tmp_path/"test.pptx";synthetic_ppt(p)
    source=tmp_path/"source.png";Image.new("RGB",(80,100),"red").save(source)
    r=verify_pptx(p,{"slides":[{"title":"完整标题","layout":"modules","modules":[{"title":"","body":"业务动作"}],"image":str(source)},
        {"title":"不存在的完整页标题","layout":"modules"}]})
    ids={x["rule_id"] for x in r["findings"]}
    assert {"PPT-P-SOURCE-IMAGE","PPT-P-TEXT-LOSS"}<=ids

def test_final_process_step_order_is_measured_from_native_shapes(tmp_path):
    from ppt_final_verify import verify_pptx
    p=tmp_path/"test.pptx";synthetic_ppt(p,steps=["第二步","第一步"])
    r=verify_pptx(p,{"slides":[{"title":"完整标题","layout":"process","steps":[
        {"title":"第一步","body":"业务动作"},{"title":"第二步","body":"业务动作"}]}]})
    assert any(x["rule_id"]=="PPT-P-FINAL-STEPS" for x in r["findings"])

def test_invalid_export_is_not_a_success(tmp_path):
    from ppt_final_verify import verify_pptx
    p=tmp_path/"broken.pptx";p.write_bytes(b"not a pptx")
    r=verify_pptx(p,{})
    assert r["status"]=="BLOCK" and not r["actual_pptx_inspected"]

def test_ppt_page_and_final_authority_bound_to_landed_sha(tmp_path):
    import hashlib
    from longform.run_state import _page_band_facts,canonical_final_qa_authority
    (tmp_path/"artifact").mkdir();p=tmp_path/"artifact/test.pptx";synthetic_ppt(p)
    for name,obj in {"brief.json":{"medium":"PPT"},"artifact_content.json":{"artifact":{"profile_p_contract":VERSION}},
        "artifact_qa_report.json":{"status":"PASS"},
        "final_artifact_qa.json":{"status":"NEEDS_REVIEW","actual_pptx_inspected":True,"artifact_path":str(p),
            "artifact_sha256":hashlib.sha256(p.read_bytes()).hexdigest()}}.items():
        (tmp_path/name).write_text(json.dumps(obj),encoding="utf-8")
    assert _page_band_facts(tmp_path)["page_band_pages"]==30
    authority=canonical_final_qa_authority(tmp_path)
    assert authority["final_qa"]=="NEEDS_REVIEW"
    p.write_bytes(p.read_bytes()+b"\n")
    assert _page_band_facts(tmp_path)["page_band"]=="NOT_MEASURED"
    assert "SHA256_MISMATCH" in canonical_final_qa_authority(tmp_path)["final_qa_missing_reason"]

class TimeoutPageProvider:
    def __init__(self):
        self.config={"provider_type":"mock","provider_name":"mock","enabled":True,"model":"mock"}
        self.calls=[];self.late_probes=0
    def get_metadata(self):return {"model":"mock","test_mode":True}
    def invoke_structured(self,request,task_dir):
        from providers.base import ProviderError
        self.calls.append(copy.deepcopy(request))
        if request.get("slide_id")=="SL-001":raise ProviderError("injected timeout",error_code="TIMEOUT")
        return {"slide_id":request["slide_id"],"headline":"下一页内容","provider_metadata":self.get_metadata()}
    def collect_late_response(self,request,task_dir):
        self.late_probes+=1
        return {"slide_id":"SL-001"},{"status":"SUCCESS"}

def page_request(sid,attempt=1):
    return {"task_id":"PPT-"+sid+"-A"+str(attempt),"slide_id":sid,"generation_mode":"ppt_slide",
            "system_prompt":"返回本页JSON","prompt":"当前页"+sid,"required_keys":["slide_id","headline"]}

def test_page_timeout_does_not_block_an_unrelated_page(tmp_path):
    from tests.test_mmf006e_r21_q01_q04 import _minimal_factory
    provider=TimeoutPageProvider();factory=_minimal_factory(tmp_path,provider)
    first,a1=factory._invoke(page_request("SL-001"),tmp_path/"first")
    assert first is None
    second,a2=factory._invoke(page_request("SL-002"),tmp_path/"second")
    assert second["slide_id"]=="SL-002" and len(provider.calls)==2
    assert a1["request_fingerprint"]!=a2["request_fingerprint"]
    assert provider.late_probes==0
    assert all("json_schema" not in p for p in provider.calls)

def test_page_retry_preserves_unknown_request_guard(tmp_path):
    from tests.test_mmf006e_r21_q01_q04 import _minimal_factory
    provider=TimeoutPageProvider();factory=_minimal_factory(tmp_path,provider)
    factory._invoke(page_request("SL-001"),tmp_path/"first")
    provider.collect_late_response=lambda request,task_dir:None
    output,audit=factory._invoke(page_request("SL-001",2),tmp_path/"retry")
    assert output is None and audit["status"]=="UNKNOWN_REQUEST_REVIEW"
    assert len(provider.calls)==1

def test_late_response_cannot_cross_slide_identity(tmp_path):
    from tests.test_mmf006e_r21_q01_q04 import _minimal_factory
    provider=TimeoutPageProvider();factory=_minimal_factory(tmp_path,provider)
    assert factory._collect_late_response({"job_id":"old","slide_id":"SL-001"},page_request("SL-002"),tmp_path/"late") is None
    assert provider.late_probes==0

def test_wrong_returned_slide_is_blocked():
    payload=process_payload();payload["slide_id"]="SL-001"
    result=payload_issues(payload,{"slide_id":"SL-002","layout_intent":"process","expected_steps":3})
    assert any(f["rule_id"]=="PPT-P-SLIDE-ID" for f in result)


def test_first_ppt_prompt_and_retry_share_the_governance_budget():
    from longform.factory import LongformGenerationFactory,SLIDE_BUDGET
    # Use every role's existing authority, including the unknown-role fallback.
    for role,cap in [*SLIDE_BUDGET.items(),("unknown",220)]:
        context={"slide":{"slide_id":"SL-010","profile_p_contract":VERSION,"slide_role":role,"layout_intent":"table"}}
        for repairs in (None,["根据上次治理结果重新组织完整业务表达"]):
            request=LongformGenerationFactory._ppt_prompt(context,repairs)
            assert f"总预算为{cap}个文字单位" in request["prompt"]
            assert "全部步骤、对象、具体措施和关键实施条件" in request["prompt"]
            assert "不把关键条件移入speaker_note_optional或generation_notes" in request["prompt"]


def test_legacy_ppt_prompt_does_not_adopt_new_capacity_contract():
    from longform.factory import LongformGenerationFactory
    request=LongformGenerationFactory._ppt_prompt({"slide":{"slide_id":"OLD","slide_role":"detail","layout_intent":"table"}})
    assert "本页可见文字总预算" not in request["prompt"]





def test_outline_merges_repeated_themes_and_scopes_images():
    from planning.ppt_service import build_plan
    from project_assets import SCENE_TOPICS
    p=pack(); plan=build_plan(p,[])
    slides=plan["slide_storyboard"]
    assert len(plan["chapter_blocks"])==6
    assert len({s["topic_id"] for s in slides})==len(slides)
    process=[s["topic_id"] for s in slides if s["layout_intent"]=="process"]
    assert process==["repair_flow","faults"]
    assert all(s["chapter_id"]=="P01" for s in slides if s["image_topic"])
    scenes=[s["scene_topic"] for s in slides if s["scene_topic"]]
    assert len(scenes)==len(set(scenes))==13 and set(scenes)<=SCENE_TOPICS
    goals=[c["narrative_goal"] for c in plan["chapter_blocks"]]
    assert len(goals)==len(set(goals))


def test_cross_page_duplicate_body_and_title_are_retained_for_review():
    from planning.ppt_service import deck_issues
    shared="与企业确认使用范围和现场联系人后按约定时间联合查验。"
    art={"slides":[
        {"slide_id":"SL-001","title":"企业入驻服务","layout":"modules","modules":[{"title":"提前沟通","body":shared}]},
        {"slide_id":"SL-002","title":"企业入驻服务","layout":"modules","modules":[{"title":"联合交接","body":shared}]}]}
    findings,warnings=deck_issues(art)
    ids={f["rule_id"] for f in findings}
    assert {"PPT-P-DUPLICATE","PPT-P-DUPLICATE-TITLE"}<=ids
    assert all(f["severity"]=="NEEDS_REVIEW" for f in findings)


def test_single_shared_module_label_is_not_a_duplicate_group():
    from planning.ppt_service import deck_issues
    art={"slides":[
        {"slide_id":"SL-001","title":"企业接待","layout":"modules","modules":[
            {"title":"首次到访","body":"由服务人员接待并记录来访事项和企业联系人。"},
            {"title":"入驻咨询","body":"说明装修、搬运和通行条件及需要企业配合的事项。"}]},
        {"slide_id":"SL-002","title":"投诉分级","layout":"modules","modules":[
            {"title":"首次到访","body":"一般投诉由项目负责人当天联系企业确认处理安排。"},
            {"title":"重复投诉","body":"同一问题再次发生时报项目管理方共同复核处理。"}]}]}
    findings,_=deck_issues(art)
    assert not any(f["rule_id"]=="PPT-P-DUPLICATE-TITLE" for f in findings)


def test_generic_five_step_loop_requires_review_without_discarding_actions():
    from planning.ppt_service import deck_issues
    titles=["受理","派单","跟踪","反馈","回访"]
    art={"slides":[{"slide_id":"SL-001","title":"报修处理","layout":"process",
        "steps":[{"title":t,"body":"由服务人员按项目安排完成本步工作。"} for t in titles]}]}
    findings,_=deck_issues(art)
    assert any(f["rule_id"]=="PPT-P-PROCESS-CLICHE" for f in findings)
    assert all(f["severity"]=="NEEDS_REVIEW" for f in findings)


def _scene_png(color=(30,90,150)):
    import io
    from PIL import Image
    buf=io.BytesIO();Image.new("RGB",(800,600),color).save(buf,format="PNG");return buf.getvalue()


def test_service_scene_registration_labeled_binding_and_tamper(tmp_path):
    from project_assets import save_service_scene,attach_project_images,verified_scene
    manifest=save_service_scene(tmp_path,"reception",_scene_png(),caption="企业前台接待",
        generation={"tool":"Qoder ImageGen","prompt":"扁平商务插画，无文字","generated_at":"2026-10-23"})
    row=manifest["scenes"][0]
    assert row["authority"]=="AI_GENERATED_SERVICE_ILLUSTRATION" and row["verified_current_site_photo"] is False
    art={"slides":[{"slide_id":"SL-001","layout":"modules","scene_topic":"reception","modules":[{"title":"企业接待","body":"说明来访和入驻咨询的对接人。"}]},
                   {"slide_id":"SL-002","layout":"process","scene_topic":"reception","steps":[{"title":"提前沟通","body":"确认来访时间。"}]}]}
    audit=attach_project_images(tmp_path,art)
    assert audit["service_scenes"]["assignments"][0]["topic"]=="reception"
    slide=art["slides"][0]
    assert slide["image_source"]=="AI_GENERATED_SERVICE_ILLUSTRATION"
    assert "服务场景示意" in slide["image_caption"] and slide["image_side"]=="left"
    assert verified_scene(tmp_path,row["scene_id"]).is_file()
    path=tmp_path/row["path"];path.write_bytes(b"changed")
    with pytest.raises(ValueError):verified_scene(tmp_path,row["scene_id"])


def test_scene_registration_is_sealed_after_generation(tmp_path):
    from project_assets import save_service_scene
    (tmp_path/"generation_input.json").write_text("{}",encoding="utf-8")
    with pytest.raises(ValueError):save_service_scene(tmp_path,"reception",_scene_png(),caption="企业前台接待")


def test_prompt_states_cross_page_and_flow_limits():
    from planning.ppt_service import prompt
    text=prompt({"slide":{"slide_id":"SL-001","layout_intent":"modules","core_message":"企业接待"}})["prompt"]
    for phrase in ("一页只讲一个业务主题","跨页不得重复使用同一句正文","流程图只用于顺序或分工会改变结果的主题","项目建筑效果图只用于项目概况"):
        assert phrase in text
    assert "本页配服务场景示意图" not in text
    scene_text=prompt({"slide":{"slide_id":"SL-002","layout_intent":"modules","core_message":"企业接待","scene_topic":"reception"}})["prompt"]
    assert "本页配服务场景示意图" in scene_text and "content_blocks只写2—3项" in scene_text


def test_service_image_side_places_photo_and_caption_on_requested_side(tmp_path):
    import os,shutil,subprocess,zipfile
    import xml.etree.ElementTree as ET
    from PIL import Image
    node=shutil.which("node");assert node,"PPT renderer runtime is required for layout regression"
    photo=tmp_path/"scene.png";Image.new("RGB",(800,600),(30,90,150)).save(photo)
    script=tmp_path/"image_side.mjs";out=tmp_path/"image_side.pptx"
    data={"title":"企业接待","core_message":"说明来访和入驻咨询的对接人","layout":"modules",
        "modules":[{"title":"首次到访","body":"由服务人员接待并登记来访事项。"},{"title":"入驻咨询","body":"说明装修、搬运和通行条件。"}],
        "_image":str(photo),"image_width":800,"image_height":600,"image_caption":"服务场景示意 · 企业前台接待","image_side":"right",
        "profile_p_contract":VERSION}
    script.write_text('import {createRequire} from "node:module";import {pathToFileURL} from "node:url";\n'+
        'import {renderServiceSlide} from '+json.dumps((APP/'ppt_renderer_v2/components.mjs').as_uri())+';\n'+
        'const entry=createRequire(import.meta.url).resolve("@oai/artifact-tool",{paths:[process.env.RUNTIME_NODE_MODULES]});\n'+
        'const {Presentation,PresentationFile}=await import(pathToFileURL(entry).href);\n'+
        'const p=Presentation.create({slideSize:{width:1280,height:720}});\n'+
        'renderServiceSlide(p.slides.add(),'+json.dumps(data,ensure_ascii=False)+',1,1,{},"test");\n'+
        'await (await PresentationFile.exportPptx(p)).save('+json.dumps(str(out))+');\n',encoding='utf-8')
    env=os.environ.copy();env['RUNTIME_NODE_MODULES']=str(APP/'node_modules')
    result=subprocess.run([node,str(script)],env=env,capture_output=True,text=True,encoding='utf-8',timeout=60)
    assert result.returncode==0,result.stderr
    ns={"a":"http://schemas.openxmlformats.org/drawingml/2006/main","p":"http://schemas.openxmlformats.org/presentationml/2006/main"}
    with zipfile.ZipFile(out) as z:root=ET.fromstring(z.read("ppt/slides/slide1.xml"))
    photo_shape=None;caption=None
    pictures=root.findall(".//p:pic",ns)
    photo_shape=pictures[0] if pictures else None
    for sp in root.findall(".//p:sp",ns):
        name=sp.find("p:nvSpPr/p:cNvPr",ns).get("name")
        if name=="service-image-caption":caption="".join(t.text or "" for t in sp.findall(".//a:t",ns))
    assert photo_shape is not None and caption and "服务场景示意" in caption
    off=photo_shape.find("p:spPr/a:xfrm/a:off",ns)
    assert int(off.get("x"))>=700*9525
