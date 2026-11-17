"""MMF-007C Round 3 / P0-1：真实分页标定 + 技术有效页口径（§9–§13、§15–§17）。

Round 2 的页数是 900 字/页估出来的，Run C 真实渲染 404 页时这把尺子已经偏了约 20%。
本模块只做两件事：

1. 把一次真实渲染（逐页可见文本）折算成 §13 要求的六类页数，并从中**反标**出
   真实的"每技术页字符密度"，供后续投影与超页门控使用；
2. 没有渲染结果时明确给出 basis=ESTIMATED（退回 document_qa 的 900 字/页 + 版面增量），
   绝不把估算冒充实测。

页面分类只用产物可见文本 + table_qa 的逐页版面特征，判据阈值一律取 R3 Replay §48-A
在 404 页真实产物上标定过的那一条线（见 `r3_page_calibration_crosscheck.json`）。
"""
from __future__ import annotations

import re
from typing import Any

from governance.document_qa import STRUCTURED_LAYOUT_UPLIFT, TEXT_CHARS_PER_PAGE

PAGE_CALIBRATION_VERSION = "technical-page-calibration-v0.1"

# §13 的六类页数在逐页层面就是这六类；结构化页/叙述页优先按真实版面特征切分
# （表格+清单占到的可见字比例），拿不到特征时才退回字符归属投影（split_technical_pages）。
PAGE_CLASSES = ("FRONT_MATTER", "BLANK", "PLACEHOLDER", "EXCLUDED_SCOPE",
                "NARRATIVE_TECHNICAL", "STRUCTURED_TECHNICAL")
TECHNICAL_CLASSES = ("NARRATIVE_TECHNICAL", "STRUCTURED_TECHNICAL")
NON_TECHNICAL_CLASSES = ("FRONT_MATTER", "BLANK", "PLACEHOLDER", "EXCLUDED_SCOPE")

MIN_PAGE_CONTENT_CHARS = 40
PLACEHOLDER_TOKENS = re.compile(
    # §42/§12：占位段按**整段**计。旧口径只数到"【待"两个字符，于是一页 30 个
    # 【待填写】（5 字）只算 60/150 字，一整页全是"【待投标人确认：岗位人员配置】"
    # （15 字）更是只算 2/15，等待填写的页会被读成技术有效页 —— 尺子对自己有利的那一侧。
    # 长话术放在前面，正则按"最左 + 先匹配到的分支"取，不会把"按项目要求填写"拆成"待填写"。
    r"按项目要求填写|待填写|请填写|待补充|【待[^】]{0,40}】|（待[^）]{0,40}）|_{3,}|×{3,}|待确认")
