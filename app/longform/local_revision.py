"""Source-bound local DOCX revisions. Never overwrite the source artifact."""
from __future__ import annotations
import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from zipfile import ZipFile
from docx import Document
from filesystem_paths import filesystem_path

class RevisionError(ValueError):
    pass

def require(ok, message):
    if not ok:
        raise RevisionError(message)

def sha(path):
    return hashlib.sha256(filesystem_path(path).read_bytes()).hexdigest()

def canonical_path(path):
    value = str(Path(path).resolve())
    if os.name == "nt" and value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif os.name == "nt" and value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)

def pointer(value, path):
    for part in path.strip("/").split("/") if path else []:
        part = part.replace("~1","/").replace("~0","~")
        value = value[int(part)] if isinstance(value,list) else value[part]
    return value

def source_evidence(run_dir, ref):
    root=canonical_path(run_dir)
    path=canonical_path(root/ref["file"])
    require(path.is_relative_to(root),"Revision source escapes run")
    require(filesystem_path(path).is_file() and sha(path)==ref["sha256"],"Revision source identity changed")
    raw=json.loads(filesystem_path(path).read_text(encoding="utf-8-sig"))
    value=pointer(raw,ref["pointer"])
    require(isinstance(value,str) and ref.get("quote") and ref["quote"] in value,
            "Revision source quote missing or changed")

