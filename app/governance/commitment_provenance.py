from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any
from .rules import load_rule
from qa_severity import (
    CLASS_FALSE_POSITIVE, CLASS_NEEDS_REVIEW, CLASS_TRUE_BLOCK, clause_of,
    _clause_span, condition_governed_spans, sentence_tail_boundary, semantic_status,
)

FORMAL_RULES = load_rule("commitment_provenance_rules_v0.1.json")

# 命中这些句式时，短语只是被引用的名词（"必须上报的范围"），不是执行承诺。
DESCRIPTIVE_USE = re.compile(r"^\s*(?:的|类|等)?\s*(?:范围|事项|内容|清单|口径|标准|依据|条件|对象|规则|流程|边界|级别|情形)")
# §40：完成语在真实交付件里常常是**名词的定语**（"本节已经完成本次闭环的时间说明"），
# 说的是一个载体叫什么，不是工作已经完成。只认这个明确形状，不放宽成"看着像引用就算"。
COMPLETION_AS_MODIFIER = re.compile(
    r"^\s*[^。；，、]{0,14}的(?:时间|时间说明|说明|记录|台账|清单|索引|报告|凭证|归档)")
# §40：被问"在什么条件下必须上报"是在描述待判定的条件，不是投标人承诺上报。
INTERROGATIVE_CONDITION = re.compile(r"(?:什么|何种|哪些|多少)(?:条件|情形|情况|场景)")
# §33：被否定/被引用/条件句里的命中不构成承诺（"不新增岗位"、"被当成已经完成"）。
NON_ASSERTIVE_STATUSES = {"DENIED", "MENTION", "CONDITIONAL"}

# ---------------------------------------------------------------------------
# MMF008A-B：承诺溯源的五类语义
# ---------------------------------------------------------------------------
# 要解决的问题：C2/C5 这两类词表命中的是**词**，但一句技术标的话在问的不是"有没有这个词"，
# 而是"这句话在主张什么"。Run F 的 9 条 TRUE_BLOCK 现读下来没有一条在主张既存事实：
#   C2 六条全在描述岗位怎么排（"七人编制按定点值守、流动巡查、盯屏接单三种状态排布"）；
#   C5 三条全在写审批接口的前置条件（"未获批准不安排进场""以甲方批准的委托计划与许可为准"）。
# 把它们判成"无依据的既存事实主张"，等于要求投标人为"我打算怎么排班"出示来源——
# 而真正的编造（"本项目所有制度文件均已整理成册"）反而和它们走同一条分支。
#
# 五类里只有最后一类保留硬 BLOCK。四道非阻断类别各自要求**正面语境凭据**，
# 并且一律让位于两根阻断标尺：既存状态（已/均已/现有/既往）与义务式绝对化
# （必须/确保/每一件事/全部…）。收严的一律在前，放宽的一律在后：
# 关键词一条都不删，全局 severity 一点都不降。
PROVENANCE_SEMANTIC_CLASSES: tuple[str, ...] = (
    "EXISTING_FACT",
    "PROPOSED_SERVICE_METHOD",
    "CONDITIONAL_CONTROL",
    "FUTURE_COMMITMENT",
    "UNSUPPORTED_EXISTING_CLAIM",
)
HARD_BLOCK_PROVENANCE_CLASS = "UNSUPPORTED_EXISTING_CLAIM"
NON_BLOCKING_PROVENANCE_CLASSES: tuple[str, ...] = tuple(
    name for name in PROVENANCE_SEMANTIC_CLASSES if name != HARD_BLOCK_PROVENANCE_CLASS
)
PROVENANCE_SEMANTICS_VERSION = "commitment-provenance-semantics-v0.1"

# 阻断标尺①：既存状态。这类话一旦缺少当前项目来源，就是编造已经存在的事实。
EXISTING_STATE_MARKER = re.compile(
    r"已(?:配备|配置|建立|完成|提交|具备|组织|开展|实现|通过)|均已|已在|现有|目前在岗|"
    r"既往|曾在|历史上|迄今为止")
# 阻断标尺②：把义务写成既成安排的绝对化措辞。刻意**不收**"承诺/全部/所有"这类词 ——
# 它们在技术标里大量出现在范围限定与自查句里（"分工全部取自已确认的岗位口径"
# "固定岗位承诺不作为服务依据"），把它们当绝对化等于用词频替代语义；
# 真正需要挡的是"必须/确保/杜绝/零事故"这一类把结果打包票的形。
OBLIGATION_ABSOLUTE_MARKER = re.compile(
    r"必须|务必|确保|保证|一律|每一件事|每件|绝无|杜绝|零事故|零故障|100%无|无条件|不可撤销")
# 非阻断类别①：服务组织设计。岗位/编制/班次/布点/职责/流程 + 一个"怎么排/谁干什么"的动词。
# `承担|分管|兼顾` 是**职责分配**动词："定点值守承担主出入口的秩序维护与人员疏导"在写
# 拟议的分工，不是在声称现场已经这样分工——既存状态标尺（已/现有/均在）在本类之前判，
# 所以"本项目现有人手已承担…"仍然走硬 BLOCK。
SERVICE_DESIGN_FIELD = re.compile(
    r"岗位|编制|班次|布点|值守|巡查|排班|职责|分工|流程|工序|机制|接口|交接|台账|清单")
SERVICE_DESIGN_VERB = re.compile(
    r"布置|排布|排为|编为|编制|设置|设立|承接|负责|承担|分管|兼顾|落在|区别对待|"
    r"按.{0,12}(?:执行|排布|循环)|主要承接|以.{0,10}为单位|整批交出|不另设")
# 非阻断类别②：条件性控制机制。写的是"谁批、什么条件下才动"，不是"已经批了"。
CONDITIONAL_CONTROL_SHAPE = re.compile(
    r"经.{0,12}(?:批准|审批|同意|签批|确认|审核)(?:后|方可|才|前)|需.{0,10}(?:批准|审批|同意|确认)|"
    r"报.{0,10}(?:批准|审批|同意|签批)|(?:批准|审批|同意)(?:人|范围|材料|书面)|"
    r"以.{0,16}(?:为准|为据|为凭)|未(?:获|经|得到).{0,12}(?:不|不得|暂停|撤场|进场)|"
    r"按.{0,14}(?:书面材料|签字|委托计划|许可).{0,8}(?:填写|执行|组织)|"
    # 审批依赖材料的**名词短语形**：表格"前置条件"一格里通常只列材料名，不写成句。
    # "甲方批准的书面材料"与"甲方已批准"是两件事：前者在说"要拿到什么才动"，
    # 后者才是在主张一件事已经发生（后者由既存状态标尺先挡住，次序不可颠倒）。
    # 动词与载体都用**类**而不是给某一句开的词：批准/审批/签批/核准/签认/批复/签发/审定/
    # 出具/同意 都是"要谁点头"，材料/文件/单据/票据/凭证/报告/证明/记录/清单/复印件 都是
    # "点头之后拿回来的那张纸"。换一类工程、换一种票单，这一形照样成立。
    r"(?:甲方|乙方|招标人|业主|监理|使用方|主管部门|归口部门)"
    r"[^。；]{0,6}(?:批准|审批|签批|核准|签认|批复|签发|审定|出具|同意)(?:的|后取得的|后再补的)"
    r"[^。；]{0,10}(?:书面)?(?:材料|文件|单据|票据|凭证|报告|证明|记录|清单|复印件|台账|表样)|"
    r"待.{0,10}(?:批准|确认|澄清)|.{0,12}(?:前|后)填用")
# 非阻断类别③：未来承诺语态（拟/将/计划/中标后）。仍要让位于上面两根阻断标尺。
FUTURE_STATE_MARKER = re.compile(r"拟|将|计划|将在|中标后|进场后|服务期内(?:将)?|后续.{0,6}(?:将|安排)")

# 「这一格在不在主张一件事」：中文的断言要有谓语线索——判断词、动词性中心语，
# 或体貌/情态标记。表格里的一格岗位名（`门岗（定点值守：主出入口与营业大厅）`）
# 与一份材料清单（`…书面材料、…接口单与检测报告`）都没有谓语，
# 因此它们**不可能**是"缺少来源的既存事实主张"——它们在给方案起名字、列条目。
# 这一份是**语法功能词**清单，不是项目词表：换任何一份标书、任何一类岗位都同样成立。
ASSERTIVE_PREDICATE_MARKER = re.compile(
    r"均?已|正在|将|应当|应该|务必|必须|需要|须|确保|保证|采用|设置|设立|配备|提供|执行|组织|"
    r"安排|承担|负责|包括|包含|涵盖|形成|建立|完成|提交|整理|属(?:于)?|是指|作为|旨在|依据|按照")
# 短语形状：括号内说明（`X（Y：Z）`）、并列条目（、/与/及/和）、以及成句标点。
_PARENTHETICAL_SPEC = re.compile(r"（[^（）()]{0,40}[:：][^（）()]{2,40}）|\([^()]{0,40}[:：][^()]{2,40}\)")
_ENUMERATED_ITEMS = re.compile(r"[、，,]|(?<=[^与及和]{2})[与及和](?=[^与及和]{2})")
_SENTENCE_PUNCTUATION = re.compile(r"[。！？!?；;]")


def non_assertive_phrase_shape(clause: Any) -> str | None:
    """这一串文字是不是**没有谓语的短语**；是则给出形状名，不是则 None。

    判据全部是结构性的：有谓语线索就不是短语；出现成句标点就不是短语；
    剩下的必须长得像"名字 + 括号说明"或"并列的几样东西"，否则仍然按句处理。
    """
    text = str(clause or "").strip()
    if not text or _SENTENCE_PUNCTUATION.search(text) or ASSERTIVE_PREDICATE_MARKER.search(text):
        return None
    if _PARENTHETICAL_SPEC.search(text):
        return "LABEL_WITH_PARENTHETICAL_SPEC"
    if _ENUMERATED_ITEMS.search(text) and len(text) <= 80:
        return "ENUMERATED_NOUN_PHRASES"
    return None


PROVENANCE_SEMANTICS_LABEL: dict[str, str] = {
    "EXISTING_FACT": "有当前项目来源的既存事实",
    "PROPOSED_SERVICE_METHOD": "服务组织方式的设计描述（岗位、编制、班次、职责、流程）",
    "CONDITIONAL_CONTROL": "条件性控制机制（谁批、什么条件下才动）",
    "FUTURE_COMMITMENT": "未来履约语态的承诺",
    HARD_BLOCK_PROVENANCE_CLASS: "缺少来源的既存事实主张",
}


def _marker_hits(pattern: re.Pattern, clause: str) -> list[str]:
    return [match.group(0) for match in pattern.finditer(str(clause or ""))][:6]


