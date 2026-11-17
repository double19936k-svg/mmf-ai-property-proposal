"""MMF008A-C：把"内部推理"写成客户读得到的招标文书语言。

Run F 最终 Word 里进了这一类句子（逐条摘自 `runF_snapshot_final/artifact_content.json`）：

    本章先把"可以写进正文的数"与只能写成条件的做法分开。
    三者分列之后，正文中出现的每一个数字都有出处，每一条做法都有前提
    据此本节把内容分三层并给出各自处置规则：已确认事实层…
    排除项与边界登记：…包括报价表与价格构成…授权委托书等格式文件、商务偏离表
    因该部分属技术标正文排除的证明材料范围…本章不列具体项目名称…也不改写为业绩描述页或履历表
    对无法以现场服务动作响应的内容，本章按技术标范围处理为不展开：企业资质…

它们不是在描述服务，而是在解释**这份文档怎么写、为什么没写某样东西**。诱因在提示词契约本身：
"商务、报价、资格、资信…一律不得展开，不得为其写说明段"逐条点名列内分类，模型就转手写一篇
"本章如何处理这些分类"的元叙述。所以这一模块做两件事：
① 把这些句式改写成自然的招标文书语言（有业务含义的**换说法**，没有业务含义的**不出现**）；
② 绝不连带删掉真正的风控内容——服务边界、责任界面、待澄清事项、前置条件、甲方确认机制，
   以及 `【待投标人确认：所需证明】` 这条 Bidder Evidence 门唯一可定位的行内锚点。

两条硬约束，都来自上一版实测踩到的坑：

**KEEP 先于 SHAPE。** 判"要不要动这句话"先看它有没有承载风控内容，有才原样保留，没有再按
形状处置。反过来做就会把风控条款一起洗掉，那是制造假干净。

**处置只作用于"整句"或"整子句"，绝不抠句子中间的一段。** 上一版把替换串拼进命中片段的位置，
产出过 `三者本方案所报数据均以现场核实结果…写明。，每一条做法都有前提` 这种半截残句；配合
"覆盖≥50% 或剩余≤24字"的主导性阈值，Run F 真实语料里 14 处命中只有 1 处真的被处理。
现在每条形状给三样东西：`sentence`（整句形状，fullmatch 才动手）、`strips`（只切**整子句**，
在 。；，、边界上收口，保留有业务内容的那半截）、`detector`（片段级探测，够不上前两者就
**只报不改**）。少一句"看起来干净"，多一条可指认的账。
"""
from __future__ import annotations

import re
from typing import Any

CLIENT_FACING_PROSE_VERSION = "client-facing-prose-v0.2"
CLIENT_FACING_PROSE_RULE_ID = "CLIENT_ARTIFACT_INTERNAL_NARRATION"
CLIENT_FACING_PROSE_REPAIR_ACTION = "NATURALIZE_INTERNAL_NARRATION"

# 行内确认锚点：`bidder_evidence_gate.confirmation_hints` 靠它在产物里定位，
# 净化器永远不得删改含锚点的句子。
CONFIRMATION_ANCHOR = re.compile(r"【[^】]{0,28}待[^】]{0,16}确认[^】]{0,28}】")
# 真正有业务含义的风控内容：出现即**整句保留**。刻意不收"前提""为准"这类裸词——
# Run F 那几句元叙述里正好都带这两个词，收了就等于让 KEEP 抢在改写前面放行。
# "只传递事实/以甲方授权/成立前提/替代做法/甲方配合事项"是 Run F 里**真实的服务规则**
# （前台与经办岗位怎么说、措施要满足什么条件），不是文档写法口径，所以列进保留侧。
RISK_CONTROL_KEEP = re.compile(
    r"服务边界|责任界面|待澄清|待确认|待填写|待填项|前置条件|另行确认|澄清前|澄清结果|"
    # 甲方确认机制（待甲方书面确认…）与"不作承诺"经常同句出现：那一截写的是**谁的批准
    # 才算数**，是责任界面，不能因为句尾带"不作为服务依据"就被换成一句澄清口径（MMF008A4-C）。
    r"(?:需|须经|需要|待|由|请)(?:经)?(?:甲方|招标人|监理|使用方|发包方|业主)"
    r"(?:方)?(?:的)?(?:书面|口头)?(?:确认|批准|明确|签批|答复|同意|审批)"
    r"|甲方(?:归口|经办)|以合同(?:约定)?为准|"
    r"控制上限|计算基数|不影响(?:服务|使用)|停机(?:窗口|安排)|应急(?:响应|处置)流程|"
    r"只传递事实|以甲方授权|甲方配合事项|成立前提|替代做法")

# 句子切分：换行**不是**边界（DOCX 里一个句子常被排版成多行），只在句末标点之后切。
_SENTENCE_SPLIT = re.compile(r"(?<=[。；;！!？?])")
# 整形状比对前先收掉首尾空白与尾部标点：形状写在"句子本体"上，不跟标点较劲。
_EDGE_PUNCT = "。；;，,、：:!！?？ \n\t\u3000"
_SENTENCE_END = "。；;！!？?"


def _core(sentence: str) -> str:
    return sentence.strip().strip(_EDGE_PUNCT).strip()


