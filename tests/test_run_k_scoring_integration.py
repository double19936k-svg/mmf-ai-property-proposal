"""Actual final DOCX scoring across H editorial and I's evidence contracts."""
import copy
import hashlib
import json

import pytest
from docx import Document

from governance.final_artifact_qa import _docx_blocks
from governance.final_editorial import SourceFacts, run_final_editorial
from longform.orchestrator import _scoring_fulfillment_ratio
from planning.hierarchy import update_scoring_fulfillment
from planning.scoring_final_evidence import (align_final_evidence, final_scoring_binding_matches,
                                            refresh_final_scoring)
from planning.scoring_taxonomy import evidence_contract_for


TITLE = '项目管理团队人员结构'
NARRATIVE = ('本项目现场组织架构由项目主管统筹，项目主管常驻现场接受甲方指令。'
             '专业负责人承担现场岗位职责与汇报归口，人员配置按现场排班和值守岗位核对。'
             '培训交底和技能核验由项目负责人安排，记录归档后复核。') + ''.join(
             f'本项目现场第{n}项岗位接口由专业负责人负责，现场项目主管核查岗位排班和交底记录，'
             '对应事项按执行状态登记，形成岗位职责确认与汇报记录。' for n in range(1, 7))
TABLE_ROWS = [['组织架构', '本项目现场项目主管常驻现场，专业负责人承担岗位职责与汇报。'],
              ['人员配置', '本项目现场人员配置以排班和值守安排逐岗核对。'],
              ['能力管理', '本项目项目负责人安排培训交底、技能核验和考核记录。']]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def setup_run(tmp_path, *, with_table=True, ambiguous=False):
    contract = evidence_contract_for('项目管理团队')
    fragment = {'section_id': 'S1', 'title': TITLE, 'content_unit_fragments': [
        {'content_unit_id': 'CU-1', 'archetype': 'STANDARD_TABLE', 'content': NARRATIVE,
         'payload': {'columns': ['配置项目', '现场安排'], 'rows': TABLE_ROWS}}]}
    if ambiguous:
        second = copy.deepcopy(fragment['content_unit_fragments'][0])
        second['content_unit_id'] = 'CU-2'
        fragment['content_unit_fragments'].append(second)
    write_json(tmp_path / 'longform/word/sections/S1/fragment.json', fragment)
    matrix = {'rows': [{'scoring_item_id': 'SCR-1', 'label': '项目管理团队', 'section_ids': ['S1'],
                       'entity_contract': contract, 'scope_disposition': 'INCLUDED',
                       'scope_class': 'TECHNICAL_INCLUDED',
                       'responsiveness_class': 'TECHNICAL_BODY_ANONYMOUS_STRUCTURE',
                       'planned_response_cu': ['CU-1']}],
              'canonical_scoring_records': [{'canonical_scoring_id': 'SCN-1', 'source_ids': ['SCR-1'],
                       'responsiveness_class': 'TECHNICAL_BODY_ANONYMOUS_STRUCTURE',
                       'evidence_contract': contract, 'planned_response_cu': ['CU-1']}],
              'coverage_summary': {}}
    matrix_path = tmp_path / '09_scoring_matrix.json'
    write_json(matrix_path, matrix)
    update_scoring_fulfillment(matrix_path, {'S1': fragment})
    docx = tmp_path / 'final.docx'
    doc = Document()
    doc.add_heading('第一章 本项目人员方案', level=1)
    doc.add_heading(TITLE, level=2)
    doc.add_paragraph(NARRATIVE)
    if with_table:
        table = doc.add_table(rows=1, cols=2)
        table.rows[0].cells[0].text, table.rows[0].cells[1].text = '配置项目', '现场安排'
        for row in TABLE_ROWS:
            cells = table.add_row().cells
            for index, text in enumerate(row):
                cells[index].text = text
    doc.save(docx)
    return docx, matrix_path, fragment


