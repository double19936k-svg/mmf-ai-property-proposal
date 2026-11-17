"""Document-level QA re-grading and composite scale model.

The pre-007C gates were written for single-section and 70-page outputs, where a
BLOCK simply meant "ask the model again for this Section".  In a 250-320 page
technical bid that same BLOCK turned into an unbounded regeneration loop and,
in the worst case, no deliverable at all.  This module keeps every existing
detector and changes only what its finding *means* for delivery:

BLOCK        the artifact cannot honestly be handed over yet
NEEDS_REVIEW deliverable, but a human must look at it
WARNING      deliverable, cosmetic, never spends a provider call

Char counts are a reference metric here, not a gate: a bid that carries 169
native tables can reach the page target well below the old character floor.
"""

from __future__ import annotations

from typing import Any

from .table_qa import is_form_table


SEVERITY_BLOCK = "BLOCK"
SEVERITY_NEEDS_REVIEW = "NEEDS_REVIEW"
SEVERITY_WARNING = "WARNING"

# Only these findings may stop delivery.  Everything else is a review item.
BLOCK_RULE_IDS = {
    "LF-001",            # document plan missing -> nothing to assemble
    "LF-002",            # contracted Section absent
    "LF-004",            # Section coverage collapsed
    "LF-005",            # mandatory tender requirement completely uncovered
    "QR-001",            # requirement/scoring coverage regression
    "SCORING_COVERAGE",  # scoring item with no response location
    "UNSUPPORTED_BIDDER_CLAIM",
    "UNSUPPORTED_COMPLETION_CLAIM",
    "COMMITMENT_BLOCK",
    "PROJECT_FACT_CONSISTENCY",
    "NATIVE_TOC_INTEGRITY",
    "FINAL_ARTIFACT_MISSING",
    "DOCX_PHYSICALLY_INVALID",
    "TABLE_SOURCE_FIDELITY",
    "SOURCE_SERVICE_REQUIREMENT_OMITTED",
    "PAGE_CONTRACT_BODY_TOO_SHORT",
    "PAGE_CONTRACT_PHYSICAL_LIMIT",
    "FAQ-004",           # JSON debris making body unreadable
    "FAQ-009",           # truncated sentence
    "SEVERE_SERIALIZATION",
}
# Fixed locally without a provider call; never escalates to BLOCK on its own.
LOCAL_REPAIR_RULE_IDS = {
    "FAQ-002",
    "FAQ-005",
    "FAQ-008",
    "FAQ-007",
    "CLIENT_ARTIFACT_INTERNAL_META",
    "CONDITIONAL_REWRITE_GRAMMAR",
    "BROKEN_CROSS_REFERENCE",
    "BROKEN_FLOW_SERIALIZATION",
    "INTERNAL_GOVERNANCE_TONE",
    "QUOTE_BALANCE_QA",
    "WPS_CURLY_QUOTE_RISK",
    "INTERNAL_SECTION_ID",
}
# Explicitly demoted from BLOCK to review: depth and volume are review items.
NEEDS_REVIEW_RULE_IDS = {
    "LF-003",
    "LF-300-BUDGET",
    "SECTION_UNDER_LENGTH",
    "DEPTH_INCOMPLETE",
    "DOCUMENT_SCALE_BELOW_TARGET",
    "STRUCTURED_CONTENT_LOW",
    "SEMANTIC_REPETITION",
    "ARCHETYPE_DIVERSITY",
    "TABLE_COUNT_LOW",
    "SOP_COUNT_LOW",
    "FORM_COUNT_LOW",
    "CHECKLIST_COUNT_LOW",
    "TENDER_TEMPLATE_LEAK",
    "MIXED_REVIEW",
    "LATE_RESPONSE_UNRESOLVED",
}

WARNING_RULE_IDS = {
    "PPT-005",
    "WORDING",
    "FORMATTING",
    "STYLE",
}

_PRIORITY_BY_SEVERITY = {
    SEVERITY_BLOCK: "P0_BLOCK",
    SEVERITY_NEEDS_REVIEW: "NEEDS_REVIEW",
    SEVERITY_WARNING: "NONE",
}


def grade_finding(finding: dict[str, Any]) -> str:
    rule = str(finding.get("rule_id") or "")
    issue = str(finding.get("issue") or "")
    declared = str(finding.get("severity") or "").upper()
    if rule in BLOCK_RULE_IDS or declared == "BLOCK" and (
        rule in BLOCK_RULE_IDS
        or any(token in issue for token in ("missing sections", "cannot be read", "unreadable"))
    ):
        return SEVERITY_BLOCK
    if rule in WARNING_RULE_IDS or declared == "WARNING":
        return SEVERITY_WARNING
    return SEVERITY_NEEDS_REVIEW


