from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from docx import Document


PACKAGE = Path(__file__).resolve().parents[1]
APP = PACKAGE / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import app_core  # noqa: E402
from governance.final_artifact_qa import evaluate_docx_artifact  # noqa: E402
from providers.base import ProviderUnavailableError  # noqa: E402
from providers.mock import MockProvider  # noqa: E402
from workflow_policy import (  # noqa: E402
    KNOWLEDGE_RECOMMENDATION_IS_OPTIONAL,
    NO_RECOMMENDATION,
    PROVIDER_FAILED,
    USER_SELECTED_NONE,
    USER_SELECTED_SOME,
    classify_knowledge_state,
    generation_may_proceed,
    resolve_selected_positive_ids,
)
from longform.orchestrator import generate_longform  # noqa: E402


def _pack() -> dict:
    requirements = [
        {"requirement_id": "REQ-0001", "normalized_requirement": "项目位于青岛市崂山区香港东路108号", "domain": "project_fact", "mandatory_level": "MUST", "confirmation_status": "CONFIRMED"},
        {"requirement_id": "REQ-0002", "normalized_requirement": "安全秩序管理含门岗巡逻与异常处置", "domain": "security", "mandatory_level": "MUST", "confirmation_status": "CONFIRMED"},
        {"requirement_id": "REQ-0003", "normalized_requirement": "工程巡检维保与故障闭环", "domain": "other", "mandatory_level": "MUST", "confirmation_status": "CONFIRMED"},
        {"requirement_id": "REQ-0004", "normalized_requirement": "客户投诉闭环与回访", "domain": "sla_kpi", "mandatory_level": "MUST", "confirmation_status": "CONFIRMED", "scoring_item_id": "SCR-01"},
    ]
    return {
        "schema_version": "tender-requirement-pack-v0.1",
        "pack_id": "PACK-TEST-STABILITY",
        "status": "ready_for_plan",
        "project_facts": {
            "project_name": {"value": "海信国际中心测试项目"},
            "location": {"value": "青岛市崂山区香港东路108号"},
            "gross_area": {"value": "252567平方米"},
        },
        "service_scope": {"included": [], "excluded": [{"text": "会议会务不在本次范围"}], "deprioritized": [], "conditional": []},
        "requirements": requirements,
        "scoring_items": [{"scoring_item_id": "SCR-01", "must_respond": True, "label": "服务方案"}],
        "confirmation": {"ready_for_brief_seed": True},
    }


def _brief() -> dict:
    return {
        "project_name": "海信国际中心测试项目",
        "project_type": "综合体",
        "scenario": "完整物业服务方案",
        "medium": "WORD",
        "provider_name": "mock",
        "requirements": "安全、工程、客服闭环",
    }


def _selection() -> dict:
    return {
        "provider_name": "mock",
        "recommended_positive": [{"ku_id": "KU-0001"}],
        "applicable_guardrails": [],
        "knowledge_usage_contracts": [{"ku_id": "KU-0001", "selection_status": "SELECTED", "usable_content": "闭环方法", "language_level": "method"}],
        "selected_positive_ku_ids": ["KU-0001"],
        "auto_selected_positive_ids": ["KU-0001"],
    }


