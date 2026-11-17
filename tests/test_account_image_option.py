"""Offline checks for account image options. No Qoder, Cursor, or ZCode calls."""
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image

from providers.account_image import account_image_rows, fill_planned_scenes, _save_image


def test_image_options_follow_connected_accounts_and_skip_zcode(monkeypatch, tmp_path):
    monkeypatch.delenv("MMF_CURSOR_BRIDGE_ROOT", raising=False)
    monkeypatch.delenv("MMF_QODER_ACCOUNT_ENABLED", raising=False)
    monkeypatch.setenv("MMF_QODER_ACCOUNT_EDITION", "cn")
    assert account_image_rows() == []
    monkeypatch.setenv("MMF_QODER_ACCOUNT_ENABLED", "1")
    assert [row["provider_name"] for row in account_image_rows()] == ["qoder_account_image"]
    monkeypatch.setenv("MMF_CURSOR_BRIDGE_ROOT", str(tmp_path))
    names = [row["provider_name"] for row in account_image_rows()]
    assert names == ["qoder_account_image", "cursor_account_image"]
    assert all("zcode" not in name for name in names)
    monkeypatch.setenv("MMF_QODER_ACCOUNT_EDITION", "global")
    assert [row["provider_name"] for row in account_image_rows()] == ["cursor_account_image"]


def test_saved_account_image_is_a_local_png(tmp_path):
    workspace = tmp_path / "workspace"
    source = workspace / "vibe_images" / "scene.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (8, 8), "white").save(source)
    saved = _save_image(workspace, "vibe_images/scene.png", tmp_path / "out")
    assert saved.suffix == ".png" and saved.is_file() and saved.stat().st_size > 0
    try:
        _save_image(workspace, "../outside.png", tmp_path / "out")
    except Exception as exc:
        assert type(exc).__name__ == "ProviderUnavailableError"
    else:
        raise AssertionError("image path outside the workspace was accepted")
    outside = tmp_path / "outside.png"
    Image.new("RGB", (8, 8), "white").save(outside)
    try:
        _save_image(workspace, outside, tmp_path / "out")
    except Exception as exc:
        assert type(exc).__name__ == "ProviderUnavailableError"
    else:
        raise AssertionError("absolute image path was accepted")


def test_scene_fill_stops_at_three_and_skips_a_disconnected_account(tmp_path, monkeypatch):
    calls = []

    class Disconnected:
        config = {"provider_name": "qoder_account_image"}

        def health_check(self):
            return {"available": False}

        def generate_image(self, *args, **kwargs):
            raise AssertionError("disconnected account was asked to generate")

    slides = [{"layout": "modules", "scene_topic": "cleaning", "image": ""}]
    assert fill_planned_scenes(tmp_path / "run", {"slides": slides}, Disconnected()) is False
    assert calls == []

    class Ready:
        config = {"provider_name": "cursor_account_image"}

        def health_check(self):
            return {"available": True}

        def generate_image(self, prompt, output, *, size):
            calls.append(prompt)
            path = Path(output) / "scene.png"
            Image.new("RGB", (8, 8), "white").save(path)
            return {"ok": True, "path": str(path), "model": "generateImage"}

    monkeypatch.setattr("project_assets.save_service_scene", lambda *args, **kwargs: None)
    topics = ["cleaning", "reception", "maintenance", "energy", "handover", "gate", "concierge", "fitout", "quality"]
    artifact = {"slides": [{"layout": "modules", "scene_topic": topic, "chapter_id": f"P{index%6+1:02d}"} for index, topic in enumerate(topics)]}
    assert fill_planned_scenes(tmp_path / "ready", artifact, Ready()) is True
    assert len(calls) == 8


