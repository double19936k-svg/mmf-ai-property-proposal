# -*- coding: utf-8 -*-
"""MMF008A4 Blocker 3：净化器按**子句角色**分离业务与编写口径，两个方向同时闭合。

复核方确认的缺口（同一轮里既有 under- 也有 over-sanitize）：
① under：「口径分层：专业负责人把抽取结果分入……」是内部生成/治理语言，客户成品里
   应删除或改写；此前判据只认"本节/正文"这类文档构件做主体，主体换成编写角色
   （专业负责人）+ 治理口径标签时就整句漏过。
② over：表格原句「关闭时点与记录归档位置，跨班次未关闭的续填至交接班记录并在本表
   标注未关闭」里的 跨班次续填 / 交接班记录 / 标注未关闭 是**真服务运行规则**，
   不得删掉；此前 `在本表标注` 被 LOCATIVE_SELF + 文字操作动词读成"文档在讲自己怎么写"。
③ 改后清理必须处理 `，。` `；。`、残缺连接词、表格单元格半句、删除后的语法断裂，
   但**禁止为了"句子完整"凭空补业务事实**。

姿态：判据是子句的语法角色（主体是谁 / 谓语作用在什么上 / 有没有甲方接口或现场实质盾），
不是把复核方的句子抄进词表。夹具全用陌生题材（冷链园区交接表、场馆后勤、跨标段台账），
只有一条来自复核方的原句作回归锚点。零 Provider 调用。

读数来源：`runtime/008a4_sanitizer_probe.py` 现读的 before/after 与逐子句角色，
不是先写期望再找证据。
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

from governance.client_facing_prose import (  # noqa: E402
    GOVERNANCE_LABEL, TEXT_ONLY_OP, WRITE_OP, _clause_roles, _generic_internal_move,
    _tidy_after_edit, client_facing_prose, internal_narration_findings)

APP_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ("，。", "；。", "、。", "：。", "。，", "，，", "；；", "、，", "：，")

# ①under：治理口径标签 + 编写角色主语（陌生题材，不抄复核方原句）
INTERNAL_EXPLANATION = (
    "口径分层：专业负责人把抽取结果分入已确认事实、条件化方法与待澄清事项三类。",
    "口径分层：专业负责人把抽取结果分入三类，逐条标注所在章节。",
    "分级归属：编写人员把巡检素材按已确认事实与实施建议分档，正文只保留其中一档。",
    "内部分档说明：本节把响应内容按语气分为两档，正文只取已确认的那一档。",
)
# ②over：真服务运行规则（记录载体怎么填、登什么），一个字都不能少
SERVICE_RULES_KEPT = (
    "关闭时点与记录归档位置，跨班次未关闭的续填至交接班记录并在本表标注未关闭",
    "异常上报路径与响应时限，跨班次未闭环的续登至次日交接表并在本栏标注未闭环",
    "本表仅登记现场作业量数据，涉及费用的事项以合同约定为准。",
    "巡检路线与频次安排，交接班时由交班人逐项确认并在本表标注未完成事项。",
    "台账只登记已核实的规模数据，未核实项另行标注后复核。",
)
# 合法的客户边界陈述：出现"本节/本章"也不动（甲方接口与合同侧）
CUSTOMER_BOUNDARY_KEPT = (
    "上述事项待甲方书面确认后再行实施，未经确认前不作为服务依据。",
    "服务范围以本项目招标文件与物业服务合同给定的技术服务范围为限。",
    "本节仅界定上述难点、管理原则与验证口径，具体交接程序由退出移交专章展开；",
)


class C1UnderSanitizeIsClosed(unittest.TestCase):
    """§①：治理口径标签与其内容段一起算内部编写语言。"""

    def test_c1a_governance_label_narration_is_removed(self):
        for sentence in INTERNAL_EXPLANATION:
            output, actions = client_facing_prose(sentence)
            self.assertEqual(output.strip(), "", f"内部口径叙述原样留下了：{sentence}")
            self.assertTrue([row for row in actions if row.get("applied")], sentence)

    def test_c1b_the_label_is_a_construction_not_a_word_list(self):
        """GOVERNANCE_LABEL 认的是「治理名词 + 冒号且后面没有谓语」这一构式。"""
        self.assertTrue(GOVERNANCE_LABEL.match("口径分层："))
        self.assertTrue(GOVERNANCE_LABEL.match("归属划分："))
        for heading in ("巡检频次：", "责任人：", "记录归档位置：", "交接要求："):
            self.assertIsNone(GOVERNANCE_LABEL.match(heading), heading)

    def test_c1c_frame_reach_stops_where_business_content_starts(self):
        """标签后面那一截带现场实质时不跟着算内部：只切标签本身，内容一个字不动。"""
        verdict = _clause_roles("口径分层：")
        self.assertTrue(verdict["internal"] and verdict["roles"]["governance_label"])
        content = _clause_roles("巡检频次按现场核定结果执行")
        self.assertFalse(content["internal"], content["roles"])
        output, _actions = client_facing_prose("口径分层：巡检频次按现场核定结果执行。")
        self.assertEqual("巡检频次按现场核定结果执行。", output)
        # 内容段带着语气标签时，现场词不再替它作盾：被操作的对象是文字而不是现场状态
        framed = _clause_roles("编写人员把巡检素材按已确认事实与实施建议分档")["roles"]
        self.assertTrue(framed["tone_label"] and framed["soft_shield"], framed)

    def test_c1d_report_side_and_repair_side_are_the_same_ruler(self):
        findings = internal_narration_findings(INTERNAL_EXPLANATION[0], "artifact.sections[3]")
        self.assertTrue(findings, "检测端看不见这句内部叙述")
        self.assertTrue(any(row["severity"] == "AUTO_REPAIR" for row in findings), findings)


class C2OverSanitizeIsClosed(unittest.TestCase):
    """§②：两栖动词（标注/登记）单独不构成文字操作。"""

    def test_c2a_service_running_rules_survive_verbatim(self):
        for sentence in SERVICE_RULES_KEPT:
            output, actions = client_facing_prose(sentence)
            self.assertEqual(output.strip(), sentence.strip(),
                             f"真服务规则被改写或删除：{output!r}")
            self.assertFalse([row for row in actions if row.get("applied")], sentence)

    def test_c2b_the_three_named_fragments_are_all_still_there(self):
        """复核方点名的三个片段：跨班次续填 / 交接班记录 / 标注未关闭。"""
        output, _actions = client_facing_prose(
            "关闭时点与记录归档位置，跨班次未关闭的续填至交接班记录并在本表标注未关闭")
        for fragment in ("跨班次", "交接班记录", "标注未关闭", "续填"):
            self.assertIn(fragment, output, output)

    def test_c2c_the_locative_marker_needs_a_text_object_to_cut(self):
        """`在本表标注未关闭` 的宾语是现场状态而不是文字制品 ⇒ 不是元叙述。"""
        for clause in ("跨班次未关闭的续填至交接班记录并在本表标注未关闭",
                       "跨班次未闭环的续登至次日交接表并在本栏标注未闭环",
                       "本表仅登记现场作业量数据"):
            roles = _clause_roles(clause)["roles"]
            self.assertTrue(roles["locative_self"] or roles["doc_self"], roles)
            self.assertTrue(roles["field_carrier_operation"], roles)
            self.assertFalse(_clause_roles(clause)["internal"], roles)
        # 同一主体形换成"文档构件/编写名词"作对象时仍然动手（不是给某个句子特判放行）
        self.assertTrue(_clause_roles("本表仅登记为编制约束，不进入技术正文")["internal"])
        self.assertTrue(_clause_roles("表中不出现对自身分档的描述")["internal"])

    def test_c2d_legitimate_customer_boundary_is_kept(self):
        for sentence in CUSTOMER_BOUNDARY_KEPT:
            output, _actions = client_facing_prose(sentence)
            self.assertEqual(output.strip(), sentence.strip(), output)

    def test_c2e_text_only_verbs_still_move_the_ruler(self):
        for sentence in ("本节不重复表述正文已写明的安排。",
                         "此类事项在本章仅登记为不纳入技术正文，不为其生成说明段、清单或空白附件页。"):
            output, _actions = client_facing_prose(sentence)
            self.assertEqual(output.strip(), "", f"整句都是编写叙述却留下了：{sentence}")

    def test_c2f_the_two_verb_lists_may_differ_only_by_the_ambiguous_pair(self):
        """`TEXT_ONLY_OP` 是 `WRITE_OP` 去掉两栖动词那一族；漏掉一格就是新的一把尺。"""
        writing_verbs = ("固定的是", "不填", "不展开", "不进入", "不出现", "改写", "表述",
                         "分列", "归为", "列为", "记为", "删除", "引用关系")
        for verb in writing_verbs:
            self.assertIn(verb, TEXT_ONLY_OP.pattern, verb)
            self.assertIn(verb, WRITE_OP.pattern, verb)
        # 表格列里"固定的是组织方式"这种句子只有文字谓语，仍然算编写口径（A3 已知集 S-29）
        sentence = ("周期一栏不填数字，实际间隔以议定后的年度维护计划与派工单为准，"
                    "表中固定的是组织方式与责任岗位。")
        output, actions = client_facing_prose(sentence)
        self.assertNotIn("表中固定的是组织方式", output, output)
        self.assertIn("以议定后的年度维护计划与派工单为准", output)
        self.assertTrue([row for row in actions if row.get("applied")])


class C3MetaAndBusinessAreSeparated(unittest.TestCase):
    """§③：meta 与 business 同句时只切 meta，业务那一截一个字不少。"""

    def test_c3a_mixed_cell_keeps_the_operational_rule(self):
        sentence = ("夜间值守每两小时巡查一次并留存记录，"
                    "因此本节把编排过程本身固化为可重复执行的程序。")
        output, actions = client_facing_prose(sentence)
        self.assertIn("每两小时巡查一次", output)
        self.assertIn("留存记录", output)
        self.assertNotIn("固化", output)
        applied = [row for row in actions if row.get("applied")]
        self.assertTrue(applied)
        self.assertEqual(applied[0]["shape_id"], "GENERIC_INTERNAL_WRITING_NARRATION")

    def test_c3b_table_cell_with_both_roles_keeps_the_business_half(self):
        sentence = ("交接班记录逐条登记为编制约束，本表标注未关闭项并续填至次日。")
        output, _actions = client_facing_prose(sentence)
        self.assertIn("本表标注未关闭项", output)
        self.assertIn("续填至次日", output)

    def test_c3c_cut_clauses_are_named_clause_by_clause(self):
        """逐子句留痕：每一格判成内部与否都要能指认，且写明依据。"""
        # 内容段自己判不出内部（没有文档主体也没有正文类宾语），靠句首口径标签的框架力
        _output, actions = client_facing_prose(INTERNAL_EXPLANATION[1])
        rows = [row for row in actions if row.get("clause_roles")]
        self.assertTrue(rows, actions)
        roles = rows[0]["clause_roles"]
        self.assertTrue(all(row["internal"] for row in roles), roles)
        self.assertTrue(any(row.get("internal_basis") == "GOVERNANCE_LABEL_FRAME_CONTENT"
                            for row in roles), roles)
        self.assertTrue(any(r["roles"].get("governance_label") for r in roles), roles)
        # 内容段自己就内部（带着语气分档词）时不需要框架力
        _second, second_actions = client_facing_prose(INTERNAL_EXPLANATION[0])
        second_roles = [row for row in second_actions if row.get("clause_roles")][0]["clause_roles"]
        self.assertTrue(all(not row.get("internal_basis") or
                            row["internal_basis"] == "GOVERNANCE_LABEL_FRAME_CONTENT"
                            for row in second_roles), second_roles)
        content = [row for row in second_roles if not row["roles"].get("governance_label")][0]
        self.assertTrue(content["roles"]["tone_system"], content)
        self.assertEqual("", content["internal_basis"], "内容段自己就内部，不该记在框架力上")


class C4PostEditCleanup(unittest.TestCase):
    """§④：删过之后必须还是一句能读的话，而清理只做减法。"""

    def test_c4a_no_punctuation_artifact_survives_an_edit(self):
        corpus = (
            "本节先把口径分为三档，。台账按月汇总。",
            "夜间巡检记录逐条登记为编制约束，评分口径一律不进入正文，正文中也不出现对自身分档的描述。",
            "本节仅登记为编制约束，本节把上述内容统一表述为观察值。",
        )
        for text in corpus:
            output, _actions = client_facing_prose(text)
            for artifact in ARTIFACTS:
                self.assertNotIn(artifact, output, f"{text} -> {output!r}")
            self.assertFalse(re.search(r"[，、：;；]$", output.strip()), output)
            self.assertFalse(re.search(r"^[，、：;；]", output.strip()), output)

    def test_c4b_dangling_link_and_cell_fragment_are_stripped(self):
        cases = {
            "巡检、保洁和": "巡检、保洁",
            "作业记录台账与": "作业记录台账",
            "与，记录归档位置": "记录归档位置",
            "以及，另行安排": "另行安排",
            "本表标注未关闭，并": "本表标注未关闭",
            "以上安排以合同为准，；": "以上安排以合同为准；",
            "因此，": "",
        }
        for text, expected in cases.items():
            self.assertEqual(expected, _tidy_after_edit(text), text)

    def test_c4c_cleanup_never_touches_ordinary_words_that_start_the_same_way(self):
        """`与/和/及/并` 也是正常实词的首字：只在紧跟分隔符或位于句末时才剥。"""
        for text in ("和平路 200 号", "参与协调对接事项", "及以上楼层按月汇总",
                     "并表登记后按月报送甲方"):
            self.assertEqual(text, _tidy_after_edit(text), text)

    def test_c4d_cleanup_is_subtraction_only_and_a_fixed_point(self):
        for text in ("本节仅登记为编制约束，", "夜间巡检记录逐条登记为编制约束，评分口径一律不进入正文。",
                     "以上安排以合同为准，；月度从台账中取数。", "…执行，因此"):
            once = _tidy_after_edit(text)
            self.assertEqual(once, _tidy_after_edit(once), text)
            self.assertLessEqual(len(once), len(text), text)
            self.assertTrue(all(ch in text for ch in once), f"清理造出了原文没有的字：{text}->{once!r}")

    def test_c4e_idempotent_on_the_new_fixtures(self):
        for sentence in INTERNAL_EXPLANATION + SERVICE_RULES_KEPT + CUSTOMER_BOUNDARY_KEPT:
            once, _a = client_facing_prose(sentence)
            twice, _b = client_facing_prose(once)
            self.assertEqual(once, twice, sentence)


class C5StructuralSentinels(unittest.TestCase):
    """两把尺都要只有一处：文字操作判据、框架标签判据，且产品码里不写复核方原句。"""

    SOURCE = "app/governance/client_facing_prose.py"

    def test_c5a_no_reviewer_sentence_baked_into_the_rulers(self):
        source = (APP_ROOT / self.SOURCE).read_text(encoding="utf-8")
        for fragment in ("口径分层：专业负责人", "跨班次未关闭", "交接班记录并在本表",
                         "标注未关闭", "分入已确认事实"):
            self.assertNotIn(fragment, source, f"{self.SOURCE} 把复核方的句子写成了规则")

    def test_c5b_two_sided_closure_is_one_role_function(self):
        source = (APP_ROOT / self.SOURCE).read_text(encoding="utf-8")
        self.assertEqual(1, len(re.findall(r"def _clause_roles\(", source)))
        self.assertEqual(1, len(re.findall(r"def _label_only\(", source)))
        self.assertEqual(1, len(re.findall(r"def _generic_internal_move\(", source)))
        # 两栖动词的补充判据与"文档自指 + 文字谓语"这一条主体判据都只写一次：
        # 别处再写一遍就是第二把尺，under/over 两个方向会各自漂。
        self.assertEqual(1, len(re.findall(r"TEXT_ONLY_OP\.search", source)))
        self.assertEqual(1, len(re.findall(r"GOVERNANCE_LABEL\.match", source)))
        self.assertEqual(1, len(re.findall(r"FIELD_CARRIER\.search", source)))
        self.assertEqual(1, len(re.findall(r"\(doc_self or locative_self\)", source)))

    def test_c5c_frame_reach_is_guarded_by_shields_not_by_a_word_list(self):
        source = (APP_ROOT / self.SOURCE).read_text(encoding="utf-8")
        self.assertEqual(1, len(re.findall(r"def _frame_label_spillover\(", source)))
        # 框架力必须同时受"句首标签 + 甲方接口/现场实质盾"两条限制
        body = source[source.index("def _frame_label_spillover("):]
        self.assertIn("hard_shield", body)
        self.assertIn("soft_shield", body)
        self.assertIn("tone_label", body)
        self.assertIn("governance_label", body)

    def test_c5d_no_dominance_threshold_reappears(self):
        """少数派业务内容不因为"内部那一截占九成"被删：按行为量，不按词量。"""
        long_meta = ("本节先把响应内容按语气分为三档，正文里只保留已确认事实那一档，"
                     "其余两档分别按条件化方法与待澄清事项处理，不再在本节重复展开说明，"
                     "编号与需求条目之间的对应关系统一由内部登记维护。")
        mixed = f"{long_meta}报修受理须在 30 分钟内响应、2 小时内到场处置。"
        output, _actions = client_facing_prose(mixed)
        self.assertIn("30 分钟内响应", output)
        self.assertIn("2 小时内到场", output)
        self.assertNotIn("语气分为三档", output)
        # 反序也一样：业务那一截在前还是在后都不决定它是否被保留
        flipped = f"报修受理须在 30 分钟内响应、2 小时内到场处置。{long_meta}"
        output2, _b = client_facing_prose(flipped)
        self.assertIn("30 分钟内响应", output2)
        self.assertIn("2 小时内到场", output2)
        self.assertIsNone(_generic_internal_move("本表仅登记现场作业量数据"))


    def test_c5f_no_classifier_alternation_matches_the_empty_string(self):
        """`A|` + `|B` 拼起来是 `A||B`：多出来的那一格是**空分支**，规则从此处处命中。

        本轮真实踩过一次（KEEP 表拿到空分支后所有句子都"有风控内容"，形状表整表停摆），
        所以这一条按模块扫，不靠人眼读拼接后的串。切分用的零宽模式不在此列。
        """
        import governance.client_facing_prose as prose_module
        import governance.commitment_provenance as provenance_module
        import qa_severity as severity_module
        probes = ("本表登记现场作业量数据，跨班次未关闭的续填至交接班记录并在本表标注未关闭。",
                  "口径分层：本节把正文分为三档。",
                  "甲方书面确认后实施。",
                  "本项目现场为同一楼层，两侧商户共用巡检通道。")
        for module in (prose_module, provenance_module, severity_module):
            for name in dir(module):
                value = getattr(module, name)
                if not isinstance(value, re.Pattern) or name.endswith("_SPLIT"):
                    continue
                for probe in probes:
                    hit = value.search(probe)
                    self.assertTrue(hit is None or hit.group(0),
                                    f"{module.__name__}.{name} 能匹配空串：{probe[:16]}")


if __name__ == "__main__":
    unittest.main()
