"""Offline acceptance for the saved Yinfeng ledger, a synthetic pack, and a rendered docx.

This does not click resume, call a model, or rewrite the saved run.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

APP = Path(__file__).resolve().parents[1] / "app"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(APP))
LIVE = Path(r"D:\YourProject")
YINFENG = "tender_20261111_134011_73d903"


def use_roots(**env):
    import paths
    saved = {key: os.environ.get(key) for key in env}
    for key, value in env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    paths.reload()
    return saved


def restore_roots(saved):
    import paths
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    paths.reload()


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        raise SystemExit(1)
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        raise SystemExit(1)


def test_yinfeng_resume():
    from account_delivery import project
    from cursor_word_recovery import inspect, public_issue, settle

    run = LIVE / "runs" / YINFENG
    ledger = run / "provider_unknown_requests.jsonl"
    before = ledger.read_bytes()
    policy = LIVE / "runtime" / "cursor_account" / __import__("hashlib").sha256(YINFENG.encode()).hexdigest()[:24] / ".word_policy"
    had_policy = policy.is_file()
    saved = use_roots(
        MMF_DATA_ROOT=str(LIVE),
        MMF_RUNS_DIR=str(LIVE / "runs"),
        MMF_RUNTIME_DIR=str(LIVE / "runtime"),
        MMF_OUTPUT_ROOT=str(LIVE / "output"),
    )
    try:
        snapshot = inspect(run)
        check("yinfeng resume is offered", bool(snapshot and snapshot.get("can_resume") and snapshot.get("reason") == "budget_not_dispatched"))
        check("yinfeng keeps the eight dispatches inside the cap", snapshot["dispatched"] == 8 and snapshot["sent"] == 8 and snapshot["returned"] == 8 and snapshot["call_cap"] == 64)
        check("yinfeng resume does not rewrite the ledger", snapshot["settlements"] == [])
        settle(snapshot)
        issue = public_issue(snapshot)
        check("yinfeng resume names the original model and quota", "原模型" in issue and "额度" in issue)
        units = ["S01-01", "S01-02", "S02-01", "S02-02", "S03-01", "S03-02"]
        check("yinfeng six sections remain reusable", all((run / "longform" / "word" / "sections" / unit / "fragment.json").is_file() for unit in units))
        shown = project({"run_id": YINFENG, "provider_name": "cursor_grok_4_6", "project_name": "银丰", "scenario": "详细技术方案", "medium": "WORD"})
        check("result and history receive the resume flag", shown.get("can_resume_cursor_word") is True)
        page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        check("result and history both render the resume button", page.count("继续生成未完成正文") >= 2 and "resume-cursor-word" in page and "resume_cursor_word:true" in page)
    finally:
        restore_roots(saved)
    check("yinfeng ledger and policy stay untouched", ledger.read_bytes() == before and policy.is_file() == had_policy)


def test_budget_gate_once():
    from providers.cursor_account import CursorAccountProvider, slot_key

    root = Path(tempfile.mkdtemp())
    run_id = "budget-sample"
    run = root / "runs" / run_id
    run.mkdir(parents=True)
    (run / "brief.json").write_text(json.dumps({
        "medium": "WORD", "provider_name": "cursor_grok_4_6", "document_scale": "detailed_technical_solution",
    }), encoding="utf-8")
    saved = use_roots(
        MMF_DATA_ROOT=str(root),
        MMF_RUNS_DIR=str(root / "runs"),
        MMF_RUNTIME_DIR=str(root / "runtime"),
        MMF_CURSOR_BRIDGE_ROOT=str(root / "bridge"),
    )
    (root / "bridge").mkdir()
    calls = []

    def bridge(_runtime, _request, _run_id):
        calls.append(1)
        return {"state": "finished", "observed_model": "grok-4.6", "run_id": "simulated", "result": '{"ok": true}'}

    import providers.cursor_account as cursor_account
    old_bridge, old_health = cursor_account.run_bridge, cursor_account.bridge_health
    cursor_account.run_bridge = bridge
    cursor_account.bridge_health = lambda model=None: (True, "simulated")
    try:
        slot = root / "runtime" / "cursor_account" / __import__("hashlib").sha256(run_id.encode()).hexdigest()[:24]
        for index in range(8):
            job = slot / slot_key(f"existing-{index}")
            job.mkdir(parents=True)
            (job / "dispatch.json").write_text(json.dumps({"scope": "generation", "model": "grok-4.6", "digest": f"old-{index}"}), encoding="utf-8")
            (job / "reply.json").write_text("{}", encoding="utf-8")
        provider = CursorAccountProvider({
            "provider_name": "cursor_grok_4_6", "provider_type": "cursor_account", "model": "grok-4.6", "timeout": 30,
        })
        request = {
            "run_id": run_id, "task_id": "next-section", "prompt": "只补缺失正文", "required_keys": ["ok"],
            "generation_mode": "longform_section",
        }
        first = provider.invoke_structured(request, root / "task-a")
        second = provider.invoke_structured(request, root / "task-b")
        check("next request passes the budget gate once", calls == [1] and first["ok"] is True and second == first)
        check("same request is not dispatched twice", len(list(slot.glob("*/dispatch.json"))) == 9)
    finally:
        cursor_account.run_bridge = old_bridge
        cursor_account.bridge_health = old_health
        restore_roots(saved)


def test_metrics():
    from governance.final_editorial import SourceFacts
    from governance.proposal_fidelity import mandatory_fact_tokens, recall_missing_facts

    rows = [
        {"text": "餐饮区实行24小时供应", "locator": "餐饮条款"},
        {"text": "监控室实行24小时值守", "locator": "监控室条款"},
        {"text": "巡查历时4.4小时", "locator": "巡查条款"},
        {"text": "会议可容纳120人", "locator": "会议条款"},
        {"text": "检查合格率达到98%", "locator": "检查条款"},
        {"text": "响应时限不少于4小时，低于时整改", "locator": "响应条款"},
    ]
    facts = SourceFacts()
    facts.rows = rows
    check("4.4 hours do not prove 4 hours", not facts.supports_metric("4小时", "故障响应4小时"))
    check("120 people do not prove 20 people", not facts.supports_metric("20人", "值守安排20人"))
    check("inspection rate does not prove complaint closure", not facts.supports_metric("98%", "投诉办结率达到98%"))
    text = "监控室实行24小时值守"
    start = text.index("24小时")
    binding = facts.numeric_binding(text, start, start + len("24小时"))
    check("control room binds its own clause", binding and binding["source_text"] == "监控室实行24小时值守" and binding["locator"] == "监控室条款")
    sourced = "响应时限不少于4小时，低于时整改"
    start = sourced.index("4小时")
    kept = facts.numeric_binding(sourced, start, start + len("4小时"))
    check("sourced qualifier stays verbatim", kept and kept["value"] == "不少于4小时" and "低于时整改" in kept["source_text"] and kept["locator"] == "响应条款")
    sections = [{"level": 2, "paragraphs": ["正文尚未写响应时限。"], "content_blocks": []}]
    recall_missing_facts(sections, {"must_requirements": [
        {"text": "响应时限不少于4小时，低于时整改", "locator": "响应条款"},
        "4小时",
    ]})
    recalled = "\n".join(sections[0]["paragraphs"])
    check("recall keeps the clause and source", "响应时限不少于4小时，低于时整改" in recalled and "响应条款" in recalled)
    check("recall does not append a bare number", "4小时" not in mandatory_fact_tokens({"must_requirements": ["4小时"]}))


def test_scope_pack_and_docx():
    from build_docx import main
    from governance.proposal_fidelity import filter_exclusion_tables
    from tender_intake.pack_builder import build_requirement_pack, candidates_from_extraction

    def extraction(*texts):
        return {
            "schema_version": "tender-extraction-v0.1", "extraction_id": "EXT-SCOPE", "run_id": "scope_sample",
            "files": [{"file_id": "FILE-001", "original_filename": "scope.txt", "media_type": "txt", "sha256": "a" * 64, "page_count": 1}],
            "outline": [], "pages": [], "paragraphs": [
                {"file_id": "FILE-001", "para_id": f"P-{index}", "paragraph_index": index, "page_no": 1,
                 "heading_path": ["服务范围"], "outline_node_id": None, "text": text}
                for index, text in enumerate(texts)
            ],
            "tables": [], "warnings": [], "processing_mode": "txt",
        }

    quiet = extraction("本次服务纳入大堂保洁", "地下车库保洁不包含在本次物业服务范围", "楼内其他区域保洁按计划执行")
    pack = build_requirement_pack(quiet, candidates_from_extraction(quiet))
    included = " ".join(item["text"] for item in pack["service_scope"]["included"])
    excluded = " ".join(item["text"] for item in pack["service_scope"]["excluded"])
    check("pack keeps lobby included and garage excluded", "大堂保洁" in included and "地下车库保洁" in excluded)
    check("pack keeps both sources", any(item["sources"] for item in pack["service_scope"]["included"]) and any(item["sources"] for item in pack["service_scope"]["excluded"]))
    check("shared cleaning word is not a conflict", pack["scope_conflicts"] == [])
    clash = extraction("本次服务纳入地下车库保洁", "地下车库保洁不包含在本次物业服务范围")
    conflicted = build_requirement_pack(clash, candidates_from_extraction(clash))
    pair = conflicted["scope_conflicts"]
    check("same object records both texts and sources", len(pair) == 1 and "纳入地下车库保洁" in pair[0]["included_text"] and "不包含" in pair[0]["excluded_text"] and pair[0]["included_source"].get("source_excerpt") and pair[0]["excluded_source"].get("source_excerpt"))
    page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    check("confirmation and review can show the conflict", page.count("服务范围纳入与排除冲突") >= 2)

    section = {
        "heading": "服务范围", "level": 2,
        "paragraphs": ["每日对地下车库进行拖擦。", "地下车库与大堂的分界以交接为准。", "地下车库保洁不包含在本次物业服务范围。"],
        "bullets": ["大堂保洁每日循环", "地下车库保洁纳入巡查"],
        "content_blocks": [
            {"type": "numbered_steps", "items": ["对地下车库拖擦并吸尘", "大堂保洁完成后办理交接"]},
            {"type": "paragraph", "content": "地下车库保洁不包含在本次物业服务范围。"},
        ],
        "tables": [{
            "title": "服务安排",
            "columns": ["对象", "安排"],
            "rows": [["大堂保洁", "每日循环"], ["地下车库", "目视积油并拖擦"], ["地下车库保洁", "不包含在本次物业服务范围"]],
        }],
    }
    filtered = filter_exclusion_tables(section, "大堂保洁", "地下车库保洁不包含在本次物业服务范围")
    work = Path(tempfile.mkdtemp())
    payload = work / "input.json"
    payload.write_text(json.dumps({"brief": {"project_name": "合成项目", "scenario": "完整物业服务方案"}, "artifact": {
        "title": "合成验收", "include_toc": False, "sections": [filtered],
    }}, ensure_ascii=False), encoding="utf-8")
    output = work / "scope.docx"
    old_argv = sys.argv
    sys.argv = ["build_docx.py", str(payload), str(output)]
    try:
        main()
    finally:
        sys.argv = old_argv
    from docx import Document
    document = Document(output)
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    cells = [" ".join(cell.text for cell in row.cells) for table in document.tables for row in table.rows]
    check("docx keeps exclusion and handover", "不包含在本次物业服务范围" in text and "交接" in text and "大堂保洁" in text)
    check("docx blocks excluded execution", "拖擦" not in text and all("拖擦" not in row for row in cells) and "吸尘" not in text)


def test_tables_docx():
    from build_docx import main
    from governance.table_fidelity import coerce_table, matrix
    from governance.table_qa import form_template_table

    day = {
        "title": "日间交接", "columns": ["事项", "安排", "记录"],
        "rows": [["交接", "当面核对", "日间记录"], ["保洁", "循环完成", "日间闭环"], ["秩序", "岗位到齐", "日间签到"]],
    }
    night = {
        "title": "夜间异常处置", "columns": ["事项", "安排", "记录"],
        "rows": [["交接", "当面核对", "日间记录"], ["异常", "先隔离再上报", "夜间专记"], ["复核", "下一班确认", "夜间闭环"]],
    }
    header_form = {
        "title": "签到表", "columns": ["事项", "签名", "日期"],
        "rows": [["日间巡查记录已完成", "", ""]],
    }
    row_form = form_template_table({
        "fields": ["签名", "实测温度"],
        "storage_location": "档案室A柜",
        "保存期限": "合同期内留存",
    }, title="现场记录表")
    ambiguous = coerce_table({"columns": 3, "headers": ["甲", "乙"], "rows": ["1", "2", "3", "4"]})
    uneven = coerce_table({"columns": ["甲", "乙"], "rows": [["1", "2"], ["3"]]})
    broken = coerce_table({"columns": ["甲"], "rows": {"bad": True}})
    check("bad tables stay original and reviewable", ambiguous["needs_review"] and ambiguous["rows"] == ["1", "2", "3", "4"] and uneven["needs_review"] and uneven["rows"] == [["1", "2"], ["3"]] and broken["needs_review"] and broken["rows"] == {"bad": True})
    check("bad tables do not raise", "待复核" in matrix(ambiguous)[0][0] and "待复核" in matrix(broken)[0][0])
    check("sourced form notes stay and missing notes are explicit", "档案室A柜" in row_form["notes"] and "合同期内留存" in row_form["notes"] and "复核要求：待确认" in row_form["notes"] and "物业项目管理台账" not in row_form["notes"])
    work = Path(tempfile.mkdtemp())
    payload = work / "tables.json"
    payload.write_text(json.dumps({"brief": {"project_name": "合成项目", "scenario": "完整物业服务方案"}, "artifact": {
        "title": "表格验收", "include_toc": False,
        "sections": [{"heading": "交接与记录", "level": 2, "paragraphs": ["本节另有说明，不因只剩一张表而改挂。"], "tables": [day, night, header_form, row_form, ambiguous]}],
    }}, ensure_ascii=False), encoding="utf-8")
    output = work / "tables.docx"
    old_argv = sys.argv
    sys.argv = ["build_docx.py", str(payload), str(output)]
    try:
        main()
    finally:
        sys.argv = old_argv
    from docx import Document
    document = Document(output)
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    tables = [[[cell.text for cell in row.cells] for row in table.rows] for table in document.tables]
    flat = json.dumps(tables, ensure_ascii=False)
    check("both handover tables and their own rows survive", "日间交接" in text and "夜间异常处置" in text and "日间闭环" in flat and "夜间专记" in flat)
    check("signature cells stay blank in both layouts", any(row[0] != "签名" and row[1] == "" for table in tables for row in table[1:]) and any(row[0] == "签名" and row[1] == "" for table in tables for row in table[1:]))
    check("unrelated lead is not consumed as a table caption", "本节另有说明" in text and "日间交接" in text)


def test_review_repair_record():
    from app_core import _record_account_review_repair

    run = Path(tempfile.mkdtemp())
    saved = use_roots(MMF_DATA_ROOT=str(run), MMF_RUNS_DIR=str(run), MMF_RUNTIME_DIR=str(run / "runtime"))
    try:
        report, _pagination = _record_account_review_repair(
            run / "missing.docx", run, {"findings": [{"rule_id": "PAGE_COUNT", "issue": "页数不足"}]},
            None, {}, "cursor_grok_4_6", "NEEDS_REVIEW")
        again, _pagination = _record_account_review_repair(
            run / "missing.docx", run, {"findings": [{"rule_id": "HEADING_GAP", "issue": "标题间距"}]},
            None, {}, "cursor_grok_4_6", "NEEDS_REVIEW")
        direct, _pagination = _record_account_review_repair(
            run / "missing.docx", run, {"findings": [{"rule_id": "PAGE_COUNT", "issue": "页数不足"}]},
            None, {}, "openai_compatible", "NEEDS_REVIEW")
    finally:
        restore_roots(saved)
    page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    check("page and fact gaps are not rewritten", report["status"] == "NO_FORMAT_FIX" and "保留原复核原因" in report["reason"])
    check("one repair record is shared", again == report and (run / "local_review_repair.json").is_file())
    check("direct api does not take the account repair", direct is None)
    check("page distinguishes the three repair outcomes", all(phrase in page for phrase in ("已试修并重新检查", "没有可自动修改的内容", "候选稿复检未通过，已保留原稿")))


def test_direct_api_unchanged():
    source = (APP / "providers" / "openai_compatible.py").read_text(encoding="utf-8")
    bridge = (APP / "providers" / "cursor_bridge.mjs").read_text(encoding="utf-8")
    check("direct api does not gain the cursor budget", "CURSOR_RUN_BUDGET" not in source and "enableAgentRetries" not in source)
    check("sdk retry stays enabled", "enableAgentRetries: true" in bridge)


def test_existing_recovery():
    import paths
    from test_cursor_word_recovery import test_resume_reconciles_confirmed_stop_and_legacy_local_refusal_only

    class Patch:
        def setattr(self, obj, name, value):
            setattr(obj, name, value)

    previous = paths.current
    try:
        test_resume_reconciles_confirmed_stop_and_legacy_local_refusal_only(Path(tempfile.mkdtemp()), Patch())
        check("existing cursor recovery test still passes", True)
    finally:
        paths.current = previous


def test_fact_sources():
    suite = unittest.defaultTestLoader.loadTestsFromName("test_run_k_fact_sources")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    check("existing fact-source cases still pass", result.wasSuccessful())


if __name__ == "__main__":
    test_yinfeng_resume()
    test_budget_gate_once()
    test_metrics()
    test_scope_pack_and_docx()
    test_tables_docx()
    test_review_repair_record()
    test_direct_api_unchanged()
    test_existing_recovery()
    test_fact_sources()
    print("ALL PASS")
