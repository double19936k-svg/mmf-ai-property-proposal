"""Last pass before a customer Word file is saved.

Facts are copied from the tender pack already stored for the run. Project names
are not chosen in code. Bid-procedure text is dropped. Repeated tables and
broken review phrases are removed on the document itself.
"""
from __future__ import annotations

import hashlib
import re

from docx.oxml import OxmlElement
from docx.text.paragraph import Paragraph

_NOISE = ("保证金", "电汇底单", "齐鲁采购与招标网", "电子响应文件", "签字后的扫描件", "无法解密")
_CONFIDENTIALITY = (
    "本项目运营期电子资料集中度高，租户档案、抄表与能耗分摊明细、门禁授权数据、设备资产清单与各类考核报告均涉及商业秘密。"
    "顾问小组严格执行采购合同保密规定，将工作内容、工作程序、业务资料及财务数据纳入保密管理范围，未经甲方书面同意绝不向任何第三方透露。"
)
_TOPICS = (
    ("服务承诺", "服务承诺及优惠条件", ("服务承诺", "优惠条件")),
    ("合理化建议", "合理化建议", ("合理化建议",)),
    ("LEED", "协助通过 LEED O+M 绿色运营认证专项方案", ("LEED",)),
)


def _has_field(paragraph) -> bool:
    element = paragraph._p
    return bool(element.findall('.//' + '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}fldChar')
                or element.findall('.//' + '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}instrText'))


def _style(paragraph) -> str:
    return paragraph.style.name if paragraph.style else ""


def _sentences(corpus: str) -> list[str]:
    found = []
    for part in re.split(r"[。！？\n]", corpus or ""):
        text = re.sub(r"\s+", "", part)
        text = re.sub(r'^[\s"\\,:{}\[\]]+', "", text)
        if 12 <= len(text) <= 500 and not any(word in text for word in _NOISE):
            found.append(text)
    return found


def _sentence_with(corpus: str, *needles: str) -> str:
    matches = [sentence for sentence in _sentences(corpus) if all(needle in sentence for needle in needles)]
    if matches:
        matches.sort(key=lambda sentence: (sentence.count('"') + sentence.count("{") + sentence.count("\\"), len(sentence)))
        return matches[0]
    compact = re.sub(r"\s+", "", corpus or "")
    positions = [compact.find(needle) for needle in needles]
    if not needles or any(position < 0 for position in positions):
        return ""
    start = max(0, min(positions) - 30)
    end = min(len(compact), max(position + len(needle) for position, needle in zip(positions, needles)) + 80)
    window = compact[start:end].strip(" ，,、")
    if any(word in window for word in _NOISE):
        return ""
    return window


def _pieces(sentence: str) -> list[str]:
    parts = [part.strip(" ，,") for part in re.split(r"[；;]", sentence) if len(part.strip(" ，,")) >= 12]
    return parts or ([sentence] if sentence else [])


def _insert_after(anchor, text: str, style: str | None = None):
    new_p = OxmlElement("w:p")
    anchor._p.addnext(new_p)
    paragraph = Paragraph(new_p, anchor._parent)
    if style:
        paragraph.style = style
    if text:
        paragraph.add_run(text)
    return paragraph


def _body_text(doc) -> str:
    chunks = [paragraph.text for paragraph in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            chunks.extend(cell.text for cell in row.cells)
    return "\n".join(chunks)


def _heading_text(doc) -> str:
    return "\n".join(paragraph.text for paragraph in doc.paragraphs if "Heading" in _style(paragraph))


def _element_text(element) -> str:
    return "".join(node.text or "" for node in element.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))


def _caption_before(element):
    previous = element.getprevious()
    while previous is not None and previous.tag.endswith("p") and not _element_text(previous).strip():
        previous = previous.getprevious()
    if previous is None or not previous.tag.endswith("p"):
        return None
    caption = _element_text(previous).strip()
    if 0 < len(caption) <= 40 and not caption.endswith(("。", "；", "：")):
        return previous
    return None