def create_docx_revision(source, target, manifest, *, run_dir, font_path=None):
    source,target=canonical_path(source),canonical_path(target)
    root=canonical_path(run_dir)
    require(target.is_relative_to(root),"Revision output escapes run")
    require(target!=source and not filesystem_path(target).exists(),"Revision target must be new")
    original_sha=sha(source)
    require(manifest.get("schema_version")=="source-bound-docx-revision-v1","Unknown revision contract")
    require(manifest.get("source_artifact_sha256")==original_sha,"Revision artifact identity changed")
    require(bool(manifest.get("operations")),"Empty revision refused")
    doc=Document(filesystem_path(source))
    # Resolve all original anchors before inserting blocks shifts paragraph indices.
    bindings=[]
    for op in manifest["operations"]:
        match=re.fullmatch(r"paragraph\[(\d+)\]",op["anchor"])
        cell_match=re.fullmatch(r'table\[(\d+)\]/row\[(\d+)\]/cell\[(\d+)\]',op['anchor'])
        table_match=re.fullmatch(r'table\[(\d+)\]',op['anchor'])
        require(match is not None or cell_match is not None or table_match is not None,"Revision requires an exact block anchor")
        if table_match:
            ti=int(table_match.group(1));require(ti<len(doc.tables),'Revision table anchor missing')
            anchor=doc.tables[ti]
            anchor_text='\n'.join(' | '.join(c.text for c in r.cells) for r in anchor.rows)
            require(op['kind']=='table_notes','Table anchor requires notes operation')
        elif cell_match:
            ti,ri,ci=map(int,cell_match.groups())
            require(ti<len(doc.tables) and ri<len(doc.tables[ti].rows) and ci<len(doc.tables[ti].rows[ri].cells),
                    'Revision cell anchor missing')
            anchor=doc.tables[ti].cell(ri,ci)
            anchor_text=anchor.text
            require(op['kind']=='replace_cell','Cell anchor requires a cell replacement')
        else:
            index=int(match.group(1))
            require(index<len(doc.paragraphs),"Revision anchor missing")
            anchor=doc.paragraphs[index]
            anchor_text=anchor.text
        require(hashlib.sha256(anchor_text.encode()).hexdigest()==op["anchor_text_sha256"],
                "Revision anchor text changed")
        require(op.get("source_evidence"),"Revision source evidence required")
        for ref in op["source_evidence"]:
            source_evidence(root,ref)
        if op['kind']=='replace_cell':
            from governance.table_fidelity import matrix, normalize
            from governance.table_qa import form_template_table
            from governance.artifact_qa import apply_artifact_repairs
            other=[c.text for c in doc.tables[ti].rows[ri].cells]
            candidates=[]
            for evidence in op['source_evidence']:
                row=json.loads(filesystem_path(root/evidence['file']).read_text(encoding='utf-8-sig'))
                payload=row.get('payload') if isinstance(row,dict) else None
                if not isinstance(payload,dict) or not row.get('content_unit_id'):continue
                expected=matrix(form_template_table(payload) if row.get('archetype')=='FORM_TEMPLATE' else payload)
                possible=expected[:1] if ri==0 else expected[1:]
                for source_row in possible:
                    if len(source_row)!=len(other):continue
                    choices=(str(source_row[ci]),apply_artifact_repairs({'text':str(source_row[ci])})['text'])
                    if op.get('new') not in choices:continue
                    stable=sum(bool(normalize(v)) and normalize(v) in {
                        normalize(source_row[index]),
                        normalize(apply_artifact_repairs({'text':str(source_row[index])})['text'])}
                        for index,v in enumerate(other) if index!=ci)
                    if stable>=2:candidates.append((row['content_unit_id'],source_row))
            unique={(cu,json.dumps(values,ensure_ascii=False)) for cu,values in candidates}
            require(len(unique)==1,'Cell restoration source mapping is ambiguous or has fewer than two stable peers')
        require(op["kind"] in {"replace_text","replace_cell","paragraph_style","move_paragraph","table_notes","paragraphs_after","table_after","visual_after","visual_replace"},
                "Unsupported revision operation")
        destination=None
        if op['kind']=='move_paragraph':
            dest=re.fullmatch(r'paragraph\[(\d+)\]',op.get('before_anchor',''))
            require(dest is not None and int(dest.group(1))<len(doc.paragraphs), 'Move destination missing')
            destination=doc.paragraphs[int(dest.group(1))]
            require(destination is not anchor and hashlib.sha256(destination.text.encode()).hexdigest()==op.get('before_anchor_text_sha256'),
                    'Move destination identity changed')
        bindings.append((op,anchor,destination))
    audit=[]
    visual_registry=[]
    for op,anchor,destination in bindings:
        cursor=anchor._p if hasattr(anchor,'_p') else None
        if op['kind']=='table_notes':
            require(op.get('scope')=='PROPOSED_RECORD_POLICY_NOT_CONFIRMED_FACT', 'Table notes require explicit proposed-method scope')
            require(op.get('notes') and not re.search(r'\d+\s*(?:天|年|月|小时)',op['notes']),
                    'Record notes may not invent a numeric retention period')
            p=doc.add_paragraph(op['notes'])
            anchor._tbl.addnext(p._p)
        elif op['kind']=='replace_cell':
            from governance.artifact_qa import apply_artifact_repairs
            permitted = [apply_artifact_repairs({'text':ref['quote']})['text'] for ref in op['source_evidence']]
            require(op.get('new') in permitted or op.get('new') in [r['quote'] for r in op['source_evidence']],
                    'Cell restoration must equal its source or deterministic customer projection')
            p=anchor.paragraphs[0]
            style=copy.deepcopy(p.runs[0]._r.rPr) if p.runs and p.runs[0]._r.rPr is not None else None
            anchor.text=op['new']
            if style is not None:
                anchor.paragraphs[0].runs[0]._r.insert(0,style)
        elif op['kind'] in {'paragraph_style','move_paragraph'}:
            require(op.get('style') in {'Heading 3','Normal','List Number'}, 'Unsupported presentation style')
            anchor.style=op['style']
            if op.get('new') is not None:
                anchor.text=op['new']
            anchor.paragraph_format.keep_with_next=op.get('keep_with_next',False)
            if op['kind']=='move_paragraph':
                destination._p.addprevious(anchor._p)
        elif op["kind"]=="replace_text":
            old,new=op["old"],op["new"]
            require(old and anchor.text.count(old)==1,"Revision replacement must be unique")
            style=copy.deepcopy(anchor.runs[0]._r.rPr) if anchor.runs and anchor.runs[0]._r.rPr is not None else None
            anchor.text=anchor.text.replace(old,new,1)
            if style is not None:
                anchor.runs[0]._r.insert(0,style)
        elif op["kind"]=="paragraphs_after":
            require(op.get("paragraphs") and all(isinstance(t,str) and t.strip() for t in op["paragraphs"]),
                    "Empty revision paragraph")
            for text in op["paragraphs"]:
                p=doc.add_paragraph(text,style=op.get("style") or anchor.style)
                cursor.addnext(p._p);cursor=p._p
        elif op["kind"]=="table_after":
            from build_docx import add_section_tables
            before=len(doc.tables)
            plan=op["table"]
            require(plan.get("columns") and plan.get("rows"),"Empty revision table")
            require(all(len(r)==len(plan["columns"]) for r in plan["rows"]),"Revision table shape invalid")
            elements=set(doc.element.body)
            governance = add_section_tables(doc,[plan],section_id=op["section_id"])
            require(len(doc.tables)==before+1,"Revision table was not rendered")
            new=[e for e in doc.element.body if e not in elements]
            for element in new:
                cursor.addnext(element);cursor=element
        else:
            from visuals.ir import validate
            from visuals.docx_renderer import add_visual
            require(font_path is not None,"Revision visual requires explicit Chinese font")
            validate(op["visual"],source_root=root)
            elements=set(doc.element.body)
            if op['kind']=='visual_replace':
                require(bool(anchor._p.xpath('.//w:drawing')), 'Visual replacement anchor has no drawing')
                from lxml import etree
                for rid,rel in list(doc.part.rels.items()):
                    if rel.reltype.endswith('/customXml') and not rel.is_external:
                        xml=etree.fromstring(rel.target_part.blob)
                        if xml.tag=='{urn:mmf:visual:v1}visual' and xml.get('id')==op['visual']['visual_id']:
                            doc.part.drop_rel(rid)
            visual_registry.append(add_visual(doc,op["visual"],filesystem_path(target.parent/"visual_assets"),font_path))
            new=[e for e in doc.element.body if e not in elements]
            for element in new:
                cursor.addnext(element);cursor=element
            if op['kind']=='visual_replace':
                anchor._p.getparent().remove(anchor._p)
        audit.append({"kind":op["kind"],"anchor":op["anchor"],"anchor_text_sha256":op["anchor_text_sha256"],
                      "source_evidence":op["source_evidence"]})
        for key in ('new','old','before_anchor','before_anchor_text_sha256','style','notes','scope'):
            if key in op:
                audit[-1][key]=op[key]
        if op["kind"] == "table_after":
            audit[-1]["table_governance"] = governance
        if op['kind'] in {'visual_after','visual_replace'}:
            audit[-1]['visual']=op['visual']
    filesystem_path(target.parent).mkdir(parents=True,exist_ok=True)
    name=None
    try:
        with tempfile.NamedTemporaryFile(dir=filesystem_path(target.parent),suffix=".docx",delete=False) as temp:
            name=Path(temp.name)
        doc.save(filesystem_path(name))
        with ZipFile(filesystem_path(name)) as package:
            require(package.testzip() is None and "word/document.xml" in package.namelist(),"Invalid revised DOCX")
        require(sha(source)==original_sha,"Source artifact changed during revision")
        # Exclusive publish refuses concurrent overwrite; source and staging remain separate.
        with filesystem_path(target).open("xb") as stream, filesystem_path(name).open("rb") as staged:
            import shutil
            shutil.copyfileobj(staged,stream)
        result={"schema_version":"source-bound-docx-revision-result-v1",
                "status":"LOCAL_REVISION_CREATED_NOT_CERTIFIED","source_artifact":str(source),
                "source_artifact_sha256":original_sha,"artifact_path":str(target),"artifact_sha256":sha(target),
                "operations":audit,"provider_calls":0,"fresh_generation":False,"page_band":"NOT_MEASURED",
                "full_visual_review":"PENDING","final_gate":"NOT_GRANTED"}
        filesystem_path(target.with_suffix(".revision.json")).write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        if visual_registry:
            filesystem_path(root/'visual_render_registry.json').write_text(json.dumps(
                {'schema_version':'visual-render-register-v1','visuals':visual_registry},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        return result
    finally:
        if name is not None:
            filesystem_path(name).unlink(missing_ok=True)
