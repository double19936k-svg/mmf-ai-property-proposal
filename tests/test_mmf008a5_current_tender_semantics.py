# -*- coding: utf-8 -*-
"""MMF008A5：当前标的语义可达性收口的**最小泛化反例**（任务书 §4）。

这一支不吃复核方那 30 条已裁决句子（那些是 §6 的 adjudicated regression，另跑），
也不建 100+ 语料：本轮要证明的是**形状**泛化了，不是基准分数好看。
所以每一条都是陌生内容（别的设施、别的岗位、别的写法），只验三件事：

B1 —— claim candidate 与 modality 的共享语义：
  * 假设/前提框架**只支配它后面的那一截**：框架词写在命中之后就无效，名词用法的
    「上述假设」也不算框架；
  * 既存状态主张要「主语 + 已/已于某年 + 状态谓语」三件同截共现才成立，
    禁止把所有「已」字句一律 BLOCK（认知动词、条件句、定语读法、被否定都不挡）；
  * 缺当前项目来源的既存状态主张照旧硬 BLOCK，且修复端有执行端。

B2 —— mixed-clause 业务保留：
  * 作者/生产语言（编写类小标题、裁剪谓语、文稿去处、编写系统的归属）在客户稿里不留；
  * 同句的业务语义原子一个都不能少（量化频次、时限、岗位指派、范围覆盖）；
  * 合法内容（本节业务边界、本章责任说明、台账口径、甲方确认机制、排班与记录载体）
    不能因为带「本节/口径/说明」这些字就被删；
  * 改后仍是能读的话：不出现 `，。` `；。` 这类残片、不留下丢了对象的空字段、幂等。

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

from governance.client_facing_prose import client_facing_prose, internal_narration_findings
from governance.commitment_provenance import (
    EXISTING_STATE_CLAIM, FORMAL_RULES, HARD_BLOCK_PROVENANCE_CLASS, STATE_UNDER_INSPECTION_VERB,
    apply_local_repairs, classify_provenance_semantics, evaluate_commitments)
from qa_severity import _HYPOTHESIS_HEAD, _hypothesis_frame

FRAGMENT_PATTERNS = ("，。", "；。", "、。", "。，", "：。", "，；", "，，", "；；")
DANGLING_TAIL = ("，", "、", "：", "；")


def gate(paragraph: str, brief: str = "") -> dict:
    return evaluate_commitments({"text": brief} if brief else {}, [],
                                {"artifact": {"paragraphs": [paragraph]}})


def blocked_claims(paragraph: str, brief: str = "") -> list[str]:
    return sorted({str(row["text"]) for row in gate(paragraph, brief)["violations"]})


class A1HypothesisFrameGovernsOnlyTheSpanItPrecedes(unittest.TestCase):
    """B1.1：假设/前提框架绑在它实际支配的那一截 claim 上，不是全句免死金牌。"""

    def test_a1_01_premise_at_head_unblocks_the_controlled_claim(self) -> None:
        report = gate("假设高低两区确需共用同一楼层的搬运通道，交叉作业时段的避让安排另附专项方案。")
        self.assertEqual([], report["violations"], "句首假设框架支配的现场共用说法不该判既存事实")
        self.assertEqual("CONDITIONAL_CONTROL", report["claims"][0]["provenance_semantics"])
        self.assertEqual("HYPOTHESIS_OR_PREMISE_FRAME_GOVERNS_CLAIM_SPAN",
                         report["claims"][0]["provenance_semantics_basis"])

    def test_a1_01b_frame_is_not_tied_to_one_claim_family(self) -> None:
        # 同一档框架词在**承诺语汇**那一支也一样有效：证明它绑的是 claim span，不是某一类词。
        report = gate("假定甲方追加夜间点位，秩序维护每班8人的安排方可落实到岗。")
        self.assertEqual([], report["violations"])
        self.assertTrue(report["claims"], "命中要登记，只是登记成条件性控制而不是硬挡")

    def test_a1_02_premise_frame_carries_a_fixed_metric(self) -> None:
        report = gate("前提是第一阶段移交全部弱电井图纸，同类点位30分钟内到场的安排方可落到具体楼层。")
        self.assertEqual([], report["violations"])
        self.assertEqual(["CONDITIONAL"],
                         sorted({row["semantic_status"] for row in report["claims"]}))

    def test_a1_03_frame_written_after_the_claim_does_not_retroactively_exempt(self) -> None:
        # 框架只往前支配：写在命中之后的「若…另行要求」救不了前面那条既存状态主张。
        self.assertTrue(blocked_claims("本项目消防水箱已投入使用，若甲方另有要求可再调整。"))

    def test_a1_04_noun_usage_of_hypothesis_is_not_a_frame(self) -> None:
        self.assertTrue(blocked_claims("上述假设不成立，实操教室已配备消防模拟逃生装置。"),
                        "「上述假设」是名词主语，不能把后半句洗成条件")

    def test_a1_05_frame_stops_at_the_sentence_boundary(self) -> None:
        self.assertTrue(blocked_claims("假定夜间不安排大件搬运。本项目所有货梯均已通过年检。"),
                        "跨句借条件词免罪是上一轮被点名的断裂")

    def test_a1_06_hypothesis_heads_are_a_word_class_not_two_strings(self) -> None:
        for head in ("假设", "假定", "设若", "要是", "万一", "前提是", "前提下"):
            report = gate(f"{head}三类作业共用同一楼层通道，将按专项方案错峰安排。")
            with self.subTest(head=head):
                self.assertEqual([], report["violations"])

    def test_a1_07_negation_and_mention_still_win_over_the_new_detector(self) -> None:
        self.assertEqual([], blocked_claims("本项目并未配备智能巡检终端，相关做法不予采用。"))
        self.assertEqual([], blocked_claims("不得把现有设备台账当作已经核对过的验收资料。"))


class A2ExistingStateClaimNeedsThreePartsTogether(unittest.TestCase):
    """B1.2：claim subject + 状态/工程谓语 + 既存体貌，三者同截共现才是一条既存状态主张。"""

    def test_a2_01_facility_state_claim_without_source_blocks(self) -> None:
        self.assertTrue(blocked_claims("地下车库坡道已加装防溜车挡轮器与声光提示。"))

    def test_a2_02_dated_historical_work_blocks(self) -> None:
        self.assertTrue(blocked_claims("二次供水泵房已于2021年完成管网整体更换。"))

    def test_a2_03_verb_class_generalises_beyond_the_two_sealed_sentences(self) -> None:
        for paragraph in ("电缆夹层已设置自动灭火装置。",
                          "屋面防水层已完成整体翻修。",
                          "电梯机房已安装独立空调与温湿度记录。",
                          "外围护栏已建设完成并交付使用。"):
            with self.subTest(text=paragraph):
                self.assertTrue(blocked_claims(paragraph), "同一类既存状态主张不能只认两种动词")

    def test_a2_04_brief_support_is_the_only_way_out(self) -> None:
        paragraph = "监控中心已配备双路供电。"
        self.assertTrue(blocked_claims(paragraph))
        self.assertEqual([], blocked_claims(
            paragraph, brief="现场条件：监控中心已配备双路供电，含柴油发电机一路。"),
            "当前项目底稿逐字在场时这一条不该挡")

    def test_a2_05_epistemic_and_scope_uses_of_yi_are_not_state_claims(self) -> None:
        for paragraph in ("按招标文件已明确的评分口径逐项响应即可。",
                          "分工全部取自已确认的岗位与目标口径。",
                          "本章只写已核实过的服务范围边界。"):
            with self.subTest(text=paragraph):
                self.assertEqual([], blocked_claims(paragraph),
                                 "认知动词与范围限定不是既存设施主张，禁止把所有「已」字句一律 BLOCK")

    def test_a2_06_attributive_reading_under_an_inspection_verb_is_not_a_claim(self) -> None:
        self.assertEqual([], blocked_claims(
            "专业负责人每周复核已完成条目，发现口径过松即改写判定标准。"),
            "「复核已完成条目」是检查动作加定语宾语，不是主张复核已完成")
        self.assertEqual([], blocked_claims(
            "项目负责人按周抽查已完成项，核对是否存在全为通过仍返工的情形。"))

    def test_a2_07_conditional_or_proposed_register_stays_non_blocking(self) -> None:
        self.assertEqual([], blocked_claims("若项目已配置门禁系统，则按其既有权限分级开放。"))
        self.assertEqual([], blocked_claims("本项目拟增设夜间巡查班次，由秩序维护班组承担。"))

    def test_a2_08_no_subject_no_state_claim(self) -> None:
        self.assertEqual([], blocked_claims("已按甲方确认的委托范围组织作业。"))

    def test_a2_09_repair_end_shares_the_gate_ruler(self) -> None:
        """门宣布 RP-VERIFY-EXISTING-STATE 就必须有执行端——但处置形状由 §B2 契约决定。

        A5 那一版在这里断言的是"改成 `将按合同要求配备`"。Codex A5 Gate 判那条策略不成立
        （"按合同要求"不是证据，无依据的事实不能换成无依据的新义务），所以本条改考三条
        安全性质：断言真的被去掉、没有新增义务词、宾语一个字不少，且修复后不再被这一支挡住。
        """
        paragraph = "培训教室已配备消防模拟逃生装置。"
        repaired = apply_local_repairs({"artifact": {"paragraphs": [paragraph]}}, brief={"x": 1})
        after = str((repaired.get("artifact") or {}).get("paragraphs")[0])
        self.assertIn("是否已配备", after, "既存断言必须被中和成待核验边界")
        for banned in ("将按合同要求配备", "将配备", "将建设", "将安装"):
            self.assertNotIn(banned, after, "无依据的事实被换成了无依据的承诺")
        self.assertIn("消防模拟逃生装置", after, "处置只换语态那一格，宾语一个字不少")
        self.assertEqual([], blocked_claims(after), "修复后同一句不应再被这一支挡住")
        twice = str((apply_local_repairs({"artifact": {"paragraphs": [after]}},
                                         brief={"x": 1}).get("artifact") or {})
                    .get("paragraphs")[0])
        self.assertEqual(after, twice, "修复必须幂等")
        # 没有底稿时这一族不动文本：断言留着，由拿得到底稿的门槛继续挡。
        held = str((apply_local_repairs({"artifact": {"paragraphs": [paragraph]}})
                    .get("artifact") or {}).get("paragraphs")[0])
        self.assertEqual(paragraph, held)
        self.assertTrue(blocked_claims(held), "不动文本不等于放行")

    def test_a2_10_claim_types_are_still_exactly_ten(self) -> None:
        self.assertEqual(10, len(FORMAL_RULES.get("claim_types", [])),
                         "C1–C10 是冻结项：本轮加的是探测器，不是第十一个承诺类别")

    def test_a2_11_state_family_is_a_fourth_blocking_family_not_a_sixth_class(self) -> None:
        report = gate("泵房已于2020年完成改造。")
        self.assertIn("UNSUPPORTED_EXISTING_STATE_CLAIM",
                      {row["claim_type"] for row in report["claims"]})
        self.assertEqual(5, len(report["provenance_semantics_classes"]),
                         "五类语义仍然只有五类")
        self.assertEqual(HARD_BLOCK_PROVENANCE_CLASS,
                         report["violations"][0]["provenance_semantics"])


class B1AuthoringLanguageDoesNotReachTheClientDraft(unittest.TestCase):
    """B2.1：按 authoring actor / object / action 判，不是靠两个字符串命中。"""

    def test_b1_01_writing_label_frames_are_cut_and_content_kept(self) -> None:
        cases = {
            "行文说明：车库入口道闸每日试启一次，异常即时登记。": "车库入口道闸每日试启一次",
            "措辞提示：消防通道占用问题以现场劝阻为主。": "消防通道占用问题以现场劝阻为主",
            "体例说明：雨污分流改造以逐栋排布为单元组织。": "雨污分流改造以逐栋排布为单元组织",
        }
        for source, kept in cases.items():
            with self.subTest(text=source[:12]):
                out, _actions = client_facing_prose(source)
                self.assertIn(kept, out, "小标题切掉之后，客户可读的那句话必须原样还在")
                for label in ("行文说明", "措辞提示", "体例说明"):
                    self.assertNotIn(label, out)

    def test_b1_02_proofreading_and_compilation_frames_disappear(self) -> None:
        for source in ("校对状态：本章与第五章口径已核对一致",
                       "编制说明：本表与评分表逐项对应，正文不再复述。",
                       "复核提示：描述性段落可压缩，指标数据不得删减。"):
            with self.subTest(text=source[:12]):
                out, _actions = client_facing_prose(source)
                self.assertEqual("", out, "整句都是编写叙述时，客户稿里一句都不该留")

    def test_b1_03_shortening_verbs_need_a_textual_object(self) -> None:
        # 现场那一族「压缩」不是裁剪谓语，整句必须原样留着。
        keep = "垃圾压缩站的除臭喷淋每日两次，压缩腔内不得积存渗滤液。"
        out, _actions = client_facing_prose(keep)
        self.assertIn("垃圾压缩站", out)
        self.assertIn("压缩腔", out)

    def test_b1_04_document_destination_moves_are_cut(self) -> None:
        out, _a = client_facing_prose(
            "访客预约的响应时长以现场实测为准，未采纳项不进入正文，其余转入内部台账。")
        self.assertIn("响应时长以现场实测为准", out)
        self.assertNotIn("进入正文", out)
        self.assertNotIn("内部台账", out)

    def test_b1_05_internal_identifier_frame_is_gone_wholesale(self) -> None:
        out, _a = client_facing_prose("内部编号不入正文：正文、表格与说明文字中不出现条款编号。")
        self.assertEqual("", out)

    def test_b1_06_gate_and_repair_read_the_same_ruler(self) -> None:
        findings = internal_narration_findings("写法提示：本节描述性文字可压缩。")
        self.assertTrue(findings, "检测端与改写端同一张形状表，不能修得出、检不出")


class B2BusinessSpansSurviveTheCut(unittest.TestCase):
    """B2.2 / B2.3：删掉元叙述之后，业务语义原子与文本完整性都在。"""

    def test_b2_01_quantified_atoms_survive_in_mixed_sentences(self) -> None:
        cases = {
            "本节登记口径：电梯困人30分钟内到场，高峰时段加派2人值守。":
                ("30分钟内到场", "加派2人"),
            "分档说明：外围垃圾清运每日2次，夏季高温加派1车。":
                ("每日2次", "加派1车"),
            "编制口径说明：地库照明每周巡检1次，全部楼栋覆盖。":
                ("每周巡检1次", "全部楼栋覆盖"),
            "写法说明：夜间秩序维护实行双人巡查，应急联系人由主管担任且24小时开机。":
                ("双人巡查", "24小时开机"),
        }
        for source, atoms in cases.items():
            with self.subTest(text=source[:14]):
                out, _actions = client_facing_prose(source)
                for atom in atoms:
                    self.assertIn(atom, out, f"业务语义原子被删：{atom}")
                for label in ("本节登记口径", "分档说明", "编制口径", "写法说明"):
                    self.assertNotIn(label, out)

    def test_b2_02_legal_content_with_ben_jie_kou_jing_is_kept(self) -> None:
        for source in ("本节业务边界：仅覆盖物业服务委托范围内的公共区域。",
                       "本章责任说明：甲方未按期移交场地时，进场时间相应顺延。",
                       "台账口径以经甲方书面确认的清单为准，未经确认的数量不作为依据。",
                       "甲方确认机制：涉及停水停电的作业须提前24小时书面通知。"):
            with self.subTest(text=source[:14]):
                out, _actions = client_facing_prose(source)
                self.assertEqual(source, out, "合法内容不能因为带「本节/口径/说明」这些字就被删")

    def test_b2_03_no_orphan_field_left_behind(self) -> None:
        # 复核方已裁 HARD 那一族（SZ-049 同形）：字段内容整截都是编写叙述 → 整格收掉，
        # 不留一枚丢了对象的字段名。
        for source in ("备注：本栏数据仅用于评审口径核算，不写入正文",
                       "责任人：本项在本方案中另行说明，正文不再展开。"):
            with self.subTest(text=source[:10]):
                out, _a = client_facing_prose(source)
                self.assertEqual("", out, "字段名丢了对象就必须整格收掉，不留空字段")
        # 反面对照：「本表仅界定事实责任」这一族是甲方读得到的**边界陈述**（复核方标签：非阻断），
        # 不能因为它也带"本表/责任/口径"这一类字就一起收掉。
        keep = "表注：本表仅界定事实责任与接口责任，不构成新增岗位或调整依据。"
        out, _actions = client_facing_prose(keep)
        self.assertEqual(keep, out, "把客户可读的边界陈述当内部元叙述删掉，是任务书明令禁止的方向")

    def test_b2_04_final_text_integrity_and_idempotence(self) -> None:
        samples = ("行文说明：消防控制室值班实行24小时双人制，交接班记录逐班签认。",
                   "客服中心承接全部报修工单，回访口径按季度台账登记，不再另行说明。",
                   "绿化养护由专业供方承担；本节不再为其生成说明段，资质随商务文件提交。",
                   "秩序维护实行三班倒值守；行文时岗位名称保持前后一致，不在正文出现内部编号。")
        for source in samples:
            with self.subTest(text=source[:14]):
                once, _a = client_facing_prose(source)
                twice, _b = client_facing_prose(once)
                self.assertEqual(once, twice, "净化必须幂等")
                for pattern in FRAGMENT_PATTERNS:
                    self.assertNotIn(pattern, once, "改后出现双标点残片")
                self.assertFalse(once.strip().endswith(DANGLING_TAIL), "改后以悬空标点收尾")
                self.assertIn("每日", once) if "每日" in source else None

    def test_b2_05_service_organisation_terms_are_not_authoring_language(self) -> None:
        keep = ("岗位口径按七人编制排布：门岗定点值守主出入口，巡检岗按楼层巡回，"
                "台账由当班负责人逐班填写并交接。")
        out, _actions = client_facing_prose(keep)
        self.assertEqual(keep, out, "排班与记录载体的描述不是编写自述")

    def test_b2_06_unconfirmed_numbers_stay_the_metric_gate_job(self) -> None:
        # 净化器不得用「删业务句」替 Commitment/Metric 门做决定。
        source = "本项目保洁频次为每日2次，全部楼栋覆盖（数值待甲方确认）。"
        out, _actions = client_facing_prose(source)
        self.assertIn("每日2次", out)
        self.assertIn("全部楼栋覆盖", out)


class C1RulerHygiene(unittest.TestCase):
    """本轮判据自身的三条纪律：不硬编码两个字符串、两根标尺不被放宽覆盖、尺只有一把。"""

    def test_c1_01_hypothesis_head_is_a_class_with_position_requirement(self) -> None:
        for head in ("假设", "假定", "设若", "要是", "万一", "前提是", "前提下"):
            self.assertTrue(_HYPOTHESIS_HEAD.search(head), head)
        self.assertTrue(_hypothesis_frame("假设四类作业确需共用同一区域作业面，"))
        self.assertTrue(_hypothesis_frame("以现场实测为准，前提是甲方移交管线图，"))
        self.assertFalse(_hypothesis_frame("上述假设不成立，"), "名词用法不算框架")
        self.assertFalse(_hypothesis_frame("本项目已配备终端，假如甲方另有要求"),
                         "写在命中之后的框架不回头支配")

    def test_c1_02_two_blocking_rulers_are_not_overridden_by_other_releases(self) -> None:
        semantics, _basis, _e = classify_provenance_semantics(
            "前提是移交图纸，30分钟内到场的安排方可落实", conditioned=True)
        self.assertEqual("CONDITIONAL_CONTROL", semantics)
        semantics, _b, _e = classify_provenance_semantics("实操教室已配备逃生装置")
        self.assertEqual(HARD_BLOCK_PROVENANCE_CLASS, semantics)

    def test_c1_03_new_detector_is_one_pattern_one_guard_one_repair(self) -> None:
        source = (APP_ROOT / "app/governance/commitment_provenance.py").read_text(
            encoding="utf-8-sig")
        self.assertEqual(1, len(re.findall(r"EXISTING_STATE_CLAIM = re\.compile", source)))
        self.assertEqual(1, len(re.findall(r"STATE_UNDER_INSPECTION_VERB = re\.compile", source)))
        self.assertEqual(1, len(re.findall(r"def _repair_existing_state_claims\(", source)))
        self.assertIsNotNone(EXISTING_STATE_CLAIM.search("泵房已完成改造"))
        self.assertIsNotNone(STATE_UNDER_INSPECTION_VERB.search("抽查"))


if __name__ == "__main__":
    unittest.main()
