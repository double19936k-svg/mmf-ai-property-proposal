"""Renderer must not print Python objects, empty steps, or one-line section shells."""
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

from governance.proposal_fidelity import filter_and_renumber_steps, gap_stub, prepare_paragraph, safe_unwrap_to_text


def test_dict_and_list_do_not_survive_as_source():
    shown = safe_unwrap_to_text({" ": "准备", "动作": "汇总台账", "岗位": "项目负责人"})
    assert "{'" not in shown and "准备" in shown and "项目负责人" in shown
    leaked = prepare_paragraph("1. {'': '准备', '': '汇总资料台账', '': '项目负责人'}", has_visual=False)
    assert "{'" not in leaked and "准备" in leaked
    note = safe_unwrap_to_text("表注：['台账登记项为准', '同步更新影响章节']")
    assert note.startswith("表注：") and "[" not in note
    steps = filter_and_renumber_steps(["核验现场", "", "2.", {"动作": "放行"}])
    assert steps[0].startswith("1.") and steps[1].startswith("2.")
    assert all(item.strip() not in {"1.", "2.", "3."} for item in steps)
    spliced = prepare_paragraph("专业负责 HE人按条线执行查验", has_visual=False)
    assert "HE" not in spliced and "专业负责人" in spliced


def test_outline_prompt_and_pending_phrase_are_not_printed():
    text = prepare_paragraph("进场前资料、人员、现场和工具准备，含进场准备计划表。", has_visual=False)
    assert "含进场准备计划表" not in text
    assert "尚待核实" not in prepare_paragraph("相关证明资料尚待核实。", has_visual=False)


def test_missing_section_is_not_a_one_line_shell():
    section = gap_stub("绿化养护", "S12-01")
    blob = "\n".join(section["paragraphs"])
    assert "按招标文件对应事项实施，并保留可检查记录" not in blob
    assert len(blob) >= 80
    assert section["tables"] and section["tables"][0]["rows"]
    advice = gap_stub("合理化建议", "S12-05")
    advice_blob = "\n".join(advice["paragraphs"])
    assert "建议单" not in advice_blob or "不写成" in advice_blob
    assert "痛点" in advice_blob
