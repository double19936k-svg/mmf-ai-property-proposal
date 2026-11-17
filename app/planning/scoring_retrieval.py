"""Auditable candidate selection; event identity precedes lexical similarity."""
from __future__ import annotations
import re
from typing import Any
from .scoring_context import team_context, exit_context, withdrawal_facts, withdrawal_clock_context, day_value


def visible_values(value: Any) -> str:
    if isinstance(value, dict):
        return ''.join(visible_values(v) for v in value.values())
    if isinstance(value, list):
        return ''.join(visible_values(v) for v in value)
    return str(value) if value is not None else ''


def select_evidence(concepts, units, rubric_id='', planned_ids=(), require_structured=False,
                    source_obligations=()):
    candidates = []
    selected = []
    clock_facts = {id(unit): withdrawal_facts(unit['text']) for unit in units} if source_obligations else {}
    conflicts = []
    for unit in units:
        for fact in clock_facts.get(id(unit), []):
            # Only an actual project withdrawal can contradict its source clock.
            # A different deadline for copying records remains unrelated.
            if (fact['trigger'] and fact['action'] == 'WITHDRAWAL'
                    and not fact['rejected_reasons']
                    and any(set(fact['objects']) & set(b.get('required_objects') or ['PERSONNEL', 'OWN_ASSETS'])
                            and fact['value'] != day_value(b.get('value', '')) for b in source_obligations)):
                conflicts.append({**fact, 'section_id': unit['section_id'],
                                  'content_unit_id': unit['assembled_content_unit_id'],
                                  'artifact_location': unit['artifact_location']})
    for concept, synonyms in concepts.items():
        group = []
        for unit in units:
            face = unit['text']
            coverage = sum(any(t in face for t in ts) for ts in concepts.values())
            for token in synonyms:
                for match in re.finditer(re.escape(token), face):
                    offset = match.start()
                    snippet = face[max(0, offset - 90):offset + len(token) + 90]
                    context = face[max(0, offset - 180):offset + len(token) + 180]
                    reasons = []
                    topic_context = {
                        'EMERGENCY_DEPLOYMENT_RESPONSE': r'应急|突发|抢险|救援|预案|演练',
                        'SERVICE_GUARANTEE_SYSTEM': r'保障|服务连续|缺岗|顶岗|应急补位',
                        'STAFF_GUARANTEE': r'人员|岗位|编制|培训|顶岗|替岗',
                        'MANAGEMENT_ADVANTAGE': r'本项目|办公楼|职工食堂|金融办公',
                        'PROJECT_MANAGEMENT_SUGGESTION': r'建议|改进|优化|风险|难点',
                    }.get(rubric_id)
                    if topic_context and not re.search(topic_context, face):
                        reasons.append('TOPIC_CONTEXT_MISMATCH')
                    basis = {}
                    if rubric_id == 'MANAGEMENT_TEAM':
                        scope_reasons, basis = team_context(face, offset)
                        reasons.extend(scope_reasons)
                    elif rubric_id == 'EXIT_HANDOVER':
                        event_reasons, basis = exit_context(face, offset)
                        reasons.extend(event_reasons)
                        if concept == '合同退出期限' and source_obligations:
                            clock_reasons, clock_basis = withdrawal_clock_context(
                                clock_facts[id(unit)], offset, token, source_obligations, conflicts)
                            reasons.extend(clock_reasons)
                            basis.update(clock_basis)
                    if re.search(r'未(?:明确|说明|覆盖|提供)|缺少|待补充|尚未', context):
                        # A negative statement about this concept cannot prove its fulfillment.
                        if re.search(r'(?:未|缺少|待补充|尚未).{0,8}' + re.escape(token), context):
                            reasons.append('NEGATED_OR_PENDING_EVIDENCE')
                    if re.search(r'横向比较|优得\d|一般得\d|较差得\d', context) and not re.search(r'执行|核查|签认|归档|记录|交底', context):
                        reasons.append('EVALUATOR_WORDING_ONLY')
                    hit = {'concept': concept, 'matched_term': token,
                           'section_id': unit['section_id'],
                           'content_unit_id': unit['assembled_content_unit_id'],
                           'archetype': unit['archetype'], 'unit_chars': unit['chars'],
                           'substantive': unit['substantive'],
                           'artifact_location': unit['artifact_location'],
                           'evidence_text': snippet,
                           'structured_backed': token in unit.get('structured_text', ''),
                           'semantic_dimension_count': coverage,
                           'business_context_valid': not reasons,
                           'business_context_basis': basis,
                           'rejected_reasons': reasons,
                           'in_planned_cu': unit['assembled_content_unit_id'] in planned_ids,
                           'offset': offset}
                    group.append(hit)
        valid = [c for c in group if c['business_context_valid']]
        valid.sort(key=lambda c: (-int(c['substantive']), -int(c['structured_backed'] and require_structured),
                                 -c['semantic_dimension_count'], -int(c['structured_backed']), -int(c['in_planned_cu']),
                                 c['artifact_location'], c['offset']))
        best = valid[0] if valid else None
        if best:
            selected.append(best)
        for c in group:
            c['selected'] = c is best
            if not c['selected'] and not c['rejected_reasons']:
                c['rejected_reasons'] = ['LOWER_RELEVANCE_THAN_SELECTED_EVIDENCE']
        candidates.extend(group)
    missing = [k for k in concepts if k not in {h['concept'] for h in selected}]
    return selected, missing, candidates
