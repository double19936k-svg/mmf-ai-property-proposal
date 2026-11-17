"""Positional source/renderer contracts. A single lost value cannot be averaged away."""
import copy
import hashlib
import json
import re
from docx import Document

KEY = 'table_source_contract'

class TableFidelityError(ValueError):
    pass

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def _label_list(value):
    if isinstance(value, (list, tuple)):
        return ["" if item is None else item for item in value]
    return None


def _keep_for_review(table, reason):
    updated = dict(table)
    updated["needs_review"] = True
    updated["table_review_reason"] = reason
    return updated


def coerce_table(table):
    """Turn a count-shaped table into columns plus rows.

    A column count that disagrees with the headers, uneven row widths, or a
    non-list ``rows`` value stays as the provider sent it and is marked for
    review. Only a flat cell list that divides evenly by a known width is
    reconstructed.
    """
    if not isinstance(table, dict):
        return table
    names = _label_list(table.get("columns"))
    header_names = _label_list(table.get("headers"))
    stated_count = table.get("columns") if isinstance(table.get("columns"), int) and not isinstance(table.get("columns"), bool) else None
    rows = table.get("rows")
    if rows is not None and rows != "" and not isinstance(rows, (list, tuple)):
        return _keep_for_review(table, "rows 不是数组，列归属无法确定，保留原值待复核。")
    if stated_count and header_names and stated_count != len(header_names):
        return _keep_for_review(table, "列数与表头不一致，列归属有歧义，保留原始数据待核对。")
    if names is None:
        names = header_names
    if not isinstance(rows, (list, tuple)):
        rows = []
    else:
        rows = list(rows)
    width = len(names) if names is not None else 0
    if width == 0:
        count = table.get("columns") if isinstance(table.get("columns"), int) else table.get("headers")
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            width = count
    list_rows = [row for row in rows if isinstance(row, (list, tuple))]
    widths = {len(row) for row in list_rows}
    if len(widths) > 1:
        return _keep_for_review(table, "各行宽度不一致，保留原值待复核。")
    if rows and width > 1 and all(isinstance(row, str) for row in rows) and len(rows) % width == 0:
        rows = [list(rows[index:index + width]) for index in range(0, len(rows), width)]
    else:
        rebuilt = []
        for row in rows:
            if isinstance(row, (list, tuple)):
                rebuilt.append(list(row))
            elif isinstance(row, dict):
                if names is None:
                    names = list(row.keys())
                    width = len(names)
                rebuilt.append([row.get(name, "") for name in names])
            elif row is not None and row != "":
                rebuilt.append([row])
        rows = rebuilt
    if names is None:
        span = width
        for row in rows:
            span = max(span, len(row))
        names = [f"列{index + 1}" for index in range(span)]
    else:
        names = list(names)
    if names and any(len(row) != len(names) for row in rows):
        return _keep_for_review(table, "各行宽度与表头不一致，保留原值待复核。")
    updated = dict(table)
    updated["columns"] = names
    updated["rows"] = rows
    return updated


def matrix(table):
    if isinstance(table, dict) and table.get("needs_review"):
        return [["待复核"], [str(table.get("table_review_reason") or ""), json.dumps(
            {"columns": table.get("columns"), "rows": table.get("rows")}, ensure_ascii=False)]]
    rows = table.get('rows') or []
    if not isinstance(rows, (list, tuple)):
        table = coerce_table(table)
        if table.get("needs_review"):
            return matrix(table)
        rows = table.get("rows") or []
    if isinstance(rows, (list, tuple)) and any(isinstance(row, dict) for row in rows):
        from longform.local_normalize import normalize_table
        normalized, _ = normalize_table(table)
        if normalized is None:
            raise TableFidelityError('source table cannot be normalized positionally')
        table = normalized
    elif not isinstance(table.get('columns'), (list, tuple)) or (
            isinstance(rows, (list, tuple)) and any(not isinstance(row, (list, tuple)) for row in rows)):
        table = coerce_table(table)
    return [list(table.get('columns') or table.get('headers') or []),
            *[list(row) if isinstance(row, (list, tuple)) else [row] for row in table.get('rows') or []]]