def classify_provenance_semantics(
    clause: Any,
    *,
    supported: bool = False,
    claim_type: str = "",
    claim_family: str = "SEMANTIC_PATTERN",
    conditioned: bool = False,
) -> tuple[str, str, dict[str, Any]]:
    """一句话在主张什么 → (provenance_semantics, basis, evidence)。

    只看**同一句**（`clause_of` 切出来的那个分句）：跨句借来的条件词不算凭据，
    否则一段里出现一次"经…批准"就能替整段免责。

    `claim_family="COMPLETION_OR_ABSOLUTE"` 的两支（完工主张、绝对化性能承诺）与
    `"EXISTING_STATE"`（既存设施/工程状态主张）按定义就在主张既存状态或绝对结果，
    不参与"这只是服务方法描述"的辩解：只有被事实底稿逐字支持才算 EXISTING_FACT，
    其余一律留在硬 BLOCK。

    `conditioned` 由调用端从 `semantic_status` 现读传入（命中的那一截是否落在前置条件
    框架里），不在这里重算：这一层的判据**必须在 claim span 那一头取**，
    否则等于允许同段别处的一句条件话替本句的既存事实主张免责。
    """
    text = str(clause or "")
    evidence: dict[str, Any] = {
        "schema_version": PROVENANCE_SEMANTICS_VERSION,
        "claim_type": str(claim_type or ""),
        "claim_family": str(claim_family or ""),
        "clause_excerpt": text[:120],
        "markers": {},
    }
    if supported:
        evidence["markers"]["brief_support"] = ["命中短语在当前项目事实底稿里逐字在场"]
        return "EXISTING_FACT", "BRIEF_SUPPORTS_PHRASE", evidence
    existing = _marker_hits(EXISTING_STATE_MARKER, text)
    obligation = _marker_hits(OBLIGATION_ABSOLUTE_MARKER, text)
    control = _marker_hits(CONDITIONAL_CONTROL_SHAPE, text)
    design_field = _marker_hits(SERVICE_DESIGN_FIELD, text)
    design_verb = _marker_hits(SERVICE_DESIGN_VERB, text)
    future = _marker_hits(FUTURE_STATE_MARKER, text)
    evidence["markers"].update({
        "existing_state": existing, "obligation_absolute": obligation,
        "conditional_control": control, "service_design_field": design_field,
        "service_design_verb": design_verb, "future_state": future,
        "claim_span_conditioned": bool(conditioned),
    })
    # 前置条件框架先判（MMF008A5-B1）。这一档不属于"后面那四类放宽类别"：它问的不是
    # "这句话像不像服务安排"，而是"**这一截 claim 有没有被当作事实来断言**"。
    # `conditioned` 由调用端从 `semantic_status` 在**命中那一头**现读传入，
    # 作用域只到句子开头（跨逗号、不跨句末，写在命中之后的"……，假如甲方另有要求"
    # 不回头免罪），所以它借不到别处的条件词，也不是"全句免死金牌"。
    # 门槛端每一支早就按 `NON_ASSERTIVE_STATUSES` 把 CONDITIONAL 挡在 violations 之外，
    # 这一格让**登记的类别**与那道门说同一件事，不再出现
    # "类别=缺少来源的既存事实主张 / 门=条件句放行"这种自相矛盾的读数
    # ——PR-040「前提是甲方移交现有管线图，…30分钟内到场…」正是这一形：
    # "现有"在框架内被预设，不是投标人在主张甲方现场本来就有那张图。
    if conditioned:
        return "CONDITIONAL_CONTROL", "HYPOTHESIS_OR_PREMISE_FRAME_GOVERNS_CLAIM_SPAN", evidence
    # 两根阻断标尺接着判，且**不被后面那些放宽类别覆盖**：写了"已配备"或"必须…"就没有
    # "这只是在描述排班"的余地。
    if existing or obligation:
        return HARD_BLOCK_PROVENANCE_CLASS, "ASSERTED_WITHOUT_CURRENT_PROJECT_SOURCE", evidence
    if str(claim_family or "") == "SITE_PREMISE":
        # 现场构型本身没有"已/必须"两根标尺可挡，先问这一句的**谓语角色**：
        # 是替甲方断言现场本来这样，还是在写投标人自己打算怎么排。
        locative = _marker_hits(SITE_PREMISE_LOCATIVE, text)
        plan = _marker_hits(SITE_PREMISE_PLAN_MARKER, text)
        evidence["markers"]["site_locative_predicates"] = locative
        evidence["markers"]["service_plan_predicates"] = plan
        if not locative and plan and design_field:
            return ("PROPOSED_SERVICE_METHOD",
                    "SITE_PREMISE_WITHIN_PROPOSED_SERVICE_ORGANISATION", evidence)
    if str(claim_family or "") in {"COMPLETION_OR_ABSOLUTE", "SITE_PREMISE", "EXISTING_STATE"}:
        # 现场拓扑断言按定义就在声称"甲方现场本来是这样"，能替它说话的只有当前项目底稿。
        basis = {"COMPLETION_OR_ABSOLUTE": "COMPLETION_OR_ABSOLUTE_CLAIM_BY_DEFINITION",
                 "SITE_PREMISE": "SITE_PREMISE_ASSERTED_BY_DEFINITION",
                 "EXISTING_STATE": "EXISTING_STATE_ASSERTED_BY_DEFINITION"}[str(claim_family or "")]
        return HARD_BLOCK_PROVENANCE_CLASS, basis, evidence
    # 审批接口：先于"服务方法"判——"甲方批准人与批准时间按…填写"同时带岗位词与条件形，
    # 但它真正在写的是前置条件。
    if control:
        return "CONDITIONAL_CONTROL", "APPROVAL_OR_CONTROL_FLOW_CONDITION_STATED", evidence
    design_shape = non_assertive_phrase_shape(text)
    evidence["markers"]["non_assertive_phrase"] = design_shape or ""
    if design_field and design_verb:
        return "PROPOSED_SERVICE_METHOD", "SERVICE_ORGANISATION_DESCRIBED_NOT_FACT_CLAIMED", evidence
    # 岗位名/布点格这一类**没有谓语**的短语：招标表格里"门岗（定点值守：主出入口与营业大厅）"
    # 是在给一个拟设岗位命名，不在声称现场已经有这个岗。判它"缺少来源的既存事实主张"，
    # 等于要求一句没有谓语的话为它的谓语提供来源。必须有服务设计词在场，且两根阻断标尺已先判过。
    if design_shape and design_field:
        return "PROPOSED_SERVICE_METHOD", f"NON_ASSERTIVE_LAYOUT_LABEL_{design_shape}", evidence
    if future:
        return "FUTURE_COMMITMENT", "FUTURE_REGISTER_WITHOUT_EXISTING_STATE_ASSERTION", evidence
    # 什么都没有：仍然是"无来源的主张"。默认值必须是挡，不是放。
    return HARD_BLOCK_PROVENANCE_CLASS, "NO_PROVENANCE_SEMANTICS_EVIDENCE_IN_CLAUSE", evidence

SEMANTIC_PATTERNS = {
    "C1": [r"\d+(?:\.\d+)?\s*(?:人|小时|分钟|次|天|%|％)", r"日清日洁|每天清扫|按日保洁|每日保洁|每日清扫"],
    "C2": [r"固定岗|专人持续值守|定点值守|常设岗亭|新增岗位|配备\d+人"],
    "C3": [r"采用外包模式|采用外委|外委作业|专业供方承担|委托第三方|采用自营"],
    "C4": [r"封闭管理|封闭式管理|封闭管控|门禁卡|外来人员禁止进入|访客证"],
    "C5": [r"项目经理批示|由项目经理审批|(?<!报)(?<!经)甲方批准|负责审批"],
    "C6": [r"必须.{0,12}(审批|上报|复盘)"],
    "C7": [r"费用与.{0,6}(结算|付款)挂钩|考核与付款挂钩|收费标准为|收费单价为"],
    "C8": [r"24\s*小时|全天候|昼夜不间断|7\s*[×xX*]\s*24|连续值守"],
    "C9": [r"系统将自动|平台统一实现|智能识别|实时监控无死角"],
    "C10": [r"依据公司制度|按现行政策|统一制度规定"],
}

# R06 完成主张覆盖扩展（Qoder r5/r01 盲区 1）：
# ① 体貌标记与动词之间允许程度/方式副词（均已【完整】移交、已【全部】完成缴纳、
#    已【全部】交回——此前副词隔断了 已+动词 的相邻形态，三句全部漏拦）；
# ② 完成动词词表补充 移交/交回/缴纳/缴清/报送/签收（完毕 收尾可选）。
# 保持 R05 规则名行为：裸 已+完成（无程度副词、无 所有/全部 前缀）仍归
#   EXISTING_STATE_CLAIM（既存状态），不进本完工主张；因此新分支要求副词必现。
# 未来态与条件、否定、引用仍由 semantic_status 与「无 已 体貌即不匹配」把关。
UNSUPPORTED_COMPLETION = re.compile(
    r"(?:所有|全部)[^。；\n]{0,32}(?:已经|均已|已)(?:全部|全数|完整|悉数|顺利|如期|依法|按约)?"
    r"(?:提交|具备|完成|建立|整理成册|准备就绪|移交|交回|缴纳|缴清|报送|签收)(?:完毕)?|"
    r"(?:已经|均已)(?:全部|全数|完整|悉数|顺利|如期|依法|按约)?"
    r"(?:提交|具备|完成|建立|整理成册|准备就绪|移交|交回|缴纳|缴清|报送|签收)(?:完毕)?|"
    r"已(?:全部|全数|完整|悉数|顺利|如期|依法|按约)"
    r"(?:提交|具备|完成|建立|整理成册|准备就绪|移交|交回|缴纳|缴清|报送|签收)(?:完毕)?|"
    r"作为本方案附件提交",
    re.I,
)
ABSOLUTE_PERFORMANCE = re.compile(r"(?:确保[^。；\n]{0,40})?(?:零故障|零事故|100%无故障|绝不发生|完全杜绝|永久确保)", re.I)

# ---------------------------------------------------------------------------
# MMF008A5-B1：既存状态主张（第四类阻断主张，与前三支同一条"只认当前项目底稿"的姿态）
# ---------------------------------------------------------------------------
# PR-049「实操教室已配备消防模拟逃生装置」/ PR-050「项目外墙保温层已于2023年完成整体更换」
# 此前**连 claim 都不生成**（复核方量具读到的 `NO_CLAIM_DETECTED` 就是这个空洞）：
# C1–C10 吃承诺语汇，完工/绝对化两支吃结果语态，而这两句在说的是"甲方项目里一样东西
# 现在是什么状态"——三张词表都不碰它，于是它连门都进不到，谈不上判轻判重。
#
# 形状要求**三个成分同截共现**，缺一个都不算，这一条就是"禁止把所有'已'字句一律 BLOCK"的闸：
#   ① claim subject：**从小句头起**的名词性成分（不跨 。；，、：，2–30 字）。
#      "已配备"前面没有主语，就不是一句关于既存状态的主张（无主语的短语、或谓语在别处）；
#   ② 既存/历史状态体貌：已 / 已经 / 均已 / 已于某年；
#   ③ 状态或工程**谓语**：设施与工程类动词。刻意**不收**"已确认/已获得/已明确"这类认知动词
#      ——它们在技术标里大量指招标文件自己已给出的内容（"按招标文件已明确的…"），
#      收进来就是拿词频替代语义；也刻意不收"具备/提交/整理成册"，那一截归完工主张管
#      （同一截文字不重复计两条主张，见下面 family 循环里的 overlap 判据）。
# 支持与否仍然走同一条 `_brief_supports`：当前项目事实底稿里逐字在场才算 EXISTING_FACT。
EXISTING_STATE_CLAIM = re.compile(
    r"(?P<subject>[^。；，、：:！？\n]{2,30}?)"
    r"(?P<aspect>已于\s*(?:19|20)?\d{2}\s*年(?:度)?|已经|均已|已)"
    r"(?:(?:全部|全数|完整|悉数|顺利|如期|依法|按约)?)"
    r"(?P<state>配备|配置|安装|加装|设置|建设|建成|建立|部署|投用|投入使用|投运|完成|更换|改造|实施|升级|扩容|铺设|修复|翻新|落成|验收|交付|实现|移交|交回|缴纳|缴清|报送|签收|通过(?:年检|验收|检测))"
    r"(?P<aspect_tail>了|过|完毕)?")
# 定语那一格（Run F 553 段真实正文扫出来的一条误挡，先修判据再引数）：
# 「项目负责人按周抽查已完成条目」里 `已` 的**前一个词是检查类动词**，
# 说明 已完成 是那个动词的**宾语内部的定语**（"已被完成的那些条目"），
# 不是投标人在主张"抽查这件事已经完成"。检查类动词这一族是封闭的、有语义身份的：
# 它们的宾语天生是"记录/条目/事项"这一类可数的载体，所以"已X"落在它们后面时，
# 读定语比读谓语更可能。两个条件缺一不可：紧贴在 `已` 之前，且中间不跨逗号。
# 反面对照：PR-049「实操教室已配备…」`已` 前是名词、PR-050「…已于2023年完成…」`已` 前是年份，
# 都落不进这一格，照常硬 BLOCK。命中时**留在 suppressed 里可查**，不静默丢掉。
STATE_UNDER_INSPECTION_VERB = re.compile(
    r"(?:抽查|检查|核验|复核|复查|查验|查阅|审阅|统计|汇总|盘点|梳理|跟踪|评估|考核|核对)$")

