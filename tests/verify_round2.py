"""Synthetic checks for scope, metrics, tables, the writing cap, and stall gates.

These samples are not a real tender rerun. A sourced metric stays verbatim.
A neighbouring number, object, or area does not borrow that source.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

from governance.final_editorial import SourceFacts
from governance.proposal_fidelity import (
    display_cell,
    filter_exclusion_tables,
    place_content_blocks,
    reconcile_excluded_scope,
)
from governance.table_fidelity import coerce_table
from longform.local_normalize import normalize_table


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        raise SystemExit(1)


def facts_from(lines):
    item = SourceFacts()
    item.rows = [
        {"text": line, "locator": f"sample:{index}", "heading_path": ["样例"]}
        for index, line in enumerate(lines, start=1)
    ]
    return item


def test_scope():
    excluded = [
        {"requirement_id": "REQ-0002", "text": "明确排除地下车库保洁"},
    ]
    kept, conflicts = reconcile_excluded_scope(excluded, "包含大堂保洁")
    check("scope keeps garage exclusion", kept == excluded and conflicts == [])
    section = {
        "tables": [{
            "columns": ["对象", "安排"],
            "rows": [
                ["大堂保洁", "每日循环"],
                ["地下车库", "目视积油并拖擦"],
                ["地下车库保洁", "不纳入本次范围"],
            ],
        }],
        "content_blocks": [],
    }
    filtered = filter_exclusion_tables(section, "包含大堂保洁", "明确排除地下车库保洁")
    rows = filtered["tables"][0]["rows"]
    texts = [" ".join(row) for row in rows]
    check("scope keeps lobby and the exclusion row", any("大堂保洁" in item for item in texts) and any("不纳入" in item for item in texts))
    check("scope drops the garage procedure", all("拖擦" not in item for item in texts))


def test_metrics():
    sourced = facts_from([
        "监控室实行24小时值守",
        "火灾后14小时内通知",
        "会议服务安排20人",
        "响应时限不少于4小时，低于时整改",
    ])
    check("14 hours does not prove 4 hours", not sourced.supports_metric("4小时", "4小时内通知"))
    check("catering hours do not prove the control room", not facts_from(["餐饮24小时供应"]).supports_metric("24小时", "监控室实行24小时值守"))
    check("120 people do not prove 20 people", not facts_from(["会议可容纳120人"]).supports_metric("20人", "会议服务安排20人"))
    check("4.4 hours do not prove 4 hours", not facts_from(["巡查历时4.4小时"]).supports_metric("4小时", "故障响应4小时"))
    check("inspection rate does not prove complaint closure", not facts_from(["检查合格率达到98%"]).supports_metric("98%", "投诉办结率达到98%"))
    check("sourced control-room hours stay", sourced.supports_metric("24小时", "监控室实行24小时值守"))
    binding = sourced.numeric_binding("响应时限不少于4小时，低于时整改", 6, 10)
    # "不少于4小时" — locate the 4小时 span dynamically
    text = "响应时限不少于4小时，低于时整改"
    start = text.index("4小时")
    binding = sourced.numeric_binding(text, start, start + len("4小时"))
    check("qualifier stays on a sourced metric", bool(binding) and "不少于" in str(binding.get("value")))


def test_tables():
    table = {"columns": ["事项", "方法"], "rows": [["核验", "留痕"]]}
    coerced = coerce_table(table)
    normalized, _report = normalize_table(table)
    check("two-column table matches at both entries", coerced["columns"] == normalized["columns"] and coerced["rows"] == normalized["rows"])
    ambiguous = {"columns": 3, "headers": ["甲", "乙"], "rows": ["1", "2", "3", "4"]}
    held = coerce_table(ambiguous)
    check("ambiguous columns keep the raw cells", "列数与表头不一致" in held.get("table_review_reason", "") and held.get("needs_review") and held["columns"] == 3 and held["headers"] == ["甲", "乙"] and held["rows"] == ["1", "2", "3", "4"])
    check("signature stays blank", display_cell("", header=False, column_name="签名") == "")
    check("measured value stays blank", display_cell("", header=False, column_name="实测值") == "")
    blocks = place_content_blocks(
        [{"type": "paragraph", "content": "保洁频次见下表。"}, {"type": "paragraph", "content": "秩序安排见下表。"}],
        [{"title": "秩序安排", "columns": ["事项", "方法"], "rows": [["巡查", "记录"]]},
         {"title": "保洁频次", "columns": ["事项", "方法"], "rows": [["大厅", "每日"]]}],
    )
    first_table = next(block["table"] for block in blocks if block.get("type") == "table")
    check("lead-in binds the named table", first_table["title"] == "保洁频次")
    from docx import Document
    document = Document()
    drawn = document.add_table(rows=2, cols=2)
    values = [coerced["columns"], *coerced["rows"]]
    for r, row in enumerate(values):
        for c, cell in enumerate(row):
            drawn.rows[r].cells[c].text = "" if c == 1 and r == 1 and False else str(cell)
    drawn.rows[1].cells[1].text = display_cell("", header=False, column_name="签名")
    out = Path(tempfile.mkdtemp()) / "table.docx"
    document.save(out)
    opened = Document(out)
    got = [[cell.text for cell in row.cells] for row in opened.tables[0].rows]
    check("docx cells match the shared table", got[0] == ["事项", "方法"] and got[1][0] == "核验" and got[1][1] == "")


def test_budget():
    root = Path(tempfile.mkdtemp())
    run = root / "runs" / "tender_budget_sample"
    run.mkdir(parents=True)
    (run / "brief.json").write_text(json.dumps({
        "medium": "WORD",
        "provider_name": "cursor_grok_4_6",
        "document_scale": "detailed_technical_solution",
    }), encoding="utf-8")
    jobs = root / "runtime" / "cursor"
    jobs.mkdir(parents=True)
    (jobs / "model-sample.json").write_text(json.dumps({"model": "grok-4.6"}), encoding="utf-8")
    os.environ["MMF_RUNS_DIR"] = str(root / "runs")
    import paths
    paths.reload()
    from account_budget import word_cap
    check("model cache does not collapse the writing cap", word_cap(jobs, "tender_budget_sample", "grok-4.6") == 64)


def test_stall_outcomes():
    from providers.cursor_account import terminal_word_interruption
    root = Path(tempfile.mkdtemp())
    shared = {
        "agent_id": "agent-sample",
        "run_id": "sdk-sample",
        "workspace": str(root),
        "requested_model": "grok-4.6",
    }

    def write(name, terminal):
        job = root / name
        job.mkdir()
        rows = [{"state": "running", **shared}, terminal]
        (job / "bridge.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        return job

    finished = write("retry-ok", {"state": "finished", "terminal_status": "ok", **shared})
    exhausted = write("retry-spent", {
        "state": "incomplete", "terminal_status": "error", "error_code": "connection_stalled", **shared,
    })
    stopped = write("user-stop", {
        "state": "incomplete", "terminal_status": "error", "error_code": "CURSOR_CANCELLED", **shared,
    })
    check("retry success clears the stall", terminal_word_interruption(finished, "grok-4.6") is None)
    stalled = terminal_word_interruption(exhausted, "grok-4.6")
    check("exhausted stall keeps the original interruption", bool(stalled) and stalled.get("error_code") == "connection_stalled")
    check("user stop is not treated as a stall", terminal_word_interruption(stopped, "grok-4.6") is None)
    bridge = (APP / "providers" / "cursor_bridge.mjs").read_text(encoding="utf-8")
    check("sdk stall retry stays enabled", "enableAgentRetries: true" in bridge)


if __name__ == "__main__":
    test_scope()
    test_metrics()
    test_tables()
    test_budget()
    test_stall_outcomes()
    print("ALL PASS")
