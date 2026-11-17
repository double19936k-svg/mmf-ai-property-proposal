"""MMF-007C Round 2 §18-§23：P0-3 真实分页 + 目录页码刷新 + TOC_PAGE_VARIANCE 门控。

上一轮交付物的目录 11 条页码全部是 "1"。那不是目录坏了，而是构建期写进去的
PAGEREF 缓存值从未被真实分页刷新过 —— Word/WPS 打开时会自己重算，所以只在字处理
软件里看过的人不会发现。这里要钉住的是三件事：

1. 缓存页码必须被测量，全同页码必须进门控（不得 COMPLETED_CLEAN）；
2. 刷新只做 XML 级最小补丁：目录域、书签、超链接、updateFields 一律不动，
   WPS 兼容性仍由 WPS 自己负责，LibreOffice 只是量尺；
3. 没有渲染器的机器必须如实报"没刷"，而不是把没刷新的产物当成通过。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1] / "app"
TESTS = Path(__file__).resolve().parent
for item in (APP, TESTS):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from delivery_state import DELIVERY_REVIEW, classify_fail_soft
from governance.final_artifact_qa import evaluate_docx_artifact
from governance.toc_qa import evaluate_native_toc
from longform import pagination

BRIEF = {"project_name": "目录分页试炼项目", "scenario": "完整物业服务方案", "medium": "WORD"}
_PARA = ("本单元规定项目基本事实与服务对象清单的形成与确认作业，以招标公告、技术需求书与合同文本"
         "已确认的内容为唯一事实来源，形成可核查、可交接、可复核的事实底稿，供各专业模块的"
         "方案编制与现场检查统一引用，并在部位发生变更时增补复核。") * 4
CHAPTERS = ["项目理解与需求边界", "服务构想与总体运营机制", "组织架构与人员履约保障", "客户服务与诉求闭环"]


def _artifact() -> dict:
    sections = []
    for title in CHAPTERS:
        sections.append({"heading": title, "level": 1, "paragraphs": [_PARA, _PARA]})
        sections.append({"heading": f"{title}的分项安排", "level": 2, "paragraphs": [_PARA]})
    return {
        "title": "目录分页试炼项目完整物业服务方案",
        "include_toc": True,
        "toc_depth": 1,
        "lead": [f"{BRIEF['project_name']}｜技术标"],
        "sections": sections,
    }


def _build_docx(root: Path) -> tuple[Path, Path]:
    """Build through the production renderer entry point, not a hand-made docx."""
    content = root / "content.json"
    content.write_text(json.dumps({"brief": BRIEF, "artifact": _artifact()}, ensure_ascii=False),
                       encoding="utf-8")
    docx = root / "artifact.docx"
    done = subprocess.run(
        [sys.executable, str(APP / "build_docx.py"), str(content), str(docx)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    assert done.returncode == 0 and docx.is_file(), done.stderr[-400:]
    return docx, root


def _document_xml(path: Path) -> bytes:
    with zipfile.ZipFile(path) as archive:
        return archive.read("word/document.xml")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Toc01GateOnIdenticalPages(unittest.TestCase):
    def test_all_one_directory_is_measured_and_cannot_complete_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            docx, run = _build_docx(Path(tmp))
            before = evaluate_native_toc(docx)
            self.assertEqual(before["toc_entry_count"], len(CHAPTERS))
            self.assertEqual(set(before["toc_displayed_pages"]), {"1"},
                             "构建期写入的 PAGEREF 缓存值必须被 QA 读到")
            self.assertEqual(before["toc_identical_page_share"], 1.0)
            self.assertFalse(before["toc_page_variance_pass"])
            # 结构本身仍是好的：门控必须是独立的一条，而不是把结构判成 FAIL。
            self.assertEqual(before["status"], "PASS")

            qa = evaluate_docx_artifact(docx, brief=BRIEF, run_dir=run)
            variance = [row for row in qa.get("findings") or [] if row.get("rule_id") == "TOC_PAGE_VARIANCE"]
            self.assertEqual(len(variance), 1, "§18-§23：全同页码必须进交付门")
            self.assertEqual(variance[0]["severity"], "AUTO_REPAIR",
                             "刷新是可自动修的动作，但不得伪装成已核对通过")
            decision = classify_fail_soft(artifact_path=docx, artifact_qa=qa)
            self.assertEqual(decision["delivery_status"], DELIVERY_REVIEW)
            self.assertTrue(decision["artifact_available"], "§8：门控不得吞掉已经生成的 Word")


class Toc02RendererAbsentIsHonest(unittest.TestCase):
    def test_missing_renderer_reports_and_leaves_the_artifact_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            docx, run = _build_docx(Path(tmp))
            digest = _sha(docx)
            with patch.object(pagination, "locate_renderer", return_value=""):
                report = pagination.refresh_artifact_pagination(docx, run_dir=run)
            self.assertEqual(report["status"], "SKIPPED_NO_RENDERER")
            self.assertEqual(_sha(docx), digest, "没有渲染器时不得碰客户产物")
            # 没刷新必须仍然是可测量的失败事实，而不是默认通过。
            self.assertFalse(evaluate_native_toc(docx)["toc_page_variance_pass"])


class Toc03RealPaginationRefresh(unittest.TestCase):
    def test_refresh_assigns_distinct_real_pages_without_touching_the_toc_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            docx, run = _build_docx(Path(tmp))
            if not pagination.locate_renderer():
                # 这台机器没有分页量尺：断言降级路径诚实，而不是跳过整条用例。
                report = pagination.refresh_artifact_pagination(docx, run_dir=run)
                self.assertIn(report["status"], {"SKIPPED_NO_RENDERER", "RENDER_FAILED"})
                self.assertEqual(evaluate_native_toc(docx)["toc_identical_page_share"], 1.0)
                return
            structure_before = evaluate_native_toc(docx)
            xml_before = _document_xml(docx)
            report = pagination.refresh_artifact_pagination(docx, run_dir=run)
            self.assertEqual(report["status"], "REFRESHED", report)
            self.assertGreaterEqual(report["rendered_pages"], len(CHAPTERS) + 2,
                                    "正文必须真的分多页，否则这个用例测不到分页")
            self.assertEqual(report["toc_pages_unresolved"], 0)
            self.assertEqual(report["toc_pages_verified"], len(CHAPTERS))
            after = evaluate_native_toc(docx)
            pages = [int(value) for value in after["toc_displayed_pages"]]
            self.assertEqual(len(set(pages)), len(pages), "刷新后每个章节应落在不同页")
            self.assertEqual(pages, sorted(pages), "目录页码必须单调不减")
            self.assertGreaterEqual(pages[0], 3, "正文页码不应从封面/目录页开始")
            self.assertTrue(after["toc_page_variance_pass"])
            self.assertLess(after["toc_identical_page_share"], 0.8)
            # XML 级最小补丁：目录域/书签/域数量与 updateFields 全部保持原样。
            for key in ("native_toc", "pageref_count", "toc_entry_count", "valid_target_count",
                        "update_fields_on_open", "bookmark_count"):
                self.assertEqual(after[key], structure_before[key], f"{key} 被刷新改动了")
            self.assertEqual(after["status"], "PASS")
            delta = len(_document_xml(docx)) - len(xml_before)
            self.assertLess(abs(delta), 400, f"页码补丁不应改动其它内容，实际 {delta:+d} 字节")

    def test_refresh_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            docx, run = _build_docx(Path(tmp))
            if not pagination.locate_renderer():
                report = pagination.refresh_artifact_pagination(docx, run_dir=run)
                self.assertIn(report["status"], {"SKIPPED_NO_RENDERER", "RENDER_FAILED"})
                return
            first = pagination.refresh_artifact_pagination(docx, run_dir=run)
            pages = list(evaluate_native_toc(docx)["toc_displayed_pages"])
            second = pagination.refresh_artifact_pagination(docx, run_dir=run)
            self.assertEqual(evaluate_native_toc(docx)["toc_displayed_pages"], pages,
                             "重复刷新不得改变已经确定的页码")
            self.assertEqual(first["rendered_pages"], second["rendered_pages"])
            self.assertEqual(int(second["toc_pages_refreshed"] or 0), 0,
                             "第二次没有新页码可写，不应再报成刷新过")


class Toc04PageMappingRules(unittest.TestCase):
    """页码映射的纯逻辑面：不依赖本机有没有 LibreOffice。"""

    def _entries(self, *labels: str) -> list[dict]:
        return [{"anchor": f"MMF_TOC_{index:04d}", "text": label, "node": None,
                 "cached_page": "1", "rendered_page": 0} for index, label in enumerate(labels, 1)]

    def test_outline_marks_are_matched_in_reading_order(self):
        entries = self._entries("第一章 项目理解", "第二章 服务构想")
        marks = [("第一章 项目理解", 3), ("第二章 服务构想", 12)]
        mapping = pagination.resolve_pages(entries, ["", "", "", "", "", "", "", "", "", "", "", "", ""], marks)
        self.assertEqual([row["rendered_page"] for row in entries], [4, 13])
        self.assertEqual(mapping["resolved"], 2)

    def test_monotonic_guard_rejects_a_backward_match(self):
        entries = self._entries("后行章节", "前行章节")
        marks = [("后行章节", 20), ("前行章节", 5)]
        mapping = pagination.resolve_pages(entries, [""] * 25, marks)
        self.assertEqual(entries[0]["rendered_page"], 21)
        self.assertEqual(entries[1]["rendered_page"], 0, "倒退的匹配一律不采信")
        self.assertEqual(mapping["unresolved"], 1)

    def test_page_text_is_used_when_there_is_no_outline(self):
        entries = self._entries("保洁服务方案")
        pages = ["", "", "", "保洁服务方案 正文开始"]
        mapping = pagination.resolve_pages(entries, pages, [])
        self.assertEqual(entries[0]["rendered_page"], 4)
        self.assertEqual(mapping["resolved"], 1)

    def test_toc_pages_are_not_matched_as_body_content(self):
        # 目录页自己就含全部条目文本；没有大纲时必须从目录之后开始找。
        entries = self._entries("甲章", "乙章", "丙章")
        pages = ["封面", "目录 甲章 乙章 丙章"] + ["正文"] * 3 + ["甲章", "乙章", "丙章"]
        mapping = pagination.resolve_pages(entries, pages, [])
        self.assertEqual(mapping["body_start_page"], 2)
        self.assertEqual([row["rendered_page"] for row in entries], [6, 7, 8])


class Toc05AuditSurface(unittest.TestCase):
    """§50 报告口径：分页事实必须能从 run_audit 读出来，而不是靠人复述。

    这里刻意用真实键名写报告：任何一侧改键名而另一侧没跟上，取值就会静默变成 0，
    而 0 在报告里看起来和"真的没映射"完全一样。
    """

    REPORT = {
        "schema_version": "artifact-pagination-v0.1",
        "status": "REFRESHED",
        "renderer": "soffice",
        "rendered_pages": 207,
        "visible_chars": 139932,
        "page_char_counts": [812] * 206 + [5],
        "thin_page_count": 1,
        "effective_content_pages": 206,
        "meaningful_page_ratio": 0.9952,
        "toc_entry_count": 11,
        "toc_pages_resolved": 11,
        "toc_pages_refreshed": 11,
        "toc_pages_verified": 11,
        "toc_pages_unresolved": 0,
        "toc_body_start_page": 3,
        "toc_outline_mark_count": 42,
        "document_xml_rewritten": True,
        "identical_page_share": 0.0909,
        "displayed_pages": ["3", "14", "49", "73", "92", "116", "130", "150", "166", "184", "197"],
    }

    def test_report_keys_map_onto_audit_fields(self):
        import app_core

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "artifact_pagination.json").write_text(json.dumps(self.REPORT), encoding="utf-8")
            with patch.object(app_core, "read_json",
                              side_effect=lambda path, *_a: json.loads(Path(path).read_text(encoding="utf-8"))):
                fields = app_core._pagination_audit_fields(run)
        self.assertEqual(fields["RENDERED_PAGES"], 207)
        self.assertEqual(fields["VISIBLE_EFFECTIVE_CHARS"], 139932)
        self.assertEqual(fields["TOC_ENTRY_COUNT"], 11)
        self.assertEqual(fields["TOC_PAGES_RESOLVED"], 11)
        self.assertEqual(fields["TOC_PAGES_VERIFIED"], 11)
        self.assertEqual(fields["TOC_PAGES_UNRESOLVED"], 0)
        self.assertTrue(fields["ARTIFACT_XML_REWRITTEN"])
        self.assertEqual(fields["TOC_BODY_START_PAGE"], 3)
        self.assertEqual(fields["THIN_PAGE_COUNT"], 1, "整页几乎无字的页必须单独计数")
        self.assertEqual(fields["EFFECTIVE_CONTENT_PAGES"], 206)
        self.assertEqual(fields["MEANINGFUL_PAGE_RATIO"], 0.9952)
        self.assertEqual(len(fields["TOC_DISPLAYED_PAGES"]), 11)

    def test_absent_report_is_absent_not_zero(self):
        import app_core

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(app_core._pagination_audit_fields(Path(tmp)), {})


if __name__ == "__main__":
    unittest.main()
