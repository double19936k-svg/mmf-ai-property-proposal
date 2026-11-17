from html.parser import HTMLParser
from pathlib import Path

def test_tender_account_controls_are_outside_all_switchable_stage_sections():
    class Tree(HTMLParser):
        def __init__(self):super().__init__();self.stack=[];self.parents={}
        def handle_starttag(self,tag,attrs):
            attrs=dict(attrs);key=attrs.get('id')
            if key:self.parents[key]=list(self.stack)
            if tag not in {'input','meta','link','img','br','hr','source','wbr'}:self.stack.append((tag,key,attrs.get('class','')))
        def handle_endtag(self,tag):
            for i in range(len(self.stack)-1,-1,-1):
                if self.stack[i][0]==tag:self.stack=self.stack[:i];break
    root=Path(__file__).parents[1];tree=Tree();tree.feed((root/'static/index.html').read_text(encoding='utf-8'))
    parents=tree.parents['account-agent-tender-slot'];ids=[p[1] for p in parents]
    assert 'view-tender' in ids
    assert not any(k in ids for k in ['tender-extraction-step','tender-confirm-step','tender-upload-step'])

def test_account_hint_names_the_visible_progress_region_not_removed_cards():
    import account_tasks as a
    from unittest.mock import patch
    with patch.object(a,'list_tasks',return_value=[{'state':'needs_attention','task_id':'r-tender-CHK-1'}]):
        result=a.recognition_handling('r',{'provider_name':'qoder_account'})
    assert '账号任务进度与处理' in result['message'] and '上方' not in result['message']
