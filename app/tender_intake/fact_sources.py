"""Canonical facts use literal, located extraction quotes, never model paraphrases."""
from __future__ import annotations
import re
from decimal import Decimal
from typing import Any
from .models import CANONICAL_FACT_KEYS

PATTERNS = {
    "project_name": r"(?<![\u4e00-\u9fff])(?:本|招标|采购)?项目名称\s*[：:|]\s*([^，。；\n|]+)",
    "project_type": r"项目(?:类型|业态)\s*[：:|]\s*([^，。；\n|]+)",
    "project_number": r"项目编号\s*[：:|]\s*([A-Za-z0-9-]+)",
    "location": r"(?<![\u4e00-\u9fff])(?:本项目的?|项目)?(?:地址|地点|所在地|[坐座]落位置)\s*[：:|]\s*([^，。；\n|]+)",
    "gross_area": r"(?:总建筑面积|建筑面积)\s*[：:]?\s*([0-9]+(?:\.[0-9]+)?\s*(?:万)?(?:平方米|㎡))",
    "managed_area": r"管理面积\s*[：:]?\s*([0-9]+(?:\.[0-9]+)?\s*(?:万)?(?:平方米|㎡))",
    "contract_duration": r"(?:合同期限|服务期限|合同期|服务期)\s*[：:]?\s*(?:为|是)?\s*((?:[0-9]+(?:\.[0-9]+)?|[一二三四五六七八九十百]+)\s*(?:年|个月|月|天|日))",
}
def comparison_value(slot: str, value: Any) -> str:
    text = re.sub(r"\s+", "", str(value))
    if slot in {"gross_area", "managed_area"}:
        m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(万)?(?:平方米|㎡)", text)
        if m:
            n = Decimal(m.group(1)) * (10000 if m.group(2) else 1)
            return format(n.normalize(), "f")
    return text