# ---------------------------------------------------------------------------
# MMF008A2-D：现场拓扑断言（第三类阻断主张，与前两支同一条"只认当前项目底稿"的姿态）
# ---------------------------------------------------------------------------
# C1–C10 吃的是**承诺语汇**（人数、班次、值守、审批、外包…），完工/绝对化两支吃的是**结果语态**。
# 但 Run F 被判"应当阻断"的两句（复核方 P-BND-07/08）里一个承诺词都没有：它们写的是
# "本项目位于单一地块""四类作业在同一空间内共用出入口"——**在替甲方描述甲方自己的现场**。
# 这类句子走不到任何一根标尺，`evaluate_commitments` 连 claim 都不生成（复核方量具读到的
# `NO_C_RULE_HIT_IN_SENTENCE` 就是这个空洞），所以放宽前两支的语境判据永远救不了它：
# 缺的不是判轻判重，是一整类断言。
#
# 形状要求**两个成分同句共现**，缺一个都不算：
#   ① 数量词 + 空间构件：(单一|单个|同一) + (地块|院落|园区|楼栋|楼层|建筑|空间|区域)
#   ② 共位谓语：共用|共享|重合|叠加|协同|相接|共处|多专业|多业态
# 只写①的是"同一区域同类问题重复出现的…"（范围限定），只写②的是"与出入口岗位协同…"
# （作业配合），都不是在断言现场构型。"共用部位/共用设施设备"是物业管理委托范围的**术语**，
# 不在构件表里，收进来就等于把招标文件自己的话判成投标人编造。
SITE_PREMISE_SPATIAL = re.compile(
    r"(?:单一|单个|同一|同)(?:地块|院落|院子|园区|楼栋|楼层|建筑|空间|区域)")
SITE_PREMISE_COLOCATION = re.compile(r"共用|共享|重合|叠加|协同|相接|共处|多专业|多业态")
# MMF008A4-B：同一个空间词在四种句子角色里只有一种是"替甲方描述甲方的地"。
# - 既存事实断言（BLOCK）：处所/判断谓语把构型写成现场本来这样——位于/地处/设有/现有/为/属；
# - 拟议的服务组织（非阻断）：谓语是投标人自己要怎么排——拟/将/安排/编排/组建/计划/建议，
#   且同句带着服务设计字段（岗位/班次/布点/台账/流程…）。
# 默认仍是挡：要走这一格，计划谓语与服务设计字段必须**同时**在场，且句中不得有任何
# 处所/既存谓语。两条尺打架时既存状态优先（"本项目现有 X，拟将…编为共用…"照样硬 BLOCK）。
SITE_PREMISE_LOCATIVE = re.compile(
    r"位于|坐落于|地处|处在|处于|设有|建有|现有|已有|既往|属|构成|即为|现场为|实际为")
SITE_PREMISE_PLAN_MARKER = re.compile(
    r"拟(?:将|设|安排|采用|以|按)?|计划|规划|安排|编排|组建|将在|将|建议|可考虑")

# 修复端与门槛端同一条尺（§45–§47 的"门严修宽"教训）：断言短语整体去掉，
# 剩下的那句"四类作业共用出入口"仍然把要说的话说完，客户读到的不再是未经核验的现场构型。
SITE_PREMISE_REPAIR = re.compile(
    r"(?:位于|坐落于|地处|在|处于|为|属)?(?:单一|单个|同一|同)"
    r"(?:地块|院落|院子|园区|楼栋|楼层|建筑|空间|区域)(?:内|中|里)?")


def site_premise_claims(sentence: Any) -> list[re.Match[str]]:
    """这一句是不是在断言"本项目现场的空间构型"；返回断言短语（用来查底稿）。"""
    text = str(sentence or "")
    if not SITE_PREMISE_COLOCATION.search(text):
        return []
    return [match for match in SITE_PREMISE_SPATIAL.finditer(text)]


def _sentences(text: str) -> list[tuple[int, str]]:
    """(句起点, 句子)：共位断言的判据范围是**一句**，不是一个字串——同段别处的"共用"不借。"""
    out, start = [], 0
    for index, char in enumerate(str(text or "")):
        if char in "。；;":
            if index > start:
                out.append((start, text[start:index + 1]))
            start = index + 1
    if start < len(text):
        out.append((start, text[start:]))
    return out


def _walk_strings(value: Any, path: str = "artifact") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        return [item for k, v in value.items() for item in _walk_strings(v, f"{path}.{k}")]
    if isinstance(value, list):
        return [item for i, v in enumerate(value) for item in _walk_strings(v, f"{path}[{i}]")]
    return [(path, value)] if isinstance(value, str) else []


def _brief_supports(brief_text: str, matched: str) -> bool:
    # Compatibility projection of the single source-fact locator below.
    return _source_fact_span(brief_text, matched) is not None


# MMF-008B：一个 claim 只在这里形成语义决策。qa_severity 提供句法 span，
# provenance/repair/final DOCX QA 消费同一份记录，不再在各自的 window 里重判条件。
CLAIM_SEMANTIC_AUTHORITY_VERSION = "canonical-claim-semantics-v1"
_SOURCE_UNCERTAIN = re.compile(r"尚待|待确认|待核实|未确认|尚未确认|是否|能否|可否|未知|不确定")
_SOURCE_NEGATED = re.compile(r"不(?:需|予|会|得|应|须|必|配备|安装|建设|改造|更换|完成)|未(?:配备|安装|建设|改造|更换|完成)|无需|不得")
_SOURCE_OBLIGATION = re.compile(r"应当|应|必须|须|要求|需|按.{0,12}执行")
_SOURCE_PROPOSITION_BREAK = re.compile(r"[。；;，,：:\n]")
_OBJECT_BREAK = re.compile(r"[。；;，,：:\n]|(?<=.{4})(?:并|且|以及|同时|以便)")


def _source_fragments(source: Any, path: str = "brief") -> list[tuple[str, str]]:
    if isinstance(source, dict):
        return [item for key, value in source.items()
                for item in _source_fragments(value, f"{path}.{key}")]
    if isinstance(source, list):
        return [item for index, value in enumerate(source)
                for item in _source_fragments(value, f"{path}[{index}]")]
    return [(path, source)] if isinstance(source, str) else []


def _source_fact_span(source: Any, phrase: str) -> dict[str, Any] | None:
    needle = re.sub(r"\s+", "", str(phrase or ""))
    if not needle:
        return None
    for path, fragment in _source_fragments(source):
        positions = [index for index, char in enumerate(fragment) if not char.isspace()]
        compact = "".join(fragment[index] for index in positions)
        for hit in re.finditer(re.escape(needle), compact):
            left, right = hit.start(), hit.end()
            context = compact[max(0, left - 14):right + 14]
            if _SOURCE_UNCERTAIN.search(context):
                continue
            return {"path": path, "span": [positions[left], positions[right - 1] + 1],
                    "text": fragment[positions[left]:positions[right - 1] + 1]}
    return None


def _target_object_span(text: str, predicate_end: int) -> tuple[int, int] | None:
    tail = text[predicate_end:]
    boundary = _OBJECT_BREAK.search(tail)
    right = predicate_end + (boundary.start() if boundary else len(tail))
    while right > predicate_end and text[right - 1].isspace():
        right -= 1
    left = predicate_end
    while left < right and text[left].isspace():
        left += 1
    return (left, right) if right > left else None


def _completion_object_span(text: str, aspect_start: int) -> tuple[int, int] | None:
    """Completion's patient may precede 已经+predicate (服务台账已经建立)."""
    left, _ = _clause_span(text, aspect_start)
    prefix = text[left:aspect_start]
    head = re.match(r"\s*(?:(?:本项目|本方案|我方|本公司|各项|所有|全部|相关)\s*)*", prefix)
    start = left + (head.end() if head else 0)
    while start < aspect_start and text[start].isspace():
        start += 1
    return (start, aspect_start) if aspect_start - start >= 4 else None


def _bind_obligation_source(source: Any, predicate: str, object_text: str) -> dict[str, Any]:
    """只接受一条 proposition 内的 obligation→predicate→target object 关系。"""
    predicate = str(predicate or "").strip()
    target = re.sub(r"\s+", "", str(object_text or ""))
    empty = {"status": "UNBOUND", "obligation_source": "", "source_span": None}
    if not predicate or len(target) < 4:
        return empty
    for path, fragment in _source_fragments(source):
        start = 0
        for boundary in list(_SOURCE_PROPOSITION_BREAK.finditer(fragment)) + [None]:
            end = boundary.start() if boundary else len(fragment)
            proposition = fragment[start:end]
            if _SOURCE_UNCERTAIN.search(proposition) or _SOURCE_NEGATED.search(proposition):
                start = boundary.end() if boundary else len(fragment)
                continue
            for marker in _SOURCE_OBLIGATION.finditer(proposition):
                for action in re.finditer(re.escape(predicate), proposition[marker.end():]):
                    pred_start = marker.end() + action.start()
                    pred_end = pred_start + len(predicate)
                    # Require a direct object. Arbitrary intervening text can contain
                    # another finite action and makes sentence co-occurrence unsafe.
                    between = proposition[pred_end:]
                    object_hit = re.match(r"\s*(?:相应的|必要的|相关的)?" + re.escape(target), between)
                    if object_hit is None:
                        continue
                    source_left = start + marker.start()
                    source_right = start + pred_end + object_hit.end()
                    return {"status": "VERIFIED", "obligation_source": fragment[start:end].strip(),
                            "source_span": {"path": path, "span": [source_left, source_right],
                                            "text": fragment[source_left:source_right]}}
            start = boundary.end() if boundary else len(fragment)
    return empty


# ---------------------------------------------------------------------------
# R05 QA 上下文更正（correction_01 / ZCode T-1 修复端）：四类可放行语境的结构性判据。
#
# E 审查（terminal_review_r4）对 R04 终稿 15 条 TRUE_BLOCK 的逐条精读结论是
# 0 确证 / 1 语义争议（692）/ 14 误判候选，误判集中在四类语境：
#   ① 表单使用条件（需要…时当场开始使用本单 / 适用场景：…时使用）；
#   ② 反事实风险说明（否则… / 等到…时… / 失效条件+时，… / 写着…而该…）；
#   ③ 台账类别定义（失败条件+的，+ 说明/写明…）；
#   ④ 信念转述（都以为…）与文档自指（在方案中已落成表格与条目——可逐件定位）。
# 这些判据全部是**结构 + 同句范围**的：框架词与被标记 span 必须同句，且 span 落在
# 框架覆盖范围内；同句另一分句的现实主张不在框架 span 里，照常走门槛。
# 没有任何一条按块编号、按文件或按"同一区域"子串特判；真实无来源主张的
# 反向差分由 tests/test_run_k_r05_qa_context.py 逐类覆盖。
FORM_USAGE_NEED_USE = re.compile(
    r"需要[^。；;，,|\n]{0,60}时(?:当场)?(?:开始)?使用(?:本单|该单|此单|本表|该表|如下表|表格)?")
APPLICABLE_SCENARIO_FRAME = re.compile(r"适用场景[:：][^。；;，,|\n]{0,100}时使用")
COUNTERFACTUAL_PREFIX = re.compile(r"(?:否则|不然)[^。；;，,|\n]{0,80}$")
AWAITED_FRAME_PREFIX = re.compile(r"等到[^。；;，,|\n]{0,40}时[^。；;，,|\n]{0,40}$")
FAILURE_CONDITION_TAIL = re.compile(
    r"(?:不同步|不一致|未同步|未回写)[^。；;，,|\n]{0,8}时[，,][^。；;，,|\n]{0,70}$")
LEDGER_CATEGORY_HEAD = re.compile(
    r"(?:仍无回应|无回应|未取得|无法联系|无法取得)[^。；;，,|\n]{0,8}的[，,][^。；;，,|\n]{0,60}$")
