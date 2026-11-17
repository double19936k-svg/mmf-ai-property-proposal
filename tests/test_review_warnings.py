from governance.review_warnings import false_review_fragment, prepare_review_issues


def test_zero_consistency_findings_are_not_warnings():
    rows = prepare_review_issues([
        {"issue": "一致性检查 NEEDS_REVIEW：关键事实核查存在未解决问题（0 项）。", "severity": "NEEDS_REVIEW"},
        {"issue": "一致性检查 NEEDS_REVIEW：关键事实核查存在未解决问题（2 项）。", "text": "面积不一致", "severity": "NEEDS_REVIEW"},
    ])
    assert len(rows) == 1
    assert "0 项" not in rows[0]["display"]
    assert "面积不一致" in rows[0]["display"]


def test_range_and_coordination_are_not_sentence_fragments():
    assert false_review_fragment({"text": "15天", "sentence": "第1—15天", "path": "artifact.slides[4].table.rows[0][0]"})
    assert false_review_fragment({"text": "45天", "sentence": "第16—45天"})
    assert false_review_fragment({"text": "访客证", "sentence": "发放临时通行码或访客证；"})
    assert not false_review_fragment({
        "text": "每天清扫三次",
        "sentence": "大堂由保洁每天清扫三次，并留下完成记录。",
        "path": "artifact.slides[2].modules[0].body",
    })


def test_short_label_column_is_exempt_and_located():
    artifact = {"profile_p_contract": "profile-p-service-v1", "chapters": [{"chapter_id": "P02", "title": "接管"}],
                "slides": [{"chapter_id": "P02", "title": "接管后首90天建议计划",
                            "table": {"columns": ["阶段", "安排"], "rows": [["第1—15天", "核对遗留问题。"]]}}]}
    hidden = prepare_review_issues([
        {"text": "15天", "sentence": "第1—15天", "path": "artifact.slides[0].table.rows[0][0]"},
    ], artifact=artifact, medium="PPT")
    assert hidden == []
    shown = prepare_review_issues([
        {"text": "未写责任岗位", "sentence": "核对遗留问题。", "path": "artifact.slides[0].table.rows[0][1]"},
    ], artifact=artifact, medium="PPT")
    assert shown[0]["slide_index"] == 4
    assert shown[0]["slide_title"] == "接管后首90天建议计划"
    assert shown[0]["component_label"] == "表格第2行第2列[安排]"
    assert shown[0]["display"].startswith("【第 4 页《接管后首90天建议计划》· 表格第2行第2列[安排]】")


def test_word_review_uses_section_location_without_touching_generation():
    artifact = {"sections": [{"heading": "秩序维护", "paragraphs": ["甲", "发放临时通行码或访客证；"]}]}
    rows = prepare_review_issues([
        {"text": "访客证", "sentence": "发放临时通行码或访客证；", "path": "artifact.sections[0].paragraphs[1]"},
        {"text": "岗位未写清", "sentence": "夜间巡查由秩序岗完成并记录。", "path": "artifact.sections[0].paragraphs[1]"},
    ], artifact=artifact, medium="WORD")
    assert len(rows) == 1
    assert rows[0]["display"].startswith("【第 1 章《秩序维护》· 第 2 段】")
