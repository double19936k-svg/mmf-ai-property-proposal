"""MMF-007C Round 2 §10-§17：把"篇幅差多少页"翻译成"缺哪几项专业能力交付物"。

Round 1 的补深候选只有一个来源：某节 `UNDER_LENGTH`。那条路在真实 300-Class 运行里
一次都没触发（Supplement 调用 = 0），因为它问的是"这一节字数不够"，而整篇文档缺的是
**能落到纸面上的交付物** —— 设备台账、巡检标准、年度维保计划、故障分类矩阵、工单模板、
应急操作卡、投诉分级、回访标准、客服记录表。本模块只做一件事：读专业域能力目录
（`assets/longform/capability_catalogue.json`），对照已经到达的正文，把页面缺口拆成
一批"新增 Content Unit 应该产出什么"，交给 `depth_supplement` 变成契约。

纯本地计算，零 Provider 调用。伪补充（"把本节写得更详细"）在这里就没有出口：
每条缺口都必须带着 required_output 与 archetype，落不到具体交付物形态上的缺口不产出。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from planning.content_engineering import CONTENT_ARCHETYPES
from planning.unified_budget import LEDGER_CHARS_PER_TECHNICAL_PAGE, pages_from_ledger_chars

from .final_assembly import visible_text

CATALOGUE_PATH = (Path(__file__).resolve().parents[1]
                  / "assets" / "longform" / "capability_catalogue.json")
CATALOGUE_SCHEMA = "mmf-capability-catalogue-v0.1"

# 一个新增 Content Unit 大约**一页技术有效内容**。补深 CU 的额度是统一货币
# （正文面 + 结构 payload，见 unified_budget），所以这里的"一页"取账本尺子
# （Run D 反标 1,062 字/技术页），不是产物的 900 字/页 —— 两者差 1.18 倍，
# 拿 900 当补深额度等于每补一个单元多写出 18% 的页数。
SUPPLEMENT_UNIT_CHARS = int(round(LEDGER_CHARS_PER_TECHNICAL_PAGE))
# 一次补深调用打包几个 CU：调用数受 §16 的 12-16 上限约束，CU 数由页面缺口决定。
MAX_CAPABILITIES_PER_SECTION = 3
MAX_SECTIONS_PER_PLAN = 24
# 缺口的相对权重：整域一项都没有 > 结构化形态缺失 > 目录里点名的常规缺项。
REASON_VALUE_SCORES = {
    "DOMAIN_ABSENT": 52,
    "PAGE_CAPABILITY_GAP": 48,
    "STRUCTURED_CAPABILITY_MISSING": 38,
}
# 正文里出现这些词就认为该能力"已经有人写了"，不再重复立项。
_GENERIC_TOKENS = frozenset({"落实", "加强", "完善", "做好", "确保", "相关", "按需", "视情况"})

STATUS_GAP = "CAPABILITY_GAP_PLANNED"
STATUS_TARGET_MET = "PAGE_TARGET_MET"
STATUS_NO_GAP = "NO_CAPABILITY_GAP_FOUND"
STATUS_CATALOGUE_UNAVAILABLE = "CATALOGUE_UNAVAILABLE"


@lru_cache(maxsize=1)
def load_catalogue(path: Path | None = None) -> dict[str, Any]:
    """专业域能力目录。读不到时返回空目录而不抛异常 —— QA 阶段不能因为数据文件失踪就中断交付。"""
    target = path or CATALOGUE_PATH
    try:
        payload = json.loads(Path(target).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"schema_version": CATALOGUE_SCHEMA, "domains": [], "status": "CATALOGUE_UNAVAILABLE"}
    domains = []
    for row in payload.get("domains") or []:
        capabilities = []
        for cap in row.get("capabilities") or []:
            detect = [str(token) for token in (cap.get("detect") or []) if str(token).strip()]
            archetype = str(cap.get("archetype") or "")
            if not cap.get("key") or not detect or archetype not in CONTENT_ARCHETYPES:
                # 目录里一条不能判定、也不能变成 Content Unit 契约的条目就是噪声。
                continue
            capabilities.append({
                "key": str(cap["key"]),
                "capability": str(cap.get("capability") or cap["key"]),
                "required_output": str(cap.get("required_output") or cap.get("capability") or ""),
                "archetype": archetype,
                "detect": detect,
                "min_items": max(4, int(cap.get("min_items") or 4)),
            })
        if not capabilities:
            continue
        domains.append({
            "key": str(row.get("key") or ""),
            "label": str(row.get("label") or row.get("key") or ""),
            "signals": [str(token) for token in (row.get("signals") or []) if str(token).strip()],
            "capabilities": capabilities,
        })
    return {"schema_version": str(payload.get("schema_version") or CATALOGUE_SCHEMA),
            "domains": domains, "status": "LOADED"}


def catalogue_status(catalogue: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = catalogue if catalogue is not None else load_catalogue()
    return {
        "status": payload.get("status"),
        "schema_version": payload.get("schema_version"),
        "domains": len(payload.get("domains") or []),
        "capabilities": sum(len(row.get("capabilities") or []) for row in (payload.get("domains") or [])),
    }


def _section_text(section: dict[str, Any]) -> str:
    """该节已经到达的可见文字：判定能力"有没有写"的唯一依据。

    两种形态都要能读 —— QA 侧的 section 用 `title/body_blocks/processes/callouts`，
    装配后的 delivery artifact 用 `heading/paragraphs/bullets`。只看其中一种会让
    另一条链路上的章节被误判成"一个字都没写"。
    """
    parts = [str(section.get("title") or section.get("heading") or "")]
    for key in ("body_blocks", "paragraphs", "bullets", "tables", "processes", "callouts"):
        parts.append(visible_text(section.get(key)))
    return "\n".join(part for part in parts if part)


def _has_body(section: dict[str, Any]) -> bool:
    """这一节自己有没有带正文。`_section_text` 把标题算进去，光看长度分不清"薄"和"壳"。"""
    for key in ("body_blocks", "paragraphs", "bullets", "tables", "processes", "callouts"):
        if section.get(key):
            return True
    return False


def match_domain(title: str, *, catalogue: dict[str, Any] | None = None) -> str:
    """按章节标题判专业域；判不出来返回 ""，让调用方退回到"通用形态缺口"。"""
    text = str(title or "")
    best, hits = "", 0
    for row in (catalogue if catalogue is not None else load_catalogue()).get("domains") or []:
        score = sum(1 for token in row["signals"] if token in text)
        if score > hits:
            best, hits = str(row["key"]), score
    return best


def present_capabilities(text: str, domain: dict[str, Any]) -> list[str]:
    """该节文本里已经点名的能力。只认具体名词，泛化动词一律不算（见 _GENERIC_TOKENS）。"""
    found = []
    for cap in domain.get("capabilities") or []:
        tokens = [token for token in cap["detect"] if token not in _GENERIC_TOKENS]
        if tokens and any(token in text for token in tokens):
            found.append(cap["key"])
    return found


def _archetype_gap_shapes(archetypes: list[str]) -> list[str]:
    """已到达 Content Unit 的原型分布里，哪些可交付形态压根没出现过。"""
    arrived = {str(name or "").upper() for name in archetypes or [] if str(name or "").strip()}
    if not arrived:
        return []
    shapes = []
    if "FORM_TEMPLATE" not in arrived:
        shapes.append("FORM_TEMPLATE")
    if "CHECKLIST" not in arrived:
        shapes.append("CHECKLIST")
    if not arrived & {"MATRIX", "RESPONSIBILITY_MATRIX", "KPI_TABLE", "STANDARD_TABLE", "RISK_REGISTER"}:
        shapes.append("MATRIX")
    if not arrived & {"SOP", "PROCESS", "EMERGENCY_PLAYBOOK", "SCHEDULE"}:
        shapes.append("SOP")
    return shapes


def plan_capability_gaps(
    artifact: dict[str, Any] | None,
    *,
    page_gap: int,
    arrived_pages: int | None = None,
    archetypes_by_section: dict[str, list[str]] | None = None,
    exclude_section_ids: set[str] | None = None,
    max_sections: int = MAX_SECTIONS_PER_PLAN,
    max_per_section: int = MAX_CAPABILITIES_PER_SECTION,
    catalogue: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把页面缺口摊到具体章节的具体能力上，返回 {"gaps": [...], "status": ..., "metrics": ...}。

    `page_gap` 是"离目标下沿还差几页"。差额决定要多少条缺口：一条缺口按一个新增
    Content Unit ≈ 1 页估算，多备一些再由 §16 的调用上限截断。缺口只落在**已经存在**
    的章节上（补一个不存在的章节是重新规划大纲，不是补深），并且跳过本轮不负责生成的
    章节，与修复预算的口径保持一致。
    """
    catalog = catalogue if catalogue is not None else load_catalogue()
    sections = [row for row in ((artifact or {}).get("sections") or []) if isinstance(row, dict)]
    skipped = set(str(sid) for sid in (exclude_section_ids or set()))
    pages_needed = max(0, int(page_gap or 0))
    gaps: list[dict[str, Any]] = []
    if pages_needed <= 0:
        # §16 的停止条件：页数已经达标就不再补深。凑到 300 页不是目标，
        # 覆盖不足由需求/评分两条候选来源处理，不靠这里加Content Unit。
        return {"schema_version": "content-capability-gaps-v0.1",
                "gaps": [], "status": STATUS_TARGET_MET,
                "page_gap": 0, "arrived_pages": arrived_pages,
                "metrics": {"sections_scanned": len(sections),
                            "catalogue": catalogue_status(catalog)}}
    if not catalog.get("domains"):
        return {"schema_version": "content-capability-gaps-v0.1",
                "gaps": [], "status": STATUS_CATALOGUE_UNAVAILABLE,
                "page_gap": pages_needed, "arrived_pages": arrived_pages,
                "metrics": {"sections_scanned": len(sections),
                            "catalogue": catalogue_status(catalog)}}

    by_domain: dict[str, int] = {}
    shells = 0
    # 排序：先补"整章一项专业能力都没有"的章节，再补"有但缺关键形态"的章节；
    # 同档内按该章已到达字数降序 —— 厚章节里加一张台账是真的加内容，薄章节里加是凑。
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for row in sections:
        sid = str(row.get("section_id") or "")
        if not sid or sid in skipped:
            continue
        if not _has_body(row):
            # 只有标题的篇/章壳子不是补深目标：它的正文在同级小节里，往壳子上挂台账
            # 既会和真正的小节重复立项，也不是"这一节写薄了"。（空节由 BLOCK 来源处理。）
            shells += 1
            continue
        domain_key = match_domain(str(row.get("title") or row.get("heading") or ""), catalogue=catalog)
        domain = next((item for item in catalog["domains"]
                       if item["key"] and item["key"] == domain_key), None)
        if domain is None:
            # 判不出专业域：没有目录背书的具体交付物，就不许编一条缺口出来。
            continue
        by_domain[domain_key] = by_domain.get(domain_key, 0) + 1
        text = _section_text(row)
        shapes = _archetype_gap_shapes((archetypes_by_section or {}).get(sid) or [])
        present = set(present_capabilities(text, domain))
        missing = [cap for cap in domain["capabilities"] if cap["key"] not in present]
        if not missing:
            continue
        rank = 0 if len(present) == 0 else 1
        scored.append((rank, -len(text), {"sid": sid,
                                          "title": str(row.get("title") or row.get("heading") or ""),
                                          "domain": domain, "missing": missing,
                                          "present": sorted(present), "shapes": shapes}))

    ordered = sorted(scored, key=lambda item: (item[0], item[1]))
    for rank, _size, carrier in ordered[:max(0, int(max_sections))]:
        if len(gaps) >= pages_needed:
            break
        domain, missing = carrier["domain"], carrier["missing"]
        shapes = set(carrier["shapes"])
        taken = 0
        for cap in missing:
            if taken >= max_per_section or len(gaps) >= pages_needed:
                break
            if shapes and cap["archetype"] not in shapes and taken >= 1:
                # 该章已有这一形态的 CU，就别再堆同类；先去找它真正缺的形态。
                continue
            reason = ("DOMAIN_ABSENT" if rank == 0
                      else ("STRUCTURED_CAPABILITY_MISSING" if cap["archetype"] in shapes
                            else "PAGE_CAPABILITY_GAP"))
            gaps.append({
                "section_id": carrier["sid"],
                "section_title": carrier["title"],
                "domain": domain["key"],
                "domain_label": domain["label"],
                "capability_key": cap["key"],
                "missing_capability": cap["capability"],
                "required_output": cap["required_output"],
                "archetype": cap["archetype"],
                "min_items": cap["min_items"],
                "reason": reason,
                "value_score": REASON_VALUE_SCORES[reason],
                "estimated_pages": float(pages_from_ledger_chars(SUPPLEMENT_UNIT_CHARS)),
                "estimated_tokens": max(1, SUPPLEMENT_UNIT_CHARS // 4),
                "novelty_basis": (
                    f"本章已到达正文中未出现「{cap['capability']}」的任一判据"
                    f"（{'、'.join(cap['detect'][:3])}）；新增 CU 与原意不重叠"
                ),
                "capabilities_already_present": carrier["present"],
            })
            taken += 1
    return {
        "schema_version": "content-capability-gaps-v0.1",
        "status": STATUS_GAP if gaps else STATUS_NO_GAP,
        "page_gap": pages_needed,
        "arrived_pages": arrived_pages,
        "gaps": gaps,
        "metrics": {
            "sections_scanned": len(sections),
            "outline_shells_skipped": shells,
            "sections_with_domain": len(by_domain),
            "sections_candidate": len(scored),
            "sections_in_plan": len({row["section_id"] for row in gaps}),
            "domain_distribution": by_domain,
            "reason_counts": {reason: sum(1 for row in gaps if row["reason"] == reason)
                              for reason in REASON_VALUE_SCORES},
            "estimated_pages_total": round(sum(float(row["estimated_pages"]) for row in gaps), 2),
            "catalogue": catalogue_status(catalog),
        },
    }