def drop_bid_noise(doc) -> int:
    removed = 0
    replaced = False
    for paragraph in list(doc.paragraphs):
        if _has_field(paragraph):
            continue
        text = paragraph.text or ""
        if not any(word in text for word in _NOISE):
            continue
        if "Heading" in _style(paragraph):
            kept = text
            for word in _NOISE:
                kept = kept.replace(word, "")
            paragraph.text = re.sub(r"\s+", "", kept) or "实施条件"
            removed += 1
            continue
        if not replaced and any(word in text for word in ("电子响应文件", "无法解密")):
            paragraph.text = _CONFIDENTIALITY
            replaced = True
            removed += 1
            continue
        paragraph._element.getparent().remove(paragraph._element)
        removed += 1
    for table in list(doc.tables):
        blob = "\n".join(cell.text for row in table.rows for cell in row.cells)
        if not any(word in blob for word in _NOISE):
            continue
        element = table._element
        caption = _caption_before(element)
        if caption is not None:
            caption.getparent().remove(caption)
        element.getparent().remove(element)
        removed += 1
    return removed


def remove_duplicate_tables_in_docx(doc) -> int:
    seen = set()
    removed = 0
    for table in list(doc.tables):
        rows = table.rows[:2] if len(table.rows) >= 2 else table.rows
        cells = [re.sub(r"\s+", "", cell.text) for row in rows for cell in row.cells]
        signature = hashlib.md5("||".join(cells).encode("utf-8")).hexdigest()
        if signature not in seen:
            seen.add(signature)
            continue
        element = table._element
        caption = _caption_before(element)
        if caption is not None and len(_element_text(caption).strip()) <= 30:
            caption.getparent().remove(caption)
        element.getparent().remove(element)
        removed += 1
    return removed


def final_polish_docx_paragraphs(doc) -> None:
    current_h2 = ""
    h3_count = 0
    for paragraph in doc.paragraphs:
        if _has_field(paragraph):
            continue
        text = paragraph.text
        if not text or not text.strip():
            continue
        text = re.sub(r"沿用本节〔\d+〕所述要求[，,]?", "按前述既定标准与时限要求执行，", text)
        text = re.sub(r"〔\d+〕", "", text)
        text = re.sub(r"相关证明资料尚待核实[。，；\s]*", "", text)
        text = re.sub(r"(尚需明确|尚待明确|尚待核实)\s*尚待核实", "待双方书面确认后执行", text)
        text = re.sub(r"[，,\s]*尚待核实(?=[。；，\s]|$)", "", text)
        text = text.replace("该项安排而是让", "该项安排旨在让")
        style = _style(paragraph)
        if "Heading 2" in style or re.match(r"^\d+\.\d+\s+", text.strip()):
            current_h2 = re.sub(r"^\d+\.\d+\s*", "", text.strip())
            h3_count = 0
        elif "Heading 3" in style and text.strip() == current_h2 and current_h2:
            h3_count += 1
            suffix = "作业与处置流程" if h3_count == 1 else f"专项执行流程（{h3_count}）"
            text = f"{current_h2}{suffix}"
        if text != paragraph.text:
            paragraph.text = text


def _restore_full_dates(doc, corpus: str) -> None:
    seen = set()
    for match in re.finditer(r"(\d{4})年(\d{1,2}月\d{1,2}日)", corpus or ""):
        year, month_day = match.group(1), match.group(2)
        if month_day in seen:
            continue
        seen.add(month_day)
        full = f"{year}年{month_day}"
        pattern = re.compile(rf"(?<!\d年){re.escape(month_day)}")
        for paragraph in doc.paragraphs:
            if _has_field(paragraph) or not pattern.search(paragraph.text or ""):
                continue
            paragraph.text = pattern.sub(full, paragraph.text)


def _anchor(doc, *needles: str):
    for paragraph in doc.paragraphs:
        if "Heading" in _style(paragraph) and any(needle in paragraph.text for needle in needles):
            return paragraph
    return doc.paragraphs[-1] if doc.paragraphs else None


def _ensure_sentence(doc, corpus: str, needles: tuple[str, ...], heading_needles: tuple[str, ...]) -> bool:
    if not all(needle in (corpus or "") for needle in needles):
        return False
    if all(needle in _body_text(doc) for needle in needles):
        return False
    sentence = _sentence_with(corpus, *needles)
    if not sentence:
        return False
    anchor = _anchor(doc, *heading_needles)
    if anchor is None:
        return False
    _insert_after(anchor, sentence)
    return True