def test_qoder_draft_selects_imagegen_and_audits_a_real_attachment(tmp_path, monkeypatch):
    import app_core
    import first_run
    import paths

    calls = []

    class Ready:
        config = {"provider_name": "qoder_account_image"}

        def health_check(self):
            return {"available": True}

        def generate_image(self, prompt, output, *, size):
            calls.append(prompt)
            path = output / "scene.png"
            Image.new("RGB", (512, 512), "#437078").save(path)
            return {"path": str(path), "model": "ImageGen", "usage": {"credits": 0.15, "cost_usd": 0}}

    monkeypatch.setattr(app_core, "provider_manager", lambda: SimpleNamespace(providers={"qoder_account_image": Ready()}))
    monkeypatch.setattr(paths, "_CURRENT", replace(paths.current(), runs_dir=tmp_path))
    selection = {"enable_image_provider": True}
    monkeypatch.setattr(first_run, "current_settings", lambda: selection)

    def draft():
        return {"profile_p_contract": "profile-p-service-v1", "slides": [
            {"slide_id": "SL-1", "layout": "modules", "chapter_id": "P02", "scene_topic": "handover"},
            {"slide_id": "SL-2", "layout": "modules", "chapter_id": "P02", "scene_topic": "move_in"},
            {"slide_id": "SL-3", "layout": "modules", "chapter_id": "P03", "scene_topic": "reception"},
            {"slide_id": "SL-4", "layout": "modules", "chapter_id": "P04", "scene_topic": "maintenance"},
        ]}

    artifact = draft()
    run = tmp_path / "auto"
    app_core._attach_ppt_images(run, artifact, "qoder_account")
    audit = json.loads((run / "images/scene_audit.json").read_text(encoding="utf-8"))
    assert [row["topic"] for row in audit["assignments"]] == ["handover", "move_in", "reception", "maintenance"]
    assert audit["provider_calls"] == audit["request_attempts"] == len(calls) == 4
    illustrated = [s for s in artifact["slides"] if s.get("image")]
    assert all(s["image_source"] == "AI_GENERATED_SERVICE_ILLUSTRATION" and "服务场景示意" in s["image_caption"] for s in illustrated)
    app_core._attach_ppt_images(run, draft(), "qoder_account")
    selection["image_provider_name"] = "qwen_image"
    app_core._attach_ppt_images(tmp_path / "explicit", draft(), "qoder_account")
    selection.update(image_provider_name="auto", enable_image_provider=False)
    app_core._attach_ppt_images(tmp_path / "disabled", draft(), "qoder_account")
    assert len(calls) == 4


def test_setup_persists_image_selection_to_launcher_profile(tmp_path, monkeypatch):
    import first_run
    import paths

    settings_file = tmp_path / "active_settings.json"
    monkeypatch.setenv("MMF_USER_SETTINGS", str(settings_file))
    for key, name in (("MMF_DATA_ROOT", "data"), ("MMF_CONFIG_ROOT", "config"),
                      ("MMF_OUTPUT_ROOT", "output"), ("MMF_LOGS_DIR", "logs"),
                      ("MMF_RUNS_DIR", "runs"), ("MMF_TEMP_ROOT", "temp"), ("MMF_RUNTIME_DIR", "runtime")):
        monkeypatch.setenv(key, str(tmp_path / name))
    import app_core
    monkeypatch.setattr(app_core, "provider_manager", Mock(side_effect=OSError))
    with monkeypatch.context() as current:
        current.setattr(paths, "_CURRENT", None)
        assert first_run.current_settings()["image_provider_name"] == "auto"
        assert first_run.save_settings({"image_provider_name": "qoder_account_image"})["image_provider_name"] == "qoder_account_image"
        assert first_run.save_settings({"enable_image_provider": False})["enable_image_provider"] is False
        assert first_run.current_settings()["image_provider_status"] == "disabled"
        assert settings_file.is_file()


def test_cost_line_does_not_say_the_account_has_no_money():
    text = (Path(__file__).resolve().parents[1] / "static" / "account_agents.js").read_text(encoding="utf-8")
    assert "官方未提供金额" not in text
    assert "尚无最终回执，费用待核实" in text
    assert "回执未提供金额" in text
    assert "这是账户总体用量" in text
