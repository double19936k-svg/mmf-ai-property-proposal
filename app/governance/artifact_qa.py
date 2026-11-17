from __future__ import annotations

import ast
import copy
import json
import re
from typing import Any
from .rules import load_rule
from .client_facing_prose import internal_narration_findings
from .customer_hygiene import apply_hygiene_tree, hygiene_findings, is_structural_path
from .text_sanitize import has_conditional_postprocess_pollution, rewrite_unconfirmed_durations

FORMAL_RULES = load_rule("formal_artifact_qa_v0.1.json")

SAFE_MAP = {"greasy": "油污", "internally": "内部", "internal": "内部", "staffing": "人员配置", "本方案不预填": "具体参数以项目确认结果为准"}
ALLOWED_ENGLISH = {"word", "ppt", "sla", "kpi", "vip", "capex", "iot", "hvac", "sop", "oem", "iso", "led", "ups", "wifi", "pdf", "ok", "ceo", "cctv", "bpm"}
INTERNAL = re.compile(
    r"(?:Guardrail|Knowledge\s*Unit|治理规则|内部门禁|AI提示|安全说明|模型判断|provider|prompt|内部治理|"
    r"Document\s*Plan|MMF规划层|Content\s*Budget|Section\s*Contract|模型仅撰写|统一Document\s*Plan)",
    re.I,
)
INTERNAL_GOVERNANCE = re.compile(
    r"(?:条件性方法|"
    r"本方案(?:不预设|按照条件性方法表述)|不升级为既定承诺)",
    re.I,
)
# §43：内部标识符一律不得进客户成品。Round 3 只有 KU-\d 一条模式，所以
# Section/CU/Requirement/Scoring ID、门名与状态枚举泄漏时是"看不见"的。
INTERNAL_BODY_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9])(?:S\d{2}-\d{2}|CH\d{2}|CU-?\d{3,}|REQ-?\d{3,}|SCR-?\d{2,}|KU-?\d{4}"
    r"|GATE-[A-Z0-9]+|RP-[A-Z-]+|QA-[0-9A-Z]{2,4}|FAQ-\d{3})"
    r"|TECHNICAL_ONLY_SCOPE_GATE|COMPLETED_CLEAN|NEEDS_REVIEW|SOURCE_REQUIRED|SCOPE_EXCLUDED"
    r"|requirement_id|section_id|content_unit_id|scoring_item_id|coverage_status|target_words",
    re.I,
)
# §43：关于"这份文档怎么写"的自我描述（正文/承诺前提/按未确认处理）也是内部口径。
INTERNAL_WRITING_META = re.compile(r"(?:不作为承诺前提|按未确认处理|写入正文|不作承诺前提|本方案按未确认)")
TENDER_TEMPLATE = re.compile(
    r"(?:本授权书声明|授权委托书|法定代表人授权书|注册于[（(]国家或地区的名称[）)]|"
    r"[（(]公司名称[）)]|[（(]法人代表姓名[）)]|[（(]被授权人的姓名、?职务[）)]|"
    r"投标书格式|投标函格式|服务费报价单|外装信封|投标文件密封)",
    re.I,
)
JSON_DEBRIS = re.compile(r"(?:\{\s*[\"']\w+[\"']\s*:|```json|\[\s*\{\s*[\"'])", re.I)
TRANSPORT_RESIDUE = re.compile(r"<<<\s*(?:CU_START\s*:[^>\n]{0,80}|CU_START|CU_?END)\s*>>>", re.I)
PLACEHOLDER = re.compile(r"(?:TODO|TBD|待补充|这里填写|XXX)", re.I)
KU_ID = re.compile(r"KU-\d{4}(?:-[A-Z0-9]+)?", re.I)
ENGLISH = re.compile(r"\b[A-Za-z]{4,}\b")
TRUNCATED = re.compile(r"(?:清洁作业按|按照$|包括$|主要为$|如下[:：]?$)")
EMPTY_QUOTE = re.compile(r"[“‘][\s\u3000]*[”’]")
ADJACENT_QUOTE = re.compile(r"[”’][\s\u3000]*[“‘]")
AREA_VALUE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(万)?\s*(?:平方米|㎡)")
CORE_AREA_VALUE = re.compile(
    r"(?:总建筑面积|建筑面积合计|项目(?:总)?面积|物业服务面积|管理面积|总面积)\s*(?:为|约|共计|合计|[:：])?\s*"
    r"(?<!\d)(\d+(?:\.\d+)?)\s*(万)?\s*(?:平方米|㎡)",
    re.I,
)


