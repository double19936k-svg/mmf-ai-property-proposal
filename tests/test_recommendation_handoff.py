"""Knowledge recommendation stays on the current run and does not consume the writing budget."""
import json
from dataclasses import replace

import pytest

import paths
from account_budget import word_cap
from providers.base import ProviderError, write_json


def test_recommendation_receipt_does_not_shrink_the_writing_budget(tmp_path, monkeypatch):
    roots = replace(paths.current(), runs_dir=tmp_path / "runs", runtime_dir=tmp_path / "runtime")
    monkeypatch.setattr(paths, "current", lambda: roots)
    write_json(roots.runs_dir / "r1" / "brief.json", {
        "medium": "WORD", "provider_name": "qoder_account", "document_scale": "regular_solution",
    })
    jobs = tmp_path / "jobs"
    write_json(jobs / "rec.json", {
        "task_id": "r1-recommend", "status": "returned",
        "generation_mode": "knowledge_recommendation", "budget_scope": "recommendation",
    })
    assert word_cap(jobs, "r1", "qmodel_latest") == 32


def test_unsent_recommendation_accepts_the_current_request(tmp_path, monkeypatch):
    import account_tasks as tasks

    roots = replace(paths.current(), runs_dir=tmp_path / "runs", runtime_dir=tmp_path / "runtime")
    monkeypatch.setattr(paths, "current", lambda: roots)
    sent = []

    class Control:
        root = tmp_path / "ctl"
        selection = {"model": "qmodel_latest"}

        def execute(self, request, task_dir, cancelled):
            sent.append(request["prompt"])
            if len(sent) == 1:
                raise ProviderError("本轮已绑定原模型，请恢复原模型或新建任务。", error_code="QODER_MODEL_LOCKED")
            return {"body": json.dumps({"ok": True}), "is_error": False, "session_id": "s", "digest": "x"}

        def event(self, *args, **kwargs):
            return None

    request = {
        "run_id": "r1", "task_id": "r1-recommend", "request_id": "r1-recommend",
        "prompt": "first", "system_prompt": "s", "required_keys": ["ok"],
        "generation_mode": "knowledge_recommendation",
    }
    with pytest.raises(ProviderError) as blocked:
        tasks.execute("qoder_account", request, tmp_path / "task", Control())
    assert blocked.value.error_code == "QODER_MODEL_LOCKED"
    recovered = tasks.execute("qoder_account", {**request, "prompt": "second"}, tmp_path / "task", Control())
    assert json.loads(recovered["body"])["ok"] is True
    assert sent == ["first", "second"]
    with pytest.raises(ProviderError) as changed:
        tasks.execute("qoder_account", {**request, "prompt": "third"}, tmp_path / "task", Control())
    assert changed.value.error_code == "ACCOUNT_TASK_IDENTITY"
