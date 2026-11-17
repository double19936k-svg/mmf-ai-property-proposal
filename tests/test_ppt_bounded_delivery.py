from __future__ import annotations
import copy,json,sys,threading,time
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))
from planning.ppt_delivery import execution_budget,generation_seconds_left,estimated_seconds
from planning.ppt_service import VERSION,render_data,prompt
from longform.factory import LongformGenerationFactory,LongformGenerationJob,PPT_REQUIRED_KEYS
from providers.base import write_json,ProviderError
from providers.qoder_internal import response_wait_seconds

def factory(tmp_path,count=4,workers=2,invoke=None):
    f=LongformGenerationFactory.__new__(LongformGenerationFactory)
    f.ppt_root=tmp_path/"longform/ppt";f.ppt_root.mkdir(parents=True)
    slides=[{"slide_id":f"SL-{i:03d}","slide_title":f"服务主题{i}","chapter_id":"P01","layout_intent":"modules","slide_role":"detail","profile_p_contract":VERSION} for i in range(1,count+1)]
    f.inputs={"global_state":{},"ppt_plan":{"slide_storyboard":slides,"chapter_blocks":[{"chapter_id":"P01"}],"execution_policy":{"profile":"bounded_90min_v1"}}}
    f.max_parallelism=workers;f.provider_name="mock"
    f.checkpoint_lock=threading.RLock()
    f.builder=SimpleNamespace(ppt=lambda slide,chapter:{"slide":slide})
    f._job=lambda *args:LongformGenerationJob("job","run","mock","PPT","v1","req","global",total_units=count)
    f._settle_request=lambda *args,**kwargs:None
    f._invoke=invoke or (lambda request,unit:(payload(request["slide_id"]),{"status":"SUCCESS"}))
    return f

def payload(sid):
    p={k:[] if k in {"content_blocks","source_trace","generation_notes"} else "" for k in PPT_REQUIRED_KEYS}
    p.update(slide_id=sid,headline="企业服务安排",key_message="说明办理方式。",component_intent="modules",visual_data={"layout_type":"modules"},
        content_blocks=[{"title":"来访接待","description":"核对来访需求并引导至企业。"},{"title":"结果告知","description":"向企业联系人说明处理结果。"}])
    return p

def test_two_workers_complete_56_body_pages_and_resume_without_extra_calls(tmp_path):
    lock=threading.Lock();active=0;peak=0;calls=[];barrier=threading.Barrier(2)
    def invoke(req,unit):
        nonlocal active,peak
        with lock:active+=1;peak=max(peak,active);calls.append(req["slide_id"])
        if len(calls)<=2:barrier.wait(timeout=2)
        time.sleep(.002)
        with lock:active-=1
        return payload(req["slide_id"]),{"status":"SUCCESS"}
    f=factory(tmp_path,56,2,invoke)
    result=f.generate_ppt();assert result["status"]=="COMPLETED"
    assert len(calls)==56 and peak==2 and result["completed_slides"]==56
    assert f.generate_ppt()["status"]=="COMPLETED" and len(calls)==56
    statuses=json.loads((f.ppt_root/"status.json").read_text(encoding="utf-8"))
    assert statuses["completed_units"]==[f"SL-{i:03d}" for i in range(1,57)]

def test_single_worker_option_is_respected(tmp_path):
    f=factory(tmp_path,3,1)
    assert f.generate_ppt()["status"]=="COMPLETED"
    assert json.loads((f.ppt_root/"execution_observation.json").read_text(encoding="utf-8"))["workers"]==1

def test_worker_directories_exist_before_concurrent_writes(tmp_path,monkeypatch):
    import longform.factory as module
    original=module.write_json
    def check(path,data):
        if threading.current_thread().name.startswith("ppt-page"):
            assert Path(path).parent.is_dir(),"shared directory chain must be created before worker writes"
        return original(path,data)
    monkeypatch.setattr(module,"write_json",check)
    assert factory(tmp_path,8,2).generate_ppt()["status"]=="COMPLETED"

def test_retry_is_local_and_does_not_rewrite_good_pages(tmp_path):
    calls=[]
    def invoke(req,unit):
        sid=req["slide_id"];calls.append(sid);p=payload(sid)
        if sid=="SL-002" and calls.count(sid)==1:p["content_blocks"][0]["description"]=""
        return p,{"status":"SUCCESS"}
    f=factory(tmp_path,3,2,invoke)
    assert f.generate_ppt()["status"]=="COMPLETED"
    assert calls.count("SL-001")==calls.count("SL-003")==1 and calls.count("SL-002")==2

def test_bad_page_is_still_blocked_after_two_attempts(tmp_path):
    calls=[]
    def invoke(req,unit):
        calls.append(req["slide_id"]);p=payload(req["slide_id"]);p["content_blocks"]=[]
        return p,{"status":"SUCCESS"}
    f=factory(tmp_path,1,2,invoke)
    assert f.generate_ppt()["status"]=="COMPLETED_WITH_GAPS" and len(calls)==2
    assert not (f.ppt_root/"slides/SL-001/payload.json").exists()

