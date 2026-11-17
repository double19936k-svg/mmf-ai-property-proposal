"""Lossless manual input ingestion; writing instructions are not tender clauses.

Only used when the product creates a pack from a new-plan form. Existing tender
packs and their scope classifiers are deliberately unchanged. Reference material
is supplied as attributed context, never promoted to confirmed project facts.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any


_HEADER = re.compile(r"(?m)^\[(?P<label>[^\]\n]{1,80})\][ \t]*(?:\r?\n|$)")
_REFERENCE = re.compile(r"项目简介|项目资料|参考资料|来源材料|逐页原文|资料原文|图件说明")
# Split clauses, not arbitrary character counts. Keep every verbatim source span.
_CLAUSE = re.compile(r"[^。！？!?；;\n，,]+(?:[。！？!?；;，,]|\n+|$)")
_INVENTION = re.compile(r"(?:不得|不可|不能|不应|不)(?:擅自|自行)?(?:编造|虚构|捏造|补值)")
_CONCRETE_ACTION = re.compile(
    r"(?:并|且|同时|但|还)(?:必须|须|应|需|要)?(?:提交|提供|附上|缴纳|支付|报价|具备|拥有)"
    r"|(?:必须|须|应|需)(?:提交|提供|缴纳|支付|具备|拥有)"
)
_RENDERING = re.compile(r"字体|字号|左对齐|首行缩进|排版|示意|图片|图文|页码|目录")
_OMIT = re.compile(
    r"^(?:本方案|本次方案|技术方案|技术文件|正文)?(?:不写|不要写|不得写入|不出现|不包含|不铺陈|避免出现)"
)
_OMIT_OBJECT = re.compile(r"招标代理|地址|表单|编号|记录表|填写|报价|资质|业绩")


def _writing_instruction(text: str) -> bool:
    # A mixed instruction must never hide a concrete credential/pricing demand.
    if _CONCRETE_ACTION.search(text):
        return False
    if _INVENTION.search(text):
        return True
    if _OMIT.search(text) and _OMIT_OBJECT.search(text):
        return True
    if _RENDERING.search(text) and re.search(r"采用|使用|统一|对齐|缩进|排版|注明|保持", text):
        return True
    if re.search(r"(?:方法|措施|建议).*(?:区别|区分).*(?:事实|现状)", text):
        return True
    return bool(re.match(r"供.{0,30}(?:阅读|比较|审阅)", text))


def parse_manual_brief(text: str, *, split_commas: bool = True) -> dict[str, Any]:
    """Return source-bound requirements, writing constraints and references.

    The exact full input is retained once. All derived records have offsets and
    their exact text; no ellipsis, 800-character slice, or fabricated service
    requirement is introduced when the input contains only excluded clauses.
    """
    text = str(text)
    source_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    requirements: list[dict[str, Any]] = []
    constraints: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []

    def record(start: int, end: int) -> dict[str, Any]:
        return {"text": text[start:end], "source_span": {"start": start, "end": end},
                "source_sha256": source_sha}

    headers = list(_HEADER.finditer(text))
    blocks = [(0, headers[0].start() if headers else len(text), "")]
    blocks.extend((h.end(), headers[i+1].start() if i+1 < len(headers) else len(text), h["label"])
                  for i, h in enumerate(headers))
    for start, end, label in blocks:
        if not text[start:end].strip():
            continue
        if _REFERENCE.search(label):
            references.append({"reference_id": f"REF-{len(references)+1:04d}",
                               "label": label, "authority": "USER_SUPPLIED_REFERENCE",
                               "verified_current_project_fact": False, **record(start, end)})
            continue
        clauses = _CLAUSE if split_commas else re.compile(r"[^。！？!?；;\n]+(?:[。！？!?；;]|\n+|$)")
        for match in clauses.finditer(text, start, end):
            raw = match.group()
            offset = len(raw) - len(raw.lstrip())
            a, b = match.start()+offset, match.end()
            if not text[a:b].strip():
                continue
            item = record(a, b)
            if _writing_instruction(item["text"]):
                constraints.append({"constraint_id": f"WRI-{len(constraints)+1:04d}", **item})
            else:
                requirements.append({"requirement_id": f"REQ-{len(requirements)+1:04d}",
                                     "normalized_requirement": item.pop("text"),
                                     "domain": "other", "mandatory_level": "MUST",
                                     "confirmation_status": "CONFIRMED", **item})
    return {"manual_input": {"kind": "MANUAL_BRIEF", "text": text, "sha256": source_sha},
            "requirements": requirements, "writing_constraints": constraints,
            "reference_material": references}