# 形状表：`detector` 是片段级探测（只报），`sentence` 是整句形状（动手改），
# `strips` 是整子句收口（保留有业务内容的半截）。排列就是优先级，第一条命中即处理。
_INTERNAL_NARRATION_SHAPES: tuple[dict[str, Any], ...] = (
    {
        "shape_id": "APPLICABILITY_LIMITS_HEADER",
        "detector": re.compile(r"允许表述与禁止表述"),
        "sentence": re.compile(r"允许表述与禁止表述"),
        "action": "REWRITE",
        "replacement": "适用范围与实施限制",
    },
    {
        # A conditional branch must retain its applicability when authoring
        # instructions are projected into customer prose.
        "shape_id": "CONDITIONAL_APPLICABILITY_INSTRUCTION",
        "detector": re.compile(r"以条件性方法表述[^。；]{0,50}条件成立时"),
        "sentence": re.compile(
            r"以条件性方法表述[，,]条件成立时适用的部分(?:单独成节|分列)"
            r"而不写成既定(?:事实|安排|承诺)?"),
        "action": "REWRITE",
        "replacement": "相关措施仅在适用条件经确认成立后实施。",
    },
    {
        # "本章先把可以写进正文的数与只能写成条件的做法分开" —— 讲的是写法，不是服务。
        "shape_id": "BODY_WRITABILITY_TUTORIAL",
        "detector": re.compile(
            r"(?:本|该)(?:章|节|部分)[^。；]{0,28}?(?:可以写进正文|写进正文|只能写成条件|"
            r"只能写成(?:条件|安排))"),
        "sentence": re.compile(
            r"[^。；]{0,16}(?:本|该)(?:章|节|部分)[^。；]{0,28}?(?:可以写进正文|写进正文|"
            r"只能写成条件|只能写成安排)[^。；]{0,40}?(?:分开|区分|分列|拆列)[^。；]*"),
        "action": "DROP",
        "replacement": "",
    },
    {
        # "三者分列之后，正文中出现的每一个数字都有出处，每一条做法都有前提…" —— 换说法。
        "shape_id": "EVERY_FIGURE_HAS_A_SOURCE",
        "detector": re.compile(
            r"正文中出现的每[^。；]{0,12}?(?:数字|数据|做法)[^。；]{0,16}?(?:出处|前提|依据)"),
        "sentence": re.compile(
            r"(?:[^。；]{0,14}(?:分列|分开)之后)?[^。；]*正文中出现的每[^。；]*?"
            r"(?:出处|前提|追溯)[^。；]*"),
        "action": "REWRITE",
        "replacement": ("本方案所报数据均以现场核实结果与招标文件给定口径为准，"
                        "各项作业的执行条件在相应内容中一并写明。"),
    },
    {
        # "据此本节把内容分三层并给出各自处置规则：已确认事实层…" —— 内部分层口径。
        "shape_id": "LAYERING_RULE_ANNOUNCEMENT",
        "detector": re.compile(
            r"(?:本|该)(?:节|章|部分)[^。；]{0,14}?(?:把内容)?分(?:为)?[二三三四五]?\s?(?:层|类)"
            r"[^。；]{0,26}?(?:处置规则|处理规则|规则)"),
        "sentence": re.compile(
            r"(?:据此)?[^。；]{0,10}(?:本|该)(?:节|章|部分)[^。；]*?"
            r"(?:处置规则|处理规则|直接引用不再讨论)[^。；]*"),
        "action": "DROP",
        "replacement": "",
    },
    {
        # "排除项与边界登记：本章只界定技术服务范围与规模事实，商务、资格…不在技术正文展开，
        #  包括报价表…商务偏离表…" —— 点名招标文书种类，既是内部口径也是模板泄漏源。
        "shape_id": "EXCLUSION_REGISTER",
        "detector": re.compile(
            r"排除项与边界登记[：:]|只界定[^。；]{0,24}?(?:服务范围|规模事实)"),
        "sentence": re.compile(
            r"(?:排除项与边界登记[：:])[^。；]*(?:不展开|不在|包括|以及)[^。；]*|"
            r"[^。；]{0,16}?只界定[^。；]{0,24}?(?:服务范围|规模事实)[^。；]*"),
        "action": "REWRITE",
        "replacement": "服务内容以本项目招标文件与物业服务合同给定的技术服务范围为限。",
    },
    {
        # "因该部分属技术标正文排除的证明材料范围…本章不列具体项目名称…也不改写为业绩描述页"
        # —— 解释为什么没写，还带出"上下文包/可核验的证据条目"这类开发口径。
        "shape_id": "NOT_LISTED_WITH_REASON",
        "detector": re.compile(
            r"(?:本|该)(?:章|节|部分)[^。；]{0,20}?不列[^。；]{0,48}?"
            r"(?:也不|或|以及)[^。；]{0,24}?(?:业绩描述|履历表|附件页|证明页)|上下文包"),
        "sentence": re.compile(
            r"[^。；]*(?:本章不列[^。；]*(?:业绩描述|履历表)|"
            r"属技术标正文排除的证明材料范围|上下文包)[^。；]*"),
        "action": "DROP",
        "replacement": "",
    },
    {
        # "对无法以现场服务动作响应的内容，本章按技术标范围处理为不展开：…不进入技术正文，
        #  也不为其生成说明段或空白附件页" —— 整句都在讲分类归属，没有服务动作。
        "shape_id": "CATEGORY_BELONGING_EXPLAINED",
        "detector": re.compile(
            r"(?:商务|报价|资格|资信|法务|投标程序)[^。；]{0,14}?"
            r"(?:条款|材料|事项|内容|文件|性质)[^。；]{0,16}?"
            r"(?:不(?:进入|列入|在|宜在|应在)|归(?:入|于))[^。；]{0,16}?(?:技术)?正文[^。；]{0,16}?"
            r"(?:展开|说明|登记|列明)?|(?:属于|归为)[^。；]{0,10}?(?:商务|资格|资信)(?:文件|范围)"),
        "sentence": re.compile(
            r"[^。；]*(?:按技术标范围处理为不展开|按范围政策只登记不展开|"
            r"不(?:进入|列入)技术(?:标)?正文[^。；]*(?:说明段|空白附|复印件页)|"
            r"不(?:进入|列入)技术(?:标)?正文[^。；]*(?:也不|或)[^。；]*"
            r"(?:说明段|清单|空白附|复印件页))[^。；]*"),
        "strips_first": True,
        "strips": (
            # "上述凭证按招标文件要求随资格与商务文件提交，不进入技术正文，也不在本章扩写为…"
            # 前半截是真的交付边界（凭证随哪类文件走），只切掉后面的写法说明。
            {"kind": "TAIL",
             "pattern": re.compile(
                 r"(?P<keep>[^。；，]{6,}?)[，、](?P<narr>不(?:进入|列入)技术(?:标)?正文"
                 r"[^。；]*(?:也不[^。；]*)?)[^。；]*")},
        ),
        "action": "DROP",
        "replacement": "",
    },
    {
        # "涉及合同价款、费用结算与商务条款的内容不在技术报告正文中展开，本项目仅提供现场作业量
        #  等数据支撑，台账形式…确认后执行" —— 前半是写法说明，后半有业务内容。
        # `strips_first`：这句的**整句形状**和"有实质尾截"的形状长得一样，先切整子句才不会
        # 把"本项目仅提供…数据支撑"一起丢掉。
        "shape_id": "BODY_NON_EXPANSION_HEAD",
        "strips_first": True,
        "detector": re.compile(
            r"(?:价款|费用结算|商务条款)[^。；]{0,18}?不在技术(?:报告|标)?正文[^。；]{0,8}?展开"),
        "strips": (
            {"kind": "HEAD",
             "pattern": re.compile(
                 r"(?:[^。；，]{0,30}[，、])?[^。；]{0,24}?不在技术(?:报告|标)?正文中?展开"
                 r"[，、](?P<keep>本项目[^。；]{6,})[^。；]*")},
        ),
    },
    {
        # "边界之外还有两个明确排除项：与本项目业态不符的其他项目模板场景不写进本方案…"
        # `sentence` 只认"排除登记/其他项目模板"这一种整句形状：Run F 里同一短语族
        # （"未经确认的历史数字""外部案例作对比"）也出现在**真实的统计口径句子**里
        # （"统计不引入未经确认的历史数字，样本量不足时如实标注为观察值"），那种句子有业务
        # 内容，整句改写等于替客户删掉方法论；所以它们只进 detector 报人工确认，不动笔。
        "shape_id": "TEMPLATE_AND_HISTORY_EXCLUSION",
        "detector": re.compile(
            r"其他项目模板|不写进本方案|明确排除项[：:]|未经确认的历史数字|外部案例作对比"),
        "sentence": re.compile(
            r"[^。；]*(?:其他项目模板场景?不写进本方案|[^。；]{0,16}?明确排除项[：:])[^。；]*"),
        "action": "REWRITE",
        "replacement": ("本方案按本项目现场条件与经核实的规模数据编制，"
                        "人员配置与服务指标以书面确认结果为准。"),
    },
    {
        # "凡涉及费用的口径不进入技术标正文，凡需甲方另行决策的改造投入只提建议不作承诺"
        # —— 两截都在讲"这份文档不写什么"，换成一句客户可读的口径。
        "shape_id": "FEE_AND_DECISION_NON_COMMITMENT",
        "detector": re.compile(
            r"(?:费用|造价)[^。；]{0,16}?(?:口径|事项|投入)[^。；]{0,16}?"
            r"不(?:进入|列入)技术(?:标)?正文"),
        "sentence": re.compile(
            r"[^。；]*(?:费用|造价)[^。；]*不进入技术(?:标)?正文[^。；]*"
            r"(?:只提建议不作承诺|不作承诺)[^。；]*"),
        "strips_first": True,
        "strips": (
            # "对应的预防行动同样只有三类，…不以此为由新增设备、人手或改造投入，
            #  涉及费用的口径不进入技术标正文" —— 前半整段都是真实作业安排，只切尾下那句写法说明。
            # `keep` 用**贪婪**量词：懒惰写法会在第一个逗号处收口，把中间有业务内容的几截一起丢掉。
            {"kind": "TAIL",
             "pattern": re.compile(
                 r"(?P<keep>[^。；]{12,})[，、](?P<narr>涉及费用的口径不进入技术(?:标)?正文)"
                 r"[^。；]*")},
        ),
        "action": "REWRITE",
        "replacement": "涉及费用的事项以合同约定为准，需甲方决策的改造投入经书面确认后实施。",
    },
    {
        # "上述事项在澄清前不作为承诺" / "凡涉及费用的口径…只提建议不作承诺" —— 治理语态。
        # `exempt`：Run F 里同一短语也出现在**真实服务规则**中（前台岗位只传递事实、
        # 以甲方授权为限），那是责任界面内容，不能当元叙述洗掉。
        "shape_id": "NOT_A_COMMITMENT_NOTE",
        "exempt": re.compile(r"服务岗位|对外经办|授权(?:为限|口径为限)"),
        "detector": re.compile(
            r"(?:上述|前述|这些)[^。；]{0,16}?在?(?:澄清|确认|核实)前?[^。；]{0,12}?"
            r"不作(?:为)?(?:承诺|服务依据|依据)|不作(?:为)?(?:承诺|服务依据)|"
            r"降为建议不作承诺|只提建议不作承诺"),
        "sentence": re.compile(
            r"(?:上述|前述|这些)[^。；]*不作(?:为)?(?:承诺|服务依据|依据)[^。；]*|"
            r"[^。；]*(?:口径|措施|事项)[^。；]{0,20}?只提建议不作承诺[^。；]*|"
            r"[^。；]*否则降为建议不作承诺[^。；]*"),
        "action": "REWRITE",
        "replacement": "上述事项以澄清结果为准。",
    },
    {
        # "没有证据，因此不得编造" —— 内部判定过程，客户成品里根本不该出现这种句子。
        "shape_id": "REASONING_ABOUT_MISSING_EVIDENCE",
        "detector": re.compile(
            r"(?:没有|缺少|暂无)[^。；]{0,12}?(?:证据|来源|依据)[^。；]{0,10}?"
            r"(?:因此|所以|故|因而)[^。；]{0,16}?(?:不|无法|不得)"),
        "sentence": re.compile(
            r"[^。；]*(?:没有|缺少|暂无)[^。；]{0,12}?(?:证据|来源|依据)"
            r"[^。；]{0,10}?(?:因此|所以|故|因而)[^。；]*"),
        "action": "DROP",
        "replacement": "",
    },
    {
        # 开发/治理词直接漏进正文：内部分类名、分母口径、门、证据合同。
        # "整句删除／登记单注明"是 Run F 里一条**内部审校清单**被当成正文写了出来
        # （"…出现即整句删除并在登记单注明删除理由与所在章节"）——它描述的是本产品的质检
        # 动作，不是给甲方的服务安排。词表刻意只收这种无处可辩解的复合形，不收裸"所在章节"。
        "shape_id": "DEVELOPER_GOVERNANCE_VOCABULARY",
        # `clause_guard`：这一族词是**词面**判据，一句里只要沾到一个就整句 DROP，
        # 于是"客服中心承接全部报修工单，…覆盖率口径不再另行说明"这种混写句被连业务一起删。
        # 打上这个标记后，整句 DROP 之前先走一遍泛化层（子句角色判据），
        # 能把业务子句留下、治理口径词又全切干净时就用子句结果；否则照旧整句不出现。
        "clause_guard": True,
        "detector": re.compile(
            r"内部分类|分类名称|登记不响应|不进(?:入)?分母|分母口径|覆盖率口径|"
            r"评分归属|可响应性|证据合同|内容单元|节契约|门控|生成前门|"
            r"整句删除|登记单注明|判定单"),
        "sentence": re.compile(
            r"[^。；]*(?:内部分类|分类名称|登记不响应|不进入分母|分母口径|覆盖率口径|"
            r"评分归属|可响应性|证据合同|内容单元|节契约|门控|生成前门|"
            r"整句删除|登记单注明|判定单)[^。；]*"),
        "action": "DROP",
        "replacement": "",
    },
)


# ---------------------------------------------------------------------------
# MMF008A2-E：泛化层——按「子句角色」判定内部编写语言，而不是按已知句逐条匹配
# ---------------------------------------------------------------------------
# 上面那张形状表是从 Run F 的 20 条真实命中长出来的，每条都写死了整句形状。任务书的
# 要求是反过来的：换一份标书、换一套章节写法，同一类「文档在讲自己怎么写」的句子
# 还得能认出来。所以这一层不吃具体文本，只吃**四个语法角色**的关系：
#
#   actor   —— 说话的主体是不是这份文档/这套编写系统本身（本节、正文、判定规则、第二条…）
#   op      —— 谓语是不是在**操作文字**（采用…法、分为…档、写成、标注、设置表格、不展开…）
#   object  —— 宾语是不是文字制品（正文、章节、句子、语气、表格、附页、口径…）
#   shield  —— 这一截是不是甲方/合同侧的真实接口（谁批、什么条件下才动、澄清路径）
#
# actor ∧ (op ∨ object) ∧ ¬shield ⇒ 内部编写语言。三个角色缺一个都不动手：
# 只有关键词没有主体（"记录载体"）、只有主体没有文字操作（"本节列入移交说明"）都保留。
#
# shield 分两层，这是本轮最需要说清的取舍：
# ① **硬盾**（甲方/合同/书面答复/另行确认/不构成承诺…）永远放行——任务书明令"不得把
#    客户可读的边界陈述当内部元叙述"，删掉业务含义比漏掉一句元叙述严重。
# ② **软盾**（成立前提、待澄清、未确认、为准…）只在**没有文档主体**时放行：
#    "待澄清事项以甲方书面答复为准"是澄清机制（放），"把正文句子分为已确认事实、条件化方法
#    与待澄清事项三档"是编写系统的语气分类（不收）。
GENERIC_INTERNAL_VERSION = "client-facing-prose-generic-v0.1"

