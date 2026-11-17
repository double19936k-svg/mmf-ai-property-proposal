"""Keep a table cell's prohibition supplied by its column heading."""
from copy import deepcopy
import re

NEGATIVE_COLUMN = re.compile(r"^(?:不得|禁止|不应|不可|不允许|应避免|避免).{0,24}$")
BUSINESS_HEADER = re.compile(r'^[\u4e00-\u9fff]{2,20}(?:标准|依据|要求|方法|方式|条件|内容|岗位|责任人|结果)$')


def is_business_header(value):
    text=str(value).strip()
    return bool(BUSINESS_HEADER.fullmatch(text) and not re.search(r'写作|推理|评分|分值|生成|自检',text))


def is_business_row_label(value):
    """A short noun label in column zero is not an authoring instruction."""
    text = str(value).strip()
    return bool(2 <= len(text) <= 48 and not re.search(r'[。；;!?！？\n]|本单元|本章|本节|正文|写作|推理|生成|自检|只写|不写|写入|应当|必须|不得', text)
                and re.search(r'项目名称|项目编号|建筑面积|服务期限|人员配置|岗位职责|设备|移交|记录|档案', text))


def negative_columns(columns):
    return {i for i, value in enumerate(columns or []) if NEGATIVE_COLUMN.fullmatch(str(value).strip())}


def visit_table(value, visit, structural):
    if not isinstance(value, dict):
        return None
    columns = value.get('columns', value.get('headers'))
    rows = value.get('rows')
    if not isinstance(columns, list) or not isinstance(rows, list) or not all(isinstance(row, (list, tuple)) for row in rows):
        return None
    negative = negative_columns(columns)
    out = {}
    for key, item in value.items():
        if structural(str(key)):
            out[key] = deepcopy(item)
        elif key in {'columns', 'headers'}:
            headers=[]
            for i,cell in enumerate(item):
                cleaned=deepcopy(cell) if i in negative else visit(cell)
                # A business label is structural context, not a prose sentence.
                # Keep only ordinary short labels that a prose cleaner erased;
                # internal authoring labels and other repairs remain governed.
                if isinstance(cell,str) and not cleaned and is_business_header(cell):
                    cleaned=cell
                headers.append(cleaned)
            out[key]=headers
        elif key == 'rows':
            out[key] = [[deepcopy(cell) if i in negative or (i == 0 and is_business_row_label(cell))
                         else visit(cell) for i, cell in enumerate(row)] for row in item]
        else:
            out[key] = visit(item)
    return out


def restore_negative_cells(table):
    """Recover only cleaner-erased prohibition cells from an intact CU receipt."""
    from .table_fidelity import digest
    contract = table.get('table_source_contract') or {}
    if not isinstance(contract, dict):
        return table, []
    raw = contract.get('source_values')
    if not raw or digest(raw) != contract.get('source_values_digest'):
        return table, []
    result = deepcopy(table)
    rows = result.get('rows') or []
    columns = result.get('columns', result.get('headers')) or []
    if not all(isinstance(r, list) for r in rows):
        return table, []
    repaired = []
    for ci in negative_columns(columns):
        for ri, row in enumerate(rows, 1):
            if ci < len(row) and ri < len(raw) and ci < len(raw[ri]) and not str(row[ci]).strip() and str(raw[ri][ci]).strip():
                row[ci] = raw[ri][ci]
                repaired.append({'content_unit_id':contract.get('content_unit_id'),'row':ri,'cell':ci,'source_digest':contract['source_values_digest'],'reason':'RESTORE_PROHIBITION_FROM_IMMUTABLE_CU_CELL'})
    return result, repaired
