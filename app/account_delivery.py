"""Account-only presentation and publication of real files; never runs a model or QA."""
from __future__ import annotations
import hashlib,json,shutil,zipfile
from pathlib import Path
from urllib.parse import quote
import paths
from providers.cursor_account import is_cursor_provider

PROVIDERS={'qoder_account','zcode_cn'}

def read(path):
    try:
        value=json.loads(path.read_text(encoding='utf-8-sig'))
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):return {}

def account_run(run_id,row=None):
    row=row or {}
    provider=row.get('provider_name') or read(paths.current().runs_dir/run_id/'brief.json').get('provider_name')
    return provider in PROVIDERS or is_cursor_provider(provider)

def valid_file(file,roots):
    try:
        if not file.is_file() or not any(file.resolve().is_relative_to(p.resolve()) for p in roots):return False
        member={'.docx':'word/document.xml','.pptx':'ppt/presentation.xml'}.get(file.suffix.lower())
        if not member:return False
        with zipfile.ZipFile(file) as z:return {'[Content_Types].xml',member}<=set(z.namelist()) and z.testzip() is None
    except (OSError,ValueError,zipfile.BadZipFile):return False

def artifact(run_id):
    roots=paths.current();run=roots.runs_dir/run_id;out=roots.output_root/run_id
    from app_core import resolve_run_artifact
    file=resolve_run_artifact(run,read(run/'run_audit.json'))
    return file if file and valid_file(file,[run]) else None

def publish_partial(run_id):
    """Export preserved draft bytes without changing QA, budgets or delivery authority."""
    roots=paths.current();run=roots.runs_dir/run_id
    if not account_run(run_id) or (run/'history_hidden.json').exists():return None
    file=artifact(run_id)
    if not file:return None
    if not any(part in {'first_draft','last_valid_artifact','_blocked_artifact'} for part in file.relative_to(run).parts):return None
    dest=roots.output_root/run_id/('PARTIAL_'+file.name)
    dest.parent.mkdir(parents=True,exist_ok=True)
    if dest.exists() and dest.read_bytes()!=file.read_bytes():
        # Never overwrite an earlier preserved export.
        dest=dest.with_stem(dest.stem+'_'+hashlib.sha256(file.read_bytes()).hexdigest()[:10])
    if not dest.exists():shutil.copy2(file,dest)
    write={'source':file.relative_to(run).as_posix(),'export':str(dest),'sha256':hashlib.sha256(file.read_bytes()).hexdigest(),'kind':'PARTIAL','model_calls':0}
    from providers.base import write_json
    write_json(run/'account_partial_export.json',write)
    return write

def project(row,worker_alive=False):
    """Shared status/history/result projection. File existence never means full success."""
    rid=row.get('run_id','')
    if not account_run(rid,row):return row
    roots=paths.current();run=roots.runs_dir/rid
    record=read(run/'generation_status.json');audit=read(run/'run_audit.json')
    if not record and not (run/'generation_input.json').exists():return row
    payload={**row,**(row.get('result') or {})};file=artifact(rid)
    if audit.get('local_review_repair'):
        payload.setdefault('local_review_repair',audit['local_review_repair'])
    elif (run/'local_review_repair.json').is_file():
        payload.setdefault('local_review_repair', read(run/'local_review_repair.json'))
    delivery=audit.get('delivery_status') or payload.get('delivery_status') or ''
    pc=payload.get('ppt_completion') or {}
    draft=bool(file and any(p in {'first_draft','last_valid_artifact','_blocked_artifact'} for p in file.relative_to(run).parts))
    complete=bool(file and not draft and not pc.get('complete') is False and delivery in {'COMPLETED_CLEAN','COMPLETED_WITH_WARNINGS','NEEDS_REVIEW'} and audit.get('status')!='technical_failed')
    qa=read(run/'final_artifact_qa.json')
    expected=payload.get('artifact_sha256') or qa.get('sha256') or (qa.get('final_pptx') or {}).get('artifact_sha256')
    if complete and expected and hashlib.sha256(file.read_bytes()).hexdigest()!=expected:complete=False
    running=bool(worker_alive)
    available=bool(file)
    url=f'/files/{quote(rid)}/{quote(file.relative_to(run).as_posix(),safe="/")}' if file else ''
    output=read(run/'account_partial_export.json').get('export')
    exported=Path(output) if output else None
    shown_dir=(exported.parent if exported and valid_file(exported,[roots.output_root/rid]) and file and exported.read_bytes()==file.read_bytes() else file.parent if file else None)
    message=('正在生成方案，已保存的分块继续用于原流程。' if running else
             ('方案已生成，部分内容待复核。' if delivery=='NEEDS_REVIEW' else '方案已生成。') if complete else
             '生成未完成，已保存可下载的部分稿。' if available else '生成未完成，尚无可下载文件。')
    fields={            'provider_name':row.get('provider_name') or read(run/'brief.json').get('provider_name'),
            'account_delivery':True,'artifact_available':available,'generated':complete and not running,
            'scope_conflicts': read(run/'requirement_pack.json').get('scope_conflicts') or [],
            'run_status':'generation_in_progress' if running else 'generation_completed' if complete else 'generation_incomplete',
            'status':'running' if running else 'generation_completed' if complete else 'generation_incomplete',
            'generation_job_status':'running' if running else 'completed' if complete else 'failed',
            'delivery_status':'' if running else delivery or 'TECHNICAL_FAILED',
            'artifact_name':file.name if file else '', 'download_url':url,
            'partial_artifact_kind':'PARTIAL' if available and not complete else '',
            'output_dir':str(shown_dir) if shown_dir else '',
            'word_path':str(file) if file and file.suffix.lower()=='.docx' else '',
            'ppt_path':str(file) if file and file.suffix.lower()=='.pptx' else '',
            'public_message':message,'message':message,'status_label':'正在生成' if running else '生成完成' if complete else '未完成（部分稿已保留）' if available else '未完成（无文件）',
            'worker_alive':running,'can_check_status':running,'can_resume_recommendation':False if not complete else row.get('can_resume_recommendation',False)}
    if not running and not complete and is_cursor_provider(fields['provider_name']):
        from cursor_word_recovery import inspect, public_issue
        recovery = inspect(run)
        if recovery:
            fields.update(execution_issues=[{'label': public_issue(recovery)}],
                          can_resume_cursor_word=recovery['can_resume'])
    result={**payload,**fields};result.pop('result',None)
    return {**row,**fields,'result':None if running else result}