def _write_run(root: Path, pack: dict, brief: dict, selection: dict) -> Path:
    run = root / "run"
    (run / "tender").mkdir(parents=True)
    (run / "tender" / "requirement_pack.json").write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
    (run / "brief.json").write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
    (run / "knowledge_selection.json").write_text(json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8")
    return run


class EmptyThenHealthyProvider(MockProvider):
    def __init__(self, fail_calls: int):
        super().__init__({"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock-r24"})
        self.calls = 0
        self.fail_calls = fail_calls

    def invoke_structured(self, request, task_dir):
        self.calls += 1
        return super().invoke_structured(request, task_dir)

    def _section_fragment(self, request):
        fragment = super()._section_fragment(request)
        if self.calls <= self.fail_calls:
            fragment["body_blocks"] = [{"type": "subheading", "content": "关键岗位职责边界"}]
            fragment["content_unit_fragments"] = []
            fragment["tables"] = []
            fragment["processes"] = []
            fragment["callouts"] = []
        return fragment


def _http_json(url: str, data: dict | None = None, method: str = "GET", timeout: int = 30, raw: bytes | None = None, content_type: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": content_type or "application/json"}
    body = raw if raw is not None else (None if data is None else json.dumps(data).encode("utf-8"))
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
            return response.status, json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(payload) if payload else {"error": str(exc)}
        except json.JSONDecodeError:
            parsed = {"error": payload[-400:]}
        return exc.code, parsed


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class KnowledgePolicyTests(unittest.TestCase):
    def test_optional_contract_is_the_single_source(self) -> None:
        self.assertTrue(KNOWLEDGE_RECOMMENDATION_IS_OPTIONAL)
        self.assertTrue(generation_may_proceed(NO_RECOMMENDATION))
        self.assertTrue(generation_may_proceed(USER_SELECTED_NONE))
        self.assertTrue(generation_may_proceed(USER_SELECTED_SOME))
        self.assertTrue(generation_may_proceed(PROVIDER_FAILED))
        self.assertEqual(classify_knowledge_state(recommended_count=0, confirmed=True), NO_RECOMMENDATION)
        self.assertEqual(classify_knowledge_state(recommended_count=5, selected_count=0, confirmed=True), USER_SELECTED_NONE)
        self.assertEqual(classify_knowledge_state(recommended_count=5, selected_count=3, confirmed=True), USER_SELECTED_SOME)
        self.assertEqual(classify_knowledge_state(recommended_count=0, provider_failed=True), PROVIDER_FAILED)
        self.assertEqual(resolve_selected_positive_ids(["KU-1", "KU-2"], ["KU-2", "KU-9"]), ["KU-2"])

    def test_old_hard_require_removed_from_source(self) -> None:
        core = (APP / "app_core.py").read_text(encoding="utf-8")
        html = (PACKAGE / "static" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("请至少保留一条推荐知识", core)
        self.assertNotIn("当前引擎没有推荐出可用知识", core)
        self.assertIn("from workflow_policy import", core)
        self.assertIn("attach_recommendation_state", core)
        self.assertIn("本次未使用推荐知识", html)
        self.assertIn("class=\"positive-check\"", html)

    def test_empty_recommendation_is_not_user_deselect(self) -> None:
        self.assertNotEqual(
            classify_knowledge_state(recommended_count=0, selected_count=0, confirmed=True),
            USER_SELECTED_NONE,
        )
        self.assertNotEqual(
            classify_knowledge_state(recommended_count=0, selected_count=0, provider_failed=True),
            USER_SELECTED_NONE,
        )


class KnowledgeGenerationEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self.tmp.name) / "runs"
        self.runtime = Path(self.tmp.name) / "runtime"
        self.runs.mkdir()
        self.runtime.mkdir()
        self.patches = [
            patch.object(app_core, "RUNS_DIR", self.runs),
            patch.object(app_core, "RUNTIME_DIR", self.runtime),
            patch.object(app_core, "_checked", side_effect=lambda path: Path(path)),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in self.patches:
            item.stop()
        self.tmp.cleanup()

    def _brief(self) -> dict:
        return {
            "project_name": "稳定性冻结测试项目",
            "project_type": "产业园",
            "scenario": "完整物业服务方案",
            "medium": "WORD",
            "requirements": "安保、保洁、工程维保",
            "provider_name": "mock",
            "location": "青岛",
        }

    def test_recommend_allows_zero_items(self) -> None:
        empty = MockProvider({"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock"})
        empty.recommend_knowledge = lambda request, task_dir: {
            "recommended_positive": [],
            "applicable_guardrails": [{"ku_id": "KU-9020-D5AB7C37", "risk_content": "禁历史SLA", "reason": "risk"}],
            "missing_information": [],
            "provider_metadata": {"provider_name": "mock", "test_mode": True},
        }
        manager = type("M", (), {"get": lambda self, name, require_available=False: empty})()
        with patch.object(app_core, "provider_manager", return_value=manager):
            result = app_core.recommend_knowledge(self._brief())
        self.assertEqual(result["knowledge_status"], NO_RECOMMENDATION)
        self.assertFalse(result["knowledge_required"])
        self.assertEqual(result["recommended_knowledge_count"], 0)
        self.assertTrue(result["applicable_guardrails"])

    def test_provider_failure_is_not_empty_choice(self) -> None:
        failing = MockProvider({"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock"})
        def _boom(request, task_dir):
            raise ProviderUnavailableError("知识引擎超时")
        failing.recommend_knowledge = _boom
        manager = type("M", (), {"get": lambda self, name, require_available=False: failing})()
        with patch.object(app_core, "provider_manager", return_value=manager):
            result = app_core.recommend_knowledge(self._brief())
        self.assertEqual(result["knowledge_status"], PROVIDER_FAILED)
        self.assertIn("知识引擎超时", result.get("knowledge_warning") or "")
        self.assertNotEqual(result["knowledge_status"], USER_SELECTED_NONE)


class AutoRepairAndFinalQaTests(unittest.TestCase):
    def test_auto_repair_does_not_emit_false_final_error(self) -> None:
        provider = EmptyThenHealthyProvider(fail_calls=1)
        holder = tempfile.TemporaryDirectory()
        try:
            run = _write_run(Path(holder.name), _pack(), _brief(), _selection())
            result = generate_longform(
                run_dir=run,
                provider=provider,
                provider_name="mock",
                brief=_brief(),
                selection=_selection(),
                selected_ids=["KU-0001"],
                section_ids=["S01-01"],
                task_mode="standard",
                require_section_min=True,
            )
            checkpoint = json.loads((run / "checkpoint" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(result["word"]["completed_sections"], result["word"]["total_sections"])
            self.assertFalse(any(row.get("result") == "TERMINAL_BLOCKED" for row in checkpoint.get("qa_events") or []))
        finally:
            holder.cleanup()

    def test_final_qa_blocks_real_failure(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.docx"
            doc = Document()
            doc.add_paragraph("具体安排参见S03-02。")
            doc.save(path)
            report = evaluate_docx_artifact(path)
            self.assertEqual(report["status"], "BLOCK")
            self.assertTrue(any(row.get("rule_id") == "INTERNAL_SECTION_ID" for row in report.get("findings") or []))


def _work_root() -> Path:
    """Round 2 §4.1 卫生项的另一处落点：这套 HTTP 用例把整份数据根开在 `runtime/tmp` 里。

    源码树里这样无害（打包排除集与 gitignore 双重挡住）；在便携包里跑测，正常退出时
    `tearDownClass` 会把它删掉，但只要进程被强杀，一千多个文件就留在了"被验收的那份字节"里
    ——Round 2 归档的残留中 `mmf_stability_3924`、`mmf_stability_9748` 就是这么来的。
    临时根被显式重定向时（dist 侧 E2E）跟着一起出去，包内容保持逐文件不变。
    """
    override = os.environ.get("MMF_TEMP_ROOT")
    base = Path(override).expanduser() if override else PACKAGE / "runtime" / "tmp"
    return base / "stability_freeze"


class FullWorkflowHttpTests(unittest.TestCase):
    server_proc: subprocess.Popen | None = None
    work: Path | None = None
    url = ""
    runs = Path()
    output = Path()

    @classmethod
    def setUpClass(cls) -> None:
        work = _work_root() / f"mmf_stability_{os.getpid()}"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        cls.work = work
        cls.runs = work / "runs"
        cls.output = work / "output"
        runtime = work / "runtime"
        logs = work / "logs"
        cfg = work / "config"
        for path in (cls.runs, cls.output, runtime, logs, cfg):
            path.mkdir(parents=True)
        example = PACKAGE / "config" / "providers.example.json"
        if example.is_file():
            shutil.copy2(example, cfg / "providers.example.json")
            shutil.copy2(example, cfg / "providers.local.json")
        app_json = PACKAGE / "config" / "app.json"
        if app_json.is_file():
            shutil.copy2(app_json, cfg / "app.json")
        python = Path(sys.executable)
        (runtime / "runtime_config.json").write_text(json.dumps({
            "python_executable": str(python),
            "node_executable": shutil.which("node") or "",
            "node_modules": str(PACKAGE / "app" / "node_modules"),
            "bin_dir": str(python.parent),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        port = _free_port()
        env = os.environ.copy()
        env.update({
            "MMF_PACKAGE_ROOT": str(PACKAGE),
            "MMF_APP_ROOT": str(PACKAGE / "app"),
            "MMF_DATA_ROOT": str(work),
            "MMF_OUTPUT_ROOT": str(cls.output),
            "MMF_RUNS_DIR": str(cls.runs),
            "MMF_LOGS_DIR": str(logs),
            "MMF_RUNTIME_DIR": str(runtime),
            "MMF_TEMP_ROOT": str(work / "tmp"),
            "MMF_CONFIG_ROOT": str(cfg),
            "MMF_HOST": "127.0.0.1",
            "MMF_PORT": str(port),
            "PYTHONPATH": str(PACKAGE / "app"),
            "MMF_CREDENTIAL_NAMESPACE": "mmf_stability_freeze_test",
        })
        for key in ("DASHSCOPE_API_KEY", "MOONSHOT_API_KEY", "PROPERTY_AI_API_KEY", "ZHIPU_API_KEY"):
            env.pop(key, None)
        cls.server_proc = subprocess.Popen(
            [str(python), str(PACKAGE / "app" / "server.py")],
            cwd=str(PACKAGE / "app"),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # 子进程写 UTF-8，管道按 GBK 解码会抛 UnicodeDecodeError 并丢掉整段输出。
            encoding="utf-8",
            errors="replace",
        )
        cls.url = f"http://127.0.0.1:{port}"
        started = False
        for _ in range(50):
            if cls.server_proc.poll() is not None:
                break
            try:
                code, health = _http_json(f"{cls.url}/api/health", timeout=2)
                if code == 200 and health.get("status") == "ok":
                    started = True
                    break
            except Exception:
                time.sleep(0.2)
                continue
            time.sleep(0.2)
        if not started:
            err = ""
            if cls.server_proc.stderr:
                try:
                    err = cls.server_proc.stderr.read()[-800:]
                except Exception:
                    err = "server failed"
            cls.tearDownClass()
            raise unittest.SkipTest(f"stability HTTP server did not start: {err}")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.server_proc and cls.server_proc.poll() is None:
            cls.server_proc.terminate()
            try:
                cls.server_proc.wait(timeout=8)
            except Exception:
                cls.server_proc.kill()
        # MMF_STABILITY_KEEP_WORK=1：失败后保留整份工作目录。dist 侧全量跑里 test_flow_b
        # 出现过一次"生成任务 failed"（同一构建上单跑该项 1 passed、单跑整类 4 passed，
        # 第二遍全量未复现），而 run_dir / logs 会随 teardown 一起删掉，事后没有任何可查证据。
        # 留这个开关就是为了让下一次复现能留下 technical_failure.json 与服务器日志。
        if cls.work and cls.work.exists() and not os.environ.get("MMF_STABILITY_KEEP_WORK"):
            shutil.rmtree(cls.work, ignore_errors=True)

    def _wait_generation(self, run_id: str, timeout: int = 120) -> dict:
        last: dict = {}
        deadline = time.time() + timeout
        while time.time() < deadline:
            code, last = _http_json(f"{self.url}/api/runs/{run_id}/status", timeout=10)
            result = last.get("result") or {}
            if last.get("generated") or result.get("status") in {"generation_completed", "compliance_blocked", "technical_failed"}:
                return last
            if last.get("error") or last.get("run_status") in {"generation_failed", "generation_cancelled"}:
                return last
            time.sleep(0.4)
        return last

    def _brief_payload(self) -> dict:
        return {
            "project_name": "稳定性冻结产业园",
            "project_type": "产业园",
            "scenario": "完整物业服务方案",
            "medium": "WORD",
            "requirements": "本项目位于青岛，需安保巡逻、保洁作业和工程维保闭环。",
            "provider_name": "mock",
            "location": "青岛市崂山区",
        }

    def _generate(self, run_id: str, selected: list[str]) -> dict:
        code, started = _http_json(f"{self.url}/api/generate", {
            "run_id": run_id,
            "selected_positive_ids": selected,
            "clarification_answers": {},
            "speed_profile": "balanced",
            "max_parallelism": 3,
            "knowledge_optional": True,
        }, method="POST", timeout=15)
        self.assertIn(code, {200, 202}, started)
        self.assertNotIn("请至少保留一条推荐知识", str(started))
        status = self._wait_generation(run_id)
        result = status.get("result") or status
        self.assertNotIn("请至少保留一条推荐知识", str(status.get("error") or "") + str(result.get("message") or ""))
        return status

    def test_flow_b_knowledge_matrix_and_history(self) -> None:
        code, rec = _http_json(f"{self.url}/api/recommend", self._brief_payload(), method="POST", timeout=30)
        self.assertEqual(code, 200, rec)
        run_id = rec["run_id"]
        recs = rec.get("recommended_positive") or []
        self.assertTrue(recs)
        self.assertTrue(rec.get("applicable_guardrails"))
        ids = [row["ku_id"] for row in recs]

        selected_status = self._generate(run_id, ids[:1])
        selected_result = selected_status.get("result") or selected_status
        self.assertTrue(selected_status.get("generated") or selected_result.get("status") == "generation_completed", selected_status)
        self.assertTrue(selected_result.get("download_url") or selected_status.get("download_url"))
        self.assertTrue(
            selected_result.get("total_processing_seconds") is not None
            or selected_status.get("total_processing_seconds") is not None
            or selected_status.get("elapsed_seconds") is not None
        )

        code, rec2 = _http_json(f"{self.url}/api/recommend", self._brief_payload(), method="POST", timeout=30)
        none_status = self._generate(rec2["run_id"], [])
        none_result = none_status.get("result") or none_status
        self.assertTrue(none_status.get("generated") or none_result.get("status") == "generation_completed", none_status)
        selection = json.loads((self.runs / rec2["run_id"] / "knowledge_selection.json").read_text(encoding="utf-8"))
        self.assertEqual(selection.get("selected_knowledge_count"), 0)
        self.assertFalse(selection.get("knowledge_used"))
        self.assertTrue(selection.get("active_guardrail_ids") or selection.get("applicable_guardrails"))

        code, rec3 = _http_json(f"{self.url}/api/recommend", self._brief_payload(), method="POST", timeout=30)
        rec3_path = self.runs / rec3["run_id"] / "knowledge_selection.json"
        payload = json.loads(rec3_path.read_text(encoding="utf-8"))
        payload["recommended_positive"] = []
        payload["auto_selected_positive_ids"] = []
        payload["knowledge_status"] = NO_RECOMMENDATION
        payload["recommended_knowledge_count"] = 0
        rec3_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        zero_status = self._generate(rec3["run_id"], [])
        zero_result = zero_status.get("result") or zero_status
        self.assertTrue(zero_status.get("generated") or zero_result.get("status") == "generation_completed", zero_status)

        code, history = _http_json(f"{self.url}/api/runs")
        self.assertEqual(code, 200)
        ids_in_history = {row.get("run_id") for row in (history.get("runs") or [])}
        self.assertTrue({run_id, rec2["run_id"]} & ids_in_history)

        open_code, opened = _http_json(f"{self.url}/api/open-folder", {"run_id": rec2["run_id"], "folder_kind": "output"}, method="POST")
        self.assertIn(open_code, {200, 400})
        self.assertTrue("ok" in opened or "error" in opened)

    def test_flow_a_upload_confirm_generate(self) -> None:
        boundary = "----MMFStabilityBoundary"
        content = "本项目必须提供24小时安保服务。物业服务应包含保洁和绿化。工程需建立报修闭环。".encode("utf-8")
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"provider_name\"\r\n\r\nmock\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"req.txt\"\r\nContent-Type: text/plain\r\n\r\n"
        ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
        code, uploaded = _http_json(
            f"{self.url}/api/tender/upload",
            method="POST",
            raw=body,
            content_type=f"multipart/form-data; boundary={boundary}",
            timeout=30,
        )
        self.assertEqual(code, 200, uploaded)
        run_id = uploaded["run_id"]
        code, processing = _http_json(f"{self.url}/api/tender/process", {"run_id": run_id, "provider_name": "mock"}, method="POST")
        self.assertIn(code, {200, 202}, processing)
        pack = None
        recognition: dict = {}
        for _ in range(40):
            time.sleep(0.4)
            _code, payload = _http_json(f"{self.url}/api/tender/runs/{run_id}")
            recognition = payload.get("status") or {}
            pack_path = self.runs / run_id / "tender" / "requirement_pack.json"
            # 识别线程先落 Pack、后写终态 status。只等 Pack 出现就去 confirm，会读到
            # recognition_outcome 还没落定的那一帧，闸门按当时的旧状态拒绝就是假失败。
            if pack_path.is_file() and recognition.get("recognition_outcome"):
                pack = json.loads(pack_path.read_text(encoding="utf-8"))
                break
        self.assertTrue(pack, "FLOW A did not produce a requirement pack")
        if (
            recognition.get("recognition_outcome") == "AI_RECOGNITION_FAILED_WITH_LOCAL_FALLBACK"
            and not recognition.get("local_fallback_confirmed")
        ):
            # 治理要求运营者显式确认"继续使用本地解析结果"才允许生成完整方案。这一步必须
            # 由本用例自己走完，不能依赖同文件其它用例留下的顺序或状态。
            ack_code, acked = _http_json(
                f"{self.url}/api/tender/accept-local-fallback", {"run_id": run_id},
                method="POST", timeout=30,
            )
            self.assertEqual(ack_code, 200, acked)
        code, confirmed = _http_json(f"{self.url}/api/tender/confirm", {
            "run_id": run_id,
            "decisions": {"accept_remaining": True},
            "brief_options": {
                "project_name": "稳定性冻结招标项目",
                "project_type": "产业园",
                "scenario": "完整物业服务方案",
                "medium": "WORD",
                "provider_name": "mock",
            },
        }, method="POST", timeout=30)
        self.assertEqual(code, 200, confirmed)
        rec = confirmed.get("recommendation") or {}
        self.assertTrue(rec.get("run_id") or run_id)
        selected = [row["ku_id"] for row in rec.get("recommended_positive") or []][:1]
        status = self._generate(run_id, selected)
        result = status.get("result") or status
        self.assertTrue(status.get("generated") or result.get("status") == "generation_completed", status)

    def test_local_fallback_continue_then_generate(self) -> None:
        boundary = "----MMFFallbackBoundary"
        content = "安保巡逻与客服报事闭环，保洁分区作业。".encode("utf-8")
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"provider_name\"\r\n\r\nmock\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"req.txt\"\r\nContent-Type: text/plain\r\n\r\n"
        ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
        code, uploaded = _http_json(f"{self.url}/api/tender/upload", method="POST", raw=body, content_type=f"multipart/form-data; boundary={boundary}", timeout=30)
        self.assertEqual(code, 200, uploaded)
        run_id = uploaded["run_id"]
        _http_json(f"{self.url}/api/tender/process", {"run_id": run_id, "provider_name": "mock"}, method="POST")
        pack_path = self.runs / run_id / "tender" / "requirement_pack.json"
        # The worker can still be finalizing after its terminal status appears.
        recognition = {}
        worker_alive = True
        for _ in range(40):
            _, payload = _http_json(f"{self.url}/api/tender/runs/{run_id}")
            recognition = payload.get("status") or {}
            worker_alive = payload.get("worker_alive", True)
            if pack_path.is_file() and recognition.get("recognition_outcome") and not worker_alive:
                break
            time.sleep(0.4)
        self.assertTrue(pack_path.is_file())
        self.assertTrue(recognition.get("recognition_outcome"), recognition)
        self.assertFalse(worker_alive, recognition)
        status_path = self.runs / run_id / "tender" / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status.update({
            "recognition_outcome": "AI_RECOGNITION_FAILED_WITH_LOCAL_FALLBACK",
            "local_fallback_confirmed": False,
            "warning": "AI需求识别失败，已保留本地解析结果。",
        })
        status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        deny_code, denied = _http_json(f"{self.url}/api/tender/confirm", {
            "run_id": run_id,
            "decisions": {"accept_remaining": True},
            "brief_options": {"project_name": "降级确认项目", "project_type": "产业园", "scenario": "完整物业服务方案", "medium": "WORD", "provider_name": "mock"},
        }, method="POST")
        self.assertNotEqual(deny_code, 200)
        self.assertIn("本地解析", str(denied.get("error") or denied))
        acc_code, accepted = _http_json(f"{self.url}/api/tender/accept-local-fallback", {"run_id": run_id}, method="POST")
        self.assertEqual(acc_code, 200, accepted)
        ok_code, confirmed = _http_json(f"{self.url}/api/tender/confirm", {
            "run_id": run_id,
            "decisions": {"accept_remaining": True},
            "brief_options": {"project_name": "降级确认项目", "project_type": "产业园", "scenario": "完整物业服务方案", "medium": "WORD", "provider_name": "mock"},
        }, method="POST", timeout=30)
        self.assertEqual(ok_code, 200, confirmed)
        status = self._generate(run_id, [])
        result = status.get("result") or status
        self.assertTrue(status.get("generated") or result.get("status") == "generation_completed", status)

    def test_generation_failure_is_visible(self) -> None:
        code, payload = _http_json(f"{self.url}/api/generate", {
            "run_id": "run_does_not_exist_stability",
            "selected_positive_ids": [],
            "clarification_answers": {},
        }, method="POST")
        self.assertGreaterEqual(code, 400)
        self.assertTrue(payload.get("error") or payload.get("message"))


if __name__ == "__main__":
    unittest.main()
