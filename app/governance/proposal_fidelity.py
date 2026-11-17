"""Tender-faithful cleanup shared by planning, assembly, and Word rendering.

Project names and project facts stay in the run data. This module only applies
generic overlap, block-order, and artifact rules.
"""
from __future__ import annotations

import json
import re
from typing import Any


_STOP = {
    "服务", "管理", "人员", "岗位", "配置", "项目", "工作", "要求", "标准", "内容",
    "进行", "以及", "相关", "按照", "合同", "约定", "确认", "实施", "日常", "现场",
    "方案", "提供", "负责", "本次", "范围", "属于", "安排", "执行", "情况", "说明",
    "根据", "其中", "可以", "应当", "需要", "不得", "本项", "招标", "投标", "包括",
    "以及", "及其", "或者", "如果", "对于", "通过", "完成", "开展", "建立", "形成",
}
_NEGATION = re.compile(r"不含|不包括|不包含|不在本次|另行采购|不安排|不负责|不承担|不纳入|不在范围内")
_EXCLUSION_ROW = re.compile(r"排除|不在范围内|不安排|不含|不包括|不包含|不负责|不承担")
_BOUNDARY = re.compile(r"交接|分界|交界")
_EXECUTION = re.compile(r"拖擦|清扫|打蜡|吸尘|值守|巡查|每日|每周")
_LEAD = re.compile(r"见下表|如下表|按以下清单|按以下步骤|按以下九项|汇总如下|规范如下|具体步骤如下|如下：|如下:")
_STEP_COUNT = re.compile(r"按以下[一二三四五六七八九十\d]+(?:个)?(?:步|步骤|环节|项)")
_LEAD_TAIL = re.compile(r"(见下表|如下表|按以下清单|按以下步骤|按以下九项|汇总如下|规范如下|具体步骤如下|如下)[。：:\s]*$|[:：]\s*$")
_LIST_KINDS = {"bullet_group", "numbered_steps", "table"}
_CU = re.compile(r"CU-[\u4e00-\u9fa5A-Za-z0-9_-]+")
_STEPS = re.compile(r"标准作业流程_STEPS:|STEPS:")
_MARK = re.compile(r"〔\d+〕")
_LIST_COMMA = re.compile(r"^(\d+\.\s*)[，,]\s*")
_MECHANICAL = re.compile(r"[:：]\s*步骤\s*\d+(?:\s*/\s*\d+)?")
_FIGURE = re.compile(r"如下图所示|本图打印后张贴|组织图中各节点的箭头方向")
_HALF = re.compile(r"需要与([^。]{1,16})对接的数据。?$")
_CN = "零一二三四五六七八九十"
_SERVICE_TOPICS = (
    ("健康管理", ("健康管理", "专业护士", "护士")),
    ("餐饮服务", ("餐饮",)),
    ("绿化养护", ("绿化", "绿植")),
    ("礼宾会务", ("礼宾", "会务", "会议服务")),
    ("库房管理", ("库房",)),
    ("档案管理", ("档案",)),
    ("其他委托事项", ("委托事项", "交流干部", "理发")),
    ("项目建议", ("项目建议",)),
    ("巡检、维保与故障闭环", ("巡检", "维保", "故障闭环")),
)


def _chars(text: str) -> list[str]:
    return re.findall(r"[\u4e00-\u9fa5]", str(text or ""))


def content_terms(text: str) -> set[str]:
    chars = _chars(text)
    grams = {"".join(chars[i:i + n]) for n in (2, 3, 4) for i in range(len(chars) - n + 1)}
    return {gram for gram in grams if gram not in _STOP}


def chapter_label(number: int) -> str:
    if number <= 0:
        return "一"
    if number <= 10:
        return "十" if number == 10 else _CN[number]
    if number < 20:
        return "十" + _CN[number - 10]
    tens, ones = divmod(number, 10)
    return _CN[tens] + "十" + (_CN[ones] if ones else "")


