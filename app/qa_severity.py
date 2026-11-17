"""Round 3 §32：QA 严重性四分类的唯一口径 —— 先分对类，再决定交付。

四类（§32 原文）：

- `TRUE_BLOCK`：产物里确实有缺陷，且不修就没法交（真空标题、真实漏号、真实坏引号）。
- `NEEDS_REVIEW`：机器判不死、必须人看一眼的（语气、口径、被禁字样的上下文）。
- `WARNING`：不影响可交付性的兼容性/风格风险（WPS 弯引号密度、重复标签）。
- `FALSE_POSITIVE`：**规则自己能否证的"这不是缺陷"** —— 必须带可复核的理由与原文，
  不能只给一个标签。没有理由的 FALSE_POSITIVE 一律退回 NEEDS_REVIEW。

为什么要单独一层：上一轮 52 条 BLOCK 里 45 条是误报（Replay-C），而下游
`delivery_state` 只看规则名、不看严重性，"BLOCK" 这个词一进 finding 就自动变成
一个待复核缺陷。本模块把"类"与"交付"分开：规则负责给出带证据的类，交付只按类走。

红线（AGENTS.md / §32）：这一层**只会把类判得更准，不会把严重性改松来制造通过** ——
`classify_findings` 遇到 `qa_class` 与声明 `severity` 冲突时取更严的那一档，
且 TRUE_BLOCK 永远进 issues。
"""
from __future__ import annotations

import re
from typing import Any

CLASS_TRUE_BLOCK = "TRUE_BLOCK"
CLASS_NEEDS_REVIEW = "NEEDS_REVIEW"
CLASS_WARNING = "WARNING"
CLASS_FALSE_POSITIVE = "FALSE_POSITIVE"

QA_CLASSES: tuple[str, ...] = (CLASS_TRUE_BLOCK, CLASS_NEEDS_REVIEW, CLASS_WARNING, CLASS_FALSE_POSITIVE)

# 类 → 既有 severity 词表（下游仍读 severity，两套口径由这里单向对齐，不允许各写各的）。
SEVERITY_BY_CLASS = {
    CLASS_TRUE_BLOCK: "BLOCK",
    CLASS_NEEDS_REVIEW: "NEEDS_REVIEW",
    CLASS_WARNING: "WARNING",
    CLASS_FALSE_POSITIVE: "FALSE_POSITIVE",
}
_CLASS_BY_SEVERITY = {
    "BLOCK": CLASS_TRUE_BLOCK,
    "CRITICAL": CLASS_TRUE_BLOCK,
    "P0": CLASS_TRUE_BLOCK,
    "NEEDS_REVIEW": CLASS_NEEDS_REVIEW,
    "AUTO_REPAIR": CLASS_NEEDS_REVIEW,
    "REVIEW": CLASS_NEEDS_REVIEW,
    "WARNING": CLASS_WARNING,
    "FALSE_POSITIVE": CLASS_FALSE_POSITIVE,
}
# 从严优先：qa_class 与声明 severity 不一致时取更严的一档。
_RANK = {CLASS_TRUE_BLOCK: 3, CLASS_NEEDS_REVIEW: 2, CLASS_WARNING: 1, CLASS_FALSE_POSITIVE: 0}