def _ensure_overview(doc, corpus: str) -> None:
    purchaser = re.search(r"采购(?:人|单位)[:：]\s*([\u4e00-\u9fff]{4,40}公司)", corpus or "")
    address_pattern = r"([\u4e00-\u9fff]{2,8}市[\u4e00-\u9fff]{1,8}区[\u4e00-\u9fff]{1,16}(?:路|街|大道)\d+号[\u4e00-\u9fffA-Za-z0-9\-]{0,24})"
    addresses = list(re.finditer(address_pattern, corpus or ""))
    purchaser_at = corpus.find("采购单位") if corpus else -1
    if purchaser_at < 0 and corpus:
        purchaser_at = corpus.find("采购人")
    address = min(addresses, key=lambda item: abs(item.start() - purchaser_at)) if addresses and purchaser_at >= 0 else (addresses[0] if addresses else None)
    annex = re.search(r"裙房\s*[A-Z]-\d+", corpus or "")
    parts = []
    if purchaser and purchaser.group(1) not in _body_text(doc):
        parts.append(f"采购人为{purchaser.group(1)}")
    if address and address.group(1) not in _body_text(doc):
        place = address.group(1)
        if annex and annex.group(0) not in place and annex.group(0) not in _body_text(doc):
            place += "及" + annex.group(0)
        parts.append(f"项目坐落于{place}")
    if not parts:
        return
    anchor = _anchor(doc, "项目概况", "项目理解")
    if anchor is not None:
        _insert_after(anchor, "。".join(parts) + "。")


def _ensure_scoring_sections(doc, corpus: str) -> None:
    headings = _heading_text(doc)
    anchor = _anchor(doc, "评分专项") or (doc.paragraphs[-1] if doc.paragraphs else None)
    if anchor is None:
        return
    for marker, title, needles in _TOPICS:
        if marker in headings or marker not in (corpus or ""):
            continue
        collected = []
        for sentence in _sentences(corpus):
            if any(needle in sentence for needle in needles):
                collected.extend(_pieces(sentence))
            if len(collected) >= 4:
                break
        collected = collected[:4]
        if not collected:
            continue
        cursor = anchor
        cursor = _insert_after(cursor, title, "Heading 2")
        for paragraph in collected:
            cursor = _insert_after(cursor, paragraph)
        table = doc.add_table(rows=1 + len(collected), cols=2)
        table.cell(0, 0).text = "响应事项"
        table.cell(0, 1).text = "招标文件原文"
        for index, paragraph in enumerate(collected, 1):
            table.cell(index, 0).text = title
            table.cell(index, 1).text = paragraph
        holder = _insert_after(cursor, "")
        cursor._p.addnext(table._tbl)
        anchor = holder


def finish_document(doc, corpus: str = "") -> dict[str, int]:
    """Apply the closing cleanup to an open document. Returns counts."""
    noise = drop_bid_noise(doc)
    _restore_full_dates(doc, corpus)
    _ensure_overview(doc, corpus)
    _ensure_sentence(doc, corpus, ("提前2个月", "催缴函"), ("客户", "档案", "租务", "评分专项"))
    _ensure_sentence(doc, corpus, ("营业执照副本", "安全责任协议书"), ("档案", "客户", "租务", "评分专项"))
    _ensure_sentence(doc, corpus, ("图文公司",), ("通行", "门禁", "秩序", "评分专项"))
    _ensure_sentence(doc, corpus, ("公共责任险", "车位险"), ("应急", "风险", "评分专项"))
    _ensure_sentence(doc, corpus, ("33周", "231"), ("LEED", "评分专项", "应急"))
    _ensure_sentence(doc, corpus, ("GBCI",), ("LEED", "评分专项"))
    _ensure_scoring_sections(doc, corpus)
    duplicates = remove_duplicate_tables_in_docx(doc)
    final_polish_docx_paragraphs(doc)
    return {"noise": noise, "duplicate_tables": duplicates}