def clean_internal_artifacts(text: str) -> str:
    """Strip workflow markers before they reach a customer document."""
    value = str(text or "")
    value = _CU.sub("", value)
    value = value.replace("确保本Section所载内容", "确保本节所载内容")
    value = re.sub(r"(?<![A-Za-z])Section(?![A-Za-z])", "本节", value)
    value = value.replace("大专及及以上", "大专及以上")
    value = _STEPS.sub("", value)
    value = _MARK.sub("", value)
    value = _LIST_COMMA.sub(r"\1", value)
    value = _HALF.sub(r"需要与\1对接的数据，由项目组按接口台账核对后使用。", value)
    value = value.replace(
        "低于合同约定的指标时甲方有权提前终止合同，低于合同约定的指标",
        "低于合同约定的指标时甲方有权提前终止合同",
    )
    value = re.sub(r"沿用本节〔\d+〕所述要求[，,]?", "按前述既定标准与时限要求执行，", value)
    value = re.sub(r"〔\d+〕", "", value)
    value = value.replace("该项安排而是让", "该项安排旨在让")
    value = re.sub(r"相关证明资料尚待核实[。，；\s]*", "", value)
    value = re.sub(r"(尚需明确|尚待明确|尚待核实)\s*尚待核实", "待双方书面确认后执行", value)
    value = re.sub(r"[，,\s]*尚待核实(?=[。；，\s]|$)", "", value)
    value = value.replace("正式经确认的人员通行凭证", "正式门禁卡")
    value = value.replace("临时经确认的人员通行凭证", "临时门禁卡")
    value = value.replace("经确认的人员通行凭证", "门禁卡")
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r"\s+([，。；：])", r"\1", value)
    return value.strip()


def normalize_heading(text: str, level: int, chapter_no: int, section_no: int) -> tuple[str, str]:
    """Return ('heading', numbered title) or ('lead', bold lead-in)."""
    title = clean_internal_artifacts(text)
    mechanical = bool(_MECHANICAL.search(title))
    title = _MECHANICAL.sub("", title).strip(" 《》")
    title = re.sub(r"\s+", " ", title).strip()
    if mechanical:
        return "lead", title or "办理顺序"
    if level == 1:
        if not re.match(r"^第[零一二三四五六七八九十百]+章", title):
            title = f"第{chapter_label(chapter_no)}章 {title}".strip()
    elif level == 2:
        if not re.match(r"^\d+(?:\.\d+)*\s", title):
            title = f"{chapter_no}.{section_no} {title}".strip()
    return "heading", title


_BLANK_FIELD = re.compile(r"签名|签字|日期|实测")
_BLANK_PLACEHOLDER = re.compile(r"^(?:按现场记录填写|按标准执行|待填写|请填写|尚待核实|/|—|-|－)$")


def display_cell(value: Any, *, header: bool, column_name: str = "", row_label: str = "") -> str:
    shown = clean_internal_artifacts(str(value or ""))
    manual = _BLANK_FIELD.search(str(column_name or "")) or _BLANK_FIELD.search(str(row_label or ""))
    if manual and (not shown or _BLANK_PLACEHOLDER.search(shown)):
        return ""
    if shown and shown not in {"按标准执行", "待确认", "尚待核实"}:
        return shown
    if header:
        return "列项"
    name = str(column_name or "")
    if "责任岗位" in name:
        return "项目负责人核对，专业岗位登记"
    if "触发条件" in name:
        return "对应事项发生并完成现场核对后当日登记"
    if shown:
        return shown
    return "按现场记录填写"


def fill_table_cells(table: dict[str, Any]) -> dict[str, Any]:
    raw_columns = list(table.get("columns") or table.get("headers") or [])
    columns = [display_cell(cell, header=True) for cell in raw_columns]
    rows = []
    for row in table.get("rows") or []:
        if isinstance(row, (list, tuple)):
            label = str(row[0] if row else "")
            rows.append([
                display_cell(
                    cell, header=False,
                    column_name=str(raw_columns[index] if index < len(raw_columns) else ""),
                    row_label="" if index == 0 else label,
                )
                for index, cell in enumerate(row)
            ])
    filled = dict(table)
    if columns:
        filled["columns"] = columns
    if rows:
        filled["rows"] = rows
    return filled


