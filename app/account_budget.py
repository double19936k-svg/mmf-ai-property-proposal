"""Persist a bounded writing budget for new account Word runs only."""
import json
from pathlib import Path
import paths
from providers.base import ProviderError,write_json
from providers.cursor_account import is_cursor_provider

WORD_CALL_CAPS = {'regular_solution': 32, 'detailed_technical_solution': 64, 'large_technical_tender': 128}

def word_cap(jobs,run_id,model):
    policy_path=jobs/'.word_policy'
    if policy_path.exists():
        row=json.loads(policy_path.read_text(encoding='utf-8'))
        if row['run_id']!=run_id or row['model']!=model:raise ProviderError('写稿预算与原模型不一致。',error_code='ACCOUNT_BUDGET_IDENTITY')
        return row['call_cap']
    run=paths.current().runs_dir/run_id;brief_path=run/'brief.json'
    if not brief_path.is_file():return 8
    brief=json.loads(brief_path.read_text(encoding='utf-8-sig'))
    provider = brief.get('provider_name')
    if brief.get('medium')!='WORD' or not (provider in {'zcode_cn','qoder_account'} or is_cursor_provider(provider)):return 8
    rows=[]
    for marker in jobs.glob('*.json'):
        # The model cache lives beside the ledger. It has no task id, and it
        # must not collapse a new Word run back to the legacy eight-call cap.
        if marker.name.startswith('model-') or marker.name.endswith('.answer.json') or marker.name=='catalog.json':
            continue
        row=json.loads(marker.read_text(encoding='utf-8'))
        if isinstance(row, dict) and (row.get('task_id') or row.get('budget_scope')=='generation'):
            rows.append(row)
    def writing(r):
        if r.get('budget_scope')=='recommendation' or r.get('generation_mode')=='knowledge_recommendation' or str(r.get('task_id') or '').endswith('-recommend'):
            return False
        return r.get('budget_scope')=='generation' or (r.get('budget_scope') is None and r.get('generation_mode')!='tender_requirement_recognition' and '-tender-' not in str(r.get('task_id','')))
    if any(writing(r) and r.get('status')!='not_dispatched' for r in rows):return 8
    cap=WORD_CALL_CAPS.get(brief.get('document_scale'),WORD_CALL_CAPS['regular_solution'])
    jobs.mkdir(parents=True,exist_ok=True)
    write_json(policy_path,{'version':1,'run_id':run_id,'model':model,'call_cap':cap,'document_scale':brief.get('document_scale'),'scope':'writing_including_repairs','legacy_expanded':False})
    return cap
