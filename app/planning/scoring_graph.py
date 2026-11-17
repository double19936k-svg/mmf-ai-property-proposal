"""MMF008A2-A：评分项的**结构身份**取自评分表的行列，不取自打平后的那一串文本。

上一轮（MMF008A）把"评分归属 × 技术正文可响应性"两维补齐之后，Reviewer 独立复核仍然判失败，
理由不在分类词，而在**主题是从哪儿来的**：canonical 主题由打平后的评分行按文本相似度折叠而成，
于是——

    服务保障能力（5分）  的抽取行是"全面性、可行性、有效性、及时响应性等方面横向比较进行打分"
    项目退出移交方案（3分）的抽取行是"根据是否能够妥善移交退出以及方案的科学可行性"

这两串都只是评分表**评分标准列的残段**：事项名（服务保障能力 / 项目退出移交方案）与分值
在**同一行的另一列**里。残段里没有事项名词、还全是评价语，分类器只能把它读成"整本质量"或
"上下文"，5 分与 3 分就这样从分母里消失了。反过来，"项目管理团队（8分）"这一件事因为
被抽成三条不同措辞的行，又被按三个独立主题重复计分；而"项目管理（4分）"被拆成
优势 2 分 + 建议 2 分两行时，其中一行根本没被抽出来，2 分整条不见。

本模块因此只认**表格结构**：`scoring row → parent rubric → independently respondable child
→ response location`。列角色按**列的形状**认（括号分值密度、区分度、平均长度、纯数字占比），
**不按表头名认**——本标书表头写着"分值"的那一列实际装的是"评分因素 + 分值"，
靠列名第一步就会走歪。父项 = 同一卷次下重复出现的评分因素；可独立响应叶子 = 一行评分表；
被拆分的父项按各行"得 N 分"落到子项，落不出来才按行数均分。分值一律从单元格现读。

三条边界：
1. **不硬编码任何本项目的分值、条数或事项名**。"7 个父项 / 8 个叶子 / 40 分"是本次解析这份
   招标文件得到的**结果**，不是这里的输入；换一份评分表（分值单列成纯数字列、表头另一套名字）
   同一套角色与分组规则照样跑，`synthetic` 反例文件就是钉这一条的。
2. 表格结构拿不到（无评分表、无带分值/无名列、只有卷次汇总没有评分标准列）时**明确报
   NOT_DERIVABLE** 并把每张表的列形状留在 `skipped_tables` 里，调用方退回上一轮的按行折叠，
   不假装建成过图。
3. 叶子与抽取行的对应关系逐条留凭据（`match_basis` + 命中的那一格原文）；一个叶子没有任何
   抽取行（漏抽）、或抽取行没落成任何叶子，都必须点名可见 —— 漏失与重复计分都长在"看不见"里。
"""
from __future__ import annotations

import re
from typing import Any

SCORING_GRAPH_VERSION = "scoring-graph-v0.2"
BASIS_TABLE_CELLS = "TENDER_SCORING_TABLE_CELLS"
BASIS_NOT_DERIVABLE = "NO_SCORING_TABLE_CELL_STRUCTURE"

# 分值写法：括号里的（8分）是"这一项值几分"，句子里的"得2分"是"这一档给几分"。
# 两者都只按数字+分的形式认，不认任何具体事项名。
_PAREN_WEIGHT = re.compile(r"[(（]\s*(\d+(?:\.\d+)?)\s*分\s*[)）]")
_GAINED_WEIGHT = re.compile(r"得\s*(\d+(?:\.\d+)?)\s*分")
_GAINED_BAND = re.compile(r"得\s*(\d+(?:\.\d+)?)\s*[-－~至到]\s*(\d+(?:\.\d+)?)\s*分")
_TRAILING_WEIGHT = re.compile(r"(?<![\d.])[①-⑩]?\s*(\d+(?:\.\d+)?)\s*分$")
_CLAUSE_CUT = re.compile(r"[，,。；;：:]")
_COMPARISON_TAIL = re.compile(r"横向比较|比较打分|进行评分|进行打分|评分|打分|优得|一般得|最高得")
_WHITESPACE = re.compile(r"\s+")
# 序号/计数列：整列都是纯数字或"1."这类，不参与列角色竞争。
_INDEX_ONLY_CELL = re.compile(r"^\d{1,3}[.、)）]?$")
# 纯分值列：整格只有"20"或"20分"。它能供分值，不能供事项名。
_PURE_WEIGHT_CELL = re.compile(r"^\d+(?:\.\d+)?\s*分?$")
# 评分标准列的最低门槛：短过这个数的列装不下"怎么评"，只能是名称或分值。
_RUBRIC_MIN_AVG_CHARS = 20
# 事项名列不得比评分标准列长一半以上，否则它是"整段说明"而不是"这一项叫什么"。
_FACTOR_LEN_VS_RUBRIC = 0.5
# 卷次列的区分度上限：反复出现同样几个取值的列是卷次，逐行都不同的是评分因素。
_VOLUME_MAX_VARIANCE = 0.6


def _norm(value: Any) -> str:
    return _WHITESPACE.sub("", str(value if value is not None else ""))