def test_populated_short_cells_survive_H_and_bind_actual_final_scoring(tmp_path):
    docx, matrix_path, fragment = setup_run(tmp_path)
    run_final_editorial(docx, facts=SourceFacts(), output_dir=tmp_path, section_titles={'S1': TITLE})
    report = refresh_final_scoring(tmp_path, docx)
    matrix = json.loads(matrix_path.read_text('utf-8'))
    assert report['status'] == 'VERIFIED' and report['completed_clean_allowed']
    assert matrix['rows'][0]['fulfillment']['status'] == 'FULFILLED'
    assert matrix['rows'][0]['fulfillment']['structured_hits'] > 0
    assert final_scoring_binding_matches(matrix, docx)
    aligned = json.loads((tmp_path / 'final_scoring_alignment.json').read_text('utf-8'))
    assert any(row['final_location'] == 'table[0]' and row['status'] == 'BOUND' for row in aligned)
    evidence = json.loads((tmp_path / 'final_scoring_evidence_fragments.json').read_text('utf-8'))
    actual = evidence['S1']['content_unit_fragments'][0]
    assert actual['payload'] == {} and '#table[0]' in ';'.join(actual['final_artifact_locations'])


def test_candidate_scoring_measures_without_replacing_published_evidence(tmp_path):
    docx, matrix_path, _ = setup_run(tmp_path, with_table=False)
    original_matrix = matrix_path.read_bytes()
    original_files = set(tmp_path.iterdir())
    report = refresh_final_scoring(tmp_path, docx, persist=False)
    assert report['status'] == 'VERIFIED' and not report['completed_clean_allowed']
    assert report['prior_matrix_preserved'] and matrix_path.read_bytes() == original_matrix
    assert set(tmp_path.iterdir()) == original_files


def test_final_table_loss_downgrades_stored_fragment_fulfillment(tmp_path):
    docx, matrix_path, _ = setup_run(tmp_path, with_table=False)
    assert json.loads(matrix_path.read_text('utf-8'))['rows'][0]['fulfillment']['status'] == 'FULFILLED'
    report = refresh_final_scoring(tmp_path, docx)
    measured = json.loads(matrix_path.read_text('utf-8'))
    assert report['status'] == 'VERIFIED' and not report['completed_clean_allowed']
    assert measured['rows'][0]['fulfillment']['status'] == 'PARTIAL'
    assert 'STRUCTURED_CARRIER' in measured['rows'][0]['fulfillment']['missing_dimensions']
    assert final_scoring_binding_matches(measured, docx)


def test_payload_only_evidence_absent_from_final_docx_cannot_fulfill(tmp_path):
    docx, matrix_path, _ = setup_run(tmp_path)
    doc = Document()
    doc.add_heading(TITLE, level=2)
    doc.add_paragraph('甲方与现场人员核对通知接口，办理通知登记。')
    doc.save(docx)
    report = refresh_final_scoring(tmp_path, docx)
    measured = json.loads(matrix_path.read_text('utf-8'))
    assert report['status'] == 'VERIFIED' and not report['completed_clean_allowed']
    assert measured['rows'][0]['fulfillment']['status'] == 'MISSING'
    assert not measured['rows'][0]['fulfillment']['selected_evidence']


def test_equal_CU_anchors_remain_ambiguous_and_supply_no_evidence(tmp_path):
    docx, matrix_path, _ = setup_run(tmp_path, ambiguous=True)
    report = refresh_final_scoring(tmp_path, docx)
    measured = json.loads(matrix_path.read_text('utf-8'))
    assert report['artifact_binding']['aligned_blocks'] == 0
    assert measured['rows'][0]['fulfillment']['status'] == 'MISSING'
    assert not report['completed_clean_allowed']


