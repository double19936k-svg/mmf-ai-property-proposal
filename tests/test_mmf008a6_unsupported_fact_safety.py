# -*- coding: utf-8 -*-
"""MMF008A6：条件作用域绑定 + 无依据事实安全 —— 最小陌生反例（任务书 §3）。

这一支**不吃复核方那 24 条**（那是 §6 的 adjudicated regression，另跑），也不建新语料。
要证的只有一件事：本轮修的是**关系**，不是那几句被点名的话。所以每条都是陌生内容
（别的场所、别的设备、别的写法），B1 十二、B2 十二，各验四件事：

B1 —— 条件框架的作用域必须绑到 clause / claim：
  * 框架词被 的 / 是否 名词化时不建立框架（「假设的合理性仍需讨论」那种）；
  * 框架只支配自己那一截 + 读作后半句的那些小句；
  * 独立命题（句末、分号、"另外/同时/实际上"重新起手、或没有结果标记的裸直陈）
    一出现就终止前一框架，其后的既存事实照旧硬 BLOCK；
  * 真条件句（前半句框架 + 后半句 则/方可/将/拟）继续放行，且判不出证据时**fail closed**。

B2 —— 无来源的既存事实不得变成无来源的新义务：
  * 拿不到底稿：两族都不动文本，门槛照旧挡（不动 ≠ 放行）；
  * 有底稿但底稿没写这项义务：只做现状核验边界，产物里不许出现 将/拟/计划/按合同要求；
  * 底稿里逐字有这项义务：才允许将来语态，且 `obligation_source` 必须逐字回指底稿；
  * 中和必须**落到断言那一头**：改完仍读得出直陈就不改（不留"看着处理过、其实照样挡"的话）；
    业务原子（场所名、设备名、动作）一个字不少，改后幂等、无标点残片。

零 Provider 调用；只读生产码，不落任何产物。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
for path in (str(APP_ROOT), str(APP_ROOT / "app")):
    if path not in sys.path:
        sys.path.insert(0, path)

from governance.commitment_provenance import (  # noqa: E402
    DISPOSITION_BOUNDARY, DISPOSITION_BOUND_OBLIGATION, DISPOSITION_HOLD_WITHOUT_EVIDENCE,
    DISPOSITION_KEEP_FACT, FORMAL_RULES, HARD_BLOCK_PROVENANCE_CLASS,
    VERIFICATION_BOUNDARY_TAIL, apply_local_repairs, unsupported_fact_disposition,
    evaluate_commitments)
from qa_severity import (  # noqa: E402
    _hypothesis_frame, condition_governed_spans, is_conditioned, semantic_status)

FRAGMENT_PATTERNS = ("，。", "；。"  , "、。", "。，", "：。", "，；", "，，", "；；")
# 「无依据的事实被换成无依据的义务」的可见形状：这些语态一旦出现就是 HARD FAIL。
# `拟` 与 `将` 都是汉语语素，裸匹配会把「虚**拟**／模**拟**／**将**来」当成将来语态——
# 本轮的取证探针在 before 读数上就因这个少算过两条，所以这里带词界守卫（与量具同一条尺）。
OBLIGATION_PATTERN = re.compile(
    r"(?<![虚模比协])拟(?!定|稿)|(?<![未即])将(?![来士帅])|计划|中标后|按合同要求|按合同约定")
STATE_CLAIM_MARK = "已"


def gate(text: str, brief: dict | None = None) -> dict:
    return evaluate_commitments(brief or {}, [], {"artifact": {"paragraphs": [text]}})


def blocked(text: str, brief: dict | None = None) -> list[str]:
    return [str(row["text"]) for row in gate(text, brief)["violations"]]


def span_of(text: str, phrase: str) -> tuple[int, int]:
    start = text.index(phrase)
    return start, start + len(phrase)


def repair(text: str, brief: dict | str | None = None) -> str:
    payload = apply_local_repairs({"artifact": {"paragraphs": [text]}}, brief=brief if brief else {})
    return str((payload.get("artifact") or {}).get("paragraphs")[0])


class B1ConditionFrameGovernsOnlyItsSpan(unittest.TestCase):
    """§B1.1：条件框架 = 词性成立的那一截 + 它实际支配的那几小句，不是"句首到句末"。"""

    def test_b1_01_genitive_nominalisation_opens_no_frame(self) -> None:
        self.assertTrue(blocked("假设的合理性仍需讨论，行李寄存间已加装温湿度记录仪。"),
                        "「假设的」是被谈论的名词，不能把后半句既存事实洗成条件")
        self.assertFalse(_hypothesis_frame("假设的合理性仍需讨论，"))

    def test_b1_02_a_not_a_nominalisation_opens_no_frame(self) -> None:
        self.assertTrue(blocked("前提是否成立尚待甲方书面回复，屋面设备房已安装防坠装置。"),
                        "「前提是否成立」里 前提 是被提问的主语，不是框架引导字")
        self.assertFalse(_hypothesis_frame("前提是否成立尚待核验，"))

    def test_b1_03_hypothesis_noun_as_subject_still_not_a_frame(self) -> None:
        """上一轮的位置判据不许回归：名词主语的「上述假定」同样不算框架。"""
        self.assertTrue(blocked("上述假定未经核验，二级配电室已加装弧光保护装置。"))
        self.assertFalse(_hypothesis_frame("上述假定未经核验，"))

    def test_b1_04_discourse_reset_terminates_the_frame(self) -> None:
        self.assertTrue(blocked("假定夜间不安排大件搬运，则调整巡查班次，"
                                "另外行李寄存间已加装温湿度记录仪。"),
                        "「另外」另起命题，前一框架管不到它")

    def test_b1_05_new_subject_with_existing_state_terminates_the_frame(self) -> None:
        self.assertTrue(blocked("万一移交图纸延后，则拟压缩勘验时间，"
                                "同时泵房已更换低噪机组。"),
                        "新主语 + 既存谓语就是独立命题，与有没有连接词无关")

    def test_b1_06_semicolon_and_full_stop_are_hard_boundaries(self) -> None:
        self.assertTrue(blocked("设想错峰作业可行，则拟分班推进；监控中心已加装门禁读卡器。"))
        self.assertTrue(blocked("如果甲方确需夜间清运，则拟调整车辆安排。值班室已建设备用供电机组。"))

    def test_b1_07_bare_indicative_clause_is_not_an_apodosis(self) -> None:
        """没有任何非现实标记的那一截就是独立断言：判不出支配证据时按直陈走（fail closed）。"""
        self.assertTrue(blocked("假定错峰清运可行，垃圾站已改造完成并交付使用。"))

    def test_b1_08_postposed_frame_never_governs_retroactively(self) -> None:
        self.assertTrue(blocked("垃圾站已改造完成，若甲方另有要求则再调整。"))

    def test_b1_09_real_apodosis_chain_stays_governed(self) -> None:
        for text in ("假定管线图移交延后，则 30 分钟到场口径需现场勘验后方可排定具体点位。",
                     "若确认高低两区需共用搬运通道，则交叉作业时段的避让安排将另附专项方案。",
                     "一旦甲方书面确认开放时段，则清运班次并另行编排。"):
            with self.subTest(text=text):
                self.assertEqual([], blocked(text), "真条件句的后半句仍归框架支配")

    def test_b1_10_claim_inside_the_frame_clause_itself_is_governed(self) -> None:
        """框架自己那一截里出现的「已X」是被假设预设的，不是投标人在主张既存事实。"""
        report = gate("假设地下机房已安装自动灭火装置，则拟将其纳入巡检台账。")
        self.assertEqual([], report["violations"])
        self.assertTrue(report["claims"], "命中要登记，只是登记成条件而不是挡下")

    def test_b1_11_governed_spans_never_cross_a_sentence_break(self) -> None:
        text = ("若确认需要共用搬运通道，则避让安排将另行编排。"
                "服务标的位于单一院落，多专业共处一坪。")
        lo, hi = span_of(text, "单一院落")
        self.assertFalse(is_conditioned(text, lo, hi))
        for start, end in condition_governed_spans(text):
            self.assertNotIn("。", text[start:end], "支配范围跨了句末就不是支配范围")

    def test_b1_12_frame_word_after_the_claim_is_outside_the_span(self) -> None:
        text = "监控中心已通过年度检测，假定甲方同意延后，则再补充一轮复核。"
        lo, hi = span_of(text, "已通过")
        self.assertFalse(is_conditioned(text, lo, hi))
        self.assertEqual("ASSERTED", semantic_status(text, lo, hi))


class B2UnsupportedFactCannotBecomeCommitment(unittest.TestCase):
    """§B1.2/B2：无来源的既存事实只有三条出路——保留、有据承诺、现状核验边界；造义务不在其中。"""

    def test_b2_01_no_brief_means_no_text_change_and_no_release(self) -> None:
        for text in ("地下车库已配备防爆照明装置。", "本项目所有服务台账已经建立。"):
            with self.subTest(text=text):
                self.assertEqual(text, repair(text), "拿不到底稿就没有证据可言，文本不许动")
                self.assertTrue(blocked(text), "不动文本不等于放行")

    def test_b2_02_boundary_never_contains_an_obligation_word(self) -> None:
        text = "培训不向业主收取任何费用，实操教室已配备消防模拟逃生装置。"
        out = repair(text, {"scope": "负责园区秩序维护与保洁作业"})
        self.assertIn("是否已配备", out)
        self.assertIn(VERIFICATION_BOUNDARY_TAIL, out)
        self.assertEqual([], OBLIGATION_PATTERN.findall(out),
                         "无依据的事实被改成了带将来/义务语态的新承诺")

    def test_b2_03_boundary_keeps_every_business_atom(self) -> None:
        text = "本方案不承诺楼栋外立面翻新，项目外墙保温层已于2023年完成整体更换。"
        out = repair(text, {"scope": "负责园区秩序维护与保洁作业"})
        for atom in ("本方案不承诺楼栋外立面翻新", "项目外墙保温层", "整体更换"):
            self.assertIn(atom, out, "处置只去掉未经核验的断言，业务内容一个字不少")

    def test_b2_04_mixed_sentence_keeps_the_other_clause_untouched(self) -> None:
        text = "行李寄存间每日通风两次，且二级配电室已加装弧光保护装置。"
        out = repair(text, {"scope": "负责园区秩序维护与保洁作业"})
        self.assertIn("行李寄存间每日通风两次", out, "同句里合法的业务断言被连带改掉了")

    def test_b2_05_boundary_makes_the_gate_clear(self) -> None:
        text = "地下车库已配备防爆照明装置。"
        brief = {"scope": "负责园区秩序维护与保洁作业"}
        out = repair(text, brief)
        self.assertEqual([], blocked(out, brief), "中和之后这一句不再是无来源的既存事实主张")

    def test_b2_06_idempotent_on_both_dispositions(self) -> None:
        brief = {"scope": "负责园区秩序维护与保洁作业"}
        for text in ("地下车库已配备防爆照明装置。", "本项目所有服务台账已经建立。"):
            once = repair(text, brief)
            self.assertEqual(once, repair(once, brief), "处置不能越跑越长")
            self.assertEqual(text, repair(text), "无底稿那一支本来就不动，更不会二次叠加")

    def test_b2_07_no_punctuation_artifacts_after_boundary(self) -> None:
        out = repair("屋面防水层已于2022年更换。", {"scope": "负责园区秩序维护与保洁作业"})
        self.assertEqual([], [pattern for pattern in FRAGMENT_PATTERNS if pattern in out])
        self.assertFalse(out.strip().endswith(("，", "、", "：", "；")))

    def test_b2_08_bound_obligation_is_the_only_way_to_a_future_register(self) -> None:
        """底稿逐字写着这项义务时，将来语态才是有据的改写，且凭据必须能回指底稿。"""
        brief = {"duty": "投标人须配备防爆照明装置并留存运行记录"}
        text = "地下车库已配备防爆照明装置。"
        disposition = unsupported_fact_disposition(
            compact_brief="投标人须配备防爆照明装置并留存运行记录", supported=False,
            predicate="配备", keys=["防爆照明装置"])
        self.assertEqual(DISPOSITION_BOUND_OBLIGATION, disposition["action"])
        self.assertIn("须配备防爆照明装置", disposition["obligation_source"])
        out = repair(text, brief)
        self.assertNotIn(STATE_CLAIM_MARK, out)
        self.assertIn("将", out)
        self.assertEqual([], blocked(out, brief))

    def test_b2_09_unrelated_brief_word_does_not_unlock_the_promise(self) -> None:
        """底稿只谈绿化就没资格写"将配备照明"：谓语与对象不同小句共现就不算义务。"""
        disposition = unsupported_fact_disposition(
            compact_brief="服务范围内含绿化带修剪与落叶清运", supported=False,
            predicate="配备", keys=["防爆照明装置"])
        self.assertEqual(DISPOSITION_BOUNDARY, disposition["action"])
        self.assertEqual("", disposition["obligation_source"])

    def test_b2_10_short_keys_below_the_floor_never_bind_an_obligation(self) -> None:
        """两三个字的泛用词（"项目/设备"）不足以证明义务存在：检索键有长度下限。"""
        disposition = unsupported_fact_disposition(
            compact_brief="项目设备应满足日常运行要求", supported=False,
            predicate="配备", keys=[])
        self.assertEqual(DISPOSITION_BOUNDARY, disposition["action"])

    def test_b2_11_supported_existing_fact_is_left_alone(self) -> None:
        """底稿逐字在场的既存事实：门槛判 EXISTING_FACT，修复一个字都不许多动。"""
        brief = {"facts": "现场条件：监控中心已配备双路供电，含柴油发电机一路"}
        text = "监控中心已配备双路供电。"
        self.assertEqual([], blocked(text, brief))
        self.assertEqual(text, repair(text, brief))
        disposition = unsupported_fact_disposition(
            compact_brief=re.sub(r"\s+", "", str(brief)), supported=True,
            predicate="配备", keys=["双路供电"])
        self.assertEqual(DISPOSITION_KEEP_FACT, disposition["action"])

    def test_b2_12_abolished_registers_are_gone_from_the_source(self) -> None:
        """§B2 废止的不是某一句输出，而是那两条拼接：源码里不该再有无据承诺语态。"""
        source = (APP_ROOT / "app/governance/commitment_provenance.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"^RP_EXISTING_STATE_REGISTER\s*=", source, re.M),
                          "被废止的拼接赋值不该还留在源码里（注释里提到它不算）")
        for name in ("_repair_completion_claims", "_repair_existing_state_claims"):
            self.assertEqual(1, len(re.findall(rf"def {name}\(", source)), name)
        # 两族都只从同一个决策点取处置：任何一族绕过契约就是"旧函数继续造承诺"。
        # 三处 = 定义一处 + 两族各一处消费，多一处就是第三套策略。
        self.assertEqual(3, len(re.findall(r"_disposition_edits\(", source)))
        self.assertEqual(1, len(re.findall(r"def unsupported_fact_disposition\(", source)))
        self.assertEqual(10, len(FORMAL_RULES.get("claim_types", [])),
                         "C1–C10 仍冻结：本轮改的是处置策略，不是承诺类别数")
        self.assertEqual(HARD_BLOCK_PROVENANCE_CLASS, "UNSUPPORTED_EXISTING_CLAIM")


class B2RulerHygiene(unittest.TestCase):
    """判据自身的三条纪律：只有一把尺、没有特判词表、fail closed 不被放宽。"""

    def test_hygiene_01_no_reviewer_sentence_baked_into_the_rulers(self) -> None:
        for rel in ("app/qa_severity.py", "app/governance/commitment_provenance.py"):
            source = (APP_ROOT / rel).read_text(encoding="utf-8")
            for fragment in ("假设的合理性", "前提是否成立", "此外地下机房", "实际上地下机房",
                             "将按合同要求配备", "屋面防水层已于"):
                self.assertNotIn(fragment, source, f"{rel} 把复核方的句子写成了规则")

    def test_hygiene_02_single_ruler_for_the_existing_state_claim(self) -> None:
        """既存状态主张仍要求主语+体貌+状态谓语三件同截共现，且只有一处判据。"""
        source = (APP_ROOT / "app/governance/commitment_provenance.py").read_text(encoding="utf-8")
        self.assertEqual(1, len(re.findall(r"EXISTING_STATE_CLAIM = re\.compile", source)))
        self.assertEqual([], blocked("项目负责人按周抽查已完成条目。"),
                         "检查类谓语后的定语读法优先，这一格本轮不许回归")

    def test_hygiene_03_scope_vocabulary_is_a_function_class_not_a_project_list(self) -> None:
        """支配范围的三条终止判据用的都是功能词：换一类工程、换一份标书同样成立。"""
        severity = (APP_ROOT / "app/qa_severity.py").read_text(encoding="utf-8")
        for operator in ("_CONSEQUENCE_MARKER", "_INDICATIVE_EXISTENCE",
                        "_DISCOURSE_RESET_CONNECTIVE", "_HYPOTHESIS_NOMINALISER"):
            self.assertEqual(1, len(re.findall(rf"{operator} = re\.compile", severity)), operator)
        self.assertEqual(1, len(re.findall(r"def condition_governed_spans\(", severity)))
        self.assertEqual(1, len(re.findall(r"def is_conditioned\(", severity)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
