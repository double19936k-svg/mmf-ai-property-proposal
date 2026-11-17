import hashlib,json,zipfile
from dataclasses import replace
from types import SimpleNamespace
import pytest
import paths
import app_core
import server
import account_delivery as d
import account_progress as p
from providers.base import write_json

@pytest.fixture
def run(tmp_path,monkeypatch):
    roots=replace(paths.current(),runs_dir=tmp_path/'runs',output_root=tmp_path/'out',runtime_dir=tmp_path/'runtime',logs_dir=tmp_path/'logs')
    monkeypatch.setattr(paths,'current',lambda:roots)
    r=roots.runs_dir/'r1';r.mkdir(parents=True)
    write_json(r/'brief.json',{'provider_name':'zcode_cn','medium':'WORD'})
    write_json(r/'generation_input.json',{})
    write_json(r/'generation_status.json',{'status':'running','message':'old heartbeat'})
    write_json(r/'run_audit.json',{'status':'technical_failed','delivery_status':'TECHNICAL_FAILED'})
    return roots,r

def docx(file):
    from docx import Document
    file.parent.mkdir(parents=True,exist_ok=True);doc=Document();doc.add_paragraph('Synthetic saved fragment');doc.save(file)


def test_cursor_progress_reads_completed_work_and_actual_usage(run):
    roots, folder = run
    provider = 'cursor_grok_4_7'
    slot = roots.runtime_dir/'cursor_account'/hashlib.sha256(b'r1').hexdigest()[:24]/'slot'
    write_json(slot/'dispatch.json', {'model':'grok-4.7','scope':'generation'})
    write_json(slot/'reply.json', {'state':'finished','observed_model':'grok-4.7','usage':{'totalTokens':120}})
    write_json(folder/'checkpoint/state.json', {'progress':{'done':2,'total':5}})
    result = p.snapshot('r1', [], generation={'provider_name':provider}, worker_alive=True)
    assert (result['state'],result['done'],result['total'],result['unit']) == ('running',2,5,'节正文')
    assert not result['indeterminate'] and result['usage']['total_tokens'] == 120
    write_json(folder/'generation_status.json', {'status':'running','phase':'DOCUMENT_QA'})
    qa = p.snapshot('r1', [], generation={'provider_name':provider}, worker_alive=True)
    assert qa['stage']=='检查与装配' and qa['indeterminate'] and qa['state']=='running'
    (folder/'generation_status.json').unlink()
    recognition = p.snapshot('r1', [], recognition={'provider_name':provider,'chunks_completed':1,'chunks_total':6},
                             worker_alive=True)
    assert recognition['stage']=='需求识别' and recognition['done']==1 and recognition['total']==6


def test_cursor_legacy_timeout_retains_reason_and_subchunk_progress(run):
    _, folder = run
    (folder/'generation_status.json').unlink()
    write_json(folder/'tender/chunks/CHK-0001/recognition_retry_audit.json', {'attempts':[
        {'error_code':'CURSOR_RESPONSE_UNKNOWN','error':'等待 Cursor 回执超时，原请求结果待核对。'},
        {'error_code':'CURSOR_RESPONSE_UNKNOWN','error':'Cursor 原请求结果待核对。'},
    ]})
    recognition = p.snapshot('r1', [], recognition={
        'provider_name':'cursor_grok_4_6', 'failed_chunk':'CHK-0001',
        'chunks_completed':0, 'chunks_total':6,
        'recognition_segments':{'completed':1,'total':9},
        'recognition_outcome':'AI_RECOGNITION_FAILED_WITH_LOCAL_FALLBACK',
    })
    assert recognition['state']=='needs_attention' and recognition['done']==0
    assert '等待 Cursor 回执超时' in recognition['message']
    assert '已校验1/9个子块' in recognition['message']
    assert recognition['recognition_controls']['accept_local']
    assert not recognition['recognition_controls']['recheck']