# actor：文档自指，**要求在主语位置**。上一版用 `search` 在整个子句里找"正文/本节"，
# 于是"另一句正常正文内容"（去重反例）、"本部分列示投标报价表"（报价段不被洗白的反例）
# 都被当成了文档在自述写法——宾语里出现一个文档构件不是主体。
# 允许一句连接词开头（"因此正文所有表述…"），但仍然只吃掉这一小段前缀。
DOC_SELF = re.compile(
    # 引导连词再收 `如/若/当/即使` 这一族：「如正文篇幅受限」的主语仍然是"正文"，
    # 条件形不改变"这一截在谈这份文稿"这件事。仍然只吃**头部的一个**引导词，
    # 主语位置的要求没有放松。
    # 列表序号（`3. ` / `（一）` / `二、`）也不算主语的一部分：编号条目里的
    # 「3. 本条只列写进正文的内容」主体仍是"本条"，序号只是它的位置记号。
    r"^(?:\s*(?:\d{1,3}[.、)）]|[（(][一二三四五六七八九十\d]{1,3}[)）]"
    r"|[一二三四五六七八九十]{1,3}[、.])\s*)?"
    r"(?:因此|为此|据此|同时|另外|此外|并且|故而|所以|如|若|当|即使)?(?:"
    r"(?:本|该|此)(?:章|节|部分|单元|方案|文本|书|处|段|表|台账|清单|矩阵|栏|页|条|款|批|稿|卷|篇)"
    r"|正文|技术标正文|全文|篇幅|标题|目录|提示语|排除项|挂靠功能|占位"
    r"|(?:表中|表内|栏中|格内)\s*(?:不|只|均|一律|固定|出现|列出|登记|写入|保留))")
# actor：编写/评审系统本身。"判定方法/判定规则/口径控制/分档比较"是**编写系统的名字**，
# 而"预警判定""判定动作"是对现场状态下判定——只有名字形态进这一类。
GOV_SELF = re.compile(
    r"判定(?:方法|规则|口径|标准)|分级(?:规则|方法|口径)|语言(?:规则|分级)"
    r"|口径控制|相对分档|分档比较|评审口径|自查口径|(?:三种|两种|几种)语气"
    r"|(?:写作|编写|表述|登记|组织)(?:规则|口径|方法|约定)|责任界定遵循"
    r"|统一(?:表述|写作|口径)(?:为|成)|(?:深度|范围|口径|表述)统一(?:表述|写作)"
    # 评分/分值归属（MMF008A5-B1）：一条现场作业安排在讲的不是"谁去干"，
    # 而是"这件事算在哪个评分项、登记在哪一份内部口径里"——那是评审系统的归属表。
    r"|(?:评分|分值|得分|计分)[^。；，]{0,6}(?:归属|核算|对应|映射)")
# actor：指向文档自身占位物的说法。两种位置都算：
#   主语位（"该提示指向的材料为…"）与**宾语位**（"证据补齐后仅替换本处提示语"）——
#   宾语形的动词只有"替换/补入/清空"这几类，动作的对象是提示占位物本身，
#   客户成品里没有一句"替换提示语"是服务安排。
POINTER_HEAD = re.compile(
    r"^(?:该|本|此|上述)提示|(?:提示语|占位符)(?:指向|替换|补入|内容)"
    r"|(?:替换|补入|清空|换掉)[^。；，]{0,4}?(?:本|该|此)?处?(?:提示语|提示|占位)")
# actor：成串的编写步骤。"第二步是差异比对"是现场流程（客户可读），"第四条口径控制"、
# "第二条是语言分级规则"、"第一类句子一律改为…表述"是文档在编号自己的写法。
ENM_HEAD = re.compile(
    r"^第?[一二三四五六七八九十百]{1,3}\s*[、.．]?\s*(?:条|类|档|层|组)"
    r"(?:是|为|：|:)?(?:语言|写作|编写|表述|语气|分档|口径控制)"
    r"|^第?[一二三四五六七八九十百]{1,3}\s*(?:步骤?|小?节)(?:控制|口径|规则)"
    r"|^第?[一二三四五六七八九十百]{1,3}\s*(?:类|种)\s*(?:句子|表述|语气|口径)"
    r"|^第?[一二三四五六七八九十百]{1,3}\s*[、.．]\s*\S{0,16}?(?:规则|口径|方法|表述|句子)")
# Scoring used to organise authoring is an internal operation, regardless of
# whether the clause also mentions a legitimate service action. Client service
# statistics and operational priority remain business content.
SCORING_AUTHORING = re.compile(
    r"(?:评分因素|评审因素|分值高低|分值区间|得分差)[^。；;]{0,35}"
    r"(?:内部编制|编制与复核|优先级排序|项目内部.{0,12}只用于)")
# object：被操作的文字制品。`前半句/后半句/写法/句式/措辞/行文` 是**语言单位**而不是文档
# 构件——它们做宾语时这句话谈论的就是文字本身，客户成品里没有一句"前半句"是服务安排。
TEXT_OBJECT = re.compile(
    r"正文|章节|小节|句子|半句|写法|句式|措辞|行文|语气|档(?:位)?|层级|表格|附页|附件页|空白附"
    r"|说明段|说明页|描述页|描述|履历表|栏|字段|格位|条目|清单|矩阵|台账|口径|表述|提示语|标题|目录"
    # MMF008A5-B1/B2：这一族的补集全是**只对文字成立**的对象——篇幅、段落、描述性文字、
    # 内部编号/内部标识。现场作业里没有"篇幅"这种东西，一句"如正文篇幅受限"谈论的
    # 只可能是这本标书怎么排版，不是任何服务安排。
    r"|篇幅|段落|描述性文字|内部(?:编号|标识|台账)")
# op：作用在文字制品上的动作。"展开"一类要求文档自指主体在场才生效（见 _clause_roles），
# 现场工作的"展开作业"因此不会被当成写法说明。
WRITE_OP = re.compile(
    r"采用[^。；]{0,60}?(?:法|口径|规则|方式|原则)(?:组织|展开|登记|编写)?"
    r"|组织[^。；，]{0,14}?(?:内容|响应|正文)|分为[^。；，]{0,24}?(?:档|层|类|组|三档)"
    r"|分入[^。；，]{0,24}?(?:档|层|类|组)"
    r"|(?:设置|不为其设置|不再为其设置)[^。；，]{0,10}?(?:矩阵|表格|附页|附件页|栏|空白附|说明段)"
    # `(?<![部等大各这那])分列`：中文没有词边界，"本部分列示…"里的"部分列"曾被裸 `分列`
    # 读成一个文字操作动词，于是一句报价对照说明被判成内部编写语言并整截删掉。
    r"|(?<![部等大各这那])分列|分栏|归为|列为|记为|写成|写进|不写进|改写|取[^。；，]{0,8}口径"
    r"|表述|标注|登记|挂起|逐条|引用关系|走通|复算|隔离|不出现|不再讨论"
    r"|不展开|不重复展开|不再展开|不进入|不作[^。；，]{0,10}宣称"
    r"|不新增[^。；，]{0,14}承诺|不扩大解释|不缩减|不填|固定的是|删除|替换本处"
    # MMF008A5-B1/B2：Codex 点名的那一族编写动作——把文字**压短、摘掉、再讲一遍、逐项对上**。
    # 它们只在有文档主体（`doc_self`/`locative_self`）的同截里才参与判定，
    # 所以"垃圾压缩站每日清理""整改项逐条核对"这类现场句子不受影响：
    # 那一截里没有文档构件做主语，`actor` 的第一条分支根本进不来。
    r"|压缩|删减|精简|复述|核对|逐项对应|受限"
    # 「涉及承诺的句子一律加限定条件」：给句子**加限定条件**是给文字加工，不是服务动作。
    r"|加限定条件"
    # 「本节把编制过程本身固化为可重复执行的程序」：把**编写动作**本身固化，主体是文档、
    # 宾语是文档自己的产物，客户读它得不到任何服务安排。只认 `固化为/固化成` 这一形，
    # 现场口语里的"固定下来/固化标准"不受影响。
    r"|固化(?:为|成)")
# 两栖动词（MMF008A4-C）：`标注 / 登记` 在现场作业里同样是常用谓语——交接表上"未关闭项
# 续填到下一班并在本栏标注"、台账上"只登记已核实的数据"，说的都是记录载体怎么填、登什么，
# 不是这份文档怎么写。它们参与文字操作判定必须再有一件**文字制品**做宾语
# （正文/章节/句子/口径…）或同句另有只对文字成立的谓语，否则文档自指主体会拿着一个
# 现场动词把真服务规则整截切掉。
TEXT_ONLY_OP = re.compile(
    r"改写|写成|写进|不写进|表述|分列|分栏|归为|列为|记为|复算|走通|挂起|隔离"
    r"|不展开|不重复展开|不再展开|不进入|不作[^。；，]{0,10}宣称|不新增[^。；，]{0,14}承诺"
    r"|不扩大解释|不缩减|不填|固定的是|删除|替换本处|固化(?:为|成)|不出现|不再讨论|引用关系"
    r"|采用[^。；]{0,60}?(?:法|口径|规则|方式|原则)(?:组织|展开|登记|编写)?"
    r"|组织[^。；，]{0,14}?(?:内容|响应|正文)"
    r"|(?:设置|不为其设置|不再为其设置)[^。；，]{0,10}?(?:矩阵|表格|附页|附件页|栏|空白附|说明段)"
    # 排他性谓语同时进这张表（MMF008A5-B1）：`_frame_label_spillover` 的
    # "只对文字成立的谓语在场"那一格读的就是它——治理口径标签后面的内容段
    # 只要出现"删减/复述/另行说明"，被操作的对象就是文字，现场盾不该替它撑腰。
    r"|删减|精简|复述|(?:不再)?另行说明|加限定条件")