def _row_text(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("text") or "")
    return str(row or "")


_GENERIC_SCOPE = {
    "保洁", "清洁", "服务", "管理", "维护", "巡检", "安保", "秩序", "绿化",
    "工程", "餐饮", "会务", "接待", "包含", "明确", "排除", "范围",
}


def _scope_objects(text: str) -> set[str]:
    """Specific place or service objects, without words such as 保洁 shared by both sides."""
    cleaned = _NEGATION.sub(" ", str(text or ""))
    cleaned = re.sub(r"包含|明确|排除|服务范围|不纳入|日常|本次|物业", " ", cleaned)
    objects: set[str] = set()
    for match in re.finditer(r"([\u4e00-\u9fa5]{2,12})(?:保洁|清洁|服务|管理|维护|巡检)", cleaned):
        part = re.sub(r"^(?:服务|纳入|属于|包含|提供|负责|安排)+", "", match.group(1))
        if len(part) >= 2 and part not in _GENERIC_SCOPE:
            objects.add(part)
    return objects


def scope_conflict_pairs(included: list[Any], excluded: list[Any]) -> list[dict[str, Any]]:
    """Record a conflict only when the same specific object is both required and excluded."""
    pairs: list[dict[str, Any]] = []
    for exc in excluded or []:
        exc_text = _row_text(exc)
        exc_objects = _scope_objects(exc_text)
        if not exc_objects:
            continue
        for inc in included or []:
            inc_text = _row_text(inc)
            shared = exc_objects & _scope_objects(inc_text)
            if not shared:
                continue
            inc_source = ((inc.get("sources") if isinstance(inc, dict) else None) or [{}])[0]
            exc_source = ((exc.get("sources") if isinstance(exc, dict) else None) or [{}])[0]
            pairs.append({
                "included_text": inc_text,
                "excluded_text": exc_text,
                "included_source": inc_source,
                "excluded_source": exc_source,
                "objects": sorted(shared),
                "reason": "同一服务对象在招标文件中同时出现纳入和排除表述，两边原文和出处均保留，待确认。",
            })
    return pairs


def reconcile_excluded_scope(excluded: list[Any], whitelist_text: str) -> tuple[list[Any], list[str]]:
    """Keep every exclusion. Overlap is reported separately and does not delete a row."""
    return list(excluded or []), []


def whitelist_from_extraction(extraction: dict[str, Any] | None) -> str:
    parts: list[str] = []
    for paragraph in (extraction or {}).get("paragraphs") or []:
        text = str(paragraph.get("text") or "")
        path = " ".join(str(item) for item in (paragraph.get("heading_path") or []))
        blob = path + "\n" + text
        if _NEGATION.search(blob):
            continue
        if re.search(r"服务内容|服务范围|岗位|附件|专项", blob[:80]) or path:
            parts.append(blob)
    for table in (extraction or {}).get("tables") or []:
        headers = " ".join(str(item) for item in (table.get("headers") or table.get("columns") or []))
        if "岗位" not in headers and "服务" not in headers:
            continue
        parts.append(headers)
        for row in (table.get("rows") or [])[:80]:
            if isinstance(row, (list, tuple)) and row:
                parts.append(str(row[0]))
    return "\n".join(parts)