STATEMENT_VERB_TAIL = re.compile(r"(?:说明|写明|注明|标注|显示|登记)[^。；;，,|\n]{0,16}$")
BELIEF_MARKER_TAIL = re.compile(r"(?:以为|误认为|误以为|自以为)[^。；;，,|\n]{0,12}$")
AVOIDANCE_MARKER = re.compile(r"防止|避免|防范|杜绝")
CONTRASTIVE_HYPOTHETICAL = re.compile(r"写着[^。；;\n]{0,60}而该")
OWNER_REGULATION_DELEGATION = re.compile(r"(?:按|依|由)甲方(?:书面)?(?:规定|确认|决定)")
NUMBERING_HEAD_TAIL = re.compile(r"^编号(?:口径|规则|约定|方法|体系)")
SELF_REFERENCE_CLAIM = re.compile(
    r"在(?:本)?方案中[^。；;\n]{0,24}已(?:经)?(?:落成|列入|列成|编制|制成)")


def _r05_context_role(local: str, span_start: int, span_end: int, family: str) -> dict[str, Any] | None:
    """被标记 span 在**同一句**内是否落在可放行的语境框架里；是则给角色名。

    `local` 是 claim 所在的整句（`_claim_usage` 已按句切好），`span_start/span_end`
    是被标记短语在 local 内的偏移。每一条判据都要求**框架覆盖 span**：
    框架词先于 span、框架收尾在 span 之后——同句另一分句的现实主张不在框架里。
    """
    before = local[:span_start]
    # A comma ends an unmarked governor; a table-cell boundary always ends it.
    # The explicit failure/category forms below may cross their one condition comma.
    clause_left = max((m.end() for m in re.finditer(r"[，,；;|\n]", before)), default=0)
    clause_right_match = re.search(r"[，,；;|\n]", local[span_end:])
    clause_right = span_end + clause_right_match.start() if clause_right_match else len(local)
    immediate_before = local[clause_left:span_start]
    immediate_after = local[span_end:clause_right]
    def bound(role: str, left: int = clause_left, right: int = clause_right) -> dict[str, Any]:
        return {'role': role, 'governor_span': [left, right], 'text': local[left:right]}
    if family == "SITE_PREMISE" and NUMBERING_HEAD_TAIL.match(local[span_end:]):
        return bound("NUMBERING_CONVENTION_ALIGNMENT")
    for frame in (FORM_USAGE_NEED_USE, APPLICABLE_SCENARIO_FRAME):
        match = frame.search(local)
        if match and match.start() <= span_start and span_end <= match.end():
            return bound("FORM_USAGE_CONDITION", match.start(), match.end())
    counter = (COUNTERFACTUAL_PREFIX.search(before) or AWAITED_FRAME_PREFIX.search(before)
               or FAILURE_CONDITION_TAIL.search(before))
    if counter:
        return bound("COUNTERFACTUAL_RISK_DESCRIPTION", counter.start())
    category = LEDGER_CATEGORY_HEAD.search(before)
    if category and STATEMENT_VERB_TAIL.search(before):
        return bound("LEDGER_CATEGORY_DESCRIPTION", category.start())
    belief = BELIEF_MARKER_TAIL.search(before)
    if belief:
        return bound("BELIEF_REPORT_NOT_ASSERTION", belief.start())
    if AVOIDANCE_MARKER.search(immediate_before) and family in {"EXISTING_STATE", "COMPLETION_OR_ABSOLUTE"}:
        return bound("AVOIDED_FAILURE_DESCRIPTION")
    contrast = re.search(r"(?:文本|清单|记录)[^。；;，,|\n]{0,16}写着[^。；;，,|\n]{0,60}[，,]?而该[^。；;，,|\n]*$", before)
    if contrast and family == "EXISTING_STATE":
        return bound("CONTRASTIVE_HYPOTHETICAL_DESCRIPTION", contrast.start())
    if (family == "SEMANTIC_PATTERN" and
            re.match(r"(?:的)?(?:具体)?取值(?:应|须|需)?", immediate_after) and
            OWNER_REGULATION_DELEGATION.search(immediate_after)):
        return bound("OWNER_REGULATION_DELEGATION")
    return None


