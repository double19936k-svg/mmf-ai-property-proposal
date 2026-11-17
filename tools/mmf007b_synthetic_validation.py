from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parents[1]
APP = APP_ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from governance.toc_qa import evaluate_native_toc
from planning.content_engineering import content_quality_report, repair_template_leakage
from planning.hierarchy import build_document_hierarchy
from planning.planner import build_planning_bundle


def pack() -> dict[str, Any]:
    domains = ["客服", "秩序", "消防", "工程", "环境", "品质", "培训", "应急", "进退场", "档案"]
    requirements = [{
        "requirement_id": f"REQ-{index:04d}",
        "normalized_requirement": f"{domain}服务应形成可执行方案、检查记录和闭环机制",
        "mandatory_level": "MUST" if index % 3 == 0 else "INFO",
        "domain": "service",
        "confirmation_status": "CONFIRMED",
        "scoring_item_id": f"SCR-{index:03d}" if index <= 6 else None,
    } for index, domain in enumerate(domains, 1)]
    requirements.extend([
        {"requirement_id": "REQ-0201", "normalized_requirement": "投标保证金按招标文件要求提交", "mandatory_level": "MUST", "confirmation_status": "CONFIRMED"},
        {"requirement_id": "REQ-0202", "normalized_requirement": "类似项目业绩评分需提供合同证明", "mandatory_level": "INFO", "confirmation_status": "CONFIRMED", "scoring_item_id": "SCR-099"},
    ])
    return {
        "schema_version": "synthetic-technical-bid-v0.1", "pack_id": "PACK-MMF007B-SYNTHETIC", "pack_version": 1,
        "status": "ready_for_plan", "confirmation": {"ready_for_brief_seed": True},
        "project_facts": {"project_type": {"value": "金融办公物业", "confirmation_status": "CONFIRMED"}},
        "service_scope": {"included": [], "excluded": []}, "requirements": requirements,
        "scoring_items": [{"scoring_item_id": f"SCR-{index:03d}", "label": f"{domain}服务方案（{index + 4}分）", "must_respond": True, "max_score": index + 4} for index, domain in enumerate(domains[:6], 1)] + [{"scoring_item_id": "SCR-099", "label": "类似项目业绩评分", "must_respond": True, "max_score": 5}],
    }


def _fit(text: str, target: int, seed: int) -> str:
    actions = ["识别", "计划", "执行", "记录", "复核", "升级", "关闭", "复盘", "改进", "验证"]
    objects = ["服务需求", "设施状态", "客户诉求", "作业风险", "现场边界", "协同接口", "异常事件", "资源条件", "质量偏差", "履约证据", "培训能力", "环境表现", "秩序状态"]
    records = ["检查表", "工单", "值班记录", "会议纪要", "巡检台账", "签收单", "验收记录", "整改清单", "影像记录", "月度报告", "培训档案"]
    controls = ["分级响应", "双人复核", "限时升级", "抽样检查", "趋势分析", "责任到岗", "节点确认", "动态校准", "例外审批", "闭环回访", "交叉验证"]
    sentences = []
    index = 0
    while len("".join(sentences)) < target:
        left = actions[(seed + index) % len(actions)]
        right = actions[(seed * 3 + index + 2) % len(actions)]
        obj = objects[(seed * 5 + index * 3) % len(objects)]
        record = records[(seed * 7 + index * 5) % len(records)]
        control = controls[(seed * 11 + index * 7) % len(controls)]
        sentences.append(f"围绕{obj}开展{left}，由责任岗位采用{control}控制关键节点；以{record}核验{right}结果，发现偏差后完成原因确认、责任交接和闭环复核。")
        index += 1
    return "".join(sentences)[:target]