def normalize_quotes(text: str) -> str:
    """Remove empty/orphan quote marks without altering valid quoted phrases."""
    value = str(text or "")
    previous = None
    while value != previous:
        previous = value
        value = EMPTY_QUOTE.sub("", value)
        value = ADJACENT_QUOTE.sub("", value)
    for opening, closing in (("“", "”"), ("‘", "’")):
        stack: list[int] = []
        unmatched_closing: list[int] = []
        for index, char in enumerate(value):
            if char == opening:
                stack.append(index)
            elif char == closing:
                if stack:
                    stack.pop()
                else:
                    unmatched_closing.append(index)
        remove = set(stack + unmatched_closing)
        if remove:
            value = "".join(char for index, char in enumerate(value) if index not in remove)
    return value


def quote_issues(text: str) -> list[str]:
    issues: list[str] = []
    if EMPTY_QUOTE.search(text):
        issues.append("empty quote pair")
    if ADJACENT_QUOTE.search(text):
        issues.append("adjacent quote boundary")
    if text.count('"') % 2 or text.count("“") != text.count("”") or text.count("‘") != text.count("’"):
        issues.append("unbalanced quotes")
    return issues


def _area_number(value: str) -> float | None:
    match = AREA_VALUE.search(str(value or ""))
    if not match:
        return None
    number = float(match.group(1)) * (10000 if match.group(2) else 1)
    return round(number, 2)


def _source_area_numbers(source: dict[str, Any]) -> set[float]:
    """Collect area values explicitly present in confirmed source facts.

    A project can legitimately contain both a project-wide total and sub-building
    totals. The former check compared every phrase containing ``总建筑面积`` only
    with the project-wide value, so a confirmed sub-building fact was permanently
    misclassified as drift.
    """
    values: set[float] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                collect(child)
            return
        if isinstance(value, list):
            for child in value:
                collect(child)
            return
        if not isinstance(value, str):
            return
        for match in AREA_VALUE.finditer(value):
            number = float(match.group(1)) * (10000 if match.group(2) else 1)
            values.add(round(number, 2))

    # These collections are assembled from Todd-confirmed requirements by the
    # planning layer. Deliberately do not scan arbitrary generated/runtime data.
    for key in ("client_requirements", "confirmed_requirements", "source_trace"):
        collect(source.get(key))
    return values


