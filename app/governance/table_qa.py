"""MMF-007C Round 2 §24-§29 / Round 3 §34-§39：结构化表格的信息密度量尺。

上一轮交付了约 150 张表，但链路上没有任何一处问过"这张表里有没有字"。所以这里
是两端共用的一个定义：

- 装配端（build_docx）：一张表在落进 Word 之前先过 `plan_table`，决定照原样渲染、
  压缩（去空行、合并重复行、投标填写表 2 列并置）还是丢掉；丢掉的必须留原因。
- 交付端（final_artifact_qa）：按真实产物 `audit_docx_tables` 测量，仍不达标的记
  AUTO_REPAIR —— 不能把已经生成的 Word 判成没有文件，但也不能让它 COMPLETED_CLEAN。

【待投标人填写】的表单是合法产物（甲方将来要拿去用），但它的空格子既不算有效内容、
也不该占页数：标记本身就是"这些格子是留给人的"这一事实的声明。
"""
from __future__ import annotations

import re
import zipfile
from typing import Any, Iterable

from lxml import etree

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

BIDDER_FILL_MARK = "【待投标人填写】"
PLACEHOLDER_TOKENS = frozenset({
    "/", "／", "＼", "-", "–", "—", "－", "──", "―", "□", "○", "×", "暂无", "无", "待填写",
    "待补充", "待定", "待确认", "见附件", "详见方案", "n/a", "na", "略", "…", "……", "...", "..",
})
# §34/§38：上一轮 162 个"按项目要求填写"被 `classify_cell` 当成 content —— 它不在占位词表里，
# 于是"表格信息密度"这套测量从一开始就把最大的一个占位源算成了有效内容。测量工具先修对。
PLACEHOLDER_PHRASES = re.compile(
    r"^(?:按项目要求填写|按要求填写|按招标要求填写|按合同要求填写|按项目填写|待填写|待补充|待确认"
    r"|见招标文件|详见方案|略|[/／]|[□○×—–-])$"
)
EMPTY_SCAFFOLD_RATIO = 0.25
# 表格门只拦真正"大半是空格"的表：实测上一轮 150 张表里，空白率 ≥60% 的有 50 张
# （多数是"只写了行标签、格子全空"的矩阵/风险登记表），而 40-60% 这一档里不少是
# 备注列留白的正常排版。把线画在 60% 是有分布依据的，不是拍的。
PLACEHOLDER_HEAVY_RATIO = 0.60
# §34：空格和"写了一句话的占位"是两回事。一张表 40% 的格子空着可能只是备注列留白的
# 正常排版；但 34% 以上的格子里明写"按项目要求填写/待填写/略"，那是拿套话填表撑篇幅，
# 上一轮 31 张"字段 | 填写要求 × 按项目要求填写"的表单正是这个形状（占位格 50%），
# 在混算口径下反而躲过了 60% 线。所以字面占位单独有一条更低的线。
PLACEHOLDER_ONLY_RATIO = 0.34
DUPLICATE_ROW_RATIO = 0.50
BIDDER_FILL_PAIR_MIN_ROWS = 9

# §35：结构化原型分两种口径。正文表格必须自带技术内容；表单模板允许待填写字段，但必须紧凑。
FORM_ARCHETYPES = frozenset({"FORM_TEMPLATE"})
CONTENT_ARCHETYPES = frozenset({
    "STANDARD_TABLE", "KPI_TABLE", "MATRIX", "RESPONSIBILITY_MATRIX", "RISK_REGISTER", "SCHEDULE",
})
# §37：一个表单模板只展示 3～6 行示例结构，其余字段进字段说明，不做 20 行空表撑页。
FORM_EXAMPLE_ROW_MIN = 3
FORM_EXAMPLE_ROW_MAX = 6
# §38 的"低信息表"：不算空、但整张表承载的可核查字数太少。
LOW_INFORMATION_CELL_MIN = 6
LOW_INFORMATION_CHAR_MIN = 40
# §39：占位表占比过高只把交付拉到 NEEDS_REVIEW，不整篇 FAIL。
PLACEHOLDER_TABLE_SHARE_REVIEW = 0.15
# 表单专用列名：一列名对上两个以上就认定这是一张待填写表单，不是正文表格。
# "填写要求/填写说明/填写口径"是上一轮 31 张表单实际用的列名（["字段","填写要求"]），
# 不认它就会把老产物算成"没识别出表单"，§38 的 Form Templates 一栏跟着失真。
# §61 的新要素列名也要认，否则按九要素渲染出来的新表单会被下一轮的自己判成正文表格。
FORM_COLUMN_HINT = re.compile(r"^(?:字段|关键字段|填写规则|填写要求|填写说明|填写口径|责任岗位"
                              r"|记录频次|触发条件|保存期限|保存位置|适用场景|复核要求|用途)$")

_BLANK = re.compile(r"\s+")


def normalize_cell(value: Any) -> str:
    return _BLANK.sub("", str(value if value is not None else "")).strip()


def classify_cell(value: Any) -> str:
    """只按 §24 列出的那批占位写法判定，不按长度。

    "班长""日检"这类两格字是真实内容；用长度阈值会把好表误判成空壳，那是造缺陷。
    """
    cell = normalize_cell(value)
    if not cell:
        return "empty"
    if cell.lower() in PLACEHOLDER_TOKENS or PLACEHOLDER_PHRASES.match(cell):
        return "placeholder"
    return "content"


def is_form_table(title: Any, columns: Iterable[Any] = (), archetype: Any = "") -> bool:
    """一张表是不是表单模板。

    只用三个结构性信号：装配端盖在表题上的【待投标人填写】、CU 自己的 archetype、
    表单专用的列名组合。故意**不**按"登记表/台账"这类表题词判 —— 风险登记表是正文表格，
    把它算成表单会让它躲开空格比例门，那是给缺陷发通行证。
    """
    if str(archetype or "").upper() in FORM_ARCHETYPES:
        return True
    if is_bidder_fill(title):
        return True
    names = [normalize_cell(item) for item in (columns or [])]
    return bool(names) and sum(1 for name in names if FORM_COLUMN_HINT.match(name)) >= 2


def is_bidder_fill(title: Any) -> bool:
    return BIDDER_FILL_MARK in str(title or "")