def regrade_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return findings carrying the 007C severity plus its repair priority."""
    graded: list[dict[str, Any]] = []
    for row in findings or []:
        finding = dict(row)
        severity = grade_finding(finding)
        finding["declared_severity"] = str(row.get("severity") or "").upper() or None
        finding["severity"] = severity
        finding["repair_priority"] = _PRIORITY_BY_SEVERITY[severity]
        finding["local_repair_only"] = str(row.get("rule_id") or "") in LOCAL_REPAIR_RULE_IDS
        finding["provider_repair_eligible"] = (
            severity == SEVERITY_BLOCK and not finding["local_repair_only"]
        )
        graded.append(finding)
    return graded


def summarize_severity(findings: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {SEVERITY_BLOCK: 0, SEVERITY_NEEDS_REVIEW: 0, SEVERITY_WARNING: 0}
    for row in findings or []:
        counts[str(row.get("severity") or SEVERITY_NEEDS_REVIEW)] = (
            counts.get(str(row.get("severity") or SEVERITY_NEEDS_REVIEW), 0) + 1
        )
    if counts[SEVERITY_BLOCK]:
        status = SEVERITY_BLOCK
    elif counts[SEVERITY_NEEDS_REVIEW]:
        status = SEVERITY_NEEDS_REVIEW
    elif counts[SEVERITY_WARNING]:
        status = SEVERITY_WARNING
    else:
        status = "PASS"
    return {"status": status, "counts": counts}


# 项目统一口径：1 页 ≈ 900 个有效字符（planner 的 chars_per_page_estimate）。
# 该口径是按连续正文标定的，因此结构化内容的字符数已经计入了 effective_chars，
# 这里只补它额外的版面留白（表题、行高、列宽、缩进），避免双重计量。
TEXT_CHARS_PER_PAGE = 900
STRUCTURED_LAYOUT_UPLIFT = {
    "table": 0.10,
    "process": 0.08,
    "checklist": 0.05,
    "form": 0.06,
    "matrix": 0.05,
    "list": 0.03,
    "callout": 0.05,
}
FRONT_MATTER_PAGES = 2.0  # 封面 + 原生目录
LIST_BLOCK_TYPES = {"bullet_group", "numbered_steps"}
# §12：占位页（整页靠【待填写】/空表格撑）超过 5% 就要人工复核。Run C 用产品自己的
# 逐页文本口径实测为 2.0%（8/404），所以这条线不是为放过本轮而画的。
PLACEHOLDER_PAGE_RATIO_REVIEW_MIN = 0.05
# §35：正文表格里的"矩阵"类，用于结构报告；表单模板走 is_form_table 判定，两者不重叠。
MATRIX_ARCHETYPES = frozenset({"MATRIX", "RESPONSIBILITY_MATRIX", "RISK_REGISTER", "KPI_TABLE"})


def count_artifact_structure(artifact: dict[str, Any] | None) -> dict[str, Any]:
    """Count the layout objects that actually reached the assembled document.

    Only arrived content is measured: planned archetype counts would credit
    pages for Content Units the Provider never returned.
    """
    rows = (artifact or {}).get("sections") or (artifact or {}).get("slides") or []
    counts = {key: 0 for key in (
        "tables", "processes", "checklists", "forms", "matrices",
        "list_blocks", "paragraph_blocks", "callouts", "sections",
    )}
    for row in rows:
        if not isinstance(row, dict):
            continue
        counts["sections"] += 1
        counts["tables"] += len(row.get("tables") or [])
        counts["processes"] += len(row.get("processes") or [])
        counts["callouts"] += len(row.get("callouts") or [])
        # §38：forms / matrices 以前只在这个字典里初始化、从来没人加过数，所以真实 300 页
        # 文档的结构报告一路报 "forms=0, matrices=0"。按**已经渲染出来的表**补上口径：
        # 只数产物里真有的表，不数计划里的 archetype（那等于为 Provider 没回来的内容记账）。
        for table in row.get("tables") or []:
            if not isinstance(table, dict):
                continue
            archetype = str(table.get("archetype") or "").upper()
            if is_form_table(table.get("title") or table.get("form_name"),
                             table.get("columns") or [], archetype):
                counts["forms"] += 1
            elif archetype in MATRIX_ARCHETYPES:
                counts["matrices"] += 1
            elif archetype == "CHECKLIST":
                counts["checklists"] += 1
        blocks = row.get("body_blocks") or []
        if blocks:
            for block in blocks:
                kind = str((block or {}).get("type") if isinstance(block, dict) else "paragraph")
                if kind in LIST_BLOCK_TYPES:
                    counts["list_blocks"] += 1
                else:
                    counts["paragraph_blocks"] += 1
            continue
        # 装配后的 Word 产物没有 body_blocks：正文在 paragraphs/bullets 两个平铺列表里。
        # 只认片段形态会让真实 300 页文档报出"零段落、零列表"，页面估算的版式增量
        # 也跟着落空 —— 上一轮的结构化内容统计就是这么变成 0 的。
        counts["paragraph_blocks"] += len(row.get("paragraphs") or [])
        counts["list_blocks"] += len(row.get("bullets") or [])
    return counts


def estimate_rendered_pages(*, effective_chars: int = 0, structure: dict[str, Any] | None = None) -> int | None:
    """Estimate WPS/Office pages from arrived text plus its layout overhead.

    Returns None when nothing arrived: NOT_MEASURED must not be confused with a
    1-page document.
    """
    layout = structure or {}
    text_pages = max(0, int(effective_chars or 0)) / TEXT_CHARS_PER_PAGE
    uplift = (
        int(layout.get("tables") or 0) * STRUCTURED_LAYOUT_UPLIFT["table"]
        + int(layout.get("processes") or 0) * STRUCTURED_LAYOUT_UPLIFT["process"]
        + int(layout.get("checklists") or 0) * STRUCTURED_LAYOUT_UPLIFT["checklist"]
        + int(layout.get("forms") or 0) * STRUCTURED_LAYOUT_UPLIFT["form"]
        + int(layout.get("matrices") or 0) * STRUCTURED_LAYOUT_UPLIFT["matrix"]
        + int(layout.get("list_blocks") or 0) * STRUCTURED_LAYOUT_UPLIFT["list"]
        + int(layout.get("callouts") or 0) * STRUCTURED_LAYOUT_UPLIFT["callout"]
    )
    front_matter = FRONT_MATTER_PAGES if int(layout.get("sections") or 0) > 0 else 0.0
    if int(effective_chars or 0) <= 0 and uplift <= 0:
        return None
    return max(1, int(text_pages + uplift + front_matter + 0.5))


def rendered_pages_for_artifact(artifact: dict[str, Any] | None, *, effective_chars: int | None = None) -> int | None:
    structure = count_artifact_structure(artifact)
    chars = int(effective_chars) if effective_chars is not None else len(str(artifact or ""))
    return estimate_rendered_pages(effective_chars=chars, structure=structure)


def evaluate_document_scale(
    *,
    rendered_pages: int | None,
    page_target_min: int,
    page_target_max: int,
    effective_chars: int = 0,
    char_target_min: int = 0,
    char_target_currency: str = "artifact_visible_chars",
    requirement_coverage: float | None = None,
    scoring_coverage: float | None = None,
    cu_completion: float | None = None,
    archetype_counts: dict[str, int] | None = None,
    table_count: int = 0,
    sop_count: int = 0,
    form_count: int = 0,
    checklist_count: int = 0,
    matrix_count: int = 0,
    semantic_repetition: int = 0,
    artifact_integrity: bool = True,
    technical_effective_pages: int | None = None,
    page_classes: dict[str, Any] | None = None,
    placeholder_page_ratio: float | None = None,
) -> dict[str, Any]:
    """Composite 300-class scale model.  Characters are reported, never gating.

    §13：页数门控以**技术有效页**为准（扣掉封面、纯目录、空页、占位页、越界范围页）；
    物理页仍然报出来，两个口径不得互相冒充。没有实测分页时 technical_effective_pages
    为空，退回物理页口径，并在 `page_basis` 里写明 PHYSICAL。
    """
    archetypes = {str(key).upper(): int(value or 0) for key, value in (archetype_counts or {}).items()}
    structured_cus = sum(count for name, count in archetypes.items() if name != "NARRATIVE")
    total_cus = sum(archetypes.values())
    narrative_share = (archetypes.get("NARRATIVE", 0) / total_cus) if total_cus else 0.0
    structured_share = (structured_cus / total_cus) if total_cus else 0.0

    dimensions: dict[str, dict[str, Any]] = {}

    def dimension(name: str, *, value: Any, target: Any, ok: bool, weight: float) -> None:
        dimensions[name] = {
            "value": value,
            "target": target,
            "met": bool(ok),
            "weight": float(weight),
        }

    pages = int(rendered_pages or 0)
    technical = int(technical_effective_pages or 0)
    # §13：能实测到技术有效页就按它判，物理页只作并列报告；两者谁在起作用必须写进 basis。
    governing_pages = technical if technical > 0 else pages
    page_basis = "TECHNICAL_EFFECTIVE" if technical > 0 else "PHYSICAL"
    if rendered_pages is None and technical <= 0:
        page_met, page_state = False, "NOT_MEASURED"
    elif governing_pages < int(page_target_min):
        page_met, page_state = False, "BELOW_TARGET"
    elif governing_pages > int(page_target_max):
        page_met, page_state = False, "ABOVE_TARGET"
    else:
        page_met, page_state = True, "IN_RANGE"
    dimension("RENDERED_PAGES", value=governing_pages, target=[page_target_min, page_target_max],
              ok=page_met, weight=0.30)
    dimensions["RENDERED_PAGES"]["physical_value"] = pages
    dimensions["RENDERED_PAGES"]["basis"] = page_basis
    dimension("REQUIREMENT_COVERAGE", value=requirement_coverage, target=0.95,
              ok=requirement_coverage is None or requirement_coverage >= 0.95, weight=0.20)
    dimension("SCORING_COVERAGE", value=scoring_coverage, target=0.95,
              ok=scoring_coverage is None or scoring_coverage >= 0.95, weight=0.20)
    dimension("CU_COMPLETION", value=cu_completion, target=0.98,
              ok=cu_completion is None or cu_completion >= 0.98, weight=0.10)
    dimension("STRUCTURED_SHARE", value=round(structured_share, 4), target=">=0.50",
              ok=bool(total_cus) and structured_share >= 0.50, weight=0.10)
    dimension("SEMANTIC_REPETITION", value=int(semantic_repetition), target=0,
              ok=int(semantic_repetition or 0) == 0, weight=0.05)
    dimension("ARTIFACT_INTEGRITY", value=bool(artifact_integrity), target=True,
              ok=bool(artifact_integrity), weight=0.05)

    scale_score = round(sum(row["weight"] for row in dimensions.values() if row["met"]), 4)
    coverage_strong = (
        (requirement_coverage is None or requirement_coverage >= 0.95)
        and (scoring_coverage is None or scoring_coverage >= 0.95)
        and structured_share >= 0.50
    )
    findings: list[dict[str, Any]] = []
    if not artifact_integrity:
        findings.append({
            "rule_id": "DOCX_PHYSICALLY_INVALID", "path": "artifact", "severity": SEVERITY_BLOCK,
            "issue": "assembled document is not readable",
        })
    if page_state == "BELOW_TARGET":
        findings.append({
            "rule_id": "DOCUMENT_SCALE_BELOW_TARGET", "path": "artifact",
            "severity": SEVERITY_BLOCK if not coverage_strong else SEVERITY_NEEDS_REVIEW,
            "issue": f"{page_basis} {governing_pages} pages, target {page_target_min}-{page_target_max}"
                     f"（物理页 {pages}）",
            "auto_supplement": bool(not coverage_strong),
            "review_note": None if not coverage_strong else (
                "需求与评分覆盖充分、结构化内容健康，仅规模低于目标区间下沿；"
                "不做机械补足，由投标人决定是否继续扩章。"
            ),
        })
    elif page_state == "ABOVE_TARGET":
        # §21：超上限是真缺陷，不是"多写点没坏处"。实测口径下按 NEEDS_REVIEW 处理，
        # 并显式要求进入定向削减；没有分页实测时仍按旧的物理页 WARNING 口径。
        findings.append({
            "rule_id": "DOCUMENT_SCALE_ABOVE_TARGET", "path": "artifact",
            "severity": SEVERITY_NEEDS_REVIEW if page_basis == "TECHNICAL_EFFECTIVE" else SEVERITY_WARNING,
            "issue": f"{page_basis} {governing_pages} pages above target {page_target_max}"
                     f"（物理页 {pages}）",
            "reduction_required": page_basis == "TECHNICAL_EFFECTIVE",
        })
    classes = {str(key): int(value or 0) for key, value in (page_classes or {}).items()}
    if classes.get("EXCLUDED_SCOPE", 0) > 0:
        # §0/§55：商务、报价、资信类内容不得进入技术方案正文。逐页文本判据会误伤
        # 顺带提到这些词的页，所以先按 NEEDS_REVIEW 交人工复核，不直接判死整篇。
        findings.append({
            "rule_id": "EXCLUDED_SCOPE_PAGE_IN_TECHNICAL_BODY", "path": "artifact",
            "severity": SEVERITY_NEEDS_REVIEW,
            "issue": f"{classes['EXCLUDED_SCOPE']} 页命中商务/报价/资信/程序性范围词，须人工确认是否越界",
        })
    ratio = float(placeholder_page_ratio or 0.0)
    if ratio > PLACEHOLDER_PAGE_RATIO_REVIEW_MIN:
        # §12/§42：占位页比例过高要复核，但不得因此把整篇判 FAIL。
        findings.append({
            "rule_id": "PLACEHOLDER_PAGE_RATIO_HIGH", "path": "artifact",
            "severity": SEVERITY_NEEDS_REVIEW,
            "issue": f"占位页比例 {ratio:.2%} 高于 {PLACEHOLDER_PAGE_RATIO_REVIEW_MIN:.0%}，"
                     "存在靠【待填写】或空表格撑篇幅的迹象",
        })
    if total_cus and structured_share < 0.50:
        findings.append({
            "rule_id": "STRUCTURED_CONTENT_LOW", "path": "plan", "severity": SEVERITY_NEEDS_REVIEW,
            "issue": f"structured content share {structured_share:.2f} below 0.50",
        })
    if scoring_coverage is not None and scoring_coverage < 0.80:
        # §27：技术评分实体覆盖率不足 80% 只能 NEEDS_REVIEW，不得自称干净交付。
        findings.append({
            "rule_id": "TECHNICAL_SCORING_COVERAGE_LOW", "path": "plan", "severity": SEVERITY_NEEDS_REVIEW,
            "issue": f"技术评分实体覆盖率 {scoring_coverage:.2f} 低于 0.80，评分项缺少可定位的实体响应",
        })
    if narrative_share > 0.90:
        findings.append({
            "rule_id": "NARRATIVE_DOMINANT", "path": "plan", "severity": SEVERITY_NEEDS_REVIEW,
            "issue": f"{narrative_share:.2f} of Content Units are NARRATIVE",
        })
    if int(semantic_repetition or 0) > 0:
        findings.append({
            "rule_id": "SEMANTIC_REPETITION", "path": "artifact", "severity": SEVERITY_NEEDS_REVIEW,
            "issue": f"{semantic_repetition} semantically repeated passages",
        })

    return {
        "schema_version": "document-scale-v0.2",
        "status": (
            SEVERITY_BLOCK if any(row["severity"] == SEVERITY_BLOCK for row in findings)
            else (SEVERITY_NEEDS_REVIEW if findings else "PASS")
        ),
        "scale_score": scale_score,
        "dimensions": dimensions,
        "page_state": page_state,
        "page_basis": page_basis,
        # §13：物理页与技术有效页并列报告，谁在门控由 page_basis 说明。
        "physical_rendered_pages": pages,
        "technical_effective_pages": technical or None,
        "page_classes": classes,
        "placeholder_page_ratio": round(float(placeholder_page_ratio or 0.0), 4),
        "coverage_strong": coverage_strong,
        "character_reference": {
            "effective_chars": int(effective_chars or 0),
            "target_min": int(char_target_min or 0),
            # 额度与产物不是同一种货币（账本字含结构 payload，产物只数可见字），
            # 所以这里的 target_min 由调用方换算后才传进来，并把怎么换算写在旁边。
            "target_min_currency": str(char_target_currency or "artifact_visible_chars"),
            "ratio": round((effective_chars or 0) / char_target_min, 4) if char_target_min else None,
            "gating": False,
        },
        "content_metrics": {
            "total_content_units": total_cus,
            "structured_content_units": structured_cus,
            "narrative_content_units": archetypes.get("NARRATIVE", 0),
            "narrative_share": round(narrative_share, 4),
            "structured_share": round(structured_share, 4),
            "tables": int(table_count or 0),
            "sop": int(sop_count or 0),
            "forms": int(form_count or 0),
            "checklists": int(checklist_count or 0),
            "matrices": int(matrix_count or 0),
            "archetype_counts": archetypes,
        },
        "findings": findings,
    }
