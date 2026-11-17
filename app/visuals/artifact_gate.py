"""Measure visible DOCX drawings against an external frozen rendering register."""
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile
from lxml import etree
from .ir import validate, digest

NS = {'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
      'a':'http://schemas.openxmlformats.org/drawingml/2006/main',
      'wp':'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing',
      'r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
      'p':'http://schemas.openxmlformats.org/package/2006/relationships'}

def evaluate_visual_artifact(path, run_dir=None):
    findings = []
    def fail(issue):
        findings.append({'rule_id':'FINAL_VISUAL_INTEGRITY','path':str(path),'severity':'BLOCK','issue':issue})
    with ZipFile(path) as package:
        body = etree.fromstring(package.read('word/document.xml'))
        visible = body.xpath('//w:drawing', namespaces=NS)
        relations = etree.fromstring(package.read('word/_rels/document.xml.rels'))
        rels = {r.get('Id'):r for r in relations}
        registry = Path(run_dir)/'visual_render_registry.json' if run_dir is not None else None
        if registry is None or not registry.is_file():
            if visible:
                fail('visible drawing has no external source/render registration')
            return {'status':'BLOCK' if findings else 'NOT_MEASURED', 'findings':findings,
                    'visible_drawings':len(visible), 'business_acceptance':'NOT_GRANTED'}
        registered = json.loads(registry.read_text(encoding='utf-8-sig')).get('visuals', [])
        if len(visible) != len(registered):
            fail('visible drawing count differs from source/render registration')
        irs = []
        for rel in relations:
            if rel.get('Type','').endswith('/customXml') and rel.get('TargetMode') != 'External':
                from posixpath import normpath
                name = normpath('word/' + rel.get('Target',''))
                if name in package.namelist():
                    xml = etree.fromstring(package.read(name))
                    if xml.tag == '{urn:mmf:visual:v1}visual':
                        irs.append(json.loads(xml.text))
        if len(irs) != len(registered):
            fail('reachable visual IR count differs from registration')
        for index, record in enumerate(registered):
            try:
                ir = record['ir']
                validate(ir, source_root=run_dir)
                if digest(ir) != record['ir_digest']:
                    raise ValueError('registered IR digest differs')
                if ir.get('relationships') and not ir.get('relationship_contract'):
                    raise ValueError('directional visual requires a separately frozen relationship contract')
                # Recreate expected pixels from validated semantics. A rewritten
                # registry digest cannot authorize a substituted/blank image.
                from io import BytesIO
                from .scene import compile_scene
                from .docx_renderer import render_png
                font = Path(record['font_path'])
                if hashlib.sha256(font.read_bytes()).hexdigest() != record['font_sha256']:
                    raise ValueError('render font identity changed')
                stream = BytesIO()
                render_png(compile_scene(ir), stream, font)
                expected_media = hashlib.sha256(stream.getvalue()).hexdigest()
                if expected_media != record['media_sha256']:
                    raise ValueError('registered media cannot be reproduced from source semantics')
                matching = [x for x in irs if x.get('visual_id') == ir['visual_id']]
                if len(matching) != 1 or matching[0] != ir:
                    raise ValueError('actual embedded IR is missing, replaced or duplicated')
                drawing = visible[index]
                props = drawing.xpath('.//wp:docPr', namespaces=NS)
                if len(props)!=1 or props[0].get('name') != 'MMF_VISUAL:' + ir['visual_id']:
                    raise ValueError('visible drawing identity differs from registered visual')
                blips = drawing.xpath('.//a:blip', namespaces=NS)
                if len(blips)!=1:
                    raise ValueError('visible visual must reference one rendered image')
                rel = rels[blips[0].get('{'+NS['r']+'}embed')]
                if rel.get('TargetMode') == 'External' or not rel.get('Type','').endswith('/image'):
                    raise ValueError('visual image relationship is external or invalid')
                from posixpath import normpath
                name = normpath('word/' + rel.get('Target',''))
                image = package.read(name)
                if hashlib.sha256(image).hexdigest() != record['media_sha256']:
                    raise ValueError('visible image bytes differ from source-bound render')
            except (KeyError, IndexError, ValueError, OSError) as exc:
                fail(str(exc))
        return {'status':'BLOCK' if findings else 'PASS', 'findings':findings,
                'visible_drawings':len(visible), 'registered_visuals':len(registered),
                'business_acceptance':'NOT_GRANTED'}
