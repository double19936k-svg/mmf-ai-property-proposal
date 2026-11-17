"""Source-cell scope evidence. No project-specific counts, scores or row IDs."""
from __future__ import annotations
import re
from typing import Any


def scoring_source_scope(item: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    leaf_id = str(item.get('scoring_leaf_id') or '')
    if not leaf_id:
        links = [r for r in graph.get('item_links') or []
                 if r.get('scoring_item_id') == item.get('scoring_item_id')]
        leaf_id = str(links[0]['leaf_id']) if len(links) == 1 else ''
    leaf = next((r for r in graph.get('leaves') or [] if r.get('leaf_id') == leaf_id), {})
    volume = str(leaf.get('volume') or '')
    locator = leaf.get('locator') or {}
    if not locator.get('table_id') or not locator.get('row_index'):
        return {}
    scope = ('PRICING_EXCLUDED' if re.search('报价|价格', volume)
             else 'BIDDER_CREDENTIAL_EXCLUDED' if re.search('商务|资格|资信', volume)
             else '')
    return {'basis': 'SCORING_TABLE_VOLUME_CELL', 'scoring_leaf_id': leaf_id,
            'volume': volume, 'scope_class': scope, 'locator': locator,
            'topic': leaf.get('topic'), 'weight': leaf.get('weight'),
            'rubric': leaf.get('rubric'),
            'protected_evidence_terms': [t for t in ('400号码', '400电话号码', '通话记录', '照片',
                                         '证书', '合同', '业绩', '营业执照', '原件', '现场拨测')
                                         if t in str(item.get('label') or '') + str(leaf.get('rubric') or '')],
            'evidence_scopes': ['BUSINESS', 'QUALIFICATION', 'PROTECTED_EVIDENCE'] if scope else []}