def _without_weight(value: Any) -> str:
    text = _norm(_PAREN_WEIGHT.sub("", value))
    return _TRAILING_WEIGHT.sub("", text).strip()


def _cells(row: list[Any]) -> list[str]:
    return [str(item if item is not None else "") for item in row]


def _paren_weight(cell: Any) -> float | None:
    match = _PAREN_WEIGHT.search(_norm(cell))
    return float(match.group(1)) if match else None


def _trailing_weight(cell: Any) -> float | None:
    """短格子里"名字+分值"连着写的形态（`总报价 / 20分`、`20分`）。"""
    text = _norm(cell)
    if len(text) > 16:
        return None
    match = _TRAILING_WEIGHT.search(text)
    return float(match.group(1)) if match else None


def _gained_weight(cell: Any) -> float | None:
    """取一行评分标准里"最高档给几分"：区间取上界，逐档取最大值。"""
    text = _norm(cell)
    values = [float(value) for value in _GAINED_WEIGHT.findall(text)]
    values += [float(high) for _, high in _GAINED_BAND.findall(text)]
    return max(values) if values else None


def _pure_weight_value(cell: Any) -> float | None:
    """纯分值格（`20`、`20分`）取数；不是这种形态就返回 None。"""
    text = _norm(cell)
    return float(text.rstrip("分")) if _PURE_WEIGHT_CELL.match(text) else None


def _cell_weight(cell: Any) -> tuple[float | None, str]:
    paren = _paren_weight(cell)
    if paren is not None:
        return paren, "FACTOR_CELL_PARENTHESIZED_WEIGHT"
    trailing = _trailing_weight(cell)
    if trailing is not None:
        return trailing, "FACTOR_CELL_TRAILING_WEIGHT"
    return None, "NO_WEIGHT_FOUND"


def _column_stats(rows: list[list[str]]) -> list[dict[str, Any]]:
    width = max((len(row) for row in rows), default=0)
    stats: list[dict[str, Any]] = []
    for index in range(width):
        cells = [_norm(row[index]) for row in rows if len(row) > index and _norm(row[index])]
        if not cells:
            stats.append({"index": index, "filled": 0, "avg_len": 0.0, "paren_weight_marks": 0,
                          "weight_marks": 0, "distinct": 0, "variance": 0.0,
                          "index_only": True, "pure_weight_share": 0.0, "gained_marks": 0})
            continue
        named = {_without_weight(cell) for cell in cells if _without_weight(cell)}
        weight_marks = sum(1 for cell in cells if _paren_weight(cell) is not None
                           or _trailing_weight(cell) is not None)
        numeric = [cell for cell in cells if _INDEX_ONLY_CELL.match(cell)]
        sequential = [int(re.sub(r"[^\d]", "", cell) or 0) for cell in numeric]
        stats.append({
            "index": index,
            "filled": len(cells),
            "avg_len": sum(len(cell) for cell in cells) / len(cells),
            "paren_weight_marks": sum(1 for cell in cells if _paren_weight(cell) is not None),
            "weight_marks": weight_marks,
            "distinct": len(named),
            "variance": len(named) / max(len(cells), 1),
            # 序号列 = 逐格只有数字，且**自上而下连号**（1,2,3…）。"25/10/8/7" 也全是数字，
            # 但它不连号，那是分值列；把两种列混为一谈就会把分值列当成序号整列丢掉。
            "index_only": bool(sequential) and len(numeric) == len(cells)
            and sequential == list(range(sequential[0], sequential[0] + len(sequential))),
            "numeric_share": len(numeric) / len(cells),
            "pure_weight_share": sum(1 for cell in cells if _PURE_WEIGHT_CELL.match(cell)) / len(cells),
            "gained_marks": sum(1 for cell in cells if _gained_weight(cell) is not None),
        })
    return stats


def _naming_columns(stats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in stats
            if item["filled"] and not item["index_only"] and item["pure_weight_share"] < 0.6]