# §33 否定句口径：中文的否定词是单字前缀，必须在**同一小句内**紧邻被引短语才生效，
# 否则"本节安排不新增岗位；同时已新增岗位"这种句子会被前半句洗白。
_CLAUSE_BREAK = re.compile(r"[。；;！!？\n]")
# 局部作用域（MMF008A3-B）：否定与引述是**小句内**的算子，它的势力范围到最近的一个
# 停顿为止。`、` 是并列顿号，属于同一个作用域（"未提供巡检记录、培训签到表"里
# 顿号后面的仍在"未提供"管内），而 `，` `：` 开一个新的小句 —— 尤其是"判断依据来自
# 现场特征【而非】某类笼统说法：服务标的位于 X，为 Y 这种构型"的形状：被否定的对象在
# 冒号左边（那是一种论述口径），事实断言在冒号右边。拿左边的对比词去洗右边的既存事实，
# 就是"看见一个否定词就 suppress 全句"。
_LOCAL_SCOPE_BREAK = re.compile(r"[。；;！!？\n，,：:（）()【】]")
# 否定词的候选（长的在前，先匹配强否定）。中文否定可以离动词短语有一小段距离：
# "不因程序设计而新增岗位" 的"不"距"新增岗位" 7 字，只贴边判会漏，因此允许 ≤12 个汉字/字母的间距。
_NEGATION_HEADS = ("禁止", "严禁", "杜绝", "不需要", "无需", "不得", "不再", "不必", "不予", "未", "不", "没", "勿", "莫", "非", "无")
# 并列顿号共享同一个谓语（MMF008A4-B）：「未提供 [甲] 、 [乙] 的证据」里两截都在"未提供"管内，
# 只认第一截会把第二截当成被主张的既存事实。作用域仍然有限：最多四截、每截 ≤12 字，
# 且**逗号/冒号/句末一律断开**——文言里两条独立断言也常被写成一串，把它们算进同一个否定
# 作用域就等于让一个否定词洗掉后面整句；这条界只有"有没有停顿"，不是词表。
_NEGATION_GAP_COORD = re.compile(r"[a-zA-Z\u4e00-\u9fff]{0,12}(?:、[a-zA-Z\u4e00-\u9fff]{0,12}){0,3}")
# 这些单字否定前缀后面紧跟这些字时不是否定："未来/无人/非法/非常/无缝"。
_NEGATION_EXCEPTIONS = {"未": ("来", "必"), "无": ("人", "缝", "穷"), "非": ("常", "法", "遗")}
_ASSERTION_MARKERS = re.compile(r"已经|均已|曾在|过去|既往")
# "被当成/表述为/是否/而非"是**提及**一个说法而不是**使用**它（Replay-C 的"被当成已经完成"）。
# 只认紧邻短语左侧的标记，不用整句扫描 —— 否则"我司已建立机制，避免投诉"会被句尾的"避免"洗白。
_MENTION_ADJACENT = re.compile(
    r"(?:被当成|被视作|被理解成|当成|视作|视为|误认|误判|表述为|说成|写成|称为|所谓|关于|针对|并非|不是|不是所谓|是否|而非|而不|避免|防止|不得|不应|不准|质疑)\s*$"
)
# 条件/否定口径分两档作用域（MMF008A3-B）。上一版把这两档合成了一把尺：只要**整小句**里
# 出现过任何一个这类词，句中的命中就一律算"非主张"。于是"依据来自项目特征【而非】某类
# 笼统说法：服务标的……为某现场构型"里，冒号左边那个否定论述口径的词，把冒号右边三条
# 现场事实全洗白了（Run F 实测：detector 认得出那两处空间构型，语义也给了
# UNSUPPORTED_EXISTING_CLAIM，最后仍然 PASS）。两档的语法身份本来就不同：
# - 条件从属连词是**框架**算子，但它支配的是"自己那一截 + 读作后半句的那几截"，
#   不是一路管到句末（MMF008A6-B1 把这条收到 `condition_governed_spans`），且只往前管；
# - 对比/否认词是**小句内**算子，作用域到最近一个停顿为止。
# 条件从属连词（MMF008A4-B 补 `如` 这一档）：单字 `如` 引导的框架与 `若 / 如果 / 倘若`
# 是同一个算子，只差一个字，此前只列举了后者，于是"如+主体+确认…"整句被当成事实主张挡下。
# 单字 `如` 必须带**词界**守卫：前一字是 比/例/不/诸/宛/恰/无 时它是"比如/例如/不如/
# 诸如/宛如/恰如"里的一颗语素，不是连词；后一字是 今/同/表/图/下 时是"如今/如同/
# 如表/如图/如下"（时间与比拟、交叉引用），也不是条件框架。
_SUBORDINATOR = re.compile(
    # 间接问句不构成条件框架；让步条件仍由其引导词建立作用域。
    r"无论|不论|即便|即使|如果|假如|倘若|若是|若(?!干)|一旦"
    r"|(?<![比例不诸宛恰无])如(?![今同表图下])")
