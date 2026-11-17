"""MMF-007C Round 3：数据根重定向姿态下的招标上传通路。

便携包装在只读目录、数据落在包外（AppData / E2E 证据目录）是受支持的部署姿态，
但 tender_intake 的越界判定以前只认包根：一次合法上传被判成"路径越界"，接口直接 400。
这里既钉住"合法姿态要跑得通"，也钉住"真正的越界还是要拒绝"。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys  # noqa: E402

sys.path.insert(0, str(ROOT / "app"))

import app_core  # noqa: E402
import paths as mmf_paths  # noqa: E402
import server as app_server  # noqa: E402
from providers.mock import MockProvider  # noqa: E402
from tender_intake import extraction as tender_extraction  # noqa: E402
from tender_intake.models import TenderError, ensure_within  # noqa: E402
from unittest.mock import patch  # noqa: E402


REDIRECT_ENV = (
    "MMF_DATA_ROOT", "MMF_OUTPUT_ROOT", "MMF_RUNS_DIR", "MMF_LOGS_DIR",
    "MMF_RUNTIME_DIR", "MMF_TEMP_ROOT", "MMF_CONFIG_ROOT",
)

TENDER_DOCX = (
    "第一章 项目概况与需求理解\n"
    "本项目为某产业园区物业服务采购项目，服务范围含秩序维护、保洁、绿化养护与工程维修。\n"
    "投标人须说明驻场项目经理配置、班次安排与交接班记录要求。\n"
)


def _make_tender_docx(path: Path) -> None:
    from docx import Document

    document = Document()
    for line in TENDER_DOCX.splitlines():
        document.add_paragraph(line)
    document.add_table(rows=2, cols=3).add_row()
    document.save(str(path))


class RootRedirectContainmentTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.package = mmf_paths.current().package_root
        cls.saved_env = {key: os.environ.get(key) for key in REDIRECT_ENV}
        cls.work = Path(tempfile.mkdtemp(prefix="mmf007c_root_redirect_"))
        _make_tender_docx(cls.work / "tender.docx")
        for key in REDIRECT_ENV:
            os.environ[key] = str(cls.work / key.removeprefix("MMF_").lower())
        os.environ["MMF_CONFIG_ROOT"] = str(cls.package / "config")
        mmf_paths.reload(cls.package)
        app_server.rebind_app_core()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            for key, value in cls.saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            mmf_paths.reload(cls.package)
            app_server.rebind_app_core()
        finally:
            shutil.rmtree(cls.work, ignore_errors=True)

    def _create_run(self) -> tuple[dict, Path]:
        # 只用 Mock Provider，并且不让这轮测试去读开发者机器上的 providers.local.json。
        mock = MockProvider({"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock"})
        manager = type("M", (), {"get": lambda self, name, require_available=False: mock})()
        payload = (self.work / "tender.docx").read_bytes()
        with patch.object(app_core, "provider_manager", return_value=manager):
            result = app_core.create_tender_run([("tender.docx", payload)], "mock")
        # 按返回的 run_id 取目录：同一秒内建两个 Run 时，按名字排序会把上一个挑回来。
        run_dir = Path(app_core.RUNS_DIR) / result["run_id"]
        self.assertTrue(run_dir.is_dir(), "重定向后的 Run 目录没有落在指定的 runs_dir 里")
        return result, run_dir

    def test_upload_and_extract_survive_runs_dir_outside_package(self) -> None:
        payload = (self.work / "tender.docx").read_bytes()
        result, run_dir = self._create_run()

        self.assertEqual(result["status"], "extraction_completed")
        self.assertEqual(result["extraction_summary"]["files"], 1)
        self.assertGreaterEqual(result["extraction_summary"]["paragraphs"], 1)

        # 包根之外、允许根之内：写盘和读回都必须成立。
        self.assertTrue((run_dir / "tender" / "originals" / "tender.docx").is_file())
        self.assertFalse(run_dir.is_relative_to(self.package))
        # 006A 进度状态属于数据，不是代码：它必须跟着被重定向的数据根走。
        self.assertTrue(app_core.MMF006A_STATE_FILE.is_file())
        self.assertTrue(app_core.MMF006A_STATE_FILE.is_relative_to(Path(app_core.RUNTIME_DIR)))
        uploads = app_core.read_json(run_dir / "tender" / "uploads.json", "test")
        record = uploads["files"][0]
        self.assertTrue(record["stored_relative_path"], "上传审计标签不能为空")
        self.assertEqual(record["sha256"], hashlib.sha256(payload).hexdigest())

        extraction = tender_extraction.extract_run(app_core.RUNTIME_ROOT, run_dir)
        joined = " ".join(row["text"] for row in extraction["paragraphs"])
        self.assertIn("产业园区物业服务采购", joined, "抽取必须能把包外原文读回来")

    def test_path_outside_every_allowed_root_is_still_refused(self) -> None:
        foreign = Path(tempfile.gettempdir()).parent / "mmf007c_outside_allowed"
        with self.assertRaises(TenderError) as ctx:
            ensure_within(foreign / "leak.docx", app_core.RUNTIME_ROOT)
        self.assertIn("越界", str(ctx.exception))

    def test_relative_path_injection_in_uploads_cannot_escape_run_dir(self) -> None:
        _, run_dir = self._create_run()
        # 原文确实在 Run 目录里：拒绝必须是因为那条被注入的路径，而不是"文件本来就没有"。
        self.assertTrue((run_dir / "tender" / "originals" / "tender.docx").is_file())
        uploads_path = run_dir / "tender" / "uploads.json"
        uploads = app_core.read_json(uploads_path, "test")
        uploads["files"][0]["stored_relative_path"] = "../../../../Windows/win.ini"
        app_core.write_json(uploads_path, uploads)

        with self.assertRaises(TenderError):
            tender_extraction.extract_run(app_core.RUNTIME_ROOT, run_dir)
        self.assertFalse((self.package.parent / "Windows").exists())

    def test_artifact_path_label_falls_back_to_absolute(self) -> None:
        outside = Path(app_core.RUNS_DIR) / "run_label_probe" / "方案.docx"
        label = app_core._path_label(outside)
        self.assertEqual(label, str(outside.resolve()))
        inside = app_core.RUNTIME_ROOT / "output" / "x.docx"
        self.assertEqual(app_core._path_label(inside), "output/x.docx")


if __name__ == "__main__":
    unittest.main(verbosity=2)