def source_contract(table, cu_id):
    values = copy.deepcopy(matrix(table))
    return {'schema_version':'cu-table-cell-contract-v1', 'content_unit_id':cu_id,
            'source_values':values, 'source_values_digest':digest(values)}

def normalize(value):
    text = re.sub(r'\s+', '', str(value or ''))
    digits = dict(zip('零〇一二两三四五六七八九', [0,0,1,2,2,3,4,5,6,7,8,9]))
    def equivalent(match):
        face = match.group(1)
        if all(c in digits for c in face):
            return str(int(''.join(str(digits[c]) for c in face)))
        from planning.scoring_context import day_value
        result = day_value(face)
        return str(result) if result is not None else face
    # Same quantity, same unit only. A changed value/unit/role still differs.
    return re.sub(r'([零〇一二两三四五六七八九十百千]+)(?=平方米|小时|分钟|人|名|次|天|日|年|月|㎡)', equivalent, text)

def compare(expected, actual, path):
    findings = []
    if len(expected) != len(actual) or any(len(a) != len(b) for a,b in zip(expected,actual)):
        findings.append({'rule_id':'TABLE_SOURCE_FIDELITY','path':path,'severity':'BLOCK',
                         'issue':'source/rendered table shape differs'})
    for ri, row in enumerate(expected):
        for ci, value in enumerate(row):
            got = actual[ri][ci] if ri < len(actual) and ci < len(actual[ri]) else None
            if normalize(value) != normalize(got):
                findings.append({'rule_id':'TABLE_SOURCE_FIDELITY','path':f'{path}/row/{ri}/cell/{ci}',
                                 'severity':'BLOCK','issue':'source-bound cell differs',
                                 'expected':str(value),'actual':str(got or '')})
    return findings

def check_source_table(table):
    contract = table.get(KEY)
    if not contract:
        return []  # Legacy carriers have no asserted binding; don't certify them.
    if not isinstance(contract, dict):
        return [{'rule_id':'TABLE_SOURCE_FIDELITY','path':str(table.get('source_content_unit_id') or 'table'),
                 'severity':'BLOCK','issue':'invalid source cell contract'}]
    raw = contract.get('source_values')
    if not _valid_source_values(raw) or digest(raw) != contract.get('source_values_digest'):
        return [{'rule_id':'TABLE_SOURCE_FIDELITY','path':str(contract.get('content_unit_id')),
                 'severity':'BLOCK','issue':'invalid source cell contract'}]
    try:
        actual = matrix(table)
    except (ValueError, TypeError) as exc:
        return [{'rule_id':'TABLE_SOURCE_FIDELITY','path':str(contract.get('content_unit_id')),
                 'severity':'BLOCK','issue':'unrenderable table matrix: '+str(exc)}]
    numeric_drift = []
    for ri, row in enumerate(raw):
        for ci, value in enumerate(row):
            got = actual[ri][ci] if ri < len(actual) and ci < len(actual[ri]) else ""
            tokens = lambda text: re.findall(r"\d+(?:\.\d+)?(?:[%％])?", normalize(text))
            if tokens(value) != tokens(got):
                numeric_drift.append({"rule_id":"TABLE_SOURCE_FIDELITY", "path":f"{contract.get('content_unit_id')}/row/{ri}/cell/{ci}", "severity":"BLOCK", "issue":"source numeric tokens changed", "expected":str(value), "actual":str(got)})
    if numeric_drift:
        return numeric_drift
    # Exact preservation is always faithful; editorial normalization is an
    # alternative display form, not a requirement to change verified cells.
    if not compare(raw, actual, str(contract.get('content_unit_id'))):
        return []
    # The same deterministic cleaner defines permitted customer prose changes.
    # An empty result from a nonempty source still requires explicit repair.
    values = _source_display(raw)
    lost = [(r,c) for r,row in enumerate(raw) for c,v in enumerate(row)
            if normalize(v) and not normalize(values[r][c])]
    if lost:
        return [{'rule_id':'TABLE_SOURCE_FIDELITY','path':str(contract.get('content_unit_id')),
                 'severity':'BLOCK','issue':'cleaner removed a nonempty source cell','cells':lost}]
    return compare(values, matrix(table), str(contract.get('content_unit_id')))


