"""Cross-mode preservation and fail-soft checks for the limited integration."""
import json
from pathlib import Path

import pytest

from test_ppt_bounded_delivery import factory, payload
from planning.ppt_service import VERSION, content_qa, issues
from longform.factory import evaluate_ppt_payload
from longform.orchestrator import run_document_level_qa


def service_slide(sid="SL-001"):
    return {"slide_id": sid, "title": "企业服务安排", "core_message": "说明来访办理与结果通知。",
            "profile_p_contract": VERSION, "layout": "modules",
            "modules": [{"title": "来访接待", "body": "核对来访需求并引导至企业。"},
                        {"title": "结果告知", "body": "向企业联系人说明处理结果。"}]}


def test_editorial_title_does_not_resend_or_drop_generated_page(tmp_path):
    calls = []
    def invoke(request, unit):
        calls.append(request["slide_id"])
        result = payload(request["slide_id"])
        result["content_blocks"][0]["title"] = "核对企业来访预约并说明具体接待办理安排"
        return result, {"status": "SUCCESS"}
    f = factory(tmp_path, 3, 1, invoke)
    result = f.generate_ppt()
    assert result["completed_slides"] == 3 and len(calls) == 3
    gate = json.loads((f.ppt_root / "slides/SL-001/governance.json").read_text(encoding="utf-8"))
    assert gate["status"] == "WARNING"
    assert any(x["severity"] == "NEEDS_REVIEW" for x in gate["profile_p_findings"])
    assert (f.ppt_root / "slides/SL-001/payload.json").is_file()


def test_review_only_findings_keep_complete_count_and_ppt_mode_qa(tmp_path):
    left, right = service_slide(), service_slide("SL-002")
    left["modules"][0]["body"] = right["modules"][0]["body"] = "与企业确认使用范围和现场联系人后按约定时间联合查验。"
    artifact = {"slides": [left, right]}
    plan = {"profile_p_contract": VERSION, "slide_storyboard": [{"slide_id": s["slide_id"]} for s in artifact["slides"]]}
    check = content_qa(artifact, plan)
    assert check["status"] == "NEEDS_REVIEW" and check["complete_content_slides"] == 2
    assert any(f["rule_id"] == "PPT-P-DUPLICATE" for f in check["findings"])
    result = run_document_level_qa(tmp_path, brief={}, bundle={"ppt_plan": plan}, contracts={},
                                  mode="production", medium="PPT", chosen=None,
                                  fragments={}, gates=[], artifact=artifact)
    assert result["depth"]["status"] == result["consistency"]["status"] == "NEEDS_REVIEW"
    assert result["depth"]["completed_content_units"] == 2
    assert result["budget_quality"]["status"] == "NOT_APPLICABLE"


@pytest.mark.parametrize("defect", ["missing_title", "empty_action", "missing_slide", "wrong_identity"])
def test_real_missing_content_and_wrong_identity_still_block(defect):
    slide = service_slide()
    if defect == "missing_title":
        slide["modules"][0]["title"] = ""
    elif defect == "empty_action":
        slide["modules"][0]["body"] = ""
    if defect == "wrong_identity":
        result = evaluate_ppt_payload(payload("SL-002"),
                                      {"slide_id": "SL-001", "layout_intent": "modules", "profile_p_contract": VERSION}, {})
        assert result["status"] == "BLOCK"
        return
    check = content_qa({"slides": [] if defect == "missing_slide" else [slide]},
                       {"slide_storyboard": [{"slide_id": "SL-001"}]})
    assert check["status"] == "BLOCK" and check["complete_content_slides"] == 0


def test_page_exception_preserves_other_pages_and_requires_local_recovery(tmp_path):
    calls = []
    def invoke(request, unit):
        sid = request["slide_id"]
        calls.append(sid)
        if sid == "SL-002":
            raise ValueError("synthetic page-local decoding error")
        return payload(sid), {"status": "SUCCESS"}
    f = factory(tmp_path, 3, 1, invoke)
    result = f.generate_ppt()
    assert result["status"] == "COMPLETED_WITH_GAPS" and result["completed_slides"] == 2
    good_path = f.ppt_root / "slides/SL-001/payload.json"
    before = good_path.read_bytes()
    error = json.loads((f.ppt_root / "slides/SL-002/page_error.json").read_text(encoding="utf-8"))
    assert error["error_type"] == "ValueError" and error["requires_targeted_recovery"]
    f.generate_ppt()
    assert calls == ["SL-001", "SL-002", "SL-003"] and good_path.read_bytes() == before
    f._invoke = lambda request, unit: (payload(request["slide_id"]), {"status": "SUCCESS"})
    assert f.generate_ppt(["SL-002"])["completed_slides"] == 1
    assert f.generate_ppt()["completed_slides"] == 3
    assert good_path.read_bytes() == before and (f.ppt_root / "slides/SL-002/page_error.json").is_file()


def test_repeated_page_exception_cannot_reset_attempt_limit(tmp_path):
    calls = []
    def invoke(request, unit):
        calls.append(request["slide_id"])
        raise RuntimeError("synthetic worker error")
    f = factory(tmp_path, 1, 1, invoke)
    f.generate_ppt()
    f.generate_ppt(["SL-001"])
    f.generate_ppt(["SL-001"])
    assert len(calls) == 2
    assert not (f.ppt_root / "slides/SL-001/payload.json").exists()


def test_corrupt_page_checkpoint_does_not_abort_healthy_pages(tmp_path):
    f = factory(tmp_path, 3, 1)
    unit = f.ppt_root / "slides/SL-002"
    unit.mkdir(parents=True)
    (unit / "status.json").write_text("{broken", encoding="utf-8")
    result = f.generate_ppt()
    assert result["completed_slides"] == 2 and result["status"] == "COMPLETED_WITH_GAPS"
    error = json.loads((unit / "page_error.json").read_text(encoding="utf-8"))
    assert error["error_type"] == "JSONDecodeError"
    assert all((f.ppt_root / f"slides/{sid}/payload.json").is_file() for sid in ("SL-001", "SL-003"))


def test_step_count_difference_is_review_without_ignoring_empty_steps():
    slide = service_slide()
    slide.update(layout="process", expected_steps=3, steps=slide.pop("modules"))
    finding = next(f for f in issues(slide) if f["rule_id"] == "PPT-P-STEP-COUNT")
    assert finding["severity"] == "NEEDS_REVIEW"
    slide["steps"][0]["body"] = ""
    assert any(f["rule_id"] == "PPT-P-NODE-BODY" and f["severity"] == "BLOCK" for f in issues(slide))