def test_recognition_progress_counts_children_and_keeps_plan_total_on_resume(run):
    _, folder = run
    (folder/'generation_status.json').unlink()
    provider = 'cursor_grok_4_7'
    status = {'provider_name':provider, 'chunks_total':2, 'chunks_completed':0,
              'recognition_segments':{'parent_chunk_id':'CHK-0001','completed':3,'total':9}}
    plan = {'provider':provider,'chunks':[{'chunk_id':'CHK-0001','requests':9},
                                        {'chunk_id':'CHK-0002','requests':7}]}
    write_json(folder/'tender/cursor_recognition_plan.json', plan)
    initial = p.snapshot('r1', [], recognition=status, worker_alive=True)
    assert initial['done'] == 0 and initial['total'] == 2
    assert initial['recognition_work'] == {'done':3,'total':16,'basis':'plan'}
    write_json(folder/'tender/cursor_recognition_plan.json', {**plan,'chunks':plan['chunks'][1:]})
    write_json(folder/'tender/chunks/CHK-0001/normalized.json', {
        'chunk_id':'CHK-0001','provider_metadata':{'provider_name':provider,
                                                'recognition_segments':{'segments_total':9}},
    })
    resumed = p.snapshot('r1', [], recognition={
        **status,'chunks_completed':1,
        'recognition_segments':{'parent_chunk_id':'CHK-0002','completed':2,'total':7},
    }, worker_alive=True)
    assert resumed['recognition_work'] == {'done':11,'total':16,'basis':'plan'}


def test_history_uses_official_tender_title_and_protects_active_recognition(run, monkeypatch):
    _, folder = run
    (folder/'generation_status.json').unlink()
    (folder/'brief.json').unlink()
    provider = 'cursor_grok_4_7'
    title = '办公楼物业服务采购项目'
    write_json(folder/'tender/extraction.json', {
        'paragraphs':[{'file_id':'f1','text':title},{'file_id':'f1','text':'招标文件'}],
    })
    write_json(folder/'tender/uploads.json', {'files':[{'original_filename':'附件-发售版.docx'}]})
    write_json(folder/'tender/status.json', {'provider_name':provider,'pack_status':'understanding'})
    row = app_core._incomplete_run_row(folder)
    assert row['project_name'] == title
    monkeypatch.setattr(server, 'TENDER_JOBS', {'r1':SimpleNamespace(is_alive=lambda:True)})
    active = server._enrich_run(row)
    assert active['tender_running'] and active['status_label'] == '正在识别需求'
    monkeypatch.setattr(server, 'delete_run', lambda *a,**kw: pytest.fail('active task must not be deleted'))
    for delete_files in (False, True):
        with pytest.raises(app_core.MMFError, match='正在识别需求'):
            server._delete_history_run('r1', delete_files=delete_files)

def test_cursor_confirmed_unsent_exposes_resume_instead_of_ambiguous_stop(run):
    roots, folder = run
    (folder/'generation_status.json').unlink()
    slot = roots.runtime_dir/'cursor_account'/hashlib.sha256(b'r1').hexdigest()[:24]/'slot'
    write_json(slot/'dispatch.json', {'model':'grok-4.7','scope':'recognition'})
    (slot/'bridge.jsonl').write_text('{"state":"error","error_code":"CURSOR_SDK_ERROR","at":1000}\n',
                                    encoding='utf-8')
    recognition = p.snapshot('r1', [], recognition={
        'provider_name':'cursor_grok_4_7', 'failed_chunk':'CHK-0002',
        'chunks_completed':1, 'chunks_total':6,
        'recognition_segments':{'completed':2,'total':7},
        'recognition_outcome':'AI_RECOGNITION_FAILED_WITH_LOCAL_FALLBACK',
    })
    assert recognition['done']==1 and '已校验2/7个子块' in recognition['message']
    assert recognition['recognition_controls']['retry_unsent']
    assert '未发送' in recognition['message']


def test_partial_exists_is_not_complete_and_download_keeps_subdirectory(run):
    roots,r=run;docx(r/'first_draft/FIRST_DRAFT.docx')
    row=d.project({'run_id':'r1','provider_name':'zcode_cn','generated':True,'download_url':'/files/r1/FIRST_DRAFT.docx'})
    assert not row['generated'] and row['artifact_available'] and row['run_status']=='generation_incomplete'
    assert row['download_url']=='/files/r1/first_draft/FIRST_DRAFT.docx'
    assert row['result']['status']=='generation_incomplete' and row['generation_job_status']=='failed'
    assert row['output_dir']==str(r/'first_draft')

def test_draft_export_is_byte_identical_idempotent_and_cannot_replace_old_export(run):
    roots,r=run;source=r/'first_draft/FIRST_DRAFT.docx';docx(source)
    first=d.publish_partial('r1');target=roots.output_root/'r1/PARTIAL_FIRST_DRAFT.docx'
    assert target.read_bytes()==source.read_bytes() and first['model_calls']==0
    assert d.publish_partial('r1')==first
    target.write_bytes(b'keep user file')
    second=d.publish_partial('r1');assert second['export']!=first['export'] and target.read_bytes()==b'keep user file'
    row=d.project({'run_id':'r1'});assert row['output_dir']==str(target.parent)

