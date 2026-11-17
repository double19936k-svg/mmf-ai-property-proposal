"""Offline editorial replay. Historical inputs are read-only, outputs isolated."""
from pathlib import Path
import argparse
import hashlib
import json
import re
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from governance.final_editorial import SourceFacts, run_final_editorial
from governance.final_artifact_qa import _docx_blocks, evaluate_docx_artifact
from governance.commitment_provenance import evaluate_commitments
from longform.factory import visible_text
from longform.process_integrity import process_integrity, visual_output_present
from longform.provisional_settlement import settle_provisional_section


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run, output = args.run.resolve(), args.output.resolve()
    if output == run or run in output.parents or output in run.parents:
        raise ValueError("Replay output must be independent of the frozen run")
    output.mkdir(parents=True, exist_ok=True)
    original = next(run.glob("*20261001_073852.docx"))
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    target = output / "RUN_G_EDITORIAL_REPLAY.docx"
    shutil.copyfile(original, target)
    facts = SourceFacts.from_run(run)
    payload = json.loads((run / "artifact_content.json").read_text("utf-8"))
    sections = payload["artifact"]["sections"]
    titles = {s.get("section_id") or s.get("id"): s.get("heading") or s.get("title") for s in sections}
    report = run_final_editorial(target, facts=facts, output_dir=output, section_titles=titles)
    before_blocks = _docx_blocks(original)
    source = {**json.loads((run / "brief.json").read_text("utf-8")), **facts.claim_source()}
    after_blocks = _docx_blocks(target)
    adjudications = []
    for index in [26, 174, 249, 323, 325, 326, 388, 448, 660, 992, 997, 1029, 1042, 1043]:
        text = before_blocks[index]["text"]
        result = evaluate_commitments(source, [], {"artifact": {"text": text}})
        adjudications.append({"historical_block_index": index, "original_locator": before_blocks[index]["path"],
                              "text": text, "expected": "RISK_RETAINED" if index in {992, 997} else "NO_HARD",
                              "actual_status": result["status"], "violations": result["violations"],
                              "claims": result["claims"]})
    dump(output / "run_g_14_adjudication.json", adjudications)
    qa = evaluate_docx_artifact(target, canonical_facts=source, brief=source, contracts=[])
    dump(output / "final_artifact_qa_before_pagination.json", qa)
    contracts = json.loads((run / "04_section_contracts.json").read_text("utf-8"))["contracts"]
    contract_by_id = {r["section_id"]: r for r in contracts}
    provisional = []
    # Reuse historical reasons but re-measure only process/visual evidence.
    state = json.loads((run / "run_state.json").read_text("utf-8"))
    names = state.get("facts", {}).get("provisional_sections") or []
    for entry in names:
        sid = entry.get("section_id") if isinstance(entry, dict) else str(entry)
        folder = run / "longform/word/sections" / sid
        if not (folder / "fragment.json").is_file():
            continue
        fragment = json.loads((folder / "fragment.json").read_text("utf-8"))
        gate = json.loads((folder / "governance.json").read_text("utf-8"))
        integrity = process_integrity(visible_text(fragment))
        if integrity["complete"]:
            gate["process_missing"] = []
        contract = contract_by_id[sid]
        visual = [name for name in contract.get("required_outputs") or [] if not visual_output_present(fragment, name)]
        gate["required_outputs_missing"] = sorted(set(gate.get("required_outputs_missing") or []) | set(visual))
        result = settle_provisional_section(sid, gate=gate)
        result.update({"process_integrity": integrity, "visual_required_missing": visual})
        provisional.append(result)
    dump(output / "provisional_replay.json", provisional)
    mechanical = r"第第[一二三四五六七八九十\d]+步|按按|与与|。；|；。|^[，,；;]"
    report["broken_grammar_from_pipeline"] = [r for r in after_blocks if re.search(mechanical, r["text"])]
    report["known_false_hard_blocks"] = sum(r["actual_status"] == "BLOCK" for r in adjudications if r["expected"] == "NO_HARD")
    report["source_numeric_preservation"] = {}
    all_before = "\n".join(b["text"] for b in before_blocks)
    all_after = "\n".join(b["text"] for b in after_blocks)
    for fact in ["27人", "8类岗位", "9163平方米", "青岛市东海西路2号甲", "每两小时", "每月5日"]:
        report["source_numeric_preservation"][fact] = {"before": all_before.count(fact), "after": all_after.count(fact)}
    report["scoring_state_frozen"] = {key: value for key, value in state.get("facts", {}).items() if "scoring" in key}
    report["frozen_original_unchanged"] = hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
    dump(output / "RUN_G_EDITORIAL_REPLAY.json", report)
    print(json.dumps({key: report[key] for key in ["before", "after", "placeholder_phrases", "clear_internal_leaks", "duplicate_cluster_count", "known_false_hard_blocks", "source_numeric_preservation"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