_CONTRAST_DENIAL = re.compile(r"并非|不是|而非|而不|并未|从不|绝不|未曾")
# 假设/前提这一档（MMF008A5-B1）：语法身份与 `如果/若` 相同——引导一个显式假设框架——
# 但它**同时是汉语里的普通名词**（"基于上述假设"
# "这一假设不成立"）。所以不并进上面那张"看见即条件"的连词表，而是额外要求它站在
# **小句头**：位于句首，或紧跟在一个停顿标点之后。**位置就是词性凭据**——
# 「上述假设不成立，本项目已配备…」里"假设"前面没有停顿，它是名词主语，
# 不能把后半句那个既存事实主张洗成条件；「假设…确需共用，…」里它后面才是要被支配的那一截。
_HYPOTHESIS_HEAD = re.compile(r"假设|假定|设若|设想|要是|万一|前提(?:是|下)")
_PAUSE_BEFORE_HEAD = re.compile(r"[，,；;：:]$")
# 名词化这一档（MMF008A6-B1）：位置只证明"它站在小句头"，不证明它的词性。框架词紧跟在
# **属格标记**或**问择标记**后面时，它是被讨论的那个东西本身，不是引出假设的算子：
# 「框架词 + 的 + 属性名」这一形里它是定语中心语，「框架词 + 是否 + 谓语」这一形里它是
# 被提问的主语。守卫只看框架词右侧那一到两个字，不扫全句。
_HYPOTHESIS_NOMINALISER = re.compile(r"^的|^是否|^能否|^可否|^与否|^有无")
# —— MMF008A6-B1：条件框架实际**支配**的那一截（frame → governed clause span → claim span）——
# 中文的条件框架是**跨小句的从属结构**，不是句级算子。它自己那一截（protasis）当然归它管；
# 往后只在"读作结果"的那些小句里继续有效。结果小句的语法凭据是**非现实语态**
# （则/就/便/那么/方可/才/应/将/拟/会/可/须/需…），而一条带既存状态体貌的**直陈**小句
# 不可能同时是假设的后半句——它另起一个命题，框架到此为止。
# 三条终止判据按强度排：① 跨了句末或分号（`_CLAUSE_BREAK` 那一档硬边界，本来就该停）；
# ② 这一截是直陈的既存断言（已/均已/现有/既往/此前…）——这就是"新独立主语＋既存谓语"
#    那个形状的量尺，认的是**体貌**而不是某个连接词；③ 这一截既没有结果标记，
#    又以篇章连接词重新起手——连接词只是同一条"另起命题"判据上的可见形状，不是决定者。
_CONSEQUENCE_MARKER = re.compile(
    r"则|就|便|那么|方可|才(?!干)|应|将|拟|会|可|须|需|宜|得(?![到])")
_INDICATIVE_EXISTENCE = re.compile(
    r"已|均已|现有|既往|此前|曾经|历史上|迄今为止|目前在岗")
# 篇章重新起手的连接词：封闭的一小撮**功能词**（任何一类工程、任何一份标书都会这么写），
# 不是项目词表。只在被检那一截的**开头**比对，句中出现的同一个词不算重新起手。
_DISCOURSE_RESET_CONNECTIVE = re.compile(
    r"^(?:此外|另外|另|另一方面|同时|实际上|事实上|其实|再者|其次|再次|除此之外|必须指出|需要说明)")
