"""Offline scoring replay: immutable historical evidence, isolated outputs, no provider."""
from pathlib import Path
import argparse
import copy
import hashlib
import json
import sys
import re

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from planning.scoring_graph import build_scoring_graph
from planning.content_engineering import build_technical_scope_gate
from planning.hierarchy import build_scoring_matrix, apply_scoring_mapping_units, update_scoring_fulfillment
from planning.scoring_taxonomy import pre_generation_scoring_mapping_gate
from providers.network_lockdown import install, status
from governance.final_artifact_qa import _docx_blocks
from governance.final_editorial import SourceFacts, NUMERIC
from planning.scoring_final_evidence import align_final_evidence
from docx import Document
from scoring_exit_control import exit_control


def read(path):
    return json.loads(path.read_text('utf-8-sig'))


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf-8')


def main():
    import os
    os.environ['MMF_PROVIDER_NETWORK_LOCKDOWN'] = '1'
    install()
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--final-docx', type=Path, required=True)
    args = parser.parse_args()
    run, output = args.run.resolve(), args.output.resolve()
    if output == run or run in output.parents or output in run.parents:
        raise ValueError('Replay output must be independent of frozen evidence')
    output.mkdir(parents=True, exist_ok=True)
    hashes = {str(p.relative_to(run)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in run.rglob('*') if p.is_file() and (p.suffix in {'.json', '.docx'})}
    pack = read(run / 'tender/requirement_pack.json')
    source = read(run / 'tender/extraction.json')
    original = run / 'tender/originals' / source['files'][0]['original_filename']
    tender_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    if tender_hash != source['files'][0]['sha256']:
        raise ValueError('Tender original digest does not match extraction registry')
    document = Document(original)
    source_checks = []
    for table in source['tables']:
        if not table.get('is_scoring_table'):
            continue
        actual = document.tables[table['table_index']]
        actual_rows = [[c.text for c in row.cells] for row in actual.rows]
        norm = lambda v: re.sub(r'\s+', '', str(v))
        expected = [table['columns']] + table['rows']
        matches = [[norm(c) for c in row] for row in actual_rows] == [[norm(c) for c in row] for row in expected]
        if not matches:
            raise ValueError('Scoring source cells differ from original DOCX')
        table['columns'], table['rows'] = actual_rows[0], actual_rows[1:]
        source_checks.append({'table_id': table['table_id'], 'table_index': table['table_index'],
                              'original_cells_verified': matches, 'rows': actual_rows})
    graph = build_scoring_graph(source, pack['scoring_items'])
    pack['scoring_graph'] = graph
    links = {x['scoring_item_id']: x['leaf_id'] for x in graph['item_links']}
    leaves = {x['leaf_id']: x for x in graph['leaves']}
    for item in pack['scoring_items']:
        item['scoring_leaf_id'] = links.get(item['scoring_item_id'])
        item['scoring_leaf_text'] = (leaves.get(item['scoring_leaf_id']) or {}).get('leaf_text')
    names = {'word_plan': '01_word_document_plan.json', 'requirement_matrix': '02_requirement_section_matrix.json',
             'content_budget': '03_word_content_budget.json', 'section_contracts': '04_section_contracts.json',
             'global_state': '05_document_global_state_v0.json', 'technical_scope_gate': '14_technical_scope_gate.json'}
    bundle = {key: read(run / name) for key, name in names.items()}
    classification = read(run / '11_requirement_classification.json')
    bundle['technical_scope_gate'] = build_technical_scope_gate(classification,
                                     matrix=bundle['requirement_matrix'], contracts=bundle['section_contracts'],
                                     scoring_items=pack['scoring_items'], scoring_graph=graph)
    hierarchy = copy.deepcopy(read(run / '08_document_hierarchy.json'))
    matrix = build_scoring_matrix(pack, bundle, hierarchy)
    apply_scoring_mapping_units(hierarchy, matrix, bundle)
    gate = pre_generation_scoring_mapping_gate(matrix,
            planned_unit_ids=[u['content_unit_id'] for u in hierarchy['content_units']],
            planned_section_ids=[s['section_id'] for s in hierarchy['sections']],
            planned_unit_sections={str(u['content_unit_id']): str(u['section_id']) for u in hierarchy['content_units']})
    matrix['pre_generation_scoring_mapping_gate'] = gate
    dump(output / '08_document_hierarchy.json', hierarchy)
    dump(output / '09_scoring_matrix.json', matrix)
    dump(output / 'pre_generation_scoring_mapping_gate.json', gate)
    dump(output / 'source_scoring_graph.json', graph)
    dump(output / '14_technical_scope_gate.json', bundle['technical_scope_gate'])
    fragments = {p.parent.name: read(p) for p in (run / 'longform/word/sections').glob('*/fragment.json')}
    fragment_reading = update_scoring_fulfillment(output / '09_scoring_matrix.json', fragments)
    dump(output / 'fragment_scoring_reading.json', read(output / '09_scoring_matrix.json'))
    facts = SourceFacts.from_run(run)
    final_docx = args.final_docx.resolve()
    final_hash = hashlib.sha256(final_docx.read_bytes()).hexdigest()
    final_fragments, alignment = align_final_evidence(_docx_blocks(final_docx), fragments, facts, str(final_docx))
    dump(output / 'final_docx_cu_alignment.json', alignment)
    dump(output / 'final_docx_evidence_fragments.json', final_fragments)
    after = update_scoring_fulfillment(output / '09_scoring_matrix.json', final_fragments)
    matrix = read(output / '09_scoring_matrix.json')
    before = read(run / '09_scoring_matrix.json')
    topics = []
    for record in matrix['canonical_scoring_records']:
        previous = next((r for r in before['canonical_scoring_records']
                         if r.get('scoring_leaf_id') == record.get('scoring_leaf_id') and record.get('scoring_leaf_id')), {})
        for candidate in (record.get('fulfillment') or {}).get('selected_evidence') or []:
            text = str(candidate.get('evidence_text') or '')
            candidate['numeric_source_bindings'] = [facts.numeric_binding(text, *m.span())
                                                     for m in NUMERIC.finditer(text)]
            candidate['numeric_source_bindings'] = [b for b in candidate['numeric_source_bindings'] if b]
        topics.append({**record, 'before_status': (previous.get('fulfillment') or {}).get('status')})
    unchanged = all(hashlib.sha256((run / rel).read_bytes()).hexdigest() == digest for rel, digest in hashes.items())
    report = {'provider_calls': 0, 'frozen_evidence_unchanged': unchanged, 'source_hashes': hashes,
              'before': before['coverage_summary'], 'fragment_after': fragment_reading, 'after': after, 'topics': topics,
              'source_original_verification': {'path': str(original), 'sha256': tender_hash, 'tables': source_checks},
              'final_docx': {'path': str(final_docx), 'sha256': final_hash,
                             'unchanged': hashlib.sha256(final_docx.read_bytes()).hexdigest() == final_hash,
                             'alignment_bound': sum(r['status'] == 'BOUND' for r in alignment),
                             'alignment_unbound': sum(r['status'] != 'BOUND' for r in alignment)},
              'network_lockdown': status(),
              'source_universe': graph['by_volume'], 'planning_gate': gate,
              'excluded_business_qualification': [r for r in bundle['technical_scope_gate']['scoring_register']
                                                   if r['disposition'] == 'EXCLUDED']}
    exit_record = next((r for r in topics if r.get('evidence_contract', {}).get('rubric_id') == 'EXIT_HANDOVER'), None)
    if exit_record:
        control = exit_control(exit_record['evidence_contract'])
        dump(output / 'exit_control_evidence.json', control)
        report['exit_development_control'] = {'status': control['verdict']['status'],
                                             'missing_dimensions': control['verdict']['missing_dimensions'],
                                             'counted_toward_run_g_coverage': False}
    dump(output / 'RUN_I_OFFLINE_REPLAY.json', report)
    print(json.dumps({'before': before['coverage_summary'].get('technical_scoring_coverage'),
                      'after': after['technical_scoring_coverage'], 'gate': gate['status'],
                      'topics': [(r['scoring_table_topic'], r['responsiveness_class'], r['fulfillment']['status'],
                                  r['fulfillment'].get('missing_dimensions')) for r in topics],
                      'frozen_unchanged': unchanged}, ensure_ascii=False))


if __name__ == '__main__':
    main()