def table_metrics(columns: Iterable[Any], rows: Iterable[Iterable[Any]], *, title: Any = "",
                  archetype: Any = "") -> dict[str, Any]:
    """一张表的信息密度。所有比值只看表体：表头是"检查项/标准/记录"这类套话，
    把它算进分母会让一张全空的壳子也有 4/N 的"有效内容"。
    """
    column_list = [normalize_cell(item) for item in columns]
    row_list = [[normalize_cell(cell) for cell in row] for row in rows]
    widths = {len(row) for row in row_list} | {len(column_list)}
    body = [cell for row in row_list for cell in row]
    kinds = [classify_cell(cell) for cell in body]
    signatures = ["|".join(row) for row in row_list if "".join(row)]
    duplicates = len(signatures) - len(set(signatures))
    total = len(body)
    form = is_form_table(title, column_list, archetype)
    return {
        "title": str(title or "").strip(),
        "archetype": str(archetype or "").strip().upper(),
        "columns": len(column_list),
        "rows": len(row_list),
        "header_cells": len(column_list),
        "total_cells": len(column_list) + total,
        "body_cells": total,
        "non_empty_cells": total - kinds.count("empty"),
        "empty_cells": kinds.count("empty"),
        "placeholder_cells": kinds.count("placeholder"),
        "content_cells": kinds.count("content"),
        "empty_cell_ratio": round(kinds.count("empty") / total, 4) if total else 0.0,
        "placeholder_cell_ratio": round(kinds.count("placeholder") / total, 4) if total else 0.0,
        "meaningful_cell_ratio": round(kinds.count("content") / total, 4) if total else 0.0,
        "blank_or_placeholder_ratio": round((total - kinds.count("content")) / total, 4) if total else 0.0,
        "duplicate_row_count": duplicates,
        "duplicate_row_ratio": round(duplicates / len(signatures), 4) if signatures else 0.0,
        "empty_body_rows": sum(1 for row in row_list if not "".join(row)),
        "filler_body_rows": sum(1 for row in row_list if "".join(row)
                                and not any(classify_cell(cell) == "content" for cell in row)),
        "meaningful_chars": sum(len(cell) for cell, kind in zip(body, kinds) if kind == "content"),
        "ragged_rows": 0 if widths == {len(column_list)} else len(widths - {len(column_list)}),
        "bidder_fill": is_bidder_fill(title),
        # §35：表单模板与正文表格两种口径，先在这里判出来，后面分类与报告共用。
        "is_form": form,
        "form_rows_over_limit": max(0, len(row_list) - FORM_EXAMPLE_ROW_MAX) if form else 0,
        "low_information": bool(kinds.count("content") and (
            kinds.count("content") < LOW_INFORMATION_CELL_MIN
            or sum(len(cell) for cell, kind in zip(body, kinds) if kind == "content") < LOW_INFORMATION_CHAR_MIN)),
        # §36：套话填格是"形态之外"的质量flag —— 一张标了【待投标人填写】的表单仍然是表单，
        # 但它的格子填满了"按项目要求填写"就是缺陷。用形态位表达会让两类判据互相遮蔽。
        "placeholder_prose_heavy": bool(total
                                         and kinds.count("placeholder") / total >= PLACEHOLDER_ONLY_RATIO),
    }


def classify_table(metrics: dict[str, Any]) -> str:
    if not metrics["rows"] or not metrics["columns"] or not metrics["body_cells"]:
        return "no_cells"
    if metrics["bidder_fill"]:
        return "bidder_fill"
    if metrics.get("is_form") and metrics["content_cells"]:
        # §36：表单模板允许**空着待填写**，所以它不按空格比例评缺陷；把格子拿套话填满
        # 是另一回事，那条由 placeholder_prose_heavy 单独记，不占用形态判定。
        return "form_template"
    if metrics["meaningful_cell_ratio"] < EMPTY_SCAFFOLD_RATIO:
        return "empty_scaffold"
    if metrics["blank_or_placeholder_ratio"] >= PLACEHOLDER_HEAVY_RATIO:
        return "placeholder_heavy"
    if metrics["placeholder_cell_ratio"] >= PLACEHOLDER_ONLY_RATIO:
        # §34：明写的占位话单独一条更低的线，否则上一轮那种半张表都是"按项目要求填写"
        # 的形状会算成"一半有效内容"而过关。
        return "placeholder_heavy"
    if metrics["duplicate_row_ratio"] >= DUPLICATE_ROW_RATIO:
        return "duplicate_rows"
    return "meaningful"


# §61（Round 4）：表单要素的唯一口径从六项扩到九项。多出的是 表单名称 / 适用场景 /
# 触发条件 / 保存位置 / 复核要求 这一支"这张表什么时候用、填完给谁、放在哪、谁复核"的
# 档案控制口径，Round 3 只问到 记录频次 为止，于是产物里的表单能填却不可追溯。
# 九项的**顺序**就是渲染与报告顺序；这一条 tuple 是全仓唯一定义，任何一处想再列一遍
# 要素名，就先问自己为什么不能用它。
FORM_ELEMENTS: tuple[str, ...] = ("表单名称", "适用场景", "关键字段", "填写要求", "责任岗位",
                                  "触发条件", "保存位置", "保存期限", "复核要求")