# 主题—评论结构里的**后置**排除谓语（MMF008A4-B）：中文把被谈的对象写在前面，把
# "它不算在本项目范围内"这件评论写在后面——「X + 否定系词 + 范围类名词」。
# 这不是"看见否定词就免罪"，两道限制缺一不可：
# ① 谓语必须在命中之后、**同一句**之内，且与命中之间不跨逗号/冒号、不超过约 12 字
#    （跨了就说明那个否定词说的是另一件事的主语）；
# ② 它否认的宾语必须是**范围/归属**这一类，不是另一个名词——
#    「本项目为 A，非 B 类项目」否认的是 B 这个身份，A 那条既存事实主张照样成立。
_POST_DENIAL = re.compile(
    r"(?:并|亦|均|皆|决|绝|素|根)?(?:非(?!常)|不属于|不属|不在|不是|不计入|不纳入|不作|不作为"
    r"|不列入|未列入|不含|不涵盖|不包括)")
_POST_DENIAL_GAP = 12
_SCOPE_OR_MEMBERSHIP = re.compile(
    r"范围|之外|以内|之内|清单|正文|标段|包件|本项目|本项|本卷|本次|本技术标|资信|资格"
    r"|商务标|别处|他处|另行|另册|由.{0,12}(?:举证|承担|负责)")