# 只对文字成立的谓语（MMF008A5-B1）：这一族**不需要文档主体在场**就能定性——
# 现场作业里没有"删减""精简""复述"的宾语，"指标数据不得删减"讲的只能是这份文稿怎么写。
# 与 `WRITE_OP` 的分工要说清：`压缩` / `核对` / `受限` 这类词在现场口语里同样成立
# （垃圾压缩站、隐患逐项核对、场地条件受限），所以它们留在 `WRITE_OP` 里、
# 必须有文档主体同截在场才算编写动作；这一张表只收**排他性**谓语。
# 这一格**不进 `named`**：软盾照常生效，"隐患照片不得删减"里有现场实质词在场时仍然放行，
# 那一句说的是记录留痕的范围，不是这份文稿的篇幅。
TEXT_ONLY_VERB = re.compile(r"删减|精简|复述|改写|写成|编入|编进|另行说明|不再另行说明")
# 只有在**同截有一件文字制品做宾语**时才成立的裁剪谓语（MMF008A5-B1）：
# `压缩` 在物业现场同样说得通（垃圾压缩站、压缩式清运车），所以它不像"删减"那样排他，
# 必须与"段落/描述性文字/篇幅/正文"同截在场才构成编写动作。
# 「如正文篇幅受限，可压缩描述性段落」的中间那一截就是这个形。
TEXT_DEPENDENT_OP = re.compile(r"压缩|扩充|限缩|裁剪|改短")
# 文字制品的**去处**（MMF008A5-B1）：`进入/写入/列入/转入 + 这份文稿自己的构件` 与它的否定形。
# 主语可以是任何业务对象（未覆盖项、其余内容、内部编号），只要谓语在决定"这条写不写进文稿、
# 写到哪一份文稿构件"，这一截谈论的就是文档自己，而不是现场。两个方向都要（动词→构件、构件→动词），
# 中文两种写法都常见：「未覆盖项不进入正文」与「不在正文出现内部编号」。
# 宾语那一头**只收这份标书自己的构件**：裸"台账/清单/登记表"在物业现场是**运行载体**，
# "待澄清清单"更是一条甲方看得懂的澄清机制——「监理另有要求的部位，先列入待澄清清单，
# 取得书面答复后排入计划」曾被这一格切掉半句（既有反例 G5 就是这条）。所以载体名要构成
# "文稿构件"必须带 `内部` 限定（"转入内部台账"这一形照收，Codex 点名的形状）。
# 与 `TEXT_ONLY_VERB` 同一姿态：**不进 `named`**，软盾在场就放行
# （"台账上出现未闭环项"是运行规则，处所论元恰好也是"台账"）。
AUTHORING_DESTINATION = re.compile(
    r"(?:不|未)?(?:进入|写入|写进|列入|转入|登入|登记到|放进|放到|放在|落在|出现在|单列)"
    r"[^。；，]{0,8}?(?:正文|本方案|技术标正文|技术标|说明段|说明页|描述页|附页|附件页|附录"
    r"|内部台账|内部清单|内部登记|内部表样)"
    r"|(?:正文|本方案|附录|附页|说明段|内部台账)[^。；，]{0,8}?(?:出现|列明|登载|刊载|写出|写明)")
# 分类语气形：把正文句子按"事实/建议/待澄清"分档的说法，本身就是编写系统的自述
TONE_LABEL = re.compile(
    r"已确认事实|实施建议|条件化(?:方法|方式)|条件性内容|待澄清事项|语言分级|三种语气|生效条件的表述")

# ---- 另外三种"文档在讲自己怎么写"的主体形（MMF008A3-C）------------------------
# 上一版只认「本节/正文」这类**文档构件**做主体。Run F 之外还有一类同样常见的写法叙述，
# 它的主体不是某一节，而是**语言单位本身**——写法、前半句、句式：
#
#     结合实际条件的实施建议的写法须成对出现，前半句写明…，后半句写明…，禁止只写结论不写前提。
#
# 这一句里没有一个"本节"，但它整句都在规定**句子怎么写**，对客户零信息量。判据仍然按
# 角色走（主体是语言单位 ∧ 谓语作用于文字），不是把这句抄进表里。
METALANGUAGE_SUBJECT = re.compile(
    # 语言单位做主语 + 一个编写谓语。谓语这一族放宽到"采用/遵循/保持/统一/做到"
    # （MMF008A5-B1）：「行文采用统一表述」「行文时岗位名称保持前后一致」与
    # 「写法须成对出现」是同一个算子——主语是语言单位，说的就只是这份文稿怎么写。
    r"^(?:[^。；，]{0,28}?)?(?:写法|句式|措辞|行文|语气)(?:须|应|要|必须|应当|一律|不得|禁止|是|为"
    r"|采用|遵循|保持|统一|做到)"
    # 「行文时…」「编写时…」「成文时…」是**编写时间状语**：它限定的整件事都发生在编稿桌上，
    # 不是现场工序的时序（现场那一族叫"进场时/交接时/巡检时"，不在这张表里）。
    r"|^(?:行文|编写|写作|成文|排版)(?:时|中|上|方面|过程)"
    r"|^(?:前半句|后半句|上半句|下半句|句首|句末)[^。；，]{0,10}?"
    r"(?:写明|写成|写|是|为|应|须|登记|标注|采用)"
    r"|^(?:禁止|不得|不能|勿)只?写[^。；，]{0,16}?(?:不|未)写"
    r"|^(?:须|应|须要)成对出现")
# 主体也可以是"文档把自己放在哪"的**位置自指**：本章里/正文中/本节内登记着什么。
# 与 `DOC_SELF` 的差别只在句法位置——`此类内容在本章仅登记为…` 的主体是"本章"，
# 只是被介词短语包住了。后置的方位词可省（"在本章登记"与"在本章中登记"同形）。
# `在服务文件里/在合同里` 不在此列：那是甲方侧接口，另有硬盾。
# `(?<![不未])` 是必需的一道：`不在本节重复表述` 里的"本节"是**否定谓语的处所论元**
# （说的是这件事在哪儿讲），不是"本章如何处置文字"的框架；上一版没有这个回看，
# 把这句客户可读的范围陈述的后半截切掉了（已知集 S-28、fresh HS-23 同形）。
LOCATIVE_SELF = re.compile(
    r"(?<![不未])(?:在|于)(?:本|该|此)(?:章|节|部分|单元|正文|文本|处|段|表|栏|矩阵|台账|清单)")
# 拒绝生成文档构件的**构式**（无主语也要算内部）：不为其生成说明段/清单/空白附件页。
# 动词与宾语两头都必须是文档构件，缺一不算——"不生成月度报表"是服务安排，不是文档构件。
GENERATION_REFUSAL = re.compile(
    # 「本节不再为其生成说明段」：文档主体（本节）可以出现在拒绝生成的谓语之前，
    # 否定与谓语之间还常夹一粒 `再/另行/单独/另外`（MMF008A5-B1 补这两处空洞——
    # 上一版只认 `^不为其生成…`，于是带"本节"前缀与"不再"的那一形整条漏掉）。
    # 宾语那头仍然必须是文档构件：`不生成月度报表` 是服务安排，不收。
    r"^(?:并?且?)?(?:(?:本|该|此)(?:章|节|部分|单元|文本))?并?且?不"
    r"(?:再|另行|单独|另外|会)?(?:为其|为它|对此)?(?:生成|设置|列|附|给出|编排)[^。；，]{0,24}?"
    r"(?:说明段|说明页|描述页|空白附|附件页|附页|矩阵|表格|清单|栏|履历表)")
# 主语**本身就是文字制品**的形：「评分表述与分档用语一律不进入正文」「内部编号不入正文」。
# 这一形不需要"本节"在场——主语已经是文档自己的构件，谓语再作用一次文字，整句就是元叙述。
# 限定条件：主语之后必须紧跟一个量化/否定副词（一律/均/不/只），否则"表述清楚责任界面"
# 这种正常业务句会被误收。
OBJECT_AS_SUBJECT = re.compile(
    r"^(?:[^。；，]{0,16}?)?(?:表述|用语|术语|编号|标识|口径|语气|提示语|脚注"
    r"|句子|段落)"                                        # 「涉及承诺的句子一律加限定条件」
    r"[^。；，]{0,8}?(?:一律|均|都不|全不|不进入|不入|不再|不得|禁止|只)")
# 「把各项评审因素逐条改写成需求条目」——`把 + 文字构件 + 改写` 的处置形：
# 对象与动作两头都在文字层，整句讲的是**怎么编这本标书**。两头各缺一头都不算。
DISPOSAL_CONSTRUCTION = re.compile(
    r"^(?:并?且?)?把[^。；，]{0,28}?(?:因素|表述|编号|条目|口径|语气|内容|要求|文字|用语)"
    r"[^。；，]{0,14}?(?:改写|写成|改成为?|转为|转成|登记|列为|归为|拆成|并成|删掉|删除)")
# 编号式的小标题（"第一步转译："、"第四条口径控制："）：文档在给**自己的编写步骤**编号。
# 判据是构式而非词表——`第X<量词>` 起头、以冒号收尾、中间没有谓语，就只是一枚标签。
# "第一步进场准备" 这类现场工序标题不以冒号收尾（它是列表项的内容），因此不收。
ENUMERATED_LABEL = re.compile(
    r"^第\s*[0-9一二三四五六七八九十百]{1,4}\s*(?:步|条|款|项|类|档|层|组|阶段)[^。；，]{0,14}[：:]$")
# 治理口径式的小标题（"××分层："、"××规则："、"归属划分："这一族）：文档在给**自己的
# 编写口径**立标签。判据是构式——整截只有「治理名词（+ 名词后缀）」并以冒号收尾，后面没有
# 任何谓语；现场表头（"巡检频次："、"责任人："、"记录归档位置："）不在此列，因为它们的
# 头词是现场事物而不是编写口径。与编号小标题同一条纪律：单独在场只报不改（见 `_label_only`）。
GOVERNANCE_LABEL = re.compile(
    # 头词这一族（MMF008A5-B1 补齐）：口径/分档/归属之外，还有一整类**给这份文稿自己起的
    # 名目**——写法说明、行文说明、表述统一说明、编制说明、写法提示、校对状态。
    # 它们与"巡检频次：""责任人：""记录归档位置：""备注："的分界不在词面，
    # 而在头词的所指：前一类指的是**编写系统**（怎么写、怎么对、怎么校），
    # 后一类指的是现场事物或表格载体本身。后缀允许 `(?:统一|一致|对应)?` 一粒，
    # 因为"表述统一说明"这一形里"统一"是名目的一部分而不是谓语；仍要求整截只有名目并以冒号收尾。
    r"^(?:[^。；，]{0,10}?)?(?:口径|分层|分档|分级|归类|分类|归属|命名|编号|登记|标注|处置"
    r"|写法|行文|表述|编制|校对|复核|措辞|体例|格式|语气|篇幅)"
    r"(?:(?:统一|一致|对应)?(?:规则|口径|方法|约定|原则|分层|分档|划分|依据|结果|要求"
    r"|说明|状态|提示|清单|表))?"
    r"(?:[：:])$")