def fact_candidates(requirements: list[dict[str, Any]], extraction: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    paragraphs = extraction.get("paragraphs", [])
    tables = extraction.get("tables", [])
    result: dict[str, list[dict[str, Any]]] = {}
    seen = set()
    for requirement in requirements:
        if requirement.get("excluded_from_solution_body"):
            continue
        for source in requirement.get("sources", []):
            file_id = source.get("file_id")
            if source.get("para_id"):
                blocks = [p.get("text", "") for p in paragraphs if p.get("file_id")==file_id and p.get("para_id")==source["para_id"]]
            elif source.get("table_id"):
                blocks = ["\n".join(" | ".join(str(c).strip() for c in row if str(c).strip())
                          for row in [t.get("columns", [])]+t.get("rows", []))
                          for t in tables if t.get("file_id")==file_id and t.get("table_id")==source["table_id"]]
            elif source.get("paragraph_index") is not None:
                blocks = [p.get("text", "") for p in paragraphs if p.get("file_id")==file_id and p.get("paragraph_index")==source["paragraph_index"]]
            elif source.get("table_index") is not None:
                blocks = ["\n".join(" | ".join(str(c).strip() for c in row if str(c).strip())
                          for row in [t.get("columns", [])]+t.get("rows", []))
                          for t in tables if t.get("file_id")==file_id and t.get("table_index")==source["table_index"]]
            else:
                continue
            # File-format samples and blank form headings are not current-project facts.
            located = [p for p in paragraphs if p.get("file_id")==file_id and
                       (p.get("para_id")==source.get("para_id") if source.get("para_id") else
                        p.get("paragraph_index")==source.get("paragraph_index") and source.get("paragraph_index") is not None)]
            located += [t for t in tables if t.get("file_id")==file_id and
                        (t.get("table_id")==source.get("table_id") if source.get("table_id") else
                         t.get("table_index")==source.get("table_index") and source.get("table_index") is not None)]
            if any("投标文件格式" in " ".join(b.get("heading_path", [])) for b in located):
                continue
            quote = str(source.get("source_excerpt") or "")
            # Whitespace tolerance only; no fuzzy matching, invented quote or wrong locator.
            compact = re.sub(r"\s+", "", quote)
            if not compact or not any(compact in re.sub(r"\s+", "", block) for block in blocks):
                continue
            if any(len(b.get("columns", [])) >= 3 and compact == re.sub(r"\s+", "", " | ".join(str(c).strip() for c in b["columns"] if str(c).strip())) for b in located):
                continue
            matches = [(key, m.group(1).strip()) for key, pattern in PATTERNS.items() for m in re.finditer(pattern, quote)]
            nearby = [p.get("text", "") for p in paragraphs if p.get("file_id")==file_id
                      and source.get("paragraph_index") is not None and p.get("paragraph_index") is not None
                      and 0 < source["paragraph_index"]-p["paragraph_index"] <= 3]
            contact_context = any("招标代理机构" in text or "联系人" in text for text in nearby)
            if contact_context or re.search(r"(?:有限公司|代理机构)[（(]\s*地址", quote):
                matches = [(k,v) for k,v in matches if k!="location" or re.search(r"(?:本项目|项目|[坐座]落位置)(?:的)?(?:地址|地点|所在地)?\s*[：:]", quote)]
            if "产业园" in quote and not any(k=="project_type" for k,_ in matches):
                matches.append(("project_type", "产业园"))
            for key, value in matches:
                # A pricing-form header is not the current project's name.
                if key == 'project_name' and re.fullmatch(r"报价方式|投标总价|投标报价|项目编号|投标人名称|采购人名称|招标人名称", value):
                    continue
                marker = (key, requirement["requirement_id"], file_id, source.get("para_id"), source.get("table_id"), source.get("paragraph_index"), source.get("table_index"), value)
                if marker in seen:
                    continue
                seen.add(marker)
                result.setdefault(key, []).append({"requirement_id":requirement["requirement_id"],
                    "value":value, "source":dict(source), "basis":"VERBATIM_EXTRACTION_QUOTE"})
    return result

def canonical_facts(requirements: list[dict[str, Any]], extraction: dict[str, Any]) -> dict[str, Any]:
    return facts_from_candidates(fact_candidates(requirements, extraction))

def facts_from_candidates(by_slot: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    facts = {}
    for key, candidates in by_slot.items():
        if key not in CANONICAL_FACT_KEYS:
            continue
        unique = {comparison_value(key, c["value"]) for c in candidates}
        multiple = len(unique)>1
        first = candidates[0]
        facts[key] = {"value":"" if multiple else first["value"],
            "source_requirement_id":None if multiple else first["requirement_id"],
            "confirmation_status":"UNCONFIRMED", "fact_status":"MULTI_VALUE" if multiple else "SOURCE_LITERAL",
            "candidates":candidates, "basis":"VERBATIM_EXTRACTION_QUOTE"}
    return facts


def brief_project_fields(pack: dict[str, Any], extraction: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Resolve the public Brief identity without rewriting source quotes or conflicts."""
    facts = pack.get("project_facts") or {}
    fields = {
        key: {"value": str((facts.get(key) or {}).get("value") or "").strip(),
              "source": "招标文件"}
        for key in ("project_name", "project_type")
    }
    paragraphs = extraction.get("paragraphs") or []
    cover_by_file: dict[str, list[str]] = {}
    invitation_names: list[str] = []
    for row in paragraphs:
        text = str(row.get("text") or "").strip()
        headings = row.get("heading_path") or []
        if any(re.search(r"(?:投标|招标|采购)邀请|招标公告|采购公告", heading) for heading in headings):
            if match := re.match(PATTERNS["project_name"], text):
                invitation_names.append(match[1].strip())
        # Cover titles can also use a Word heading style.
        front_rows = cover_by_file.setdefault(row.get("file_id") or "", [])
        if len(front_rows) < 12:
            front_rows.append(text)

    cover_names: list[str] = []
    for rows in cover_by_file.values():
        front = []
        for text in rows:
            if re.match(r"目\s*录|第\s*[一二三四五六七八九十\d]+\s*章", text):
                break
            front.append(text)
        if not any(re.search(r"(?:招标|采购|磋商|询价)文件", text) for text in front):
            continue
        for text in front:
            if match := re.match(PATTERNS["project_name"], text):
                cover_names.append(match[1].strip())
                continue
            title = re.sub(r"\s*[（(]?(?:招标|采购|磋商|询价)文件[）)]?\s*$", "", text).strip()
            if re.fullmatch(r"[^：:。；\n]{3,139}(?:项目|工程|服务|采购)", title):
                cover_names.append(title)

    for names, source in ((cover_names, "招标文件封面"), (invitation_names, "招标公告或投标邀请")):
        distinct = {comparison_value("project_name", name): name for name in names if name}
        if distinct:
            fields["project_name"] = {
                "value": next(iter(distinct.values())) if len(distinct) == 1 else "",
                "source": source,
                "official": len(distinct) == 1,
            }
            break

    name = fields["project_name"]["value"]
    type_fact = facts.get("project_type") or {}
    if not fields["project_type"]["value"] and type_fact.get("fact_status") != "MULTI_VALUE":
        if re.search(r"办公楼|办公用房|写字楼", name) or any(
            re.match(r"^(?:[一二三四五六七八九十]+[、.．]\s*)?(?:办公楼|办公用房|写字楼)概况", str(row.get("text") or "").strip())
            and any("需求" in heading or "服务范围" in heading for heading in row.get("heading_path") or [])
            for row in paragraphs
        ):
            fields["project_type"] = {"value": "办公楼", "source": "招标文件的办公用房说明"}
        elif re.search(r"物业(?:管理|服务|外包)", name):
            fields["project_type"] = {"value": "物业服务项目", "source": "招标文件的项目名称"}
    return fields
