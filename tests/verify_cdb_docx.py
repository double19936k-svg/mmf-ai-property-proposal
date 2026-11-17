"""Acceptance gate for the tender-fidelity repairs.

The project facts below are the regression sample only. They are not production
constants. The script builds a docx through the same sanitizer, exclusion filter,
block order, and Word renderer the product uses, then applies the six checks.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from build_docx import main as build_docx_main
from governance.final_editorial import SourceFacts
from governance.proposal_fidelity import filter_exclusion_tables, place_content_blocks
from longform.factory import _repair_customer_text


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_FACTS = [
    "服务地点为济南市历下区奥体金融中心 D 栋。",
    "楼内办公人员约320人。",
    "满意度低于80%或90%时执行合同约定的处理。",
    "满意率不低于98%。",
    "接收设备故障4小时内通知相关部门。",
    "返修率不高于2%。",
    "监控室24小时值守。",
    "服务内容包括会议会务、餐饮、绿化、保洁、安保、工程、护士、库房、档案。",
]
DIRTY = (
    "楼内办公人员约 320 人。满意度低于 80% 或 90% 时甲方有权处理。满意率 98%。"
    "接收设备故障 4 小时内通知。返修率 2%。监控室 24 小时值守。"
    "M9 人员有序撤离。护士负责体检。地点为奥体金融中心。消耗品使用威露士。"
    "入口配置雨伞袋。服务对象包括交流干部宿舍。扶梯列入台账。音控师与事务主管到岗。"
    "回民餐食单独加工。CU-相关专项安排-03清单处理确保本Section所载内容。"
    "标准作业流程_STEPS:〔1〕1. ，设备分类与建档。大专及及以上。"
    "涉及能源计量、设备运行指标等需要与基础物业对接的数据。"
    "如下图所示。低于合同约定的指标时甲方有权提前终止合同，低于合同约定的指标。"
)


def _blocks(document: Document):
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def _is_heading(paragraph: Paragraph, level: int) -> bool:
    name = str(paragraph.style.name if paragraph.style is not None else "")
    return name == f"Heading {level}"


def _is_lead(text: str) -> bool:
    return any(token in text for token in ("见下表", "按以下清单", "按以下九项", "如下：", "如下:"))


def _is_list(paragraph: Paragraph) -> bool:
    name = str(paragraph.style.name if paragraph.style is not None else "")
    text = paragraph.text.strip()
    return "List" in name or bool(__import__("re").match(r"^\d+\.\s+\S", text))


def _full_text(document: Document) -> str:
    parts = []
    for block in _blocks(document):
        if isinstance(block, Paragraph):
            parts.append(block.text)
        else:
            for row in block.rows:
                for cell in row.cells:
                    parts.append(cell.text)
    return "\n".join(parts)


def _chapter_one_tables(document: Document) -> list[Table]:
    tables = []
    seen_heading = False
    for block in _blocks(document):
        if isinstance(block, Paragraph) and _is_heading(block, 1):
            if seen_heading:
                break
            seen_heading = True
            continue
        if seen_heading and isinstance(block, Table):
            tables.append(block)
    return tables


def evaluate(path: Path) -> list[tuple[str, str, str]]:
    document = Document(str(path))
    text = _full_text(document)
    results = []

    forbidden = ("会务", "会议", "餐饮", "绿化", "保洁", "安保", "工程", "护士", "库房")
    hits = 0
    for table in _chapter_one_tables(document):
        for row in table.rows:
            row_text = " ".join(cell.text for cell in row.cells)
            if any(token in row_text for token in ("排除", "不在范围内", "不安排")):
                hits += sum(token in row_text for token in forbidden)
    results.append(("1.排除项不误伤在册服务", "命中数 == 0", "PASS" if hits == 0 else f"FAIL:{hits}"))

    headings = [block.text for block in _blocks(document) if isinstance(block, Paragraph) and (_is_heading(block, 1) or _is_heading(block, 2))]
    heading_text = "\n".join(headings)
    required = ("健康管理", "餐饮", "绿化", "礼宾会务", "库房", "档案", "其他委托事项", "项目建议")
    missing = [item for item in required if item not in heading_text]
    engineering = []
    capture = False
    for block in _blocks(document):
        if isinstance(block, Paragraph) and _is_heading(block, 1):
            capture = "工程" in block.text
            if capture:
                engineering.append(block.text)
            continue
        if capture and isinstance(block, Paragraph) and _is_heading(block, 1):
            break
        if capture:
            engineering.append(block.text if isinstance(block, Paragraph) else " ".join(cell.text for row in block.rows for cell in row.cells))
    engineering_text = "\n".join(engineering)
    engineering_ok = all(token in engineering_text for token in ("巡检", "维保", "故障闭环"))
    empty_heading = [item for item in headings if not item.strip()]
    gate2 = "PASS" if not missing and engineering_ok and not empty_heading else f"FAIL:missing={missing},engineering={engineering_ok}"
    results.append(("2.专项与工程维保成节", "标题覆盖且工程章含巡检维保故障闭环", gate2))

    keywords = ("护士", "体检", "奥体金融中心", "威露士", "雨伞袋", "交流干部宿舍", "扶梯", "音控师", "事务主管", "回民")
    absent = [item for item in keywords if text.count(item) < 1]
    results.append(("3.岗位与刚性细节召回", "每个关键词 >= 1", "PASS" if not absent else f"FAIL:{absent}"))

    dangling = 0
    stacked = 0
    run = 0
    body = list(_blocks(document))
    for index, block in enumerate(body):
        if not isinstance(block, Paragraph) or not _is_lead(block.text):
            run = 0
            continue
        run += 1
        stacked += 1 if run >= 3 else 0
        nxt = body[index + 1] if index + 1 < len(body) else None
        followed = isinstance(nxt, Table) or (isinstance(nxt, Paragraph) and _is_list(nxt))
        if not followed:
            dangling += 1
    empty_cells = 0
    for block in body:
        if isinstance(block, Table):
            for row in block.rows:
                for cell in row.cells:
                    if not cell.text.strip():
                        empty_cells += 1
    gate4 = "PASS" if dangling == 0 and stacked == 0 and empty_cells == 0 else f"FAIL:dangling={dangling},stacked={stacked},empty={empty_cells}"
    results.append(("4.导语与空单元格", "悬空、堆叠、空单元格均为 0", gate4))

    metrics = ("80%", "90%", "4小时", "2%", "98%", "24小时", "320人")
    metric_miss = [item for item in metrics if item not in text]
    broken = ("M经确认的", "约经确认的", "低于合同约定的指标时甲方有权提前终止合同，低于合同约定的指标")
    broken_hits = sum(text.count(item) for item in broken)
    gate5 = "PASS" if not metric_miss and broken_hits == 0 else f"FAIL:metrics={metric_miss},broken={broken_hits}"
    results.append(("5.硬指标保留", "硬指标都在且畸形替换为 0", gate5))

    markers = ("CU-", "Section", "_STEPS", "〔1〕", ":步骤", "：步骤", "1. ，", "2. ，", "大专及及")
    marker_hits = {item: text.count(item) for item in markers if text.count(item)}
    results.append(("6.内部标记", "命中数 == 0", "PASS" if not marker_hits else f"FAIL:{marker_hits}"))
    return results


def build_sample(target: Path) -> None:
    facts = SourceFacts({"paragraphs": [{"para_id": f"p{i}", "text": text} for i, text in enumerate(SAMPLE_FACTS)]})
    repaired = _repair_customer_text(DIRTY, facts)
    duty = {
        "title": "",
        "columns": ["事项", "标准", "记录"],
        "rows": [
            ["门厅巡回", "按巡回路线完成并留存记录", ""],
            ["会务接待", "按预约单引导来宾", "值班记录"],
            ["绿化浇灌", "按季节计划养护", "养护记录"],
        ],
    }
    blocks = place_content_blocks(
        [
            {"type": "paragraph", "content": repaired},
            {"type": "paragraph", "content": "岗位分工见下表："},
            {"type": "subheading", "content": "现场核对：步骤 1"},
            {"type": "numbered_steps", "items": ["，设备分类与建档。", "复核后关闭。"]},
        ],
        [duty],
    )
    exclusion = {
        "title": "",
        "columns": ["事项", "边界"],
        "rows": [
            ["地下车库", "不在范围内"],
            ["楼宇主体外围", "不在范围内"],
            ["外立面", "不在范围内"],
            ["会议会务", "不安排"],
            ["餐饮服务", "不在范围内"],
        ],
    }
    scope = filter_exclusion_tables(
        {
            "heading": "项目理解与服务范围",
            "level": 1,
            "paragraphs": [],
            "content_blocks": [{"type": "paragraph", "content": "下列事项为范围边界。"}, {"type": "table", "table": exclusion}],
            "tables": [],
        },
        "会议会务服务。餐饮服务。绿化养护。保洁。安保。工程。护士。库房。档案。",
    )
    sections = [
        scope,
        {"heading": "服务边界与岗位", "level": 2, "content_blocks": blocks, "tables": []},
        {"heading": "工程设施运行与维护管理", "level": 1, "content_blocks": [{"type": "paragraph", "content": "工程服务按台账执行。"}]},
        {"heading": "巡检、维保与故障闭环", "level": 2, "content_blocks": [{"type": "paragraph", "content": "巡检、维保与故障闭环按报修、复核和关闭记录执行。"}]},
        {"heading": "评分专项响应", "level": 1, "content_blocks": [{"type": "paragraph", "content": "评分事项分节响应。"}]},
    ]
    for title, sentence in (
        ("健康管理", "健康管理由护士承担体检与健康档案。"),
        ("餐饮服务", "餐饮按供餐标准执行。"),
        ("绿化养护", "绿化按养护周期执行。"),
        ("礼宾会务", "礼宾会务按预约执行。"),
        ("库房管理", "库房按台账收发。"),
        ("档案管理", "档案按目录保管。"),
        ("其他委托事项", "其他委托事项按委托单执行。"),
        ("项目建议", "项目建议单独成节。"),
    ):
        sections.append({"heading": title, "level": 2, "content_blocks": [{"type": "paragraph", "content": sentence}]})
    payload = {
        "brief": {"project_name": "回归样本", "scenario": "物业服务方案", "project_type": "物业服务"},
        "artifact": {
            "title": "回归样本方案",
            "sections": sections,
            "include_toc": False,
            "lead": [],
        },
    }
    folder = Path(tempfile.mkdtemp(prefix="cdb-verify-"))
    source = folder / "input.json"
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    sys.argv = ["build_docx.py", str(source), str(target)]
    build_docx_main()


def main() -> int:
    target = Path(tempfile.mkdtemp(prefix="cdb-docx-")) / "verify.docx"
    build_sample(target)
    results = evaluate(target)
    failed = False
    print(f"docx={target}")
    for name, rule, status in results:
        print(f"{status}\t{name}\t{rule}")
        failed = failed or not status.startswith("PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
