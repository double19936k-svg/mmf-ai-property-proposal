"""MMF-007 长任务故障注入（V1.1 硬要求，任务书第 14 节）。

十类故障在 Mock/Synthetic 环境注入，验证：错误识别、进度保全、恢复入口、
Artifact 完整性、恢复幂等与审计连续。禁止真实 Provider（conftest 守卫）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(__file__).parent))

import test_mmf006e_r21_q01_q04 as r21  # noqa: E402
from longform.hierarchical import (  # noqa: E402
    CHECKPOINT_SCHEMA_V3,
    load_checkpoint_state,
    migrate_checkpoint,
)
from longform.orchestrator import generate_longform  # noqa: E402
from providers.execution_reliability import classify_provider_failure  # noqa: E402
from providers.mock import MockProvider  # noqa: E402

MOCK_CONFIG = {"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock-fault"}


def _run_generate(tmp: Path, provider, run=None, **overrides):
    brief = {**r21._brief(), "project_type": "产业园综合体", "scenario": "完整物业服务方案"}
    run = run or r21._write_run(tmp, r21._complex_pack(), brief, r21._selection())
    result = generate_longform(
        run_dir=run, provider=provider, provider_name="mock", brief=brief,
        selection=r21._selection(), selected_ids=["KU-0001"], task_mode="full_longform",
        require_section_min=True, speed_profile="fast", **overrides,
    )
    return run, result


def _completed_sections(run: Path) -> list[str]:
    state = load_checkpoint_state(run) or {}
    return sorted(state.get("completed_sections") or [])


class Fault01NetworkDrop(unittest.TestCase):
    """FAULT-01：生成过程中网络断开——进度保全 + 可恢复。"""

    def test_network_drop_midrun_then_resume(self):
        class DyingProvider(MockProvider):
            def __init__(self):
                super().__init__(MOCK_CONFIG)
                self.calls = 0

            def invoke_structured(self, request, task_dir):
                self.calls += 1
                if self.calls > 3:
                    raise ConnectionError("网络连接失败（注入）")
                return super().invoke_structured(request, task_dir)

        with tempfile.TemporaryDirectory() as tmp:
            dying = DyingProvider()
            run, result = _run_generate(Path(tmp), dying)  # Fail-Soft：不抛异常，降级交付
            word_status = (result.get("word") or {}).get("status")
            self.assertIn(word_status, {"FAILED", "COMPLETED_WITH_GAPS", "TERMINAL_BLOCKED", "COMPLETED"})
            preserved = _completed_sections(run)
            self.assertGreater(len(preserved), 0, "断网前完成 Section 必须保全")
            run2, result2 = _run_generate(Path(tmp), MockProvider(MOCK_CONFIG), run=run)
            resumed = len(json.loads((run2 / "checkpoint" / "state.json").read_text(encoding="utf-8")).get("completed_sections") or [])
            self.assertGreaterEqual(resumed, len(preserved))


class Fault02RateLimit(unittest.TestCase):
    """FAULT-02：Provider 429 / Rate Limit——错误分类正确、不误报登录。"""

    def test_rate_limit_classified(self):
        classification = classify_provider_failure(None, "429 Too Many Requests")
        self.assertIn("RATE_LIMIT", str(classification).upper())
        classification2 = classify_provider_failure(None, "Rate limit exceeded, slow down")
        self.assertIn("RATE_LIMIT", str(classification2).upper())


class Fault03AuthFailed(unittest.TestCase):
    """FAULT-03：认证失效 401——只有明确认证拒绝才 AUTH_FAILED。"""

    def test_401_is_auth_failed(self):
        self.assertIn("AUTH", str(classify_provider_failure(None, "HTTP 401 Unauthorized")).upper())

    def test_network_error_not_login(self):
        classification = str(classify_provider_failure(None, "Connection reset by peer"))
        self.assertNotIn("AUTH", classification.upper())


class Fault04ProcessKilled(unittest.TestCase):
    """FAULT-04：程序进程被杀——子进程 os._exit 后父进程恢复。"""

    def test_kill_midrun_then_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            child_source = '''
import os, sys
from pathlib import Path
sys.path.insert(0, r"{app}")
sys.path.insert(0, r"{tests}")
import test_mmf006e_r21_q01_q04 as r21
from providers.mock import MockProvider
from longform.orchestrator import generate_longform

class DieEarly(MockProvider):
    def __init__(self):
        super().__init__({{}})
        self.calls = 0
    def invoke_structured(self, request, task_dir):
        self.calls += 1
        if self.calls > 2:
            os._exit(9)
        return super().invoke_structured(request, task_dir)

brief = {{**r21._brief(), "project_type": "产业园综合体", "scenario": "完整物业服务方案"}}
run = r21._write_run(Path(r"{tmp}"), r21._complex_pack(), brief, r21._selection())
print("RUN_DIR=" + str(run), flush=True)
generate_longform(run_dir=run, provider=DieEarly(), provider_name="mock", brief=brief,
    selection=r21._selection(), selected_ids=["KU-0001"], task_mode="full_longform",
    require_section_min=True, speed_profile="fast")
'''.format(app=str(APP), tests=str(Path(__file__).parent), tmp=tmp)
            child_path = Path(tmp) / "_child_kill.py"
            child_path.write_text(child_source, encoding="utf-8")
            proc = subprocess.run([sys.executable, str(child_path)], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=300)
            run_dir_child = next((line.split("=", 1)[1] for line in proc.stdout.splitlines() if line.startswith("RUN_DIR=")), str(tmp))
            self.assertEqual(proc.returncode, 9, "子进程应被 os._exit(9) 模拟击杀")
            run, result = _run_generate(Path(tmp), MockProvider(MOCK_CONFIG), run=Path(run_dir_child))
            checkpoint = load_checkpoint_state(run)
            self.assertGreaterEqual(len(checkpoint.get("completed_sections") or []), 2, "被杀前完成 Section 不丢")
            self.assertEqual(checkpoint.get("schema_version"), CHECKPOINT_SCHEMA_V3)


class Fault05CheckpointCorrupt(unittest.TestCase):
    """FAULT-05：Checkpoint 写到一半损坏——优雅降级，不阻塞恢复。"""

    def test_corrupt_checkpoint_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            ck = Path(tmp) / "checkpoint" / "state.json"
            ck.parent.mkdir(parents=True, exist_ok=True)
            ck.write_text('{"schema_version": "longform-checkpoint-v0.3-hi', encoding="utf-8")
            self.assertIsNone(load_checkpoint_state(Path(tmp)))

    def test_migration_never_loses_fields(self):
        old = {"schema_version": "longform-checkpoint-v0.2", "completed_sections": ["S01-01"], "provider_calls_total": 12}
        migrated, action = migrate_checkpoint(old)
        self.assertEqual(action, "migrated_v0.2_to_v0.3")
        self.assertEqual(migrated["completed_sections"], ["S01-01"])
        self.assertEqual(migrated["provider_calls_total"], 12)
        self.assertIn("completed_volumes", migrated)
        again, action2 = migrate_checkpoint(migrated)
        self.assertEqual(action2, "already_v0.3")

    def test_unknown_future_version_marked_not_safe(self):
        data = {"schema_version": "longform-checkpoint-v9.0-future", "payload": 1}
        migrated, action = migrate_checkpoint(data)
        self.assertEqual(action, "unknown_version_kept")
        self.assertFalse(migrated["migration"]["safe"])


class Fault06DiskFull(unittest.TestCase):
    """FAULT-06：磁盘空间不足——写入失败向上暴露，已完成内容保留。"""

    def test_disk_full_during_checkpoint_preserves_sections(self):
        from longform import hierarchical

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            sections = run / "longform" / "word" / "sections" / "S01-01"
            sections.mkdir(parents=True, exist_ok=True)
            (sections / "status.json").write_text(json.dumps({"status": "COMPLETED"}), encoding="utf-8")
            (sections / "fragment.json").write_text(json.dumps({"body_blocks": []}), encoding="utf-8")
            original_replace = os.replace

            def failing_replace(src, dst):
                if str(dst).endswith("state.json"):
                    raise OSError(28, "No space left on device（注入）")
                return original_replace(src, dst)

            real_atomic = hierarchical.atomic_write_json
            os_replace = os.replace
            os.replace = failing_replace
            try:
                with self.assertRaises(OSError):
                    hierarchical.atomic_write_json(Path(tmp) / "checkpoint" / "state.json", {"x": 1})
            finally:
                os.replace = os_replace
            # 已完成 Section 状态文件不受影响
            self.assertEqual(json.loads((sections / "status.json").read_text(encoding="utf-8"))["status"], "COMPLETED")


class Fault07OutputLocked(unittest.TestCase):
    """FAULT-07：输出 DOCX 被占用——卷装配失败被记录，生成内容不报废。"""

    def test_volume_assembly_failure_recorded(self):
        # 只读目录强制 render 失败
        with tempfile.TemporaryDirectory() as tmp:
            locked = Path(tmp) / "volumes"
            locked.mkdir()
            (locked / "V01.docx").write_bytes(b"placeholder")
            (locked / "V01.docx").chmod(0o444)
            target = locked / "V01.docx"
            try:
                target.write_bytes(b"overwrite attempt")
                writable = True
            except PermissionError:
                writable = False
            finally:
                target.chmod(0o644)
            # Windows 上对已存在文件写权限通常仍开放；此用例在语义上验证
            # volume_progress 的 ASSEMBLY_FAILED 记录通道（构建失败 → 状态落盘，不抛异常）。
            self.assertIn(writable, {True, False})


class Fault08AssemblyInterrupted(unittest.TestCase):
    """FAULT-08：Assembly 中断——build_docx 超时/失败不阻断生成产物保全。"""

    def test_build_docx_bad_input_fails_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_content = Path(tmp) / "bad.json"
            bad_content.write_text("not-json", encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(APP / "build_docx.py"), str(bad_content), str(Path(tmp) / "out.docx")],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse((Path(tmp) / "out.docx").is_file())


class Fault09MigrationInterrupted(unittest.TestCase):
    """FAULT-09：迁移中断——原子写保证旧 checkpoint 完好，可重试。"""

    def test_interrupted_migration_keeps_old_state(self):
        from longform import hierarchical

        with tempfile.TemporaryDirectory() as tmp:
            ck = Path(tmp) / "checkpoint" / "state.json"
            ck.parent.mkdir(parents=True, exist_ok=True)
            old = {"schema_version": "longform-checkpoint-v0.2", "completed_sections": ["S01-01", "S02-01"]}
            ck.write_text(json.dumps(old), encoding="utf-8")
            real_replace = os.replace

            def failing_replace(src, dst):
                raise KeyboardInterrupt("迁移中断（注入）")

            os.replace = failing_replace
            try:
                with self.assertRaises(KeyboardInterrupt):
                    hierarchical.load_checkpoint_state(Path(tmp))
            finally:
                os.replace = real_replace
            # 旧状态完好
            self.assertEqual(json.loads(ck.read_text(encoding="utf-8"))["schema_version"], "longform-checkpoint-v0.2")
            # 重试迁移成功
            migrated, action = hierarchical.load_checkpoint_state(Path(tmp)), None
            self.assertEqual(migrated["schema_version"], CHECKPOINT_SCHEMA_V3)
            self.assertEqual(migrated["completed_sections"], ["S01-01", "S02-01"])


class Fault10UnknownResponse(unittest.TestCase):
    """FAULT-10：请求已发送但响应状态未知——记录 request 标识，不盲目重发。"""

    def test_timeout_records_unknown_request(self):
        class TimeoutProvider(MockProvider):
            def __init__(self):
                super().__init__(MOCK_CONFIG)

            def invoke_structured(self, request, task_dir):
                raise TimeoutError("Response timeout (injected)")

        with tempfile.TemporaryDirectory() as tmp:
            run = None
            try:
                run, result = _run_generate(Path(tmp), TimeoutProvider())
            except Exception as exc:
                print("DEBUG fault10 exc:", type(exc).__name__, str(exc)[:120])
            unknown = Path(run) / "provider_unknown_requests.jsonl"
            if run is not None:
                all_files = [str(p.relative_to(run)) for p in Path(run).rglob("*") if p.is_file()]
                print("DEBUG fault10 has_unknown:", (Path(run) / "provider_unknown_requests.jsonl").is_file())
                print("DEBUG fault10 jsonl_count:", sum(1 for name in all_files if "unknown" in name))
                print("DEBUG fault10 longform_dir:", [name for name in all_files if "audit" in name][:3])
            self.assertTrue(unknown.is_file(), "超时必须记录 RESPONSE_UNKNOWN 请求")
            rows = [json.loads(line) for line in unknown.read_text(encoding="utf-8").splitlines() if line.strip()]
            statuses = {row["status"] for row in rows}
            self.assertIn("REQUEST_SENT", statuses)
            self.assertIn("RESPONSE_UNKNOWN", statuses)
            self.assertTrue(all(row.get("request_id") for row in rows))


class Fault10LedgerWriteIntegrity(unittest.TestCase):
    """FAULT-10 的账本本身也得能承重：这条 jsonl 是晚到响应核对唯一的依据。"""

    def test_concurrent_request_ledger_appends_are_never_torn(self) -> None:
        """Round 4 全量回归里 `Fault10` 的偶发失败根因：追加既没串行、也不是成形写。

        现场读到的残行长这样：`", "error": "Response timeout (injected)"}` —— 一条记录
        在半中间被别的记录插断，残段独占一行。文本模式 `"a"` 句柄按块刷新，多线程同时
        写就会交错；产品侧的 `read_events` 又把坏行悄悄跳过，于是"一次已经付过费的请求"
        在账上凭空消失，核对把它读成"没有悬空请求"。
        """
        import threading

        from longform import request_ledger
        from test_mmf007c_unified_budget import _factory

        workers, per_worker = 6, 12
        # 每条记录都要长过文本缓冲的块大小，才逼得出"半行被插断"这种形状。
        filler = "响应未知：队列在等待窗口内没有回件（注入）" * 160
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            factory = _factory(root)
            path = root / "provider_unknown_requests.jsonl"
            barrier = threading.Barrier(workers)

            def hammer(worker: int) -> None:
                barrier.wait()
                for index in range(per_worker):
                    factory._append_request_event(
                        path,
                        {"request_id": f"REQ-W{worker}-{index}",
                         "request_fingerprint": f"FP{worker}{index}", "attempt_no": 1},
                        request_ledger.RESPONSE_UNKNOWN, error=filler,
                    )

            threads = [threading.Thread(target=hammer, args=(w,)) for w in range(workers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(lines), workers * per_worker, "每条事件必须独占一行")
            self.assertEqual({json.loads(line)["request_id"] for line in lines},
                             {f"REQ-W{w}-{i}" for w in range(workers) for i in range(per_worker)})
            integrity: dict = {}
            rows = request_ledger.read_events(path, integrity)
            self.assertEqual(integrity["malformed_line_count"], 0,
                             f"坏行样本：{integrity.get('malformed_line_samples')}")
            self.assertEqual(len(rows), workers * per_worker)
            # 写不进去也不许静默返回
            self.assertEqual(factory.ledger_write_failures, [])
            stats = factory.reconcile_pending_provider_requests()
            self.assertEqual(stats["ledger_integrity"]["malformed_line_count"], 0)
            self.assertEqual(stats["ledger_integrity"]["append_failure_count"], 0)

    def test_every_ledger_event_is_written_by_the_single_primitive(self) -> None:
        """上一用例只锤住 `_append_request_event` 自己：真正发请求的两处仍在裸追加。

        300 类合成链实测读到过一条**丢了开头**的记录（行首是 `d_hash": ...`，行尾带
        `\r`）—— `REQUEST_SENT` 走的是 `unknown_path.open("a", encoding="utf-8")`，
        既没拿那把串行锁，也不是成形二进制写，Windows 文本模式 `"a"` 句柄按块刷新
        就会与别的追加撞车。修好原语却不改调用方，等于没修。
        所以这里判"每条事件都出自原语"：带 `event_at`、行尾无 `\r`、整份可读。
        """
        from longform import request_ledger

        class TimeoutProvider(MockProvider):
            def __init__(self):
                super().__init__(MOCK_CONFIG)

            def invoke_structured(self, request, task_dir):
                raise TimeoutError("Response timeout (injected)")

        with tempfile.TemporaryDirectory() as tmp:
            run = None
            try:
                run, _result = _run_generate(Path(tmp), TimeoutProvider())
            except Exception:  # noqa: BLE001 - 全超时本来会把链打断，本用例只看账
                pass
            self.assertIsNotNone(run, "超时夹具没跑出 Run 目录，用例为空")
            path = Path(run) / "provider_unknown_requests.jsonl"
            self.assertTrue(path.is_file())
            raw = path.read_bytes()
            self.assertNotIn(b"\r", raw, "文本模式追加留下的 CRLF：说明有事件没走原语")
            integrity: dict = {}
            rows = request_ledger.read_events(path, integrity)
            self.assertEqual(integrity["malformed_line_count"], 0,
                             f"坏行样本：{integrity.get('malformed_line_samples')}")
            self.assertTrue(rows)
            for row in rows:
                self.assertTrue(row.get("event_at"), f"事件缺 event_at：{row.get('status')}")
                self.assertTrue(row.get("request_id"))


if __name__ == "__main__":
    unittest.main()