def _claim_usage(text: str, start: int, end: int, family: str,
                 context: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Bind a process use to its grammatical governor, inside the existing authority.

    A checklist elsewhere in the paragraph cannot exempt an independent assertion.
    Installation/configuration claims never inherit a completion-label exemption.
    """
    breaks = list(re.finditer(r"[。.!！？\n]", text[:start]))
    left = breaks[-1].end() if breaks else 0
    tail = re.search(r"[。.!！？\n]", text[end:])
    right = end + tail.start() if tail else len(text)
    sentence = text[left:right]
    prefix, suffix = text[left:start], text[end:right]
    reset = re.search(r"(?:此外|另外|另一方面|另有|且|并且|而且)[^，,；;]*$", prefix)
    local_left = left + reset.end() if reset else left
    local = text[local_left:right]
    role = None
    # Spatial words used as record identities or as the antecedent of a temporal
    # operating condition do not assert the physical topology of the site.
    if family == "SITE_PREMISE":
        if re.search(r"事实.{0,20}(?:检查项|核对项).{0,12}指向.{0,12}记录", local) and not SITE_PREMISE_LOCATIVE.search(local):
            role = "RECORD_IDENTITY_REFERENCE"
        elif re.match(r"(?:同一|同)区域[^。；;]{0,35}(?:时|情况下)[，,]", text[start:right]):
            role = "OPERATING_CONDITION"
    else:
        # Scope is the immediate state-label predicate, not arbitrary prose that
        # happens to mention a state. The completion patient must be a workflow
        # item, never a facility whose existence requires source support.
        completion = bool(re.search(r"已(?:经)?(?:完成|交付)", text[start:end]) or re.match(r"[:：]", suffix))
        facility = bool(re.search(r"已(?:配备|配置|安装|建成|建设|改造|更换)|已于", text[start:end]))
        facility = facility or bool(re.match(r'(?:整体|全部)?(?:改造|更换|建设|安装|扩容|升级)', suffix))
        if not facility and completion:
            # A semicolon ends an unlabelled criterion proposition. An
            # independent completion cannot borrow a previous criterion head.
            criterion_left = max(local_left, text.rfind(';', local_left, start) + 1,
                                 text.rfind('；', local_left, start) + 1)
            governed_prefix = text[criterion_left:start]
            if re.search(r"(?:状态(?:固定|改记|设定)?为|反馈口径按)[^。\n]{0,100}$", governed_prefix):
                role = "STATE_LABEL"
            elif (re.search(r"(?:状态|形态|阶段)[:：](?:(?:未开始|已开始|未完成|已完成|受阻|待确认|待处理)[、与或]?){0,5}$", governed_prefix)
                  and re.match(r"[与或、](?:未开始|已开始|未完成|已完成|受阻|待确认|待处理)", suffix)):
                role = "STATE_ENUMERATION"
            elif re.search(r"(?:已受理|已派单)[:：][^。.]{1,55}[；;]$", governed_prefix) and re.match(r"[:：]", suffix):
                role = "STATE_LABEL"
            elif re.search(r"(?:交代|交接写明|交接.{0,8}写明|复核|抽查|核对)[^。；;，,]{0,16}$", governed_prefix):
                role = "OPERATING_INSTRUCTION"
            elif re.search(r"(?:关闭|验收|放行)(?:条件|判据)(?:为|是|包括|[:：])[^。\n]{0,65}$", governed_prefix):
                role = "CLOSURE_CRITERION"
            elif re.search(r"(?:关闭只认|字段设计对应)[^。\n]{0,18}(?:关闭)?形态[:：，,]?[^。\n]{0,35}$", governed_prefix):
                role = "CLOSURE_STATE_ENUMERATION"
            elif re.match(r'(?:的)?(?:动作|清单|条目|记录)', suffix) and (
                    re.search(r'(?:反馈|交代|复核|抽查|核对|回查|统计|跟踪|对)[^。；;，,]{0,28}$', governed_prefix)
                    or re.search(r'逐条(?:复核|核对|回查)', suffix)):
                role = "WORKFLOW_ITEM_ATTRIBUTE"
            elif re.match(r"[^。；;，,]{2,18}核验[:：]", local) and re.search(r"条目|岗位|交接|修订|签认", local):
                role = "VERIFICATION_CHECKLIST"
            elif "|" in sentence and re.search(r"已完成但[^|]{0,25}(?:未确认|空缺)", sentence) and re.search(r"保留开放状态|补录后|重新确认", sentence):
                role = "FAILURE_SCENARIO"
            elif (context and context.get('kind') == 'process_checklist'
                  and re.search(r'(?:首次响应|处置措施|处置动作|受理登记|交接条目)[^。；;，,]{0,4}$', governed_prefix)):
                # A list in a process is not evidence that an event happened.
                # Without an explicit criterion governor it remains reviewable,
                # rather than being silently accepted as a non-assertion.
                role = "PROCESS_CHECKLIST_REVIEW"
        # A service instruction prescribing who does what at a time is a future
        # method, not an assertion that the project already has a response SLA.
        if family == "SEMANTIC_PATTERN" and not EXISTING_STATE_MARKER.search(local):
            if re.search(r"(?:负责人|责任岗|值班岗)[^。；;，,]{0,8}在\d+分钟内[^。；;]{0,45}(?:指定|安排|组织)", local):
                role = "PROPOSED_OPERATING_INSTRUCTION"
            elif re.search(r"按[“\"].{2,65}[”\"](?:四|三|五|六|\d+)类岗位展开", local):
                role = "SERVICE_ORGANISATION"
            elif not re.search(r'现行|现有|既有制度|制度规定|已经|既往', local) and (
                    re.search(r'[^。；;]{1,45}(?:事项|作业)[^。；;]{0,12}必须事前上报', local)
                    or re.match(r'甲方负责审批(?:作业|检修|维护|维保)[^。；;]{1,45}', local)):
                role = "EXTERNAL_APPROVAL_METHOD_REVIEW"
    # R05 QA 上下文更正：放在既有角色之后作为补充判据（不回改任何既有角色的行为）。
    # 全部逐句取 span，见 _r05_context_role 的说明与差分测试。
    if not role:
        context_role = _r05_context_role(local, start - local_left, end - local_left, family)
        if context_role:
            lo, hi = context_role['governor_span']
            return {**context_role, 'governor_span': [local_left + lo, local_left + hi]}
    if role and local_left <= start and end <= right:
        return {"role": role, "governor_span": [local_left, right],
                "text": text[local_left:right]}
    return None


def canonical_claim_record(text: str, *, claim_type: str, claim_family: str,
                           claim_span: tuple[int, int], path: str = "artifact",
                           modality_probe_span: tuple[int, int] | None = None,
                           subject_span: tuple[int, int] | None = None,
                           predicate_span: tuple[int, int] | None = None,
                           object_span: tuple[int, int] | None = None,
                           source: Any = None, source_phrase: str | None = None,
                           context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Single decision record consumed by provenance, repair and final artifact QA."""
    start, end = claim_span
    left, right = _clause_span(text, start)
    probe_start, probe_end = modality_probe_span or claim_span
    modality = semantic_status(text, probe_start, probe_end)
    candidate_span = [start, end]
    # Candidate regexes can include a relative subject before the frame:
    # "缺少复验的条目即使现场已完成". Resolve the proposition from the frame
    # inside that candidate subject, retaining the original span for audit.
    # Never extend the governed frame backwards or exempt a preceding predicate.
    if modality == 'CONDITIONAL' and subject_span:
        embedded = next(((lo, hi) for lo, hi in condition_governed_spans(text)
                         if subject_span[0] <= lo < subject_span[1] and lo <= probe_start and end <= hi), None)
        if embedded and start < embedded[0] and not re.search(
                r'已(?:经|于)?|现有|既往', text[start:embedded[0]]):
            start = embedded[0]
    governed = next(([lo, hi] for lo, hi in condition_governed_spans(text)
                     if lo <= start and end <= hi), None) if modality == "CONDITIONAL" else None
    if modality == "CONDITIONAL" and governed is None:
        modality = "ASSERTED"
    source_span = _source_fact_span(source, source_phrase or text[start:end])
    supported = source_span is not None
    subject = text[slice(*subject_span)] if subject_span else ""
    predicate = text[slice(*predicate_span)] if predicate_span else ""
    obj = text[slice(*object_span)] if object_span else ""
    semantics, basis, evidence = classify_provenance_semantics(
        text[left:right], supported=supported, claim_type=claim_type,
        claim_family=claim_family, conditioned=(modality == "CONDITIONAL"))
    usage = _claim_usage(text, probe_start, probe_end, claim_family, context)
    if usage and not supported and modality == "ASSERTED":
        semantics = "PROPOSED_SERVICE_METHOD"
        basis = "GRAMMATICAL_PROCESS_ROLE_" + usage["role"]
        evidence["grammatical_usage"] = usage
    disposition = unsupported_fact_disposition(
        compact_brief=source, supported=supported, predicate=predicate,
        keys=[obj] if obj else [])
    binding = disposition["obligation_binding"]
    repair = (DISPOSITION_KEEP_FACT if supported else
              "KEEP_NON_ASSERTIVE" if modality != "ASSERTED" or
              semantics in NON_BLOCKING_PROVENANCE_CLASSES else disposition["action"])
    gate = "SUPPRESS" if supported or modality != "ASSERTED" or semantics in NON_BLOCKING_PROVENANCE_CLASSES else "BLOCK"
    if usage and usage['role'].endswith('_REVIEW') and not supported and modality == 'ASSERTED':
        gate = 'REVIEW'
    digest = hashlib.sha256(f"{path}\0{claim_type}\0{start}:{end}\0{text}".encode("utf-8")).hexdigest()[:20]
    return {"schema_version": CLAIM_SEMANTIC_AUTHORITY_VERSION, "claim_id": f"CLAIM-{digest}",
            "claim_type": claim_type, "claim_family": claim_family, "claim_span": [start, end],
            "candidate_span": candidate_span,
            "modality_probe_span": [probe_start, probe_end],
            "subject_span": list(subject_span) if subject_span else None,
            "predicate_span": list(predicate_span) if predicate_span else None,
            "object_span": list(object_span) if object_span else None,
            "clause_span": [left, right], "modality": modality,
            "modality_governed_span": governed, "assertion_type": semantics,
            "usage_role": usage,
            "source_support": supported, "source_support_span": source_span,
            "obligation_binding": binding,
            "decision": {"gate": gate, "repair": repair,
                         "tone_review": bool(INTERROGATIVE_CONDITION.search(text[left:right]))},
            "provenance_semantics_basis": basis, "provenance_semantics_evidence": evidence,
            "subject": subject, "predicate": predicate, "object": obj}


def _self_reference_items(before_claim: str) -> list[str]:
    """从自指句**之前**的正文里取枚举件（“包含 N 件…：A、B、以及C”）。

    只取最后一条“件数：”式枚举；件以顿号/逗号/以及/和切开，保留 ≥4 字的件
    （更短的碎片定位力不足）。取不到枚举 → 空列表，自指照旧走 BLOCK。
    """
    heads = re.findall(r"包含([一二三四五六七八九十]|\d+)件[^。；;\n]{0,20}[:：]([^。；;\n]+)", before_claim)
    if not heads:
        return []
    count, raw = heads[-1]
    expected = int(count) if count.isdigit() else '一二三四五六七八九十'.index(count) + 1
    items = [part.strip() for part in re.split(r"[、，,]|以及", raw) if part.strip()]
    return items if len(items) == expected and all(len(p) >= 4 for p in items) else []


def _carrier_item_matches(item: str, text: str) -> bool:
    """Match the entire enumerated subject, including conjuncts or responsibility.

    Connective normalization is lexical; uncertainty or mere mention is not a carrier.
    Table callers supply a header and one data row, never unrelated cells from many rows.
    """
    normalize = lambda s: re.sub(r"及其|以及|各项|相关|所需|如何|由谁|[的与和时将\s]", "", s)
    if re.search(r"尚待|暂不确定|待澄清|另行通知|可能缺少", text):
        return False
    parts = [normalize(s) for s in re.split(r"及其|将如何|由谁", item)]
    if '由谁' in item:
        subject = normalize(parts[0])
        # Both the nonactivation condition and an explicit actor/action must be present.
        remaining_subject = subject.removeprefix('未启用')
        return (('未启用' not in subject or '未启用' in text) and
                bool(remaining_subject) and remaining_subject in normalize(text) and
                bool(re.search(r"(?:由[^。；;|\n]{1,30}(?:承担|执行)|本团队自行承担)", text)))
    normalized = normalize(text)
    return all(part and part in normalized for part in parts)


def _locate_item(item: str, texts_by_path: dict[str, str], claim_path: str,
                 contexts: dict[str, dict[str, Any]] | None = None) -> str | None:
    """Require a structured carrier in the claim's own heading section."""
    contexts = contexts or {}
    claim_context = contexts.get(claim_path)
    if not claim_context:
        return None
    for path, text in texts_by_path.items():
        if path == claim_path:
            continue
        context = contexts.get(path, {})
        if context.get('section_id') != claim_context.get('section_id'):
            continue
        if context.get('block_kind') == 'table':
            rows = text.split('\n')
            if len(rows) < 2:
                continue
            if any(_carrier_item_matches(item, rows[0] + '\n' + row) for row in rows[1:]):
                return path
        elif context.get('structured_list') and _carrier_item_matches(item, text):
            return path
    return None


def evaluate_commitments(brief: dict[str, Any], contracts: list[dict[str, Any]], generated: dict[str, Any],
                         *, claim_contexts: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    if len(FORMAL_RULES.get("claim_types", [])) != 10:
        raise ValueError("Formal Commitment rules must define C1-C10")
    violations: list[dict[str, Any]] = []
    review_items: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    for path, text in _walk_strings(generated.get("artifact", generated)):
        context = (claim_contexts or {}).get(path)
        for claim_type, patterns in SEMANTIC_PATTERNS.items():
            for pattern in patterns:
                for match in re.finditer(pattern, text, re.I):
                    phrase = match.group(0)
                    record = canonical_claim_record(
                        text, claim_type=claim_type, claim_family="SEMANTIC_PATTERN",
                        claim_span=match.span(), path=path, source=brief,
                        source_phrase=phrase, context=context)
                    supported = record["source_support"]
                    semantic = record["modality"]
                    sentence = clause_of(text, match.start(), match.end())
                    provenance = "PROJECT_FACT" if supported else (
                        "NEGATED_DISCLAIMER" if semantic in NON_ASSERTIVE_STATUSES else
                        "CONDITIONAL_KU" if record["assertion_type"] in NON_BLOCKING_PROVENANCE_CLASSES
                        else "UNSUPPORTED")
                    language = "FACT" if supported else (
                        "DISCLAIMER" if semantic in NON_ASSERTIVE_STATUSES else
                        "CONDITIONAL_METHOD" if record["assertion_type"] in NON_BLOCKING_PROVENANCE_CLASSES
                        else "COMMITMENT")
                    semantics = record["assertion_type"]
                    semantics_basis = record["provenance_semantics_basis"]
                    semantics_evidence = record["provenance_semantics_evidence"]
                    row = {"claim_type": claim_type, "text": phrase, "path": path, "provenance": provenance,
                           "language_level": language, "semantic_status": semantic,
                           "provenance_semantics": semantics,
                           "provenance_semantics_basis": semantics_basis,
                           "provenance_semantics_evidence": semantics_evidence,
                           "sentence": sentence, "canonical_claim_record": record}
                    claims.append(row)
                    if record['decision']['gate'] == 'REVIEW':
                        review_items.append({**row, 'severity': 'NEEDS_REVIEW',
                                             'classification_reason': semantics_basis,
                                             'reason': 'Proposed external approval interface requires confirmation.'})
                        continue
                    if supported:
                        continue
                    if semantic in NON_ASSERTIVE_STATUSES:
                        # §33：这是本轮 12 条误报的同一根因 —— "不新增岗位"是合规免责声明，
                        # 不是无依据承诺。逐条留在 suppressed 里，不静默丢掉。
                        suppressed.append({**row, "claim_path": path, "qa_class": CLASS_FALSE_POSITIVE,
                                           "classification_reason": f"命中处于{semantic}语境，是否定式免责或被引用句式，不构成承诺"})
                        continue
                    if claim_type in {"C5", "C6"} and (
                            DESCRIPTIVE_USE.match(text[match.end():match.end() + 12])
                            or record["decision"]["tone_review"]):
                        # §40：两种"看着像承诺"的真实句式 —— 短语是被定义的对象（"必须上报的
                        # 范围"），或整句在问"什么条件下"要上报。都交人工确认语气，不直接判承诺。
                        review_items.append({**row, "severity": "NEEDS_REVIEW", "qa_class": CLASS_NEEDS_REVIEW,
                                             "repair_action": "RP-REVIEW-TONE",
                                             "classification_reason": "短语在句中是被定义的对象或条件问句（范围/事项/条件），不是投标人自身的审批或上报承诺",
                                             "reason": "承诺语态存疑：句式写的是制度对象，需人工确认语气。"})
                        continue
                    repair = "RP-DOWNGRADE-RECOMMENDATION"
                    if claim_type in {"C2", "C3", "C4", "C7", "C8"}:
                        repair = "RP-CONDITIONALIZE"
                    if semantics in NON_BLOCKING_PROVENANCE_CLASSES:
                        # MMF008A-B：这一句不是"缺少来源的既存事实主张"。它不阻断交付，
                        # 但**逐条留在 suppressed 里**（类别 + 凭据 + 原句），
                        # 读门槛的人能看见是哪几类词被放过、为什么放过。
                        suppressed.append({**row, "claim_path": path, "qa_class": CLASS_FALSE_POSITIVE,
                                           "provenance_semantics": semantics,
                                           "classification_reason": (
                                               f"这一句在写「{PROVENANCE_SEMANTICS_LABEL[semantics]}」"
                                               f"（凭据 {semantics_basis}），"
                                               "不是缺少当前项目来源的既存事实主张")})
                        continue
                    violations.append({**row, "severity": "BLOCK", "qa_class": CLASS_TRUE_BLOCK,
                                       "classification_reason": (
                                           "命中处在主张语境（非否定/非引述/非条件），且缺少当前项目来源；"
                                           f"语义类别 {semantics}（凭据 {semantics_basis}）"),
                                       "repair_action": repair,
                                       "reason": "Plausibility is not evidence; current project support is absent."})
        covered_spans: list[tuple[int, int]] = []
        for claim_type, pattern, repair, claim_family in (
            ("UNSUPPORTED_COMPLETION_CLAIM", UNSUPPORTED_COMPLETION, "RP-FUTURE-STATE",
             "COMPLETION_OR_ABSOLUTE"),
            ("ABSOLUTE_PERFORMANCE_COMMITMENT", ABSOLUTE_PERFORMANCE, "RP-DOWNGRADE-ABSOLUTE",
             "COMPLETION_OR_ABSOLUTE"),
            ("UNSUPPORTED_EXISTING_STATE_CLAIM", EXISTING_STATE_CLAIM, "RP-VERIFY-EXISTING-STATE",
             "EXISTING_STATE"),
        ):
            for match in pattern.finditer(text):
                if claim_family == "EXISTING_STATE" and any(
                        span_start < match.end() and span_end > match.start()
                        for span_start, span_end in covered_spans):
                    # 同一截文字不重复计两条主张：完工/绝对化两支已经登记过这一截，
                    # 既存状态这支只补它们覆盖不到的那些（"…已经建立…"归完工管）。
                    continue
                covered_spans.append(match.span())
                phrase = match.group(0)
                if claim_family == "EXISTING_STATE":
                    # 既存状态这一支真正被主张的那一截是**状态谓语本身**（已配备 / 已于2023年完成），
                    # 主语只说明"这句话在说哪样东西"。modality 必须绑到谓语那一头去查前置框架：
                    # "如项目已配置…"里条件词正落在主语区内，若按整截匹配的起点查，
                    # 条件词被算进 claim 自身，显式条件句就被当成既存主张挡下。
                    # 支持判定仍吃整截（含主语）：底稿里要有"该主语+该谓语"逐字在场，
                    # 只出现过一次"已配备"不算给这条主张出示了来源。
                    claim_start, claim_end = match.span()
                else:
                    claim_start, claim_end = match.span()
                subject_span = predicate_span = object_span = None
                if claim_family == "EXISTING_STATE":
                    subject_span = match.span("subject")
                    predicate_span = match.span("state")
                    object_span = _target_object_span(text, predicate_span[1])
                elif claim_type == "UNSUPPORTED_COMPLETION_CLAIM":
                    action_hit = re.search(r"提交|具备|完成|建立|整理成册|准备就绪", phrase)
                    if action_hit:
                        predicate_span = (match.start() + action_hit.start(),
                                          match.start() + action_hit.end())
                        aspect_hit = re.search(r"(?:已经|均已|已)$", text[:predicate_span[0]])
                        object_span = (_target_object_span(text, predicate_span[1])
                                       or _completion_object_span(text, aspect_hit.start()
                                                                  if aspect_hit else predicate_span[0]))
                record = canonical_claim_record(
                    text, claim_type=claim_type, claim_family=claim_family,
                    claim_span=(claim_start, claim_end), path=path,
                    modality_probe_span=(match.start("aspect"), match.end("state"))
                    if claim_family == "EXISTING_STATE" else None,
                    subject_span=subject_span, predicate_span=predicate_span,
                    object_span=object_span, source=brief, source_phrase=phrase, context=context)
                supported = record["source_support"]
                semantic = record["modality"]
                provenance = "PROJECT_FACT" if supported else (
                    "NEGATED_DISCLAIMER" if semantic in NON_ASSERTIVE_STATUSES else "UNSUPPORTED")
                language = "FACT" if supported else (
                    "DISCLAIMER" if semantic in NON_ASSERTIVE_STATUSES else "COMMITMENT")
                semantics = record["assertion_type"]
                semantics_basis = record["provenance_semantics_basis"]
                semantics_evidence = record["provenance_semantics_evidence"]
                row = {"claim_type": claim_type, "text": phrase, "path": path, "provenance": provenance,
                       "language_level": language, "semantic_status": semantic,
                       "provenance_semantics": semantics,
                       "provenance_semantics_basis": semantics_basis,
                       "provenance_semantics_evidence": semantics_evidence,
                       "sentence": clause_of(text, claim_start, claim_end),
                       "canonical_claim_record": record}
                claims.append(row)
                if record['decision']['gate'] == 'REVIEW':
                    review_items.append({**row, 'severity': 'NEEDS_REVIEW',
                                         'classification_reason': semantics_basis,
                                         'reason': 'Process checklist completion language needs contextual review.'})
                    continue
                if supported:
                    continue
                if record["decision"]["gate"] == "SUPPRESS":
                    suppressed.append({**row, "qa_class": CLASS_FALSE_POSITIVE,
                                       "classification_reason": semantics_basis})
                    continue
                if semantic in NON_ASSERTIVE_STATUSES:
                    suppressed.append({**row, "qa_class": CLASS_FALSE_POSITIVE,
                                       "classification_reason": f"命中处于{semantic}语境（否定/仅提及/条件句），不是完工、绝对化或既存状态主张"})
                    continue
                if claim_type in {"UNSUPPORTED_COMPLETION_CLAIM",
                                  "UNSUPPORTED_EXISTING_STATE_CLAIM"} and COMPLETION_AS_MODIFIER.match(
                        text[match.end():match.end() + 18]):
                    # §40 真实件形状："…以及本节已经完成本次闭环的时间说明"—— 完成语做定语，
                    # 说的是那份载体的名字，不是"工作已经做完"。留在 suppressed 里可查。
                    suppressed.append({**row, "qa_class": CLASS_FALSE_POSITIVE,
                                       "classification_reason": "完成语在句中作定语，修饰的是记录/说明/清单这类载体名称，不构成完工主张"})
                    continue
                if claim_type == "UNSUPPORTED_EXISTING_STATE_CLAIM" and STATE_UNDER_INSPECTION_VERB.search(
                        text[max(0, match.start("aspect") - 8):match.start("aspect")]):
                    # 「项目负责人按周抽查已完成条目」：`已` 紧贴检查类谓语，定语读法优先。
                    suppressed.append({**row, "qa_class": CLASS_FALSE_POSITIVE,
                                       "classification_reason": "既存状态形前紧贴检查类谓语"
                                       "（抽查/复核/统计…），这一格是宾语内部的定语"
                                       "（「已完成条目」指已完成的那些条目），不是主张检查已完成"})
                    continue
                violations.append({**row, "severity": "BLOCK", "qa_class": CLASS_TRUE_BLOCK,
                                   "classification_reason": "命中处在主张语境（非否定/非引述/非条件），缺少当前项目证据",
                                   "repair_action": repair,
                                   "reason": "A completed or absolute outcome requires explicit current-project evidence."})
        # 现场拓扑断言：这一支不在 C 词表里，也不在完工语态里——它在替甲方描述甲方自己的现场。
        # 判据范围是**一句**：同段别处的"共用"不替本句的"同一空间"作证。
        for offset, sentence in _sentences(text):
            for match in site_premise_claims(sentence):
                token = match.group(0)
                start = offset + match.start()
                record = canonical_claim_record(
                    text, claim_type="UNSUPPORTED_SITE_PREMISE", claim_family="SITE_PREMISE",
                    claim_span=(start, start + len(token)), path=path,
                    source=brief, source_phrase=token)
                supported = record["source_support"]
                semantic = record["modality"]
                semantics = record["assertion_type"]
                semantics_basis = record["provenance_semantics_basis"]
                semantics_evidence = record["provenance_semantics_evidence"]
                row = {"claim_type": "UNSUPPORTED_SITE_PREMISE", "text": token, "path": path,
                       "provenance": "PROJECT_FACT" if supported else (
                           "NEGATED_DISCLAIMER" if semantic in NON_ASSERTIVE_STATUSES else "UNSUPPORTED"),
                       "language_level": "FACT" if supported else (
                           "DISCLAIMER" if semantic in NON_ASSERTIVE_STATUSES else "COMMITMENT"),
                       "semantic_status": semantic,
                       "provenance_semantics": semantics,
                       "provenance_semantics_basis": semantics_basis,
                       "provenance_semantics_evidence": semantics_evidence,
                       "sentence": sentence.strip(), "canonical_claim_record": record}
                claims.append(row)
                if supported:
                    continue
                if semantic in NON_ASSERTIVE_STATUSES:
                    suppressed.append({**row, "claim_path": path, "qa_class": CLASS_FALSE_POSITIVE,
                                       "classification_reason": (
                                           f"命中处于{semantic}语境（否定/仅提及/条件句），"
                                           "不是在断言现场既有构型")})
                    continue
                if semantics in NON_BLOCKING_PROVENANCE_CLASSES:
                    # MMF008A4-B：同一个空间词，写在**拟议的服务组织**里与写在
                    # "甲方现场本来这样"里不是一件事。这里不放宽挡的口径——判据仍是
                    # `classify_provenance_semantics` 那一处（同句须有服务设计字段与计划
                    # 谓语，且不得有任何处所/既存谓语），门槛与修复端读同一份结论。
                    suppressed.append({**row, "claim_path": path, "qa_class": CLASS_FALSE_POSITIVE,
                                       "provenance_semantics": semantics,
                                       "classification_reason": (
                                           f"这一句在写「{PROVENANCE_SEMANTICS_LABEL[semantics]}」"
                                           f"（凭据 {semantics_basis}），"
                                           "不是在断言当前项目现场的既有空间构型")})
                    continue
                violations.append({**row, "severity": "BLOCK", "qa_class": CLASS_TRUE_BLOCK,
                                   "classification_reason": (
                                       "这一句在断言当前项目现场的空间构型"
                                       f"（{token}＋共位谓语），且该构型在当前项目事实底稿里查无逐字来源；"
                                       f"语义类别 {semantics}（凭据 {semantics_basis}）"),
                                   "repair_action": "RP-SITE-PREMISE-VERIFY",
                                   "reason": "A site condition stated as fact needs current-project evidence; "
                                             "otherwise it must be verified on site."})
    # R05 QA 上下文更正：文档自指（“在方案中已落成表格与条目”）不是空口主张——
    # 它声称的载体就在本文档里，可以逐件定位。枚举件全部定位到**其他块** → 该主张
    # 是可自查的真话（suppress，留定位路径）；任何一件定位不到 → 照样 BLOCK。
    # 差分：删掉所称载体（表/条目）后定位失败，仍然拒绝（差分测试覆盖）。
    texts_by_path = dict(_walk_strings(generated.get("artifact", generated)))
    kept_violations: list[dict[str, Any]] = []
    for row in violations:
        sentence = str(row.get("sentence") or "")
        if row.get('claim_type') != 'UNSUPPORTED_EXISTING_STATE_CLAIM' or not SELF_REFERENCE_CLAIM.search(sentence):
            kept_violations.append(row)
            continue
        block_text = str(texts_by_path.get(row.get("path") or "", ""))
        record = row.get('canonical_claim_record') or {}
        claim_span = record.get('claim_span') or [0, 0]
        reference = next((m for m in SELF_REFERENCE_CLAIM.finditer(block_text)
                          if claim_span[0] <= m.start() and m.end() <= claim_span[1]), None)
        if reference is None:
            kept_violations.append(row)
            continue
        before_claim = block_text[:reference.start()]
        items = _self_reference_items(before_claim)
        located = []
        if items:
            for item in items:
                hit = _locate_item(item, texts_by_path, row.get("path") or "", claim_contexts)
                if hit:
                    located.append({"item": item, "path": hit})
        if items and len(located) == len(items):
            # Update the same decision object used by claims, repair and final QA.
            record['document_carrier_support'] = located
            record['source_support'] = True
            record['source_support_span'] = {'kind': 'DOCUMENT_CARRIERS', 'bindings': located}
            record['decision'] = {**record.get('decision', {}), 'gate': 'SUPPRESS', 'repair': 'KEEP_SOURCE_SUPPORTED_FACT'}
            suppressed.append({**row, "qa_class": CLASS_FALSE_POSITIVE,
                               "classification_reason": (
                                   "文档自指已逐件定位（"
                                   + "；".join(f"{l['item']}→{l['path']}" for l in located[:6])
                                   + "），所称载体真实存在，不构成无来源的既存事实主张")})
            continue
        missing_items = sorted(set(items) - {l["item"] for l in located}) if items else []
        extra_reason = ("；文档自指未能在本文档内逐件定位所称载体"
                        + (f"（缺 {'、'.join(missing_items)}）" if missing_items else ""))
        kept_violations.append({**row,
                                 "classification_reason": (
                                     str(row.get("classification_reason") or "") + extra_reason)})
    violations = kept_violations
    unique = []
    seen = set()
    for row in violations:
        key = (row["path"], row["claim_type"], row["text"])
        if key not in seen:
            seen.add(key); unique.append(row)
    review_unique = []
    for row in review_items:
        key = (row["path"], row["claim_type"], row["text"])
        if key in seen:
            continue
        seen.add(key); review_unique.append(row)
    # suppressed 是已经自证不是缺陷的行（§32），只计数、不再把整个门槛拉到 NEEDS_REVIEW。
    status = ("BLOCK" if unique else ("NEEDS_REVIEW" if review_unique else "PASS"))
    semantics_counts: dict[str, int] = {name: 0 for name in PROVENANCE_SEMANTIC_CLASSES}
    for claim in claims:
        name = str(claim.get("provenance_semantics") or "")
        semantics_counts[name] = semantics_counts.get(name, 0) + 1
    return {"schema_version": "commitment-provenance-gate-v0.3", "status": status,
            "claims": claims, "violations": unique, "review_items": review_unique,
            "suppressed": suppressed,
            "negation_aware": True,
            "suppressed_count": len(suppressed),
            # MMF008A-B：五类语义的分布随门槛一起落盘。四类非阻断类别各自要求正面语境
            # 凭据；只有 `HARD_BLOCK_PROVENANCE_CLASS` 一类进 violations。
            "provenance_semantics_version": PROVENANCE_SEMANTICS_VERSION,
            "provenance_semantics_classes": list(PROVENANCE_SEMANTIC_CLASSES),
            "provenance_semantics_counts": semantics_counts,
            "hard_block_provenance_class": HARD_BLOCK_PROVENANCE_CLASS,
            "delivery_blocking_only_for_class": HARD_BLOCK_PROVENANCE_CLASS,
            "repair_actions": list(dict.fromkeys(x["repair_action"] for x in unique))}


# 这些词本身是**中性的技术/评分口径词**，只在同一句真的命中对应承诺类别时才允许被改写。
# 无条件擦词会把"明确 30 分钟到场时限"这类正确的技术承诺改成"到场安排"，
# 既丢掉评分点又批量生产空话（§45–§47）；门与修必须同口径。
SCOPED_COMMITMENT_TERMS = {
    "响应时限": "C1", "到场时限": "C1",
    "日清日洁": "C1", "每天清扫": "C1", "每日保洁": "C1",
    "固定岗": "C2", "专人持续值守": "C2",
    "封闭式管理": "C4", "门禁卡": "C4",
    "24小时值守": "C8", "全天候监控": "C8",
    "统一受理": "C9",
}


COMPLETION_ASSERTION = re.compile(r"(已经|均已|已)(提交|具备|完成|建立|整理成册|准备就绪)")
# §40：允许的承诺语态只有一套 —— 拟 / 将 / 计划 / 建议 / 中标后按合同确认 / 以招标人最终要求为准。
FUTURE_STATE_REGISTER = {
    "提交": "将在中标后按合同约定提交",
    "具备": "将按合同要求逐项核对确认",
    "完成": "将按计划完成，具体节点以合同与现场条件为准",
    "建立": "将按合同约定建立",
    "整理成册": "将在中标后按招标人最终要求整理成册",
    "准备就绪": "将按合同约定准备就绪",
}


# §40：资信材料类的完工主张由 Bidder Evidence 交付边界负责（"另行提供"），比未来态
# 改写更准确 —— 承诺"中标后整理成册"仍等于把资信页留在技术标里。让位，不抢改写。
CREDENTIAL_MATERIAL = re.compile(r"资历|证明材料|资质证书|证书|业绩|履历|获奖|荣誉")

# ---------------------------------------------------------------------------
# MMF008A6-B2：无来源的既存事实**不得**变成无来源的新义务（两族修复共用的一条安全契约）
# ---------------------------------------------------------------------------
# 被废止的策略：既存状态主张缺少当前项目来源时，把体貌标记换成一条固定语态（旧代码里那个
# `RP_EXISTING_STATE_REGISTER`），动词与宾语原样留着——于是"某场所已经装了某设备"这句无来源的
# 事实，读起来变成了"我方承诺去装这台设备"。Codex A5 Gate 判这条策略不成立：招标与合同里
# 并没有这一条义务，"按合同要求"那四个字不是证据，它只是把一个假的事实主张换成了一句新承诺。
# 同一族里完工主张的 `FUTURE_STATE_REGISTER` 在做同一件事，而且它**不看底稿**，
# 所以那条风险就长在默认生产链上。两族因此必须共用下面这一条契约，只有一个决策点：
#
#   ① 底稿逐字支持的既存事实 → 原样保留（门槛那一头已判 EXISTING_FACT，修复不许比门更狠）；
#   ② 底稿里逐字存在"这一谓语 + 这一对象"的**现行义务** → 才可写成将来承诺，
#      并把那一句登记为可复核凭据；
#   ③ 其余（无来源的既存事实）→ 只做**中和**：体貌标记前挂"是否"，若这一截小句到句末为止
#      再在句末补一条现状核验边界。既不新增义务，也不删掉那句话本来要谈的东西。
#
# 拿不到底稿时两件事同时成立：②这一支没有证据可言（关），而③也不能开——
# 没有底稿就分不出"招标已写明的既有设施"与"替甲方想象的既有设施"，此时连"是否"这种
# 待定写法都会把**本来有据**的事实改成疑问（Run F 那 553 段实测过这类擦词的分量）。
# 所以第四条分支是**不动文本**：把这条主张原样留给门槛。门槛那一头是拿到底稿的
# （`final_artifact_qa` 用 brief/canonical_facts 判），于是它照样 BLOCK、照样进
# `final_artifact_qa.json` 的终态权威——不动文本不等于放过，只是不再用一句新承诺去遮它。
UNSUPPORTED_FACT_DISPOSITION_VERSION = "unsupported-fact-disposition-v0.1"
DISPOSITION_KEEP_FACT = "KEEP_SOURCE_SUPPORTED_FACT"
DISPOSITION_BOUND_OBLIGATION = "REWRITE_TO_SOURCE_BOUND_OBLIGATION"
DISPOSITION_BOUNDARY = "NEUTRALIZE_TO_VERIFICATION_BOUNDARY"
DISPOSITION_HOLD_WITHOUT_EVIDENCE = "HOLD_BLOCK_WITHOUT_SOURCE_EVIDENCE"
# Source obligation decisions live in _bind_obligation_source. This legacy
# helper is only a projection of the canonical binding result.
VERIFICATION_BOUNDARY_MARK = "是否"
VERIFICATION_BOUNDARY_TAIL = "，以合同约定及现场确认结果为准"


def source_obligation_evidence(compact_brief: Any, predicate: str, keys: list[str]) -> str:
    for key in sorted((k for k in keys if k), key=len, reverse=True):
        binding = _bind_obligation_source(compact_brief, predicate, key)
        if binding["status"] == "VERIFIED":
            return binding["obligation_source"]
    return ""


def unsupported_fact_disposition(*, compact_brief: Any, supported: bool, predicate: str,
                                 keys: list[str]) -> dict[str, Any]:
    """§B2 契约的唯一决策点：这一截既存事实主张允许被改成什么（两族修复都只从这一处取）。

    四条分支互斥且穷尽：有来源的既存事实保留；没有底稿就没有证据可言，谁也不许动文本；
    底稿里有对应现行义务才可写将来承诺；三头都不沾的才做现状核验边界。
    **"按合同要求"这类措辞不在任何分支里**——它既不是证据，也不是承诺的凭据。
    """
    empty = {"status": "UNBOUND", "obligation_source": "", "source_span": None}
    base = {"schema_version": UNSUPPORTED_FACT_DISPOSITION_VERSION,
            "obligation_source": "", "obligation_binding": empty}
    if supported:
        return {**base, "action": DISPOSITION_KEEP_FACT}
    if not compact_brief:
        return {**base, "action": DISPOSITION_HOLD_WITHOUT_EVIDENCE}
    for key in sorted((k for k in keys if k), key=len, reverse=True):
        binding = _bind_obligation_source(compact_brief, predicate, key)
        if binding["status"] == "VERIFIED":
            return {**base, "action": DISPOSITION_BOUND_OBLIGATION,
                    "obligation_source": binding["obligation_source"],
                    "obligation_binding": binding}
    return {**base, "action": DISPOSITION_BOUNDARY}


def _disposition_edits(text: str, aspect_start: int, aspect_end: int,
                       aspect: str, bound_replacement: str, *, record: dict[str, Any], still_asserted,
                       bound_span: tuple[int, int] | None = None) -> list[tuple[int, int, str]]:
    """两族修复共同的落点：按契约给出这一次要落的 edits `(起, 止, 替换串)`。

    保留与不动这两档给空 edits（文本一个字不改）；将来承诺只换体貌标记；边界那一档才是
    "在断言那一头挂 是否 + 在句末补一条核验边界"。把两件事放在同一次扫描里算好，交给
    `_apply_fact_edits` 从右往左落——先落右边的编辑再算左边的位置，索引就不会漂；
    分两遍扫描全文则是第二把尺。

    **边界这一档还要自证有效**：改完之后用门槛那一族的同一把尺再读一遍
    （`still_asserted(candidate, lo, hi)`），只要那一头还读得出直陈，就**不动文本**。
    完工主张那一族的门槛正则比修复端用的更宽（还会从"所有…"起算），只按修复端自己的
    命中位置挂 是否，就会改出一句"看着已处理、其实照样挡"的话——那正是 §4 的 HARD FAIL 形状，
    所以宁可留给门槛继续挡，也不生产这种半处理文本。
    """
    action = record["decision"]["repair"]
    if action in (DISPOSITION_KEEP_FACT, DISPOSITION_HOLD_WITHOUT_EVIDENCE, "KEEP_NON_ASSERTIVE"):
        return []
    if action == DISPOSITION_BOUND_OBLIGATION:
        # 承诺语态落在哪一格由调用端给：完工那一族的寄存器串里已经带着动词，要连动词一起换；
        # 既存状态那一族只换体貌标记（"已配备"→"将配备"），动词与宾语一个字都不动。
        lo, hi = bound_span or (aspect_start, aspect_end)
        return [(lo, hi, bound_replacement)]
    if action != DISPOSITION_BOUNDARY:
        return []
    edits = [(aspect_start, aspect_end, f"{VERIFICATION_BOUNDARY_MARK}{aspect}")]
    tail_at = sentence_tail_boundary(text, aspect_end)
    if tail_at is not None:
        edits.append((tail_at, tail_at, VERIFICATION_BOUNDARY_TAIL))
    candidate = _apply_fact_edits(text, edits)
    if still_asserted(candidate, aspect_start, aspect_end):
        return []             # 中和没落到断言那一头上：不动文本，继续由门槛挡
    return edits


def _apply_fact_edits(text: str, edits: list[tuple[int, int, str]]) -> str:
    """从右往左落 edits；同一位置的同一插入（一句里两条命中）只落一次。"""
    for start, end, replacement in sorted(set(edits), key=lambda item: item[0], reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _unsupported_fact_anchors(candidate: str, lo: int, hi: int) -> list[tuple[int, int]]:
    """改后文本里**所有**可能仍把这一截谓语读作直陈的查询点（两族门槛正则一起问）。

    为什么两支都要问：同一截"已X"在门槛那里可能同时落进完工主张与既存状态两支，而
    两支查 modality 的那一头不同（完工按整截命中起点、既存状态按体貌标记）。只按自己
    那一支复查，就会出现"这一支改绿了、那一支还挡着"的半处理文本。
    重叠判据按插入造成的位移给了 `len(是否)` 的容错。
    """
    pad = len(VERIFICATION_BOUNDARY_MARK)
    anchors = [(probe.start(), probe.end()) for probe in UNSUPPORTED_COMPLETION.finditer(candidate)
               if probe.end() > lo and probe.start() < hi + pad]
    anchors += [(probe.start("aspect"), probe.end("state"))
                for probe in EXISTING_STATE_CLAIM.finditer(candidate)
                if probe.end("aspect") > lo and probe.start("aspect") < hi + pad]
    return anchors


def _unsupported_fact_still_asserted(candidate: str, lo: int, hi: int) -> bool:
    """中和之后，门槛那两支尺在这一截谓语的**每一个**查询点上是不是还读得出直陈。"""
    return any(canonical_claim_record(
        candidate, claim_type="UNSUPPORTED_EXISTING_STATE_CLAIM",
        claim_family="EXISTING_STATE", claim_span=(start, end))["modality"] == "ASSERTED"
               for start, end in _unsupported_fact_anchors(candidate, lo, hi))


def _repair_completion_claims(text: str, compact_brief: str = "", source: Any = None) -> str:
    """RP-FUTURE-STATE 的执行端。§B2 之后它**不再无条件**把完工主张改写成将来承诺。

    门槛判 BLOCK 的完工主张要有可执行的处置，否则就是"门严修宽"（Round 3 的
    RP-FUTURE-STATE 只有名字、没有实现）；但改成的**形状**由那条安全契约决定：
    ① 有底稿逐字支持 → 保留；② 底稿里有对应现行义务 → 才写将来承诺；
    ③ 拿不到底稿、或中和落不到断言那一头上（这一族的断言查在整截短语开头，
    不在体貌标记上）→ **不动文本**，让门槛继续挡。
    作定语、被否定、条件句、资信材料这四格仍原样保留。
    """
    edits: list[tuple[int, int, str]] = []
    for match in COMPLETION_ASSERTION.finditer(text):
        action = match.group(2)
        record = canonical_claim_record(
            text, claim_type="UNSUPPORTED_COMPLETION_CLAIM",
            claim_family="COMPLETION_OR_ABSOLUTE", claim_span=match.span(),
            predicate_span=match.span(2),
            object_span=(_target_object_span(text, match.end(2))
                         or _completion_object_span(text, match.start(1))),
            source=source if source is not None else compact_brief,
            source_phrase=match.group(0))
        if record["modality"] != "ASSERTED":
            continue
        tail = text[match.end():match.end() + 18]
        if COMPLETION_AS_MODIFIER.match(tail) or DESCRIPTIVE_USE.match(tail):
            continue
        clause = text[max(0, match.start() - 40):match.end() + 24]
        if CREDENTIAL_MATERIAL.search(clause):
            continue
        edits += _disposition_edits(
            text, match.start(1), match.end(1), match.group(1),
            FUTURE_STATE_REGISTER.get(action, f"将{action}"), record=record,
            # 这一族自证有效用的就是门槛那条更宽的正则：改完仍读得出直陈就不动文本。
            still_asserted=_unsupported_fact_still_asserted,
            # 寄存器串里已经含动词（"将按合同约定建立"），所以有据承诺那一档要连动词一起换，
            # 不然会写出"将按合同约定建立建立"这种叠字。
            bound_span=(match.start(1), match.end(2)))
    return _apply_fact_edits(text, edits) if edits else text


def asserted_claim_classes(text: str) -> set[str]:
    """本句里**被主张**的承诺类别（否定/引述/条件句里的命中不算），供修复端同口径取用。"""
    text = str(text or "")
    hits: set[str] = set()
    for claim_type, patterns in SEMANTIC_PATTERNS.items():
        if claim_type in hits:
            continue
        for pattern in patterns:
            matched = False
            for found in re.finditer(pattern, text):
                if canonical_claim_record(
                    text, claim_type=claim_type, claim_family="SEMANTIC_PATTERN",
                    claim_span=found.span())["modality"] == "ASSERTED":
                    hits.add(claim_type)
                    matched = True
                    break
            if matched:
                break
    return hits


def _repair_site_premises(text: str, compact_brief: str = "", source: Any = None) -> str:
    """RP-SITE-PREMISE-VERIFY 的执行端：去掉**未经核验**的现场构型限定，保留那句真正要说的安排。

    三条约束：
    ① 门槛与修复同一条尺（`SITE_PREMISE_REPAIR` 覆盖 `SITE_PREMISE_SPATIAL` 的引导介词与
       方位后缀），否则就是"报得出修不回"；
    ② **只有底稿里查无此构型时才动**——拿不到底稿（compact_brief 为空）就一律不动。
       无底稿的全文擦词会把招标已给的"单一院落"一起抹掉，那是删业务事实；
    ③ 删除只吃限定短语本身："四类作业在同一空间内共用出入口" → "四类作业共用出入口"，
       要给甲方的安排一个字不少。
    """
    if not compact_brief or not SITE_PREMISE_COLOCATION.search(text or ""):
        return text
    removed: list[str] = []

    def drop(match: re.Match[str]) -> str:
        token = SITE_PREMISE_SPATIAL.search(match.group(0))
        if token is not None:
            claim_start = match.start() + token.start()
            record = canonical_claim_record(
                text, claim_type="UNSUPPORTED_SITE_PREMISE", claim_family="SITE_PREMISE",
                claim_span=(claim_start, claim_start + len(token.group(0))),
                source=source if source is not None else compact_brief,
                source_phrase=token.group(0))
            if record["decision"]["gate"] == "SUPPRESS":
                return match.group(0)
        removed.append(match.group(0))
        return ""

    repaired = SITE_PREMISE_REPAIR.sub(drop, text)
    if not removed:
        return text
    repaired = re.sub(r"[，、]{2,}", "，", repaired)
    repaired = re.sub(r"^[，、；]", "", repaired)
    # 摘掉限定后若留下"，。"这类双标点，保留句末那一个。写 `r"\1\2"` 而模式只有一个组，
    # `re.sub` 会当场抛 invalid group reference —— 门槛拦得越多，这条崩得越早。
    repaired = re.sub(r"[，、]([；。])", r"\1", repaired)
    return repaired


# MMF008A6-B2：这一格原来是 `RP_EXISTING_STATE_REGISTER = "将按合同要求"`，与完工主张那族
# 一起被废止（见上面那条契约）。留着它等于留着那条 bug：拼接处不核义务来源。
# RP_EXISTING_STATE_REGISTER 已删除，两族都只从 `_disposition_edits` 取处置。


def _repair_existing_state_claims(text: str, compact_brief: str = "", source: Any = None) -> str:
    """RP-VERIFY-EXISTING-STATE 的执行端：与完工主张那族**共用同一条**安全契约。

    "某样东西现在是什么状态"这类话真正被主张的那一截是**体貌标记**（已 / 已于某年），
    主语只说明这句话在谈哪样东西，宾语与其余小句才是业务内容。所以处置只动体貌标记那一格：
    ① 与门槛同一条 claim span（modality 在谓语这一头查，修复端按另一头查就是两把尺）；
    ② 只动 `semantic_status` 判为**主张**的命中，否定/引述/条件句原样保留；
    ③ 完成语做定语、短语是制度对象、紧贴检查类谓语这三格仍不动；
    ④ 其余全部交给 `_disposition_edits`：底稿逐字支持的保留，没有底稿的不动（原样留给门槛，
       门槛那一头拿得到底稿），有对应现行义务的才写将来承诺，最后一档才是现状核验边界。
    动词与其后的宾语一个字都不动——要说的安排还在，丢掉的只是那句未经核验的既存断言，
    而新增的承诺只在底稿里真有那条义务时才出现。
    """
    edits: list[tuple[int, int, str]] = []
    for match in EXISTING_STATE_CLAIM.finditer(text):
        # 与门槛同一条 claim span：modality 在**状态谓语**那一头查（见 family 循环里的注释），
        # 修复端按另一头查就会"门放行、修却动"或"门挡下、修不动"。
        record = canonical_claim_record(
            text, claim_type="UNSUPPORTED_EXISTING_STATE_CLAIM", claim_family="EXISTING_STATE",
            claim_span=match.span(),
            modality_probe_span=(match.start("aspect"), match.end("state")),
            subject_span=match.span("subject"), predicate_span=match.span("state"),
            object_span=_target_object_span(text, match.end("state")),
            source=source if source is not None else compact_brief,
            source_phrase=match.group(0))
        if record["modality"] != "ASSERTED":
            continue
        tail = text[match.end():match.end() + 18]
        if COMPLETION_AS_MODIFIER.match(tail) or DESCRIPTIVE_USE.match(tail):
            continue
        if STATE_UNDER_INSPECTION_VERB.search(
                text[max(0, match.start("aspect") - 8):match.start("aspect")]):
            # 与门槛同一条定语判据：门槛不挡的那一格，修复也不许动（否则"抽查已完成条目"
            # 会被改写成一句谁都不认识的话）。
            continue
        edits += _disposition_edits(
            text, match.start("aspect"), match.end("aspect"), match.group("aspect"), "将",
            record=record, still_asserted=_unsupported_fact_still_asserted)
    return _apply_fact_edits(text, edits) if edits else text


def _repair_text(text: str, compact_brief: str = "", source: Any = None) -> str:
    replacements = {
        "采用外包模式": "如经确认使用外委方式，可按合同要求设置供方管理机制",
        "采用外委": "如经确认使用外委方式",
        "外委作业": "如经确认后的外委作业",
        "由专业供方承担日常作业": "如经确认使用外委方式，可由专业供方按合同承担相应作业",
        "实行封闭管理": "可结合园区开放条件评估分区管理方式",
        "封闭式管理": "分区管理",
        "外来人员禁止进入": "可按经确认的访客权限实施分区管控",
        "项目经理批示": "按当前项目确认的审批权限办理",
        "费用与结算挂钩": "履约结果可按当前合同约定用于管理评价",
        "考核与付款挂钩": "履约结果可按当前合同约定用于管理评价",
        "日清日洁": "按项目确认的保洁频次组织作业",
        "每天清扫": "按项目确认的保洁频次组织作业",
        "每日保洁": "按项目确认的保洁频次组织作业",
        "24小时值守": "可根据风险等级确定值守时段",
        "全天候监控": "可根据风险等级确定监控时段",
        "固定岗": "值守责任",
        "专人持续值守": "明确值守责任",
        "统一受理": "按确认路径受理",
        "响应时限": "响应安排",
        "到场时限": "到场安排",
    }
    from governance.text_sanitize import apply_replacements_idempotent, collapse_repeated_conditionals
    active_classes = asserted_claim_classes(text)
    scoped = {
        term: replacement for term, replacement in replacements.items()
        if term in SCOPED_COMMITMENT_TERMS and SCOPED_COMMITMENT_TERMS[term] in active_classes
    }
    plain = {term: replacement for term, replacement in replacements.items() if term not in SCOPED_COMMITMENT_TERMS}
    text = apply_replacements_idempotent(text, plain)
    if scoped:
        text = apply_replacements_idempotent(text, scoped)
    text = re.sub(r"确保([^。；\n]{0,40})零故障运行", r"保障\1稳定运行并降低故障发生概率", text)
    # 与门槛口径对齐：门槛拦截 零故障/100%无故障/绝不发生/完全杜绝/永久确保，
    # 修复必须覆盖同样的变体，避免"门严修宽"导致不可修复的 BLOCK。
    text = re.sub(r"确保([^。；\n]{0,40})零故障", r"保障\1稳定可靠", text)
    text = re.sub(r"100%无故障", "显著降低故障风险", text)
    text = re.sub(r"绝不发生", "最大限度避免", text)
    text = re.sub(r"完全杜绝", "有效防范", text)
    text = re.sub(r"永久确保", "长期保障", text)
    # RP-FUTURE-STATE：完工主张的处置与门槛同口径，且**与既存状态那族共用同一条安全契约**
    # （§B2：拿到底稿才可能有承诺，拿不到底稿不动文本，无依据的事实一律不换成新义务）。
    text = _repair_completion_claims(text, compact_brief, source)
    # RP-SITE-PREMISE-VERIFY：未经核验的现场构型限定，与门槛同口径（拿到底稿才动）。
    text = _repair_site_premises(text, compact_brief, source)
    # RP-VERIFY-EXISTING-STATE：未经核验的既存状态主张，与门槛同口径（拿到底稿才动）。
    text = _repair_existing_state_claims(text, compact_brief, source)
    return collapse_repeated_conditionals(text)


def apply_local_repairs(generated: dict[str, Any],
                        brief: dict[str, Any] | str | None = None) -> dict[str, Any]:
    """局部改写。`brief` 在场时现场拓扑断言才有得修：没有底稿就分不出
    "招标已给的单一院落"和"替甲方想象的同一空间"，此时宁可把 BLOCK 留给有界重生成。"""
    compact_brief = re.sub(r"\s+", "", brief if isinstance(brief, str)
                           else json.dumps(brief or {}, ensure_ascii=False)) if brief else ""
    repaired = copy.deepcopy(generated)

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            from .customer_hygiene import is_structural_path
            from .table_context import visit_table
            contextual = visit_table(value, walk, is_structural_path)
            if contextual is not None:
                return contextual
            return {k: (v if is_structural_path(str(k)) else walk(v)) for k, v in value.items()}
        if isinstance(value, list): return [walk(v) for v in value]
        if isinstance(value, str): return _repair_text(value, compact_brief, brief)
        return value
    return walk(repaired)
