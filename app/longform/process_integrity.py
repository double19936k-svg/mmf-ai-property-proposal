"""Process QA by evidenced operational facets, not fixed step-title strings."""
import re
from pathlib import Path

FACETS = {
    "trigger_input": r"适用于|受理|发生|发现|接到|出现|启动条件|触发|输入",
    "responsible_actor": r"负责人|责任岗位|专业负责人|客户服务岗位|工程岗位|执行岗|主责岗|报修人|使用人",
    "action": r"登记|处置|处理|维修|修复|分派|派单|隔离|巡查|检查|确认",
    "verification": r"复查|复验|试机|核验|核查|验证|查验|测试|试运行",
    "output_record": r"记录|台账|工单|签认|签收|签字|报告",
    "closure": r"关闭|结案|归档|移交|回访|销项",
}


def process_integrity(text: str) -> dict:
    evidence = {}
    for facet, pattern in FACETS.items():
        match = re.search(pattern, text)
        evidence[facet] = ({"span": list(match.span()),
                            "context": text[max(0, match.start() - 35):match.end() + 70]}
                           if match else None)
    # Actors must govern an action in a clause, and verification/closure must
    # concern a record or a result. A bag of six isolated headings is not a loop.
    actor_action = re.search(r"(?:负责人|责任岗位|专业负责人|客户服务岗位|工程岗位|执行岗|主责岗)[^。\n]{0,65}(?:登记|处置|处理|分派|核查|复核|验证|确认|关闭|签认)", text)
    verified_closure = re.search(r"(?:复查|复验|核验|核查|验证|确认)[^。\n]{0,100}(?:记录|结果|工单|台账|关闭|签认)|(?:记录|结果|工单|台账)[^。\n]{0,100}(?:复查|复验|核验|核查|验证|关闭|签认)", text)
    return {"schema_version": "process-integrity-v1", "facets": evidence,
            "actor_action_binding": actor_action.group() if actor_action else None,
            "verification_closure_binding": verified_closure.group() if verified_closure else None,
            "complete": all(evidence.values()) and bool(actor_action and verified_closure)}


def visual_output_present(fragment: dict, output_name: str) -> bool:
    if not re.search(r"组织图|关系图|关系示意图", output_name):
        return True
    # A table named 'organization diagram' is not visual evidence. Only a real
    # supplied asset or an explicit rendered relationship graph can satisfy it.
    return any(isinstance(block, dict) and block.get("type") in {"image", "diagram"}
               and Path(str(block.get("asset_path") or block.get("rendered_asset") or "")).is_file()
               for block in fragment.get("body_blocks") or [])
