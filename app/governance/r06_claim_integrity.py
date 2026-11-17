"""Explicit counts need complete, uniquely scoped DOCX carriers.
Unknown headers, merged cells, mixed carriers and incomplete numbering remain
NEEDS_REVIEW. Structured cells preserve embedded newlines as cell content.
"""
from __future__ import annotations
import re
from typing import Any
SCHEMA_VERSION = "r07-promised-count-integrity-v3"
RULE_MISMATCH = "R06_PROMISED_COUNT_MISMATCH"
RULE_UNVERIFIABLE = "R06_PROMISED_COUNT_UNVERIFIABLE"
RULE_REFERENCE_UNVERIFIABLE = "R07_PROMISED_CARRIER_REFERENCE_UNVERIFIABLE"
_NUMBER = r"(?P<number>[一二两三四五六七八九十]{1,3}|[0-9]{1,3})"
_UNIT = r"(?P<unit>项|条|类|步|个)"
_NOUN = r"(?:规定|约定|关键|主要|核心|必经)?(?:步骤|环节|方面|措施|条件|内容|服务|工作|事项|义务|责任|流程)"
_QUALIFIED_NOUN = r"(?:规定|约定|关键|主要|核心|必经|应急|服务|管理|作业|技术|安全|交接){0,3}(?:步骤|环节|方面|措施|条件|内容|服务|工作|事项|义务|责任|流程)"
_PATTERNS = (
    re.compile(r"(?:本表|下表|如下表|表格)(?:列出|包含|含|覆盖|给出)(?:了)?"+_NUMBER+_UNIT+r"(?!人|小时|分钟|天|班|年|元|%)"),
    re.compile(r"(?:承诺|包含|涵盖|包括)(?:了)?(?:全部)?"+_NUMBER+_UNIT+_QUALIFIED_NOUN),
    re.compile(r"(?:以下|下列|如下)"+_NUMBER+_UNIT+_QUALIFIED_NOUN),
)
_NUM={"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}
_LIST=re.compile(r"^\s*(?P<number>[一二两三四五六七八九十]{1,3}|[0-9]{1,3})[、.．]\s*\S")
_HEADER=re.compile(r"^(?:序号|编号|内容|说明|措施|事项|类别|对象|部位|项目|工作|阶段|步骤|环节|关键字段|管理事项|字段说明|核对项)$")
_SUMMARY=re.compile(r"^(?:合计|总计|汇总|小计)(?:\s|[:：]|$)")
_NEGATED=re.compile(r"(?:不|未|没有|无需|不得|不能|例如|比如|举例)\s*$")
def _to_int(word:str)->int|None:
    if word.isdigit():return int(word)
    if word in _NUM:return _NUM[word]
    if re.fullmatch(r"[二两三四五六七八九]?十[一二三四五六七八九]?",word):
        a,b=word.split("十");return (_NUM[a] if a else 1)*10+(_NUM[b] if b else 0)
    return None
def _promises(text:str)->list[dict[str,Any]]:
    matches=[]
    for pattern in _PATTERNS:
        for m in pattern.finditer(text):
            if any(m.start()<v["span"][1] and v["span"][0]<m.end() for v in matches):continue
            prefix=text[:m.start()]
            if _NEGATED.search(prefix[-12:]) or re.search(r"(?:不足|足)$",prefix):continue
            if any(prefix.count(a)>prefix.count(b) for a,b in (("“","”"),("‘","’"),("「","」"))):continue
            n=_to_int(m.group("number"))
            if n is None or n<=0:continue
            suffix=text[m.end("unit"):]
            if m.group("unit")=="个" and not re.match(
                    r"(?:规定|约定|关键|主要|核心|必经)?(?:步骤|环节|方面|措施|条件|内容|服务项目|工作事项|事项|义务|责任|流程)",suffix):continue
            carrier_kind = "table" if re.match(r"(?:本表|下表|如下表|表格)",m.group(0)) else (
                "sequence" if re.search(r"步骤|环节|流程",m.group(0)) else "items")
            matches.append({"count":n,"unit":m.group("unit"),"text":m.group(0),"span":[m.start(),m.end()],"carrier_kind":carrier_kind})
    return sorted(matches,key=lambda v:v["span"])
def _table_count(block:dict[str,Any],unit:str,carrier_kind:str)->tuple[int|None,str]:
    rows=block.get("table_rows")
    if rows is None:rows=[line.split(" | ") for line in str(block.get("text") or "").splitlines() if line.strip()]
    if not isinstance(rows,list) or len(rows)<2:return None,"NO_COMPLETE_TABLE"
    if block.get("table_has_merged_cells"):return None,"MERGED_CELLS"
    if any(not isinstance(row,list) or len(row)!=len(rows[0]) for row in rows):return None,"IRREGULAR_ROWS"
    header=[str(v).strip() for v in rows[0]];flags=block.get("table_header_rows") or []
    explicit=bool(flags and flags[0] is True)
    lexical=all(_HEADER.fullmatch(v) for v in header) and len(header)>=2
    if not explicit and not lexical:return None,"HEADER_UNPROVEN"
    data=[row for row in rows[1:] if any(str(v).strip() for v in row)]
    data=[row for row in data if not _SUMMARY.match(str(row[0]).strip())]
    if not data:return None,"NO_COUNTABLE_DATA"
    identities=[str(row[0]).strip() for row in data]
    if not all(identities) or len(set(identities))!=len(identities):return None,"ROW_IDENTITY_AMBIGUOUS"
    if unit=="类" and not re.search(r"类别|类型",header[0]):return None,"CATEGORY_TO_ROWS_UNPROVEN"
    if carrier_kind=="sequence" and not re.search(r"步骤|环节|流程",header[0]):
        if header[0] not in {"序号","编号"} or identities!=[str(n) for n in range(1,len(data)+1)]:
            return None,"STEPS_TO_ROWS_UNPROVEN"
    return len(data),"EXPLICIT_HEADER" if explicit else "LEXICAL_HEADER"
def _numbering(block:dict[str,Any])->tuple[str,int|str]|None:
    if block.get("heading_level") or block.get("kind")!="paragraph":return None
    m=_LIST.match(str(block.get("text") or ""))
    if m:return ("literal",_to_int(m.group("number")) or -1)
    native=block.get("numbering")
    if native and native.get("num_id") is not None:
        kind = "bullet" if native.get("format")=="bullet" else ("native" if native.get("format") else "unknown")
        return (kind,str(native["num_id"])+":"+str(native.get("level",0)))
    return None
def _carriers(blocks:list[dict[str,Any]],start:int,window:int,unit:str,carrier_kind:str)->list[dict[str,Any]]:
    end=min(len(blocks),start+window)
    # No look-ahead helper may cross the same explicit heading boundary.
    for at in range(start,end):
        if blocks[at].get("heading_level"):end=at;break
    carriers=[];at=start
    while at<end:
        block=blocks[at]
        if block.get("kind")=="table":
            count,proof=_table_count(block,unit,carrier_kind)
            carriers.append({"count":count,"proof":proof,"paths":[block.get("path")],"kind":"table"});at+=1;continue
        if _numbering(block) is None:at+=1;continue
        group=[];markers=[]
        while at<end and _numbering(blocks[at]) is not None:
            group.append(blocks[at]);markers.append(_numbering(blocks[at]));at+=1
        follows=_numbering(blocks[at]) if at<len(blocks) else None
        complete=at!=end or follows is None
        literals=[v[1] for v in markers if v[0]=="literal"];native=[v[1] for v in markers if v[0]=="native"]
        sequence_ok=(len(literals)==len(markers) and literals==list(range(1,len(markers)+1))) or (
                len(native)==len(markers) and len(set(native))==1)
        bullets=[v[1] for v in markers if v[0]=="bullet"]
        if carrier_kind=="items" and len(bullets)==len(markers) and len(set(bullets))==1:
            sequence_ok=len({v.get('text') for v in group})==len(group)
        valid=complete and sequence_ok and unit!="类" and carrier_kind!="table"
        proof="CONTIGUOUS_NUMBERED_LIST" if valid else ("WINDOW_CUT" if not complete else "NUMBERING_OR_CATEGORY_AMBIGUOUS")
        carriers.append({"count":len(group) if valid else None,"proof":proof,"paths":[v.get("path") for v in group],"kind":"list"})
    return carriers
def _inline_carrier(text:str,promise:dict[str,Any],path:str)->dict[str,Any]|None:
    """Only a closed, flat enumeration introduced by this very claim counts.

    Examples, alternatives, parenthesized nested lists and open-ended 'etc.'
    stay unproven. A table locator is a separate obligation; proving an inline
    count never supplies a missing table.
    """
    if promise["carrier_kind"] == "sequence":
        return None  # a flat noun list does not prove an ordered procedure
    start=promise["span"][1]
    remainder=text[start:]
    head=re.match(r"[^。；;，,：:]{0,24}[：:]",remainder)
    if not head:return None
    at=start+head.end()
    tail=text[at:]
    end=re.search(r"[。；;，,!！?？]",tail)
    if not end:return None  # no closed boundary, potentially truncated text
    body=tail[:end.start()].strip()
    if re.search(r"[（）()\[\]【】“”‘’「」]|(?:等|其他|其它|例如|比如|包括但不限于|或|或者|及其他)",body):
        return None
    parts=[re.sub(r"^(?:以及|及|和)","",v.strip()).strip() for v in body.split("、")]
    if len(parts)<2 or any(not 2<=len(v)<=60 for v in parts) or len(set(parts))!=len(parts):
        return None
    return {"kind":"inline","count":len(parts),"proof":"COMPLETE_INLINE_ENUMERATION",
            "paths":[path],"span":[at,at+len(body)],"items":parts,"text":body}


def scan_promised_counts(blocks:list[dict[str,Any]],*,window:int=12)->list[dict[str,Any]]:
    findings=[]
    for index,block in enumerate(blocks):
        if block.get("kind")!="paragraph":continue
        text=str(block.get("text") or "");promises=_promises(text)
        for promise in promises:
            carriers=_carriers(blocks,index+1,window,promise["unit"],promise["carrier_kind"])
            actual=carriers[0]["count"] if len(promises)==1 and len(carriers)==1 else None
            inline=_inline_carrier(text,promise,block.get("path") or f"artifact.paragraphs[{index}]") if len(promises)==1 else None
            inline_count=inline["count"] if inline else None
            reference_unproven=False
            if inline:
                if inline_count!=promise["count"]:
                    # A correctly sized later table cannot erase a complete
                    # contradictory enumeration in the claim sentence itself.
                    actual=inline_count
                elif promise["carrier_kind"]=="table":
                    # A proven contradictory table remains a mismatch even
                    # when a sentence's own enumeration has the right size.
                    if actual is None:
                        actual=inline_count
                        reference_unproven=actual==promise["count"]
                else:
                    actual=inline_count
            if reference_unproven:
                findings.append({
                    "rule_id":RULE_REFERENCE_UNVERIFIABLE,
                    "path":block.get("path") or f"artifact.paragraphs[{index}]",
                    "severity":"NEEDS_REVIEW","qa_class":"NEEDS_REVIEW",
                    "text":promise["text"],"sentence":text,"claim_span":promise["span"],
                    "promised_count":promise["count"],"carrier_count":None,
                    "inline_count":inline_count,"quantity_verified":True,
                    "carrier_evidence":[inline,*carriers],
                    "issue":"同句完整列举的数量吻合；但所指表格的范围或完整性仍无法唯一证明。",
                    "classification_reason":"数量可证，不等于表格引用已成立"})
                continue
            if actual==promise["count"]:continue
            uncertain=actual is None
            findings.append({"rule_id":RULE_UNVERIFIABLE if uncertain else RULE_MISMATCH,
                "path":block.get("path") or f"artifact.paragraphs[{index}]","severity":"NEEDS_REVIEW" if uncertain else "BLOCK",
                "qa_class":"NEEDS_REVIEW" if uncertain else "TRUE_BLOCK","text":promise["text"],"sentence":text,"claim_span":promise["span"],
                "promised_count":promise["count"],"carrier_count":actual,
                "inline_count":inline_count,"quantity_verified":False,
                "carrier_evidence":([inline,*carriers] if inline else carriers),
                "issue":f"明确承诺数量 {promise['count']}；"+("载体范围/完整性无法唯一证明，需人工核对。" if uncertain else f"同节完整载体实际为 {actual}。"),
                "classification_reason":"数量对象或载体范围不明" if uncertain else "明确承诺数量与唯一完整载体可证不一致"})
    return findings