@pytest.mark.parametrize('failure', ['missing_docx', 'invalid_docx', 'missing_fragments', 'identity_mismatch'])
def test_failure_is_visible_and_preserves_prior_matrix_bytes(tmp_path, failure):
    docx, matrix_path, _ = setup_run(tmp_path)
    prior = matrix_path.read_bytes()
    expected = None
    if failure == 'missing_docx':
        docx.unlink()
    elif failure == 'invalid_docx':
        docx.write_text('not an office document', encoding='utf-8')
    elif failure == 'missing_fragments':
        (tmp_path / 'longform/word/sections/S1/fragment.json').unlink()
    else:
        expected = '0' * 64
    report = refresh_final_scoring(tmp_path, docx, expected_artifact_sha256=expected)
    assert report['status'] == 'FAILED' and report['reason_code']
    assert report['prior_matrix_preserved'] and not report['may_claim_final_scoring']
    assert not report['completed_clean_allowed']
    assert matrix_path.read_bytes() == prior
    assert json.loads((tmp_path / 'final_scoring_report.json').read_text('utf-8')) == report
    assert not list(tmp_path.glob('.final_scoring_*'))


def test_final_SHA_binding_rejects_a_later_readable_artifact_change(tmp_path):
    docx, matrix_path, _ = setup_run(tmp_path)
    sha = hashlib.sha256(docx.read_bytes()).hexdigest()
    assert refresh_final_scoring(tmp_path, docx, expected_artifact_sha256=sha)['status'] == 'VERIFIED'
    measured = json.loads(matrix_path.read_text('utf-8'))
    assert final_scoring_binding_matches(measured, docx)
    doc = Document(docx)
    doc.add_paragraph('新的甲方确认事项登记到接口记录。')
    doc.save(docx)
    assert not final_scoring_binding_matches(measured, docx)


def test_artifact_change_during_scoring_preserves_prior_matrix(tmp_path, monkeypatch):
    docx, matrix_path, _ = setup_run(tmp_path)
    prior = matrix_path.read_bytes()
    real_update = update_scoring_fulfillment

    def update_then_change(*args, **kwargs):
        result = real_update(*args, **kwargs)
        doc = Document(docx)
        doc.add_paragraph('执行期间发生的新变更。')
        doc.save(docx)
        return result

    monkeypatch.setattr('planning.hierarchy.update_scoring_fulfillment', update_then_change)
    report = refresh_final_scoring(tmp_path, docx)
    assert report['reason_code'] == 'FINAL_SCORING_ARTIFACT_CHANGED_DURING_CHECK'
    assert matrix_path.read_bytes() == prior


def test_scale_scoring_ratio_uses_I_technical_denominator_including_global(tmp_path):
    records = [{'responsiveness_class': 'TECHNICAL_BODY_DIRECT', 'fulfillment': {'status': 'FULFILLED'}},
               {'responsiveness_class': 'WHOLE_DOCUMENT_RUBRIC', 'fulfillment': {'status': 'PARTIAL'}},
               {'responsiveness_class': 'CROSS_FILE_PROTECTED_EVIDENCE', 'fulfillment': {'status': 'MISSING'}},
               {'responsiveness_class': 'CONTRACTUAL_CONTEXT', 'fulfillment': {'status': 'NOT_GENERATED'}}]
    matrix_path = tmp_path / '09_scoring_matrix.json'
    write_json(matrix_path, {'canonical_scoring_records': records})
    assert _scoring_fulfillment_ratio(tmp_path) == 0.5
    records[1]['fulfillment']['status'] = 'FULFILLED'
    write_json(matrix_path, {'canonical_scoring_records': records})
    assert _scoring_fulfillment_ratio(tmp_path) == 1.0
    records[1]['fulfillment']['status'] = 'NOT_GENERATED'
    write_json(matrix_path, {'canonical_scoring_records': records})
    assert _scoring_fulfillment_ratio(tmp_path) is None


def test_no_technical_acceptance_topics_does_not_create_clean_coverage(tmp_path):
    write_json(tmp_path / '09_scoring_matrix.json', {'canonical_scoring_records': [
        {'responsiveness_class': 'CONTRACTUAL_CONTEXT', 'fulfillment': {'status': 'FULFILLED'}}]})
    assert _scoring_fulfillment_ratio(tmp_path) == 0.0