def test_missing_or_corrupt_file_never_offers_download(run):
    roots,r=run;(r/'first_draft').mkdir();(r/'first_draft/FIRST_DRAFT.docx').write_bytes(b'not office')
    row=d.project({'run_id':'r1','generated':True,'artifact_available':True,'download_url':'/bad'})
    assert not row['generated'] and not row['artifact_available'] and not row['download_url'] and not row['output_dir']

def test_live_worker_is_authority_over_child_reply_or_interim_qa(run):
    roots,r=run;docx(r/'first_draft/FIRST_DRAFT.docx')
    row=d.project({'run_id':'r1','result':{'status':'technical_failed','delivery_status':'TECHNICAL_FAILED'}},worker_alive=True)
    assert row['run_status']=='generation_in_progress' and row['result'] is None
    assert not row['generated'] and row['delivery_status']==''

def test_final_file_and_existing_delivery_authority_allow_completion(run):
    roots,r=run;docx(r/'final.docx')
    write_json(r/'run_audit.json',{'status':'generation_completed','delivery_status':'NEEDS_REVIEW'})
    row=d.project({'run_id':'r1'});assert row['generated'] and row['delivery_status']=='NEEDS_REVIEW'

def test_api_provider_projection_is_unchanged(run):
    roots,r=run;write_json(r/'brief.json',{'provider_name':'glm_zhipu'})
    row={'run_id':'r1','generated':True,'download_url':'/original'}
    assert d.project(row) is row and d.publish_partial('r1') is None

def task(i,state='content_ready',**kw):
    return {'key':str(i),'task_id':'r1-section-'+str(i),'provider':'zcode_cn','state':state,'label':state,**kw}

def test_many_child_notices_produce_one_bounded_workflow_snapshot(run):
    roots,r=run;write_json(r/'checkpoint/state.json',{'progress':{'done':6,'total':27}})
    tasks=[task(i,'needs_attention',error_code='ZCODE_RUN_BUDGET',message='budget exhausted') for i in range(29)]+[task(40)]
    g=p.snapshot('r1',tasks,d.project({'run_id':'r1'}),worker_alive=False)
    assert g['done']==6 and g['total']==27 and len(g['records'])==10 and g['record_count']==30
    assert isinstance(g['task'],dict) and g['state']=='needs_attention'

def test_important_questions_are_queued_without_completed_child_buttons(run):
    roots,r=run
    rows=[task(1,'waiting_for_user',nonce='n',question={'question':'Synthetic budget choice'})]+[task(i) for i in range(2,8)]
    g=p.snapshot('r1',rows,d.project({'run_id':'r1'}),worker_alive=True)
    assert g['pending_count']==1 and g['state']=='waiting_for_user' and g['task']['nonce']=='n'

def test_recognition_progress_counts_original_chunks_not_ready_content_cards(run):
    roots,r=run;(r/'generation_status.json').unlink()
    rows=[{**task(i),'task_id':f'r1-tender-{i}'} for i in range(3)]
    g=p.snapshot('r1',rows,recognition={'provider_name':'zcode_cn','chunks_completed':2,'chunks_total':6,'recognition_segments':{'current':3,'total':5}},worker_alive=True)
    assert g['done']==2 and g['total']==6 and g['task'] is None and '3/5' in g['message']

def test_hidden_run_has_no_progress_or_export(run):
    roots,r=run;write_json(r/'history_hidden.json',{'hidden':True});docx(r/'first_draft/FIRST_DRAFT.docx')
    assert p.snapshot('r1',[task(1)]) is None and d.publish_partial('r1') is None

def test_usage_comes_from_matched_returned_receipts_not_attempt_count(run):
    roots,r=run;jobs=roots.runtime_dir/'zcode_cn_account/jobs'/hashlib.sha256(b'r1').hexdigest()[:24]
    for i,scope in enumerate(['recognition','generation','generation']):
        write_json(jobs/f'{i}.json',{'status':'returned','digest':str(i),'budget_scope':scope})
        write_json(jobs/f'{i}.answer.json',{'digest':str(i),'transport':'official_standalone_cli','usage':{'totalTokens':100+i}})
    write_json(jobs/'unsent.json',{'status':'not_dispatched','digest':'no'})
    write_json(jobs/'bad.answer.json',{'digest':'wrong','usage':{'totalTokens':999999}})
    u=p.usage('r1','zcode_cn');assert u['returned_requests']==3 and u['recognition_requests']==1 and u['generation_requests']==2 and u['total_tokens']==303
    assert p.usage('r1','zcode_cn')==u

