"""One local final editorial pass; no generation and no claim/score authority.

Source facts are dereferenced from frozen extraction inputs. Deduplication only
removes exact propositions within the same section and preserves a locator to
the surviving proposition. Paraphrases remain for human review.
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from lxml import etree

from .client_facing_prose import client_facing_prose

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NS = {"w": W[1:-1]}
PLACEHOLDER = "按现场条件配置的人员"
NUMERIC = re.compile(r"\d+(?:\.\d+)?\s*(?:人|名|类岗位|平方米|㎡|小时|分钟|次|天|日|年|%|％)")
_METRIC_INDICATORS = ("合格率", "办结率", "满意率", "及时率", "返修率", "值守", "供应", "通知", "容纳")
_OBJECT_STOP = {
    "服务", "管理", "人员", "岗位", "配置", "项目", "工作", "要求", "进行", "以及", "相关",
    "小时", "分钟", "以内", "以上", "不少于", "不低于", "不超过", "通知", "内通知",
}
CLEAR_META = re.compile(r"本单元|patrol_record|共\s*\d+\s*个字段|写作自检|删除一切无凭据|评分/算术|生成管线|内容生产说明")
FILL = re.compile(r"【(?:待[^】]{0,100}|[^】]*填写[^】]*)】|\[(?:待[^\]]+)\]")


class SourceFacts:
    def __init__(self, extraction: dict[str, Any] | None = None):
        self.rows: list[dict[str, Any]] = []
        self.staff: dict[str, int] = {}
        self.staff_source: dict[str, Any] | None = None
        self.canonical_area: dict[str, Any] | None = None
        self.source_conflicts: list[dict[str, Any]] = []
        for row in (extraction or {}).get("paragraphs", []):
            self.rows.append({"locator": row.get("para_id"), "text": str(row.get("text") or ""),
                              "file_id": row.get("file_id"), "heading_path": row.get("heading_path")})
        for table in (extraction or {}).get("tables", []):
            columns = table.get("columns") or []
            for index, row in enumerate(table.get("rows") or []):
                self.rows.append({"locator": f"{table.get('table_id')}.row[{index}]",
                                  "file_id": table.get("file_id"), "text": " | ".join(map(str, row))})
            if columns and "岗位" in str(columns[0]) and any("人" in str(col) for col in columns[1:]):
                roles = {}
                total = None
                for row in table.get("rows") or []:
                    if len(row) < 2 or not str(row[1]).strip().isdigit():
                        continue
                    role = re.sub(r"\s+", "", str(row[0]))
                    if re.search(r"合计|总计", role):
                        total = int(row[1])
                    else:
                        roles[role] = int(row[1])
                if roles and total == sum(roles.values()):
                    self.staff = roles
                    self.staff_source = {"locator": table.get("table_id"), "file_id": table.get("file_id"),
                                         "total": total, "roles": roles}
                    for role, number in roles.items():
                        self.rows.append({"locator": table.get("table_id"), "text": f"{role}{number}人"})
                    self.rows.append({"locator": table.get("table_id"),
                                      "text": f"服务人员合计{total}人，{len(roles)}类岗位"})
        for row in list(self.rows):
            numbers = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
                       '六': 6, '七': 7, '八': 8, '九': 9, '十': 10, '二十四': 24}
            def numeric_alias(match):
                value = numbers.get(match.group(1))
                return f"{value}{match.group(2)}" if value is not None else match.group()
            alias = re.sub(r"([一二两三四五六七八九十]+)个?(小时|分钟|人|名|次|天|日|年)", numeric_alias, row['text'])
            if alias != row['text']:
                self.rows.append({**row, 'text': alias, 'original_source_text': row['text'],
                                  'derivation': 'CHINESE_NUMERAL_LITERAL_EQUIVALENCE'})
        areas = []
        for row in self.rows:
            match = re.search(r"建筑面积[：:\s]*(\d+(?:\.\d+)?)\s*(平方米|㎡)", row['text'])
            if match:
                areas.append({**row, 'value': match.group(1), 'unit': match.group(2)})
        technical = [row for row in areas if any('技术需求' in heading for heading in row.get('heading_path') or [])]
        if len({row['value'] for row in technical}) == 1:
            self.canonical_area = technical[0]
            if any(row['value'] != self.canonical_area['value'] for row in areas):
                self.source_conflicts.append({'object': '建筑面积', 'selected_technical_basis': self.canonical_area,
                                              'source_values': areas, 'requires_confirmation': True})

    @classmethod
    def from_run(cls, run_dir: Path) -> "SourceFacts":
        run_dir = Path(run_dir)
        path = run_dir / "tender" / "extraction.json"
        if not path.is_file():
            return cls()
        extraction = json.loads(path.read_text(encoding="utf-8"))
        verified = set()
        for entry in extraction.get("files") or []:
            relative = Path(str(entry.get("stored_relative_path") or ""))
            candidates = [run_dir / "tender" / "originals" / relative.name,
                          run_dir / "tender" / "originals" / str(entry.get("original_filename") or "")]
            for candidate in candidates:
                if candidate.is_file() and hashlib.sha256(candidate.read_bytes()).hexdigest() == entry.get("sha256"):
                    verified.add(entry.get("file_id"))
                    break
        extraction = {**extraction, **{key: [r for r in extraction.get(key, []) if r.get("file_id") in verified]
                                      for key in ("paragraphs", "tables")}}
        return cls(extraction)

    def claim_source(self) -> dict[str, Any]:
        return {"verified_tender_facts": self.rows}

    def _whole_metric(self, token: str, source: str) -> bool:
        compact = re.sub(r"\s+", "", token)
        match = re.match(r"(\d+(?:\.\d+)?)(.*)$", compact)
        if not match or not match.group(2):
            return False
        return re.search(
            rf"(?<![\d.]){re.escape(match.group(1))}(?!\.\d)\s*{re.escape(match.group(2))}",
            source,
        ) is not None

    def _object_grams(self, text: str) -> set[str]:
        chars = re.findall(r"[\u4e00-\u9fa5]", str(text or ""))
        return {"".join(chars[index:index + 2]) for index in range(len(chars) - 1)} - _OBJECT_STOP

    def _best_metric_row(self, token: str, context: str) -> dict[str, Any] | None:
        """Return the source clause that matches this metric, not the first number hit."""
        claim = self._object_grams(context)
        if not claim:
            return None
        claim_indicators = {name for name in _METRIC_INDICATORS if name in context}
        qualifier = re.search(r"不少于|不低于|不超过|不大于|不小于|以内|以下|低于时整改", context)
        best: dict[str, Any] | None = None
        best_score = -1
        for row in self.rows:
            source = str(row.get("text") or "")
            if not self._whole_metric(token, source):
                continue
            if qualifier and qualifier.group(0) not in source:
                continue
            source_qualifier = re.search(r"不少于|不低于|不超过|不大于|不小于|以内|以下|低于时整改", source)
            if source_qualifier and source_qualifier.group(0) not in context:
                continue
            source_indicators = {name for name in _METRIC_INDICATORS if name in source}
            if claim_indicators and claim_indicators.isdisjoint(source_indicators):
                continue
            overlap = claim & self._object_grams(source)
            if not overlap:
                continue
            score = len(overlap) + 3 * len(claim_indicators & source_indicators)
            if score > best_score:
                best, best_score = row, score
        return best

    def supports_metric(self, token: str, context: str) -> bool:
        """A metric is sourced only when number, unit, object and qualifier agree."""
        return self._best_metric_row(token, context) is not None

    def _verbatim_metric(self, token: str, text: str, lo: int, hi: int) -> dict[str, Any] | None:
        window = text[max(0, lo - 24):min(len(text), hi + 16)]
        row = self._best_metric_row(token, window)
        if row is None:
            return None
        if re.search(r"[人名]$", token) and re.search(r"参演|演练|培训|学员|外援|新增|增派|另设|另增|额外", window):
            return None
        if re.search(r"[人名]$", token):
            clause = text[max(0, lo - 48):min(len(text), hi + 36)]
            stop = {"服务", "管理", "人员", "岗位", "配置", "项目", "工作", "要求", "进行", "以及", "相关"}

            def grams(value: str) -> set[str]:
                chars = re.findall(r"[\u4e00-\u9fa5]", value)
                return {"".join(chars[i:i + 2]) for i in range(len(chars) - 1)} - stop

            if not any(grams(clause) & grams(row["text"]) for row in [row]):
                return None
        row = self._best_metric_row(token, window)
        qualifier = re.search(r"(不少于|不低于|不超过|不大于|不小于|以内|以下|低于时整改)", row["text"])
        value = (qualifier.group(1) if qualifier and qualifier.group(1) not in token else "") + token
        return {"locator": row["locator"], "value": value, "source_text": row["text"], "derivation": "VERBATIM_TENDER_METRIC"}

    def numeric_binding(self, text: str, lo: int, hi: int) -> dict[str, Any] | None:
        token = re.sub(r"\s+", "", text[lo:hi])
        # Staff binding includes the role/total patient. A number found elsewhere
        # in a source cannot support an unrelated proposal's headcount.
        if re.search(r"[人名]$", token):
            number = float(re.match(r"\d+(?:\.\d+)?", token).group())
            prefix = text[max(0, lo - 20):lo]
            prefix = re.sub(r"[（(][^）)]{0,20}[）)]$|线$", "", prefix)
            # Preserve the verified role through ordinary staffing grammar;
            # unrelated attendance/resource counts cannot borrow this source.
            prefix = re.sub(r"(?:岗位)?(?:配置|编制|定员|人数)(?:为|：|:)?\s*$", "", prefix)
            prefix = re.sub(r"岗位\s*$", "", prefix)
            if self.staff_source:
                for role, count in self.staff.items():
                    if prefix.endswith(role) and count == number:
                        return {**self.staff_source, "value": token, "object": role}
                    # A duty allocation can use a subset of the sourced role
                    # capacity. It is not evidence of existing staff or extra
                    # capacity, and cannot exceed the verified headcount.
                    clause = (re.split(r"[。；;，,、]", text[max(0, lo - 35):lo])[-1]
                              + token + re.split(r"[。；;，,、]", text[hi:hi + 24])[0])
                    tail = text[hi:hi + 12]
                    if (prefix.endswith(role) and number.is_integer() and 1 <= number <= count
                            and re.match(r"(?:当班|每班|负责|承担)", tail)
                            and not re.search(r"现有|已配|目前|确保|保证|新增|增派|岗位配置|编制|定员", clause)):
                        return {**self.staff_source, "value": token, "object": role,
                                "derivation": "PROPOSED_ROLE_ALLOCATION_WITHIN_SOURCE_CAPACITY",
                                "source_role_capacity": count}
                if number == self.staff_source["total"] and re.search(r"(?:合计|共|编制|服务人员|投入人员)\s*$", prefix):
                    return {**self.staff_source, "value": token, "object": "staff_total"}
                nearby = re.split(r'[。；;\n]', text[max(0, lo - 100):lo])[-1]
                tail = text[hi:hi + 18]
                claim_tail = re.split(r'[。；;，,\n]', tail)[0]
                unrelated = re.search(r"参演|演练人数|培训人数|培训班|学员|外援|新增|增派|另设|另增|额外|现有|已配", nearby + claim_tail)
                if not unrelated:
                    # A verified staffing total can be introduced by the number
                    # of sourced role categories, rather than by 'total'. Keep
                    # the category count bound too; equal numbers alone are not
                    # authority for attendance or an unrelated resource pool.
                    staffing_clause = nearby + token + claim_tail
                    categories = re.findall(r'(\d+)类岗位', staffing_clause)
                    if (number == self.staff_source['total']
                            and any(int(category) == len(self.staff) for category in categories)
                            and re.search(r'到岗|组织|执行|编制|配置|定员|落实|(?:小)?团队', staffing_clause)):
                        return {**self.staff_source, 'value': token, 'object': 'staff_total'}
                    if (number == self.staff_source['total']
                            and re.search(r'(?:人员配置|人员编制|岗位编制|最低配置|人员总数)(?:为|是|上|：|:)?\s*$', nearby)):
                        return {**self.staff_source, 'value': token, 'object': 'staff_total'}
                    if number == self.staff_source['total'] and re.match(r"(?:的)?(?:配置|编制|定员)", tail):
                        return {**self.staff_source, 'value': token, 'object': 'staff_total'}
                    matches = [(m.end(), role) for role in self.staff for m in re.finditer(re.escape(role), nearby)]
                    if matches:
                        role = max(matches)[1]
                        if self.staff[role] == number and (re.search(r"岗位|值守|力量|配置|分配|模式", nearby) or re.match(r"(?:的)?(?:配置|编制|定员)|力量|按.{0,10}分配", tail)):
                            return {**self.staff_source, 'value': token, 'object': role}
                    # Operational allocation can refer back to the paragraph's
                    # sourced role across several sentences. An exact unique
                    # role capacity plus an explicit allocation is required;
                    # exercise attendance or additional resources cannot borrow it.
                    paragraph = re.split(r'\n\s*\n', text[:lo])[-1]
                    candidates = [role for role,count in self.staff.items()
                                  if count == number and role in paragraph]
                    if (len(candidates) == 1
                            and re.search(r'(?:组织上)?(?:把|将)\s*$', nearby)
                            and re.match(r'(?:分为|分成|分配至|分配到)', tail)
                            and not re.search(r'演练|参演|培训|学员|外援|新增|增派|另设|额外|现有|已配', paragraph + claim_tail)):
                        return {**self.staff_source, 'value': token, 'object': candidates[0],
                                'derivation':'PROPOSED_SOURCE_ROLE_ALLOCATION_IN_SAME_PARAGRAPH'}
            verbatim = self._verbatim_metric(token, text, lo, hi)
            if verbatim:
                return verbatim
            return None
        # For other numeric facts require an exact source phrase, plus a shared
        # semantic noun in its immediate clause (not numeric co-occurrence alone).
        clause = re.split(r"[。；;，,]", text[max(0, lo - 40):min(len(text), hi + 35)])
        context = " ".join(clause)
        row = self._best_metric_row(token, context)
        if row:
            qualifier = re.search(r"(不少于|不低于|不超过|不大于|不小于|以内|以下|低于时整改)", row["text"])
            value = (qualifier.group(1) if qualifier and qualifier.group(1) not in token else "") + token
            return {"locator": row["locator"], "value": value, "source_text": row["text"]}
        return self._verbatim_metric(token, text, lo, hi)

    def protect(self, text: str) -> tuple[str, dict[str, str]]:
        saved = {}
        for match in reversed(list(NUMERIC.finditer(text))):
            if self.numeric_binding(text, *match.span()):
                key = f"\ue100{chr(0xe200 + len(saved))}\ue101"
                saved[key] = match.group()
                text = text[:match.start()] + key + text[match.end():]
        return text, saved


def restore_protected(text: str, saved: dict[str, str]) -> str:
    for key, value in saved.items():
        text = text.replace(key, value)
    return text


def repair_with_source_facts(value, repair, facts):
    """Protect located numeric facts before any of the shared repair stages."""
    from .customer_hygiene import is_structural_path
    saved = {}
    def protect(item):
        if isinstance(item, dict):
            return {k: v if is_structural_path(str(k)) else protect(v) for k,v in item.items()}
        if isinstance(item, list):
            return [protect(v) for v in item]
        if isinstance(item, str) and facts is not None:
            text, local = facts.protect(item)
            for key, literal in local.items():
                unique = '\ue300' + chr(0xe500 + len(saved)) + '\ue301'
                text = text.replace(key, unique)
                saved[unique] = literal
            return text
        return item
    def restore(item):
        if isinstance(item, dict):
            return {k: restore(v) for k,v in item.items()}
        if isinstance(item, list):
            return [restore(v) for v in item]
        return restore_protected(item, saved) if isinstance(item, str) else item
    return restore(repair(protect(value)))


def _restore_staff(text: str, facts: SourceFacts, bindings: list[dict[str, Any]]) -> str:
    def replace(match):
        prefix = text[max(0, match.start() - 35):match.start()]
        prefix = re.sub(r"[（(][^）)]{0,20}[）)]$", "", prefix)
        prefix = re.sub(r"线$", "", prefix)
        # An exercise's attendance is not the project's entire staffing count.
        role = next((role for role in sorted(facts.staff, key=len, reverse=True) if prefix.endswith(role)), None)
        if role is None:
            aliases = {"保洁": "室内保洁", "秩序": "秩序维护", "前台接待": "前台",
                       "项目负责人": "项目主管"}
            role = next((canonical for alias, canonical in aliases.items()
                         if prefix.endswith(alias) and canonical in facts.staff), None)
        subject_context = re.split(r"[。；;]", text[max(0, match.start() - 90):match.start()])[-1]
        total_context = re.search(r"(?:合计|总计|总数|共|人员编制|服务人员|投入人员|类岗位|人员构成|配置为)\s*$", prefix)
        if total_context and not re.search(r"服务人员|人员编制|人员配置|岗位|编制|定员|投入人员|项目主管|项目负责人|工程|秩序维护|前台|室内保洁|厨师|面点师|打荷", subject_context):
            total_context = False
        if re.search(r"参演|演练人数|培训人数", subject_context):
            total_context = False
        if re.match(r"(?:编制|定员|在册范围|的岗位构成)", text[match.end():]):
            total_context = True
        kitchen_subject = (prefix.endswith(("后厨", "食堂餐饮", "食堂")) or
                           (prefix.endswith("合计") and ("后厨" in subject_context or
                            all(r in subject_context for r in ("厨师", "面点师", "打荷")))))
        kitchen = kitchen_subject and all(r in facts.staff for r in ("厨师", "面点师", "打荷"))
        if kitchen:
            number = sum(facts.staff[r] for r in ("厨师", "面点师", "打荷"))
            bindings.append({**facts.staff_source, "object": "后厨三岗位合计", "value": f"{number}人", "derivation": "厨师+面点师+打荷"})
            return f"{number}人"
        if facts.staff_source and (role or total_context):
            number = facts.staff[role] if role else facts.staff_source["total"]
            bindings.append({**facts.staff_source, "object": role or "staff_total", "value": f"{number}人"})
            return f"{number}人"
        if re.search(r"(约|办公人员|就餐|常驻)\s*$", text[max(0, match.start() - 12):match.start()]):
            return "数量待核实"
        return "待核实"
    text = re.sub(re.escape(PLACEHOLDER), replace, text)
    if facts.staff_source:
        # Explicit role-count descriptions of this staff list, not arbitrary
        # functional teams (four security duties remain four duties).
        text = re.sub(r"\d+类岗位(?=" + str(facts.staff_source['total']) + r"人)", f"{len(facts.staff)}类岗位", text)
    text = re.sub(r"合计经确认的岗位人员", "合计人数以经确认的人员配置表为准", text)
    text = re.sub(r"共经确认的岗位人员", "人员范围以经确认的人员配置表为准", text)
    text = text.replace("按经确认的岗位人员岗位口径", "按经确认的岗位编制口径")
    return text


def edit_text(text: str, facts: SourceFacts | None = None) -> tuple[str, dict[str, Any]]:
    facts = facts or SourceFacts()
    before = str(text)
    bindings: list[dict[str, Any]] = []
    text = _restore_staff(before, facts, bindings)
    if facts.canonical_area:
        # Technical body uses the technical requirement quantity. Conflicting
        # contract-template quantities remain recorded in the human input index;
        # quotes/comparisons are not overwritten and the source is never edited.
        selected = facts.canonical_area['value']
        for conflict in facts.source_conflicts:
            for value in conflict['source_values']:
                if value['value'] != selected:
                    pattern = re.escape(value['value']) + r"(?=\s*(?:平方米|㎡))"
                    def area_projection(match):
                        left = max(text.rfind('。', 0, match.start()), text.rfind(';', 0, match.start())) + 1
                        right = text.find('。', match.end())
                        context = text[left:right if right >= 0 else len(text)]
                        # Project only an unqualified core-area assertion. Any
                        # source attribution, comparison or dependent clause is
                        # preserved; a whitelist of comparison phrases is unsafe.
                        direct = re.fullmatch(
                            r'\s*(?:本项目|项目|物业服务|管理|服务|总建筑|建筑|办公用房建筑)'
                            r'面积\s*(?:为|是|约|[:：])?\s*' + pattern + r'\s*(?:平方米|㎡)\s*', context)
                        source_mentions = sum(len(re.findall(r'(?<![\d.])' + re.escape(v['value']) + r'(?![\d.])', text))
                                              for v in conflict['source_values'])
                        if not direct or source_mentions > 1:
                            return match.group()
                        bindings.append({'object': '建筑面积', 'value': selected,
                                         'source_locator': facts.canonical_area['locator'],
                                         'basis': 'TECHNICAL_REQUIREMENT_BODY_QUANTITY', 'source_conflict': conflict})
                        return selected
                    text = re.sub(pattern, area_projection, text)
    text = text.replace("patrol_record", "巡逻记录")
    text = re.sub(r"本单元(?:规定|明确|给出|约定|采用|按)?", "", text)
    text = re.sub(r"(?:共\s*\d+\s*个字段)[；;，,]?", "", text)
    text = text.replace("其余字段：", "补充记录项：")
    # Pure authoring instructions are removed clause by clause. Mixed business
    # clauses retain their original actor/action and never gain new obligations.
    text = re.sub(r"[^。；;]*删除一切无凭据[^。；;]*[。；;]?", "", text)
    text = re.sub(r"[^。；;]*(?:写作自检|评分/算术元语言|内部内容生产说明)[^。；;]*[。；;]?", "", text)
    text = re.sub(r"第第(?=[一二三四五六七八九十\d]+步)", "第", text)
    text = re.sub(r"(?<![按])按按(?=[^按])", "按", text)
    text = re.sub(r"与与", "与", text)
    if "四问" in text:
        questions = re.search(r"((?:是否[^。]+[；;]){1,5}是否[^。]+。)四问", text)
        if questions:
            count = questions.group(1).count("是否")
            if count == 3:
                text = text[:questions.end(1)] + text[questions.end(1):].replace("四问", "三问", 1)
    protected, saved = facts.protect(text)
    protected, prose_actions = client_facing_prose(protected)
    text = restore_protected(protected, saved)
    text = re.sub(r"[。；;][；;]+", "。", text)
    text = re.sub(r"[，,；;]+(?=[。])", "", text)
    text = re.sub(r"^[，,；;。]+|[，,；;]+$", "", text.strip())
    return text, {"changed": text != before, "before": before, "after": text,
                  "numeric_bindings": bindings, "prose_actions": prose_actions}


def _resolve_inline_confirmation(text: str) -> str:
    """Move authoring anchors to the index without leaving blanks or inventing facts.

    A pending verification boundary is explicit. Grammar-bearing slots retain
    their input role; proof availability is never turned into a service promise.
    """
    if not FILL.search(text):
        return text
    text = re.sub(r'以(?:一处|一个)?行内提示(?:登记|标注)[：:]?', '', text)
    text = re.sub(r'(?:只)?登记为(?:一处|一个)?行内提示', '相关证明资料', text)
    # A nominal topic followed by an anchor needs a predicate, not a dangling
    # noun plus an unrelated paragraph-end disclaimer.
    text = re.sub(r'(事实|资料|证明|持证情况|达标情况|材料)[，,](?=【待|\[待)', r'\1', text)
    def replace(match):
        prefix = text[:match.start()]
        suffix = text[match.end():]
        if re.search(r'(?:按|依据|根据|凭|以)$', prefix):
            return '核实结果'
        if not prefix or prefix.endswith(('。', '；', ';', '，', ',', '：', ':', ' ')):
            ending = '。' if suffix[:1].isspace() else ''
            return '相关证明资料尚待核实' + ending
        if suffix.startswith(('由', '、')):
            return '（相关证明资料尚待核实）'
        if re.search(r'(?:提供|签发|归档|提交|登记)$', prefix):
            return '，相关证明资料尚待核实'
        return '尚待核实'
    value = FILL.sub(replace, text)
    value = re.sub(r'([，,；;])\s*[，,；;]+', r'\1', value)
    value = re.sub(r'[，,；;：:]\s*(?=[。])', '', value)
    value = re.sub(r'^[，,；;。\s]+', '', value).strip()
    value = re.sub(r'[。]\s*(?=[，,])', '', value)
    if not re.search(r'待核实|待确认', value):
        value = value.rstrip('。') + '。相关证明资料尚待核实。'
    return value


def _norm(text: str) -> str:
    return re.sub(r"[\s，,。；;：:‘’“\"”]", "", text)


def _self_contained_clause(text: str) -> bool:
    # Dependent fragments cannot be removed merely because their wording recurs
    # under two different subjects, triggers or output records.
    if re.match(r"^(?:经|由|与|并|即|一律|按|纳入|保存至|期满|覆盖|标注|如|若|当|确认|判断|把|坚持|改判|登记)", text):
        return False
    return bool(len(_norm(text)) >= 14 and re.search(r"^.{2,30}(?:为|以|按|在|向|从|同属|含|写明|承担|覆盖|完成|要求|进入|须|应|不得|登记|复核|签认|处置|分派|可用|予以|补正|穿工作服)", text))


def _replace_xml_text(paragraph, new: str) -> None:
    """Apply minimal edits to text runs, preserving fields, drawings and bookmarks."""
    nodes = paragraph.findall(".//" + W + "t")
    old = "".join(node.text or "" for node in nodes)
    positions = []
    for node in nodes:
        positions.extend((node, index) for index in range(len(node.text or "")))
    for tag, lo, hi, a, b in reversed(SequenceMatcher(None, old, new, autojunk=False).get_opcodes()):
        if tag == "equal":
            continue
        if lo < len(positions):
            first, offset = positions[lo]
        elif positions:
            first, offset = positions[-1][0], len(positions[-1][0].text or "")
        else:
            run = etree.SubElement(paragraph, W + "r")
            first = etree.SubElement(run, W + "t")
            offset = 0
        affected = {}
        for node, index in positions[lo:hi]:
            affected.setdefault(node, []).append(index)
        for node, indices in affected.items():
            current = node.text or ""
            node.text = current[:min(indices)] + current[max(indices) + 1:]
        current = first.text or ""
        first.text = current[:offset] + new[a:b] + current[offset:]
        first.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def _metrics(body) -> dict[str, int]:
    texts = [node.text or "" for node in body.findall(".//" + W + "t")]
    return {"paragraphs": len(body.findall(W + "p")), "tables": len(body.findall(W + "tbl")),
            "effective_chars": len(re.sub(r"\s+", "", "".join(texts)))}


def _editorial_content_hash(body) -> str:
    texts = []
    for p in body.findall('.//' + W + 'p'):
        if p.findall('.//' + W + 'fldChar') or p.findall('.//' + W + 'instrText') or p.findall('.//' + W + 'hyperlink'):
            continue
        texts.append(''.join(p.xpath('.//w:t/text()', namespaces=NS)))
    return hashlib.sha256('\n'.join(texts).encode('utf-8')).hexdigest()


def run_final_editorial(path: Path, *, facts: SourceFacts, output_dir: Path,
                        section_titles: dict[str, str] | None = None) -> dict[str, Any]:
    path, output_dir = Path(path), Path(output_dir)
    original = path.read_bytes()
    with zipfile.ZipFile(path) as archive:
        parts = {entry.filename: (entry, archive.read(entry.filename)) for entry in archive.infolist()}
    xml = etree.fromstring(parts["word/document.xml"][1])
    body = xml.find(W + "body")
    source_identity = hashlib.sha256(json.dumps(facts.rows, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    previous_report = output_dir / 'final_editorial_report.json'
    if previous_report.is_file():
        previous = json.loads(previous_report.read_text(encoding='utf-8'))
        if source_identity == previous.get('source_identity') and _editorial_content_hash(body) == previous.get('output_content_hash'):
            return {**previous, 'idempotent_reuse': True, 'changes': [],
                    'before': previous['after'], 'duplicate_clusters': [], 'duplicate_cluster_count': 0,
                    'deleted_chars': 0, 'merged_chars': 0}
    before = _metrics(body)
    titles = {re.sub(r"^\d+(?:\.\d+)*\s*", "", title): sid for sid, title in (section_titles or {}).items()}
    section, section_title = "FRONT", "前部"
    changes, clusters, human = [], [], []
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    citations = Counter()
    table_count = 0
    form_tables = 0
    table_label = ""
    def add_human(locator, field, source_type, hint="", table=None):
        category = "QUALIFICATION" if re.search(r"姓名|证书|资历|经历|资格", field) else "BUSINESS" if re.search(r"价格|报价|企业|财务", field) else "TECHNICAL"
        human.append({"id": f"H-{len(human) + 1:03d}", "section_id": section,
                      "section_title": section_title, "locator": locator, "page_locator": None,
                      "required_field": field, "source_type": source_type, "category": category,
                      "required_before_submission": True, "suggested_owner": "投标负责人" if category != "TECHNICAL" else "项目专业负责人",
                      "status": "UNFILLED", "hint": hint, "table": table})
    for index, child in enumerate(list(body)):
        is_table = child.tag == W + "tbl"
        if child.tag not in {W + "p", W + "tbl"}:
            continue
        if is_table:
            locator = f"table[{table_count}]"
            table_count += 1
            rows = child.findall(W + "tr")
            headers = ["".join(c.xpath('.//w:t/text()', namespaces=NS)) for c in rows[0].findall(W + "tc")] if rows else []
            form = "待投标人填写" in table_label
            if form:
                form_tables += 1
                for ri, row in enumerate(rows[1:], 1):
                    cells = row.findall(W + "tc")
                    values = ["".join(cell.xpath('.//w:t/text()', namespaces=NS)).strip() for cell in cells]
                    if headers and "关键字段" in headers[0] and values and values[0]:
                        add_human(f"{locator}.row[{ri}]", values[0], "OPERATIONAL_FORM_FIELD",
                                  hint=" | ".join(values[1:]), table=table_label)
                        human[-1]["required_before_submission"] = False
                        human[-1]["status"] = "OPERATIONAL_TEMPLATE_UNFILLED"
                    for ci, cell in enumerate(row.findall(W + "tc")):
                        cell_text = "".join(cell.xpath('.//w:t/text()', namespaces=NS)).strip()
                        if not cell_text or re.fullmatch(r"[_—\s]+|待填写|待确认", cell_text):
                            add_human(f"{locator}.row[{ri}].cell[{ci}]", headers[ci] if ci < len(headers) else f"字段{ci + 1}", "FORM_FIELD", table=table_label)
            paragraphs = child.findall(".//" + W + "p")
            table_label = ""  # A following content table is not the prior form.
        else:
            locator = f"body[{index}]"
            paragraphs = [child]
            style = child.find("./" + W + "pPr/" + W + "pStyle")
            heading = style is not None and re.search(r"Heading|标题", str(style.get(W + "val")), re.I)
            if child.findall(".//" + W + "fldChar") or child.findall(".//" + W + "instrText") or child.findall(".//" + W + "hyperlink"):
                continue  # Native TOC fields and links are owned by pagination.
            text = "".join(child.xpath('.//w:t/text()', namespaces=NS))
            if heading:
                section_title = text
                stripped = re.sub(r"^\d+(?:\.\d+)*\s*", "", text)
                section = titles.get(stripped, stripped)
            table_label = text if re.search(r"表|台账|清单|填写", text) and len(text) < 100 else table_label
        for pi, paragraph in enumerate(paragraphs):
            text = "".join(paragraph.xpath('.//w:t/text()', namespaces=NS))
            if not text.strip():
                continue
            loc = f"{locator}.p[{pi}]"
            for match in FILL.finditer(text):
                if match.group() != "【待投标人填写】":
                    add_human(loc, match.group().strip("【】[]"), "BIDDER_OR_SITE_CONFIRMATION")
            if "待投标人填写" in text:
                # The title remains a comprehensible business table title; filling
                # instructions belong only in the sidecar.
                add_human(loc, table_label, "FORM_COMPLETION_INSTRUCTION", hint=text)
            prepared = _resolve_inline_confirmation(re.sub(r'【待投标人填写】', '', text).strip())
            preserve_table_context = False
            if is_table:
                from .table_context import is_business_header, negative_columns
                cell = paragraph.getparent()
                while cell is not None and cell.tag != W + 'tc':
                    cell = cell.getparent()
                row = cell.getparent() if cell is not None else None
                if row is not None and row.tag == W + 'tr' and row in rows:
                    cells = row.findall(W + 'tc')
                    ci = cells.index(cell)
                    preserve_table_context = ((row is rows[0] and is_business_header(prepared))
                                              or (row is not rows[0] and ci in negative_columns(headers)))
            if preserve_table_context:
                edited, audit = prepared, {'numeric_bindings': [], 'prose_actions': [],
                                           'basis': 'PRESERVE_NATIVE_TABLE_LABEL_OR_PROHIBITION_CONTEXT'}
            else:
                edited, audit = edit_text(prepared, facts)
            audit['before'] = text
            audit['changed'] = edited != text
            if (not is_table and heading and not re.fullmatch(r'Heading1|标题1', str(style.get(W + 'val')), re.I)
                    and edited.count('与') > 1):
                # Native TOC entry text owns chapter identity. Rephrasing its
                # heading alone would make the measured outline unresolvable.
                title_parts = edited.split('与')
                edited = '、'.join(title_parts[:-1]) + '与' + title_parts[-1]
            edited = re.sub(r"【待投标人填写】", "", edited).strip()
            edited = _resolve_inline_confirmation(edited)
            # Keep headings, table cells and field-bearing paragraphs intact.
            eligible = (not is_table and not (paragraph.findall('.//' + W + 'bookmarkStart') or
                        paragraph.findall('.//' + W + 'drawing')) and not FILL.search(text)
                        and not (paragraph.find('./' + W + 'pPr/' + W + 'pStyle') is not None and heading))
            if eligible:
                # Sentence identity includes actor, action, boundary, record and
                # numeric details verbatim. No fuzzy semantic deletion.
                sentences = re.findall(r"[^。!?！？；;，,\n]+[。!?！？；;，,]?|\n", edited)
                keep = []
                for sentence in sentences:
                    norm = _norm(sentence)
                    key = (section, norm)
                    if _self_contained_clause(norm) and key in seen:
                        survivor = seen[key]
                        if 'citation' not in survivor:
                            citations[section] += 1
                            survivor['citation'] = f"〔{citations[section]}〕"
                            kept_p = survivor['paragraph']
                            if kept_p is paragraph:
                                keep[survivor['keep_index']] = survivor['citation'] + keep[survivor['keep_index']]
                            else:
                                previous_text = ''.join(kept_p.xpath('.//w:t/text()', namespaces=NS))
                                cited_text = previous_text.replace(survivor['text'], survivor['citation'] + survivor['text'], 1)
                                _replace_xml_text(kept_p, cited_text)
                                changes.append({'locator': survivor['locator'], 'section_id': section,
                                                'before': previous_text, 'after': cited_text,
                                                'changed': True, 'numeric_bindings': [],
                                                'prose_actions': [], 'basis': 'LOCAL_BUSINESS_CROSS_REFERENCE'})
                        reference = f"沿用本节{survivor['citation']}所述要求"
                        punctuation = sentence[-1] if sentence[-1] in '。!?！？；;，,' else ''
                        keep.append(reference + punctuation)
                        clusters.append({"section_id": section, "removed_locator": loc,
                                         "kept_locator": seen[key]["locator"], "removed": sentence,
                                         "kept": seen[key]["text"], "basis": "EXACT_SELF_CONTAINED_PROPOSITION_SAME_SECTION",
                                         "reference": reference, "disposition": "MERGED_BY_EXPLICIT_LOCAL_REFERENCE",
                                         "unique_action_lost": False, "source_fact_lost": False,
                                         "scoring_response_lost": False})
                    else:
                        keep.append(sentence)
                        if _self_contained_clause(norm):
                            seen[key] = {"locator": loc, "text": sentence, 'paragraph': paragraph, 'keep_index': len(keep) - 1}
                edited = "".join(keep).strip()
                edited = re.sub(r"^[，,；;。]+|[，,；;]+$", "", edited)
            if edited != text:
                changes.append({"locator": loc, "section_id": section, **audit, "after": edited})
                if not edited and not is_table and eligible:
                    body.remove(paragraph)
                else:
                    _replace_xml_text(paragraph, edited)
    # Pure orphan headings produced by removal are folded away; headings which
    # precede a real table/list/body remain. No replacement prose is generated.
    children = list(body)
    for index, child in enumerate(children[:-1]):
        style = child.find('./' + W + 'pPr/' + W + 'pStyle')
        following = children[index + 1]
        next_style = following.find('./' + W + 'pPr/' + W + 'pStyle')
        if style is not None and next_style is not None and style.get(W + 'val') == next_style.get(W + 'val') and not child.findall('.//' + W + 'bookmarkStart'):
            # Keep chapter/divider structure. Only explicit unnumbered subheads
            # made empty by this pass can be removed.
            if re.search(r"Heading[3-9]", str(style.get(W + 'val'))):
                body.remove(child)
    after = _metrics(body)
    for conflict in facts.source_conflicts:
        add_human('verified_tender_source', conflict['object'] + '：技术需求书与合同范本数值差异确认',
                  'TENDER_SOURCE_CONFLICT', hint=json.dumps(conflict, ensure_ascii=False))
        human[-1]['section_id'] = next((sid for sid in (section_titles or {}) if not str(sid).startswith('CH')), 'INTRODUCTION')
        human[-1]['section_title'] = (section_titles or {}).get(human[-1]['section_id'], '项目概况')
    final_text = ''.join(xml.xpath('//w:t/text()', namespaces=NS))
    document_bytes = etree.tostring(xml, xml_declaration=True, encoding='UTF-8', standalone=True)
    temp = path.with_suffix('.editorial.tmp')
    with zipfile.ZipFile(temp, 'w') as archive:
        for name, (entry, data) in parts.items():
            archive.writestr(entry, document_bytes if name == 'word/document.xml' else data)
    temp.replace(path)
    output_dir.mkdir(parents=True, exist_ok=True)
    index_report = {"schema_version": "human-input-index-v1", "entries": human,
                    "entry_count": len(human), "unfilled_form_tables": form_tables}
    (output_dir / 'HUMAN_INPUT_INDEX.json').write_text(json.dumps(index_report, ensure_ascii=False, indent=2), encoding='utf-8')
    report = {"schema_version": "final-editorial-v1", "phase": "FINAL_EDITORIAL", "provider_calls": 0,
              "input_sha256": hashlib.sha256(original).hexdigest(), "output_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "source_identity": source_identity, "output_content_hash": _editorial_content_hash(body),
              "before": before, "after": after, "changes": changes, "duplicate_clusters": clusters,
              "duplicate_cluster_count": len(clusters),
              "deleted_chars": sum(len(change['before'][lo:hi]) for change in changes
                                   for tag, lo, hi, _, _ in SequenceMatcher(None, change['before'], change['after'], autojunk=False).get_opcodes()
                                   if tag in {'delete', 'replace'}),
              "merged_chars": sum(len(_norm(c['removed'])) for c in clusters),
              "dedup_net_removed_chars": sum(len(_norm(c['removed'])) - len(c['reference']) for c in clusters) - sum(len(v['citation']) for v in seen.values() if 'citation' in v),
              "placeholder_phrases": final_text.count(PLACEHOLDER),
              "unresolved_inline_placeholders": len(FILL.findall(final_text)) + len(re.findall(r'_{4,}', final_text)),
              "clear_internal_leaks": len(CLEAR_META.findall(final_text)), "human_input_index": index_report,
              "source_fact_catalog": facts.rows}
    (output_dir / 'final_editorial_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def bind_human_input_pages(output_dir: Path, pagination: dict[str, Any]) -> None:
    """Page ranges use the existing measured chapter ruler, never estimated pages."""
    path = Path(output_dir) / 'HUMAN_INPUT_INDEX.json'
    if not path.is_file():
        return
    index = json.loads(path.read_text(encoding='utf-8'))
    # Displayed TOC values include unresolved build-time caches. Aggregate
    # verified counters cannot certify any particular chapter boundary.
    rows = pagination.get('toc_entry_pages') or []
    def measured_page(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
    physical = measured_page(pagination.get('physical_pages'))
    pages = [measured_page(row.get('rendered_page')) for row in rows]
    for entry in index['entries']:
        entry['page_locator'] = None
        entry['page_locator_status'] = 'NOT_MEASURED'
        chapter = re.match(r'(?:S|CH)(\d+)', str(entry['section_id']))
        if not chapter or not physical or not (0 < int(chapter.group(1)) <= len(pages)):
            continue
        pos = int(chapter.group(1)) - 1
        start = pages[pos]
        boundary = pages[pos + 1] if pos + 1 < len(pages) else physical + 1
        previous = [page for page in pages[:pos] if page is not None]
        if start is None or boundary is None or not (1 <= start < boundary <= physical + 1):
            continue
        if previous and start < max(previous):
            continue
        entry['page_locator'] = {'from': start, 'to': boundary - 1,
                                 'basis': 'MEASURED_CHAPTER_RANGE', 'exact_section_locator': entry['section_title']}
        entry['page_locator_status'] = 'MEASURED'
    index['page_locators_measured'] = sum(row['page_locator'] is not None for row in index['entries'])
    index['page_locators_not_measured'] = len(index['entries']) - index['page_locators_measured']
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding='utf-8')
