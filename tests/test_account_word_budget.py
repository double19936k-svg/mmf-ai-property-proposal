import json
from dataclasses import replace
import paths
from account_budget import word_cap
from providers.base import write_json

def test_new_large_word_budget_persists_and_legacy_does_not_expand(tmp_path,monkeypatch):
    roots=replace(paths.current(),runs_dir=tmp_path/'runs');monkeypatch.setattr(paths,'current',lambda:roots)
    write_json(roots.runs_dir/'new/brief.json',{'medium':'WORD','provider_name':'zcode_cn','document_scale':'large_technical_tender'})
    jobs=tmp_path/'jobs';assert word_cap(jobs,'new','Flash')==128
    write_json(jobs/'one.json',{'budget_scope':'generation','status':'returned'})
    assert word_cap(jobs,'new','Flash')==128
    old=tmp_path/'old';write_json(old/'one.json',{'budget_scope':'generation','status':'returned'})
    assert word_cap(old,'new','Flash')==8 and not (old/'.word_policy').exists()

def test_recognition_ledger_keeps_its_marker_when_the_writing_policy_is_created(tmp_path,monkeypatch):
    roots=replace(paths.current(),runs_dir=tmp_path/'runs');monkeypatch.setattr(paths,'current',lambda:roots)
    write_json(roots.runs_dir/'new/brief.json',{'medium':'WORD','provider_name':'qoder_account','document_scale':'detailed_technical_solution'})
    jobs=tmp_path/'jobs'
    marker=jobs/'recognition.json'
    write_json(marker,{'task_id':'new-tender-CHK-0002','generation_mode':'tender_requirement_recognition','status':'returned','budget_scope':'recognition'})
    assert word_cap(jobs,'new','qmodel_latest')==64
    saved=json.loads((jobs/'.word_policy').read_text(encoding='utf-8'))
    assert saved['call_cap']==64 and saved['run_id']=='new'
    assert json.loads(marker.read_text(encoding='utf-8'))['task_id']=='new-tender-CHK-0002'

def test_recognition_records_do_not_use_writing_budget(tmp_path,monkeypatch):
    roots=replace(paths.current(),runs_dir=tmp_path/'runs');monkeypatch.setattr(paths,'current',lambda:roots)
    write_json(roots.runs_dir/'new/brief.json',{'medium':'WORD','provider_name':'qoder_account','document_scale':'large_technical_tender'})
    jobs=tmp_path/'jobs';write_json(jobs/'one.json',{'generation_mode':'tender_requirement_recognition','status':'returned'})
    assert word_cap(jobs,'new','Flash')==128
