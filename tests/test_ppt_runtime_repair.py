import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
import env_check

class PptRuntimeRepair(unittest.TestCase):
    def fixture(self,root,version='2.8.52',body=None):
        (root/'package.json').write_text(json.dumps({'dependencies':{'@oai/artifact-tool':'2.8.52'}}),encoding='utf-8')
        pkg=root/'node_modules/@oai/artifact-tool'
        pkg.mkdir(parents=True)
        (pkg/'package.json').write_text(json.dumps({'name':'@oai/artifact-tool','version':version,'type':'module','exports':'./index.mjs'}),encoding='utf-8')
        if body is not None:(pkg/'index.mjs').write_text(body,encoding='utf-8')
        return root/'node_modules'
    def check(self,modules):
        return subprocess.run([shutil.which('node'),str(ROOT/'app/check_ppt_runtime.mjs'),str(modules)],capture_output=True,text=True,encoding='utf-8',timeout=20)
    def test_missing_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=self.check(Path(tmp)/'node_modules')
            self.assertNotEqual(result.returncode,0)
            self.assertIn('MISSING_PPT_PACKAGE',result.stdout)
    def test_version_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=self.check(self.fixture(Path(tmp),'0.0.0'))
            self.assertNotEqual(result.returncode,0)
            self.assertIn('VERSION_MISMATCH',result.stdout)
    def test_manifest_without_entry_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertNotEqual(self.check(self.fixture(Path(tmp))).returncode,0)
    def test_missing_exports_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=self.check(self.fixture(Path(tmp),body='export const unrelated=1;'))
            self.assertNotEqual(result.returncode,0)
            self.assertIn('EXPORTS_MISSING',result.stdout)
    def test_valid_import_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=self.check(self.fixture(Path(tmp),body='export class Presentation{}; export class PresentationFile{};'))
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual(json.loads(result.stdout)['status'],'PASS')
            self.assertEqual(list(Path(tmp).glob('*.pptx')),[])
    def test_node_ready_message_does_not_ask_to_install_node(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(env_check,'_find_node',return_value='present-node'):
            row=env_check._artifact_tool_ok(Path(tmp)/'node_modules')
            self.assertFalse(row['ok'])
            self.assertIn('Node已就绪',row['user'])
            self.assertNotIn('安装Node',row['user'])
    def test_loaded_check_cached(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(env_check,'_find_node',return_value='present-node'):
            modules=self.fixture(Path(tmp))
            completed=subprocess.CompletedProcess([],0,json.dumps({'status':'PASS','version':'2.8.52'}),'')
            env_check._PPT_CHECK_CACHE.clear()
            with patch.object(env_check.subprocess,'run',return_value=completed) as check:
                self.assertTrue(env_check._artifact_tool_ok(modules)['ok'])
                self.assertTrue(env_check._artifact_tool_ok(modules)['ok'])
                self.assertEqual(check.call_count,1)

if __name__=='__main__':unittest.main()