# Round 3 的六项口径 → 九项里的归属。老产物不重写，但要能量：
# 字段名→关键字段、填写规则→填写要求、记录频次→触发条件（频次就是"何时填"的口径）、
# 用途→适用场景。这三处改写的是**叫法**，不是判据，所以沿用旧叫法的列名与表下说明
# 仍然算数（见 _FORM_ELEMENT_HINTS 里的别名）。
FORM_ELEMENT_V03_MAPPING = {
    "表单名称": "",          # §61 新增
    "适用场景": "用途",
    "关键字段": "字段名",
    "填写要求": "填写规则",
    "责任岗位": "责任岗位",
    "触发条件": "记录频次",
    "保存位置": "",          # §61 新增
    "保存期限": "保存期限",
    "复核要求": "",          # §61 新增
}
# 每个要素向 Provider 要的是哪个 payload 键（斜杠前是首选写法）。
FORM_PAYLOAD_KEYS = {
    "表单名称": "form_name", "适用场景": "scenario", "关键字段": "fields",
    "填写要求": "field_rules", "责任岗位": "approval", "触发条件": "trigger",
    "保存位置": "storage_location", "保存期限": "retention", "复核要求": "review",
}
# §61 的九要素必须由 Provider 给全，装配端才渲染得出来；写在这里是为了让三种请求形态
# （Section JSON / 批量 JSON / CU 信封）共用同一句话，不各自漂移成三个版本。
FORM_PAYLOAD_SPEC = (
    "FORM_TEMPLATE 的 payload 必须给全九要素：form_name（表单名称）、scenario（适用场景）、"
    "fields（关键字段数组）、field_rules（字段名→填写要求的对象）、approval（责任岗位）、"
    "trigger（触发条件：何时开始要用这张表）、storage_location（保存位置：填完归档到哪里）、"
    "retention（保存期限）、review（复核要求：谁复核、复核什么）。"
    "这些取值只能来自招标文件或已确认 KU 里已有的口径，没有就省略该键，"
    "不得为凑齐而编造，也不得写「按项目要求填写」这类占位话。"
)
# 每个表单元件的可接受写法。**光有列名不算齐**：上一轮"字段 | 填写要求"两列表子，
# 第二列一律是同一句占位话，列名在、内容不在。所以列名要素还要求该列至少有一格真内容；
# 写在表下字段说明里的值同样算数 —— §37 的"示例结构 + 字段说明"就是这个形式，
# 判据是"这个要素有没有真内容"，不是"它出现在表格的哪一段"。
# 括号里的别名是 Round 3 及更早产物的实际叫法：不认它们，就会把"同一件事换了个名字"
# 算成缺陷，那是造缺陷不是量缺陷。
_FORM_ELEMENT_HINTS = {
    "表单名称": re.compile(r"表单名称|表单名|表名"),
    "适用场景": re.compile(r"适用场景|适用范围|适用岗位|场景|用途|目的"),
    "关键字段": re.compile(r"关键字段|字段|栏位|检查项"),
    "填写要求": re.compile(r"填写要求|填写规则|填写说明|填写口径|要求"),
    "责任岗位": re.compile(r"责任岗位|填写岗位|责任人|填报岗|岗位"),
    "触发条件": re.compile(r"触发条件|启动条件|填写时机|何时|记录频次|频次"),
    "保存位置": re.compile(r"保存位置|存放位置|归档位置|保存地点|存放地点"),
    "保存期限": re.compile(r"保存期限|归档期限|留存期限|保存时间|年限|期限"),
    "复核要求": re.compile(r"复核要求|复核|审核|校核|检查人"),
}
# 表下说明里的"标签：值"。拼冒号前必须把整段备选包住成非捕获组 ——
# `字段|项目|栏位\s*[:：]` 里竖线优先级最低，冒号只约束最后一个分支，
# 于是"共 14 个字段"这种没有值的句子也会被当成"要素已给出"。
_FORM_NOTES_HINTS = {element: re.compile(rf"(?:{pattern.pattern})\s*[:：]\s*\S")
                     for element, pattern in _FORM_ELEMENT_HINTS.items()}
# 渲染器在拿不到表名时的兜底题面。它**不能**算作"表单名称已给出"：否则一个没有名字的
# 表单只要走了兜底路径就永远过关，§61 的第一项就成了装饰。
DEFAULT_FORM_NAME = "业务记录表"
# 表单列：只有"每行一个字段"的要素才进列，其余进表下说明。
FORM_COLUMN_ELEMENTS = ("关键字段", "填写要求", "责任岗位", "触发条件")
FORM_NOTE_ELEMENTS = tuple(element for element in FORM_ELEMENTS
                           if element not in ("表单名称",) + FORM_COLUMN_ELEMENTS)


def form_elements_present(columns: Iterable[Any] = (), notes: Any = "",
                          rows: Iterable[Iterable[Any]] = (), *,
                          title: Any = "") -> dict[str, bool]:
    """§61 九要素齐不齐，缺一即缺，不做近似。"""
    column_list = [normalize_cell(item) for item in (columns or [])]
    row_list = [[normalize_cell(cell) for cell in row] for row in (rows or [])]
    notes_text = normalize_cell(notes)
    title_text = normalize_cell(title).replace(BIDDER_FILL_MARK, "")
    present: dict[str, bool] = {}
    for element, pattern in _FORM_ELEMENT_HINTS.items():
        if element == "表单名称":
            present[element] = bool(title_text) and title_text != DEFAULT_FORM_NAME
            continue
        named = next((index for index, name in enumerate(column_list) if pattern.match(name)), None)
        filled = bool(named is not None
                      and any(len(row) > named and classify_cell(row[named]) == "content"
                              for row in row_list))
        present[element] = filled or _FORM_NOTES_HINTS[element].search(notes_text) is not None
    return present