# 语气切分形：句子在**给正文分类**（分为…档 / 记为…事项 / 按…表述）。这一形在场时，
# "待澄清事项"是标签名而不是澄清机制，硬盾让位——否则整条分类叙述都洗不掉。
TONE_DIVISION = re.compile(
    r"分(?:为|入|类)|归(?:为|入)|划(?:为|入)|列(?:为|入)|记为|写成|表述|标注为"
    r"|按[^。；，]{0,12}语气|使用[^。；，]{0,12}语气")
# shield ①：硬盾。甲方/合同/书面答复侧的真实接口与承诺边界，任何情况都不动。
# "确认/批准/答复"一类**必须带后续形**（后/前/结果/情况/书面/函/单）：裸"已确认事实"是
# 语气标签，不是接口，收进来就等于让最常见的一类元叙述永远放行。
CLIENT_INTERFACE = re.compile(
    # `本方(?!案)`：不做这个负向断言，"本方案"会被读成"本方"，于是**每一句**以
    # "本方案"开头的元叙述都拿到硬盾——这是量具的错，不是产品的判断。
    r"甲方|乙方|本方(?!案)|招标人|监理|使用方|双方|归口|经办|主管部门"
    r"|合同|招标文件|服务文件|书面(?:确认|答复|通知|同意|签认|材料)"
    r"|(?:确认|批准|答复|议定|认可|裁定)(?:后|前|结果|情况|书面|函|单)"
    r"|另行(?:书面)?确认|逐项确认"
    r"|(?:不构成|不作为|不作|不据此推定|不视为)[^。；，]{0,14}(?:承诺|服务依据|依据|新增)"
    # 「本节不作条款性表述」是**对合同条款的立场**（不重述条款），客户读得到且有边界含义，
    # 属于业务侧。只认"条款性/条款约定/条款原文"这一族；`需求条款编号` 那种内部标识
    # 不在这里放行——它引用的是编号体系本身，不是条款内容。
    r"|条款(?:性|约定|规定|原文|既有约定)"
    r"|待甲方(?:明确|确认)|待投标人|澄清(?:路径|前|结果)|未获答复")
# shield ②：软盾。只在没有文档主体时放行。
SOFT_SHIELD = re.compile(
    r"成立前提|失效条件|替代做法|待确认|待填写?|未确认|未经确认|尚未(?:取得|确认|最终)"
    r"|以[^。；，]{0,18}(?:为准|为据)|现场(?:核对|核验|复测)|可核查|可追溯|可指认"
    r"|不[把得]?\S{0,12}写成\S{0,8}承诺|防止\S{0,14}承诺|不当承诺|越权|越界")
# 现场实质：服务动作与现场对象。只在**没有文档主体**时参与放行判断，避免"本节不展开…"
# 这类句子因为带了"巡视"就被当成业务内容。
SUBSTANCE = re.compile(
    r"巡视|巡查|巡检|保洁|清洁|维护|保养|排班|班次|岗位|编制|交接|进场|移交|值守|疏导"
    r"|作业|派工|工单|记录载体|设备|机具|食材|就餐|消防|隐患|整改|回访|投诉")
# 客户可读的"对象—依据"句式：检查依据/判定线/锁定为三组/只按已确认的口径判定。
# 它们看着像编写规则，实际是**对现场状态下判定**，最容易被误伤，因此单列为硬放行。
SUBSTANCE_AS_OBJECT = re.compile(
    r"(?:检查|核验|核对|定性|判定|评价)依据|(?:锁定|圈定|划分)(?:为|成)|判定线|依据只有"
    r"|只按[^。；，]{0,16}(?:判定|执行|核算|报送|核[验对]?)|按[^。；，]{0,12}已确认的"
    r"(?:[^。；，]{0,8})(?:判定|执行|核算|表述判定)")
# 记录载体的两种身份（MMF008A4-C）：`本表 / 本栏 / 本台账` 既可以是**这份文档的构件**
# （"表中不出现对自身分档的描述"），也可以是**现场用来填写的运行载体**
# （续登到下一班的交接表、在本栏标记尚未闭环的那一行）。后者被 `在本栏 + 标注` 这种形
# 态撞上文档自指判据，就会被整截切掉——那是一条真服务运行规则。区分仍然按角色走：
# 指着载体 ∧ 说的是现场状态或现场动作 ∧ 同句没有一件文档构件或编写名词 ⇒ 现场盾。
FIELD_CARRIER = re.compile(r"(?:在|于)?本(?:表|栏|台账|清单|矩阵)|(?:该|此)(?:表|栏|台账)")
FIELD_STATE = re.compile(
    r"未关闭|未闭环|未完成|已闭环|闭环|异常|隐患|缺陷|故障|巡检|巡查|交接|班次|作业|工单"
    r"|到场|响应|整改|回访|投诉|签认|值守|疏导|保洁|维保|消杀|清运")
# 编写名词：这一族词在场，说明被登记/标注的对象是**文字**而不是现场状态，现场盾让位。
# 刻意不收"分类/归类/分级"——物业语境里它们是业务词（垃圾分类、风险分级管控），
# 收了就等于把一批真规则推回被切的一侧。
EDITORIAL_NOUN = re.compile(
    r"约束|口径|写法|句式|措辞|表述|编制|分档|语气|正文|章节|小节|句子|标题|目录"
    r"|说明段|说明页|描述页|履历表|提示语|附页|附件页|空白附")
_CLAUSE_SPLIT = re.compile(r"(?<=[；;，：:])")

# A criterion, ledger entry or training retelling is customer content too. Its
# role is determined by the operation/object pair, not the verb 'write/retell'.
_OPERATING_CONTENT = re.compile(
    r"(?:受理|核验|核对)[^。；]{0,48}(?:身份|通行|证件|访客|门区)"
    r"|(?:设备|设施)[^。；，]{0,20}(?:主责|责任|职责)(?:标注|登记|标记)"
    r"|"
    r"(?:情景|演练|培训|疏散)[^。；]{0,20}(?:复述|说明|确认|方向|顺序)"
    r"|(?:范围|条件|版本|修订|变化)[^。；]{0,28}(?:生效时点|承诺边界|历史)"
    r"|(?:生效时点|可回查)[^。；]{0,16}(?:文本|记录|历史)"
    r"|(?:规定|义务)[^。；]{0,26}(?:时限|对象)"
    r"|(?:项目负责人|履职|考核意见|专业结论|恢复使用)"
    r"|(?:口头需求|事项|协调|多方配合)[^。；]{0,22}(?:登记|编号|配合|决定|条目)"
    r"|(?:发酵|蒸制|时间)[^。；]{0,20}(?:可回查|记录)"
    r"|(?:复核|归并|复盘|核对)[^。；]{0,28}(?:条件|记录|条目|状态|字段|读数|区域|改进项|缺项|时点|经办岗位|完整性|当班|覆盖|判定结论|现场|数据|未闭合项|取值|已提请|未答复|已答复)"
    r"|(?:登记|分级)[^。；]{0,28}(?:发现的问题|追踪的条目|整改条目|故障记录)"
    r"|(?:咨询解答|跨岗位转介)[^。；]{0,28}(?:登记|口径|转介)"
    r"|(?:不假定|不预设)[^。；]{0,16}(?:外委安排|供方名单|外部人员编组)"
)
_NOMINAL_CRITERION = re.compile(
    r"^(?:本方案的)?[^。；，：:]{0,18}(?:执行标准|判定口径|边界表述)"
    r"(?:与(?:责任岗位|复核内容|判定条件|结论归属))?$")
_EXPLICIT_WRITING = re.compile(r"正文|章节|本节|本章|段落|句式|措辞|行文|写法|语气|分档|生成|评分")


def _operating_clause(text: str) -> bool:
    # An affected-chapter locator in a fact clarification register is not a
    # direction to author prose. Actual writing predicates retain priority.
    role_text = re.sub(r'(?:影响|所涉)章节', '适用范围', text)
    return bool(_OPERATING_CONTENT.search(role_text)) and not bool(_EXPLICIT_WRITING.search(role_text))