def project_fact_conflicts(texts: list[str], canonical_facts: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    expected: set[float] = set()
    source = canonical_facts or {}
    project_facts = source.get("project_facts") if isinstance(source, dict) else None
    if isinstance(project_facts, dict):
        for key in ("gross_area", "managed_area"):
            row = project_facts.get(key)
            raw = row.get("value") if isinstance(row, dict) else row
            parsed = _area_number(str(raw or ""))
            if parsed is not None:
                expected.add(parsed)
    for key in ("area", "gross_area", "managed_area"):
        raw = source.get(key) if isinstance(source, dict) else None
        parsed = _area_number(str(raw or ""))
        if parsed is not None:
            expected.add(parsed)
    if isinstance(source, dict):
        expected.update(_source_area_numbers(source))
    observed: set[float] = set()
    for text in texts:
        for match in CORE_AREA_VALUE.finditer(str(text or "")):
            number = float(match.group(1)) * (10000 if match.group(2) else 1)
            observed.add(round(number, 2))
    if expected:
        unexpected = sorted(value for value in observed if value not in expected)
        return [{"expected": sorted(expected), "observed": sorted(observed), "unexpected": unexpected}] if unexpected else []
    return [{"expected": [], "observed": sorted(observed), "unexpected": sorted(observed)}] if len(observed) > 1 else []


def _english_action(token: str) -> tuple[str, str]:
    low = token.lower()
    if low in SAFE_MAP:
        return "AUTO_REPAIR", SAFE_MAP[low]
    if low in ALLOWED_ENGLISH:
        return "PASS", token
    return "AUTO_REPAIR", ""


def flatten_structured_text(value: Any) -> list[str]:
    if value is None or value is False:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if JSON_DEBRIS.search(text):
            parsed = None
            try:
                parsed = ast.literal_eval(text)
            except (ValueError, SyntaxError, MemoryError):
                try:
                    parsed = json.loads(text)
                except (json.JSONDecodeError, TypeError, ValueError):
                    parsed = None
            if parsed is not None and parsed != text:
                return flatten_structured_text(parsed)
            text = re.sub(r"```json|```", "", text).strip()
        return [text] if text else []
    if isinstance(value, dict):
        lines: list[str] = []
        title = str(value.get("title") or value.get("heading") or "").strip()
        if title:
            lines.append(title)
        for key in ("items", "points", "content", "text", "paragraphs", "bullets"):
            child = value.get(key)
            if child is None or child == "" or child == title:
                continue
            lines.extend(flatten_structured_text(child))
        return lines
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(flatten_structured_text(item))
        return out
    text = str(value).strip()
    return [text] if text else []


def _walk_strings(value: Any, path: str = "artifact") -> list[tuple[str, str]]:
    if isinstance(value, dict) and value.get("profile_p_contract") == "profile-p-service-v1":
        from planning.ppt_service import visible_parts
        if "slides" in value:
            return [row for i, slide in enumerate(value["slides"]) for row in _walk_strings(slide, f"{path}.slides[{i}]")]
        return [(f"{path}.visible[{i}]", text) for i, text in enumerate(visible_parts(value))]
    # Structural renderer enums are backend instructions, not customer-visible copy.
    if isinstance(value, dict): return [x for k, v in value.items() if not is_structural_path(str(k))
        and not ('content_blocks' in value and k in {'paragraphs','bullets'}) for x in _walk_strings(v, f"{path}.{k}")]
    if isinstance(value, list): return [x for i, v in enumerate(value) for x in _walk_strings(v, f"{path}[{i}]")]
    return [(path, value)] if isinstance(value, str) else []


# §43：结构字段里的 "CH01 / S05-02 / CU-0012" 是后端寻址用的值，渲染器不会把它们
# 排进客户可见文本；标识符泄漏判定只看**正文位**，否则会批量产出假 BLOCK。
def _is_structural_key(path: str) -> bool:
    return is_structural_path(path)


def evaluate_artifact(generated: dict[str, Any], canonical_facts: dict[str, Any] | None = None) -> dict[str, Any]:
    if not FORMAL_RULES.get("rules"):
        raise ValueError("Formal Artifact QA rules are empty")
    findings: list[dict[str, Any]] = []
    seen_paragraphs: dict[str, str] = {}
    walked = _walk_strings(generated.get("artifact", generated))
    for path, text in walked:
        from .identifiers import malformed_email_findings
        findings.extend(malformed_email_findings(text, path))
        if KU_ID.search(text): findings.append({"rule_id": "FAQ-005", "path": path, "severity": "BLOCK", "issue": "KU ID leaked into formal body"})
        if TRUNCATED.search(text.strip()): findings.append({"rule_id": "FAQ-009", "path": path, "severity": "BLOCK", "issue": "truncated sentence"})
        if JSON_DEBRIS.search(text): findings.append({"rule_id": "FAQ-004", "path": path, "severity": "BLOCK", "issue": "JSON debris"})
        if TRANSPORT_RESIDUE.search(text): findings.append({"rule_id": "ENVELOPE_TRANSPORT_RESIDUE", "path": path, "severity": "BLOCK", "issue": "CU envelope transport marker leaked into customer artifact"})
        if INTERNAL.search(text) or INTERNAL_GOVERNANCE.search(text) or "不套用其他项目" in text: findings.append({"rule_id": "CLIENT_ARTIFACT_INTERNAL_META", "path": path, "severity": "AUTO_REPAIR", "issue": "internal governance language"})
        leaked_ids = [] if _is_structural_key(path) else sorted({str(hit.group(0)) for hit in INTERNAL_BODY_IDENTIFIER.finditer(text)})
        if leaked_ids:
            # 与语态泄漏同归一条规则：final_artifact_qa 对 CLIENT_ARTIFACT_INTERNAL_META
            # 的 BLOCK 提升因此同时覆盖"内部口径"和"内部标识符"两类元数据泄漏。
            findings.append({"rule_id": "CLIENT_ARTIFACT_INTERNAL_META", "path": path, "severity": "AUTO_REPAIR",
                             "issue": "internal identifier or governance metadata leaked into customer artifact",
                             "leaked_tokens": leaked_ids, "repair_action": "STRIP_INTERNAL_META"})
        if TENDER_TEMPLATE.search(text): findings.append({"rule_id": "TENDER_TEMPLATE_LEAK", "path": path, "severity": "BLOCK", "issue": "tender form/template leaked into customer artifact"})
        if PLACEHOLDER.search(text): findings.append({"rule_id": "FAQ-007", "path": path, "severity": "BLOCK", "issue": "placeholder"})
        from .identifiers import EMAIL
        for token in ENGLISH.findall(EMAIL.sub('', text)):
            severity, _ = _english_action(token)
            if severity == "AUTO_REPAIR":
                findings.append({"rule_id": "FAQ-002", "path": path, "severity": "AUTO_REPAIR", "issue": f"unknown English token: {token}"})
        if "本方案不预填" in text: findings.append({"rule_id": "FAQ-005", "path": path, "severity": "AUTO_REPAIR", "issue": "reverse governance phrasing"})
        if has_conditional_postprocess_pollution(text):
            findings.append({"rule_id": "CONDITIONAL_POSTPROCESS_POLLUTION", "path": path, "severity": "AUTO_REPAIR", "issue": "conditionalization post-process polluted customer text"})
        findings.extend(hygiene_findings(text, path, severity="AUTO_REPAIR"))
        # MMF008A-C：内部推理句。修复端在 `rewrite_customer_hygiene` 里（`apply_hygiene_tree`
        # 会走到），所以检测必须与它同一条尺子：结构化寻址值（section_id / content_unit_id …）
        # 不参与——上一轮就是因为把后端枚举值一起洗成中文，装配与覆盖找回全部静默落空。
        if not _is_structural_key(path):
            findings.extend(internal_narration_findings(text, path, severity="AUTO_REPAIR"))
        norm = re.sub(r"\s+", "", text)
        if len(norm) >= 18 and norm in seen_paragraphs: findings.append({"rule_id": "FAQ-008", "path": path, "severity": "AUTO_REPAIR", "issue": f"duplicate of {seen_paragraphs[norm]}"})
        else: seen_paragraphs[norm] = path
        for issue in quote_issues(text):
            findings.append({"rule_id": "QUOTE_BALANCE_QA", "path": path, "severity": "BLOCK", "issue": issue})
    for conflict in project_fact_conflicts([text for _path, text in walked], canonical_facts):
        findings.append({"rule_id": "PROJECT_FACT_CONSISTENCY", "path": "artifact", "severity": "BLOCK", "issue": "conflicting project area", **conflict})
    status = "BLOCK" if any(x["severity"] == "BLOCK" for x in findings) else ("AUTO_REPAIR" if findings else "PASS")
    return {"schema_version": "formal-artifact-qa-v0.1", "status": status, "findings": findings, "summary": {"block_count": sum(x["severity"] == "BLOCK" for x in findings), "auto_repair_count": sum(x["severity"] == "AUTO_REPAIR" for x in findings)}}


def merge_longform_qa(formal: dict[str, Any], depth: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(formal or {})
    extra = list(merged.get("findings") or [])
    if depth:
        extra.extend(depth.get("findings") or [])
        merged["longform_depth"] = depth
        if depth.get("status") in {"BLOCK", "DEPTH_INCOMPLETE"}:
            merged["status"] = "BLOCK"
    merged["findings"] = extra
    merged["summary"] = {
        "block_count": sum(item.get("severity") == "BLOCK" for item in extra),
        "auto_repair_count": sum(item.get("severity") == "AUTO_REPAIR" for item in extra),
    }
    return merged


def apply_artifact_repairs(generated: dict[str, Any]) -> dict[str, Any]:
    repaired = copy.deepcopy(generated)
    def walk(value: Any, seen: set[str] | None = None, positional: bool = False) -> Any:
        if isinstance(value, dict):
            from .table_context import visit_table
            # Table context must retain the positional nonempty-cell fallback
            # used below, even when the context visitor handles the table.
            contextual = visit_table(value, lambda item: walk([item], positional=True)[0], is_structural_path)
            if contextual is not None:
                return contextual
            # 寻址值与渲染器枚举必须原样保留：它们不在 `final_assembly` 的客户可见基座
            # （title/body_blocks/tables/processes/callouts）里，而英文清洗器会把
            # "paragraph" / "TABLE" / "S03-01" 一律清空或换成中文归类词，fragment 从此
            # 找不回 CU 出处。豁免口径与检测端共用 `is_structural_path`。
            return {
                k: (v if is_structural_path(str(k)) else walk(v, seen, str(k) in {"rows", "columns", "headers"}))
                for k, v in value.items()
                if k not in {"provider_metadata"}
            }
        if isinstance(value, list):
            # 去重域 = 一个列表，不是整棵树。`apply_content_units` 会把同一批 CU payload
            # 同时写进渲染槽位（tables/processes）与审计镜像 `content_unit_fragments`，
            # 整棵树共享 `seen` 时"后走到的那份"被判成重复删掉 —— 合成 300 类实测
            # 每节的 CU 表格行与流程步骤在提交站被清空（S05-03：4/5 表 0 行、3/4 流程 0 步），
            # 文档级整篇再走一遍时还会跨节互删。重复段落的真实形态就是同一列表里的
            # 相邻同句，收窄到列表内不丢这个能力；跨节重复由 LF-ANTI-PADDING 判，不靠删。
            scope: set[str] = set()
            out = []
            for item in value:
                fixed = walk(item, scope, positional)
                if positional and isinstance(item, str) and item.strip() and isinstance(fixed, str) and not fixed.strip():
                    fixed = item  # Preserve source text; final QA adjudicates narration.
                key = re.sub(r"\s+", "", fixed) if isinstance(fixed, str) else ""
                if not positional and key and len(key) >= 18 and key in scope: continue
                if key: scope.add(key)
                out.append(fixed)
            return out
        if isinstance(value, str):
            value = TRANSPORT_RESIDUE.sub("", value)
            if JSON_DEBRIS.search(value):
                flattened = flatten_structured_text(value)
                value = "\n".join(flattened) if flattened else value
            for old, new in SAFE_MAP.items(): value = re.sub(re.escape(old), new, value, flags=re.I)
            value = re.sub(r"[^。；！]*不套用其他项目[^。；！]*[。；！]?", "", value)
            value = INTERNAL.sub("", value)
            def _replace_english(match: re.Match[str]) -> str:
                _, replacement = _english_action(match.group(0))
                return replacement
            # Email address tokens are identifiers, not untranslated prose.
            # Replace only outside their complete spans, including domain names.
            from .identifiers import preserve_email_substitution
            value = preserve_email_substitution(value, lambda part: ENGLISH.sub(_replace_english, part))
            value = rewrite_unconfirmed_durations(value)
            value = apply_hygiene_tree(value)
            value = re.sub(r" +", " ", value)
            value = re.sub(r"\s+([，。；：、）】])", r"\1", value)
            value = normalize_quotes(value)
            natural_replacements = {
                "本方案按照条件性方法表述，不升级为既定承诺": "相关措施结合项目实际条件实施",
                "未确认事项一律不作为承诺": "相关事项以双方确认的项目要求为准",
                "本方案不预设": "具体安排结合项目实际确定",
                "项目事实、条件性方法与待澄清事项的边界": "项目条件、实施建议与待确认事项的适用范围",
                "待澄清事项、风险与承诺边界": "待确认事项、风险及服务责任范围",
                "明确项目事实、承诺与条件性方法的区分": "明确项目条件、服务责任与实施建议的适用范围",
                "承诺边界清单": "服务责任与实施条件清单",
                "事实边界": "项目条件适用范围",
                "条件性方法": "结合实际条件的实施建议",
                "承诺边界": "服务责任与实施条件",
            }
            for old, new in natural_replacements.items():
                value = value.replace(old, new)
            value = re.sub(r"确保([^。；]{0,40})零故障运行", r"保障\1稳定运行并降低故障发生概率", value)
            return value.strip()
        return value
    if isinstance(repaired, dict) and "artifact" in repaired:
        repaired["artifact"] = walk(repaired["artifact"])
        return repaired
    return walk(repaired)


def evaluate_ppt_artifact(generated: dict[str, Any], canonical_facts: dict[str, Any] | None = None, expected_slides: int | None = None) -> dict[str, Any]:
    """PPT 媒介的最终内容 QA：正式文本规则（媒介无关）+ 幻灯片结构检查。

    不套用 Word 长文深度规则（LF 系列）——那是 Word 文档的量尺。
    """
    formal = evaluate_artifact(generated, canonical_facts=canonical_facts)
    structural_path = re.compile(r"\.(type|component_intent|slide_id|visual_data|speaker_note_optional)(\.|$)")
    findings: list[dict[str, Any]] = [
        f for f in (formal.get("findings") or [])
        if not structural_path.search(str(f.get("path") or ""))
    ]
    artifact = generated.get("artifact") or generated
    if artifact.get("profile_p_contract") == "profile-p-service-v1":
        from planning.ppt_service import issues
        for index, slide in enumerate(artifact.get("slides") or []):
            findings.extend({**f, "path": f"artifact.slides[{index}]"} for f in issues(slide))
    slides = artifact.get("slides") if isinstance(artifact, dict) else None
    if not isinstance(slides, list) or not slides:
        findings.append({"rule_id": "PPT-001", "path": "artifact.slides", "severity": "BLOCK", "issue": "PPT内容缺少幻灯片列表"})
        slides = []
    else:
        for index, slide in enumerate(slides, 1):
            path = f"artifact.slides[{index - 1}]"
            if not isinstance(slide, dict):
                findings.append({"rule_id": "PPT-002", "path": path, "severity": "BLOCK", "issue": "幻灯片结构无效（非对象）"})
                continue
            # 实际渲染契约：title/core_message/bullets（build_ppt.mjs 消费）；headline/content_blocks 为规划契约的兼容写法。
            headline = str(slide.get("title") or slide.get("headline") or "").strip()
            if not headline:
                findings.append({"rule_id": "PPT-003", "path": f"{path}.title", "severity": "BLOCK", "issue": f"第{index}页缺少标题"})
            bullets = slide.get("bullets")
            blocks = slide.get("content_blocks")
            core_message = str(slide.get("core_message") or slide.get("key_message") or "").strip()
            has_content = core_message or (isinstance(bullets, list) and any(str(b).strip() for b in bullets)) or (isinstance(blocks, list) and bool(blocks))
            if not has_content:
                findings.append({"rule_id": "PPT-004", "path": path, "severity": "BLOCK", "issue": f"第{index}页缺少核心观点与内容要点"})
        if expected_slides and len(slides) != expected_slides:
            findings.append({"rule_id": "PPT-005", "path": "artifact.slides", "severity": "WARNING", "issue": f"幻灯片数量与规划不一致：生成{len(slides)}页，规划{expected_slides}页"})
    scoring_re = re.compile(r"评分标准|评分项|评分细则|分值|评标办法|商务及报价")
    for slide_index, slide in enumerate(slides, 1):
        for path, value in _walk_strings(slide, f"artifact.slides[{slide_index - 1}]"):
            if scoring_re.search(str(value)):
                findings.append({"rule_id": "PPT-006", "path": path, "severity": "BLOCK", "issue": "评分/招标信息出现在汇报PPT中"})
    status = "BLOCK" if any(x["severity"] == "BLOCK" for x in findings) else ("AUTO_REPAIR" if any(x["severity"] == "AUTO_REPAIR" for x in findings) else ("WARNING" if findings else "PASS"))
    return {
        "schema_version": "ppt-artifact-qa-v0.1",
        "status": status,
        "findings": findings,
        "summary": {
            "slide_count": len(slides),
            "block_count": sum(x["severity"] == "BLOCK" for x in findings),
            "auto_repair_count": sum(x["severity"] == "AUTO_REPAIR" for x in findings),
            "warning_count": sum(x["severity"] == "WARNING" for x in findings),
        },
    }