def _source_display(raw):
    from .artifact_qa import apply_artifact_repairs
    from .commitment_provenance import apply_local_repairs
    from .final_editorial import SourceFacts, repair_with_source_facts
    from longform.factory import _repair_customer_text
    class CellContractFacts(SourceFacts):
        def numeric_binding(self, text, lo, hi):
            # Numeric equality with the immutable cell contract was checked
            # above. This supports display equivalence, not tender facts.
            return {'basis':'POSITIONAL_CELL_NUMERIC_IDENTITY'}
    local_source = CellContractFacts()
    expected = repair_with_source_facts({'columns':raw[0], 'rows':raw[1:]},
        lambda value: _repair_customer_text(apply_artifact_repairs(apply_local_repairs(value)), local_source), local_source)
    return matrix(expected)


def _valid_source_values(raw):
    return bool(isinstance(raw, list) and raw and isinstance(raw[0], list) and raw[0]
                and all(isinstance(row, list) and len(row) == len(raw[0]) for row in raw))


def project_source_table(table):
    """Recover display from an intact CU receipt; never repair an invalid receipt."""
    contract = table.get(KEY) or {}
    if not isinstance(contract, dict):
        return None
    raw = contract.get('source_values')
    if not _valid_source_values(raw) or digest(raw) != contract.get('source_values_digest'):
        return None
    values = _source_display(raw)
    if compare(raw, values, str(contract.get('content_unit_id'))):
        # Editorial changes are permitted, but no whole nonempty cell is lost.
        if any(normalize(v) and not normalize(values[r][c])
               for r,row in enumerate(raw) for c,v in enumerate(row)):
            return None
    projected = copy.deepcopy(table)
    projected['columns'], projected['rows'] = values[0], values[1:]
    if 'headers' in projected:
        projected['headers'] = values[0]
    return projected if not check_source_table(projected) else None

def require_source_tables(value):
    if isinstance(value,dict):
        if KEY in value:
            findings = check_source_table(value)
            if findings:
                raise TableFidelityError(json.dumps(findings, ensure_ascii=False))
        for key, child in value.items():
            if key != KEY:
                require_source_tables(child)
    elif isinstance(value,list):
        for child in value:
            require_source_tables(child)

