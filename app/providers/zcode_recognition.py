"""Parse a complete ZCode recognition reply without salvaging nested item objects.

Only serialization punctuation is repaired. Request identity and all candidate
channels remain checked; a truncated document is never made complete here.
"""
from __future__ import annotations

import json
import re
from .base import FIELD_ALIASES, ProviderError

ARRAY_KEYS = ('proposed_items', 'proposed_conflicts', 'proposed_boilerplate',
              'proposed_scoring_items', 'chunk_missing_facts')
WRAPPERS = ('data', 'result', 'response', 'payload', 'output')

def _numeric_chunk_identity(value):
    if not isinstance(value,str):return None
    match=re.fullmatch(r'CHK-(\d+)(?:-([SZ])(\d+))?',value)
    if not match:return None
    parent=int(match[1]);child=int(match[3]) if match[3] else None
    if parent<1 or (child is not None and child<1):return None
    return parent,match[2],child


def _syntax_only(text):
    """Escape obvious interior quotes/control characters and remove trailing commas.

    Do not invent values, delimiters, closing containers, or completed strings.
    The resulting *entire* document must still pass json.loads.
    """
    out = []; edits = []; inside = False; escaped = False
    for i, char in enumerate(text):
        if inside:
            if escaped:
                out.append(char); escaped = False; continue
            if char == '\\':
                out.append(char); escaped = True; continue
            if char == '"':
                tail = text[i + 1:].lstrip()
                closes = not tail or tail[0] in ':,]}'
                # A comma in prose does not end a JSON string. A real comma
                # must be followed by a possible next JSON value/member.
                if tail.startswith(','):
                    rest = tail[1:].lstrip()
                    closes = not rest or rest[0] in '"{[}]tfn-0123456789'
                if not closes:
                    out.append('\\"'); edits.append({'type': 'escape_quote', 'offset': i}); continue
                inside = False
            elif ord(char) < 32:
                out.append(json.dumps(char)[1:-1]); edits.append({'type': 'escape_control', 'offset': i}); continue
        elif char == '"':
            inside = True
        elif char == ',' and text[i + 1:].lstrip().startswith(('}', ']')):
            edits.append({'type': 'trailing_comma', 'offset': i}); continue
        out.append(char)
    return ''.join(out), edits


def _loads(text):
    def unique(pairs):
        row = {}
        for key, value in pairs:
            if key in row: raise ValueError('duplicate JSON key')
            row[key] = value
        return row
    return json.loads(text, object_pairs_hook=unique)


def _document(value):
    if isinstance(value, dict): return value, []
    if not isinstance(value, str):
        raise ProviderError('ZCode已返回结果，但需求数据不是JSON对象；原答已保留。', error_code='ZCODE_RECOGNITION_FORMAT')
    text = value.strip().lstrip('\ufeff')
    text = re.sub(r'^```(?:json)?\s*\n?', '', text, count=1, flags=re.I)
    text = re.sub(r'\s*```$', '', text).strip()
    # Outside prose is not business data; keep the whole outer object, never
    # search within an invalid object for a convenient inner requirement.
    start, end = text.find('{'), text.rfind('}')
    if start >= 0 and end >= start: text = text[start:end + 1]
    try: return _loads(text), []
    except (ValueError, TypeError): pass
    repaired, edits = _syntax_only(text)
    try:
        if not edits or len(edits) > 64: raise ValueError('not a bounded syntax repair')
        return _loads(repaired), edits
    except (ValueError, TypeError):
        raise ProviderError('ZCode已返回结果，但JSON格式损坏或内容不完整，无法安全恢复；原答和已完成子块已保留。',
                            error_code='ZCODE_RECOGNITION_FORMAT') from None


def parse_recognition(request, value):
    document, edits = _document(value)
    candidates = []
    def visit(row, path='', depth=0):
        if not isinstance(row, dict) or depth > 4: return
        # Never mistake a requirement item or an arbitrary object for a reply.
        keys = ('proposed_items', *FIELD_ALIASES['proposed_items'])
        if any(key in row for key in keys): candidates.append((row, path)); return
        for key in WRAPPERS:
            if isinstance(row.get(key), dict): visit(row[key], path + '/' + key, depth + 1)
    visit(document)
    if len(candidates) != 1:
        raise ProviderError('ZCode回复中没有唯一可识别的需求结果对象；原答已保留，未把内部条目当作完整结果。',
                            error_code='ZCODE_RECOGNITION_SCHEMA')
    original, path = candidates[0]; result = dict(original); aliases = {}
    for canonical in ('chunk_id', *ARRAY_KEYS):
        for alias in FIELD_ALIASES.get(canonical, ()):
            if alias in result and canonical in result and result[alias] != result[canonical]:
                raise ProviderError('ZCode回复的同义字段内容冲突；原答已保留。', error_code='ZCODE_RECOGNITION_SCHEMA')
            if canonical not in result and alias in result:
                result[canonical] = result[alias]; aliases[canonical] = alias
    if not isinstance(result.get('proposed_items'), list):
        raise ProviderError('ZCode需求条目必须是数组；原答已保留。', error_code='ZCODE_RECOGNITION_SCHEMA')
    expected = request.get('chunk_id'); filled = []
    identity_edits = []
    if 'chunk_id' not in result and isinstance(expected, str) and expected:
        result['chunk_id'] = expected; filled.append('chunk_id')
    if expected and result.get('chunk_id')!=expected:
        original_id=result.get('chunk_id');identity=_numeric_chunk_identity(expected)
        if identity is not None and _numeric_chunk_identity(original_id)==identity:
            result['chunk_id']=expected
            identity_edits.append({'from':original_id,'to':expected,'reason':'numeric_zero_padding_only'})
    if not expected or result.get('chunk_id') != expected:
        raise ProviderError('ZCode返回的分块编号与当前请求不一致；原答已保留，未混入其他块。',
                            error_code='ZCODE_RECOGNITION_ID_MISMATCH')
    containers = []
    # The contract is array<string>. Some account models serialize a single
    # missing-information note as a scalar. Preserve that exact note as one
    # list member; do not infer facts or relax object/identity validation.
    if isinstance(result.get('chunk_missing_facts'), str):
        note = result['chunk_missing_facts']
        result['chunk_missing_facts'] = [note] if note.strip() else []
        containers.append({'field':'chunk_missing_facts','from':'string','to':'array',
                           'empty':not bool(note.strip()),'business_text_changed':False})
    for key in ARRAY_KEYS:
        if key not in result:
            result[key] = []; filled.append(key)
        if not isinstance(result[key], list):
            raise ProviderError(f'ZCode字段{key}不是数组；原答已保留。', error_code='ZCODE_RECOGNITION_SCHEMA')
    audit = {'applied': bool(edits or filled or aliases or path or containers or identity_edits), 'type': 'zcode_recognition_serialization',
             'identity_normalizations': identity_edits,
             'container_normalizations': containers,
             'syntax_edits': edits, 'filled_keys': filled, 'aliases': aliases, 'wrapper': path,
             'business_text_changed': False, 'partial_items_salvaged': False,
             'channel_counts': {key: len(result[key]) for key in ARRAY_KEYS}}
    return result, audit