def _column_roles(rows: list[list[str]]) -> tuple[dict[str, int | None], list[dict[str, Any]]]:
    """按列的**形状**认列角色：卷次 / 评分因素 / 评分标准 / 纯分值列。

    评分因素必须同时满足"逐行取值不同（区分度高）"和"不是整段说明（长度远短于评分标准列）"；
    卷次是同一列里反复出现的那一个；评分标准是最长且本身不标价分值的列。
    表头名一概不用：本标书"分值"列装的是事项名+分值，"评分因素及权重"列装的是卷次。
    """
    stats = _column_stats(rows)
    naming = _naming_columns(stats)
    roles: dict[str, int | None] = {"volume": None, "factor": None, "rubric": None, "weight_col": None}
    if not naming:
        return roles, stats
    rubric_pool = [item for item in naming
                   if item["paren_weight_marks"] == 0 and item["avg_len"] >= _RUBRIC_MIN_AVG_CHARS]
    rubric = max(rubric_pool, key=lambda item: item["avg_len"])["index"] if rubric_pool else None
    # 独立分值列的列号。它同时是"事项名列"的第二条正面证据：`序号 | 评审因素 | 分值 | 评审标准`
    # 这种最普通的四列表里，分值单列成数字，事项名那一格**自己不带任何分值标记**，只按
    # "有分值标记"选列就会落到反复出现的卷次格上（30 行的表里卷次列的方差必然最低，
    # 于是卷次当因子、叶子从评分标准里截，事项名与 volume 双双丢失）。所以认两种形状之一：
    # 本格自带分值，或**紧贴**一条纯分值列。
    pure_indexes = {item["index"] for item in stats
                    if item["filled"] and not item["index_only"]
                    and item["pure_weight_share"] >= 0.6}
    factor_pool = [item for item in naming
                   if item["index"] != rubric
                   and (item["weight_marks"]
                        or any(abs(item["index"] - other) == 1 for other in pure_indexes))
                   and (rubric is None or item["avg_len"]
                        <= next(s["avg_len"] for s in stats if s["index"] == rubric) * _FACTOR_LEN_VS_RUBRIC
                   or item["paren_weight_marks"] >= max(1, item["filled"] // 2))]
    if not factor_pool:
        factor_pool = [item for item in naming
                       if item["index"] != rubric and item["avg_len"] >= 3
                       and (rubric is None or item["avg_len"]
                            <= next(s["avg_len"] for s in stats if s["index"] == rubric) * 2)]
    if rubric is not None and factor_pool:
        # 评分因素列若与评分标准列同为一列（名称与标准挤在一起），按最长列优先当标准。
        factor_pool = [item for item in factor_pool if item["index"] != rubric]
    if factor_pool:
        roles["factor"] = max(factor_pool, key=lambda item: (item["variance"], item["weight_marks"]))["index"]
    volume_pool = [item for item in factor_pool if item["index"] != roles["factor"]]
    if volume_pool and roles["factor"] is not None:
        lowest = min(volume_pool, key=lambda item: item["variance"])
        if lowest["variance"] < _VOLUME_MAX_VARIANCE:
            roles["volume"] = lowest["index"]
    if roles["factor"] is None:
        roles["rubric"] = rubric
        return roles, stats
    # 纯数字列供分值（另一份评分表常见的"分值"独立列形态）。两条约束缺一不可：
    # ①它必须**紧贴**评分因素列（真实评分表里"评审因素 | 分值"总是相邻的），否则本合同标书
    #    的"序号"列（1,2,2,2…）会被当成分值列，从序号里读出 2 分、3 分的假分值；
    # ②它不能同时是连号序号列。
    pure = [item for item in stats
            if item["filled"] and not item["index_only"] and item["pure_weight_share"] >= 0.6
            and abs(item["index"] - int(roles["factor"])) == 1
            and item["index"] not in {roles["volume"], rubric}]
    if pure:
        roles["weight_col"] = min(pure, key=lambda item: abs(item["index"] - int(roles["factor"])))["index"]
    if roles["rubric"] is None:
        roles["rubric"] = rubric
    return roles, stats


def _row_weight(factor_cell: str, column_value: float | None,
                rubric_cell: str) -> tuple[float | None, str]:
    """一行自己的分值从哪来：因素格括号 / 紧贴的纯数字分值列 / 本行"得N分"最高档。"""
    weight, basis = _cell_weight(factor_cell)
    if weight is not None:
        return weight, basis
    if column_value is not None:
        return column_value, "WEIGHT_COLUMN_CELL"
    gained = _gained_weight(rubric_cell)
    if gained is not None:
        return gained, "ROW_GAINED_SCORE"
    return None, "NO_WEIGHT_FOUND"


def _child_weights(cells: list[tuple[int, str, str, float | None]],
                   parent_weight: float) -> list[float]:
    """组内每行"这一档最高给几分"；任一行点不出来就不算子项（返回空表）。

    只有当这些分值**加起来正好等于父项分值、且每一行都严格小于父项分值**时，才认为这个父项
    被拆成了几件独立的事："项目管理（4分）"两行各"优得2分" → 2+2=4 是两件事；
    "分部分项施工方案"两行"得10分 / 得5分"里 10 就等于父项，那是同一件事的两档，不是两件事。
    """
    values: list[float] = []
    for _, _, rubric, column_value in cells:
        gained = _gained_weight(rubric)
        value = gained if gained is not None else column_value
        if value is None or value >= parent_weight:
            return []
        values.append(float(value))
    return values if len(values) == len(cells) else []


def _parent_weight(row_count: int, cells: list[tuple[int, str, str, float | None]]
                   ) -> tuple[float | None, str, bool]:
    """一组同父行 → (父项分值, 分值凭据, 是否真被拆成多件独立事项)。"""
    if row_count == 1:
        weight, basis = _row_weight(cells[0][1], cells[0][3], cells[0][2])
        return weight, basis, False
    stated = [_cell_weight(factor_cell)[0] for _, factor_cell, _, _ in cells]
    merged_mark = next((value for value in stated if value is not None), None)
    column_values = [value for _, _, _, value in cells]
    if merged_mark is not None:
        children = _child_weights(cells, merged_mark)
        split = bool(children) and abs(sum(children) - merged_mark) < 1e-9
        return merged_mark, "MERGED_FACTOR_CELL_WEIGHT", split
    if all(value is not None for value in column_values):
        total = sum(float(value) for value in column_values if value is not None)
        # 分值列逐行都标价，且父项格没标过总分：这几行各自是一件事项，父项分是它们之和。
        # 逐行同价的重复格（"8 / 8"）不是求和关系，是同一件事写了两遍 → 不拆。
        return (total, "SUM_OF_ROW_WEIGHT_COLUMN", True) if len(
            {float(value) for value in column_values if value is not None}) == len(column_values) \
            else (max(float(value) for value in column_values if value is not None),
                  "MERGED_ROW_WEIGHT_COLUMN", False)
    marked = [float(value) for value in column_values if value is not None]
    if len(marked) == 1:
        return marked[0], "MERGED_WEIGHT_COLUMN", False
    if marked:
        return max(marked), "ROW_WEIGHT_COLUMN_MAX", False
    gained = [_gained_weight(rubric) for _, _, rubric, _ in cells]
    if all(value is not None for value in gained):
        values = [float(value) for value in gained if value is not None]
        return sum(values), "SUM_OF_ROW_GAINED_SCORES", len(set(values)) == len(values)
    return None, "NO_WEIGHT_FOUND", False


def _child_topic(rubric_cell: str, sibling_cells: list[str]) -> str:
    """被拆分父项的子项名：取评分标准第一句，去掉比较语；同族多行时再剥公共前缀。"""
    text = _norm(rubric_cell)
    head = _CLAUSE_CUT.split(text)[0] if text else ""
    head = _COMPARISON_TAIL.split(head)[0] if head else ""
    if len(sibling_cells) > 1:
        commons = {_without_weight(cell)[:12] for cell in sibling_cells}
        prefix = next(iter(commons)) if len(commons) == 1 else ""
        if prefix and head.startswith(prefix) and len(head) > len(prefix):
            head = head[len(prefix):]
    return head or text[:24]


def _leaf_id(table_id: str, row_index: int) -> str:
    return f"{table_id}-R{row_index:02d}"


def build_scoring_graph(extraction: Any, scoring_items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """从招标抽取结果里的评分表建"卷次 → 父项 → 可独立响应叶子"的结构图。

    返回 `{"status", "basis", "parents", "leaves", "item_links", "by_volume", ...}`；
    `status` 只有 `DERIVED` 与 `NOT_DERIVABLE` 两种，后者带 `reason`，调用方必须把它落进
    产物而不是静默降级。
    """
    tables = [table for table in ((extraction or {}).get("tables") or []) if isinstance(table, dict)]
    scoring_tables = [table for table in tables if table.get("is_scoring_table")]
    empty = {"status": "NOT_DERIVABLE", "basis": BASIS_NOT_DERIVABLE, "version": SCORING_GRAPH_VERSION,
             "parents": [], "leaves": [], "item_links": [], "by_volume": {}}
    if not scoring_tables:
        return {**empty, "reason": "NO_SCORING_TABLE"}
    leaves: list[dict[str, Any]] = []
    parents: list[dict[str, Any]] = []
    roles_by_table: dict[str, dict[str, int | None]] = {}
    stats_by_table: dict[str, list[dict[str, Any]]] = {}
    skipped_tables: list[dict[str, Any]] = []
    for table in scoring_tables:
        table_id = str(table.get("table_id") or "")
        rows = [_cells(row) for row in (table.get("rows") or []) if isinstance(row, list)]
        roles, stats = _column_roles(rows)
        roles_by_table[table_id] = roles
        stats_by_table[table_id] = stats
        if roles.get("factor") is None:
            skipped_tables.append({"table_id": table_id, "reason": "NO_FACTOR_NAME_COLUMN",
                                   "columns": list(table.get("columns") or []), "column_stats": stats})
            continue
        if roles.get("rubric") is None:
            # 只有"卷次/事项名 → 分值"的汇总表（评标办法开头那张"价格20/商务40/技术40/合计100"）：
            # 一行都不是一件可响应的事，硬认叶子就会造出 200 分的假分母。
            # 唯一例外是"事项名与评分标准挤在同一格"的紧凑表——因素列本身就够长。
            factor_avg = next((item["avg_len"] for item in stats if item["index"] == roles["factor"]), 0.0)
            if factor_avg < _RUBRIC_MIN_AVG_CHARS:
                skipped_tables.append({"table_id": table_id, "reason": "NO_RUBRIC_COLUMN_ONLY_ROLLUP",
                                       "columns": list(table.get("columns") or []), "column_stats": stats})
                continue
            roles["rubric"] = roles["factor"]
        factor_index = int(roles["factor"])
        rubric_index = roles.get("rubric")
        weight_index = roles.get("weight_col")
        volume_index = roles.get("volume")
        grouped: dict[tuple[str, str], list[tuple[int, list[str]]]] = {}
        for position, row in enumerate(rows, start=1):
            if len(row) <= factor_index or not _without_weight(row[factor_index]):
                continue
            volume = (_norm(row[int(volume_index)]) if volume_index is not None
                      and len(row) > int(volume_index) else "")
            grouped.setdefault((volume, _norm(row[factor_index])), []).append((position, row))
        for (volume, factor), group in sorted(grouped.items(), key=lambda item: item[1][0][0]):
            cells = [(position, str(row[factor_index]),
                      str(row[int(rubric_index)]) if rubric_index is not None
                      and len(row) > int(rubric_index) else "",
                      _pure_weight_value(row[int(weight_index)]) if weight_index is not None
                      and len(row) > int(weight_index) else None)
                     for position, row in group]
            parent_weight, parent_weight_basis, split = _parent_weight(len(group), cells)
            parent_id = f"{table_id}-P{group[0][0]:02d}"
            sibling_rubrics = [rubric for _, _, rubric, _ in cells]
            child_weight_total = 0.0
            leaf_ids: list[str] = []
            if split:
                members = [(position, factor_cell, rubric, weight, True)
                           for (position, factor_cell, rubric, _), weight
                           in zip(cells, _child_weights(cells, parent_weight))]
            else:
                # 同一父项的多行**不是**多件事：得分档写在几行里（"得10分 / 得5分"两档）时，
                # 拆成两个叶子就等于把一件事的分母乘以档数。合成一个叶子，评分标准按行合并，
                # 抽取行落在哪一档都指回同一个主题。
                merged_rubric = "".join(_norm(rubric) for _, _, rubric, _ in cells)
                head = cells[0]
                members = [(head[0], head[1], merged_rubric, parent_weight, False)]
            for position, factor_cell, rubric_cell, weight, is_child in members:
                if is_child:
                    weight_basis = "ROW_GAINED_SCORE"
                    topic = _child_topic(rubric_cell, sibling_rubrics) or _without_weight(factor_cell)
                    parent_group: str | None = _without_weight(factor_cell)
                elif rubric_index is not None and int(rubric_index) == factor_index:
                    # 名称与标准挤在同一格：主题取第一个停顿之前那段。
                    topic = _without_weight(_CLAUSE_CUT.split(_norm(factor_cell))[0]) or _without_weight(factor_cell)
                    weight_basis = parent_weight_basis
                    parent_group = None
                else:
                    topic = _without_weight(factor_cell)
                    weight_basis = parent_weight_basis
                    parent_group = None
                if not is_child and weight is None:
                    weight, weight_basis = _row_weight(factor_cell, cells[0][3], rubric_cell)
                child_weight_total += weight or 0.0
                leaf_id = _leaf_id(table_id, position)
                leaf_ids.append(leaf_id)
                leaves.append({
                    "leaf_id": leaf_id,
                    "topic": topic,
                    "factor_label": _without_weight(factor_cell),
                    "rubric": _norm(rubric_cell),
                    # 分类读这一格：事项名 + 该行评分标准全文。残段之所以丢分，就是因为
                    # 上一轮只读得到评分标准那半句，事项名与分值在别的列里。
                    "leaf_text": _norm(rubric_cell) if topic and _norm(rubric_cell).startswith(topic)
                    else _norm(topic) + _norm(rubric_cell),
                    "weight": weight,
                    "weight_basis": weight_basis,
                    "volume": _without_weight(volume),
                    "parent_id": parent_id,
                    "parent_group": parent_group,
                    "parent_weight": parent_weight,
                    "locator": {"table_id": table_id, "row_index": position, "row_count": len(rows),
                                "table_index": table.get("table_index"), "page_no": table.get("page_no"),
                                "heading_path": list(table.get("heading_path") or [])},
                    "rows_in_parent": len(group),
                    "split_parent": bool(split),
                    # 合并成一条叶子的那些行都点名留着：档位行不是独立主题，但它们仍是
                    # "这一件事在这份表里出现在哪几行"的凭据，落空时要知道落在哪。
                    "merged_row_positions": [] if split or len(group) == 1
                    else [position for position, _, _, _ in cells],
                })
            parents.append({
                "parent_id": parent_id, "table_id": table_id, "row_index": group[0][0],
                "volume": _without_weight(volume), "label": _without_weight(str(cells[0][1])),
                "weight": parent_weight, "weight_basis": parent_weight_basis,
                "split_into": len(group) if split else 1,
                "band_rows_merged": 1 if split else len(group),
                "leaf_ids": leaf_ids,
                "child_weight_total": child_weight_total,
                "weights_reconcile": (abs(child_weight_total - float(parent_weight)) < 1e-9
                                      if parent_weight is not None else False),
            })
    if not leaves:
        return {**empty, "reason": "NO_LEAF_WITH_FACTOR_CELL", "skipped_tables": skipped_tables,
                "column_roles_by_table": {key: dict(value) for key, value in roles_by_table.items()},
                "column_stats_by_table": stats_by_table}
    links, unattached = _attach_items(scoring_items or [], leaves, parents)
    by_leaf: dict[str, list[str]] = {}
    for link in links:
        by_leaf.setdefault(str(link["leaf_id"]), []).append(str(link["scoring_item_id"]))
    for leaf in leaves:
        attached = by_leaf.get(str(leaf["leaf_id"])) or []
        leaf["attached_scoring_ids"] = attached
        leaf["row_count_attached"] = len(attached)
    by_volume: dict[str, dict[str, Any]] = {}
    for leaf in leaves:
        bucket = by_volume.setdefault(str(leaf.get("volume") or ""), {
            "volume": leaf.get("volume"), "parent_ids": [], "leaf_ids": [], "weight_total": 0.0,
            "parents_without_weight": 0, "leaves_without_weight": 0})
        if leaf.get("parent_id") not in bucket["parent_ids"]:
            bucket["parent_ids"].append(leaf.get("parent_id"))
        bucket["leaf_ids"].append(leaf.get("leaf_id"))
        bucket["weight_total"] += float(leaf.get("weight") or 0.0)
        if leaf.get("weight") is None:
            bucket["leaves_without_weight"] += 1
    return {
        "status": "DERIVED",
        "basis": BASIS_TABLE_CELLS,
        "version": SCORING_GRAPH_VERSION,
        "column_roles_by_table": {key: dict(value) for key, value in roles_by_table.items()},
        "column_stats_by_table": stats_by_table,
        "scoring_tables_seen": [str(table.get("table_id") or "") for table in scoring_tables],
        "leaf_bearing_tables": sorted({str(leaf["locator"]["table_id"]) for leaf in leaves}),
        "skipped_tables": skipped_tables,
        "parents": parents,
        "leaves": leaves,
        "item_links": links,
        "unattached_scoring_item_ids": unattached,
        "by_volume": by_volume,
        "counts": {
            "parents": len(parents), "split_parents": sum(1 for row in parents if row["split_into"] > 1),
            "leaves": len(leaves),
            "leaf_weight_total": sum(float(row["weight"] or 0) for row in leaves),
            "parent_weight_total": sum(float(row["weight"] or 0) for row in parents),
            "items_linked": len(links), "items_unattached": len(unattached),
        },
    }


# 抽取行落到叶子上的凭据，按可信度从高到低；配不上就点名列出，不做相似度猜配。
# 两个方向要分开写：`格里含着这段摘录` 是"这行确实是从这一格里抄出来的"，
# `摘录含着整格` 只说明摘录是更大的一段（常常跨行/跨列），可信度低一档。
BASIS_RUBRIC_CONTAINS_EXCERPT = "RUBRIC_CELL_CONTAINS_EXCERPT"
BASIS_ROW_CONTAINS_EXCERPT = "EXCERPT_SPANS_RUBRIC_CELL"
BASIS_FACTOR_CELL = "FACTOR_CELL_APPEARS_IN_LEAF_LABEL"
BASIS_EXCERPT_CONTAINS_ROW = "EXCERPT_CONTAINS_ROW_TEXT"
BASIS_ROW_JOIN_IN_LABEL = "ROW_JOIN_APPEARS_IN_ITEM_LABEL"
BASIS_TOPIC_CELL = "TOPIC_CELL_APPEARS_IN_LABEL"
ATTACHMENT_BASES: tuple[str, ...] = (
    BASIS_RUBRIC_CONTAINS_EXCERPT, BASIS_ROW_CONTAINS_EXCERPT, BASIS_EXCERPT_CONTAINS_ROW,
    BASIS_ROW_JOIN_IN_LABEL, BASIS_FACTOR_CELL, BASIS_TOPIC_CELL,
)


def _attach_items(items: list[dict[str, Any]], leaves: list[dict[str, Any]],
                  parents: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """把抽取行落到叶子上：只按原文包含关系配。

    被拆分的父项里，父项名那几格在多行完全相同（"项目管理（4分）"两行），拿它配行等于掷硬币，
    所以 `split_parent` 的叶子只接受**本行评分标准**级别的凭据；其余叶子按五档包含关系取最高分。
    """
    by_table: dict[str, list[dict[str, Any]]] = {}
    for leaf in leaves:
        by_table.setdefault(str(leaf["locator"]["table_id"]), []).append(leaf)
    links: list[dict[str, Any]] = []
    unattached: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("scoring_item_id") or "")
        if not item_id:
            continue
        source = item.get("source") or {}
        candidates = by_table.get(str(source.get("table_id") or "")) or []
        if not candidates:
            unattached.append(item_id)
            continue
        excerpt = _norm(source.get("source_excerpt") or item.get("label"))
        label = _norm(item.get("label"))
        best: tuple[int, str, dict[str, Any]] | None = None
        for leaf in candidates:
            row_join = _norm(leaf.get("factor_label")) + _norm(leaf.get("rubric"))
            ambiguous_parent = bool(leaf.get("split_parent"))
            checks = (
                (45, BASIS_RUBRIC_CONTAINS_EXCERPT, bool(leaf.get("rubric")) and bool(excerpt)
                 and excerpt in _norm(leaf["rubric"])),
                (40, BASIS_ROW_CONTAINS_EXCERPT, bool(leaf.get("rubric")) and bool(excerpt)
                 and len(_norm(leaf["rubric"])) >= 8 and _norm(leaf["rubric"]) in excerpt),
                (35, BASIS_EXCERPT_CONTAINS_ROW, bool(excerpt) and bool(row_join)
                 and row_join in excerpt),
                (30, BASIS_ROW_JOIN_IN_LABEL, bool(row_join) and len(row_join) >= 12 and row_join in label),
                (20, BASIS_FACTOR_CELL, not ambiguous_parent and bool(leaf.get("factor_label"))
                 and len(_norm(leaf["factor_label"])) >= 4 and _norm(leaf["factor_label"]) in label),
                (15, BASIS_TOPIC_CELL, bool(leaf.get("topic")) and len(_norm(leaf["topic"])) >= 6
                 and _norm(leaf["topic"]) in label),
            )
            for score, basis, hit in checks:
                if hit and (best is None or score > best[0]):
                    best = (score, basis, leaf)
        if best is None:
            unattached.append(item_id)
            continue
        _, basis, leaf = best
        links.append({"scoring_item_id": item_id, "leaf_id": leaf["leaf_id"], "match_basis": basis,
                      "locator": dict(leaf.get("locator") or {}),
                      "excerpt_length": len(excerpt or label)})
    return links, sorted(set(unattached))


def leaf_by_id(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row.get("leaf_id")): row for row in (graph or {}).get("leaves") or []}


# 「这条评分项要拿什么文本去判技术正文怎么回答它」——只有一个产地，行上与记录上都读它。
RESPONSE_TEXT_FROM_TABLE = "SCORING_TABLE_CELL_TEXT"
RESPONSE_TEXT_FROM_ROW_LABEL = "FLATTENED_ROW_LABEL"


def response_text_for_item(item: dict[str, Any], leaves: dict[str, dict[str, Any]]
                           ) -> tuple[str, str]:
    """抽取行的分类输入：能落到叶子就用**事项名 + 本行评分标准全文**，落不到才用打平标签。

    上一轮只给得出后半句残段的行（"…全面性、可行性、及时性等方面横向比较进行打分"）之所以
    丢分，不是因为分类器词表不够，而是因为它**看到的文本里就没有事项名与分值**。
    """
    leaf = leaves.get(str((item or {}).get("scoring_leaf_id") or ""))
    if leaf:
        text = str(leaf.get("leaf_text") or "") or str((item or {}).get("label") or "")
        return text, RESPONSE_TEXT_FROM_TABLE
    return str((item or {}).get("label") or ""), RESPONSE_TEXT_FROM_ROW_LABEL


ANCHOR_LEAF_CELL = "SCORING_TABLE_LEAF_CELL"
ANCHOR_SOURCE_TABLE = "SOURCE_LOCATOR_IS_LEAF_BEARING_TABLE"
ANCHOR_RUBRIC_CHAPTER_WEIGHT = "RUBRIC_CHAPTER_WITH_OWN_WEIGHT"
ANCHOR_TEXT_ONLY = "NO_TABLE_NO_RUBRIC_CHAPTER_NO_OWN_WEIGHT"
ANCHOR_GRAPH_ABSENT = "NO_SCORING_TABLE_GRAPH"


def rubric_lineage_index(graph: dict[str, Any] | None) -> dict[str, set[str]]:
    """这张图**自己**声明的评分血缘：哪些表格产出过可响应叶子、这些表格落在哪些章节里。

    章节集合不是词表：它由这份招标文件自己的评分表位置决定。换一份标书（章节编号不同、
    评分办法在第二章、评分表长在另一张表上）这里就跟着换，不需要任何项目专有名。
    """
    if not isinstance(graph, dict) or graph.get("status") != "DERIVED":
        return {"tables": set(), "chapters": set()}
    tables: set[str] = set()
    chapters: set[str] = set()
    for row in graph.get("leaves") or []:
        locator = row.get("locator") or {}
        table_id = str(locator.get("table_id") or "").strip()
        if table_id:
            tables.add(table_id)
        for node in locator.get("heading_path") or []:
            text = str(node or "").strip()
            if text:
                chapters.add(text)
    for row in graph.get("parents") or []:
        table_id = str(row.get("table_id") or "").strip()
        if table_id:
            tables.add(table_id)
    return {"tables": tables, "chapters": chapters}


def rubric_lineage(item: dict[str, Any], graph: dict[str, Any] | None,
                   *, leaves: dict[str, dict[str, Any]] | None = None,
                   index: dict[str, set[str]] | None = None) -> dict[str, Any]:
    """一条评分项的**评分血缘**：这一项是不是从评分表里来的（叶子、表格、章节、分值）。

    为什么需要它：第二维只看"这一格文本像不像一篇技术正文要回答的事"。招标文件里还有
    一类句子长得**像**技术要求、身份却是**合同条款**——它引用的是投标人已经承诺过的内容
    （结构配置、管理模式、操作程序…），要它的那几行评分表项在别处已经立过了。只凭文本
    给它一条独立技术主题，就等于凭空多造一笔正文欠账。

    `anchored` 要求三选一的正面向证据：落在叶子上、来源本身就是产出过叶子的表格、
    或来源章节正是评分表所在章且这一项自带分值。三条都没有 ⇒ 文本判断不足以立项。
    没有可解析的评分表时（图 NOT_DERIVABLE）一律给 `anchored=True`：那是**文件形态**
    没有表可指认，不是这一项没有评分义务，不能拿缺表当降类的理由。
    """
    index = index if index is not None else rubric_lineage_index(graph)
    leaves = leaves if leaves is not None else leaf_by_id(graph or {})
    source = (item or {}).get("source") or {}
    heading_path = [str(node) for node in (source.get("heading_path") or [])
                    if str(node or "").strip()]
    chapter = str(source.get("source_section") or "") or (heading_path[0] if heading_path else "")
    table_id = str(source.get("table_id") or "").strip()
    leaf_id = str((item or {}).get("scoring_leaf_id") or "")
    leaf = leaves.get(leaf_id)
    own_weight: float | None = None
    for key in ("weight_numeric", "weight", "max_score", "score"):
        value = (item or {}).get(key)
        if isinstance(value, (int, float)) and value:
            own_weight = float(value)
            break
        if isinstance(value, str):
            match = re.search(r"\d+(?:\.\d+)?", value)
            if match:
                own_weight = float(match.group())
                break
    if own_weight is None:
        match = re.search(r"(\d+(?:\.\d+)?)\s*分", str((item or {}).get("label") or ""))
        if match:
            own_weight = float(match.group(1))
    graph_derived = bool(isinstance(graph, dict) and graph.get("status") == "DERIVED")
    table_lineage = bool(table_id) and table_id in index["tables"]
    chapter_lineage = bool(chapter) and any(chapter in node or node in chapter
                                            for node in index["chapters"])
    if not graph_derived:
        anchor_basis = ANCHOR_GRAPH_ABSENT
    elif leaf:
        anchor_basis = ANCHOR_LEAF_CELL
    elif table_lineage:
        anchor_basis = ANCHOR_SOURCE_TABLE
    elif chapter_lineage and own_weight is not None:
        anchor_basis = ANCHOR_RUBRIC_CHAPTER_WEIGHT
    else:
        anchor_basis = ANCHOR_TEXT_ONLY
    return {
        "graph_derived": graph_derived,
        "leaf_id": leaf_id or None,
        "leaf_weight": (leaf or {}).get("weight"),
        "parent_rubric": (leaf or {}).get("parent_group") or (leaf or {}).get("topic"),
        "volume": (leaf or {}).get("volume"),
        "source_table_id": table_id or None,
        "source_chapter": chapter or None,
        "source_locator": str(source.get("source_paragraph_or_table") or "").strip() or None,
        "table_lineage": table_lineage,
        "chapter_lineage": chapter_lineage,
        "rubric_chapter_count": len(index["chapters"]),
        "own_weight": own_weight,
        "anchored": anchor_basis != ANCHOR_TEXT_ONLY,
        "anchor_basis": anchor_basis,
    }


def graph_readings(graph: dict[str, Any]) -> dict[str, Any]:
    """给产物与取证用的一格图读数：父项数、叶子数、分值合计、漏失与重复计分的可见性。

    每卷次一份并列读数（条数尺 + 分值尺）。**只报数，不做判定**：哪一卷是技术标由既有的
    单一范围门决定，本模块不另立一处判定。
    """
    if not isinstance(graph, dict) or graph.get("status") != "DERIVED":
        return {"status": (graph or {}).get("status") or "ABSENT",
                "reason": (graph or {}).get("reason"), "basis": BASIS_NOT_DERIVABLE,
                "version": SCORING_GRAPH_VERSION}
    split_parents = [row for row in graph.get("parents") or [] if int(row.get("split_into") or 1) > 1]
    return {
        "status": "DERIVED", "basis": BASIS_TABLE_CELLS, "version": SCORING_GRAPH_VERSION,
        "parent_count": len(graph.get("parents") or []),
        "split_parent_count": len(split_parents),
        "leaf_count": len(graph.get("leaves") or []),
        "leaf_weight_total": graph["counts"]["leaf_weight_total"],
        "parent_weight_total": graph["counts"]["parent_weight_total"],
        "by_volume": {key: {"parent_count": len(value["parent_ids"]),
                            "leaf_count": len(value["leaf_ids"]),
                            "leaf_weight_total": value["weight_total"]}
                      for key, value in (graph.get("by_volume") or {}).items()},
        "parents_with_weight_mismatch": [str(row["parent_id"]) for row in split_parents
                                         if not row.get("weights_reconcile")],
        "parents_without_weight": [str(row["parent_id"]) for row in graph.get("parents") or []
                                   if row.get("weight") is None],
        "leaves_without_attached_row": [str(row.get("leaf_id")) for row in graph["leaves"]
                                        if not row.get("attached_scoring_ids")],
        "leaves_with_multiple_attached_rows": {
            str(row.get("leaf_id")): list(row.get("attached_scoring_ids") or [])
            for row in graph["leaves"] if int(row.get("row_count_attached") or 0) > 1},
        "unattached_scoring_item_ids": list(graph.get("unattached_scoring_item_ids") or []),
        "skipped_tables": [str(row.get("table_id")) for row in graph.get("skipped_tables") or []],
        "attachment_basis_counts": _basis_counts(graph.get("item_links") or []),
    }


def _basis_counts(links: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {basis: 0 for basis in ATTACHMENT_BASES}
    for link in links:
        basis = str(link.get("match_basis") or "")
        counts[basis] = counts.get(basis, 0) + 1
    return counts