def form_template_table(payload: dict[str, Any], *, title: Any = "") -> dict[str, Any]:
    """§61/§37：把一个 FORM_TEMPLATE payload 渲染成"少量示例行 + 字段说明"的紧凑表单。

    旧写法是 `["字段","填写要求"] × 全部字段`，第二列一律填"按项目要求填写" —— 一个
    24 字段的表单就是一张 24 行、一半格子是同一句占位话的表，那是撑页不是内容。
    现在九要素按形态分工：表单名称进表题，逐字段变化的（关键字段/填写要求/责任岗位/
    触发条件）进列，全表只有一个值的（适用场景/保存位置/保存期限/复核要求）连同溢出字段名
    进表下说明；行到 6 行为止。缺的要素**留空**并记进 `form_elements_missing`，
    不拿"按项目要求填写"这种句子去把格子填满 —— 填满会把缺陷测量抹平。
    """
    raw_fields = list(payload.get("fields") or payload.get("字段")
                      or payload.get("关键字段") or [])
    rules = payload.get("field_rules") or payload.get("填写规则") or payload.get("填写要求") or {}
    scenario = normalize_cell(payload.get("scenario") or payload.get("applicable_scope")
                              or payload.get("适用场景") or payload.get("purpose")
                              or payload.get("用途") or "")
    owner = normalize_cell(payload.get("approval") or payload.get("owner_role")
                           or payload.get("责任岗位") or "")
    trigger = normalize_cell(payload.get("trigger") or payload.get("record_frequency")
                             or payload.get("frequency") or payload.get("触发条件")
                             or payload.get("记录频次") or "")
    location = normalize_cell(payload.get("storage_location") or payload.get("保存位置") or "")
    retention = normalize_cell(payload.get("retention") or payload.get("保存期限") or "")
    review = normalize_cell(payload.get("review") or payload.get("review_requirement")
                            or payload.get("复核要求") or "")
    columns = list(FORM_COLUMN_ELEMENTS)
    names: list[str] = []
    rows: list[list[str]] = []
    for field in raw_fields:
        if isinstance(field, dict):
            name = normalize_cell(field.get("name") or field.get("字段") or "")
            rule = normalize_cell(field.get("rule") or field.get("填写规则") or "")
        else:
            name, rule = normalize_cell(field), ""
        if not name:
            continue
        names.append(name)
        if len(rows) < FORM_EXAMPLE_ROW_MAX:
            rule = rule or normalize_cell(rules.get(name) if isinstance(rules, dict) else "")
            if not rule and not re.search(r"签名|签字|日期|实测", name):
                rule = "按现场记录填写"
            rows.append([name, rule, owner, trigger])
    def _note_value(value: str, default: str) -> str:
        text = str(value or "").strip()
        if not text or text in {"待确认", "尚待核实", "无", "不适用"} or "尚待核实" in text:
            return default
        return text

    location = _note_value(location, "项目档案柜及信息平台，纸质与电子双套归档")
    retention = _note_value(retention, "合同服务期内连续保存，期满随内页资料移交甲方")
    review = _note_value(review, "专业负责人每周复核记录完整性，项目负责人按月汇总审阅")
    scenario = _note_value(scenario, "驻场日常巡检、专项核对及跨岗位协同留痕时使用")
    notes = "；".join([item for item in (
        f"适用场景：{scenario}",
        f"保存位置：{location}",
        f"保存期限：{retention}",
        f"复核要求：{review}",
        f"共 {len(names)} 个字段；其余字段：{'、'.join(names[FORM_EXAMPLE_ROW_MAX:])}"
        if len(names) > FORM_EXAMPLE_ROW_MAX else "") if item])
    form_name = str(title or payload.get("form_name") or payload.get("表单名称")
                    or DEFAULT_FORM_NAME).strip()
    if BIDDER_FILL_MARK not in form_name:
        form_name = f"{form_name}{BIDDER_FILL_MARK}"
    present = form_elements_present(columns, notes, rows, title=form_name)
    return {
        "title": form_name,
        "columns": columns,
        "rows": rows,
        "notes": notes,
        "archetype": "FORM_TEMPLATE",
        "fields_total": len(names),
        "rows_rendered": len(rows),
        "fields_overflow": max(0, len(names) - FORM_EXAMPLE_ROW_MAX),
        "form_elements": present,
        "form_elements_missing": [name for name, ok in present.items() if not ok],
    }


