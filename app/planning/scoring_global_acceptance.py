"""Whole-document service plan acceptance, independently of literal scoring wording."""
from __future__ import annotations
import re

SERVICE_DOMAINS = {
    'engineering': ('工程', '设备', '设施', '维修', '配电'),
    'security': ('秩序', '巡视', '消防', '出入', '安全防范'),
    'environment': ('保洁', '清洁', '卫生', '垃圾'),
    'customer_service': ('前台', '客户服务', '投诉', '受理'),
    'staffing': ('人员', '岗位', '编制', '培训'),
    'handover': ('交接', '移交', '进场', '退出'),
    'catering': ('食堂', '餐饮', '后厨', '三餐'),
}


def build_global_plan(pack, bundle):
    matrix = {r.get('requirement_id'): r for r in (bundle.get('requirement_matrix') or {}).get('matrix') or []}
    domains = {}
    for req in pack.get('requirements') or []:
        sources = [s for s in req.get('sources') or [] if any('技术需求' in h for h in s.get('heading_path') or [])]
        if not sources or req.get('excluded_from_solution_body'):
            continue
        text = ''.join(str(s.get('source_excerpt') or '') for s in sources)
        domain = str(req.get('domain') or '')
        kinds = [domain] if domain in SERVICE_DOMAINS else []
        if re.search('食堂|三餐|后厨', text):
            kinds.append('catering')
        for kind in set(kinds):
            entry = domains.setdefault(kind, {'domain': kind, 'terms': list(SERVICE_DOMAINS[kind]),
                                               'requirements': [], 'planned_sections': []})
            entry['requirements'].append({'requirement_id': req.get('requirement_id'), 'sources': sources})
            sid = (matrix.get(req.get('requirement_id')) or {}).get('primary_section_id')
            if sid and sid not in entry['planned_sections']:
                entry['planned_sections'].append(sid)
    sections = [s['section_id'] for chapter in (bundle.get('word_plan') or {}).get('outline') or []
                for s in chapter.get('sections') or [] if s.get('section_id')]
    # Source-backed service nouns establish project context; no generated identity facts.
    project_terms = []
    for req in pack.get('requirements') or []:
        for source in req.get('sources') or []:
            if not any('技术需求' in h for h in source.get('heading_path') or []):
                continue
            text = str(source.get('source_excerpt') or '')
            project_terms.extend(t for t in ('办公楼', '综合服务楼', '职工食堂') if t in text)
    return {'acceptance_object': 'WHOLE_DOCUMENT', 'service_domains': list(domains.values()),
            'required_sections': sections, 'source_project_terms': sorted(set(project_terms)),
            'basis': 'TECHNICAL_REQUIREMENT_SOURCE_AND_DOCUMENT_PLAN'}


def judge_global_service_plan(plan, unit_index):
    candidates, selected, missing = [], [], []
    for domain in plan.get('service_domains') or []:
        valid = []
        for sid, units in unit_index.items():
            for unit in units:
                text = unit['text']
                hits = [t for t in domain['terms'] if t in text]
                if not hits:
                    continue
                dimensions = {'domain_response': bool(hits),
                              'responsibility': bool(re.search('负责|岗位|主管|牵头', text)),
                              'operation': bool(re.search('执行|核查|检查|处置|作业|交底|清理|维修|培训', text)),
                              'records_and_control': bool(re.search('记录|台账|签认|归档|复核|清单', text))}
                in_plan = sid in domain['planned_sections']
                okay = unit['substantive'] and all(dimensions.values()) and in_plan
                c = {'domain': domain['domain'], 'section_id': sid,
                     'content_unit_id': unit['assembled_content_unit_id'],
                     'artifact_location': unit['artifact_location'], 'dimensions': dimensions,
                     'source_requirements': domain['requirements'], 'in_domain_plan': in_plan,
                     'evidence_text': text[:500], 'valid': okay, 'selected': False,
                     'rejected_reasons': [] if okay else ['DOMAIN_RESPONSE_INCOMPLETE_OR_WRONG_SCOPE']}
                candidates.append(c)
                if okay:
                    valid.append(c)
        if valid:
            valid.sort(key=lambda c: (c['artifact_location'], c['content_unit_id']))
            valid[0]['selected'] = True
            selected.append(valid[0])
        else:
            missing.append('SERVICE_DOMAIN:' + domain['domain'])
    empty_sections = [sid for sid in plan.get('required_sections') or []
                      if not any(u['substantive'] for u in unit_index.get(sid) or [])]
    missing.extend('EMPTY_SECTION:' + sid for sid in empty_sections)
    all_text = ''.join(u['text'] for units in unit_index.values() for u in units)
    context_missing = [t for t in plan.get('source_project_terms') or [] if t not in all_text]
    missing.extend('PROJECT_CONTEXT:' + t for t in context_missing)
    if not plan.get('service_domains') or not plan.get('required_sections'):
        missing.append('SOURCE_DERIVED_GLOBAL_ACCEPTANCE_PLAN')
    for c in candidates:
        if not c['selected'] and c['valid']:
            c['rejected_reasons'] = ['ALTERNATIVE_VALID_DOMAIN_EVIDENCE']
    fulfilled = bool(selected) and not missing
    return {'status': 'FULFILLED' if fulfilled else 'PARTIAL' if selected else 'MISSING',
            'rubric_id': 'GLOBAL_SERVICE_PLAN', 'acceptance_object': 'WHOLE_DOCUMENT',
            'entity_backed': fulfilled, 'evidence_found': bool(selected),
            'concepts_missing': missing, 'missing_dimensions': missing,
            'response_sections': sorted({c['section_id'] for c in selected}),
            'response_cu': sorted({c['content_unit_id'] for c in selected}),
            'selected_evidence': selected, 'evidence_candidates': candidates,
            'rejected_evidence': [c for c in candidates if not c['selected']],
            'effective_chars': sum(u['chars'] for us in unit_index.values() for u in us),
            'global_dimensions': {'service_domains': {'required': len(plan.get('service_domains') or []), 'fulfilled': len(selected)},
                                  'structure': {'missing_sections': empty_sections},
                                  'project_context': {'missing_terms': context_missing},
                                  'operational_controls': {'required_for_every_domain': True}}}