PLACEHOLDER_PAGE_CHARS_MIN = 120
# 占位页阈值按 Run C 404 页实测分布标定（r3_page_calibration_crosscheck.json）：
# 137 页出现过填写词，但字符占比最高只有 0.177、≥0.10 的只有 9 页、≥0.30 的 0 页 ——
# 这些词是表单里"按项目要求填写"那一列，页面本身仍有内容。所以：
# 有版面特征时按"填写占比 ≥0.10 且整页确实是表格页"判（= 回放 fill_share/table_share 两条线），
# 没有版面特征时只能看文本，要求整页近三分之一是占位词，避免把混合页从技术页里扣掉。
# 那组分布是**窄词元**下量的；换成上面的整段口径后在 Run C 404 页只读复算过一次：
# 全篇只差 1 页（技术有效页 392 → 391，占位页 8 → 9，每技术页密度 945.4 → 946.3 字），
# 阈值因此保持不动，证据见 r3_placeholder_token_probe_runC.json。
PLACEHOLDER_SHARE_MIN = 0.10
PLACEHOLDER_TABLE_SHARE_MIN = 0.50
PLACEHOLDER_DOMINANT_SHARE_MIN = 0.30
# 越界范围页：商务/报价/程序/资信命中多、技术信号词一个都没有。
# 词表与阈值都沿用 R3 Replay §48-A 对 404 页真实产物实测标定过的 `r3_page_audit.py`
# （PRICING/COMMERCIAL/QUALIFICATION/LEGAL_PROCEDURAL 四组 + hits≥3、技术词 0、表格占比≤0.20
# → Run C 只有 2 页）。这里**故意不**复用 P0-0 需求分类的 scope 词表：那套词是按一条
# 需求短句设计的，"报价""资质""澄清"单词命中就算越界；放到 1000 字的整页散文里会大量
# 误伤 —— 回放实测第 11/36/316/331 页都在讲技术口径与责任岗位，却因为顺带提到
# "招标公告""资格证明"被判成越界页，这是把技术页扣掉的假阳性，方向上对自己不利但数字是错的。
EXCLUDED_SCOPE_PATTERNS = (
    re.compile(r"投标报价|报价表|最高限价|预算金额|报价方式|价格部分|评标基准价|税率|含税|付款条件|结算方式|据实结算"),
    re.compile(r"投标保证金|履约保证金|代理服务费|联合体|商务部分|商务偏离|商务响应|商务条款"),
    re.compile(r"营业执照|资质证书|资格证明|信用证书|AAA|ISO9001|ISO14001|OHSAS|GB/T28001"
               r"|体系认证|企业荣誉|守合同重信用|百强企业|类似业绩|项目经验证明|用户评价书|合同复印件"
               # §41：这五类"附件页"是拿来凑页数最顺手的形状，词表必须按交付件里会出现的
               # **页面标题写法**收，而不是只收证书本名。"ISO9001" 抓不到一行标题"ISO证书附件"。
               r"|证书附件|资质附件|业绩附件|ISO证书|认证证书|荣誉证书"
               r"|人员履历|个人简历|履历表|简历表"),
    re.compile(r"评标委员会|中标候选人|资格审查|符合性审查|开标|投标文件的密封|投标无效"),
)
# 与上面四组同序：报告要按"哪一类越界"分开数，不能只报一个总数。
EXCLUDED_SCOPE_GROUP_NAMES = ("PRICING", "COMMERCIAL", "CREDENTIAL", "PROCEDURAL")
EXCLUDED_SCOPE_HITS_MIN = 3
EXCLUDED_SCOPE_TECHNICAL_MAX = 0
EXCLUDED_SCOPE_TABLE_SHARE_MAX = 0.20
# 页级"这是不是技术内容"的信号词。不能直接复用 P0-0 需求分类的 _TECHNICAL_SUBJECT：
# 那里的 "服务" 是技术主题，在这里却会被"代理服务费"命中，把一整页报价/商务页
# 误判成技术页 —— 词表相同不代表口径相同，这里是按整页文本量的判据。
TECHNICAL_PAGE_TERMS = re.compile(
    r"SOP|作业流程|巡检|台账|记录表|责任矩阵|岗位职责|排班|响应时限|应急预案|风险|品质|考核|指标"
    r"|保洁|绿化|消杀|秩序|消防|客服|会务|工程设备|配电|电梯|空调|供冷|弱电|培训|进退场|移交|保养")
# 同样是 r3_page_audit.py:56 的实测线：表格 + 清单占到的可见字比例。
STRUCTURED_PAGE_SHARE_MIN = 0.50


