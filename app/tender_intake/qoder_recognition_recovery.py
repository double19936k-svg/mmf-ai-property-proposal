"""Bounded Qoder recognition for dense paragraphs and verified legacy replies."""
import json
import math
from pathlib import Path


def split_dense_paragraphs(chunk):
    blocks = chunk['blocks']
    if len(blocks) <= 300 or any(block.get('kind') == 'table' for block in blocks):
        return [chunk]
    count = math.ceil(len(blocks) / 220)
    size = math.ceil(len(blocks) / count)
    groups = [blocks[start:start + size] for start in range(0, len(blocks), size)]
    return [
        {'chunk_id': f"{chunk['chunk_id']}-S{index:03d}",
         'parent_chunk_id': chunk['chunk_id'], 'blocks': group,
         'char_count': sum(len(block['text']) for block in group)}
        for index, group in enumerate(groups, 1)
    ]


def approved_reply_split(call_dir: Path, task_id: str) -> bool:
    """A local recovery approval must match the original returned receipt."""
    try:
        approval = json.loads((call_dir / 'qoder_incomplete_reply_recovery.json').read_text(encoding='utf-8'))
        raw = json.loads((call_dir / 'qoder_raw_answer.json').read_text(encoding='utf-8'))
        identity = json.loads((call_dir / 'qoder_request_identity.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    digest = approval.get('original_digest')
    return bool(
        approval.get('reason') == 'verified_incomplete_terminal_reply' and digest
        and task_id == identity.get('task_id') == approval.get('task_id')
        and digest == raw.get('digest') == identity.get('digest')
        and raw.get('event') == 'result' and raw.get('stop_reason') == 'end_turn'
        and raw.get('is_error') is False
    )