def final_table_findings(path, run_dir):
    if run_dir is None:
        return []
    witness = run_dir / 'artifact_table_build_metrics.json'
    if not witness.is_file():
        return []
    receipt = json.loads(witness.read_text(encoding='utf-8-sig'))
    contracts = receipt.get('rendered_cell_contracts')
    if contracts is None:
        return []  # A historical metric is not a cell-fidelity claim.
    doc = Document(str(path))
    findings = []
    origin = receipt.get('revision_source_origin')
    source_input=receipt.get('source_input_receipt')
    if source_input and not origin:
        from pathlib import Path
        from filesystem_paths import filesystem_path as fs
        from .table_qa import plan_section_tables
        try:
            root=Path(run_dir).resolve();source=(root/source_input['file']).resolve()
            if not source.is_relative_to(root):raise ValueError('assembly input escapes run')
            raw=fs(source).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=source_input['sha256']:
                raise ValueError('assembly source input identity changed')
            artifact=json.loads(raw.decode('utf-8-sig'))['artifact'];expected=[]
            findings.extend(artifact.get('source_table_findings') or [])
            for section in artifact.get('sections',[]):
                if not str(section.get('heading','')).strip():continue
                tables=[t for t in section.get('tables',[]) if isinstance(t,dict)]
                if isinstance(section.get('table'),dict):tables.append(section['table'])
                for table in tables:
                    findings.extend(check_source_table(table))
                for plan in plan_section_tables(tables)['tables']:
                    expected.append(matrix(plan))
            if len(expected)!=len(contracts):raise ValueError('assembly input table count differs')
            for ti,values in enumerate(expected):
                findings.extend(compare(values,contracts[ti].get('values',[]),f'table[{ti}]/registration'))
        except (ValueError,KeyError,IndexError,OSError) as exc:
            findings.append({'rule_id':'TABLE_SOURCE_FIDELITY','path':str(path),'severity':'BLOCK','issue':str(exc)})
    if origin:
        from pathlib import Path
        from filesystem_paths import filesystem_path as fs
        from longform.local_revision import source_evidence
        try:
            root=Path(run_dir).resolve()
            source=(root/origin['file']).resolve()
            manifest=(root/origin['manifest_file']).resolve()
            if not source.is_relative_to(root) or not manifest.is_relative_to(root):
                raise ValueError('revision source escapes run')
            if hashlib.sha256(fs(source).read_bytes()).hexdigest()!=origin['sha256']:
                raise ValueError('revision original artifact changed')
            raw=fs(manifest).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=origin['manifest_sha256']:
                raise ValueError('revision source manifest changed')
            operations=json.loads(raw.decode('utf-8-sig'))['operations']
            old=Document(fs(source))
            expected=[[[c.text for c in r.cells] for r in t.rows] for t in old.tables]
            for operation in operations:
                if operation['kind']!='replace_cell':continue
                match=re.fullmatch(r'table\[(\d+)\]/row\[(\d+)\]/cell\[(\d+)\]',operation['anchor'])
                if match is None:raise ValueError('invalid revision cell locator')
                ti,ri,ci=map(int,match.groups())
                if hashlib.sha256(expected[ti][ri][ci].encode()).hexdigest()!=operation['anchor_text_sha256']:
                    raise ValueError('revision cell original anchor changed')
                for evidence in operation['source_evidence']:source_evidence(root,evidence)
                from .artifact_qa import apply_artifact_repairs
                choices=[v for e in operation['source_evidence'] for v in
                         (e['quote'],apply_artifact_repairs({'text':e['quote']})['text'])]
                if operation['new'] not in choices:raise ValueError('revision cell has no source projection')
                expected[ti][ri][ci]=operation['new']
            if len(expected)!=len(contracts):raise ValueError('revision table count changed')
            for ti,values in enumerate(expected):
                findings.extend(compare(values,contracts[ti].get('values',[]),f'table[{ti}]/registration'))
        except (ValueError,KeyError,IndexError,OSError) as exc:
            findings.append({'rule_id':'TABLE_SOURCE_FIDELITY','path':str(path),'severity':'BLOCK','issue':str(exc)})
    if len(contracts) != len(doc.tables):
        findings.append({'rule_id':'TABLE_SOURCE_FIDELITY','path':str(path),'severity':'BLOCK',
                         'issue':'rendered table count differs from assembly registration'})
    for index, contract in enumerate(contracts):
        expected = contract.get('values')
        if digest(expected) != contract.get('values_digest'):
            findings.append({'rule_id':'TABLE_SOURCE_FIDELITY','path':f'table[{index}]',
                             'severity':'BLOCK','issue':'cell registration digest mismatch'})
            continue
        actual = [[cell.text for cell in row.cells] for row in doc.tables[index].rows] if index < len(doc.tables) else []
        findings.extend(compare(expected, actual, f'table[{index}]'))
    return findings