def normalize_text(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _clause_span(text: str, start: int) -> tuple[int, int]:
    """包含 `start` 的那一小句在原串里的 [left, right) 区间。"""
    text = str(text or "")
    left = 0
    for boundary in _CLAUSE_BREAK.finditer(text[:start]):
        left = boundary.end()
    right_match = _CLAUSE_BREAK.search(text, start)
    return left, right_match.start() if right_match else len(text)


def clause_of(text: str, start: int, end: int | None = None) -> str:
    """取出包含 [start,end) 的那一小句（到最近的小句边界为止）。"""
    text = str(text or "")
    left, right = _clause_span(text, start)
    if end is not None:
        right = max(right, end)
    return text[left:right]


def _local_span(text: str, start: int) -> tuple[int, int]:
    """包含 `start` 的**最小**小句区间：除了句级边界，还在逗号与冒号处断开。

    顿号不断：并列列举共享同一个谓语（"未提供 A、B、C" 的三样都被"未提供"管着）。
    """
    text = str(text or "")
    left = 0
    for boundary in _LOCAL_SCOPE_BREAK.finditer(text[:start]):
        left = boundary.end()
    right_match = _LOCAL_SCOPE_BREAK.search(text, start)
    return left, right_match.start() if right_match else len(text)


def sentence_tail_boundary(text: str, pos: int) -> int | None:
    """`pos` 所在的那一小句如果**一直到句末**，返回句末标点的位置；否则返回 None。

    这是给修复端用的：它改完一截直陈断言后要问"这句话还说没说别的"，才能决定要不要在句末
    补一条现状核验边界。组合的是已有的两把尺（`_local_span` 给这一截小句的右界、
    `_clause_span` 给所在句子的右界），两边相同才说明这一截就是句子的最后一截——
    不在修复端另写一份标点表，那是第二把尺。
    """
    text = str(text or "")
    _, local_right = _local_span(text, pos)
    _, sentence_right = _clause_span(text, pos)
    return local_right if local_right >= sentence_right else None


def is_scope_excluded(text: str, start: int, end: int | None = None) -> bool:
    """命中短语是不是**被排除出本项目范围**的主题（"对象 + 排除谓语 + 范围类名词"那一格）。

    与 `is_denied` 的区别只在谓语的位置：前者的否定词在短语**之前**（"未 + 提供 + 这一项"），
    中文的主题—评论结构里更常见的是对象在前、排除谓语在后。两者是同一种语义行为
    （这一句没有把 X 当作当前项目的既存事实来主张），所以并入同一个 DENIED 类别，
    但各用自己的作用域判据：本函数只看命中之后、同句之内的这一段。
    """
    text = str(text or "")
    end = end if end is not None else start
    break_match = _CLAUSE_BREAK.search(text, end)
    tail = text[end:break_match.start() if break_match else len(text)]
    for hit in _POST_DENIAL.finditer(tail):
        between = tail[:hit.start()]
        if len(between) > _POST_DENIAL_GAP or _LOCAL_SCOPE_BREAK.search(between):
            # 中间跨了逗号/冒号，或隔了整整一个小句的量：这个否定词说的是别的主语，
            # 后面再出现的也一样不作用于本命中。
            break
        if _SCOPE_OR_MEMBERSHIP.search(tail[hit.end():hit.end() + 24]):
            return True
    return False


def is_denied(text: str, start: int, end: int | None = None) -> bool:
    """被引短语是否处在否定语境里（"不新增岗位"、"亦不因程序设计而新增岗位"、"未新增岗位承诺"）。

    两种位置都算否定同一件事：前置否定（"未 + 动词 + 这一项"）与中文主题—评论结构的
    后置排除谓语（对象在前、"排除出范围"这一评论在后）。两者都意味着这一句**没有**把
    X 当作当前项目的既存事实来主张，所以并入同一类；作用域判据各自独立，
    都不做整句关键词扫描。
    """
    text = str(text or "")
    if is_scope_excluded(text, start, end):
        return True
    left, _ = _clause_span(text, start)
    head = text[left:start]
    if not head:
        return False
    for token in _NEGATION_HEADS:
        index = head.rfind(token)
        if index < 0:
            continue
        gap = head[index + len(token):]
        if gap[:1] in _NEGATION_EXCEPTIONS.get(token, ()):
            continue
        if gap and not _NEGATION_GAP_COORD.fullmatch(gap):
            continue          # 夹了逗号/冒号/句末/数字 → 否定管的是前一个并列项，不作用到本短语
        if _ASSERTION_MARKERS.search(gap):
            continue          # "不是已经完成" 这类由 MENTION 口径处理，不算被否定的承诺
        return True
    return False


def is_mention_not_use(text: str, start: int, end: int | None = None) -> bool:
    """短语只是被引用/被质疑（"被当成已经完成"、"而非某类笼统说法"），不是主张。

    作用域按算子类型分两档（MMF008A3-B）：

    - **引述与对比/否认**（视为 / 而非 / 并非 / 不纳入…）只在被引短语的**局部小句**里
      起作用，且必须出现在短语**之前**。跨过一个冒号或逗号的对比词管不到后面那句
      事实陈述——"依据来自项目特征而非某类笼统说法：服务标的……为某现场构型"里被否定的
      是左边那种论述口径，不是右边的现场构型。
    - **条件从属连词**（如果 / 若 / 无论 / 即便…）是整条框架的算子，但它同样是**前置**算子，
      出现在命中之后的条件词不回头洗前面的主张；它能延伸到哪一截，由 `is_conditioned`
      那条支配范围判据决定（MMF008A6-B1），这里不重算一遍。
    """
    text = str(text or "")
    left_local, _ = _local_span(text, start)
    head_local = text[left_local:start]
    if _MENTION_ADJACENT.search(head_local):
        return True
    # Denied inference governs its embedded proposition, not every assertion
    # elsewhere in the sentence. The inference complement stays in this clause.
    if re.search(r"(?:不|不得|不能|不可)(?:因[^，,：:；;。]{1,35})?"
                 r"(?:就|便|即)?(?:认定|认为|判定|视为)[^，,：:；;。]{0,12}$", head_local):
        return True
    return bool(_CONTRAST_DENIAL.search(head_local))


def _nominalised_head(unit: str, match: re.Match[str]) -> bool:
    """框架词右侧是不是**属格/问择标记**——是则它是被谈到的那个名词，不是引出假设的算子。

    `前提(?:是|下)` 会把「框架词 + 是否 + 谓语」这一形里的 `是` 吃进框架词，右侧只剩 `否`，
    所以 A-not-A 的两种可见形状都要认：右侧整串 `是否/能否/与否…`，或
    "框架词以 `是` 收尾 + 右侧紧跟 `否`"。只看右侧那一两个字，不扫全句。
    """
    tail = unit[match.end():]
    if _HYPOTHESIS_NOMINALISER.match(tail):
        return True
    return tail[:1] == "否" and match.group(0).endswith("是")


def _hypothesis_frame_starts(unit: str) -> list[int]:
    """这一截小句里**成立**的假设/前提框架起点（位置与名词化两道守卫都过才算）。"""
    starts: list[int] = []
    for match in _HYPOTHESIS_HEAD.finditer(unit):
        if match.start() != 0 and not _PAUSE_BEFORE_HEAD.search(unit[:match.start()]):
            continue          # 不在小句头：它是名词主语（「上述假设不成立」那一形）
        if _nominalised_head(unit, match):
            continue          # 被 的/是否 名词化：它是这句话谈论的对象
        starts.append(match.start())
    return starts


def _hypothesis_frame(head: str) -> bool:
    """命中之前的这一段里，有没有一个**站在小句头且没被名词化**的假设/前提框架词。

    名字与口径都并到 `_hypothesis_frame_starts` 那一处，这里只是它的布尔读法——
    判据只有一把尺，不在别处再扫一遍。
    """
    return bool(_hypothesis_frame_starts(str(head or "")))


def _clause_units(text: str) -> list[tuple[int, int, str]]:
    """把整串切成小句单元 `[(left, right, 结束它的那个标点)]`。

    边界取 `_LOCAL_SCOPE_BREAK` 那一档（句末与分号也在里面）：条件框架的支配范围要按**小句**
    走，而"这一截结束了没有"必须能从结束符本身读出来，所以把结束符一并带回来。
    """
    units: list[tuple[int, int, str]] = []
    start = 0
    for boundary in _LOCAL_SCOPE_BREAK.finditer(text):
        if boundary.start() > start:
            units.append((start, boundary.start(), boundary.group(0)))
        start = boundary.end()
    if start < len(text):
        units.append((start, len(text), ""))
    return units


def _frame_starts_in_unit(text: str, left: int, right: int) -> list[int]:
    """这一截小句里所有**成立**的条件框架起点（全局偏移）。

    两档框架各有各的词性凭据：从属连词（若/如果/即使/无论/一旦/如…）词类唯一，
    出现在这一截里就算框架；假设/前提那一档兼作名词，还要过位置与名词化两道守卫
    （`_hypothesis_frame_starts`）。
    """
    unit = text[left:right]
    starts = [left + match.start() for match in _SUBORDINATOR.finditer(unit)]
    return starts + [left + offset for offset in _hypothesis_frame_starts(unit)]


def _governed_continuation(text: str, left: int, right: int) -> bool:
    """后面这一截小句还读不读作同一条框架的**后半句**（apodosis）。

    三条否决按强度排：篇章重新起手（另起一个命题）、直陈的既存断言（带"已/现有/既往"
    这一类体貌的小句不可能同时是假设的后半句），以及"没有任何非现实标记"——
    中文的结果小句要靠 则/就/便/方可/才/应/将/拟/会/可/须/需 这一类语态标记才读得出
    它是被支配的那一截，一个都没有就是独立命题。**判不出来即不支配**（fail closed）。
    """
    unit = text[left:right]
    if _DISCOURSE_RESET_CONNECTIVE.search(unit):
        return False
    if _INDICATIVE_EXISTENCE.search(unit):
        return False
    return bool(_CONSEQUENCE_MARKER.search(unit))


def condition_governed_spans(text: str) -> list[tuple[int, int]]:
    """每条成立框架**实际支配**的区间 `[框架词起点, 支配终点)`。

    支配范围 = 框架自己那一截小句，加上紧随其后、逐截都读作后半句的那些小句；
    遇到句末/分号、篇章重新起手、直陈既存断言，或缺任何结果标记，即在那一截之前终止。
    起点用**框架词自己**的位置而不是小句左界：写在命中之后的条件词不回头支配。
    """
    text = str(text or "")
    units = _clause_units(text)
    spans: list[tuple[int, int]] = []
    for index, (left, right, terminator) in enumerate(units):
        for frame_start in _frame_starts_in_unit(text, left, right):
            end, cursor, current_term = right, index, terminator
            while cursor + 1 < len(units):
                if _CLAUSE_BREAK.fullmatch(current_term or ""):
                    break      # 句末与分号：硬边界，框架跨不过去
                next_left, next_right, next_term = units[cursor + 1]
                if not _governed_continuation(text, next_left, next_right):
                    break      # 独立命题开启：前一框架到此终止
                end, cursor, current_term = next_right, cursor + 1, next_term
            spans.append((frame_start, end))
    return spans


def is_conditioned(text: str, start: int, end: int | None = None) -> bool:
    """命中是不是落在某条**成立**框架实际支配的那一截里（frame → governed clause span → claim span）。

    上一轮的尺是"句首有框架词 + 命中在句内任何位置"，于是同句逗号后面的独立断言被洗白
    （Codex A5 Gate B1：M-NOUN-01/02/03、M-INDEPENDENT-02/03 五条 fresh false-pass）。
    这一轮的三条判据全是结构性的，没有一条认"句子里出现过某个词"：

    - **框架要成立**：从属连词按词类认；假设/前提那一档还要站在小句头，且右侧不挂 的/是否
      （位置与名词化是词性凭据，不是措辞黑名单）；
    - **支配范围要算出来**：只延伸到读作后半句的那些小句，直陈的既存断言与篇章重新起手都终止它；
    - **命中要在范围里且在框架词之后**：前置算子不回头洗前面的主张。

    任一环节判不出证据，这一截就读作**直陈**：门槛按 unsupported existing fact 挡下，
    不因为"句中有条件词"自动 suppress。
    """
    text = str(text or "")
    claim_end = start if end is None else end
    return any(frame_start <= start and claim_end <= governed_end
               for frame_start, governed_end in condition_governed_spans(text))


def semantic_status(text: str, start: int, end: int | None = None) -> str:
    """一个命中短语在其原句里的语义：DENIED / MENTION / CONDITIONAL / ASSERTED。

    三档非主张各自只管自己那点作用域（顺序即优先级：先问有没有被否定，再问是不是
    只被引述，最后问在不在条件框架里）。任何一步都不许"全句扫一遍"——那是把
    别处的否定词当成这一处主张的免罪符。
    """
    if is_denied(text, start, end):
        return "DENIED"
    if is_mention_not_use(text, start, end):
        return "MENTION"
    if is_conditioned(text, start, end):
        return "CONDITIONAL"
    return "ASSERTED"


def classify_finding(row: dict[str, Any]) -> dict[str, Any]:
    """给一条 finding 定类。

    - 没声明 `qa_class`：按声明的 severity 推。
    - 声明 FALSE_POSITIVE 但**没有可复核理由**：不接受，也不反手判成确定缺陷 → NEEDS_REVIEW。
    - 声明 FALSE_POSITIVE **且带理由**：这是规则用证据推翻自己先前的 BLOCK（§32/§33 的修法本体），
      允许，但必须写 `severity_overridden_by_evidence` 留痕，并且整行进 false_positives 而不是被丢掉。
    - 其余情况与 severity 冲突时取更严的那一档（不能把 BLOCK 改标 WARNING 来制造通过）。
    """
    finding = dict(row)
    declared_class = str(finding.get("qa_class") or "").upper()
    if declared_class not in QA_CLASSES:
        declared_class = None
    declared_severity = str(finding.get("severity") or "").upper()
    severity_class = _CLASS_BY_SEVERITY.get(declared_severity or "")
    reason = str(finding.get("classification_reason") or "").strip()
    if declared_class == CLASS_FALSE_POSITIVE and not reason:
        finding["declared_qa_class"] = CLASS_FALSE_POSITIVE
        finding["declared_severity"] = declared_severity
        finding["qa_class"] = CLASS_NEEDS_REVIEW
        finding["severity"] = declared_severity or "NEEDS_REVIEW"
        finding["delivery_blocking"] = False
        finding["classification_reason"] = (
            str(finding.get("issue") or "") + "（FALSE_POSITIVE 未给出可复核理由，退回 NEEDS_REVIEW）"
        )
        return finding
    if declared_class is None:
        qa_class = severity_class or CLASS_NEEDS_REVIEW
    elif declared_class == CLASS_FALSE_POSITIVE:
        qa_class = CLASS_FALSE_POSITIVE
        if severity_class == CLASS_TRUE_BLOCK:
            finding["severity_overridden_by_evidence"] = declared_severity
    else:
        qa_class = declared_class if severity_class is None or _RANK[declared_class] >= _RANK[severity_class] else severity_class
    finding["qa_class"] = qa_class
    finding["declared_severity"] = declared_severity
    finding["declared_qa_class"] = declared_class
    # severity 与类同档时**保留原标签**：AUTO_REPAIR 这类口径词下游（交付分级、表格门）在读，
    # 统一改写成 NEEDS_REVIEW 会把"文件仍可交付、只是要自动修补"这个区别抹平。
    # 只有类被推高（或没声明 severity）时才按类归一，避免把 BLOCK 降级成 WARNING。
    same_band = bool(declared_severity) and (_CLASS_BY_SEVERITY.get(declared_severity) or qa_class) == qa_class
    if qa_class == CLASS_FALSE_POSITIVE:
        finding["severity"] = declared_severity or "FALSE_POSITIVE"
    elif same_band:
        finding["severity"] = declared_severity
    else:
        finding["severity"] = SEVERITY_BY_CLASS[qa_class]
    finding["delivery_blocking"] = qa_class == CLASS_TRUE_BLOCK
    finding.setdefault("classification_reason", reason)
    return finding


def classify_findings(findings: list[dict[str, Any]]) -> dict[str, Any]:
    """把一批 finding 分成"仍算问题"与"能否证的非缺陷"两份，并给出四类计数。"""
    issues: list[dict[str, Any]] = []
    false_positives: list[dict[str, Any]] = []
    counts = {key: 0 for key in QA_CLASSES}
    by_rule: dict[str, dict[str, int]] = {}
    for row in findings or []:
        finding = classify_finding(row)
        qa_class = str(finding["qa_class"])
        counts[qa_class] = counts.get(qa_class, 0) + 1
        rule = str(finding.get("rule_id") or finding.get("claim_type") or "")
        bucket = by_rule.setdefault(rule, {key: 0 for key in QA_CLASSES})
        bucket[qa_class] = bucket.get(qa_class, 0) + 1
        if qa_class == CLASS_FALSE_POSITIVE:
            false_positives.append(finding)
        else:
            issues.append(finding)
    if counts.get(CLASS_TRUE_BLOCK):
        status = CLASS_TRUE_BLOCK
    elif counts.get(CLASS_NEEDS_REVIEW):
        status = CLASS_NEEDS_REVIEW
    elif counts.get(CLASS_WARNING):
        status = CLASS_WARNING
    else:
        status = "PASS"
    return {
        "schema_version": "qa-severity-classes-v0.1",
        "findings": issues,
        "false_positives": false_positives,
        "summary": {
            "status": status,
            "counts": counts,
            "counts_by_rule": by_rule,
            "true_block_count": counts.get(CLASS_TRUE_BLOCK, 0),
            "needs_review_count": counts.get(CLASS_NEEDS_REVIEW, 0),
            "warning_count": counts.get(CLASS_WARNING, 0),
            "false_positive_count": counts.get(CLASS_FALSE_POSITIVE, 0),
            "false_positive_overriding_block": sum(
                1 for row in false_positives if row.get("severity_overridden_by_evidence")),
            "false_positive_rate": round(
                counts.get(CLASS_FALSE_POSITIVE, 0) / max(1, sum(counts.values())), 4
            ),
            "policy": "先分类再决定交付；FALSE_POSITIVE 必须带可复核理由，未给理由退回 NEEDS_REVIEW",
        },
    }