def filter_exclusion_tables(section: dict[str, Any], whitelist_text: str, excluded_text: str = "") -> dict[str, Any]:
    """Drop procedure rows whose subject is an excluded area.

    A row that states the exclusion itself stays, so an included service and
    its excluded neighbour can both remain visible.
    """
    excluded_objects = _scope_objects(excluded_text)
    if not excluded_objects:
        return section

    def keep_text(text: str) -> bool:
        if not any(item and item in text for item in excluded_objects):
            return True
        if _EXCLUSION_ROW.search(text) or _NEGATION.search(text):
            return True
        if _BOUNDARY.search(text) and not _EXECUTION.search(text):
            return True
        return False

    def keep_row(row: Any) -> bool:
        if isinstance(row, (list, tuple)):
            text = " ".join(str(cell) for cell in row)
        else:
            text = str(row or "")
        return keep_text(text)

    def scrub(table: dict[str, Any]) -> dict[str, Any]:
        rows = table.get("rows")
        if not isinstance(rows, list):
            return table
        return {**table, "rows": [row for row in rows if keep_row(row)]}

    tables = [scrub(table) if isinstance(table, dict) else table for table in (section.get("tables") or [])]
    paragraphs = [item for item in (section.get("paragraphs") or []) if keep_text(str(item))]
    bullets = [item for item in (section.get("bullets") or []) if keep_text(str(item))]
    blocks = []
    for block in section.get("content_blocks") or []:
        if not isinstance(block, dict):
            blocks.append(block)
            continue
        if block.get("type") == "table" and isinstance(block.get("table"), dict):
            blocks.append({**block, "table": scrub(block["table"])})
            continue
        items = block.get("items")
        content = str(block.get("content") or "")
        if content and not keep_text(content):
            continue
        if isinstance(items, list):
            kept_items = [item for item in items if keep_text(str(item if not isinstance(item, dict) else item.get("text") or item))]
            blocks.append({**block, "items": kept_items})
            continue
        blocks.append(block)
    return {**section, "tables": tables, "paragraphs": paragraphs, "bullets": bullets, "content_blocks": blocks}


def _table_label(table: dict[str, Any]) -> str:
    columns = table.get("columns") or table.get("headers") or []
    return " ".join([str(table.get("title") or "")] + [str(item) for item in columns])


def _matching_table(lead: str, tables: list[dict[str, Any]]) -> int | None:
    """Attach a table only when the lead-in and the table name the same subject."""
    matches = [index for index, table in enumerate(tables) if content_terms(lead) & content_terms(_table_label(table))]
    if len(matches) == 1:
        return matches[0]
    return None


