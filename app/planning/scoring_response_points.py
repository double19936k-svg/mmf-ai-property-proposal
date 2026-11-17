"""Explicit pre-generation response structures and source-bound exit obligations."""
from __future__ import annotations
from typing import Any
from .scoring_context import source_withdrawal_obligations, deadline_terms


def bind_source_obligations(contract: dict[str, Any], pack: dict[str, Any]) -> None:
    if contract.get('rubric_id') != 'EXIT_HANDOVER':
        return
    bindings = []
    for requirement in pack.get('requirements') or []:
        for source in requirement.get('sources') or []:
            text = str(source.get('source_excerpt') or '')
            obligations = source_withdrawal_obligations(text)
            if not obligations or not source.get('para_id'):
                continue
            for obligation in obligations:
                # Keep the exact source obligation alongside its typed identity;
                # a records deadline is not a personnel/assets withdrawal clock.
                binding = {'concept': '合同退出期限', 'value': str(obligation['value']), 'unit': '日',
                           'text': obligation['clause'], 'source': dict(source),
                           'required_objects': obligation['objects'], 'required_action': 'WITHDRAWAL',
                           'required_trigger': 'CONTRACT_END',
                           'requirement_id': requirement.get('requirement_id')}
                if not any(b['source'].get('para_id') == source['para_id']
                           and b['text'] == binding['text'] for b in bindings):
                    bindings.append(binding)
    if bindings:
        contract['source_obligations'] = bindings
        if len({b['value'] for b in bindings}) > 1:
            contract['source_obligation_conflicts'] = bindings
        if '合同退出期限' not in contract['concepts']:
            contract['concepts'].append('合同退出期限')
            contract['min_concepts'] += 1
        contract.setdefault('concept_synonyms', {})['合同退出期限'] = list(deadline_terms(bindings))


def attach_response_points(matrix: dict[str, Any], hierarchy: dict[str, Any]) -> None:
    units = {str(u.get('content_unit_id')): u for u in hierarchy.get('content_units') or []}
    for record in matrix.get('canonical_scoring_records') or []:
        contract = record.get('evidence_contract') or {}
        if record.get('responsiveness_class') == 'WHOLE_DOCUMENT_RUBRIC':
            record['global_acceptance_plan'] = {'acceptance_object': 'WHOLE_DOCUMENT',
                                               'dimensions': list(contract.get('concepts') or []),
                                               'expected_location': 'FINAL_WORD_DOCUMENT'}
            record['planning_response_points'] = []
            continue
        if not record.get('requires_technical_body_response', True):
            record['planning_response_points'] = []
            continue
        points = []
        declared = [str(uid) for uid in record.get('planned_response_cu') or [] if str(uid) in units]
        synonyms = contract.get('concept_synonyms') or {}
        def relevance(uid):
            u = units[uid]
            face = str(u.get('title') or '') + str(u.get('intent') or '') + str(u.get('purpose') or '')
            return sum(any(term in face for term in synonyms.get(c) or [c]) for c in contract.get('concepts') or [])
        anchors = sorted(declared, key=lambda uid: (-relevance(uid), uid))[:1]
        for uid in anchors:
            unit = units.get(str(uid))
            if not unit:
                continue
            sid = str(unit.get('section_id') or '')
            point = {'topic_id': record['canonical_scoring_id'], 'section_id': sid,
                     'content_unit_id': uid, 'required_concepts': list(contract.get('concepts') or []),
                     'evidence_contract': contract,
                     'supporting_content_unit_ids': [c for c in declared if c != uid],
                     'expected_final_evidence_location': f'word/sections/{sid}/fragment.json#{uid}',
                     'business_event': 'PROJECT_EXIT' if contract.get('rubric_id') == 'EXIT_HANDOVER' else contract.get('rubric_id')}
            points.append(point)
            unit.setdefault('scoring_response_points', [])
            unit['scoring_response_points'] = [p for p in unit['scoring_response_points'] if p['topic_id'] != point['topic_id']] + [point]
            unit['must_cover'] = list(dict.fromkeys(list(unit.get('must_cover') or []) + point['required_concepts']
                                    + [b['text'] for b in contract.get('source_obligations') or []]))
            source_ids = [b['requirement_id'] for b in contract.get('source_obligations') or [] if b.get('requirement_id')]
            unit['source_requirements'] = list(dict.fromkeys(list(unit.get('source_requirements') or []) + source_ids))
            unit['requirement_ids'] = list(dict.fromkeys(list(unit.get('requirement_ids') or []) + source_ids))
            record['planned_response_section'] = sid
            record['planned_response_sections'] = list(dict.fromkeys([sid] + list(record.get('planned_response_sections') or [])))
        record['planning_response_points'] = points
    matrix['response_point_contract_version'] = 'explicit-scoring-response-point-v1'
    matrix['protected_evidence_register'] = [
        {'topic_id': r['canonical_scoring_id'], 'source_scoring_rows': r.get('source_ids') or [],
         'terms': list(r.get('protected_evidence_terms') or []),
         'scope': ['QUALIFICATION', 'PROTECTED_EVIDENCE'], 'status': 'HUMAN_INPUT_REQUIRED',
         'technical_body_identity_evidence_required': False}
        for r in matrix.get('canonical_scoring_records') or [] if r.get('protected_evidence_terms')]