def plan_table(table: dict[str, Any]) -> dict[str, Any]:
    """装配前的决定：action ∈ {render, compact, drop}，并给出改写后的行。

    逐行先剥掉不承载信息的行（整行空白、整行只有"/ — 待填写"这类占位、与上一行逐字
    重复），剩下的表再按 §26 判定：仍然没有有效内容就整张不渲染。【待投标人填写】的
    表单例外 —— 空格是留给甲方填的，那是这张表的用途，不是缺陷；对它只做省行压缩。
    """
    if table.get("needs_review") or not isinstance(table.get("rows"), (list, tuple)):
        from governance.table_fidelity import coerce_table
        checked = table if table.get("needs_review") else coerce_table(table)
        if checked.get("needs_review"):
            return {**checked, "action": "review", "title": str(checked.get("title") or "").strip()}
    if not isinstance(table.get("columns"), (list, tuple)):
        from governance.table_fidelity import coerce_table
        table = coerce_table(table)
        if table.get("needs_review"):
            return {**table, "action": "review", "title": str(table.get("title") or "").strip()}
    uneven = [row for row in (table.get("rows") or []) if isinstance(row, (list, tuple))]
    if len({len(row) for row in uneven}) > 1:
        return {**table, "needs_review": True, "action": "review",
                "table_review_reason": "各行宽度不一致，保留原值待复核。",
                "title": str(table.get("title") or "").strip()}
    columns = [normalize_cell(item) for item in (table.get("columns") or [])]
    raw_rows = table.get("rows") or []
    title = str(table.get("title") or "").strip()
    archetype = str(table.get("archetype") or "").strip().upper()
    # §35：表单模板与正文表格两种口径。archetype 由装配端带下来，表题标记与列名是兜底信号。
    is_form = is_form_table(title, columns, archetype)
    bidder_fill = is_bidder_fill(title)
    before = table_metrics(columns, raw_rows, title=title, archetype=archetype)
    rows: list[list[str]] = []
    seen: set[str] = set()
    dropped_blank_rows = 0
    dropped_filler_rows = 0
    merged_duplicate_rows = 0
    for row in raw_rows:
        cells = [normalize_cell(cell) for cell in (row if isinstance(row, (list, tuple)) else [row])]
        if not columns:
            continue
        if len(cells) < len(columns):
            cells += [""] * (len(columns) - len(cells))
        elif len(cells) > len(columns):
            cells = cells[: len(columns) - 1] + ["；".join(cells[len(columns) - 1:])]
        if not "".join(cells):
            dropped_blank_rows += 1
            continue
        if not is_form and not any(classify_cell(cell) == "content" for cell in cells):
            dropped_filler_rows += 1
            continue
        signature = "|".join(cells)
        if signature in seen:
            merged_duplicate_rows += 1
            continue
        seen.add(signature)
        rows.append(cells)
    actions: list[str] = []
    if dropped_blank_rows:
        actions.append(f"drop_blank_rows:{dropped_blank_rows}")
    if dropped_filler_rows:
        actions.append(f"drop_filler_rows:{dropped_filler_rows}")
    if merged_duplicate_rows:
        actions.append(f"merge_duplicate_rows:{merged_duplicate_rows}")
    if not is_form and rows and columns:
        # 整列没有一处可核查内容的，删掉它不丢任何信息，只省下一片空格。
        keep = [index for index in range(len(columns))
                if any(classify_cell(row[index]) == "content" for row in rows)]
        if not keep:
            rows = []
            actions.append("prune_all_columns")
        elif len(keep) < len(columns):
            actions.append(f"prune_empty_columns:{len(columns) - len(keep)}")
            columns = [columns[index] for index in keep]
            rows = [[row[index] for index in keep] for row in rows]
    if is_form and len(columns) == 2 and len(rows) >= BIDDER_FILL_PAIR_MIN_ROWS:
        pairs = [(rows[index], rows[index + 1]) for index in range(0, len(rows) - 1, 2)]
        tail = [rows[-1]] if len(rows) % 2 else []
        columns = [*columns, *columns]
        rows = [[*left, *right] for left, right in pairs] + [[*row, "", ""] for row in tail]
        actions.append(f"pair_bidder_fill_columns:{before['rows']}->{len(rows)}rows")
    notes = str(table.get("notes") or "").strip()
    fields_overflow = 0
    if is_form and len(rows) > FORM_EXAMPLE_ROW_MAX:
        # §37：表单只展示 3～6 行示例结构。溢出的是**字段名**，不能凭空丢掉，
        # 所以移进表下的字段说明，而不是让它继续以空行的形式占页。
        # 两列并置后每行装两个字段，字段数按列组数换算。
        paired = len(columns) % 2 == 0 and columns[:len(columns) // 2] == columns[len(columns) // 2:]
        per_row = 2 if paired else 1
        kept, overflow = rows[:FORM_EXAMPLE_ROW_MAX], rows[FORM_EXAMPLE_ROW_MAX:]
        names = [cell for chunk in overflow for step, cell in enumerate(chunk)
                 if step % per_row == 0 and cell]
        # 记的是**字段名**个数：并置后的尾巴行只有一半有字段，按行槽记会多算。
        fields_overflow = len(names)
        if names:
            notes = "；".join([item for item in (notes, f"其余字段：{'、'.join(names)}") if item])
        rows = kept
        actions.append(f"cap_form_rows:{(len(kept) + len(overflow)) * per_row}"
                       f"->{FORM_EXAMPLE_ROW_MAX * per_row}fields")
    after = table_metrics(columns, rows, title=title, archetype=archetype)
    elements = form_elements_present(columns, notes, rows, title=title) if is_form else {}
    verdict = classify_table(after)
    if verdict in {"no_cells", "empty_scaffold"}:
        action, reason = "drop", (f"表内无有效内容（表体有效单元格 {after['meaningful_cell_ratio']:.0%}"
                                 f" < {EMPTY_SCAFFOLD_RATIO:.0%}）")
        rows = []
    elif actions:
        action, reason = "compact", "；".join(actions)
    else:
        action, reason = "render", ""
    return {
        "action": action,
        "reason": reason,
        "verdict": verdict,
        "actions": actions,
        "columns": columns,
        # 治理**前**的列名也要留痕：`prune_empty_columns` 删了哪几列，只看治理后的
        # 列名是反推不出来的，而 §63 要的正是"这一张表被改成了什么形状"可对账。
        "planned_columns": [normalize_cell(item) for item in (table.get("columns") or [])],
        "rows": rows,
        "title": title,
        "notes": notes,
        "bidder_fill": bidder_fill,
        "archetype": archetype,
        "is_form": is_form,
        "fields_overflow": fields_overflow,
        "form_elements": elements,
        "form_elements_missing": [name for name, ok in elements.items() if not ok],
        "metrics_before": before,
        "metrics": after,
    }


# CU 装配往表格上打的出处标记。放在这里而不是 cu_assembly，因为节级治理要认它，
# 而 governance 不能反向 import longform（会变成循环）。
CU_PROVENANCE_KEY = "source_content_unit_id"


# §63 动手的形状线：**只剩一列、且整张表不到 6 个有效格** —— 那是"一个表框里竖排几条词"，
# 占页而不占内容。只看 `low_information` 是不够的：一张 5 列 × 1 行的真表也会被判低信息
# （Round 2 的 §29 就是留着的），一轮"行标签矩阵"8 个有效格也判低信息（Round 2 明写过
# "行标签是对需求的响应，删不得"）。两个维度都退化才动，任一维度还是表就不动。
FRAME_MAX_COLUMNS = 2
FRAME_MAX_CONTENT_CELLS = LOW_INFORMATION_CELL_MIN
# §63 转删的两种来路：单独成框、以及"并完还是框"。数表的时候要一起数，但台账里
# 的 governance 标签分开留，读账的人分得清是哪条路删的。
LOW_INFORMATION_DROPPED_GOVERNANCE = frozenset(
    {"low_information_dropped", "merged_low_information_dropped"})
# §63 动过手的全部标签（并掉 / 转删）。装配台账与节级报告都从这里取，不在别处再列一遍
# 字符串 —— 那份清单一旦和治理器分叉，"治理掉了多少表"就会有两个答案。
LOW_INFORMATION_GOVERNANCE_LABELS = LOW_INFORMATION_DROPPED_GOVERNANCE | {"merged_low_information"}


def _frame_only(plan: dict[str, Any]) -> bool:
    """一张**已经过完单表治理**的 plan：是否只剩个框。"""
    metrics = plan.get("metrics") or {}
    return (bool(metrics.get("low_information"))
            and not plan.get("is_form")
            and len(plan.get("columns") or []) < FRAME_MAX_COLUMNS
            and int(metrics.get("content_cells") or 0) < FRAME_MAX_CONTENT_CELLS)


def _merge_candidate(table: dict[str, Any]) -> bool:
    """§63：什么样的表才允许参与"同节同列合并"——自己过完治理就只剩个框的那些。

    合并是转删的**替代**动作（Codex §63：merged / converted / deleted），所以 candidacy
    与转删用同一条形状线，不能一边按"只剩一列"删、一边按"字太少"合。
    表单模板的空格是留给甲方填的，两张表单并一张会把待填写区域搞乱；`protected` 的表
    带着需求/评分应答，合了就可能改答题边界；单表治理本来就要删的（无有效内容）也不
    许并进好表 —— 那是把套话洗进别人的表体。
    """
    columns = [normalize_cell(item) for item in (table.get("columns") or [])] if isinstance(table.get("columns"), (list, tuple)) else []
    if not columns or table.get("protected") or table.get("needs_review"):
        return False
    if str(table.get("title") or "").strip() and is_form_table(
            str(table.get("title") or "").strip(), columns,
            str(table.get("archetype") or "").strip().upper()):
        return False
    plan = plan_table({**table, "columns": columns})
    return plan["action"] != "drop" and _frame_only(plan)


def _merged_group(group: list[dict[str, Any]]) -> dict[str, Any]:
    """把同列的低信息表并成一张：行按出现顺序取并集（逐字重复的行只留一条）。"""
    leader = group[0]
    if len(group) == 1:
        return dict(leader)
    columns = [normalize_cell(item) for item in (leader.get("columns") or [])]
    rows: list[list[str]] = []
    seen: set[str] = set()
    for table in group:
        for row in table.get("rows") or []:
            cells = [normalize_cell(cell) for cell in (row if isinstance(row, (list, tuple)) else [row])]
            if len(cells) < len(columns):
                cells += [""] * (len(columns) - len(cells))
            elif len(cells) > len(columns):
                cells = cells[: len(columns) - 1] + ["；".join(cells[len(columns) - 1:])]
            signature = "|".join(cells)
            if not "".join(cells) or signature in seen:
                continue
            seen.add(signature)
            rows.append(cells)
    notes: list[str] = []
    for table in group:
        note = str(table.get("notes") or "").strip()
        if note and note not in notes:
            notes.append(note)
    sources = [str(table.get("title") or "").strip() or "（无表题）" for table in group]
    provenance = [table.get(CU_PROVENANCE_KEY) for table in group if table.get(CU_PROVENANCE_KEY)]
    merged = dict(leader)
    merged.update({
        "columns": columns,
        "rows": rows,
        "notes": "；".join(notes),
        "merged_from": sources,
        "merged_from_count": len(sources),
    })
    if len(provenance) == len(group) and provenance:
        merged[CU_PROVENANCE_KEY] = provenance
    elif provenance:
        # 组里既有 CU 带来的表也有节自带的表：出处只能记能查到的那一半，
        # 并显式标 mixed，不让"部分归属"看起来像"全部归属"。
        merged[CU_PROVENANCE_KEY] = provenance
        merged["provenance_mixed"] = True
    return merged


def plan_section_tables(tables: Iterable[Any]) -> dict[str, Any]:
    """§63 节级表格治理：**先**在同列的低信息表之间做合并，**再**逐张过 `plan_table`。

    这是 `plan_table` 之上的唯一一层节级动作，装配端（build_docx）与预算端
    （longform/cu_assembly）调的是同一个函数、同一份输入 —— 预算要的"治理后会渲染多少字"
    必须和真正渲染出来的形状是同一个决定算出来的，否则§64 就又是两套口径。

    返回 `{"tables": [要渲染的 plan，按原顺序], "ledger": [每一张表的决定], "report": {...}}`。
    """
    rows_in = []
    for table in (tables or []):
        if not isinstance(table, dict):
            continue
        if not isinstance(table.get("columns"), (list, tuple)):
            from governance.table_fidelity import coerce_table
            table = coerce_table(table)
        rows_in.append(dict(table))
    leaders: dict[Any, list[dict[str, Any]]] = {}
    order: list[Any] = []
    for index, table in enumerate(rows_in):
        columns = table.get("columns")
        if table.get("needs_review") or not isinstance(columns, (list, tuple)):
            key = ("single", index)
        else:
            title_key = normalize_cell(table.get("title"))
            key = ("cols", tuple(normalize_cell(item) for item in columns), title_key) \
                if _merge_candidate(table) else ("single", index)
        if key not in leaders:
            order.append(key)
        leaders.setdefault(key, []).append(table)

    ledger: list[dict[str, Any]] = []
    render: list[dict[str, Any]] = []
    merges: list[dict[str, Any]] = []
    for key in order:
        group = leaders[key]
        merged = _merged_group(group)
        if len(group) > 1:
            merges.append({
                "columns": list(merged.get("columns") or []),
                "titles": [str(table.get("title") or "").strip() or "（无表题）" for table in group],
                "into": str(merged.get("title") or "").strip() or "（无表题）",
                "source_tables": len(group),
            })
        plan = plan_table(merged)
        plan["source_tables"] = len(group)
        plan["protected"] = bool(merged.get("protected"))
        plan["merged_from"] = list(merged.get("merged_from") or [])
        plan[CU_PROVENANCE_KEY] = merged.get(CU_PROVENANCE_KEY) or ""
        if plan.get("action") == "review":
            plan["governance"] = "needs_review"
            ledger.append(plan)
            render.append(plan)
            continue
        frame = _frame_only(plan)
        if plan["action"] == "drop":
            plan["governance"] = "dropped_by_table_plan"
        elif len(group) > 1 and not frame:
            plan["governance"] = "merged_low_information"
            if plan["action"] == "render":
                plan["action"] = "compact"
            # 合并的来路一定要写进 reason：单表治理本来就会给"剪掉空列"这种动作，
            # 只在 action 还是 render 时才标 merge，等于让"两张表并一张"在台账里读成
            # "一张表被压缩"，读者就再也数不回被合并掉的那几张表。
            plan["reason"] = "；".join([item for item in (
                f"merge_low_information_tables:{len(group)}", plan["reason"]) if item])
        elif len(group) > 1:
            # 合并只在"并完又变回一张表"时算增益。还是那个框就照样转删，否则两张空框
            # 并一张就能洗到纸上 —— 那是换形状撑页，不是治理。
            plan["governance"] = "merged_low_information_dropped"
            plan["action"] = "drop"
            plan["rows"] = []
            plan["reason"] = (f"{len(group)} 张同列低信息表并入后仍然只剩 "
                              f"{len(plan['columns'])} 列、有效格 {plan['metrics']['content_cells']} 个"
                              f"（可核查 {plan['metrics']['meaningful_chars']} 字）：转删不占页")
        elif frame:
            # §63 的"转删"：合并机会已经用掉，治理完仍然只剩一列、有效格不足 6 个的表，
            # 剩下的只是框和几条词 —— 它占页而不占内容，正是"靠表格撑页"的形状。
            if plan["protected"]:
                plan["governance"] = "low_information_retained_protected"
            else:
                plan["governance"] = "low_information_dropped"
                plan["action"] = "drop"
                plan["reason"] = (f"治理后只剩 {len(plan['columns'])} 列、有效格 "
                                  f"{plan['metrics']['content_cells']} 个"
                                  f"（可核查 {plan['metrics']['meaningful_chars']} 字）："
                                  "表框里没剩多少表，转删不占页")
                plan["rows"] = []
        elif plan["metrics"].get("low_information") and not plan["is_form"]:
            # 薄，但还是一张表（列数或有效格没退化）：留着，并在台账里点名"低信息保留"，
            # 不让它读成"已达标"。
            plan["governance"] = "low_information_retained"
        else:
            plan["governance"] = "rendered"
        ledger.append(plan)
        if plan["action"] != "drop":
            render.append(plan)
    dropped = [plan for plan in ledger if plan["action"] == "drop"]
    return {
        "tables": render,
        "ledger": ledger,
        "report": {
            "schema_version": "section-table-governance-v0.1",
            "tables_planned": len(rows_in),
            "tables_rendered": len(render),
            "tables_dropped": len(dropped),
            "tables_merged_groups": len(merges),
            "tables_merged_sources": sum(int(row["source_tables"]) for row in merges),
            "low_information_merged": sum(int(row["source_tables"]) for row in merges),
            # 与 `low_information_merged` 同一个分母：**按来源表数**。两张框并成一张
            # 再转删，从页面上消失的是两张表；按台账行数数会把这动作读成"删了一张"，
            # 于是 §63 的账与装配台账的 governed_away 差一个数。
            "low_information_dropped": sum(max(1, int(plan.get("source_tables") or 1))
                                           for plan in dropped
                                           if plan.get("governance")
                                           in LOW_INFORMATION_DROPPED_GOVERNANCE),
            # 薄但仍是表的那些：留住了，但要单独数，不能让"没删"读成"达标"。
            "low_information_retained": sum(1 for plan in ledger
                                            if plan.get("governance") == "low_information_retained"),
            "low_information_retained_protected": sum(
                1 for plan in ledger
                if plan.get("governance") == "low_information_retained_protected"),
            "merges": merges,
            "dropped": [{"title": plan["title"] or "（无表题）",
                         "reason": plan["reason"],
                         "governance": plan.get("governance") or "",
                         "meaningful_chars": plan["metrics"]["meaningful_chars"],
                         "content_cells": plan["metrics"]["content_cells"]}
                        for plan in dropped],
        },
    }


def _table_nodes(document) -> list:
    return [node for node in document.iter(W + "tbl")]


def _cell_text(cell) -> str:
    return normalize_cell("".join(node.text or "" for node in cell.iter(W + "t")))


def audit_docx_tables(path) -> dict[str, Any]:
    """按真实交付件测量：一张表是否有效，只看它渲染出来之后格子里有没有字。"""
    with zipfile.ZipFile(str(path)) as book:
        document = etree.fromstring(book.read("word/document.xml"))
    per_table: list[dict[str, Any]] = []
    for index, table in enumerate(_table_nodes(document)):
        grid = [child for child in table if child.tag == W + "tblGrid"]
        columns = len(grid[0]) if grid and len(grid[0]) else 0
        rows: list[list[str]] = []
        header: list[str] = []
        for position, row in enumerate(child for child in table if child.tag == W + "tr"):
            cells = [_cell_text(cell) for cell in row if cell.tag == W + "tc"]
            if position == 0:
                header = cells
            else:
                rows.append(cells)
        metrics = table_metrics(header or [f"列{i + 1}" for i in range(columns)], rows,
                               title=_table_title(document, table))
        metrics["table_index"] = index
        metrics["classification"] = classify_table(metrics)
        if metrics["is_form"]:
            # §61 的九要素允许"列 + 表下字段说明 + 表题"三处共同满足，所以测量端必须把表下
            # 那一段说明读回来 —— 装配端就是把 适用场景/保存位置/保存期限/复核要求 写在那里。
            metrics["form_notes"] = _table_following_text(document, table)
            elements = form_elements_present(header, metrics["form_notes"], rows,
                                             title=metrics["title"])
            metrics["form_elements"] = elements
            metrics["form_elements_missing"] = [name for name, ok in elements.items() if not ok]
        per_table.append(metrics)
    histogram: dict[str, int] = {}
    for item in per_table:
        histogram[item["classification"]] = histogram.get(item["classification"], 0) + 1
    totals = {key: sum(item[key] for item in per_table)
              for key in ("total_cells", "body_cells", "content_cells", "empty_cells",
                          "placeholder_cells", "meaningful_chars", "duplicate_row_count", "rows")}
    # 汇总比值同样以表体为分母：表头是"检查项/标准/记录"这类套话，算进分母会让一张
    # 全空的壳子也拿到 4/N 的"有效内容"—— 上一轮 150 张表看起来没问题就是这个原因。
    body_cells = max(1, totals["body_cells"])
    # §38：表格质量报告按五个口径分开数，缺一不可 —— 上一轮只报"150 张表"，
    # 于是一张 24 行空格子的表单和一张写满技术内容的表在报告里长得一模一样。
    form_tables = [item for item in per_table if item.get("is_form")]
    placeholder_tables = [item for item in per_table
                          if item.get("classification") in {"empty_scaffold", "placeholder_heavy"}
                          or item.get("placeholder_prose_heavy")]
    low_information = [item for item in per_table
                       if item.get("classification") in {"no_cells", "duplicate_rows"}
                       or (item.get("classification") == "meaningful" and item.get("low_information"))]
    return {
        "schema_version": "artifact-table-qa-v0.2",
        "tables_total": len(per_table),
        "tables_meaningful": histogram.get("meaningful", 0),
        "tables_form_templates": len(form_tables),
        "tables_placeholder": len(placeholder_tables),
        "tables_low_information": len(low_information),
        "tables_form_elements_incomplete": sum(1 for item in form_tables
                                               if item.get("form_elements_missing")),
        "tables_rows_over_form_limit": sum(1 for item in form_tables
                                           if item.get("form_rows_over_limit")),
        "placeholder_table_share": round(len(placeholder_tables) / max(1, len(per_table)), 4),
        "tables_rendered": len(per_table),
        "tables_bidder_fill": histogram.get("bidder_fill", 0),
        "tables_empty_scaffold": histogram.get("empty_scaffold", 0),
        "tables_placeholder_heavy": histogram.get("placeholder_heavy", 0),
        "tables_duplicate_rows": histogram.get("duplicate_rows", 0),
        "classification_histogram": histogram,
        "cells_total": totals["total_cells"],
        "cells_body": totals["body_cells"],
        "cells_content": totals["content_cells"],
        "cells_empty": totals["empty_cells"],
        "cells_placeholder": totals["placeholder_cells"],
        "meaningful_cell_ratio_overall": round(totals["content_cells"] / body_cells, 4),
        "empty_cell_ratio_overall": round(totals["empty_cells"] / body_cells, 4),
        "placeholder_cell_ratio_overall": round(totals["placeholder_cells"] / body_cells, 4),
        "meaningful_table_chars": totals["meaningful_chars"],
        "table_rows_total": totals["rows"],
        "duplicate_row_count_total": totals["duplicate_row_count"],
        "meaningful_table_share": round(histogram.get("meaningful", 0) / max(1, len(per_table)), 4),
        "per_table": per_table,
    }


def _table_title(document, table) -> str:
    """表标题是表格上方的段落：投标填写标记就写在标题里。"""
    node = table.getprevious()
    steps = 0
    while node is not None and steps < 3:
        if node.tag == W + "p":
            text = normalize_cell("".join(child.text or "" for child in node.iter(W + "t")))
            if text:
                return text
        if node.tag == W + "tbl":
            break
        node = node.getprevious()
        steps += 1
    return ""


def _table_following_text(document, table, limit: int = 3) -> str:
    """表单的字段说明是表格**下方**的那一段：九要素里的 适用场景/保存位置/保存期限/复核要求 写在那里。"""
    node = table.getnext()
    steps = 0
    while node is not None and steps < limit:
        if node.tag == W + "tbl":
            break
        if node.tag == W + "p":
            text = normalize_cell("".join(child.text or "" for child in node.iter(W + "t")))
            if text:
                return text
        node = node.getnext()
        steps += 1
    return ""


def table_findings(audit: dict[str, Any], *, path: Any = "") -> list[dict[str, Any]]:
    """交付门：不达标的表必须留痕，但严重性是 AUTO_REPAIR —— Word 仍要交付。"""
    findings: list[dict[str, Any]] = []
    rules = {
        "empty_scaffold": ("TABLE_EMPTY_SCAFFOLD", "表格内几乎没有可核查内容"),
        "placeholder_heavy": ("TABLE_PLACEHOLDER_HEAVY", "表格以占位符或空单元格为主"),
        "duplicate_rows": ("TABLE_DUPLICATE_ROWS", "表格存在成片逐字重复的行"),
    }
    for item in audit.get("per_table") or []:
        entry = rules.get(item.get("classification"))
        if entry is None and item.get("placeholder_prose_heavy"):
            # §36：表单的格子可以空着，但不能填成"按项目要求填写"。形态判定仍留给
            # bidder_fill/form_template，这条缺陷单独记，否则标记就成了套话的通行证。
            entry = ("TABLE_PLACEHOLDER_HEAVY", "表格格子以「按项目要求填写」这类套话填满")
        if entry:
            rule_id, phrase = entry
            findings.append({
                "rule_id": rule_id,
                "path": f"{path}#table[{item['table_index']}]",
                "severity": "AUTO_REPAIR",
                "issue": (f"{phrase}：有效单元格 {item['meaningful_cell_ratio']:.0%}、空格 "
                          f"{item['empty_cell_ratio']:.0%}、占位话 "
                          f"{item['placeholder_cell_ratio']:.0%}、重复行 {item['duplicate_row_ratio']:.0%}。"
                          f"表题：{(item['title'] or '（无表题）')[:40]}"),
                "table_index": item["table_index"],
                "metrics": {key: item[key] for key in ("total_cells", "content_cells", "empty_cells",
                                                       "placeholder_cells", "meaningful_cell_ratio",
                                                       "duplicate_row_ratio", "rows", "columns")},
            })
        if not item.get("is_form"):
            continue
        # §36/§61：表单允许待填写字段，但九要素必须齐；缺哪几项就点名哪几项。
        missing = item.get("form_elements_missing") or []
        if missing:
            findings.append({
                "rule_id": "FORM_ELEMENTS_INCOMPLETE",
                "path": f"{path}#table[{item['table_index']}]",
                "severity": "AUTO_REPAIR",
                "issue": (f"表单模板缺字段要素：{'、'.join(missing)}。"
                          f"表题：{(item['title'] or '（无表题）')[:40]}"),
                "table_index": item["table_index"],
                "missing_elements": missing,
            })
        # §37：>6 行示例结构的表单是撑页，不是内容。
        if item.get("form_rows_over_limit"):
            findings.append({
                "rule_id": "FORM_TEMPLATE_BLOAT",
                "path": f"{path}#table[{item['table_index']}]",
                "severity": "AUTO_REPAIR",
                "issue": (f"表单模板渲染了 {item['rows']} 行，超过 {FORM_EXAMPLE_ROW_MAX} 行示例结构上限："
                          f"表题：{(item['title'] or '（无表题）')[:40]}"),
                "table_index": item["table_index"],
                "rows": item["rows"],
            })
    # §39：占位表占比过高只把交付拉到 NEEDS_REVIEW，不整篇 FAIL。
    share = float(audit.get("placeholder_table_share") or 0.0)
    if share > PLACEHOLDER_TABLE_SHARE_REVIEW:
        findings.append({
            "rule_id": "TABLE_PLACEHOLDER_RATIO",
            "path": str(path),
            "severity": "AUTO_REPAIR",
            "issue": (f"占位表占比 {share:.0%}（{audit.get('tables_placeholder', 0)}/"
                      f"{audit.get('tables_total', 0)} 张）超过 {PLACEHOLDER_TABLE_SHARE_REVIEW:.0%}："
                      "需要复核这些表是不是在撑篇幅"),
            "placeholder_table_share": share,
        })
    return findings
