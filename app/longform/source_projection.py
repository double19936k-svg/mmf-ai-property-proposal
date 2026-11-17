"""Recover only evidenced staffing literals erased by the legacy cleaner."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from governance.final_editorial import NUMERIC, SourceFacts
from governance.table_context import is_business_header
from governance.table_fidelity import digest
from .cu_assembly import apply_content_units
from .hierarchical import atomic_write_json

STAFF_REDACTION = '按现场条件配置的人员'
DISPLAY_FIELDS = ('body_blocks', 'tables', 'processes', 'callouts')


def restore_staff_projection(fragment, expected, facts, *, source_hashes):
    """Neither accept a changed number nor alter a CU/source contract.

    An old display value must equal the current source-backed projection with
    *only* verified staffing tokens replaced by the known cleaner placeholder.
    Any additional edit, mismatched CU identity, changed contract or unverified
    staffing claim stays untouched for the existing QA/fidelity gates.
    """
    actions = []
    def walk(actual, wanted, path, cu=None):
        if isinstance(actual, dict) and isinstance(wanted, dict):
            a_id=actual.get('source_content_unit_id')
            w_id=wanted.get('source_content_unit_id')
            if a_id is not None or w_id is not None:
                if a_id!=w_id or a_id not in source_hashes:return deepcopy(actual)
                cu=a_id
            if 'table_source_contract' in actual or 'table_source_contract' in wanted:
                if actual.get('table_source_contract')!=wanted.get('table_source_contract'):
                    return deepcopy(actual)
            result={key:(deepcopy(value) if key=='table_source_contract' or key not in wanted
                        else walk(value,wanted[key],path+'.'+str(key),cu)) for key,value in actual.items()}
            contract=actual.get('table_source_contract') or {}
            raw=contract.get('source_values')
            if cu and raw and digest(raw)==contract.get('source_values_digest'):
                for key in ('columns','headers'):
                    old=actual.get(key);new=wanted.get(key)
                    if not isinstance(old,list) or not isinstance(new,list) or len(old)!=len(new):continue
                    for i,(lost,label) in enumerate(zip(old,new)):
                        if (not str(lost).strip() and i<len(raw[0]) and label==raw[0][i]
                                and is_business_header(label)):
                            result[key][i]=label
                            actions.append({'path':f'{path}.{key}[{i}]','content_unit_id':cu,
                                'source_cu_sha256':source_hashes[cu],'source_contract_digest':contract['source_values_digest'],
                                'before':lost,'after':label,'reason':'RESTORE_SOURCE_BUSINESS_HEADER_ERASED_BY_PROSE_CLEANER'})
            return result
        if isinstance(actual,list) and isinstance(wanted,list) and len(actual)==len(wanted):
            return [walk(a,b,f'{path}[{i}]',cu) for i,(a,b) in enumerate(zip(actual,wanted))]
        if isinstance(actual,str) and isinstance(wanted,str) and cu and STAFF_REDACTION in actual:
            pieces=[];bindings=[];cursor=0
            for match in NUMERIC.finditer(wanted):
                if not re.search(r'[人名]$',match.group()):continue
                binding=facts.numeric_binding(wanted,*match.span())
                if binding:
                    pieces.append(re.escape(wanted[cursor:match.start()]))
                    pieces.append(f'(?P<staff_{len(bindings)}>'+re.escape(match.group())+'|'+re.escape(STAFF_REDACTION)+')')
                    cursor=match.end()
                    bindings.append(binding)
            pieces.append(re.escape(wanted[cursor:]))
            matched=re.fullmatch(''.join(pieces),actual,re.S) if bindings else None
            restored=[binding for i,binding in enumerate(bindings) if matched and matched.group(f'staff_{i}')==STAFF_REDACTION]
            if restored and wanted!=actual:
                actions.append({'path':path,'content_unit_id':cu,'source_cu_sha256':source_hashes[cu],
                                'before':actual,'after':wanted,'bindings':restored,
                                'reason':'RESTORE_VERIFIED_STAFF_LITERAL_ERASED_BY_LEGACY_CLEANER'})
                return wanted
        return deepcopy(actual)
    result=deepcopy(fragment)
    for field in DISPLAY_FIELDS:
        if field in fragment and field in expected:
            result[field]=walk(fragment[field],expected[field],field)
    return result,actions


def restore_cached_staffing(run_dir, section_ids, text_pipeline):
    """Bounded, provider-free recovery before reusing section/volume caches."""
    root=Path(run_dir);facts=SourceFacts.from_run(root)
    report={'schema_version':'source-staff-projection-recovery-v1','provider_calls':0,
            'sections_changed':[],'restored_fields':0,'sections':{}}
    report['verified_staff_source_available']=bool(facts.staff_source)
    sections_root=root/'longform/word/sections'
    selected=section_ids if section_ids is not None else [p.name for p in sections_root.iterdir() if p.is_dir()] if sections_root.is_dir() else []
    for sid in selected:
        unit=root/'longform/word/sections'/sid;path=unit/'fragment.json'
        if not path.is_file():continue
        before=path.read_bytes();fragment=json.loads(before.decode('utf-8-sig'))
        lost_header=any(not str(label).strip() for table in fragment.get('tables') or []
                        for label in table.get('columns',table.get('headers',[])))
        if not lost_header and not any(STAFF_REDACTION in json.dumps(fragment.get(k),ensure_ascii=False) for k in DISPLAY_FIELDS):continue
        rows=[];hashes={}
        for cu_path in sorted((unit/'cus').glob('CU-*.json')):
            raw=cu_path.read_bytes();row=json.loads(raw.decode('utf-8-sig'))
            if row.get('generation_origin','provider_content_unit')!='provider_content_unit':continue
            cu=str(row.get('content_unit_id') or '')
            if cu!=cu_path.stem or row.get('parent_section_id',sid)!=sid:continue
            rows.append(row);hashes[cu]=hashlib.sha256(raw).hexdigest()
        if not rows:continue
        expected=apply_content_units(rows,base_fragment=fragment,section_id=sid,
                                     planned_ids=[row['content_unit_id'] for row in rows])
        expected=text_pipeline(expected)
        fixed,actions=restore_staff_projection(fragment,expected,facts,source_hashes=hashes)
        if not actions:continue
        atomic_write_json(path,fixed)
        summary_path=unit/'summary.json'
        if summary_path.is_file():
            from .factory import visible_text
            summary=json.loads(summary_path.read_text(encoding='utf-8-sig'))
            summary['core_points']=re.sub(r'\s+','',visible_text(fixed))[:180]
            atomic_write_json(summary_path,summary)
        report['sections_changed'].append(sid);report['restored_fields']+=len(actions)
        report['sections'][sid]={'before_fragment_sha256':hashlib.sha256(before).hexdigest(),
                                'after_fragment_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                                'actions':actions,'source_cus_unchanged':all(hashlib.sha256((unit/'cus'/f'{cu}.json').read_bytes()).hexdigest()==h for cu,h in hashes.items())}
    report['status']='RECOVERED' if report['sections_changed'] else 'NO_CHANGE'
    if report['sections_changed']:
        target=root/'longform/word/source_staff_projection_recovery.json'
        # Preserve prior receipts when a subsequent resume has no work left.
        if target.is_file():
            prior=json.loads(target.read_text(encoding='utf-8-sig'))
            report['prior_receipts']=prior.get('prior_receipts',[])+[{k:v for k,v in prior.items() if k!='prior_receipts'}]
        atomic_write_json(target,report)
    return report
