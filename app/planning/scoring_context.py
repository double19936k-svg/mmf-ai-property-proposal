"""Scoring-local business identity and typed withdrawal obligations.

These checks qualify evidence candidates; they never rewrite source claims or
customer text. A nearby topic word cannot supply another event's identity.
"""
from __future__ import annotations

import re


_SENTENCE = re.compile(r'[^。；;！？!?\n|]+')
_EXIT = re.compile(r'合同.{0,15}?(?:终止|解除|期满)|项目.{0,8}?(?:退出|退场)|退出移交')
_ENTRY = re.compile(r'进场|入场|接管(?!方|单位|团队|人员)|我方接收.{0,12}?管理权|原物业.{0,12}?(?:移交|签收)')
_CONTRACTOR = re.compile(r'供方|供应商|临时承包|施工单位|维保单位|维保合同')
_SITE = re.compile(r'本项目|本标段|项目现场|现场(?:组织|团队|岗位|人员)|常驻现场|驻场')
_ONSITE = re.compile(r'现场(?:组织|团队|岗位|人员|项目)|常驻现场|驻场|项目现场')
_HQ = re.compile(r'公司总部|总部|全国项目|公司组织架构')
_PENDING = re.compile(r'未(?:明确|说明|覆盖|提供|撤出|撤离)|不(?:涉及|包含|负责|在|按|执行|安排|撤出)|'
                      r'缺少|待(?:补充|确认)|尚未|另行约定|无需|无须|禁止|不得(?!晚于|超过|延迟)')
_DAYS = re.compile(r'(?P<value>\d+|[零〇一二两三四五六七八九十百千]+)(?:日|天)内')
_TRIGGER = re.compile(r'合同.{0,12}?(?:终止|解除|期满)|终止(?:或者|或)解除')
_WITHDRAWAL = re.compile(r'(?:撤出|撤离|撤场)(?!计划|安排|期限|手续|流程|方案|时间)')
_OBJECTS = {'PERSONNEL': re.compile(r'人员'),
            'OWN_ASSETS': re.compile(r'自有(?:资产|设备|物资)')}


def _sentences(text):
    return [(m.start(), m.end(), m.group()) for m in _SENTENCE.finditer(text)]


def _local(text, offset):
    return next(((start, end, face) for start, end, face in _sentences(text)
                 if start <= offset < end), (0, len(text), text))


def _positive_scope(face, pattern):
    return [m for m in pattern.finditer(face)
            if not re.search(r'(?:不涉及|不包含|非|不是|不属于|不负责|未设置|没有).{0,8}$', face[:m.start()])]


def team_context(text, offset):
    start, _, clause = _local(text, offset)
    site = _positive_scope(clause, _SITE)
    onsite = _positive_scope(clause, _ONSITE)
    reasons = []
    if _HQ.search(clause) and not onsite:
        reasons.append('HEADQUARTERS_NOT_PROJECT_TEAM')
    # Neutral role rows may inherit an earlier project heading, but a later
    # headquarters scope switches that identity until a site anchor returns.
    anchors = []
    for clause_start, _, prior in _sentences(text[:offset + 1]):
        prior_site = _positive_scope(prior, _SITE)
        if _HQ.search(prior) and not _positive_scope(prior, _ONSITE):
            anchors.append((clause_start, 'HEADQUARTERS'))
        elif prior_site:
            anchors.append((clause_start, 'PROJECT_SITE'))
    preceding = max(anchors, default=(None, None), key=lambda pair: pair[0])
    if not site and preceding[1] != 'PROJECT_SITE':
        reasons.append('PROJECT_SITE_CONTEXT_MISSING')
    return reasons, {'scope': 'PROJECT_SITE' if not reasons else 'UNBOUND',
                     'clause_offset': start, 'clause': clause,
                     'scope_anchor_offset': site[0].start() + start if site else preceding[0]}


def exit_context(text, offset):
    start, _, clause = _local(text, offset)
    reasons = []
    if _CONTRACTOR.search(clause):
        reasons.append('CONTRACTOR_WITHDRAWAL_NOT_PROJECT_EXIT')
    local_exit = _positive_scope(clause, _EXIT)
    local_entry = _positive_scope(clause, _ENTRY)
    events = [(m.start(), 'PROJECT_EXIT') for m in _positive_scope(text[:offset + 1], _EXIT)]
    events += [(m.start(), 'PROJECT_ENTRY') for m in _positive_scope(text[:offset + 1], _ENTRY)]
    events += [(m.start(), 'CONTRACTOR_WITHDRAWAL') for m in _CONTRACTOR.finditer(text[:offset + 1])]
    preceding = max(events, default=(None, None), key=lambda pair: pair[0])
    # An explicit entry clause cannot borrow an exit heading elsewhere in a CU.
    if local_entry:
        reasons.append('ENTRY_TAKEOVER_NOT_PROJECT_EXIT')
    if not local_exit and preceding[1] != 'PROJECT_EXIT':
        reasons.append('WRONG_BUSINESS_EVENT')
    if reasons and 'WRONG_BUSINESS_EVENT' not in reasons:
        reasons.append('WRONG_BUSINESS_EVENT')
    return reasons, {'event': 'PROJECT_EXIT' if not reasons else 'UNBOUND',
                     'clause_offset': start, 'clause': clause,
                     'event_anchor_offset': local_exit[0].start() + start if local_exit else preceding[0]}