def test_deck_retry_budget_is_shared_and_persisted(tmp_path):
    calls=[]
    def invoke(req,unit):
        calls.append(req["slide_id"]);p=payload(req["slide_id"]);p["content_blocks"]=[]
        return p,{"status":"SUCCESS"}
    f=factory(tmp_path,10,2,invoke)
    assert f.generate_ppt()["status"]!="COMPLETED"
    assert len(calls)==16
    assert f.generate_ppt()["status"]!="COMPLETED" and len(calls)==16
    assert execution_budget(f.ppt_root)["page_requests_started"]==16

def test_budget_expiry_preserves_pending_pages_without_calls(tmp_path):
    f=factory(tmp_path,3)
    old=execution_budget(f.ppt_root,started_epoch=time.time()-4300)
    def forbidden(*a):raise AssertionError("must not dispatch after generation cutoff")
    f._invoke=forbidden
    assert f.generate_ppt()["status"]=="DEFERRED_EXTERNAL"
    assert execution_budget(f.ppt_root,started_epoch=time.time())["deadline_epoch"]==old["deadline_epoch"]
    assert generation_seconds_left(old)==0

def test_physical_page_cap_applies_before_model_calls(tmp_path):
    f=factory(tmp_path,78)
    with pytest.raises(ValueError,match="80 physical"):f.generate_ppt()

def test_provider_wait_obeys_ppt_deadline_and_leaves_word_unchanged():
    assert response_wait_seconds({"generation_mode":"ppt_slide","response_deadline_epoch":140},1800,now=100)==40
    assert response_wait_seconds({"generation_mode":"word","response_deadline_epoch":140},1800,now=100)==1800
    with pytest.raises(ProviderError):response_wait_seconds({"generation_mode":"ppt_slide","response_deadline_epoch":100},1800,now=100)

def test_capacity_estimate_is_explicit_and_not_real_sla_claim():
    assert estimated_seconds(51,120,workers=2)==4980
    assert estimated_seconds(51,150,workers=2)>5400

def test_generation_cutoff_cannot_be_extended_on_resume(tmp_path):
    budget=execution_budget(tmp_path,started_epoch=100)
    budget["generation_deadline_epoch"]+=60
    write_json(tmp_path/"execution_budget.json",budget)
    with pytest.raises(ValueError,match="altered"):execution_budget(tmp_path)

def test_clock_preflight_keeps_original_start_and_reports_capacity(tmp_path,monkeypatch,capsys):
    import importlib.util
    path=Path(__file__).resolve().parents[1]/"tools/ppt_delivery_preflight.py"
    spec=importlib.util.spec_from_file_location("ppt_preflight",path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr(sys,"argv",[str(path),"--run-root",str(tmp_path),"--body-pages","51","--chapters","6","--page-p90-seconds","120"])
    assert module.main()==0
    first=json.loads(capsys.readouterr().out)
    assert module.main()==0
    second=json.loads(capsys.readouterr().out)
    assert first["started_epoch"]==second["started_epoch"] and second["capacity_estimate"]=="WITHIN_90MIN"

def test_known_typos_are_fixed_without_mutating_raw_or_removing_conditions():
    p=payload("SL-001");p["content_blocks"][0]["description"]="共用部位按界面分承担，未复核不安装。缺项成清单。多组来访集中到时分流。"
    before=copy.deepcopy(p);d=render_data(p,{"layout_intent":"modules"})
    assert p==before
    text=d["modules"][0]["body"]
    assert "分别承担" in text and "整理成清单" in text and "集中到访时" in text and "未复核不安装" in text

def test_short_caption_keeps_original_source_metadata():
    from project_assets import display_caption
    entry={"kind":"layout_map","caption":"园区功能分区示意图 · 项目简介第2页","source_page":2}
    old=copy.deepcopy(entry);label=display_caption(entry)
    assert "第2页" not in label and "示意图" in label and "效果图" not in label and entry==old

def test_planner_adds_two_nonduplicate_topics_and_keeps_aerial_only_for_cover():
    from test_ppt_service_contract import pack
    from planning.ppt_service import build_plan
    plan=build_plan(pack(),[]);topics={s["topic_id"]:s for s in plan["slide_storyboard"]}
    assert {"customer_groups","cost_procurement"}<=topics.keys()
    assert topics["project_overview"]["image_topic"] is None
    assert plan["execution_policy"]["parallelism"]==2

def test_writer_uses_soft_density_without_authorizing_unconfirmed_sla():
    result=prompt({"slide":{"slide_id":"SL-001","layout_intent":"modules"}},text_budget=260)["prompt"]
    assert "150—190" in result and "软目标" in result and "不默认添加10分钟" in result
    assert "层高不是卸货净高" in result and "不代作结论" in result