def place_content_blocks(blocks: list[dict[str, Any]], tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep each table or list with the lead-in that introduced it."""
    unused = [fill_table_cells(table) for table in tables if isinstance(table, dict)]
    placed: list[dict[str, Any]] = []
    index = 0
    while index < len(blocks):
        block = dict(blocks[index])
        kind = str(block.get("type") or "paragraph")
        content = str(block.get("content") or "")
        nxt = blocks[index + 1] if index + 1 < len(blocks) else None
        nxt_kind = str((nxt or {}).get("type") or "")
        is_lead = kind in {"paragraph", "subheading"} and bool(_LEAD.search(content) or content.rstrip().endswith(("：", ":")))
        follower = nxt_kind in _LIST_KINDS or (nxt_kind == "subheading" and False)
        if _STEP_COUNT.search(content) and nxt_kind not in {"bullet_group", "numbered_steps"}:
            content = _STEP_COUNT.sub("按本节流程", content)
            block["content"] = content
            is_lead = bool(_LEAD.search(content) or content.rstrip().endswith(("：", ":")))
        if is_lead and not follower:
            pick = _matching_table(content, unused)
            if pick is not None:
                placed.append(block)
                placed.append({"type": "table", "table": unused.pop(pick)})
            else:
                cleaned = _LEAD_TAIL.sub("。", content).strip()
                if cleaned in {"", "。"}:
                    cleaned = "相关安排按本节正文执行。"
                placed.append({**block, "type": "paragraph", "content": cleaned})
            index += 1
            continue
        if kind == "table" and isinstance(block.get("table"), dict):
            block["table"] = fill_table_cells(block["table"])
        placed.append(block)
        index += 1
    for table in unused:
        placed.append({"type": "table", "table": table})
    return _collapse_lead_stack(placed)


def _collapse_lead_stack(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    collapsed: list[dict[str, Any]] = []
    for block in blocks:
        content = str(block.get("content") or "")
        previous = collapsed[-1] if collapsed else None
        previous_lead = bool(previous and previous.get("type") in {"paragraph", "subheading"} and _LEAD.search(str(previous.get("content") or "")))
        if previous_lead and block.get("type") in {"paragraph", "subheading"} and _LEAD.search(content):
            cleaned = _LEAD_TAIL.sub("。", content).strip() or "相关安排按本节正文执行。"
            collapsed.append({**block, "type": "paragraph", "content": cleaned})
            continue
        collapsed.append(block)
    return collapsed


_OUTLINE_LEAK = re.compile(r"含.{0,16}(?:计划表|事项表|机制表|跟踪表)")
_LATIN_SPLICE = re.compile(r"([\u4e00-\u9fa5])\s+[A-Za-z]{1,3}(?=[\u4e00-\u9fa5])")


def safe_unwrap_to_text(val: Any) -> str:
    """Turn a dict, list, or their printed form into sentences. Do not leave {'': ...} or ['...'] in the document."""
    if val is None:
        return ""
    if isinstance(val, dict):
        parts = [str(item).strip() for item in val.values() if str(item).strip()]
        if len(parts) >= 3:
            return f"{parts[0]}：{parts[1]}（责任岗位：{parts[2]}）。"
        return "：".join(parts)
    if isinstance(val, (list, tuple)):
        parts = [safe_unwrap_to_text(item).strip("。；; ") for item in val if safe_unwrap_to_text(item).strip()]
        return "；".join(parts) + ("。" if parts else "")
    text = str(val).strip()
    if (text.startswith("{") and text.endswith("}")) or re.search(r"^\d+\.\s*\{.*\}$", text):
        prefix_match = re.match(r"^(\d+\.\s*)", text)
        prefix = prefix_match.group(1) if prefix_match else ""
        values = [item.strip(" ：:，,") for item in re.findall(r"['\"]([^'\"]*)['\"]", text[len(prefix):])]
        values = [item for item in values if item]
        if len(values) >= 3:
            text = f"{prefix}{values[0]}：{values[1]}（责任岗位：{values[2]}）。"
        elif values:
            text = f"{prefix}{'：'.join(values)}。"
    elif text.startswith("表注：[") or (text.startswith("[") and text.endswith("]")):
        is_note = text.startswith("表注：")
        body = text[3:].strip() if is_note else text
        values = [item.strip(" ：:，,") for item in re.findall(r"['\"]([^'\"]*)['\"]", body)]
        values = [item for item in values if item]
        if values:
            joined = "；".join(values)
            text = f"表注：{joined}。" if is_note else f"{joined}。"
    text = _LATIN_SPLICE.sub(r"\1", text)
    return text


def filter_and_renumber_steps(steps: list[Any]) -> list[str]:
    cleaned = []
    for step in steps:
        text = safe_unwrap_to_text(step)
        text = re.sub(r"^\d+[\.\、\s]*", "", text).strip()
        if text:
            cleaned.append(text)
    return [f"{index}. {item}" for index, item in enumerate(cleaned, start=1)]


def prepare_paragraph(text: str, *, has_visual: bool) -> str:
    value = safe_unwrap_to_text(text)
    value = clean_internal_artifacts(value)
    if _OUTLINE_LEAK.search(value) and len(value) <= 90:
        value = "该项安排写明对象、责任岗位、完成条件和核验记录。编写提示不直接作为正文。"
    value = value.replace("相关证明资料尚待核实", "具体证明文件随商务文件一并提交核验")
    value = value.replace("尚待核实", "按双方确认的安排执行")
    if not has_visual and _FIGURE.search(value):
        value = _FIGURE.sub("", value).strip(" ，。；")
        value = (value + "。" if value else "") + "组织按三层落实：指挥决策层、专业执行层、现场作业层。各层职责以岗位分工表为准。"
    return value


def recall_missing_facts(sections: list[dict[str, Any]], brief: dict[str, Any] | None) -> None:
    """Append tender metrics the body never mentioned. No-op without a brief list."""
    tokens = mandatory_fact_tokens(brief or {})
    if not tokens or not sections:
        return
    blob = "\n".join(_section_blob(section) for section in sections)
    missing = []
    for token in tokens:
        if token and token not in blob and token not in missing:
            missing.append(token)
    if not missing:
        return
    target = next((section for section in sections if int(section.get("level") or 2) == 2), sections[0])
    line = "下列表述摘自招标材料，引用时须连同原条件和出处核对，正文未展开的不视为已确认承诺：" + "；".join(missing[:24]) + "。"
    target.setdefault("paragraphs", []).append(line)
    target.setdefault("content_blocks", []).append({"type": "paragraph", "content": line})


def mandatory_fact_tokens(brief: dict[str, Any]) -> list[str]:
    """Full sourced clauses. A bare number is not recalled as a commitment."""
    clauses: list[str] = []
    entries: list[Any] = []
    entries.extend(brief.get("must_requirements") or [])
    entries.extend(brief.get("mandatory_fact_terms") or [])
    for item in entries:
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("source_text") or "")
            locator = str(item.get("locator") or item.get("source") or "")
        else:
            text, locator = str(item), ""
        sentence = text.strip()
        if not re.search(r"\d", sentence):
            continue
        if re.fullmatch(r"(?:不少于|不低于|不超过|不大于|不小于|以内|以下)?\s*\d+(?:\.\d+)?\s*(?:人|名|小时|分钟|次|%|％)", re.sub(r"\s+", "", sentence)):
            continue
        if locator and locator not in sentence:
            sentence = f"{sentence}（出处：{locator}）"
        if sentence not in clauses:
            clauses.append(sentence)
    location = str(brief.get("location") or "").strip()
    if location and location not in clauses and not re.fullmatch(r"\d+(?:\.\d+)?", location):
        clauses.append(location)
    return clauses


def _section_blob(section: dict[str, Any]) -> str:
    chunks = [str(section.get("heading") or "")]
    chunks.extend(str(item) for item in (section.get("paragraphs") or []))
    chunks.extend(str(item) for item in (section.get("bullets") or []))
    for block in section.get("content_blocks") or []:
        if isinstance(block, dict):
            chunks.append(str(block.get("content") or ""))
            chunks.extend(str(item) for item in (block.get("items") or []))
    return "\n".join(chunks)


def mandatory_scope_text(pack: dict[str, Any] | None) -> str:
    pack = pack or {}
    parts = [str(pack.get("mandatory_scope_text") or "")]
    for row in (pack.get("service_scope") or {}).get("included") or []:
        parts.append(_row_text(row))
    for row in pack.get("scoring_items") or []:
        if isinstance(row, dict):
            parts.append(str(row.get("label") or ""))
    return "\n".join(part for part in parts if part.strip())


def _scoring_response_titles(pack: dict[str, Any], titles: str) -> list[str]:
    """Mount independent scoring dimensions the tender actually names."""
    blob = "\n".join([
        mandatory_scope_text(pack),
        json.dumps(pack.get("scoring_items") or [], ensure_ascii=False),
        json.dumps(pack.get("commercial_or_assessment_requirements") or [], ensure_ascii=False),
    ])
    wanted = []
    if ("服务承诺" in blob or "优惠条件" in blob) and "服务承诺" not in titles:
        wanted.append("服务承诺及优惠条件")
    if "合理化建议" in blob and "合理化建议" not in titles:
        wanted.append("合理化建议")
    if "LEED" in blob and "LEED" not in titles:
        wanted.append("协助通过 LEED O+M 绿色运营认证专项方案")
    return wanted


def append_uncovered_topics(outline: list[dict[str, Any]], pack: dict[str, Any]) -> list[dict[str, Any]]:
    """Add a section when a scoring or included-scope topic has no chapter title."""
    titles = " ".join(
        str(chapter.get("chapter_title") or "") + " " + " ".join(str(row.get("section_title") or "") for row in chapter.get("sections") or [])
        for chapter in outline
    )
    excluded = " ".join(_row_text(row) for row in (pack.get("service_scope") or {}).get("excluded") or [])
    demanded = mandatory_scope_text(pack)
    extra = []
    for title, keywords in _SERVICE_TOPICS:
        if any(keyword in titles for keyword in keywords):
            continue
        if not any(keyword in demanded for keyword in keywords):
            continue
        if any(keyword in excluded for keyword in keywords) and not any(keyword in demanded.replace(excluded, "") for keyword in keywords):
            continue
        if any(keyword in excluded for keyword in keywords) and not any(keyword in str(pack.get("mandatory_scope_text") or "") + " ".join(_row_text(row) for row in (pack.get("service_scope") or {}).get("included") or []) for keyword in keywords):
            continue
        extra.append(title)
    scoring = _scoring_response_titles(pack, titles)
    if not extra and not scoring:
        return outline
    engineering = [title for title in extra if "巡检" in title]
    others = [title for title in extra if title not in engineering] + scoring
    copied = [dict(chapter, sections=[dict(row) for row in chapter.get("sections") or []]) for chapter in outline]
    if engineering:
        host = next((chapter for chapter in copied if "工程" in str(chapter.get("chapter_title") or "")), None)
        if host is None:
            host = {"chapter_id": "CH06", "chapter_title": "工程设施运行与维护管理", "target_pages": 6, "priority": "high", "sections": []}
            copied.append(host)
        for offset, title in enumerate(engineering, 1):
            host["sections"].append({"section_id": f"S06-9{offset}", "section_title": title})
    if others:
        host = next((chapter for chapter in copied if "评分专项" in str(chapter.get("chapter_title") or "")), None)
        if host is None:
            host = {"chapter_id": "CH12", "chapter_title": "评分专项响应", "target_pages": max(4, len(others)), "priority": "high", "sections": []}
            copied.append(host)
        start = len(host.get("sections") or []) + 1
        host.setdefault("sections", [])
        for offset, title in enumerate(others, start):
            host["sections"].append({"section_id": f"S12-{offset:02d}", "section_title": title})
    return copied


def gap_stub(title: str, section_id: str) -> dict[str, Any]:
    """A missing section still gets two working paragraphs and one table. Never a one-line shell."""
    work = re.sub(r"\s+", "", str(title or "")) or "本项服务"
    if "合理化建议" in work:
        paragraphs = [
            f"{work}写给委托方，只讨论本次材料已经写明的业态、界面和待确认条件，不写成员工填写建议单、评审试行和固化核销。",
            "每条建议分开写当前痛点、可执行措施、落地步骤，以及能够核对的管理结果。材料里没有的面积、人数、费用和节省比例不写入建议。",
            "项目负责人与委托方核对哪些建议纳入服务安排。未采纳的事项保留原因，不在正文里另造承诺。",
        ]
        rows = [
            ["痛点", "委托范围里已经出现、尚未分开安排的事项", "对照材料核对"],
            ["措施", "责任岗位可以执行的调整", "写明动作和交接"],
            ["结果", "可以核对的管理变化", "不写未出现的节省比例"],
        ]
    else:
        paragraphs = [
            f"{work}由项目负责人指定责任岗位。岗位先核对委托范围里的对应事项，再按现场条件安排动作、交接和完成时点。",
            f"每次安排留下对象、时间和结果。未完成事项在下一次核对前关闭，或者写明未关闭的原因，不把未确认的数量写成承诺。",
            "需要其他专业配合时，由项目负责人约定交接条件。记录随事项归档，供后续检查对照。",
        ]
        rows = [
            [work, "项目负责人指定的责任岗位", "留下对象、时间和结果"],
            ["未完成事项", "责任岗位", "下一次核对前关闭或说明原因"],
        ]
    table = {"title": f"{work}安排", "columns": ["事项", "安排", "核验"], "rows": rows}
    blocks = [{"type": "paragraph", "content": text} for text in paragraphs]
    blocks.append({"type": "table", "table": table})
    return {
        "heading": title,
        "paragraphs": paragraphs,
        "bullets": [],
        "tables": [table],
        "content_blocks": blocks,
        "level": 2,
        "section_id": section_id,
    }
