"""MMF-007C 阶段7：Qoder 内部模型桥（BRIDGE-01..08）。

被钉住的行为：
- 只消耗 Qoder 内部额度，Provider 侧不读取任何凭据、不发起任何外部网络调用；
- 队列等待超时归入技术失败（TIMEOUT），不得伪装成质量问题；
- 停止信号下未答请求就地作废，已答内容不得丢弃；
- 正式 providers.local.json 不被写入任务作用域通道。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
APP = PACKAGE_ROOT / "app"
sys.path.insert(0, str(APP))
sys.path.insert(0, str(PACKAGE_ROOT / "tools"))

import qoder_bridge  # noqa: E402
from longform.cu_state import TECHNICAL_RETRY_CODES, is_technical_retryable  # noqa: E402
from providers import manager as provider_manager_module  # noqa: E402
from providers.base import ProviderError  # noqa: E402
from providers.network_lockdown import is_allowed_host  # noqa: E402
from providers.openai_compatible import GenerationCancelled  # noqa: E402
from providers.qoder_internal import CREDENTIAL_SOURCE, QoderInternalProvider  # noqa: E402

def _scratch_root() -> Path:
    """Round 2 §4.1 登记的 Round 3 卫生项：默认写包内 runtime/tmp 在源码树里无害
    （被 gitignore 与打包排除集双重挡住），但在便携包里跑测就会往"被验收的那份字节"里
    追加文件，于是"被验收的包"和"被跑的包"不再是同一份内容。
    数据根被显式重定向时（dist 侧 E2E）scratch 跟着一起出去，包内容保持逐文件不变。
    """
    override = os.environ.get("MMF_TEMP_ROOT")
    base = Path(override).expanduser() if override else PACKAGE_ROOT / "runtime" / "tmp"
    return base / "bridge007c"


SCRATCH_ROOT = _scratch_root()
_scratch_seq = iter(range(1, 10_000))

ENVELOPE_REPLY = (
    "<<<CU_START:CU-S01-01-01>>>\n桥头岗交接记录：白班 07:00 与夜班 19:00 双人签字确认，"
    "车辆与人员进出登记齐全，异常十分钟内上报项目负责人并留痕。\n<<<CU_END>>>\n"
)


def _root(label: str) -> Path:
    """app_core 的落盘受工作范围校验限制，写盘类测试只能落在允许根内的 scratch 目录。"""
    root = SCRATCH_ROOT / f"{label}-{os.getpid()}-{next(_scratch_seq)}"
    for folder in ("queue/out", "queue/in", "queue/ledger", "task"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    return root


def _provider(root: Path, **overrides) -> QoderInternalProvider:
    config = {
        "provider_name": "qoder_internal",
        "provider_type": "qoder_internal",
        "display_name": "Qoder 内部 Qwen3.8-Flash（文件队列）",
        "enabled": True,
        "test_mode": True,
        "queue_dir": str(root / "queue"),
        "wait_timeout": 60,
        "poll_interval": 0.02,
    }
    config.update(overrides)
    return QoderInternalProvider(config, credential_store=object())


def _request(**overrides) -> dict:
    request = {
        "task_id": "RUN-S01-01-GB-01",
        "system_prompt": "你是物业投标方案撰写引擎。",
        "prompt": "请按 CU 契约产出内容单元正文。",
        "generation_mode": "longform_batch",
        "response_protocol": "cu_envelope",
        "required_keys": ["cu_envelope_text"],
        "attempt_mode": "generate_batch",
        "batch_id": "GB-01",
        "section_ids": ["S01-01"],
        "content_unit_contracts": [{"content_unit_id": "CU-S01-01-01", "archetype": "NARRATIVE"}],
    }
    request.update(overrides)
    return request


def _answer_first_request(root: Path, payload_for, *, deadline: float = 90.0) -> tuple[threading.Thread, dict]:
    """后台模拟执行者取单：全量回归时机器负载高，等待窗口必须宽松。"""
    seen: dict = {}

    def waiter() -> None:
        stop_at = time.monotonic() + deadline
        while time.monotonic() < stop_at:
            rows = sorted((root / "queue" / "out").glob("*.request.json"))
            if rows:
                # 替身必须与真实执行者同一读取姿态：`tools/qoder_bridge._load` 与产品侧
                # `_read_json` 都是"读不到就下一轮再读"，因为 Windows 上 `os.replace` 落地的
                # 一瞬间并发读者会拿到 PermissionError [WinError 5]。这里不容忍，负载高的整轮
                # 回归就会被这个窗口判成"执行者未在 60 秒内回队"（Round 4 dist E2E 实测）。
                try:
                    seen["request"] = json.loads(rows[0].read_text(encoding="utf-8-sig"))
                except (OSError, json.JSONDecodeError):
                    time.sleep(0.02)
                    continue
                seen["job_id"] = str(seen["request"].get("job_id") or rows[0].name.split(".")[0])
                (root / "queue" / "in" / f"{seen['job_id']}.response.json").write_text(
                    json.dumps(payload_for(seen), ensure_ascii=False), encoding="utf-8"
                )
                return
            time.sleep(0.02)

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    return thread, seen


def _ledger(root: Path) -> list[dict]:
    path = root / "queue" / "ledger" / "calls.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def _job_id_of(request_path: Path) -> str:
    return json.loads(request_path.read_text(encoding="utf-8-sig"))["job_id"]


class BridgeNoCredentialTests(unittest.TestCase):
    """BRIDGE-01：桥接通道没有凭据概念，也不能触网。"""

    def test_credential_store_is_dropped(self):
        class ExplodingStore:
            def get(self, *args, **kwargs):  # pragma: no cover - 被调用即失败
                raise AssertionError("qoder_internal 不得读取凭据库")

            def credential_ref(self, *args, **kwargs):  # pragma: no cover
                raise AssertionError("qoder_internal 不得登记 credential_ref")

        base = _provider(_root("nokey"))
        guarded = QoderInternalProvider(base.config, credential_store=ExplodingStore())
        self.assertIsNone(guarded.credential_store)
        self.assertTrue(guarded.health_check()["available"])
        meta = guarded.get_metadata()
        self.assertEqual(meta["credential_source"], CREDENTIAL_SOURCE)
        self.assertEqual(meta["external_network_calls"], 0)
        self.assertFalse(meta["fallback_used"])
        self.assertFalse(base.config.get("api_key_env"))

    def test_lockdown_blocks_external_hosts_only(self):
        self.assertTrue(is_allowed_host("127.0.0.1"))
        self.assertTrue(is_allowed_host("localhost"))
        self.assertTrue(is_allowed_host("::1"))
        self.assertFalse(is_allowed_host("dashscope.aliyuncs.com"))
        self.assertFalse(is_allowed_host("api.moonshot.cn"))

    def test_lockdown_installed_in_subprocess_rejects_external_connect(self):
        script = (
            "import socket,sys;sys.path.insert(0,r'%s');"
            "from providers import network_lockdown;"
            "print(network_lockdown.install());"
            "socket.create_connection(('dashscope.aliyuncs.com',443),timeout=1)" % (APP,)
        )
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "MMF_PROVIDER_NETWORK_LOCKDOWN": "1", "PYTHONPATH": str(APP)},
        )
        self.assertIn("UNEXPECTED_REAL_PROVIDER_CALL", proc.stderr)
        self.assertNotIn("Traceback (most recent call last):", proc.stdout)
        self.assertIn("{'installed': True", proc.stdout)


class BridgeRoundTripTests(unittest.TestCase):
    """BRIDGE-02/03/04：信封回传、超时归技术失败、Worker 错误原样上报。"""

    def test_envelope_round_trip_preserves_contract(self):
        root = _root("roundtrip")
        provider = _provider(root)
        task_dir = root / "task" / "call1"
        thread, collector = _answer_first_request(root, lambda seen: {
            "job_id": seen["job_id"], "worker": "qoder-test-a", "status": "OK", "body": ENVELOPE_REPLY,
        })
        output = provider.invoke_structured(_request(), task_dir)
        thread.join(timeout=90)
        sent = collector["request"]
        self.assertEqual(sent["prompt"], "请按 CU 契约产出内容单元正文。")
        self.assertEqual(sent["response_protocol"], "cu_envelope")
        self.assertEqual(sent["content_unit_ids"], ["CU-S01-01-01"])
        self.assertEqual(sent["section_ids"], ["S01-01"])
        self.assertEqual(sent["attempt_mode"], "generate_batch")
        self.assertIn("CU_START", output["cu_envelope_text"])
        self.assertEqual(output["provider_metadata"]["transport"], "qoder_internal_file_queue")
        self.assertEqual(output["provider_metadata"]["credential_source"], CREDENTIAL_SOURCE)
        self.assertEqual(output["provider_metadata"]["worker"], "qoder-test-a")
        self.assertTrue((task_dir / "provider_structured_output.json").is_file())
        self.assertTrue((task_dir / "provider_audit.json").is_file())
        events = [row["event"] for row in _ledger(root)]
        self.assertEqual(events, ["REQUEST_ENQUEUED", "RESPONSE_RECEIVED"])
        received = [row for row in _ledger(root) if row["event"] == "RESPONSE_RECEIVED"][0]
        self.assertEqual(received["model"], "qwen3.8-flash (Qoder internal free quota)")
        self.assertGreater(received["response_chars"], 0)
        # 回单必须被消费掉，不能在队列里留下第二次读取的余地。
        self.assertEqual(list((root / "queue" / "in").glob("*.response.json")), [])
        self.assertEqual(list((root / "queue" / "out").glob("*.request.json")), [])

    def test_queue_wait_timeout_is_a_technical_failure(self):
        root = _root("timeout")
        provider = _provider(root, wait_timeout=0.4)
        with self.assertRaises(ProviderError) as caught:
            provider.invoke_structured(_request(task_id="RUN-TIMEOUT-01"), root / "task" / "t1")
        self.assertIn(caught.exception.error_code, TECHNICAL_RETRY_CODES)
        self.assertTrue(is_technical_retryable(caught.exception.error_code))
        self.assertEqual(caught.exception.error_code, "TIMEOUT")
        events = {row["event"] for row in _ledger(root)}
        self.assertIn("REQUEST_TIMED_OUT", events)
        self.assertEqual(list((root / "queue" / "out").glob("*.request.json")), [])
        self.assertTrue(list((root / "queue" / "abandoned").glob("*.TIMEOUT.request.json")))

    def test_worker_error_keeps_its_own_error_code(self):
        root = _root("workererror")
        provider = _provider(root)
        thread, _seen = _answer_first_request(root, lambda seen: {
            "job_id": seen["job_id"], "status": "ERROR", "error_code": "RATE_LIMIT", "error": "内部额度限流",
        })
        with self.assertRaises(ProviderError) as caught:
            provider.invoke_structured(_request(task_id="RUN-RATE-01"), root / "task" / "t2")
        thread.join(timeout=90)
        self.assertEqual(caught.exception.error_code, "RATE_LIMIT")
        self.assertIn("REQUEST_ENQUEUED", {row["event"] for row in _ledger(root)})
        self.assertEqual([row for row in _ledger(root) if row["event"] == "WORKER_ERROR"][0]["error_code"], "RATE_LIMIT")

    def test_empty_worker_body_is_empty_response(self):
        root = _root("emptybody")
        provider = _provider(root)
        thread, _seen = _answer_first_request(root, lambda seen: {"job_id": seen["job_id"], "status": "OK", "body": ""})
        with self.assertRaises(ProviderError) as caught:
            provider.invoke_structured(_request(task_id="RUN-EMPTY-01"), root / "task" / "t3")
        thread.join(timeout=90)
        self.assertEqual(caught.exception.error_code, "EMPTY_RESPONSE")


class BridgeStopTests(unittest.TestCase):
    """BRIDGE-05：停止信号下未答请求作废，链路立即回到可存稿状态。"""

    def test_stop_sentinel_prevents_new_request(self):
        root = _root("stop")
        provider = _provider(root, wait_timeout=6)
        (root / "queue" / "STOP").write_text("{}", encoding="utf-8")
        with self.assertRaises(GenerationCancelled):
            provider.invoke_structured(_request(task_id="RUN-STOP-01"), root / "task" / "t4")
        events = {row["event"] for row in _ledger(root)}
        self.assertNotIn("REQUEST_ENQUEUED", events)
        self.assertFalse(list((root / "queue" / "out").glob("*.request.json")))

    def test_stop_after_enqueue_preserves_abandoned_request(self):
        root = _root("inflight-stop")
        provider = _provider(root, wait_timeout=6)
        def cancel_pending():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if list((root / "queue" / "out").glob("*.request.json")):
                    (root / "queue" / "STOP").write_text("{}", encoding="utf-8")
                    return
                time.sleep(.01)
        thread = threading.Thread(target=cancel_pending, daemon=True)
        thread.start()
        with self.assertRaises(GenerationCancelled):
            provider.invoke_structured(_request(task_id="RUN-STOP-PENDING"), root / "task" / "pending")
        thread.join(timeout=6)
        self.assertIn("REQUEST_CANCELLED", {row["event"] for row in _ledger(root)})
        self.assertTrue(list((root / "queue" / "abandoned").glob("*.CANCELLED.request.json")))

    def test_late_answer_after_stop_is_not_silently_dropped(self):
        root = _root("late")
        provider = _provider(root, wait_timeout=0.3)
        with self.assertRaises(ProviderError):
            provider.invoke_structured(_request(task_id="RUN-LATE-01"), root / "task" / "t5")
        rows = sorted((root / "queue" / "abandoned").glob("*.request.json"))
        self.assertTrue(rows)
        abandoned = json.loads(rows[0].read_text(encoding="utf-8-sig"))
        self.assertTrue(abandoned["job_id"].startswith("RUN-LATE-01"))
        # 作废后的请求文件仍在磁盘上，迟到回单可以被审计出来而不是凭空消失。
        self._late_answer(root, abandoned["job_id"])
        self.assertTrue((root / "queue" / "in" / f"{abandoned['job_id']}.response.json").is_file())

    def _late_answer(self, root: Path, job_id: str) -> None:
        (root / "queue" / "in" / f"{job_id}.response.json").write_text(
            json.dumps({"job_id": job_id, "status": "OK", "body": ENVELOPE_REPLY}, ensure_ascii=False), encoding="utf-8"
        )


class BridgeConfigIsolationTests(unittest.TestCase):
    """BRIDGE-06/07：任务作用域通道只存在于进程内，不写正式配置文件。"""

    def setUp(self):
        self._saved_namespace = os.environ.get("MMF_CREDENTIAL_NAMESPACE")

    def tearDown(self):
        # 不污染同一进程内后续测试的凭据命名空间。
        if self._saved_namespace is None:
            os.environ.pop("MMF_CREDENTIAL_NAMESPACE", None)
        else:
            os.environ["MMF_CREDENTIAL_NAMESPACE"] = self._saved_namespace

    def _manager(self, cfg: Path):
        example = APP.parent / "config" / "providers.example.json"
        if not example.is_file():
            example = APP / "config" / "providers.example.json"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "providers.example.json").write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        # 凭据命名空间隔离：本测试不得触碰 Todd 在系统凭据库里的真实 Provider 凭证。
        os.environ["MMF_CREDENTIAL_NAMESPACE"] = "mmf_bridge_test"
        return provider_manager_module.ProviderManager(APP.parent, config_dir=cfg)

    def test_provider_type_is_registered(self):
        self.assertIs(provider_manager_module.PROVIDER_TYPES.get("qoder_internal"), QoderInternalProvider)

    def test_task_scoped_row_is_not_persisted(self):
        root = _root("config")
        cfg = root / "config"
        previous = os.environ.get(provider_manager_module.TASK_SCOPED_PROVIDER_ENV)
        os.environ[provider_manager_module.TASK_SCOPED_PROVIDER_ENV] = str(root / "queue")
        try:
            manager = self._manager(cfg)
            self.assertIn("qoder_internal", manager.providers)
            stored = json.loads((cfg / "providers.local.json").read_text(encoding="utf-8-sig"))
            self.assertNotIn("qoder_internal", {row.get("provider_name") for row in stored.get("available_providers", [])})
            status = {row["provider_name"]: row for row in manager.list_status()}
            self.assertTrue(status["qoder_internal"]["available"])
            self.assertTrue(status["qoder_internal"]["test_mode"])
            self.assertEqual(status["qoder_internal"]["connection_status"], "test_mode")
        finally:
            if previous is None:
                os.environ.pop(provider_manager_module.TASK_SCOPED_PROVIDER_ENV, None)
            else:
                os.environ[provider_manager_module.TASK_SCOPED_PROVIDER_ENV] = previous
        without = self._manager(root / "config2")
        self.assertNotIn("qoder_internal", without.providers)

    def test_capability_probe_never_enters_the_queue(self):
        root = _root("probe")
        provider = _provider(root, wait_timeout=1)
        output = provider.invoke_structured(
            {"task_id": "mmf-capability-probe", "purpose": "capability_probe", "generation_mode": "capability_probe",
             "prompt": "ping", "system_prompt": "", "required_keys": ["probe", "ok"]},
            root / "task" / "probe",
        )
        self.assertIs(output.get("probe"), True)
        self.assertIs(output.get("ok"), True)
        self.assertEqual(list((root / "queue" / "out").glob("*.request.json")), [])
        self.assertEqual(_ledger(root), [])


class BridgeToolingTests(unittest.TestCase):
    """BRIDGE-08：取单/回单工具与统计口径。"""

    def _run(self, root: Path, argv: list[str]) -> str:
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = qoder_bridge.main(["--queue", str(root / "queue"), "--create"] + argv)
        self.assertIn(code, (0, 2))
        return buffer.getvalue()

    def test_claim_submit_and_stats_flow(self):
        root = _root("cli")
        queue = root / "queue"
        for folder in ("out", "claims", "done", "in", "ledger"):
            (queue / folder).mkdir(parents=True, exist_ok=True)
        (queue / "out" / "RUN-A.request.json").write_text(
            json.dumps({"job_id": "RUN-A", "task_id": "RUN-A", "prompt": "正文", "system_prompt": "系统", "created_at": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
        pending = self._run(root, ["pending"])
        self.assertIn("RUN-A", pending)
        self._run(root, ["claim", "RUN-A", "--worker", "qoder-w1"])
        self.assertFalse((queue / "out" / "RUN-A.request.json").exists())
        self.assertTrue((queue / "claims" / "RUN-A.request.json").is_file())
        answer = queue / "answer.txt"
        answer.write_text(ENVELOPE_REPLY, encoding="utf-8")
        self._run(root, ["submit", "RUN-A", "--worker", "qoder-w1", "--file", str(answer)])
        self.assertTrue((queue / "done" / "RUN-A.request.json").is_file())
        body = json.loads((queue / "in" / "RUN-A.response.json").read_text(encoding="utf-8-sig"))
        self.assertIn("CU_START", body["body"])
        self.assertEqual(body["worker"], "qoder-w1")
        # 迟到回单：请求已不在队列，必须留痕而不是静默吞掉。
        late = queue / "late_answer.txt"
        late.write_text("<<<CU_START:X>>>\n迟到\n<<<CU_END>>>", encoding="utf-8")
        self._run(root, ["submit", "RUN-MISSING", "--worker", "qoder-w1", "--file", str(late)])
        self.assertTrue(list((queue / "late").glob("RUN-MISSING.response.json")))
        stats = json.loads(self._run(root, ["stats"]))
        self.assertEqual(stats["answered"], 1)
        self.assertEqual(stats["late_responses"], 1)
        self.assertFalse(stats["stop_sentinel"])
        self._run(root, ["stop"])
        self.assertTrue((queue / "STOP").is_file())
        self._run(root, ["resume"])
        self.assertFalse((queue / "STOP").is_file())
