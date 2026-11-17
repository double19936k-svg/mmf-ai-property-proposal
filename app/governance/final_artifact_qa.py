from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from .artifact_qa import evaluate_artifact
from .artifact_qa import normalize_quotes
from .client_facing_prose import CLIENT_FACING_PROSE_RULE_ID, internal_narration_findings
from .commitment_provenance import canonical_claim_record, evaluate_commitments
from .customer_hygiene import hygiene_findings, rewrite_customer_hygiene
from qa_severity import (
    CLASS_FALSE_POSITIVE, CLASS_NEEDS_REVIEW, CLASS_TRUE_BLOCK, QA_CLASSES, classify_findings,
    clause_of,
)
from .table_qa import audit_docx_tables, classify_cell, table_findings
from .r06_claim_integrity import scan_promised_counts
from .toc_qa import evaluate_native_toc
from planning.content_engineering import sanitize_technical_body_scope_text, scan_scope_text


def _heading_level(style_name: str) -> int | None:
    name = str(style_name or "").strip()
    match = re.search(r"(?:Heading|标题)\s*([1-9])", name, re.I)
    return int(match.group(1)) if match else None


def _looks_like_heading(text: str, style_name: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    if _heading_level(style_name) is not None:
        return True
    if str(style_name or "").lower().startswith("list"):
        return False
    return bool(4 <= len(compact) <= 30 and not re.search(r"[，。；：！？,.!?;:]", compact))


def _docx_blocks(path: Path) -> list[dict[str, Any]]:
    doc = Document(str(path))
    blocks: list[dict[str, Any]] = []
    paragraph_index = 0
    table_index = 0
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            paragraph = Paragraph(child, doc)
            text = str(paragraph.text or "").strip()
            style = str(paragraph.style.name if paragraph.style is not None else "")
            if text:
                props = paragraph._p.pPr
                numbering = props.numPr if props is not None else None
                style_cursor = paragraph.style
                while numbering is None and style_cursor is not None:
                    style_props = style_cursor.element.pPr
                    numbering = style_props.numPr if style_props is not None else None
                    style_cursor = style_cursor.base_style
                number_format = None
                if numbering is not None and numbering.numId is not None:
                    number_id = numbering.numId.val
                    number_level = numbering.ilvl.val if numbering.ilvl is not None else 0
                    numbering_root = doc.part.numbering_part.element
                    abstracts = numbering_root.xpath(f'./w:num[@w:numId="{number_id}"]/w:abstractNumId/@w:val')
                    if abstracts:
                        formats = numbering_root.xpath(f'./w:abstractNum[@w:abstractNumId="{abstracts[0]}"]/w:lvl[@w:ilvl="{number_level}"]/w:numFmt/@w:val')
                        number_format = formats[0] if formats else None
                blocks.append({
                    "path": f"paragraph[{paragraph_index}]", "paragraph_index": paragraph_index,
                    "text": text, "kind": "paragraph", "heading_level": _heading_level(style),
                    "heading_candidate": _looks_like_heading(text, style), "style": style,
                    "numbering": ({"num_id": numbering.numId.val,
                                   "level": numbering.ilvl.val if numbering.ilvl is not None else 0,
                                   "format": number_format}
                                  if numbering is not None and numbering.numId is not None else None),
                })
            paragraph_index += 1
        elif child.tag.endswith("}tbl"):
            table = Table(child, doc)
            text = "\n".join(" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows).strip()
            if text:
                blocks.append({"path": f"table[{table_index}]", "text": text, "kind": "table", "heading_level": None, "style": "Table",
                               "table_rows": [[cell.text.strip() for cell in row.cells] for row in table.rows],
                               "table_header_rows": [bool(row._tr.xpath('./w:trPr/w:tblHeader[not(@w:val="0" or @w:val="false" or @w:val="off")]')) for row in table.rows],
                               "table_has_merged_cells": bool(table._tbl.xpath('.//w:vMerge|.//w:gridSpan'))})
            table_index += 1
    return blocks


# ── §29/§30：真正空标题的唯一口径 ─────────────────────────────────────────────
# EMPTY_HEADING_TRUE 只有一种写法成立：标题之后、下一个**同级或更高级**标题之前，
# 正文 / 表格 / 清单 / 流程一概没有。§30 列出的六类（封面、目录、一级章节页、
# Volume Divider、标题后直接跟表格、标题后直接跟列表、合法章节分隔）都不算缺陷 ——
# 它们在这一定义下自然落空，不再靠"紧邻另一个标题就算空"这种旧判据（那是上一轮
# 33 条 EMPTY_HEADING_BLOCK 全误报的根因：`_looks_like_heading` 把封面行、目录条目、
# 流程标签都当成了标题）。
MIN_BODY_CHARS = 20
TOC_TITLE = {"目录", "目 录", "目次", "contents"}
TOC_ENTRY = re.compile(r"[\t\u3000 ]\s*\d{1,4}\s*$")
LIST_STYLE = re.compile(r"^(?:list|列表|项目符号|编号|bullet)", re.I)
# 引导句载体里的有序步骤。只认编号/步骤形态，不认主题词。
_LEAD_IN_STEP_ITEM = re.compile(
    r"^\s*(?:"
    r"\d{1,2}[.、．]"
    r"|[（(]\d{1,2}[）)]"
    r"|第[一二三四五六七八九十\d]+步"
    r"|步骤\s*[0-9一二三四五六七八九十]+"
    r"|[一二三四五六七八九十]+[、.．]"
    r")"
)
FLOW_MARK = re.compile(r"→|➜|▶|»|⟶")
STEP_MARK = re.compile(r"^\s*(?:步骤|step|第[一二三四五六七八九十0-9]+步)", re.I)
# 未带样式的一行只有匹配显式编号/标签形态时才当标题判：Run C 的 190 条"4–30 字无标点"
# 短行几乎全是流程标签与表题，它们下面都有正文，不该进判据。
ENUMERATED_LABEL = re.compile(
    r"^\s*(?:第[一二三四五六七八九十百]+[章节部分篇编]|[0-9]+[.、．（(]|"
    r"[（(][一二三四五六七八九十0-9]+[）)]|[一二三四五六七八九十]+[、.．]|"
    r"(?:阶段|步骤|环节|附[件表录]|章|节|卷)[一二三四五六七八九十0-9]+)"
)


def _is_heading_block(row: dict[str, Any], body_start: int, index: int) -> bool:
    """这一格是不是"要判有无正文"的标题。封面/目录区里的一律不是（§30）。"""
    level = row.get("heading_level")
    if isinstance(level, int):
        return True
    if index < body_start or not row.get("heading_candidate"):
        return False
    text = str(row.get("text") or "")
    if TOC_ENTRY.search(text) or normalize_title_text(text) in TOC_TITLE:
        return False
    return bool(ENUMERATED_LABEL.match(text))


def _body_kind(row: dict[str, Any]) -> str | None:
    """一个块能不能算作"正文"（§29 的四类：正文 / 表格 / 清单 / 流程）。"""
    if row.get("kind") == "table":
        return "TABLE" if _substantive_table(row) else "PLACEHOLDER_TABLE"
    style = str(row.get("style") or "")
    compact = re.sub(r"\s+", "", str(row.get("text") or ""))
    if isinstance(row.get("heading_level"), int):
        return "HEADING"
    if LIST_STYLE.match(style):
        return "LIST"
    if FLOW_MARK.search(compact) or STEP_MARK.match(compact):
        return "PROCESS"
    if len(compact) >= MIN_BODY_CHARS:
        return "TEXT"
    return None


def _substantive_table(row: dict[str, Any]) -> bool:
    """表格要有 ≥2 个真实内容格才算正文；一格"按项目要求填写"的壳子不算（§34 的口径）。"""
    cells = [line.strip() for line in str(row.get("text") or "").split("\n") for line in line.split("|")]
    filled = [cell for cell in cells if cell and classify_cell(cell) == "content"]
    return len(filled) >= 2


def assess_headings(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """按 §29 判空标题，并把"看着像缺陷但不是缺陷"的候选逐条留下理由（§31/§32）。"""
    styled = [index for index, row in enumerate(blocks) if isinstance(row.get("heading_level"), int)]
    body_start = styled[0] if styled else len(blocks)
    findings: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, row in enumerate(blocks):
        text = str(row.get("text") or "")
        compact = re.sub(r"\s+", "", text)
        if normalize_title_text(compact) in TOC_TITLE:
            continue
        level = row.get("heading_level")
        candidate = bool(row.get("heading_candidate"))
        if not candidate and not isinstance(level, int):
            continue
        following = blocks[index + 1:index + 2]
        adjacency = bool(following and (following[0].get("heading_candidate")
                                        or isinstance(following[0].get("heading_level"), int)))
        if not _is_heading_block(row, body_start, index):
            if adjacency:
                rejected.append({
                    "path": row["path"], "text": text[:60], "reason": _not_a_heading_reason(row, body_start, index),
                    "paragraph_index": row.get("paragraph_index"),
                    "rule_id": "EMPTY_HEADING_BLOCK", "qa_class": CLASS_FALSE_POSITIVE,
                    "classification_reason": _not_a_heading_reason(row, body_start, index),
                })
            continue
        own_level = level if isinstance(level, int) else None
        body: str | None = None
        scanned = 0
        thin_chars = 0
        next_is_lower_heading = False
        for follow in blocks[index + 1:]:
            follow_level = follow.get("heading_level")
            if isinstance(follow_level, int):
                if own_level is None or follow_level <= own_level:
                    break                     # 同级或更高级标题：本节到此为止
                if body is None and scanned == 0:
                    next_is_lower_heading = True
                scanned += 1                   # 更低级标题：其下的内容都算这一节的正文
                continue
            scanned += 1
            kind = _body_kind(follow)
            if kind in {"TABLE", "LIST", "PROCESS", "TEXT"}:
                body = kind
                break
            if kind == "PLACEHOLDER_TABLE" and body is None:
                body = "PLACEHOLDER_TABLE"
            thin_chars += len(re.sub(r"\s+", "", str(follow.get("text") or "")))
        if body is None:
            if thin_chars:
                # §29 的"空"是**没有正文**；有一两句短命正文属于"不完整"，不是空。
                # 两者必须分开：判成 EMPTY_HEADING_BLOCK 会被 REMOVE_EMPTY_HEADING 删掉标题，
                # 把下面那 18 个字孤儿化。
                findings.append({
                    "rule_id": "THIN_HEADING_BODY",
                    "path": row["path"],
                    "severity": "NEEDS_REVIEW",
                    "qa_class": CLASS_NEEDS_REVIEW,
                    "classification_reason": f"本节正文只有 {thin_chars} 字（不足 {MIN_BODY_CHARS} 字），属于不完整而非空标题（§29）",
                    "issue": f"heading has only {thin_chars} characters of body text: {text}",
                    "text": text,
                    "heading_level": own_level,
                    "paragraph_index": row.get("paragraph_index"),
                    "body_chars": thin_chars,
                    "blocks_scanned": scanned,
                })
                continue
            findings.append({
                "rule_id": "EMPTY_HEADING_BLOCK",
                "path": row["path"],
                "severity": "BLOCK",
                "qa_class": CLASS_TRUE_BLOCK,
                "classification_reason": "标题与下一个同级或更高级标题之间没有正文、表格、清单或流程（§29）",
                "issue": f"empty heading with no body before the next same-or-higher heading: {text}",
                "text": text,
                "heading_level": own_level,
                "paragraph_index": row.get("paragraph_index"),
                "blocks_scanned": scanned,
                "repair_action": "REMOVE_EMPTY_HEADING",
            })
            continue
        if adjacency:
            reason = _body_reason(body, own_level=own_level, divider=next_is_lower_heading)
            rejected.append({
                "path": row["path"], "text": compact[:60],
                "reason": reason,
                "paragraph_index": row.get("paragraph_index"),
                "rule_id": "EMPTY_HEADING_BLOCK", "qa_class": CLASS_FALSE_POSITIVE,
                "classification_reason": reason,
            })
        if body == "PLACEHOLDER_TABLE":
            findings.append({
                "rule_id": "HEADING_WITHOUT_SUBSTANTIVE_BODY",
                "path": row["path"],
                "severity": "NEEDS_REVIEW",
                "qa_class": CLASS_NEEDS_REVIEW,
                "classification_reason": "本节唯一的内容是一张占位表格（空格或待填写），需要补真实内容或按表单治理收口",
                "issue": f"heading is backed only by a placeholder table: {text}",
                "text": text,
                "heading_level": own_level,
            })
    return {"findings": findings, "rejected_candidates": rejected}


def _not_a_heading_reason(row: dict[str, Any], body_start: int, index: int) -> str:
    text = str(row.get("text") or "")
    if index < body_start:
        return "FRONT_MATTER：封面/目录页（§30 明列非缺陷）"
    if TOC_ENTRY.search(text):
        return "TOC_ENTRY：目录条目（标题文本 + 制表符 + 页码）"
    if not ENUMERATED_LABEL.match(text):
        return "NOT_A_HEADING：无标题样式且非编号标签的短行（表题/流程标签），其下有正文"
    return "NOT_A_HEADING：未通过标题形态判据"


def _body_reason(body: str, own_level: int | None = None, divider: bool = False) -> str:
    """旧判据会报、§29/§30 判下来不是缺陷的那一类，把理由写死在这里，不留人工记忆。"""
    if divider and own_level == 1:
        return "VOLUME_DIVIDER：一级章节页，紧接其下的是更低级小节，本节内容即各小节（§30）"
    if divider:
        return "LEGAL_SECTION_BREAK：合法层级分隔，下一节更低级且其下有正文（§30）"
    table = {"TABLE": "Heading 后直接跟表格（§30）", "LIST": "Heading 后直接跟列表（§30）",
             "PROCESS": "其下有流程/步骤行", "TEXT": "其下有正文",
             "PLACEHOLDER_TABLE": "其下只有占位表格，另记 NEEDS_REVIEW"}
    return f"NOT_EMPTY_BY_R29：{table.get(body, body)}"


def _empty_headings(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """兼容旧调用点：只返回缺陷列表，分类明细走 `assess_headings`。"""
    return assess_headings(blocks)["findings"]


def normalize_title_text(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


INTERNAL_SECTION_ID = re.compile(r"(?<![A-Za-z0-9])S\d{2}-\d{2}(?![A-Za-z0-9])")
EMPTY_QUOTE = re.compile(r"[“‘][\s\u3000]*[”’]")
ADJACENT_QUOTE = re.compile(r"[”’][\s\u3000]*[“‘]")
VALID_DOUBLE_QUOTE = re.compile(r"“[^”\n]{1,120}”")
UNSUPPORTED_ARTIFACT_CLAIMS = (
    re.compile(r"(?:所有|全部)[^。；\n]{0,32}(?:履历|资历证明)[^。；\n]{0,16}(?:将|已)[^。；\n]{0,8}(?:单独成册|另行成册|整理成册)"),
    re.compile(r"(?:简历|履历|资历评价)[^。；\n]{0,16}(?:详见|见)[^。；\n]{0,8}(?:附表|附件)"),
    re.compile(r"所有拟派人员[^。；\n]{0,12}(?:已经|均已)[^。；\n]{0,24}"),
)
RECOVERABLE_RULES = {
    "QUOTE_BALANCE_QA", "BROKEN_QUOTE_SERIALIZATION", "EMPTY_HEADING_BLOCK",
    "UNSUPPORTED_COMPLETION_CLAIM", "INTERNAL_SECTION_ID",
    "CONDITIONAL_REWRITE_GRAMMAR", "BROKEN_CROSS_REFERENCE", "BROKEN_FLOW_SERIALIZATION",
    "UNSUPPORTED_SYSTEM_CAPABILITY", "UNSUPPORTED_NUMERIC_THRESHOLD", "INTERNAL_GOVERNANCE_TONE",
    "CONDITIONAL_POSTPROCESS_POLLUTION", "CLIENT_ARTIFACT_INTERNAL_META",
    # MMF008A-C：只在"整句就是这条形状"时才会升 BLOCK，而那种句子 DOCX 修复端走的正是
    # `rewrite_customer_hygiene`（内部已接 `client_facing_prose`）——报得出来就修得回去。
    CLIENT_FACING_PROSE_RULE_ID,
}


def quote_serialization_status(payload: Any) -> dict[str, Any]:
    """Return an auditable quote status for provider/intermediate JSON text."""
    texts: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    visit(payload)
    exact = sum(len(EMPTY_QUOTE.findall(text)) + len(ADJACENT_QUOTE.findall(text)) for text in texts)
    unbalanced = sum(int(text.count("“") != text.count("”") or text.count("‘") != text.count("’")) for text in texts)
    valid_pairs = sum(len(VALID_DOUBLE_QUOTE.findall(text)) for text in texts)
    status = "FAIL" if exact or unbalanced else ("RISK" if valid_pairs >= 20 else "PASS")
    return {
        "status": status,
        "empty_or_adjacent_count": exact,
        "unbalanced_text_count": unbalanced,
        "curly_quote_pair_count": valid_pairs,
    }


def _quote_findings(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    valid_pairs = 0
    quote_paths: list[str] = []
    for row in blocks:
        text = row["text"]
        exact = len(EMPTY_QUOTE.findall(text)) + len(ADJACENT_QUOTE.findall(text))
        unbalanced = int(text.count("“") != text.count("”") or text.count("‘") != text.count("’"))
        if exact or unbalanced:
            findings.append({
                "rule_id": "BROKEN_QUOTE_SERIALIZATION", "path": row["path"], "severity": "BLOCK",
                "qa_class": CLASS_TRUE_BLOCK,
                "classification_reason": "引号成对性/相邻性被真实破坏，打开即可见（§32 TRUE_BLOCK）",
                "issue": "empty, adjacent, or unbalanced Chinese quote marks in final DOCX",
                "occurrence_count": exact + unbalanced, "repair_action": "NORMALIZE_QUOTES",
            })
        pairs = len(VALID_DOUBLE_QUOTE.findall(text))
        if pairs:
            valid_pairs += pairs
            quote_paths.append(row["path"])
    # A high concentration of curly quotes in the legacy renderer is a known WPS
    # compatibility failure: the enclosed text can render outside the marks and
    # leave visually empty quotes even though plain-text extraction looks balanced.
    # §32：这一条测的是"合法弯引号太多"，不是"引号坏了"，所以只能记 WARNING，
    # 不能因为引号用得多就阻断交付（上一轮那条 BLOCK 就是这么来的 1 处误报）。
    if valid_pairs >= 20:
        findings.append({
            "rule_id": "BROKEN_QUOTE_SERIALIZATION", "path": quote_paths[0] if quote_paths else "artifact",
            "severity": "WARNING", "qa_class": "WARNING",
            "classification_reason": "WPS 兼容性风险：合法弯引号密度高，需要在 WPS 里看一眼，但不是内容缺陷",
            "issue": "WPS curly-quote serialization compatibility risk",
            "occurrence_count": 0, "curly_quote_pair_count": valid_pairs,
            "repair_action": "NORMALIZE_QUOTES_FOR_WPS",
        })
    return findings


def _deterministic_artifact_findings(blocks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """确定性文本检查。返回 (缺陷, 被语义口径推翻的命中)。

    §33：完成/附件类主张必须否定句与"仅提及"免疫。上一轮 4 处"已经完成"里没有一处是
    "我司已完成"式主张（"被当成已经完成"、"不在记录中把建议表述为已经完成"、
    "无论现场作业是否已经完成"），但规则只看短语，于是 3 条 BLOCK。被推翻的命中不进
    findings，但逐条留在 `suppressed` 里，谁能都说清"为什么这条不算缺陷"。
    """
    findings = _quote_findings(blocks)
    suppressed: list[dict[str, Any]] = []
    for row in blocks:
        text = row["text"]
        if row.get('kind') == 'paragraph' and re.search(r'_{4,}', text):
            findings.append({'rule_id': 'UNRESOLVED_INLINE_PLACEHOLDER', 'path': row['path'],
                             'severity': 'BLOCK', 'qa_class': CLASS_TRUE_BLOCK,
                             'classification_reason': '正文含未解决的空白占位，不能用空白掩盖资料缺口',
                             'issue': 'unresolved blank placeholder in customer prose', 'text': text})
        ids = INTERNAL_SECTION_ID.findall(text)
        if ids:
            findings.append({
                "rule_id": "INTERNAL_SECTION_ID", "path": row["path"], "severity": "BLOCK",
                "qa_class": CLASS_TRUE_BLOCK,
                "classification_reason": "内部章节编号出现在客户产物里，可定位、可复核",
                "issue": "internal section id leaked into customer artifact", "text": "、".join(ids),
                "occurrence_count": len(ids), "repair_action": "REMOVE_INTERNAL_SECTION_ID",
            })
        for pattern in UNSUPPORTED_ARTIFACT_CLAIMS:
            asserted: list[str] = []
            for match in pattern.finditer(text):
                status = canonical_claim_record(
                    text, claim_type="UNSUPPORTED_COMPLETION_CLAIM",
                    claim_family="COMPLETION_OR_ABSOLUTE",
                    claim_span=match.span(), path=row["path"])["modality"]
                if status in {"DENIED", "MENTION", "CONDITIONAL"}:
                    suppressed.append({
                        "rule_id": "UNSUPPORTED_COMPLETION_CLAIM", "path": row["path"],
                        "text": match.group(0), "semantic_status": status,
                        "sentence": clause_of(text, match.start(), match.end()),
                        "qa_class": CLASS_FALSE_POSITIVE,
                        "classification_reason": f"命中处于{status}语境，不是投标人对甲方的完成/附件主张（§33）",
                    })
                    continue
                asserted.append(match.group(0))
            if asserted:
                findings.append({
                    "rule_id": "UNSUPPORTED_COMPLETION_CLAIM", "path": row["path"],
                    # §33 只要求否定句/仅提及/条件句不误判；肯定式主张的严重性不降档，
                    # 降档会让本地修复不再触发（app_core 只在 BLOCK 时修），那是把误报换成漏报。
                    "severity": "BLOCK", "qa_class": CLASS_TRUE_BLOCK,
                    "classification_reason": "肯定式完成/附件主张缺少证据登记：产物里写了「已经完成/详见附表」，但没有对应实体材料",
                    "issue": "completion or attachment claim lacks an evidence registry entry",
                    "text": asserted[0], "sentence": clause_of(text, text.find(asserted[0]))[:300],
                    "occurrence_count": len(asserted),
                    "repair_action": "REWRITE_AS_REQUIRED_FUTURE_EVIDENCE",
                })
        findings.extend(hygiene_findings(text, row["path"], severity="BLOCK"))
        # Narration roles belong to one native carrier. Joining cells can make
        # a nominal header the subject of a predicate in an unrelated row.
        if row.get("kind") == "table":
            from .table_context import is_business_header, is_business_row_label
            header_rows = row.get('table_header_rows') or []
            for ri, cells in enumerate(row.get("table_rows") or []):
                for ci, cell_text in enumerate(cells):
                    if ri < len(header_rows) and header_rows[ri] and is_business_header(cell_text):
                        continue
                    if ci == 0 and is_business_row_label(cell_text):
                        continue
                    local = internal_narration_findings(
                        cell_text, f"{row['path']}.rows[{ri}].cells[{ci}]",
                        severity="NEEDS_REVIEW")
                    for finding in local:
                        finding.update({
                            "rewrite_proposed": bool(finding.get("auto_repair_applied")),
                            "auto_repair_applied": False,
                            "repair_execution_state": "NOT_APPLIED",
                            "automatic_docx_repair_supported": False,
                            "repair_action": "REVIEW_NATIVE_CELL_NARRATION",
                            "scope": "cell", "carrier_text": cell_text,
                        })
                    findings.extend(local)
        else:
            local = internal_narration_findings(text, row["path"], severity="BLOCK")
            for finding in local:
                finding.update({
                    "rewrite_proposed": bool(finding.get("auto_repair_applied")),
                    "auto_repair_applied": False,
                    "repair_execution_state": "NOT_APPLIED",
                    "automatic_docx_repair_supported": bool(finding.get("auto_repair_applied")),
                    "scope": "paragraph",
                })
            findings.extend(local)
    findings.extend(scan_scope_text((row["path"], row["text"]) for row in blocks))
    return findings, suppressed


def artifact_findings_are_locally_recoverable(report: dict[str, Any]) -> bool:
    blocked = [row for row in (report.get("findings") or []) if row.get("severity") == "BLOCK"]
    return bool(blocked) and all(str(row.get("rule_id")) in RECOVERABLE_RULES for row in blocked)


def _replace_paragraph_text(paragraph: Paragraph, text: str) -> None:
    if paragraph.runs:
        paragraph.runs[0].text = text
        for run in paragraph.runs[1:]:
            run.text = ""
    else:
        paragraph.add_run(text)


def repair_docx_artifact(path: Path) -> dict[str, Any]:
    """Apply deterministic, provider-independent repairs to a newly rendered DOCX."""
    target = Path(path)
    before = _docx_blocks(target)
    # 只删 §29 判下来的真空标题（TRUE_BLOCK）；占位表格撑的"薄节"归表单治理，不在这里删内容。
    empty_targets = []
    for row in assess_headings(before)["findings"]:
        if row.get("rule_id") != "EMPTY_HEADING_BLOCK" or str(row.get("qa_class") or "") != CLASS_TRUE_BLOCK:
            continue
        index = row.get("paragraph_index")
        if not isinstance(index, int):
            continue          # 拿不到段落定位就不删：删除是不可逆动作，宁可留一条待复核
        empty_targets.append((index, str(row.get("text") or "")))
    quote_pair_count = sum(len(VALID_DOUBLE_QUOTE.findall(row["text"])) for row in before)
    doc = Document(str(target))
    counts = {"quote": 0, "empty_heading": 0, "unsupported_completion": 0, "internal_section_id": 0,
              "technical_scope_terms": 0, "empty_heading_skipped": 0,
              CLIENT_FACING_PROSE_RULE_ID: 0}
    # 整段被"内部推理"形状清空时按**元素引用**收集，不按段落下标：下标 join 是 §59 那类
    # "节重建 join 落空导致正文坍缩"的成因，而这里我们手上就有正在编辑的那个段落对象。
    narration_emptied: list[Any] = []
    for paragraph in doc.paragraphs:
        original = paragraph.text or ""
        fixed = normalize_quotes(original)
        if quote_pair_count >= 20:
            fixed = fixed.replace("“", "「").replace("”", "」").replace("‘", "『").replace("’", "』")
        counts["quote"] += int(fixed != original and any(ch in original for ch in "“”‘’"))
        fixed, count = re.subn(r"S\d{2}-\d{2}\s*[（(]([^）)]{1,30})[）)]", r"\1", fixed)
        counts["internal_section_id"] += count
        before_hygiene = fixed
        fixed = rewrite_customer_hygiene(fixed)
        counts["internal_section_id"] += int("S0" in before_hygiene and "S0" not in fixed)
        fixed, scope_changed = sanitize_technical_body_scope_text(fixed)
        counts["technical_scope_terms"] += scope_changed
        replacements = (
            (re.compile(r"(?:注：)?所有岗位人员履历及资历证明将单独成册[^。；\n]*[。；]?"), "拟派团队资历及相关证明材料应按招标要求另行提供。"),
            (re.compile(r"其简历及资历评价详见附表"), "拟派人员资历及相关证明材料应按招标要求另行提供"),
            (re.compile(r"所有拟派人员(?:已经|均已)[^。；\n]*"), "拟派人员安排及资历证明应以实际提交并经确认的材料为准"),
        )
        for pattern, replacement in replacements:
            fixed, changed = pattern.subn(replacement, fixed)
            counts["unsupported_completion"] += changed
        if fixed == original:
            continue
        if not fixed.strip():
            style = str(paragraph.style.name if paragraph.style is not None else "")
            if _looks_like_heading(original, style):
                # 标题整条被判成元叙述：删标题会动目录与分节结构，超出文本修复的授权范围。
                # 保留原文不动，让它继续作为待复核项由检测端报出来，不在这里静默改结构。
                _replace_paragraph_text(paragraph, original)
                continue
            narration_emptied.append(paragraph._element)
            counts[CLIENT_FACING_PROSE_RULE_ID] += 1
            continue
        _replace_paragraph_text(paragraph, fixed)
    for element in narration_emptied:
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)
    # 删除按"下标 + 原文"双条件执行：段落文字与判定时不一致就不删。
    # Run C 实测 `paragraph[N]` 与 `doc.paragraphs[N]` 1,567 段全对齐（Replay-C 说的
    # "只登记非空段所以会删错"不成立），但删内容是不可逆动作，不能只靠下标口径巧合一致。
    for index, expected in sorted(empty_targets, reverse=True):
        if not 0 <= index < len(doc.paragraphs):
            counts["empty_heading_skipped"] += 1
            continue
        paragraph = doc.paragraphs[index]
        if re.sub(r"\s+", "", paragraph.text or "") != re.sub(r"\s+", "", expected):
            counts["empty_heading_skipped"] += 1
            continue
        paragraph._element.getparent().remove(paragraph._element)
        counts["empty_heading"] += 1
    tmp = target.with_name(f"{target.stem}.artifact-qa-repair.tmp{target.suffix}")
    doc.save(str(tmp))
    os.replace(tmp, target)
    return {"status": "repaired", "artifact_path": str(target), "repairs": counts}


def _request_ledger_findings(run_dir: Path | None) -> list[dict[str, Any]]:
    """§8：清扫后仍未知的 Provider 请求必须反映到交付状态，但不得吞掉成品。

    严重性停在 NEEDS_REVIEW 而不是 BLOCK：内容确实丢了，可已生成的章节仍要交到
    甲方手里复核，绝不能因为缺一节就把整个 Word 扣住。
    """
    if run_dir is None:
        return []
    root = Path(run_dir)
    reconciliation: dict[str, Any] = {}
    path = root / "late_response_reconciliation.json"
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            reconciliation = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            reconciliation = {}
    unresolved = reconciliation.get("unresolved_content_unit_ids")
    if unresolved is None:
        from longform.request_ledger import open_requests, read_events
        events = read_events(root / "provider_unknown_requests.jsonl")
        unresolved = sorted({
            str(cu) for row in open_requests(events) for cu in (row.get("content_unit_ids") or [])
        })
    still_unknown = int(reconciliation.get("still_unknown") or 0)
    if not unresolved and not still_unknown:
        return []
    return [{
        "rule_id": "LATE_RESPONSE_UNRESOLVED",
        "path": str(root / "late_response_reconciliation.json"),
        "severity": "NEEDS_REVIEW",
        "issue": (
            f"{still_unknown} 个 Provider 请求在晚到响应清扫后仍为 RESPONSE_UNKNOWN，"
            f"未回收 Content Unit：{', '.join(map(str, unresolved[:12]))}"
        ),
        "unresolved_content_unit_ids": list(unresolved),
        "late_response_recovered": int(reconciliation.get("late_recovered") or 0),
        "controlled_resends": int(reconciliation.get("controlled_resends") or 0),
    }]


def _style_first_line(style) -> tuple[str | None, int]:
    cursor, depth = style, 0
    while cursor is not None and depth < 8:
        p_pr = cursor.element.pPr
        if p_pr is not None:
            ind = p_pr.find(qn("w:ind"))
            if ind is not None:
                chars = ind.get(qn("w:firstLineChars"))
                if chars is not None:
                    return "chars", int(chars)
                twips = ind.get(qn("w:firstLine"))
                if twips is not None:
                    return "twips", int(twips)
        cursor = cursor.base_style
        depth += 1
    return None, 0


def _first_line_state(paragraph) -> tuple[str | None, int]:
    p_pr = paragraph._p.pPr
    if p_pr is not None:
        ind = p_pr.find(qn("w:ind"))
        if ind is not None:
            chars = ind.get(qn("w:firstLineChars"))
            if chars is not None:
                return "chars", int(chars)
            twips = ind.get(qn("w:firstLine"))
            if twips is not None:
                return "twips", int(twips)
    return _style_first_line(paragraph.style)


def _has_prose_indent(paragraph) -> bool:
    unit, value = _first_line_state(paragraph)
    return (unit == "chars" and value >= 200) or (unit == "twips" and value >= 400)


def _has_any_indent(paragraph) -> bool:
    unit, value = _first_line_state(paragraph)
    return (unit == "chars" and value > 0) or (unit == "twips" and value > 0)


LAYOUT_CAPTION = re.compile(r"^(?:表|图)\s*\d+")
LAYOUT_NOTE = re.compile(r"^(?:表注|字段说明|资料来源|注)[:：]")
LAYOUT_NUMBERED = re.compile(r"^\d+[.、．]\s*")


def audit_docx_layout(path: Path) -> dict[str, Any]:
    """Visual direction measurement on the actual delivered DOCX.

    Only product-built documents (native TOC bookmarks) are gated here; synthetic
    text-only fixtures in older unit tests have no cover/TOC identity to judge.
    """
    target = Path(path)
    doc = Document(str(target))
    xml = doc._element.xml
    native_toc = "MMF_TOC_" in xml and "TOC \\o" in xml
    prose_checked = 0
    prose_violations: list[dict[str, Any]] = []
    structure_violations: list[dict[str, Any]] = []
    heading_alignment_violations = 0
    title_22 = False
    subtitle_18 = False
    for index, paragraph in enumerate(doc.paragraphs):
        text = str(paragraph.text or "").strip()
        style_name = str(paragraph.style.name if paragraph.style is not None else "")
        for run in paragraph.runs:
            size = run.font.size.pt if run.font.size is not None else None
            if size and abs(size - 22) <= 0.2:
                title_22 = True
            if size and abs(size - 18) <= 0.2:
                subtitle_18 = True
        if not text or "w:fldChar" in paragraph._p.xml:
            continue
        heading = _heading_level(style_name) is not None
        structural = bool(LAYOUT_CAPTION.match(text) or LAYOUT_NOTE.match(text)
                          or LAYOUT_NUMBERED.match(text) or style_name.startswith(("List", "Caption"))
                          or text == "目录")
        if heading:
            if paragraph.alignment is not None and int(paragraph.alignment) != 0:
                heading_alignment_violations += 1
            if _has_any_indent(paragraph):
                structure_violations.append({"paragraph": index, "kind": "heading", "text": text[:40]})
            continue
        if paragraph.alignment is not None and int(paragraph.alignment) != 0:
            # Cover/title block is centered by design; it is not continuous prose.
            continue
        if structural:
            if _has_any_indent(paragraph):
                structure_violations.append({"paragraph": index, "kind": "structure", "text": text[:40]})
            continue
        if paragraph.paragraph_format.left_indent is not None:
            continue
        prose_checked += 1
        if not _has_prose_indent(paragraph):
            prose_violations.append({"paragraph": index, "text": text[:40]})
    table_structure_violations = 0
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    if paragraph.text.strip() and _has_any_indent(paragraph):
                        table_structure_violations += 1
    normal_alignment = doc.styles["Normal"].paragraph_format.alignment
    normal_left = normal_alignment is not None and int(normal_alignment) == 0
    status = "PASS"
    if native_toc and (prose_violations or structure_violations or table_structure_violations
                       or heading_alignment_violations or not normal_left or not title_22 or not subtitle_18):
        status = "NEEDS_REVIEW"
    return {
        "schema_version": "word-layout-audit-v1",
        "native_toc_present": native_toc,
        "prose_paragraphs_checked": prose_checked,
        "prose_first_line_violations": prose_violations,
        "structure_indent_violations": structure_violations + [
            {"kind": "table_cell", "count": table_structure_violations}
        ] if table_structure_violations else structure_violations,
        "heading_alignment_violations": heading_alignment_violations,
        "normal_left_aligned": normal_left,
        "main_title_22pt": title_22,
        "subtitle_18pt": subtitle_18,
        "status": status,
    }


def _compound_residue_findings(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from .external_cleanup import cleanup_text
    findings: list[dict[str, Any]] = []
    for row in blocks:
        cleaned, actions = cleanup_text(row.get("text") or "")
        if not actions:
            continue
        findings.append({
            "rule_id": "CONTEXTUAL_TERM_RESIDUE",
            "path": row.get("path") or "artifact",
            "severity": "NEEDS_REVIEW",
            "issue": f"客户正文存在可定位的复合词残留（{len(actions)} 处），应由对外清理阶段按上下文改写。",
            "text": row.get("text"),
            "occurrence_count": len(actions),
            "actions": actions,
            "repair_action": "EXTERNAL_CLEANUP",
            "scope": "paragraph" if row.get("kind") == "paragraph" else "cell",
        })
    return findings


def evaluate_docx_artifact(
    path: Path,
    *,
    canonical_facts: dict[str, Any] | None = None,
    brief: dict[str, Any] | None = None,
    contracts: list[dict[str, Any]] | None = None,
    run_dir: Path | None = None,
) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file() or target.suffix.lower() != ".docx":
        return {
            "schema_version": "final-docx-artifact-qa-v0.1",
            "status": "BLOCK",
            "actual_docx_inspected": False,
            "findings": [{"rule_id": "FINAL_ARTIFACT_MISSING", "path": str(target), "severity": "BLOCK", "issue": "final DOCX is missing"}],
            "summary": {"block_count": 1, "warning_count": 0},
        }
    blocks = _docx_blocks(target)
    texts = [row["text"] for row in blocks]
    structured = evaluate_artifact({"artifact": {"paragraphs": texts}}, canonical_facts=canonical_facts)
    # The structured text view is retained for all other checks. Its narration
    # checks are replaced by the native paragraph/cell pass below, so an empty
    # paragraph or a flattened table cannot create a second locator or verdict.
    findings = [row for row in (structured.get("findings") or [])
                if row.get("rule_id") != CLIENT_FACING_PROSE_RULE_ID]
    from .tender_controls import controls_from_run, final_control_findings
    findings.extend(final_control_findings(blocks, controls_from_run(run_dir)) if run_dir else [])
    findings.extend(scan_promised_counts(blocks))
    # R05 QA 上下文更正：FAQ-009 的“如下：/包括：”等结尾只说明这一块是引导句；
    # 是否真截断要看**同节内**紧随其后的块有没有真实承接内容。有承接 → 引导句放行；
    # 紧随的是新标题/新章节或没有后续 → 仍按截断处理。差分：删掉承接块后照样 BLOCK。
    # R07：同节里更深的子标题不是新章节，扫描可以进入它的正文。子标题文字本身
    # 不是载体；下一个同级子标题是另一流程，不能借来补这一条。放行仍要承诺锚点
    # 在正文里逐字齐备。缺一个锚点就保持 BLOCK，不用标题或同数条目证明完整。
    lead_in_dropped: list[dict[str, Any]] = []
    for row in [f for f in findings if f.get("rule_id") == "FAQ-009"]:
        m = re.search(r"paragraphs\[(\d+)\]", str(row.get("path") or ""))
        if not m:
            continue
        index = int(m.group(1))
        continuation = None
        lead = texts[index]
        # An introduction must name what is being continued. Length alone cannot
        # turn unrelated prose into proof that the promised steps were supplied.
        enumerations = re.findall(r"按([^。；;\n]{2,100}?)[一二三四五六七八九十\d]+(?:步|项|环节|类)(?:执行|展开|进行)", lead)
        anchors = [part.strip() for part in re.split(r"[、，,]|以及", enumerations[-1])] if enumerations else []
        if len(anchors) < 2 or any(len(part) < 2 for part in anchors):
            continue
        section_level = next((blocks[j].get("heading_level") for j in range(index - 1, -1, -1)
                               if isinstance(blocks[j].get("heading_level"), int)), None)
        carrier_level = None
        saw_subtitle = False
        carrier_body: list[dict[str, Any]] = []
        list_group: list[dict[str, Any]] = []
        for j in range(index + 1, len(blocks)):
            block = blocks[j]
            level = block.get("heading_level")
            if isinstance(level, int):
                if section_level is None or level <= section_level:
                    break
                if carrier_level is not None and level <= carrier_level:
                    break
                carrier_level = level
                saw_subtitle = True
                list_group = []
                continue
            text = (block.get("text") or "").strip()
            if LIST_STYLE.match(block.get('style') or '') or _LEAD_IN_STEP_ITEM.match(text):
                list_group.append(block)
            else:
                list_group = []
            if text:
                carrier_body.append(block)
            if len(text) >= 15 and all(anchor in text for anchor in anchors):
                continuation = {"path": block.get("path"), "chars": len(text), 'anchors': anchors}
                break
            if list_group and all(any(anchor in b['text'] for b in list_group) for anchor in anchors):
                continuation = {'path': ','.join(b['path'] for b in list_group),
                                'chars': sum(len(b['text']) for b in list_group), 'anchors': anchors}
                break
        if continuation:
            findings.remove(row)
            lead_in_dropped.append({**row,
                                    "classification_reason": (
                                        "引导句在同节内有真实承接内容（"
                                        f"{continuation['path']}，{continuation['chars']} 字），"
                                        "不是截断")})
        elif saw_subtitle:
            present = [anchor for anchor in anchors if any(anchor in str(b.get("text") or "") for b in carrier_body)]
            missing = [anchor for anchor in anchors if anchor not in present]
            row["classification_reason"] = (
                "同节子标题后的正文没有逐字覆盖全部承诺锚点，标题不作为完整证明。"
                f"已见：{'、'.join(present) or '无'}；缺失：{'、'.join(missing) or '无'}。"
            )
    for item in findings:
        if item.get("rule_id") == "CLIENT_ARTIFACT_INTERNAL_META":
            item["severity"] = "BLOCK"
    # §32：先分类再决定交付。旧判据会报、新定义判下来不是缺陷的候选，逐条留原因，
    # 不做"为了降 BLOCK 全部忽略"（§31）。
    heading_audit = assess_headings(blocks)
    findings.extend(heading_audit["findings"])
    deterministic_findings, semantic_suppressed = _deterministic_artifact_findings(blocks)
    findings.extend(deterministic_findings)
    contexts = {}
    heading = ''
    section_id = -1
    for index, block in enumerate(blocks):
        if block.get('heading_level') or block.get('heading_candidate'):
            heading = block['text']
        if block.get('heading_level'):
            section_id = index
        contexts[f'artifact.paragraphs[{index}]'] = {
            'kind': block['kind'], 'block_kind': block['kind'], 'section_id': section_id,
            'heading': heading, 'locator': block['path'],
            'structured_list': bool(LIST_STYLE.match(block.get('style') or '') or
                                    re.match(r'^(?:[一二三四五六七八九十]+[、.．]|\d+[、.．])', block['text']))}
        if LIST_STYLE.match(block.get('style') or '') and re.search(r'流程|核验|验收|检查清单', heading):
            contexts[f'artifact.paragraphs[{index}]']['kind'] = 'process_checklist'
    commitment_source = dict(brief or canonical_facts or {})
    if run_dir:
        from .final_editorial import SourceFacts
        # Dereference the hash-verified tender, never infer authority from output.
        commitment_source.update(SourceFacts.from_run(run_dir).claim_source())
    commitment = evaluate_commitments(commitment_source, contracts or [],
                                      {"artifact": {"paragraphs": texts}}, claim_contexts=contexts)
    remaining_violations = []
    for row in commitment.get("violations") or []:
        sentence = str(row.get("sentence") or "")
        future_record = (row.get("claim_type") == "UNSUPPORTED_EXISTING_STATE_CLAIM"
                         and re.search(r"(?:阶段|启动|检查|执行|服务).{0,16}(?:结束|完成)后", sentence)
                         and re.search(r"(?:写明|记录|列明)已完成项", sentence))
        if future_record:
            commitment.setdefault("suppressed", []).append({**row, "classification_reason":"FUTURE_RECORD_CONTENT_NOT_CURRENT_COMPLETION_ASSERTION"})
        else:
            remaining_violations.append(row)
    commitment["violations"] = remaining_violations
    findings.extend({
        "rule_id": str(row.get("claim_type") or "UNSUPPORTED_COMMITMENT"),
        "path": row.get("path") or "artifact",
        "severity": "BLOCK",
        "qa_class": CLASS_TRUE_BLOCK,
        "classification_reason": row.get("classification_reason") or "肯定式承诺语态，且当前项目资料里没有出处",
        "issue": row.get("reason") or "unsupported commitment",
        "text": row.get("text"),
        "sentence": row.get("sentence"),
        "repair_action": row.get("repair_action"),
        "canonical_claim_record": row.get("canonical_claim_record"),
    } for row in (commitment.get("violations") or []))
    findings.extend({
        "rule_id": str(row.get("claim_type") or "UNSUPPORTED_COMMITMENT"),
        "path": row.get("path") or "artifact",
        "severity": "NEEDS_REVIEW",
        "qa_class": CLASS_NEEDS_REVIEW,
        "classification_reason": row.get("classification_reason") or "句式像承诺，实际写的是被定义的对象或甲方自己的职责",
        "issue": row.get("reason") or "unsupported commitment",
        "text": row.get("text"),
        "sentence": row.get("sentence"),
        "repair_action": row.get("repair_action"),
        "canonical_claim_record": row.get("canonical_claim_record"),
    } for row in (commitment.get("review_items") or []))
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in findings:
        key = (str(item.get("rule_id")), str(item.get("path")), str(item.get("issue")))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    blocks_count = sum(item.get("severity") == "BLOCK" for item in unique)
    warnings = sum(item.get("severity") != "BLOCK" for item in unique)
    toc = evaluate_native_toc(target)
    if brief and str(brief.get("medium") or "WORD").upper() == "WORD" and toc.get("status") != "PASS":
        unique.append({"rule_id": "NATIVE_TOC_INTEGRITY", "path": str(target), "severity": "BLOCK", "issue": "Native TOC/WPS bookmark structure is incomplete"})
        blocks_count += 1
    if brief and str(brief.get("medium") or "WORD").upper() == "WORD" and not toc.get("toc_page_variance_pass", True):
        # §18-§23：目录页码全部相同（尤其是全 "1"）说明分页/字段刷新没有发生。
        # 记 AUTO_REPAIR 而不是 BLOCK：文件仍然要交付，但绝不可能是 COMPLETED_CLEAN。
        share = float(toc.get("toc_identical_page_share") or 0.0)
        unique.append({
            "rule_id": "TOC_PAGE_VARIANCE", "path": str(target), "severity": "AUTO_REPAIR",
            "issue": f"目录页码未真实分页：{share:.0%} 的条目落在同一个页码值上，需要在交付前刷新分页与目录字段。",
        })
        warnings += 1
    for row in _request_ledger_findings(run_dir):
        if (str(row.get("rule_id")), str(row.get("path")), str(row.get("issue"))) not in seen:
            unique.append(row)
    # §24-§29：表格按真实交付件测量。装配端已经删过一轮空格行/列，这里仍不达标说明
    # 内容本身没有信息，只能记账进门控 —— 删表是丢需求响应，不是治理手段。
    table_audit = audit_docx_tables(target)
    for row in table_findings(table_audit, path=target):
        key = (str(row.get("rule_id")), str(row.get("path")), str(row.get("issue")))
        if key not in seen:
            seen.add(key)
            unique.append(row)
    # §32：四类严重性在这里一次性定完，交付只看类，不看规则名。
    from .table_fidelity import final_table_findings
    unique.extend(final_table_findings(target, run_dir))
    from visuals.artifact_gate import evaluate_visual_artifact
    visual_gate = evaluate_visual_artifact(target, run_dir)
    unique.extend(visual_gate['findings'])
    layout_audit = audit_docx_layout(target)
    if layout_audit.get("native_toc_present") and layout_audit.get("status") != "PASS":
        unique.append({
            "rule_id": "WORD_V12_LAYOUT", "path": str(target), "severity": "AUTO_REPAIR",
            "issue": "排版未满足交付口径：正文首行缩进或结构缩进/标题字号/左对齐存在偏差。",
            "layout": layout_audit,
            "repair_action": "APPLY_V12_LAYOUT",
        })
    unique.extend(_compound_residue_findings(blocks))
    classified = classify_findings(unique)
    unique = classified["findings"]
    fp_channels = {
        "declared_false_positive": list(classified["false_positives"]),
        "heading_rejected": list(heading_audit["rejected_candidates"]),
        "semantic_suppressed": list(semantic_suppressed),
        "commitment_suppressed": list(commitment.get("suppressed") or []),
        "lead_in_continuation": lead_in_dropped,
    }
    false_positives = [row for group in fp_channels.values() for row in group]
    # classify_findings 只看得到"声明为 FALSE_POSITIVE"的那一路；其余三条是规则自己自证的
    # 抑制行。不并回同一个总体，误报率就永远是 0 —— 那正是 §28 要修的口径。
    counts = dict(classified["summary"]["counts"])
    counts[CLASS_FALSE_POSITIVE] = len(false_positives)
    adjudicated_total = len(unique) + len(false_positives)
    by_rule = {rule: dict(bucket) for rule, bucket in classified["summary"]["counts_by_rule"].items()}
    for group_name, group in fp_channels.items():
        if group_name == "declared_false_positive":
            continue
        for row in group:
            rule = str(row.get("rule_id") or row.get("claim_type") or "")
            bucket = by_rule.setdefault(rule, {key: 0 for key in QA_CLASSES})
            bucket[CLASS_FALSE_POSITIVE] = bucket.get(CLASS_FALSE_POSITIVE, 0) + 1
    qa_classification = {
        **classified["summary"],
        "counts": counts,
        "counts_by_rule": by_rule,
        "false_positive_count": len(false_positives),
        "adjudicated_total": adjudicated_total,
        "false_positive_rate": round(len(false_positives) / max(1, adjudicated_total), 4),
        "false_positive_channels": {name: len(group) for name, group in fp_channels.items()},
    }
    blocks_count = int(counts.get(CLASS_TRUE_BLOCK) or 0)
    review_count = int(counts.get(CLASS_NEEDS_REVIEW) or 0)
    warning_count = int(counts.get("WARNING") or 0)
    status = ("BLOCK" if blocks_count else
              ("NEEDS_REVIEW" if review_count else ("WARNING" if warning_count else "PASS")))
    broken_quotes = [row for row in unique
                     if row.get("rule_id") == "BROKEN_QUOTE_SERIALIZATION" and row.get("qa_class") == CLASS_TRUE_BLOCK]
    return {
        "schema_version": "final-docx-artifact-qa-v0.3",
        "status": status,
        "qa_classification": qa_classification,
        "false_positive_count": len(false_positives),
        "false_positives": false_positives,
        "empty_heading_rejected_candidates": heading_audit["rejected_candidates"],
        "actual_docx_inspected": True,
        "artifact_path": str(target),
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "paragraph_count": sum(row["kind"] == "paragraph" for row in blocks),
        "table_count": sum(row["kind"] == "table" for row in blocks),
        "tables": {key: value for key, value in table_audit.items() if key != "per_table"},
        "toc": toc,
        "layout": layout_audit,
        "visual_integrity": visual_gate,
        "request_ledger": {"run_dir": str(run_dir) if run_dir else None},
        "findings": unique,
        "summary": {
            "block_count": blocks_count,
            "needs_review_count": review_count,
            "warning_count": warning_count,
            "false_positive_count": len(false_positives),
        },
        # 引号口径分开写：FAIL=引号真的坏了（可定位、要修），RISK=合法弯引号密度高（WPS 兼容复核）。
        "quote_status": "FAIL" if broken_quotes else ("RISK" if any(
            row.get("rule_id") == "BROKEN_QUOTE_SERIALIZATION" for row in unique) else "PASS"),
        "broken_quote_count": sum(int(row.get("occurrence_count") or 0) for row in broken_quotes),
        "curly_quote_pair_count": sum(int(row.get("curly_quote_pair_count") or 0) for row in unique
                                      if row.get("rule_id") == "BROKEN_QUOTE_SERIALIZATION"),
    }
