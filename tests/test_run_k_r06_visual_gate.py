import copy, hashlib, json
from zipfile import ZipFile
from pathlib import Path
from lxml import etree
from docx import Document
import pytest
from test_run_j_visuals import ir
from visuals.ir import validate, digest, VisualContractError
from visuals.docx_renderer import add_visual
from visuals.artifact_gate import evaluate_visual_artifact, NS

def registered(ir, tmp_path):
    snapshot={'scope':'GENERATED_PROPOSED_METHOD', 'nodes':{n['id']:n['label'] for n in ir['nodes']},
              'relationships':[{k:r[k] for k in ('id','from','to','relation')} for r in ir['relationships']],
              'evidence':ir['evidence']}
    p=tmp_path/'relations.json';p.write_text(json.dumps(snapshot,ensure_ascii=False),encoding='utf-8')
    ir['relationship_contract']={'file':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
                                 'snapshot':snapshot,'snapshot_digest':digest(snapshot)}
    doc=Document();record=add_visual(doc,ir,tmp_path/'assets',Path('C:/Windows/Fonts/msyh.ttc'))
    target=tmp_path/'good.docx';doc.save(target)
    reg=tmp_path/'visual_render_registry.json'
    reg.write_text(json.dumps({'visuals':[record]},ensure_ascii=False),encoding='utf-8')
    return target,record,reg

def mutate_zip(source,target,fn):
    with ZipFile(source) as z:parts={n:z.read(n) for n in z.namelist()}
    fn(parts)
    with ZipFile(target,'w') as z:
        for n,data in parts.items():z.writestr(n,data)

def test_source_bound_visible_visual_passes_without_granting_business_approval(ir,tmp_path):
    target,_,_=registered(ir,tmp_path)
    result=evaluate_visual_artifact(target,tmp_path)
    assert result['status']=='PASS' and result['business_acceptance']=='NOT_GRANTED'

@pytest.mark.parametrize('mutation',['remove_drawing','changed_pixels','foreign_pixels_and_forged_hash',
                                   'delete_ir','source_changed','ir_changed','no_registry'])
def test_actual_drawing_mutations_are_blocked_even_after_rehash(ir,tmp_path,mutation):
    target,record,reg=registered(ir,tmp_path);bad=tmp_path/'bad.docx'
    def alter(parts):
        if mutation=='remove_drawing':
            xml=etree.fromstring(parts['word/document.xml'])
            for d in xml.xpath('//w:drawing',namespaces=NS):d.getparent().remove(d)
            parts['word/document.xml']=etree.tostring(xml)
        elif mutation in ('changed_pixels','foreign_pixels_and_forged_hash'):
            n=next(n for n in parts if n.startswith('word/media/'))
            parts[n]=parts[n][:-9]+b'different'
            if mutation=='foreign_pixels_and_forged_hash':
                record['media_sha256']=hashlib.sha256(parts[n]).hexdigest()
                reg.write_text(json.dumps({'visuals':[record]},ensure_ascii=False),encoding='utf-8')
        elif mutation=='delete_ir':
            n=next(n for n in parts if n.startswith('customXml/visual'))
            del parts[n]
        elif mutation=='ir_changed':
            n=next(n for n in parts if n.startswith('customXml/visual'))
            parts[n]=parts[n].replace('受理登记'.encode(),'错误来源'.encode())
    mutate_zip(target,bad,alter)
    if mutation=='source_changed':(tmp_path/'source.json').write_text('{}')
    if mutation=='no_registry':reg.unlink()
    result=evaluate_visual_artifact(bad,tmp_path)
    assert result['status']=='BLOCK' and result['findings']

def test_direction_change_cannot_be_authorized_by_reviewed_projection_boolean(ir,tmp_path):
    registered(ir,tmp_path)
    changed=copy.deepcopy(ir)
    for r in changed['relationships']:r['from'],r['to']=r['to'],r['from']
    with pytest.raises(VisualContractError):validate(changed,tmp_path)

def test_missing_contract_and_self_rewritten_contract_are_blocked(ir,tmp_path):
    target,record,reg=registered(ir,tmp_path)
    record['ir']['relationship_contract']['snapshot']['relationships'][0]['to']='n2'
    record['ir']['relationship_contract']['snapshot_digest']=digest(record['ir']['relationship_contract']['snapshot'])
    record['ir_digest']=digest(record['ir'])
    reg.write_text(json.dumps({'visuals':[record]},ensure_ascii=False),encoding='utf-8')
    assert evaluate_visual_artifact(target,tmp_path)['status']=='BLOCK'
