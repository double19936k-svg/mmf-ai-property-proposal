"""Repeatable external-cleanup stage for the customer-facing Word copy.

The product-direct artifact is never edited here.  This stage only works on a
copy and only applies context-bound compound-term corrections validated against
the 60 human revision pairs in the Word v12 EDIT_LOG.  It deliberately has no
global replacement for 字段/接口/工单号/编号 and no bare-term ban: legitimate
"记录内容", "沟通衔接", "处理情况" and "事项" survive untouched.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

from lxml import etree

from .final_editorial import W, NS, _replace_xml_text

SCHEMA_VERSION = "external-cleanup-v1"


def _rule(rule_id: str, pattern: str, replacement: str) -> tuple[str, re.Pattern[str], str]:
    return rule_id, re.compile(pattern), replacement


# Order matters: context-rich rules precede their shorter fallbacks.  Each rule
# names a grammatical role (missing form element, reception position, reply
# outlet, referenced work-order record), not a paragraph locator.
RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    # ── 记录内容：表单要素、表样、登记项、口径 ──────────────────────────────
    _rule("TERM_RECORD_ELEMENT", r"核验内容的记录内容", "核验登记要素"),
    _rule("TERM_RECORD_ELEMENT", r"缺记录内容(?=即视为)", "要素不全"),
    _rule("TERM_RECORD_ELEMENT", r"缺记录内容的条目", "要素不全的条目"),
    _rule("TERM_RECORD_ELEMENT", r"缺记录内容", "缺项"),
    _rule("TERM_RECORD_TEMPLATE", r"表样记录内容", "记录表样"),
    _rule("TERM_RECORD_TEMPLATE", r"(?<=对应表与)登记记录内容", "登记表样"),
    _rule("TERM_RECORD_ELEMENT", r"登记记录内容", "登记要素"),
    _rule("TERM_RECORD_TEMPLATE", r"用类别记录内容", "用类别栏位"),
    _rule("TERM_RECORD_ELEMENT", r"类别记录内容与确认来源", "人员类别与确认来源"),
    _rule("TERM_RECORD_ELEMENT", r"(?:一处|一个)记录内容", "一项登记要素"),
    _rule("TERM_RECORD_ELEMENT", r"某记录内容", "某登记项"),
    _rule("TERM_RECORD_ELEMENT", r"新记录内容", "新登记项"),
    _rule("TERM_RECORD_ELEMENT", r"台账记录内容", "台账登记项"),
    _rule("TERM_RECORD_CALIBER", r"记录内容口径", "记录口径"),
    _rule("TERM_RECORD_ELEMENT", r"记录内容填写方式", "记录填写方式"),
    _rule("TERM_RECORD_ELEMENT", r"单独成记录内容", "单独列项记录"),
    _rule("TERM_RECORD_ELEMENT", r"补加记录内容", "补加登记要素"),
    _rule("TERM_RECORD_ELEMENT", r"记录内容含义", "各项登记要素的含义"),
    # ── 沟通衔接：接待窗口、答复出口/口径、对接人、分派路径、交接界面 ──────
    _rule("TERM_INTERFACE_RECEPTION", r"对外沟通衔接处", "对外接待窗口"),
    _rule("TERM_INTERFACE_RECEPTION", r"对外沟通衔接岗位", "对外接待岗位"),
    _rule("TERM_INTERFACE_RECEPTION", r"对外沟通衔接层级", "对外对接层级"),
    _rule("TERM_INTERFACE_REPLY", r"对外沟通衔接(?=之所以)", "对外答复口径"),
    _rule("TERM_INTERFACE_REPLY", r"对外沟通衔接", "对外答复出口"),
    _rule("TERM_INTERFACE_HANDOVER", r"后续处理沟通衔接", "后续处理安排"),
    _rule("TERM_INTERFACE_HANDOVER", r"登记承接沟通衔接", "登记承接界面"),
    _rule("TERM_INTERFACE_HANDOVER", r"处理沟通衔接", "处理安排"),
    _rule("TERM_INTERFACE_HANDOVER", r"分派沟通衔接", "分派路径"),
    _rule("TERM_INTERFACE_CONTACT", r"沟通衔接对象与渠道", "对接对象与渠道"),
    _rule("TERM_INTERFACE_CONTACT", r"甲方调整沟通衔接后", "甲方调整对接人或渠道后"),
    _rule("TERM_INTERFACE_HANDOVER", r"识别沟通衔接性质", "识别事项协同性质"),
    _rule("TERM_INTERFACE_HANDOVER", r"核对沟通衔接是否顺畅通用", "核对协同衔接是否顺畅"),
    _rule("TERM_INTERFACE_HANDOVER", r"按沟通衔接引用", "专门规定"),
    _rule("TERM_INTERFACE_HANDOVER", r"沟通衔接面", "通行引导面"),
    _rule("TERM_INTERFACE_HANDOVER", r"按类别规定核验沟通衔接", "按类别规定通行核验与交接要求"),
    _rule("TERM_INTERFACE_HANDOVER", r"核验沟通衔接处理的是", "通行核验衔接处理的是"),
    _rule("TERM_INTERFACE_HANDOVER", r"核验沟通衔接按对象", "通行核验要求按对象"),
    _rule("TERM_INTERFACE_HANDOVER", r"核验沟通衔接不是规定", "通行核验的对接关系不是规定"),
    _rule("TERM_INTERFACE_HANDOVER", r"相邻责任按沟通衔接登记", "相邻责任按交接界面登记"),
    # ── 处理情况：工单记录、原事项归口、统一登记规则 ───────────────────────
    _rule("TERM_WORKORDER_RECORD", r"沿处理情况续写", "沿同一工单续写"),
    _rule("TERM_WORKORDER_RECORD", r"引用所属处理情况", "引用所属工单记录"),
    _rule("TERM_WORKORDER_RECORD", r"引用原处理情况", "引用原工单记录"),
    _rule("TERM_WORKORDER_RECORD", r"其引用的处理情况", "其关联的工单记录"),
    _rule("TERM_WORKORDER_RECORD", r"同一处理情况", "同一工单记录"),
    _rule("TERM_MATTER_TRACE", r"另记变化说明并保持原事项(?=[。；])",
          "另记变化说明并保持原事项归口不变"),
    _rule("TERM_MATTER_TRACE", r"使用同一事项规则", "按统一的登记规则管理"),
    _rule("TERM_MATTER_TRACE", r"沿用同一事项规则", "沿用统一的事项登记规则"),
)


def cleanup_text(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Apply context-bound rules; return the new text and a per-match mapping."""
    value = str(text or "")
    actions: list[dict[str, Any]] = []
    for rule_id, pattern, replacement in RULES:
        def _replace(match: re.Match[str]) -> str:
            after = match.expand(replacement)
            if after != match.group():
                actions.append({"rule_id": rule_id, "before": match.group(), "after": after,
                                "start": match.start(), "end": match.end()})
            return after
        value = pattern.sub(_replace, value)
    return value, actions


