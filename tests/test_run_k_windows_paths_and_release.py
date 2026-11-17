"""Run K: deep Windows storage, native aliases, and truthful package evidence."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from filesystem_paths import filesystem_path, native_render_root
from providers import base
from longform import pagination

spec = importlib.util.spec_from_file_location("run_k_release_evidence", ROOT / "tools/gen_release_evidence.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@unittest.skipUnless(os.name == "nt", "Win32 filesystem and native drive alias integration")
class WindowsPaths(unittest.TestCase):
    def test_deep_json_directory_and_target_survive_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ("a" * 100) / ("b" * 100) / "requirement_classification.json"
            self.assertGreater(len(str(path)), 260)
            base.write_json(path, {"status": "BLOCK", "content": "原始正文"})
            base.write_json(path, {"status": "NEEDS_REVIEW", "content": "保留正文"})
            self.assertEqual(json.loads(filesystem_path(path).read_text(encoding="utf-8"))["content"], "保留正文")
            self.assertEqual(list(filesystem_path(path.parent).glob("*.tmp")), [])

    def test_failed_deep_replace_keeps_prior_evidence_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ("a" * 100) / ("b" * 100) / "matrix.json"
            base.write_json(path, {"status": "BLOCK"})
            with patch.object(base.os, "replace", side_effect=PermissionError("locked")):
                with self.assertRaises(PermissionError):
                    base.write_json(path, {"status": "PASS"})
            self.assertEqual(json.loads(filesystem_path(path).read_text())["status"], "BLOCK")
            self.assertEqual(list(filesystem_path(path.parent).glob("*.tmp")), [])

    def test_native_aliases_are_distinct_and_removed_after_an_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / ("a" * 100)
            second = Path(tmp) / ("b" * 100)
            filesystem_path(first).mkdir()
            filesystem_path(second).mkdir()
            aliases = []
            with self.assertRaisesRegex(RuntimeError, "injected"):
                with native_render_root(first) as a, native_render_root(second) as b:
                    aliases = [a, b]
                    self.assertNotEqual(a.drive, b.drive)
                    self.assertEqual(filesystem_path(a.resolve()), filesystem_path(first.resolve()))
                    self.assertEqual(filesystem_path(b.resolve()), filesystem_path(second.resolve()))
                    (a / "proof.txt").write_text("same physical directory")
                    self.assertTrue(filesystem_path(first / "proof.txt").is_file())
                    raise RuntimeError("injected")
            for alias in aliases:
                self.assertFalse(alias.exists(), "our mapping must be removed on exception")

    def test_real_renderer_uses_deep_temp_without_changing_original_docx(self):
        if not pagination.locate_renderer():
            self.skipTest("No local LibreOffice; real render remains unverified")
        from docx import Document
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.docx"
            document = Document()
            document.add_paragraph("真实分页测试：原始内容必须保留。")
            document.save(source)
            before = source.read_bytes()
            deep = root / ("t" * 100)
            filesystem_path(deep).mkdir()
            note = {}
            with patch.object(tempfile, "tempdir", str(filesystem_path(deep))):
                rendered = pagination.render_document(source, diagnostics=note, timeout=60)
            self.assertIsNotNone(rendered, note)
            pdf, work, pages, marks = rendered
            try:
                self.assertTrue(pdf.is_file())
                self.assertGreater(len(pages), 0)
                self.assertTrue(note["native_path_alias_used"])
                self.assertTrue(note["native_path_alias_released"])
                self.assertEqual(source.read_bytes(), before)
                self.assertTrue(work.resolve().is_relative_to(deep.resolve()))
            finally:
                shutil.rmtree(filesystem_path(work))

    def test_unc_and_existing_extended_paths_keep_the_same_target(self):
        unc = "\\\\host\\share\\" + "x" * 240 + "\\file.json"
        expected = "\\\\?\\UNC\\" + unc[2:]
        self.assertEqual(str(filesystem_path(unc)), expected)
        self.assertEqual(str(filesystem_path(expected)), expected)

    def test_concurrent_and_followup_real_renders_do_not_reuse_profile_uris(self):
        if not pagination.locate_renderer():
            self.skipTest("No local LibreOffice; concurrent render remains unverified")
        from docx import Document
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.docx"
            document = Document()
            document.add_paragraph("独立渲染会话正文。")
            document.save(source)
            notes = [{}, {}, {}]
            def render(index):
                return pagination.render_document(source, diagnostics=notes[index], timeout=60)
            results = []
            try:
                with patch.object(tempfile, "tempdir", str(root)):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        results.extend(pool.map(render, [0, 1]))
                    results.append(render(2))
                self.assertTrue(all(result is not None for result in results), notes)
                self.assertEqual(len({note["native_render_profile_uri"] for note in notes}), 3)
                self.assertTrue(all(note["native_path_alias_released"] for note in notes))
                self.assertTrue(all(len(result[2]) > 0 for result in results))
            finally:
                for result in results:
                    if result is not None:
                        self.assertTrue(result[1].resolve().is_relative_to(root.resolve()))
                        shutil.rmtree(filesystem_path(result[1]))

    def test_node_module_check_accepts_extended_deep_filesystem_roots(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ("a" * 100) / ("b" * 100)
            package = filesystem_path(root / "node_modules/@oai/artifact-tool")
            package.mkdir(parents=True)
            filesystem_path(root / "package.json").write_text(json.dumps({"dependencies": {"@oai/artifact-tool": "2.8.52"}}))
            (package / "package.json").write_text(json.dumps({"name": "@oai/artifact-tool", "version": "2.8.52", "type": "module", "exports": "./index.mjs"}))
            (package / "index.mjs").write_text("export class Presentation{}; export class PresentationFile{};")
            modules = filesystem_path(root / "node_modules")
            result = subprocess.run([node, str(ROOT / "app/check_ppt_runtime.mjs"), str(modules)],
                                    capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "PASS")
            self.assertEqual(list(filesystem_path(root).glob("*.pptx")), [])


class DependencyEvidence(unittest.TestCase):
    def _candidate(self, root):
        (root / "app/node_modules/@oai/artifact-tool").mkdir(parents=True)
        (root / "app/requirements.txt").write_text("python-docx==1.2.0\npypdf==6.1.1\nPillow==12.3.0\n", encoding="utf-8")
        (root / "app/package.json").write_text(json.dumps({"dependencies": {"@oai/artifact-tool": "2.8.52"}}))
        installed = root / "app/node_modules/@oai/artifact-tool/package.json"
        installed.write_text(json.dumps({"name": "@oai/artifact-tool", "version": "2.8.52"}))
        return {"ppt_dependencies": {"version": "2.8.52"}}, installed

    def test_manifest_uses_candidate_pins_and_hashes_every_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _ = self._candidate(root)
            dependencies, sources = release.dependency_evidence(root, manifest)
            self.assertEqual(dependencies, {"python-docx": "1.2.0", "pypdf": "6.1.1", "Pillow": "12.3.0", "@oai/artifact-tool": "2.8.52"})
            self.assertNotIn("pptxgenjs", dependencies)
            self.assertEqual(len(sources), 3)
            self.assertTrue(all(len(sha) == 64 for sha in sources.values()))

    def test_bundled_version_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, installed = self._candidate(root)
            installed.write_text(json.dumps({"name": "@oai/artifact-tool", "version": "2.8.59"}))
            with self.assertRaisesRegex(ValueError, "disagrees"):
                release.dependency_evidence(root, manifest)

    def test_build_version_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _ = self._candidate(root)
            manifest["ppt_dependencies"]["version"] = "2.8.59"
            with self.assertRaisesRegex(ValueError, "build manifest disagrees"):
                release.dependency_evidence(root, manifest)

    def test_unpinned_python_dependency_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _ = self._candidate(root)
            (root / "app/requirements.txt").write_text("Pillow>=12\n")
            with self.assertRaisesRegex(ValueError, "exact Python pins"):
                release.dependency_evidence(root, manifest)


if __name__ == "__main__":
    unittest.main()
