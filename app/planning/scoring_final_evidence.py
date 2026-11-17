"""Read-only CU alignment to actual final DOCX blocks; no evidence synthesis."""
from __future__ import annotations
import re
import hashlib
import json
import tempfile
from pathlib import Path
from .scoring_evidence import normalize_text


def _visible_leaves(value):
    """Keep cell/step boundaries: final tables add separators between values."""
    if isinstance(value, dict):
        for child in value.values():
            yield from _visible_leaves(child)
    elif isinstance(value, list):
        for child in value:
            yield from _visible_leaves(child)
    elif value is not None:
        yield str(value)


def align_final_evidence(blocks, fragments, facts, artifact_path):
    from governance.final_editorial import edit_text
    titles = {normalize_text(f.get('title')): sid for sid, f in fragments.items() if f.get('title')}
    section_blocks = {}
    sid = None
    for block in blocks:
        if block.get('heading_level') == 2:
            text = normalize_text(block['text'])
            sid = titles.get(text)
            if sid is None:
                matches = [s for title, s in titles.items() if title and (title in text or text in title
                           or len(title) >= 8 and len(text) >= 8 and title[:8] == text[:8])]
                sid = matches[0] if len(matches) == 1 else None
        elif block.get('heading_level') == 1:
            sid = None
        if sid and not block.get('heading_level'):
            section_blocks.setdefault(sid, []).append(block)
    final_fragments, audit = {}, []
    for sid, fragment in fragments.items():
        rows = fragment.get('content_unit_fragments') or []
        aligned = {r['content_unit_id']: [] for r in rows}
        # CU paragraph and payload anchors identify a carrier, not a scoring concept.
        anchors = {}
        for row in rows:
            content = normalize_text(edit_text(str(row.get('content') or ''), facts)[0])
            payload = row.get('payload') or {}
            parts = [content]
            if isinstance(payload, dict):
                parts.extend(normalize_text(edit_text(v, facts)[0]) for v in _visible_leaves(payload))
            spans = {part[i:i + 22] for part in parts for i in range(0, max(0, len(part) - 21), 75)}
            anchors[row['content_unit_id']] = spans
        for block in section_blocks.get(sid) or []:
            face = normalize_text(block['text'])
            ranked = sorted(((sum(a in face for a in spans), uid) for uid, spans in anchors.items()), reverse=True)
            if not ranked or ranked[0][0] < 2 or len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
                audit.append({'section_id': sid, 'final_location': block['path'],
                              'status': 'UNBOUND_OR_AMBIGUOUS', 'candidates': ranked[:3]})
                continue
            score, uid = ranked[0]
            aligned[uid].append(block)
            audit.append({'section_id': sid, 'content_unit_id': uid, 'final_location': block['path'],
                          'status': 'BOUND', 'exact_carrier_anchors': score, 'candidates': ranked[:3]})
        final_rows = []
        for row in rows:
            own = aligned[row['content_unit_id']]
            final_rows.append({'content_unit_id': row['content_unit_id'], 'archetype': row.get('archetype'),
                               'content': '\n'.join(b['text'] for b in own), 'payload': {},
                               'final_structured_text': '\n'.join(b['text'] for b in own if b['kind'] == 'table'),
                               'final_artifact_locations': [f'{artifact_path}#{b["path"]}' for b in own]})
        final_fragments[sid] = {'section_id': sid, 'title': fragment.get('title'),
                                'content_unit_fragments': final_rows, 'tables': [], 'processes': []}
    return final_fragments, audit


def final_scoring_binding_matches(matrix, artifact_path, *, artifact_sha256=None):
    """A stored final reading only describes the exact artifact it measured."""
    path = Path(artifact_path)
    binding = matrix.get('final_artifact_scoring_binding') or {}
    try:
        return (binding.get('basis') == 'ACTUAL_FINAL_DOCX'
                and Path(binding.get('artifact_path') or '').resolve() == path.resolve()
                and binding.get('artifact_sha256') == (artifact_sha256 or hashlib.sha256(path.read_bytes()).hexdigest()))
    except (OSError, ValueError, TypeError):
        return False


def final_scoring_matrix_matches(reading, matrix_path):
    """Guard the measured matrix separately from the landed artifact identity."""
    try:
        return reading.get('matrix_output_sha256') == hashlib.sha256(Path(matrix_path).read_bytes()).hexdigest()
    except OSError:
        return False