def has_cleanup_terms(text: str) -> bool:
    return bool(cleanup_text(text)[1])


def _paragraph_text(paragraph) -> str:
    return "".join(paragraph.xpath('.//w:t/text()', namespaces=NS))


def clean_docx(source: Path, target: Path) -> dict[str, Any]:
    """Copy `source` to `target`, applying rules to body text only.

    Field-bearing paragraphs (native TOC) and headings are left untouched:
    their cached page/text identity belongs to the pagination stage.  Mapping
    entries keep the paragraph ordinal and exact before/after text.
    """
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    original = source.read_bytes()
    with zipfile.ZipFile(source) as archive:
        parts = {entry.filename: (entry, archive.read(entry.filename))
                 for entry in archive.infolist()}
    xml = etree.fromstring(parts["word/document.xml"][1])
    body = xml.find(W + "body")
    changes: list[dict[str, Any]] = []
    rule_counts: dict[str, int] = {}
    skipped_fields = 0
    skipped_headings = 0
    paragraph_index = 0
    for paragraph in body.iter(W + "p"):
        index = paragraph_index
        paragraph_index += 1
        if (paragraph.findall('.//' + W + 'fldChar') or
                paragraph.findall('.//' + W + 'instrText') or
                paragraph.findall('.//' + W + 'hyperlink')):
            skipped_fields += 1
            continue
        style = paragraph.find('./' + W + 'pPr/' + W + 'pStyle')
        style_value = str(style.get(W + 'val')) if style is not None else ""
        if re.search(r"heading|标题", style_value, re.I):
            skipped_headings += 1
            continue
        text = _paragraph_text(paragraph)
        if not text.strip():
            continue
        cleaned, actions = cleanup_text(text)
        if not actions:
            continue
        _replace_xml_text(paragraph, cleaned)
        for action in actions:
            rule_counts[action["rule_id"]] = rule_counts.get(action["rule_id"], 0) + 1
        changes.append({"paragraph": index, "before": text, "after": cleaned,
                        "actions": actions})
    if not changes:
        shutil.copyfile(source, target)
    else:
        document_bytes = etree.tostring(xml, xml_declaration=True, encoding="UTF-8", standalone=True)
        temp = target.with_suffix('.cleanup.tmp')
        with zipfile.ZipFile(temp, "w") as archive:
            for name, (entry, data) in parts.items():
                archive.writestr(entry, document_bytes if name == "word/document.xml" else data)
        temp.replace(target)
    residual = []
    with zipfile.ZipFile(target) as archive:
        check = etree.fromstring(archive.read("word/document.xml"))
    for index, paragraph in enumerate(check.find(W + "body").iter(W + "p")):
        if paragraph.findall('.//' + W + 'fldChar') or paragraph.findall('.//' + W + 'instrText'):
            continue
        text = _paragraph_text(paragraph)
        if text.strip() and has_cleanup_terms(text):
            residual.append({"paragraph": index, "text": text})
    before_sha = hashlib.sha256(original).hexdigest()
    after_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "CLEANED" if changes else "NO_CHANGES",
        "source_path": str(source),
        "target_path": str(target),
        "source_sha256": before_sha,
        "target_sha256": after_sha,
        "identical_copy": before_sha == after_sha,
        "paragraphs_scanned": paragraph_index,
        "changed_paragraphs": len(changes),
        "skipped_field_paragraphs": skipped_fields,
        "skipped_heading_paragraphs": skipped_headings,
        "rule_counts": rule_counts,
        "changes": changes,
        "residual_count": len(residual),
        "residuals": residual,
        "provider_calls": 0,
    }


def write_report(report: dict[str, Any], path: Path) -> None:
    Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