def test_absent_usage_is_unavailable_not_zero(run):
    roots,r=run
    assert p.usage('r1','zcode_cn')['total_tokens'] is None

def test_saved_reply_has_one_explicit_resume_control_when_workflow_stopped(run):
    roots,r=run
    rows=[task(i) for i in range(4)]
    g=p.snapshot('r1',rows,d.project({'run_id':'r1'}),worker_alive=False)
    assert g['task']['state']=='content_ready' and g['record_count']==4
    live=p.snapshot('r1',rows,d.project({'run_id':'r1'},worker_alive=True),worker_alive=True)
    assert live['task'] is None

def test_account_terminal_failure_cannot_be_overwritten_by_running_heartbeat(run,monkeypatch):
    import server
    roots,r=run;docx(r/'first_draft/FIRST_DRAFT.docx')
    monkeypatch.setattr(server,'generate_artifact',lambda *a,**k:{'run_id':'r1','status':'technical_failed','provider_name':'zcode_cn','delivery_status':'TECHNICAL_FAILED'})
    monkeypatch.setattr(server,'finalize_run',lambda rid,result:result)
    monkeypatch.setattr(server,'bind_run',lambda *a:None)
    monkeypatch.setattr(server,'unbind_run',lambda *a:None)
    monkeypatch.setattr(server,'finish_workflow',lambda *a:None)
    monkeypatch.setattr(server,'workflow_audit_fields',lambda *a:{})
    monkeypatch.setattr(server,'is_cancelled',lambda *a:False)
    server._run_generation_job('r1',[],{})
    record=d.read(r/'generation_status.json')
    assert record['status']=='failed' and record['result']['run_status']=='generation_incomplete'
    from generation_lifecycle import write_progress
    assert not write_progress(r/'generation_status.json',{'run_id':'r1','status':'running'},write_json)
    assert (roots.output_root/'r1/PARTIAL_FIRST_DRAFT.docx').is_file()

def test_matching_qa_hash_is_required_when_recorded(run):
    roots,r=run;docx(r/'final.docx');write_json(r/'run_audit.json',{'delivery_status':'COMPLETED_CLEAN','status':'generation_completed'})
    write_json(r/'final_artifact_qa.json',{'sha256':'stale-hash'})
    assert not d.project({'run_id':'r1'})['generated']

def test_qoder_usage_keeps_receipt_money_separate_from_account_quota(run, monkeypatch):
    import threading
    roots, _ = run
    jobs = roots.runtime_dir / "qoder_account_cn" / "jobs" / hashlib.sha256(b"r1").hexdigest()[:16]
    write_json(jobs / "a.json", {"status": "returned", "digest": "a", "budget_scope": "recognition"})
    write_json(jobs / "a.answer.json", {
        "digest": "a", "total_cost_usd": 0.02, "total_credits": 3,
        "usage": {"input_tokens": 10, "output_tokens": 4,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    })
    write_json(jobs / "b.json", {"status": "returned", "digest": "b", "budget_scope": "generation"})
    write_json(jobs / "b.answer.json", {"digest": "b", "usage": {}})

    class Quota:
        lock = threading.Lock()
        state = {"usage": {"totalUsagePercentage": 41, "isQuotaExceeded": False}}

    monkeypatch.setattr("qoder_account.connection.connection", lambda: Quota())
    shown = p.usage("r1", "qoder_account")
    assert shown["returned_requests"] == 2
    assert shown["cost"] == 0.02 and shown["cost_complete"] is False
    assert shown["total_credits"] == 3 and shown["credits_complete"] is False
    assert shown["account_usage_percent"] == 41 and shown["account_quota_exceeded"] is False
    empty = p.usage("nobody", "qoder_account")
    assert empty["returned_requests"] == 0 and empty["cost"] == "NOT_AVAILABLE" and empty["total_credits"] is None
    assert empty["account_usage_percent"] == 41


def test_open_folder_matches_partial_download_location(run,monkeypatch):
    from desktop_actions import open_run_folder
    import desktop_actions
    roots,r=run;docx(r/'first_draft/FIRST_DRAFT.docx');(roots.output_root/'r1').mkdir(parents=True)
    opened=[];monkeypatch.setattr(desktop_actions.subprocess,'Popen',lambda args,**kw:opened.append(args))
    result=open_run_folder('r1');assert result['path']==str((r/'first_draft').resolve())
    assert opened[0][1]==result['path']
