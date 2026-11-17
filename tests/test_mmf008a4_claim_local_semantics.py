# -*- coding: utf-8 -*-
"""MMF008A4 Blocker 2：条件、否定、提议、事实判断都必须作用在 **claim 那一截**。

复核方确认的缺口（HP-15 已关，不许回归；本轮新增反例）：
① 「如 + 主体 + 确认 …」这类条件框架此前只列举了 `若/如果/倘若`，单字 `如` 被漏掉，
   整句被当成既存事实主张挡下；`若…` `如经…` 必须同义处理。
② 「未提供 X 的证据」「X 非本项目范围」都不得被当成 X 已成立（后者是中文主题—评论
   结构：被谈的对象在前，排除谓语在后）。
③ 同一个空间名词在四种句子角色里必须给出四种结论（既存事实→挡 / 条件→放 / 否定→放 /
   拟议组织→放），**词本身不决定结论**。
④ 无来源的现场构型断言（单一地块 / 单一院落 / 同一空间 / 同一建筑 / 同一区域 / 竖向叠加）
   仍须硬挡；有当前项目底稿逐字来源时同一句必须放行。

姿态：判据是**作用域与谓语角色**，不是词表。夹具全用陌生题材（文体综合体、场馆后勤、
冷链园区、跨标段台账），复核方原句一格都不当判据；名词×角色矩阵证明规则是关系式的。
每条放过都要留下具名凭据（类别 + 凭据 + 原句），不做静默丢弃。零 Provider 调用。

读数来源：`runtime/008a4_claim_local_probe.py` 现读的 case 表（同一门槛入口
`evaluate_commitments`），不是先写期望再找证据。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
for path in (str(APP), str(Path(__file__).resolve().parent)):
    if path not in sys.path:
        sys.path.insert(0, path)

from governance.commitment_provenance import (  # noqa: E402
    HARD_BLOCK_PROVENANCE_CLASS, SITE_PREMISE_SPATIAL, evaluate_commitments,
    site_premise_claims)
from qa_severity import (  # noqa: E402
    _SUBORDINATOR, is_conditioned, is_denied, is_mention_not_use, is_scope_excluded,
    semantic_status)

APP_ROOT = Path(__file__).resolve().parents[1]

# 一个空间名词，四种句子角色：词不变、角色变，结论必须跟着角色变。
NOUN = "同一楼层"
ROLES = {
    "EXISTING_FACT": f"本项目现场为{NOUN}，两侧商户共用巡检通道。",
    "CONDITIONAL": f"如甲方确认现场为{NOUN}，则两侧商户共用巡检通道。",
    "NEGATED": f"本次未提供现场为{NOUN}、两侧商户共用巡检通道的证据。",
    "SCOPE_EXCLUDED": f"现场为{NOUN}非本项目范围，两侧保洁班次与另一标段共用一套台账。",
    "PROPOSED_METHOD": f"本方案拟将{NOUN}两侧的巡检班次编排为共用一套台账。",
}
ROLE_EXPECTED_STATUS = {
    "EXISTING_FACT": "ASSERTED", "CONDITIONAL": "CONDITIONAL", "NEGATED": "DENIED",
    "SCOPE_EXCLUDED": "DENIED", "PROPOSED_METHOD": "ASSERTED",
}
ROLE_EXPECTED_GATE = {
    "EXISTING_FACT": "BLOCK", "CONDITIONAL": "PASS", "NEGATED": "PASS",
    "SCOPE_EXCLUDED": "PASS", "PROPOSED_METHOD": "PASS",
}
# 条件从属连词是**同一档词类**：换引导字不换语义。
CONDITION_HEADS = ("如甲方确认", "若甲方确认", "如经确认", "若经确认", "倘若甲方确认",
                   "如果甲方确认", "一旦甲方确认", "即使甲方确认", "即便甲方确认")
# 单字 `如` 的词界守卫：这些写法里它是语素或交叉引用，不是条件框架。
NON_CONDITION_LIKE = (
    ("服务标准如表5所示，现场为同一楼层且两侧商户共用巡检通道。", "同一楼层"),
    ("平面布置如图所示，现场为同一楼层，两侧商户共用巡检通道。", "同一楼层"),
    ("本项目为单一院落（如今两侧商户共用巡检通道）。", "单一院落"),
    ("口径如同现行做法，现场为同一楼层并共用巡检通道。", "同一楼层"),
    ("比如现场为同一楼层，两侧商户共用巡检通道。", "同一楼层"),
)
# Run F 里判"应当阻断"的现场事实形状：无来源的既存构型断言（题材换掉，形状保留）。
MUST_STILL_BLOCK = (
    "本项目位于单一地块，各专业作业面相接。",
    "服务标的为同一院落，多专业共处一坪。",
    "本项目为单一院落，三个标段共用出入口。",
    "多专业在同院落内竖向叠加，共用货运通道。",
    "四类作业在同一空间内竖向叠加，共用出入口。",
    "现场为同一建筑，三个标段共用出入口。",
    "园区为同一区域，多业态共用货运通道。",
    "本项目位于同一楼栋，各专业共用货梯。",
)
# 后置条件词不回头洗前面的主张；跨逗号的对比词不洗后面的事实；否定否认的是另一个身份。
NO_LAUNDERING = {
    "postposed_condition": ("现场为同一楼层，两侧商户共用巡检通道，若甲方另有要求则调整。",
                            "同一楼层"),
    "contrast_before_colon": ("判断依据来自现场特征而非某种笼统说法：现场为同一楼层，"
                              "两侧商户共用巡检通道。", "同一楼层"),
    "denial_of_another_noun": ("本项目为单一院落，非文体综合体，两侧商户共用巡检通道。",
                               "单一院落"),
    "negation_stops_at_comma": ("未提交巡检台账，同一楼层两侧共用通道。", "同一楼层"),
    "frame_stops_at_sentence_break": ("如甲方确认现场为同一楼层，则两侧商户共用巡检通道。"
                                      "服务标的位于单一院落，多专业共处一坪。", "单一院落"),
}
NOUNS_IN_CLASS = ("同一楼层", "同一建筑", "同一区域", "同一空间", "同一院落", "单一地块")
ROLE_TEMPLATES = {
    "EXISTING_FACT": "本项目现场为{n}，两侧商户共用巡检通道。",
    "CONDITIONAL": "如甲方确认现场为{n}，则两侧商户共用巡检通道。",
    "NEGATED": "本次未提供现场为{n}、两侧商户共用巡检通道的证据。",
    "PROPOSED_METHOD": "本方案拟将{n}两侧的巡检班次编排为共用一套台账。",
}


def _gate(text: str, brief: dict | None = None) -> dict:
    return evaluate_commitments(brief or {}, [], {"artifact": {"cases": [{"text": text}]}})


def _span(text: str, phrase: str) -> tuple[int, int]:
    start = text.index(phrase)
    return start, start + len(phrase)


def _status_of(text: str, phrase: str) -> str:
    start, end = _span(text, phrase)
    return semantic_status(text, start, end)


class B1ConditionFramesAreOneWordClass(unittest.TestCase):
    """§①：条件框架按词类识别，引导字之间是同义关系而不是逐条例外。"""

    def test_b1_condition_heads_all_open_the_same_frame(self):
        readings = set()
        for head in CONDITION_HEADS:
            text = f"{head}现场为{NOUN}，则两侧商户共用巡检通道。"
            start, end = _span(text, NOUN)
            self.assertTrue(is_conditioned(text, start, end),
                            f"条件引导词 {head!r} 没被认成同一档框架")
            self.assertEqual("CONDITIONAL", semantic_status(text, start, end), head)
            self.assertEqual("PASS", _gate(text)["status"], f"{head} 仍被挡（反例未关）")
            readings.add(_gate(text)["suppressed"][0]["semantic_status"])
        self.assertEqual({"CONDITIONAL"}, readings, "同一档框架给出了不同语义")

    def test_b1b_the_release_leaves_a_recheckable_record(self):
        reading = _gate(ROLES["CONDITIONAL"])
        self.assertTrue(reading["suppressed"], "放过的那一步没有留下可复核记录")
        row = reading["suppressed"][0]
        self.assertEqual("CONDITIONAL", row["semantic_status"])
        self.assertEqual("FALSE_POSITIVE", row["qa_class"])
        self.assertTrue(row["classification_reason"])
        self.assertIn(NOUN, row["sentence"])

    def test_b1c_word_boundary_guard_keeps_non_conditions_out(self):
        """`如` 在"比如/例如/如今/如同/如表/如图"里都不是条件框架，命中照旧要来源。"""
        for text, phrase in NON_CONDITION_LIKE:
            start, end = _span(text, phrase)
            self.assertFalse(is_conditioned(text, start, end),
                             f"交叉引用/比拟被当成条件框架而放过：{text}")
            self.assertEqual("ASSERTED", semantic_status(text, start, end), text)
            self.assertEqual("BLOCK", _gate(text)["status"], text)

    def test_b1d_frame_is_forward_only_and_stops_at_the_sentence_break(self):
        text, phrase = NO_LAUNDERING["postposed_condition"]
        self.assertEqual("ASSERTED", _status_of(text, phrase),
                         "写在命中之后的条件词回头洗掉了既存事实主张")
        self.assertEqual("BLOCK", _gate(text)["status"])
        text2, phrase2 = NO_LAUNDERING["frame_stops_at_sentence_break"]
        self.assertEqual("CONDITIONAL", _status_of(text2, NOUN), "前一句的条件框架没管到它自己的分句")
        self.assertEqual("ASSERTED", _status_of(text2, phrase2),
                         "条件框架跨过了句号去洗下一句的既存事实")
        reading = _gate(text2)
        self.assertEqual("BLOCK", reading["status"])
        self.assertEqual([phrase2], [row["text"] for row in reading["violations"]])


class B2NegationAndScopeDenial(unittest.TestCase):
    """§②：否定与范围排除都只管自己那一截；后置排除谓语与前置否定是同一种语义行为。"""

    def test_b2a_missing_evidence_is_not_an_assertion(self):
        self.assertEqual("DENIED", _status_of(ROLES["NEGATED"], NOUN))
        reading = _gate(ROLES["NEGATED"])
        self.assertEqual("PASS", reading["status"])
        self.assertEqual("DENIED", reading["suppressed"][0]["semantic_status"])

    def test_b2b_topic_comment_scope_exclusion_denies_the_same_claim(self):
        for text in (ROLES["SCOPE_EXCLUDED"],
                     "同一楼层不属于本次技术正文范围，其保洁班次与另一标段共用一套台账。",
                     "同一楼层不在本标段清单之内，两侧商户仍共用巡检通道。"):
            start, end = _span(text, NOUN)
            self.assertTrue(is_scope_excluded(text, start, end),
                            f"主题—评论结构的排除谓语没被认出来：{text}")
            self.assertTrue(is_denied(text, start, end), text)
            self.assertEqual("DENIED", semantic_status(text, start, end), text)
            self.assertTrue(site_premise_claims(text), f"该句本应命中现场构型断言：{text}")
            self.assertEqual("PASS", _gate(text)["status"], text)

    def test_b2c_negation_nested_inside_a_condition_is_still_not_an_assertion(self):
        for text in ("若甲方未确认现场为同一楼层，则两侧商户不共用巡检通道。",
                     "如乙方未提供现场为同一楼层的证据，则本方案不安排共用巡检通道。"):
            self.assertEqual("DENIED", _status_of(text, NOUN), text)
            self.assertEqual("PASS", _gate(text)["status"], text)

    def test_b2d_denial_of_another_noun_does_not_launder_the_claim(self):
        text, phrase = NO_LAUNDERING["denial_of_another_noun"]
        self.assertEqual("ASSERTED", _status_of(text, phrase),
                         "否定词否认的是另一个名词的身份，本句的现场构型主张照样成立")
        self.assertEqual("BLOCK", _gate(text)["status"])

    def test_b2e_contrast_before_a_colon_stays_out_of_the_fact_clause(self):
        text, phrase = NO_LAUNDERING["contrast_before_colon"]
        self.assertEqual("ASSERTED", _status_of(text, phrase))
        start, end = _span(text, "某种笼统说法")
        self.assertTrue(is_mention_not_use(text, start, end),
                        "被对比掉的那个说法本身应当看得见是引述")
        self.assertEqual("BLOCK", _gate(text)["status"])

    def test_b2f_negation_scope_is_the_clause_it_precedes(self):
        """逗号断作用域：过了逗号的那一句是自己在新事实主张，不跟着前句一起免罪。"""
        text, phrase = NO_LAUNDERING["negation_stops_at_comma"]
        self.assertEqual("ASSERTED", _status_of(text, phrase))
        self.assertEqual("BLOCK", _gate(text)["status"])
        first = "未提交同一楼层的巡检台账、演练签到表，两班共用一套记录表。"
        self.assertEqual("DENIED", _status_of(first, NOUN))
        self.assertEqual("PASS", _gate(first)["status"])

    def test_b2g_negation_reaches_over_the_dun_coordination(self):
        """并列顿号共享同一个谓语：一个否定词管得住整串列举，不必逐格重复"未"。

        复核方 A3 自持见证里就有这一形（"未提供 [甲] 、 [乙] 的证据"），只认第一截会把
        第二截当成被主张的既存事实；跨过逗号则不算（那是另一条断言，见 b2f）。
        """
        text = "未提交巡检台账、同一楼层共用通道的任何证据。"
        self.assertEqual("DENIED", _status_of(text, NOUN))
        self.assertEqual("PASS", _gate(text)["status"])
        two_list = "未提供本项目为单一院落、功能在同一建筑内竖向叠加的证据。"
        for phrase in ("单一院落", "同一建筑"):
            self.assertEqual("DENIED", _status_of(two_list, phrase), phrase)
        self.assertEqual("PASS", _gate(two_list)["status"])

    def test_b2h_coordination_reach_is_still_bounded(self):
        """限制在作用域本身：停顿断开、长度断开、数字断开，三条都不越。"""
        for text, phrase in (
                ("未提交巡检台账、演练签到表，同一楼层两侧商户共用巡检通道。", "同一楼层"),
                ("未提交 1 份台账、同一楼层两侧共用通道的证明。", "同一楼层")):
            self.assertEqual("ASSERTED", _status_of(text, phrase), text)
            self.assertEqual("BLOCK", _gate(text)["status"], text)


class B3SameNounFourRoles(unittest.TestCase):
    """§③：词不变、角色变 ⇒ 结论变。这一组是"不能靠词本身决定"的正面证明。"""

    def test_b3a_status_follows_the_clause_role_not_the_noun(self):
        for role, text in ROLES.items():
            self.assertEqual(ROLE_EXPECTED_STATUS[role], _status_of(text, NOUN),
                             f"{role}：{text}")

    def test_b3b_gate_follows_the_clause_role_not_the_noun(self):
        for role, text in ROLES.items():
            self.assertEqual(ROLE_EXPECTED_GATE[role], _gate(text)["status"],
                             f"{role}：{text}")

    def test_b3c_the_matrix_holds_for_every_noun_in_the_class(self):
        """六个名词 × 四种角色：每个名词都给出同一种角色结论 ⇒ 规则是关系式的。"""
        for noun in NOUNS_IN_CLASS:
            self.assertTrue(SITE_PREMISE_SPATIAL.fullmatch(noun), noun)
            readings = {}
            for role, template in ROLE_TEMPLATES.items():
                text = template.format(n=noun)
                self.assertEqual(role == "EXISTING_FACT", _gate(text)["status"] == "BLOCK",
                                 f"{role}/{noun}：{text}")
                readings[role] = _status_of(text, noun)
            self.assertEqual(ROLE_EXPECTED_STATUS["EXISTING_FACT"],
                             readings["EXISTING_FACT"], noun)
            self.assertEqual(ROLE_EXPECTED_STATUS["CONDITIONAL"], readings["CONDITIONAL"], noun)
            self.assertEqual(ROLE_EXPECTED_STATUS["NEGATED"], readings["NEGATED"], noun)
            # 拟议组织这一格的语义类别来自谓语角色，不是状态档
            self.assertEqual("ASSERTED", readings["PROPOSED_METHOD"], noun)

    def test_b3d_proposed_method_is_not_a_free_pass_on_one_character(self):
        """`拟` 单独在场不够：既要有服务设计字段，也不能同句带既存/处所谓语。"""
        with_existing = f"本项目现有{NOUN}，拟将两侧商户的巡检班次编为共用一套台账。"
        reading = _gate(with_existing)
        self.assertEqual("BLOCK", reading["status"])
        self.assertIn(HARD_BLOCK_PROVENANCE_CLASS,
                      {row["provenance_semantics"] for row in reading["violations"]})
        no_design_field = "拟将同一楼层作为场地说明写入概述，两侧共用巡检通道。"
        self.assertEqual("BLOCK", _gate(no_design_field)["status"],
                         "没有服务设计字段的「拟」也放过了，等于给一个词开免检")

    def test_b3e_every_release_is_evidenced_not_silent(self):
        for role in ("CONDITIONAL", "NEGATED", "SCOPE_EXCLUDED", "PROPOSED_METHOD"):
            reading = _gate(ROLES[role])
            self.assertEqual("PASS", reading["status"], role)
            self.assertTrue(reading["suppressed"], f"{role} 放过时没有可复核记录")
            row = reading["suppressed"][0]
            self.assertTrue(row["classification_reason"], f"{role} 放过没写理由")
            self.assertEqual("FALSE_POSITIVE", row["qa_class"], role)
            self.assertIn(NOUN, row["text"])
        proposed = _gate(ROLES["PROPOSED_METHOD"])["suppressed"][0]
        self.assertEqual("PROPOSED_SERVICE_METHOD", proposed["provenance_semantics"])
        self.assertEqual("SITE_PREMISE_WITHIN_PROPOSED_SERVICE_ORGANISATION",
                         proposed["provenance_semantics_basis"])


class B4RealUnsupportedFactsStillBlock(unittest.TestCase):
    """§④：真实形状不能因为本轮放宽而漏挡——这一组只做回归，不定义规则。"""

    def test_b4a_site_premises_without_source_still_hard_block(self):
        for text in MUST_STILL_BLOCK:
            reading = _gate(text)
            self.assertEqual("BLOCK", reading["status"], f"漏挡真实现场事实：{text}")
            self.assertTrue(reading["violations"])
            row = reading["violations"][0]
            self.assertEqual(HARD_BLOCK_PROVENANCE_CLASS, row["provenance_semantics"])
            self.assertEqual("ASSERTED", row["semantic_status"])
            self.assertIn("RP-SITE-PREMISE-VERIFY", row["repair_action"])

    def test_b4b_vertical_superposition_shape_is_blocked(self):
        reading = _gate("四类作业在同一空间内竖向叠加，共用出入口。")
        self.assertEqual("BLOCK", reading["status"])
        self.assertEqual("同一空间", reading["violations"][0]["text"])

    def test_b4c_current_project_evidence_releases_the_same_sentence(self):
        """有当前项目底稿逐字来源时，同一句合法放行——挡的是没来源，不是这个词。"""
        brief = {"project_name": "文体综合体",
                 "facts": "本项目现场为同一楼层，两侧商户共用巡检通道"}
        reading = _gate(ROLES["EXISTING_FACT"], brief)
        self.assertNotEqual("BLOCK", reading["status"])
        self.assertTrue(any(row["provenance"] == "PROJECT_FACT" for row in reading["claims"]))

    def test_b4d_the_reviewer_named_noun_also_follows_its_role(self):
        """「同一建筑」在四种角色里的四种结论（句子是新写的，不搬复核方原句）。"""
        cases = {
            "EXISTING_FACT": ("本项目现场为同一建筑，三个标段共用出入口。", "BLOCK"),
            "CONDITIONAL": ("如甲方确认现场为同一建筑，则三个标段共用一条货运通道。", "PASS"),
            "NEGATED": ("本次未提供现场为同一建筑、各标段共用出入口的当前项目证据。", "PASS"),
            "PROPOSED_METHOD": ("本方案拟将同一建筑内的巡检班次编排为共用一套台账。", "PASS"),
        }
        for role, (text, expected) in cases.items():
            self.assertEqual(expected, _gate(text)["status"], f"{role}：{text}")


class B5StructuralSentinels(unittest.TestCase):
    """规则必须是关系：产品码里不写复核方原句，每个作用域算子只有一处判据。"""

    RULERS = ("app/qa_severity.py", "app/governance/commitment_provenance.py")

    def test_b5a_no_reviewer_sentence_baked_into_the_rulers(self):
        for rel in self.RULERS:
            source = (APP_ROOT / rel).read_text(encoding="utf-8")
            for fragment in ("如甲方确认", "非本项目范围", "口径分层", "专业负责人",
                             "跨班次", "交接班记录", "标注未关闭", "同一楼层"):
                self.assertNotIn(fragment, source, f"{rel} 把复核方的句子写成了规则")

    def test_b5b_one_operator_one_scope_function(self):
        severity = (APP_ROOT / "app/qa_severity.py").read_text(encoding="utf-8-sig")
        for name in ("is_conditioned", "is_denied", "is_scope_excluded", "is_mention_not_use"):
            self.assertEqual(1, len(re.findall(rf"def {name}\(", severity)), name)
        # 每个作用域算子的正则只在一处被消费：别处再扫一遍就是第二把尺。
        # MMF008A6-B1 把条件框架的读法从「句首前缀里 search 一次」换成「逐小句 finditer 一次」，
        # 消费点仍然只有一处（`_frame_starts_in_unit`），所以这里数的是**消费点**而不是方法名：
        # 定义那一行 + 唯一一处消费 = 两次引用，多一次就是第二把尺。
        self.assertEqual(1, len(re.findall(r"_SUBORDINATOR\.finditer", severity)))
        self.assertEqual(0, len(re.findall(r"_SUBORDINATOR\.search", severity)),
                         "条件框架又被人当整句关键词扫了一遍")
        self.assertEqual(2, len(re.findall(r"_SUBORDINATOR\b", severity)))
        self.assertEqual(1, len(re.findall(r"_POST_DENIAL\.finditer", severity)))
        self.assertEqual(1, len(re.findall(r"_CONTRAST_DENIAL\.search", severity)))
        # 范围排除只被定义一次、只被前置否定这一个入口使用（语义档不另开一类）。
        self.assertEqual(2, len(re.findall(r"is_scope_excluded\(", severity)))

    def test_b5c_semantic_ordering_is_decided_in_one_place(self):
        severity = (APP_ROOT / "app/qa_severity.py").read_text(encoding="utf-8-sig")
        self.assertEqual(1, len(re.findall(r"def semantic_status\(", severity)))
        provenance = (APP_ROOT / "app/governance/commitment_provenance.py").read_text(
            encoding="utf-8-sig")
        for operator in ("is_denied", "is_conditioned", "is_scope_excluded", "is_mention_not_use"):
            self.assertEqual(0, len(re.findall(rf"\b{operator}\(", provenance)),
                             f"门槛自己另写了一遍 {operator} 的作用域判据")
        self.assertEqual(1, len(re.findall(r"NON_ASSERTIVE_STATUSES = ", provenance)))

    def test_b5d_condition_marker_is_a_word_class_not_a_case_list(self):
        self.assertIn("如", _SUBORDINATOR.pattern)
        self.assertIn("(?<![比例不诸宛恰无])", _SUBORDINATOR.pattern)
        self.assertIn("(?![今同表图下])", _SUBORDINATOR.pattern)
        for head in ("若", "如果", "倘若", "即使", "无论", "一旦", "如"):
            self.assertTrue(_SUBORDINATOR.search(head), head)

    def test_b5e_site_premise_release_needs_both_positive_marks(self):
        """拟议组织这一格的放行条件写在一处，且要求"无既存谓语 + 有计划谓语 + 有服务字段"。"""
        provenance = (APP_ROOT / "app/governance/commitment_provenance.py").read_text(
            encoding="utf-8-sig")
        self.assertEqual(1, len(re.findall(
            r"if not locative and plan and design_field:", provenance)))
        self.assertLess(provenance.index("if not locative and plan and design_field:"),
                        provenance.index('"SITE_PREMISE_ASSERTED_BY_DEFINITION"'),
                        "拟议角色的判定必须早于「按定义就挡」")


if __name__ == "__main__":
    unittest.main()