def _norm(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def classify_pages(page_texts: list[str], *, body_start_page: int = 1,
                   structure_by_page: dict[int, dict[str, Any]] | None = None
                   ) -> dict[str, Any]:
    """逐页定类。page_texts 是真实渲染的每页可见文本（顺序即页序）。

    structure_by_page 来自 table_qa 的逐页版面特征（页号 → table_chars/list_chars）。
    传了才能把技术页切成结构化页与叙述页，并按回放尺子复核越界页的"表格占比 ≤ 0.20"
    这一维；没传就整页只按文本判，并在 `layout_basis` 里如实写明是 NO_LAYOUT_FEATURES。
    """
    classes: list[str] = []
    scope_group_hits: list[tuple[int, list[int]]] = []
    features = {int(key): value for key, value in (structure_by_page or {}).items()}
    for index, raw in enumerate(page_texts or [], start=1):
        text = _norm(raw)
        visible = len(text)
        if index < max(1, int(body_start_page or 1)):
            classes.append("FRONT_MATTER")
            continue
        if visible < MIN_PAGE_CONTENT_CHARS:
            # 整页几乎无字：多余换页符、表格溢出只剩边框，都不算内容页。
            classes.append("BLANK")
            continue
        row = features.get(index) or {}
        table_share = int(row.get("table_chars") or 0) / visible if visible else 0.0
        layout_share = ((int(row.get("table_chars") or 0) + int(row.get("list_chars") or 0)) / visible
                        if visible else 0.0)
        placeholder_share = (sum(len(_norm(match.group(0)))
                                 for match in PLACEHOLDER_TOKENS.finditer(text)) / visible
                             if visible else 0.0)
        if visible >= PLACEHOLDER_PAGE_CHARS_MIN and (
                placeholder_share >= PLACEHOLDER_DOMINANT_SHARE_MIN
                or (features and placeholder_share >= PLACEHOLDER_SHARE_MIN
                    and table_share >= PLACEHOLDER_TABLE_SHARE_MIN)):
            classes.append("PLACEHOLDER")
            continue
        hits_per_group = [len(pattern.findall(text)) for pattern in EXCLUDED_SCOPE_PATTERNS]
        if (sum(hits_per_group) >= EXCLUDED_SCOPE_HITS_MIN
                and len(TECHNICAL_PAGE_TERMS.findall(text)) <= EXCLUDED_SCOPE_TECHNICAL_MAX
                and table_share <= EXCLUDED_SCOPE_TABLE_SHARE_MAX):
            classes.append("EXCLUDED_SCOPE")
            scope_group_hits.append((index, hits_per_group))
            continue
        classes.append("STRUCTURED_TECHNICAL" if features and layout_share >= STRUCTURED_PAGE_SHARE_MIN
                       else "NARRATIVE_TECHNICAL")
    char_counts = [len(_norm(raw)) for raw in (page_texts or [])]
    counts = {name: sum(value == name for value in classes) for name in PAGE_CLASSES}
    total = len(classes)
    technical = counts["NARRATIVE_TECHNICAL"] + counts["STRUCTURED_TECHNICAL"]
    technical_chars = sum(size for name, size in zip(classes, char_counts) if name in TECHNICAL_CLASSES)
    # §41 的两层口径：present 只说"这页提到过资信词"，count 才是指令禁的那个形状 ——
    # 资信词在一页里占多数。Run C 404 页实测 present=1 / count=0：那一页讲的是
    # "证明材料索引只登记材料种类与所在文件位置"，命中的词是"合同复印件"。
    # 只报 present 会把一句提及说成"生成了一页资信附件"，只报 count 又丢了点名，所以两个都留，
    # 并把页号与命中分布抄在旁边，结论要能被逐页核。
    credential_pages = [{"page": page, "credential_hits": hits[2],
                         "other_group_hits": sum(hits) - hits[2],
                         "dominant": hits[2] >= sum(hits) - hits[2]}
                        for page, hits in scope_group_hits if hits[2]]
    return {
        "schema_version": PAGE_CALIBRATION_VERSION,
        "physical_pages": total,
        "body_start_page": max(1, int(body_start_page or 1)),
        "page_classes": classes,
        "page_char_counts": char_counts,
        "counts": counts,
        "layout_basis": "MEASURED_LAYOUT_FEATURES" if features else "NO_LAYOUT_FEATURES",
        # 只有整页文本时看不出"这一页是不是整页待填写表格"，所以文本口径下的技术有效页
        # 只会**偏高不会偏低**（Run C 实测：文本 400 页 / 带版面特征 392 页）。
        # 门控与报告必须写明这个方向，否则"没超页"会被当成"确实没超页"。
        "technical_page_count_bound": "MEASURED_WITH_LAYOUT" if features else "UPPER_BOUND_TEXT_ONLY",
        "technical_effective_pages": technical,
        "visible_chars": sum(char_counts),
        "technical_chars": technical_chars,
        "front_matter_pages": counts["FRONT_MATTER"],
        "blank_pages": counts["BLANK"],
        "placeholder_pages": counts["PLACEHOLDER"],
        "excluded_scope_pages": counts["EXCLUDED_SCOPE"],
        # §41：越界页必须说清"越的是哪一界"。只报一个总数时，"2 页报价条款"和"2 页证书附件"
        # 长得一样，而 §41 禁的正是后者被拿来凑页数。分组数只统计**已被判为越界**的页：
        # 一页顺带提到"资质"但仍全是技术口径的页不进这里，那是 P0-0 需求分类管的层次。
        "excluded_scope_group_hits": {name: sum(row[1][index] for row in scope_group_hits)
                                      for index, name in enumerate(EXCLUDED_SCOPE_GROUP_NAMES)},
        "credential_attachment_pages_present": len(credential_pages),
        "credential_attachment_pages": sum(1 for row in credential_pages if row["dominant"]),
        "credential_attachment_page_details": credential_pages,
        "structured_technical_pages": counts["STRUCTURED_TECHNICAL"],
        "narrative_technical_pages": counts["NARRATIVE_TECHNICAL"],
        # §12：占位页占比按物理页算，分母含全部页，避免"只按内容页算"把问题摊薄。
        "placeholder_page_ratio": round(counts["PLACEHOLDER"] / total, 4) if total else 0.0,
        "non_technical_page_ratio": round((total - technical) / total, 4) if total else 0.0,
    }


def calibrate_ruler(classification: dict[str, Any], *, visible_chars: int,
                    technical_chars: int | None = None) -> dict[str, Any]:
    """从真实渲染反标每技术页字符密度。"""
    pages = int((classification or {}).get("technical_effective_pages") or 0)
    chars = int(technical_chars if technical_chars is not None else visible_chars or 0)
    measured = round(chars / pages, 1) if pages and chars else None
    return {
        "schema_version": PAGE_CALIBRATION_VERSION,
        "basis": "MEASURED" if measured else "ESTIMATED",
        "chars_per_technical_page": measured or TEXT_CHARS_PER_PAGE,
        "fallback_chars_per_technical_page": TEXT_CHARS_PER_PAGE,
        "technical_effective_pages": pages,
        "technical_chars": chars,
        "visible_chars": int(visible_chars or 0),
    }


def layout_uplift_pages(structure: dict[str, Any] | None) -> float:
    """版面增量当量（页）：与 document_qa 的估算器共用同一组系数（每个对象折算多少页）。"""
    layout = structure or {}
    return round(sum(
        int(layout.get(count) or 0) * STRUCTURED_LAYOUT_UPLIFT[key]
        for count, key in (("tables", "table"), ("processes", "process"), ("checklists", "checklist"),
                           ("forms", "form"), ("matrices", "matrix"), ("list_blocks", "list"),
                           ("callouts", "callout"))
    ), 2)


def split_technical_pages(ruler: dict[str, Any], *, structured_chars: int = 0) -> dict[str, Any]:
    """按字符归属把技术有效页切成叙述页 / 结构化页。

    逐页文本看不出"这页是不是表格"，但每个 Content Unit 的字数与原型看得见，
    所以切分依据是"结构化原型贡献了多少字"，而不是再猜版面。
    """
    technical = int(ruler.get("technical_effective_pages") or 0)
    per_page = float(ruler.get("chars_per_technical_page") or TEXT_CHARS_PER_PAGE) or float(TEXT_CHARS_PER_PAGE)
    structured = min(technical, max(0, int(round(int(structured_chars or 0) / per_page))))
    return {
        "structured_technical_pages": structured,
        "narrative_technical_pages": technical - structured,
    }


def project_pages(effective_chars: int, *, ruler: dict[str, Any] | None = None,
                  structure: dict[str, Any] | None = None) -> dict[str, Any]:
    """投影技术有效页。

    实测尺子下密度已含表格留白，不再叠加版面增量；
    没有实测时退回 document_qa 的口径（900 字/页 + 版面增量），并把 basis 写成 ESTIMATED。
    """
    active = ruler or {}
    basis = str(active.get("basis") or "ESTIMATED")
    chars = max(0, int(effective_chars or 0))
    per_page = float(active.get("chars_per_technical_page") or TEXT_CHARS_PER_PAGE) or float(TEXT_CHARS_PER_PAGE)
    narrative = chars / per_page
    uplift = 0.0 if basis == "MEASURED" else layout_uplift_pages(structure)
    return {
        "basis": basis,
        "chars_per_technical_page": round(per_page, 1),
        "layout_uplift_pages": uplift,
        "projected_technical_effective_pages": int(round(narrative + uplift)),
        "projected_narrative_pages": int(round(narrative)),
    }


def page_report(classification: dict[str, Any] | None, *, effective_chars: int = 0,
                structure: dict[str, Any] | None = None,
                structured_chars: int = 0) -> dict[str, Any]:
    """§13：一次运行必须同时报出这几类页数，不得只报"404 页"。

    有真实渲染逐页文本时，各类页数是数出来的（basis=MEASURED）；没有渲染时只能按
    document_qa 的 900 字/页投影（basis=ESTIMATED），两者不得混着报。
    """
    active = classification or {}
    # 磁盘上的初稿分类把六类页数记在 `page_classes`（classify_pages 的内存结果里那是逐页清单）。
    counts = dict(active.get("counts") or active.get("page_classes") or {})
    pages = int(active.get("technical_effective_pages") or 0)
    technical_chars = int(active.get("technical_chars") or 0)
    measured = bool(pages and technical_chars)
    ruler = calibrate_ruler(
        active,
        visible_chars=int(active.get("visible_chars") or 0) or int(effective_chars or 0),
        technical_chars=technical_chars if pages else None,
    )
    projection = project_pages(int(effective_chars or 0), ruler=ruler, structure=structure)
    technical_pages = pages if measured else projection["projected_technical_effective_pages"]
    if str(active.get("layout_basis") or "") == "MEASURED_LAYOUT_FEATURES":
        # 版面特征在手：叙述页/结构化页是逐页看出来的，不再按字符归属投影。
        split = {"narrative_technical_pages": int(active.get("narrative_technical_pages") or 0),
                 "structured_technical_pages": int(active.get("structured_technical_pages") or 0)}
        split_basis = "MEASURED_PAGE_LAYOUT"
    else:
        split = split_technical_pages(
            {"technical_effective_pages": technical_pages,
             "chars_per_technical_page": ruler["chars_per_technical_page"]},
            structured_chars=structured_chars)
        split_basis = "CHAR_ATTRIBUTION"
    return {
        "schema_version": PAGE_CALIBRATION_VERSION,
        "measured": measured,
        "basis": ruler["basis"],
        "page_split_basis": split_basis,
        "physical_pages": int(active.get("physical_pages") or 0),
        "technical_effective_pages": technical_pages,
        "narrative_technical_pages": split["narrative_technical_pages"],
        "structured_technical_pages": split["structured_technical_pages"],
        "placeholder_pages": int(counts.get("PLACEHOLDER") or 0),
        "placeholder_page_ratio": float(active.get("placeholder_page_ratio") or 0.0),
        "front_matter_pages": int(counts.get("FRONT_MATTER") or 0),
        "blank_pages": int(counts.get("BLANK") or 0),
        "excluded_scope_pages": int(counts.get("EXCLUDED_SCOPE") or 0),
        # §41：报告必须说得出"越的是哪一界"，尤其资信附件页是不是被拿来凑了页数。
        "excluded_scope_group_hits": dict(active.get("excluded_scope_group_hits") or {}),
        "credential_attachment_pages_present": int(active.get("credential_attachment_pages_present") or 0),
        "credential_attachment_pages": int(active.get("credential_attachment_pages") or 0),
        "credential_attachment_page_details": list(active.get("credential_attachment_page_details") or []),
        "non_technical_page_ratio": float(active.get("non_technical_page_ratio") or 0.0),
        # 文本口径的技术有效页是上界（看不见整页待填写表格），报告必须带上这个方向。
        "technical_page_count_bound": str(active.get("technical_page_count_bound") or "UNKNOWN"),
        "chars_per_technical_page": ruler["chars_per_technical_page"],
        "effective_chars": int(effective_chars or 0),
    }