def _clause_roles(clause: str) -> dict[str, Any]:
    """一个子句的四个角色 + 是否内部编写语言。纯函数，便于逐条取证与回归。

    主体（actor）只认三种来源：文档自指并且这一截确实在**操作文字**、编写系统的名字
    （判定方法/口径控制/第四条）、指向文档自身的占位提示（"该提示指向的材料…"）。
    语气标签（已确认事实/实施建议/待澄清事项）**单独不构成主体**：
    "未确认事项一律按实施建议或待澄清表述"讲的是哪些事项按什么路径处理（澄清机制），
    "把正文句子分为已确认事实、条件化方法与待澄清事项三档"才是文档在自述分级方式——
    后者有"正文"这个文档主体在场。
    """
    text = str(clause or "").strip()
    if not text:
        return {"clause": text, "internal": False, "roles": {}}
    if _operating_clause(text) or _NOMINAL_CRITERION.fullmatch(text):
        return {"clause": text, "internal": False,
                "roles": {"operating_content": True, "soft_shield": True}}
    doc_self = bool(DOC_SELF.search(text))
    gov_self = bool(GOV_SELF.search(text))
    scoring_authoring = bool(SCORING_AUTHORING.search(text))
    enm = bool(ENM_HEAD.search(text))
    pointer = bool(POINTER_HEAD.search(text))
    metalanguage = bool(METALANGUAGE_SUBJECT.search(text))
    locative_self = bool(LOCATIVE_SELF.search(text))
    generation_refusal = bool(GENERATION_REFUSAL.search(text))
    object_as_subject = bool(OBJECT_AS_SUBJECT.search(text))
    disposal = bool(DISPOSAL_CONSTRUCTION.search(text))
    enumerated_label = bool(ENUMERATED_LABEL.match(text))
    obj = bool(TEXT_OBJECT.search(text))
    op = bool(WRITE_OP.search(text))
    text_only_op = bool(TEXT_ONLY_OP.search(text))
    # An operational subject can own a criterion just as a writing system can.
    # Enumeration and the word "criterion" alone do not identify authoring.
    if (re.match(r'^(?:(?:进场|交接|启动)?时序|关闭|验收|放行|故障|隐患|投诉)(?:的)?判定(?:口径|方法|规则|标准)', text)
            and not doc_self and not text_only_op):
        gov_self = False
    # 两个"不必有文档主体在场"的编写角色（MMF008A5-B1，见上面两张表的注释）：
    # 排他性谓语、文字制品的去处。两者都**不进 `named`**，盾照常生效。
    text_only_verb = bool(TEXT_ONLY_VERB.search(text))
    authoring_destination = bool(AUTHORING_DESTINATION.search(text))
    shortening_op = bool(TEXT_DEPENDENT_OP.search(text)) and obj
    field_operation = (bool(FIELD_CARRIER.search(text)) and bool(FIELD_STATE.search(text))
                       and not bool(EDITORIAL_NOUN.search(text)))
    tone_label = bool(TONE_LABEL.search(text))
    tone = tone_label and bool(TONE_DIVISION.search(text))
    governance_label = bool(GOVERNANCE_LABEL.match(text))
    hard = bool(CLIENT_INTERFACE.search(text)) or bool(SUBSTANCE_AS_OBJECT.search(text))
    soft = bool(SOFT_SHIELD.search(text)) or bool(SUBSTANCE.search(text))
    roles = {"doc_self": doc_self, "gov_self": gov_self, "enumerated_step": enm,
             "scoring_authoring": scoring_authoring,
             "placeholder_pointer": pointer, "metalanguage_subject": metalanguage,
             "locative_self": locative_self, "generation_refusal": generation_refusal,
             "object_as_subject": object_as_subject, "disposal_construction": disposal,
             "enumerated_label": enumerated_label, "governance_label": governance_label,
             "text_object": obj, "write_op": op, "text_only_op": text_only_op,
             "text_only_verb": text_only_verb, "authoring_destination": authoring_destination,
             "shortening_op": shortening_op,
             "field_carrier_operation": field_operation,
             "tone_label": tone_label, "tone_system": tone,
             "hard_shield": hard, "soft_shield": soft}
    # 主体必须**同时**有一个作用在文字上的谓语：`本节按 CH03 的口径执行` 里"本节"是主语、
    # "按…执行"不是文字操作，它是元数据修复（改名）的活，净化器不参与。
    # 语言单位做主体（写法/前半句）与"拒绝生成文档构件"两形自己就是文字操作，不必再凑 op。
    # `标注/登记` 这一类两栖动词单独不够（见 `TEXT_ONLY_OP` 的注释）：文档自指主体要动手，
    # 同句还得有一件文字制品或一个只对文字成立的谓语——否则交接班规则会被当写法说明切掉。
    # `field_operation` 再挡一层：载体上标注的是现场状态（未闭环/交接/续填）时，
    # 那一截说的是运行规则，文档自指形态（在本栏…）只是它的处所。
    actor = ((doc_self or locative_self) and op and (obj or text_only_op)
             and not field_operation) \
        or gov_self or enm or pointer or (tone and op) \
        or metalanguage or generation_refusal or enumerated_label or governance_label \
        or ((object_as_subject or disposal) and (op or obj)) \
        or ((text_only_verb or authoring_destination or shortening_op) and not field_operation)
    # 只有**编写系统的名字**或文档主体在场时，盾才可能让位；单独一个语气标签不构成让位
    # 理由："未确认事项按实施建议或待澄清表述"（软盾在场）是澄清机制，要留。
    named = (gov_self or enm or pointer or doc_self or locative_self or metalanguage
             or enumerated_label or governance_label or object_as_subject or disposal)
    # 主语就是文档自己的占位物（"该提示指向的材料为…"）时，硬盾也让位：这一句在说
    # "本处提示是什么"，客户从来不需要知道提示的存在，材料清单里有"合同"也不改变主语。
    shielded = (hard and not pointer and not (tone and named)) or (soft and not named)
    return {"clause": text, "internal": bool(scoring_authoring or (actor and not shielded)), "roles": roles}


# 两枚"只是标签"的主体形：编号式小标题与治理口径式小标题。它们自己不是元叙述：
# 标签后面接的可能是现场工序，也可能是编写动作；单独摘掉会打断客户可读的编号结构。
_LABEL_ROLES = ("enumerated_label", "governance_label")
_SUBSTANTIVE_ACTOR_ROLES = ("doc_self", "gov_self", "enumerated_step",
                            "placeholder_pointer", "metalanguage_subject", "locative_self",
                            "generation_refusal", "object_as_subject",
                            "disposal_construction", "tone_system")


def _label_only(verdict: dict[str, Any]) -> bool:
    """这一截是不是**只有一枚小标题**（"第二步是差异比对："、"口径分层："）。

    只因为一枚标签就把它从整句里摘掉，会把客户可读的编号结构打断（已知集 S-28 就是这么被
    误伤的）。所以标签只有在**同句还有别的内部子句**时才跟着一起切，单独在场只报不改。
    """
    if not verdict.get("internal"):
        return False
    roles = verdict.get("roles") or {}
    return bool(any(roles.get(key) for key in _LABEL_ROLES)) and not any(
        roles.get(key) for key in _SUBSTANTIVE_ACTOR_ROLES)


# 一条口径标签后面接的到底是不是"这条口径自己怎么算"？（MMF008A5-B2）
# 判据不看词表而看**形状**：一截内容里带着可交付的服务承诺形状——量化频次/时限/数量、
# 岗位指派、范围覆盖、现场动作——它就是在讲现场怎么干，不是在讲这份文稿怎么写。
# 「垃圾分类每日2次清运」「高峰时段加派1车」「一级故障2小时内到场」「全部楼栋覆盖」
# 「应急联系人由项目经理担任」「先安抚后处置」全是这一形；
# 「不写成对服务的承诺」「按三种语气登记」那种带文字谓语的仍然跟着标签算内部
# （下面 `textual_claim` 那一格优先）。
_SERVICE_CONTENT = re.compile(
    r"\d+\s*(?:次|小时|分钟|天|车|班|人|元|%|％)"
    r"|(?:每日|每天|每班|每周|每月|每季|每半年|每年)[^。；，]{0,8}?(?:次|清运|保洁|巡查|巡检"
    r"|签到|到场|响应|加派|记录|交接)"
    r"|(?:24|７|7)\s*小时"
    r"|由[^。；，]{0,12}?(?:担任|负责|分管|承接|指派)"
    r"|(?:加派|增派|增设|配备)\d"
    r"|(?:全部|所有|红线内)[^。；，]{0,10}?(?:覆盖|楼栋|区域|范围|公共区域)"
    r"|(?:清运|签到|进场|到场|响应|处置|录像|安抚|值守|巡查|巡检|保洁|绿化|秩序|交接"
    r"|更换|维修|养护|保养|疏通|消杀|清掏|返修|改造|翻修|清淤)"
    # 现场**组织形**（陌生内容夹具抓出来的一格过度净化）：「雨污分流改造以逐栋排布为单元组织」
    # 里没有数字、没有岗位指派，讲的却是工程怎么在现场铺开——一单元一单元地组织施工。
    # 判据是"以/按 + 单元/批次/轮次/班组 + 组织/施工/作业/推进"与"逐栋/逐层/分区"这两族
    # 现场组织说法，不是这一句本身。
    r"|(?:以|按)[^。；，]{0,10}?(?:单元|批次|轮次|班组|流水)[^。；，]{0,6}?"
    r"(?:组织|施工|作业|推进|排布|轮作)"
    r"|逐(?:栋|层|区|段|项)[^。；，]{0,6}?(?:排布|施工|改造|作业|清理|推进)"
    r"|(?:一级|二级|三级|重大|特级)[^。；，]{0,8}(?:故障|事件|响应|处置)")