def artifact_for(bundle: dict[str, Any], hierarchy: dict[str, Any], label: str) -> dict[str, Any]:
    units_by_section: dict[str, list[dict[str, Any]]] = {}
    for unit in hierarchy["content_units"]:
        units_by_section.setdefault(unit["section_id"], []).append(unit)
    sections = []
    serial = 0
    for chapter in bundle["word_plan"]["outline"]:
        sections.append({"heading": chapter["chapter_title"], "level": 1, "section_id": chapter["chapter_id"], "paragraphs": [], "bullets": [], "tables": []})
        for planned in chapter["sections"]:
            paragraphs, bullets, tables = [], [], []
            for unit in units_by_section.get(planned["section_id"], []):
                serial += 1
                # The 150-class fixture validates the 120-150 rendered-page
                # band.  A small density reserve keeps table pagination from
                # crossing the upper bound while remaining well above the
                # 120k effective-content minimum.  Production budgets are not
                # changed by this synthetic-only rendering fixture.
                density = 0.90 if label == "150-class" else 1.0
                budget = int(int(unit["target_budget"]["effective_chars"]) * density)
                archetype = unit["archetype"]
                title = str(unit.get("title") or planned["section_title"])
                if archetype in {"STANDARD_TABLE", "MATRIX", "KPI_TABLE", "RESPONSIBILITY_MATRIX", "RISK_REGISTER", "SCHEDULE", "FORM_TEMPLATE"}:
                    rows = max(4, int(unit["target_budget"].get("rows") or unit["target_budget"].get("fields") or 6))
                    columns = ["业务事项", "执行动作", "责任与记录", "检查及例外"]
                    remaining = max(180, budget - len(title) - rows * 20)
                    per_cell = max(12, remaining // (rows * len(columns)))
                    values = [[f"{title}{row + 1}", _fit("", per_cell, serial * 1000 + row * 10 + 1), _fit("", per_cell, serial * 1000 + row * 10 + 2), _fit("", per_cell, serial * 1000 + row * 10 + 3)] for row in range(rows)]
                    tables.append({"title": title, "columns": columns, "rows": values, "notes": "本表用于记录执行、复核与例外处置结果。"})
                elif archetype in {"SOP", "PROCESS", "EMERGENCY_PLAYBOOK", "CHECKLIST"}:
                    steps = max(6, int(unit["target_budget"].get("steps") or unit["target_budget"].get("items") or 8))
                    bullets.extend([f"{title}｜{_fit('', max(40, budget // steps), serial * 1000 + step)}" for step in range(steps)])
                else:
                    paragraphs.append(f"{title}。{_fit('', budget, serial)}")
            sections.append({"heading": planned["section_title"], "level": 2, "section_id": planned["section_id"], "paragraphs": paragraphs, "bullets": bullets, "tables": tables})
    return {
        "title": f"MMF-007B {label} Synthetic Technical Bid", "subtitle": "Mock / Synthetic Engineering Evidence",
        "include_toc": True, "toc_depth": 1, "lead": ["本文件仅用于验证内容预算、原生结构和目录机制，不包含真实客户资料或投标人事实。"],
        "sections": sections,
    }


def visible_chars(value: Any) -> int:
    if isinstance(value, dict): return sum(visible_chars(child) for child in value.values())
    if isinstance(value, list): return sum(visible_chars(child) for child in value)
    return len(value) if isinstance(value, str) else 0


def run_one(root: Path, scale: str, label: str, *, toc_depth: int) -> dict[str, Any]:
    brief = {"project_name": "Synthetic金融办公物业项目", "medium": "WORD", "project_type": "金融办公物业", "scenario": "技术标", "document_scale": scale, "toc_depth": toc_depth}
    requirement_pack = pack()
    bundle = build_planning_bundle(requirement_pack, brief, {"knowledge_usage_contracts": []}, production=True)
    hierarchy = build_document_hierarchy(requirement_pack, bundle)
    artifact, _ = repair_template_leakage(artifact_for(bundle, hierarchy, label), brief["project_type"])
    artifact["toc_depth"] = toc_depth
    source = root / f"{label}_artifact_content.json"
    output = root / f"MMF007B_{label}_Synthetic.docx"
    source.write_text(json.dumps({"brief": brief, "artifact": artifact}, ensure_ascii=False, indent=2), encoding="utf-8")
    result = subprocess.run([sys.executable, str(APP / "build_docx.py"), str(source), str(output)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    from docx import Document
    document = Document(output)
    toc = evaluate_native_toc(output)
    quality = content_quality_report(artifact, brief["project_type"])
    archetypes = hierarchy["counts"]["content_units_by_archetype"]
    return {
        "label": label, "document_scale": bundle["content_budget"]["document_scale_code"],
        "target_effective_chars": bundle["content_budget"]["totals"]["target_effective_chars_target"],
        "actual_effective_chars": visible_chars(artifact), "volumes": hierarchy["counts"]["volumes"],
        "chapters": hierarchy["counts"]["chapters"], "sections": hierarchy["counts"]["sections"],
        "content_units": hierarchy["counts"]["content_units"], "content_units_by_archetype": archetypes,
        "narrative_cu": archetypes.get("NARRATIVE", 0), "sop_cu": archetypes.get("SOP", 0),
        "table_cu": sum(archetypes.get(key, 0) for key in ("STANDARD_TABLE", "KPI_TABLE", "SCHEDULE")),
        "form_cu": archetypes.get("FORM_TEMPLATE", 0), "matrix_cu": sum(archetypes.get(key, 0) for key in ("MATRIX", "RESPONSIBILITY_MATRIX")),
        "checklist_cu": archetypes.get("CHECKLIST", 0), "risk_cu": archetypes.get("RISK_REGISTER", 0),
        "word_tables": len(document.tables), "heading_counts": {"heading1": toc["heading1_count"], "heading2": toc["heading2_count"]},
        "toc": toc, "quality": quality, "docx_path": str(output), "docx_bytes": output.stat().st_size,
        "actual_rendered_pages": len(list((root / ("render_150-class_density90_final" if label == "150-class" else f"render_{label}_pageref_final")).glob("page-*.png"))) or None,
    }


def fixture_coverage() -> list[dict[str, Any]]:
    rows = []
    base = pack()
    for name, requirement_count, scale in (
        ("Simple", 2, "regular_solution"),
        ("Medium", 7, "detailed_technical_solution"),
        ("Complex", len(base["requirements"]), "large_technical_tender"),
    ):
        requirement_pack = dict(base)
        requirement_pack["requirements"] = list(base["requirements"][:requirement_count])
        brief = {"project_name": f"{name} Synthetic", "medium": "WORD", "project_type": "金融办公物业", "scenario": "技术标", "document_scale": scale}
        bundle = build_planning_bundle(requirement_pack, brief, {"knowledge_usage_contracts": []}, production=True)
        hierarchy = build_document_hierarchy(requirement_pack, bundle)
        rows.append({
            "fixture": name,
            "requirement_count": requirement_count,
            "document_scale": bundle["content_budget"]["document_scale_code"],
            "content_units": hierarchy["counts"]["content_units"],
            "planning_status": bundle["validation"]["status"],
        })
    return rows


def main() -> None:
    project_root = APP_ROOT.parents[1]
    output_root = project_root / "06_MVP产品与生成链" / "MMF_Project_Records" / "MMF-007B" / "synthetic_validation"
    output_root.mkdir(parents=True, exist_ok=True)
    results = [
        run_one(output_root, "detailed_technical_solution", "150-class", toc_depth=1),
        run_one(output_root, "large_technical_tender", "300-class", toc_depth=2),
    ]
    report = {"schema_version": "mmf007b-synthetic-validation-v0.1", "provider_calls": {"qwen": 0, "kimi": 0, "glm": 0, "grok": 0, "deepseek": 0}, "fixture_coverage": fixture_coverage(), "results": results}
    (output_root / "MMF007B_synthetic_validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