def test_no_matrix_is_explicitly_not_applicable(tmp_path):
    report = refresh_final_scoring(tmp_path, tmp_path / 'missing.docx')
    assert report['status'] == 'NOT_APPLICABLE'
    assert not report['may_claim_final_scoring']


@pytest.mark.parametrize('with_table', [True, False])
def test_delivery_reads_final_carriers_and_keeps_partial_visible(tmp_path, with_table):
    from app_core import _refresh_final_scoring_delivery
    from longform.run_state import _scoring_gate_facts
    docx, _, _ = setup_run(tmp_path, with_table=with_table)
    target = tmp_path / 'artifact/final.docx'
    target.parent.mkdir()
    docx.replace(target)
    qa = {'status': 'PASS', 'findings': [], 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
    result = _refresh_final_scoring_delivery(tmp_path, target, qa)
    assert result['status'] == ('PASS' if with_table else 'NEEDS_REVIEW')
    assert result['final_scoring']['completed_clean_allowed'] is with_table
    assert result['summary']['needs_review_count'] == int(not with_table)
    facts = _scoring_gate_facts(tmp_path)
    assert facts['scoring_artifact_binding_state'] == 'BOUND'
    assert facts['scoring_gate_state'] == ('COVERAGE_OK' if with_table else 'BELOW_GATE')


@pytest.mark.parametrize('changed', ['artifact', 'matrix', 'missing_matrix', 'report_failed'])
def test_terminal_consumer_rejects_stale_or_failed_final_measurement(tmp_path, changed):
    from longform.run_state import _scoring_gate_facts
    docx, matrix_path, _ = setup_run(tmp_path)
    target = tmp_path / 'artifact/final.docx'
    target.parent.mkdir()
    docx.replace(target)
    assert refresh_final_scoring(tmp_path, target)['completed_clean_allowed']
    assert _scoring_gate_facts(tmp_path)['scoring_gate_state'] == 'COVERAGE_OK'
    if changed == 'artifact':
        document = Document(target)
        document.add_paragraph('交付件已发生后续修改。')
        document.save(target)
    elif changed == 'matrix':
        matrix_path.write_bytes(matrix_path.read_bytes() + b' ')
    elif changed == 'missing_matrix':
        matrix_path.unlink()
    else:
        write_json(tmp_path / 'final_scoring_report.json', {'status': 'FAILED'})
    facts = _scoring_gate_facts(tmp_path)
    assert facts['scoring_artifact_binding_state'] == 'FAILED_OR_STALE'
    assert facts['scoring_gate_state'] == 'NOT_MEASURED'
    assert facts['scoring_technical_coverage'] is None


def test_failed_delivery_measurement_blocks_without_destroying_valid_matrix(tmp_path):
    from app_core import _refresh_final_scoring_delivery
    docx, matrix_path, _ = setup_run(tmp_path)
    prior = matrix_path.read_bytes()
    (tmp_path / 'longform/word/sections/S1/fragment.json').unlink()
    result = _refresh_final_scoring_delivery(tmp_path, docx, {'status': 'PASS', 'findings': []})
    assert result['status'] == 'BLOCK'
    assert result['findings'][0]['qa_class'] == 'TRUE_BLOCK'
    assert result['summary']['block_count'] == 1
    assert matrix_path.read_bytes() == prior


def test_records_without_rows_are_failed_not_inapplicable(tmp_path):
    write_json(tmp_path / '09_scoring_matrix.json', {'canonical_scoring_records': [
        {'responsiveness_class': 'TECHNICAL_BODY_DIRECT'}]})
    report = refresh_final_scoring(tmp_path, tmp_path / 'missing.docx')
    assert report['status'] == 'FAILED'
    assert report['reason_code'] == 'FINAL_SCORING_ROWS_MISSING'


def test_historical_unbound_reading_is_identified_explicitly(tmp_path):
    from longform.run_state import _scoring_gate_facts
    setup_run(tmp_path)
    facts = _scoring_gate_facts(tmp_path)
    assert facts['scoring_artifact_binding_state'] == 'LEGACY_NOT_BOUND'