def _frame_label_spillover(parts: list[str],
                           marked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """句首的治理口径标签是**框架**：它后面那几截是这条口径自己的内容说明。

    「一个治理口径小标题 + 冒号 + 由编写角色执行的分档叙述」——内容段自己判不出内部
    （既没有"本节"做主体，也没有正文类宾语），但它整段都在解释这套编写口径怎么分档，
    客户读它得不到任何服务安排。四条限制缺一不可：
    ① 只有**句首**的治理口径标签才有框架力（现场事物的标题——频次、责任人、归档位置——
       本来就不在 `GOVERNANCE_LABEL` 认的那族编写名词里）；
    ② 内容段自己带甲方接口或现场实质时**不跟着算内部**，那一截有业务含义，照旧留下；
    ③ 内容段自己带着**可交付的服务承诺形状**（`_SERVICE_CONTENT`：量化频次/时限/数量、
       岗位指派、范围覆盖、现场动作）时同样不跟着算内部——这一条是本轮修的过度净化：
       「分档说明：设施故障按影响程度分级响应，一级故障2小时内到场」被整句删掉，
       丢掉的是两条真承诺；标签照切，承诺照留；
    ④ 标签仍然只在同句还有别的内部子句时才动手（`_label_only` 那条纪律不变）。
    """
    if not marked or not parts:
        return marked
    head_roles = marked[0].get("roles") or {}
    # An operational category followed by its definition is a single carrier.
    # Keep the label and definition together; don't turn risk-category examples
    # into assertions by stripping their label. Explicit authoring still wins.
    whole = "".join(parts)
    if (_operating_clause(whole) and str(parts[0]).rstrip().endswith(("：", ":"))):
        return [({**row, "internal": False, "internal_basis": "OPERATIONAL_CATEGORY_FRAME"}
                 if not _EXPLICIT_WRITING.search(part) else row)
                for part, row in zip(parts, marked)]
    if not (head_roles.get("governance_label") and marked[0]["internal"]
            and str(parts[0]).rstrip().endswith(("：", ":"))):
        return marked
    for index in range(1, len(marked)):
        roles = marked[index].get("roles") or {}
        # 现场实质盾让位于**只对文字成立的谓语**：语气标签（已确认事实/实施建议/待澄清）或
        # `写成/表述/分档` 这一类动词在场时，即便同句提到岗位、班次，被操作的对象也是文字
        # 而不是现场状态（"不写成对服务的承诺"说的就是"怎么写"）。
        textual_claim = bool(roles.get("tone_label") or roles.get("text_only_op"))
        service_content = bool(_SERVICE_CONTENT.search(str(parts[index] or "")))
        if (marked[index]["internal"] or roles.get("hard_shield")
                or (roles.get("soft_shield") and not textual_claim)
                or (service_content and not textual_claim)):
            continue
        marked[index] = {**marked[index], "internal": True,
                         "internal_basis": "GOVERNANCE_LABEL_FRAME_CONTENT"}
    return marked


# 只剩字段名的那一格（MMF008A5-B2.3）：把内部内容切干净之后，如果剩下的只是**一枚以冒号
# 收尾的载体名**（`备注：`、`责任人：`、`校对状态：`），这一格已经没有了对象。
# 留着一个空字段比整句不出现更糟——表格会把它渲染成一行看得见的空白说明；
# 而把它连名一起收掉，客户读到的只是"这一栏没有内容"，那是事实。
_ORPHAN_FIELD_ONLY = re.compile(r"^(?:[^。；，：:]{1,12}[：:]\s*)+$")


def _generic_internal_move(core: str) -> tuple[str, str, str, list[dict[str, Any]]] | None:
    """整子句处置：每一截都是内部编写→整句不出现；内部与实质混写→只切内部那几截。

    收口只发生在 `；，、：` 这些子句边界上，绝不抠句子中段——这是"不产出残句"的结构保证。
    **没有主导性阈值**：即使内部部分占九成，带业务内容的那一截也照旧留下。删掉少数派
    业务内容换取"看起来干净"，是上一版 Run F 只有 1/14 命中被处理、另一版又把风控句一起
    洗掉的同一个根因（要么不动要么全动），这一层把它拆成两条独立的判据。
    """
    parts = [part for part in _CLAUSE_SPLIT.split(core) if part]
    # 收口时**不剥冒号**：`第一步转译：` 这种编号式小标题的判据就是"以冒号收尾、后面没有
    # 谓语"，把冒号剥掉等于把这枚证据擦掉。其它标点仍然是边界噪声，照旧剥。
    _RSTRIP = "。；;，、!！?？ "
    if len(parts) < 2:
        verdict = _clause_roles(core.rstrip(_RSTRIP))
        if verdict["internal"] and not _label_only(verdict):
            return "", "REMOVE", core, [verdict]
        return None
    marked = _frame_label_spillover(
        parts, [_clause_roles(part.rstrip(_RSTRIP)) for part in parts])
    internal_rows = [row for row in marked if row["internal"]]
    # 只有小标题在场、别的一格都不动 → 不动句子（见 `_label_only`）。
    if internal_rows and all(_label_only(row) for row in internal_rows):
        # 例外只给**治理口径标签**：它后面接的即使是客户可读的现场安排，标签本身仍然只是
        # 编写系统的目录词，切掉标签、内容照旧（REWRITE_HEAD）。编号式小标题不走这条路——
        # "第一步进场准备"那种编号是客户读得到的结构，不能因为一枚标签就断句。
        head_roles = marked[0].get("roles") or {}
        kept = [part for part, row in zip(parts, marked) if not row["internal"]]
        if (marked[0]["internal"] and head_roles.get("governance_label")
                and str(parts[0]).rstrip().endswith(("：", ":"))
                and len(kept) == len(parts) - 1 and kept):
            return "".join(kept).strip(), "REWRITE_HEAD", core, marked
        return None
    if not internal_rows:
        return None
    kept = [part for part, row in zip(parts, marked) if not row["internal"]]
    if not kept:
        return "", "REMOVE", core, marked
    joined = "".join(kept).strip()
    if _ORPHAN_FIELD_ONLY.match(joined):
        # 剩下的只有一枚字段名：整格收掉，别在客户稿里留一个丢了对象的空字段。
        return "", "REMOVE_ORPHAN_FIELD", core, marked
    head = parts[0].strip()
    # 引导性小标题（"可行性的判定方法是规模匹配："）：切掉引导语、后面整段照旧，
    # 补一句替代文本反而是在客户成品里再造一句元叙述。
    kind = ("REWRITE_HEAD" if len(kept) == len(parts) - 1 and marked[0]["internal"]
            and head.endswith("：") and len(head) < 26 else "REWRITE")
    return joined, kind, core, marked


_GENERIC_SHAPE: dict[str, Any] = {
    "shape_id": "GENERIC_INTERNAL_WRITING_NARRATION",
    "action": "DROP",
    "detector": None,
}


def _matches_keep(sentence: str) -> str:
    """KEEP 侧的两把盾：锚点是产品不变量（Bidder Evidence 门靠它在产物里定位），
    风控内容是**旧形状的**整句盾；泛化层自己按子句带盾，所以不被第二把盾整体挡住。"""
    if CONFIRMATION_ANCHOR.search(sentence):
        return "CONFIRMATION_ANCHOR"
    hit = RISK_CONTROL_KEEP.search(sentence)
    return f"RISK_CONTROL_CONTENT:{hit.group(0)}" if hit else ""


def _action_row(shape: dict[str, Any], sentence: str, output: str, matched: str,
                *, applied: bool, kind: str,
                not_applied_reason: str = "") -> dict[str, Any]:
    return {
        "rule_id": CLIENT_FACING_PROSE_RULE_ID,
        "shape_id": shape["shape_id"],
        "action": kind,
        "applied": applied,
        "not_applied_reason": not_applied_reason,
        "matched_text": matched,
        "before": sentence,
        "after": output,
    }


# ---- 改后语法清理（MMF008A3-C）----------------------------------------------
# 删掉一截内部叙述之后，剩下的必须**还是一句能读的话**。Run F 与上一轮 fresh 反例里真实
# 出现过的坏形状是这几类，它们都不是"措辞不好看"，是句法被切断了：
#   `，。` `；。` `，，` `：。`     —— 引导句被删空，只剩它后面的标点
#   `…为准，；月度从台账…`          —— 两个子句的边界标点叠在一起
#   `…处理按合同既有约定执行，`      —— 删除后以逗号/顿号收尾，后面没有下文
#   `因此，` / `并，`               —— 连接词失去它所连接的半句
#   `作业记录台账与`                —— 被删的那一截把连接词留在了尾巴上
#   `与，记录归档位置`（单元格半句） —— 表格单元格里删掉前半截后，连接词与分隔符留在开头
# 清理只做**减法**：不补词、不改写、不把逗号升级成句号。补出来的内容是本模块编的，
# 而这一层的职责只是让删除留下干净的接缝。
#
# 一条刻意的边界：`；` 与 `。` 是**句子自己的终结符**，绝不当悬空标点删。
# "…为准，；" 这种接缝修成 "…为准；"（删逗号留分号），而不是删成分号缺失——
# 上一版如果在整段上做清理，就会把两个句子粘成一句，那是另一种残句。
_PUNCT_BEFORE_END = re.compile(r"[，、：；;]+(?=[。！？!?])")
_PUNCT_BEFORE_SEMI = re.compile(r"[，、：]+(?=[；;])")
_PUNCT_RUN = re.compile(r"([，、；;：])\1+")
_DANGLING_TAIL = re.compile(r"[，、：]+$")
_ORPHAN_CONNECTIVE_TAIL = re.compile(
    r"[，、；:]?\s*(?:因此|为此|据此|所以|故而|并且|而且|同时|另外|此外|以及|并|而|则|且"
    r"|与|和|及|或)\s*$")
# 表格单元格里的半句（MMF008A4-C）：删掉一截之后，剩下那一截可能**以连接词或分隔符开头**
# （"与记录归档位置"、"，交接班时逐项确认"）。连接词只在紧跟标点时才剥——
# "和平路""参与协调"这类以同一个字开头的正常内容不能被动。
_LEADING_LINK = re.compile(r"^(?:以及|并且|并|与|和|及|或)(?=[，、：:])")
_LEADING_SEPARATOR = re.compile(r"^[，、：:；;]+")
_PUNCT_ONLY = re.compile(r"^[\s，、：；;。.！!？?]+$")


def _tidy_after_edit(text: str) -> str:
    """把删除留下的坏接缝收干净，反复到不动点（因此本身幂等）。"""
    value = str(text or "")
    if not value.strip() or _PUNCT_ONLY.match(value):
        return ""
    previous = None
    while previous != value:
        previous = value
        value = _PUNCT_BEFORE_END.sub("", value)
        value = _PUNCT_BEFORE_SEMI.sub("", value)
        value = _PUNCT_RUN.sub(r"\1", value)
        # 连接词独立悬在结尾（"…执行，因此"）或悬在开头（"与，记录归档位置"）：只删这一个词。
        value = _ORPHAN_CONNECTIVE_TAIL.sub("", value)
        value = _LEADING_LINK.sub("", value)
        value = _LEADING_SEPARATOR.sub("", value)
        value = _DANGLING_TAIL.sub("", value)
        value = re.sub(r"[ \t]{2,}", " ", value).strip()
    return value


def _apply_generic_move(core: str, sentence: str,
                        terminator: str) -> tuple[str, list[dict[str, Any]]] | None:
    """把泛化层那一次处置落成 (输出, 动作记录)，落点只写一处。

    形状表的 `clause_guard` 分支与 `_process_sentence` 的收尾共用这一份，
    否则同一个处置在两处补句末标点、两处判"有没有真的改动"，两把尺会各自漂。
    """
    move = _generic_internal_move(core)
    if move is None:
        return None
    output, kind, matched, marked = move
    if kind in ("REWRITE", "REWRITE_HEAD"):
        # 被切掉的如果是句中**最后几截**，留下来的那一截末尾原来带着"后面还有半句"的接缝
        # （`…三班倒值守；行文时…` 里那个分号）。后面没有了，那枚分隔符就成了悬空标点：
        # 先收掉接缝，再补上这一句自己的终结符——补什么由原句决定，不擅自把分号升成句号。
        output = re.sub(r"[，、；;：:]\s*$", "", output)
        output = output if output.endswith(_SENTENCE_END) else f"{output}{terminator}"
    output = _tidy_after_edit(output)
    if output == sentence.strip() or output == core:
        return None
    return output, [_action_row(_GENERIC_SHAPE, sentence, output, matched, applied=True,
                                kind=kind, not_applied_reason="") | {"clause_roles": [
        {"clause": row["clause"], "internal": row["internal"],
         "internal_basis": row.get("internal_basis", ""), "roles": row["roles"]}
        for row in marked]}]


_R07_DOCUMENT_NAVIGATION = re.compile(
    r"^(?:(?:至于|关于)[^。；]{1,100}，)?"
    r"(?:本|该|此)(?:章|节|部分)(?:后半部分|前半部分|后续部分|前文|后文|上文|下文)"
    r"[^。；]{0,100}?(?:给出|列出|阐述|介绍|说明)"
    r"[^。；]{1,120}(?:步骤|内容|方法|安排)[^。；]{0,120}$")
_R07_NAVIGATION_SHAPE = {
    "shape_id": "DOCUMENT_PORTION_NAVIGATION",
    "action": "REVIEW",
}
_R07_DOCUMENT_COVERAGE = re.compile(
    r"(?:^|，)(?:本|该|此)(?:章|节|部分)(?:只|仅)(?:固定|规定|列出|给出|界定)"
    r"[^。；]{1,90}$")
_R07_COVERAGE_SHAPE = {"shape_id": "DOCUMENT_COVERAGE_EXPLANATION", "action": "REVIEW"}


def _process_sentence(sentence: str, keep_reason: str = "") -> tuple[str, list[dict[str, Any]]]:
    """一句一处置：整句形状 → 改写/不出现；整子句形状 → 只切一截；片段形状 → 只报。

    片段命中**不立即返回**：Run F 里一句"涉及费用的口径不进入技术标正文，…只提建议不作承诺"
    先被前一条形状的探测器抓到，能被后一条形状的整句形状处理。探测器一命中就收工会让
    形状表的排列顺序决定"能不能修"，所以先把这条候选记下，继续找能动手的形状。

    已知形状全部落空之后，才轮到**泛化层**（`_generic_internal_move`）按子句角色判定。
    次序不是风格问题：形状表带替代文本，能把"正文中出现的每一个数字都有出处"换成客户
    语言；泛化层只会删，先跑它就把该换说法的句子删了。
    """
    core = _core(sentence)
    terminator = ""
    tail = sentence.rstrip()
    if tail and tail[-1] in _SENTENCE_END:
        terminator = tail[-1]
    pending: dict[str, Any] | None = None
    if not keep_reason:
        for shape in _INTERNAL_NARRATION_SHAPES:
            exempt = shape.get("exempt")
            if exempt is not None and exempt.search(core):
                continue
            moves = (("strips", "sentence") if shape.get("strips_first")
                     else ("sentence", "strips"))
            for move in moves:
                if move == "sentence":
                    pattern = shape.get("sentence")
                    if pattern is not None and core and pattern.fullmatch(core):
                        action = shape.get("action") or "DROP"
                        if (action != "REWRITE" and shape.get("clause_guard")
                                and keep_reason != "CONFIRMATION_ANCHOR"):
                            # 整句 DROP 之前先问一句：这一句里有没有**该留下的业务子句**？
                            # （MMF008A5-B2.2）「客服中心承接全部报修工单，…覆盖率口径不再
                            # 另行说明」整句被删，丢掉的是"承接全部报修工单"这条真安排。
                            # 只有泛化层既留下了内容、又把那些治理口径词一个不留地切干净时，
                            # 才用切子句的结果替掉整句 DROP；留下任何一处口径词就照旧整句不出现
                            # ——放宽的是"删多少"，不是"漏不漏"。
                            mixed = _apply_generic_move(core, sentence, terminator)
                            residue = shape.get("detector")
                            if (mixed is not None and mixed[0].strip()
                                    and (residue is None
                                         or not residue.search(_core(mixed[0])))):
                                return mixed
                        if action == "REWRITE":
                            # 替代文本自带句末标点时**不再补** terminator：写出"。。"或"。；"
                            # 会让 QA 的句末检查把一次修复读成一个新缺陷。
                            repl = str(shape["replacement"])
                            output = repl if repl[-1] in _SENTENCE_END else f"{repl}{terminator}"
                        else:
                            output = ""
                        output = _tidy_after_edit(output)
                        return output, [_action_row(shape, sentence, output, core,
                                                    applied=True, kind=action)]
                    continue
                for strip in shape.get("strips") or ():
                    match = strip["pattern"].fullmatch(core)
                    if not match:
                        continue
                    kept_text = (match.groupdict().get("keep") or "").strip()
                    if len(kept_text) < 6:
                        continue
                    output = _tidy_after_edit(f"{kept_text}{terminator}")
                    kind = f"STRIP_{'HEAD' if strip['kind'] == 'HEAD' else 'TAIL'}_CLAUSE"
                    return output, [_action_row(shape, sentence, output,
                                                match.group("narr") if "narr" in match.groupdict()
                                                else match.group(0),
                                                applied=True, kind=kind)]
            if pending is None:
                detector = shape.get("detector")
                if detector is not None:
                    hit = detector.search(core)
                    if hit:
                        pending = _action_row(shape, sentence, sentence, hit.group(0),
                                              applied=False,
                                              kind=shape.get("action") or "REVIEW",
                                              not_applied_reason="NOT_A_WHOLE_SENTENCE_SHAPE")
    if keep_reason != "CONFIRMATION_ANCHOR":
        applied = _apply_generic_move(core, sentence, terminator)
        if applied is not None:
            return applied
    if pending is not None:
        return sentence, [pending]
    # Only extend uncovered roles. REVIEW must never replace an existing
    # deterministic repair/BLOCK, nor weaken a previously reported fragment.
    navigation = _R07_DOCUMENT_NAVIGATION.fullmatch(core)
    if navigation and not keep_reason:
        return sentence, [_action_row(
            _R07_NAVIGATION_SHAPE, sentence, sentence, navigation.group(0),
            applied=False, kind="REVIEW_DOCUMENT_NAVIGATION",
            not_applied_reason="document navigation includes service content; source-backed edit required")]
    coverage = _R07_DOCUMENT_COVERAGE.search(core)
    if coverage and not keep_reason and not CLIENT_INTERFACE.search(coverage.group(0)):
        return sentence, [_action_row(
            _R07_COVERAGE_SHAPE, sentence, sentence, coverage.group(0),
            applied=False, kind="REVIEW_DOCUMENT_COVERAGE",
            not_applied_reason="document coverage statement; review native carrier before editing")]
    return sentence, []


def client_facing_prose(text: Any) -> tuple[str, list[dict[str, Any]]]:
    """内部推理 → 客户可读语言。返回 (新文本, 逐条动作记录)。

    逐动作留痕：改了什么、按哪条形状、原句是什么、命中片段在哪。净化器最容易犯的错是
    "悄悄删掉几句"，所以这里没有一条改写是不记名的；反过来，够不上"整句就是这句元叙述"
    的命中一律**只报不改**（`applied=False`），不用残句换掉一个可能有业务内容的句子。
    """
    value = str(text or "")
    if not value.strip():
        return value, []
    actions: list[dict[str, Any]] = []
    rebuilt: list[str] = []
    changed = False
    for sentence in _SENTENCE_SPLIT.split(value):
        if not sentence or not sentence.strip():
            rebuilt.append(sentence)
            continue
        keep_reason = _matches_keep(sentence)
        if keep_reason == "CONFIRMATION_ANCHOR":
            rebuilt.append(sentence)
            continue
        output, rows = _process_sentence(sentence, keep_reason)
        # A prose cleanup may remove authoring words, never business quantities
        # or the source-protection markers wrapped around verified quantities.
        protected_values = re.findall(
            r"\d+(?:\.\d+)?\s*(?:类岗位|平方米|小时|分钟|人|名|次|天|日|年|月|㎡|%|％)"
            r"|[\ue100\ue300][^\ue101\ue301]*[\ue101\ue301]", sentence)
        if output != sentence and any(output.count(v) < sentence.count(v) for v in protected_values):
            natural = re.sub(r"^(\s*)(?:正文|本方案)(?:表述|写)为", r"\1", sentence)
            if natural != sentence and all(natural.count(v) == sentence.count(v) for v in protected_values):
                output = natural
                rows = [_action_row(
                    {"shape_id": "SERVICE_FACT_AUTHORING_PREFIX"}, sentence, output,
                    sentence[:len(sentence)-len(natural)], applied=True, kind="REWRITE")]
            else:
                output = sentence
                rows = [{**row, "applied": False,
                         "not_applied_reason": "BUSINESS_VALUE_WOULD_BE_REMOVED"} for row in rows]
        actions.extend(rows)
        if output != sentence:
            changed = True
        rebuilt.append(output)
    if not changed:
        return value, actions
    joined = "".join(rebuilt)
    # 整段再过一遍清理：跨句的坏接缝（前一句被删空、后一句以标点开头）在单句层面看不见。
    joined = _tidy_after_edit(joined)
    # 段末的分号（MMF008A5-B2.3）：`；` 是**分隔符**，它成立的后面还有另一截
    # （`秩序维护实行三班倒值守；行文时…` 被切成两句，后一句整句不出现）。
    # 后面那一截没了，它就是段末一个悬空接缝。这里不删——删了就把前后两句粘成一句，
    # 那是另一种残句——改成本段真正的终结符。
    joined = re.sub(r"[；;](?=\s*$)", "。", joined)
    joined = re.sub(r"[ \t]{2,}", " ", joined)
    return joined.strip(), actions


def internal_narration_findings(text: Any, path: str = "artifact",
                                *, severity: str = "AUTO_REPAIR") -> list[dict[str, Any]]:
    """检测端与改写端同源：能改的报 AUTO_REPAIR，够不上整句的报人工确认。

    形状表只有一份。检测与修复各写一套词，历史上就会出现"检出口有、修复口没有"，
    句子原样进客户 DOCX（§43 的 Run D 教训）。
    """
    _value, actions = client_facing_prose(text)
    findings: list[dict[str, Any]] = []
    for row in actions:
        applied = bool(row.get("applied"))
        findings.append({
            "rule_id": CLIENT_FACING_PROSE_RULE_ID,
            "path": path,
            "severity": severity if applied else "NEEDS_REVIEW",
            "issue": (f"internal reasoning narrated to the client reader ({row['shape_id']})"
                      if applied else
                      f"sentence contains an internal-reasoning fragment ({row['shape_id']}) "
                      "but is not wholly that shape; needs human review, not auto rewrite"),
            "shape_id": row["shape_id"],
            "matched_text": row["matched_text"],
            "auto_repair_applied": applied,
            "not_applied_reason": row.get("not_applied_reason"),
            "repair_action": CLIENT_FACING_PROSE_REPAIR_ACTION,
            "scope": "sentence",
        })
    return findings


# 兼容旧名（形状表在本模块内只有一份，检测端与改写端共用）。
INTERNAL_NARRATION_SHAPES = _INTERNAL_NARRATION_SHAPES