def day_value(value):
    if str(value).isdigit():
        return int(value)
    digits = dict(zip('零〇一二两三四五六七八九', [0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9]))
    total, current = 0, 0
    for char in str(value):
        if char in digits:
            current = digits[char]
        elif char in '十百千':
            total += (current or 1) * {'十': 10, '百': 100, '千': 1000}[char]
            current = 0
        else:
            return None
    return total + current


def _chinese_days(value):
    """A spelling alias for a source number, never a newly invented deadline."""
    value = int(value)
    if not 0 < value < 100:
        return None
    digits = '零一二三四五六七八九'
    if value < 10:
        return digits[value]
    tens, ones = divmod(value, 10)
    return (digits[tens] if tens > 1 else '') + '十' + (digits[ones] if ones else '')


def deadline_terms(bindings):
    terms = []
    for binding in bindings:
        value = day_value(binding.get('value', ''))
        if value is None:
            continue
        spellings = [str(value), _chinese_days(value)]
        terms += [str(binding.get('text') or '')]
        terms += [f'{spelling}{unit}内' for spelling in spellings if spelling for unit in ('日', '天')]
    return tuple(dict.fromkeys(term for term in terms if term))


def withdrawal_facts(text):
    """Bind each clock to its own clause, subjects and performed action.

    A comma after a bare clock introduces its action; a comma after a completed
    predicate starts another obligation. Multiple clocks in one clause remain
    ambiguous, rather than lending one clock to all objects in that sentence.
    """
    facts = []
    for start, _, sentence in _sentences(text):
        pieces = list(re.finditer(r'[^，,]+', sentence))
        merged = []
        for piece in pieces:
            if merged and re.search(r'(?:日|天)内[：:]?$', merged[-1][1]):
                previous_start, previous_text = merged.pop()
                merged.append((previous_start, sentence[previous_start:piece.end()]))
            else:
                merged.append((piece.start(), piece.group()))
        for relative, clause in merged:
            clocks = list(_DAYS.finditer(clause))
            for clock in clocks:
                offset = start + relative + clock.start()
                reasons, event = exit_context(text, offset)
                objects = [name for name, pattern in _OBJECTS.items() if pattern.search(clause)]
                action = bool(_WITHDRAWAL.search(clause)) and not bool(_PENDING.search(clause))
                trigger = bool(_TRIGGER.search(clause))
                if len(clocks) != 1:
                    reasons.append('AMBIGUOUS_WITHDRAWAL_CLOCK_BINDING')
                facts.append({'value': day_value(clock['value']), 'objects': objects,
                              'action': 'WITHDRAWAL' if action else None,
                              'trigger': 'CONTRACT_END' if trigger else None,
                              'offset': offset, 'clock_end': offset + len(clock.group()),
                              'clause': clause, 'clause_offset': start + relative,
                              'business_context': event, 'rejected_reasons': reasons})
    return facts


def source_withdrawal_obligations(text):
    return [fact for fact in withdrawal_facts(re.sub(r'\s+', '', text))
            if fact['trigger'] and fact['action'] == 'WITHDRAWAL'
            and fact['objects'] == ['PERSONNEL', 'OWN_ASSETS'] and not fact['rejected_reasons']]


def withdrawal_clock_context(facts, offset, term, bindings, conflicts):
    matches = [fact for fact in facts if offset <= fact['offset']
               and fact['clock_end'] <= offset + len(term)]
    reasons = []
    proof = matches[0] if len(matches) == 1 else None
    if not proof:
        reasons.append('WITHDRAWAL_OBLIGATION_NOT_BOUND')
    else:
        reasons.extend(proof['rejected_reasons'])
        for binding in bindings:
            required = binding.get('required_objects') or ['PERSONNEL', 'OWN_ASSETS']
            supporting = [fact for fact in facts if fact['value'] == day_value(binding.get('value', ''))
                          and fact['trigger'] == 'CONTRACT_END' and fact['action'] == 'WITHDRAWAL'
                          and not fact['rejected_reasons']]
            # Explicit, equally timed personnel and own-asset obligations may
            # be written separately in the same carrier. Retain both proofs.
            objects = {obj for fact in supporting for obj in fact['objects']}
            if (proof['value'] != day_value(binding.get('value', ''))
                    or proof['trigger'] != 'CONTRACT_END'
                    or proof['action'] != 'WITHDRAWAL'
                    or not proof['objects']
                    or not set(required).issubset(objects)):
                reasons.append('SOURCE_WITHDRAWAL_OBJECT_ACTION_CLOCK_MISMATCH')
    if conflicts:
        reasons.append('CONTRADICTORY_WITHDRAWAL_CLOCK')
    return list(dict.fromkeys(reasons)), {'obligation': proof,
                                          'object_obligation_bindings': supporting if proof else [],
                                          'conflicting_withdrawal_obligations': conflicts}