def refresh_final_scoring(run_dir, artifact_path, *, expected_artifact_sha256=None, persist=True):
    """Rejudge the actual final DOCX without replacing a valid prior matrix on failure.

    Final carriers contain only text read from the reopened DOCX. Original CU
    fragments identify those carriers; their payload is never scoring evidence.
    This runs after editorial/pagination/local repair, with no Provider calls.
    """
    from governance.final_artifact_qa import _docx_blocks
    from governance.final_editorial import SourceFacts
    from .hierarchy import update_scoring_fulfillment

    run_dir, artifact_path = Path(run_dir), Path(artifact_path).resolve()
    matrix_path = run_dir / '09_scoring_matrix.json'
    report_path = run_dir / 'final_scoring_report.json'
    report = {'schema_version': 'final-artifact-scoring-v1', 'phase': 'FINAL_SCORING',
              'provider_calls': 0, 'artifact_path': str(artifact_path),
              'status': 'FAILED', 'completed_clean_allowed': False,
              'may_claim_final_scoring': False, 'prior_matrix_preserved': True}

    def finish(**fields):
        report.update(fields)
        if persist:
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return report

    if not matrix_path.is_file():
        return finish(status='NOT_APPLICABLE', reason_code='SCORING_MATRIX_ABSENT')
    staged_path = None
    try:
        original_matrix = matrix_path.read_bytes()
        report['matrix_input_sha256'] = hashlib.sha256(original_matrix).hexdigest()
        matrix = json.loads(original_matrix.decode('utf-8-sig'))
        if not matrix.get('rows'):
            if matrix.get('canonical_scoring_records'):
                return finish(reason_code='FINAL_SCORING_ROWS_MISSING')
            return finish(status='NOT_APPLICABLE', reason_code='NO_SCORING_ROWS')
        if not artifact_path.is_file():
            return finish(reason_code='FINAL_SCORING_ARTIFACT_MISSING')
        artifact_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        report['artifact_sha256'] = artifact_hash
        if expected_artifact_sha256 is not None and artifact_hash != expected_artifact_sha256:
            return finish(reason_code='FINAL_SCORING_ARTIFACT_IDENTITY_MISMATCH')
        fragment_paths = sorted((run_dir / 'longform/word/sections').glob('*/fragment.json'))
        if not fragment_paths:
            return finish(reason_code='FINAL_SCORING_FRAGMENTS_MISSING')
        fragments, fragment_hashes = {}, {}
        for path in fragment_paths:
            raw = path.read_bytes()
            fragment = json.loads(raw.decode('utf-8-sig'))
            if not isinstance(fragment, dict) or not isinstance(fragment.get('content_unit_fragments'), list):
                return finish(reason_code='FINAL_SCORING_FRAGMENTS_INVALID')
            fragments[path.parent.name] = fragment
            fragment_hashes[str(path.relative_to(run_dir))] = hashlib.sha256(raw).hexdigest()
        facts = SourceFacts.from_run(run_dir)
        final_fragments, alignment = align_final_evidence(_docx_blocks(artifact_path), fragments,
                                                        facts, str(artifact_path))
        # Fulfillment writes its input path. Stage a private copy so invalid DOCX,
        # scorer failure or an identity change cannot leave a half-updated matrix.
        with tempfile.NamedTemporaryFile(dir=run_dir, prefix='.final_scoring_', suffix='.json', delete=False) as temp:
            staged_path = Path(temp.name)
            temp.write(original_matrix)
        fulfillment = update_scoring_fulfillment(staged_path, final_fragments)
        measured = json.loads(staged_path.read_text(encoding='utf-8-sig'))
        binding = {'schema_version': 'final-artifact-scoring-binding-v1', 'basis': 'ACTUAL_FINAL_DOCX',
                   'artifact_path': str(artifact_path), 'artifact_sha256': artifact_hash,
                   'source_fragment_sha256': fragment_hashes,
                   'aligned_blocks': sum(row['status'] == 'BOUND' for row in alignment),
                   'unbound_blocks': sum(row['status'] != 'BOUND' for row in alignment)}
        measured['final_artifact_scoring_binding'] = binding
        staged_path.write_text(json.dumps(measured, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        if hashlib.sha256(artifact_path.read_bytes()).hexdigest() != artifact_hash:
            return finish(reason_code='FINAL_SCORING_ARTIFACT_CHANGED_DURING_CHECK')
        if matrix_path.read_bytes() != original_matrix:
            return finish(reason_code='FINAL_SCORING_MATRIX_CHANGED_DURING_CHECK')
        if not persist:
            return finish(status='VERIFIED', reason_code=None, may_claim_final_scoring=True,
                          fulfillment=fulfillment,
                          completed_clean_allowed=bool(fulfillment.get('completed_clean_allowed')),
                          artifact_binding=binding)
        (run_dir / 'final_scoring_alignment.json').write_text(
            json.dumps(alignment, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        (run_dir / 'final_scoring_evidence_fragments.json').write_text(
            json.dumps(final_fragments, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        staged_path.replace(matrix_path)
        staged_path = None
        return finish(status='VERIFIED', reason_code=None, may_claim_final_scoring=True,
                      prior_matrix_preserved=False, fulfillment=fulfillment,
                      completed_clean_allowed=bool(fulfillment.get('completed_clean_allowed')),
                      artifact_binding=binding,
                      matrix_output_sha256=hashlib.sha256(matrix_path.read_bytes()).hexdigest())
    except Exception as exc:
        # The report is a delivery input: exceptions must never silently retain
        # a pre-assembly FULFILLED reading as final scoring authority.
        return finish(reason_code='FINAL_SCORING_CHECK_FAILED', error_type=type(exc).__name__)
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)
