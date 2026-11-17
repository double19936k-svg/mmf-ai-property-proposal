"""Actual source-bound local revision, including refusal of stale and forged inputs."""
import copy,hashlib,json
from docx import Document
import pytest
from longform.local_revision import create_docx_revision,RevisionError,sha
from filesystem_paths import filesystem_path

def setup_revision(tmp_path):
    original=tmp_path/"original.docx";doc=Document();doc.add_paragraph("人员履约安排");doc.save(original)
    source=tmp_path/"sources.json";source.write_text(json.dumps({"requirement":"按时缴纳社会保险费"},ensure_ascii=False),encoding="utf-8")
    op={"kind":"paragraphs_after","anchor":"paragraph[0]","anchor_text_sha256":hashlib.sha256("人员履约安排".encode()).hexdigest(),
        "paragraphs":["服务期间，我方将按时缴纳社会保险费。"],"source_evidence":[{
        "file":"sources.json","sha256":sha(source),"pointer":"/requirement","quote":"按时缴纳社会保险费"}]}
    manifest={"schema_version":"source-bound-docx-revision-v1","source_artifact_sha256":sha(original),"operations":[op]}
    return original,tmp_path/"revised.docx",manifest

def test_new_revision_preserves_source_and_reads_actual_output(tmp_path):
    original,target,manifest=setup_revision(tmp_path);before=original.read_bytes()
    result=create_docx_revision(original,target,manifest,run_dir=filesystem_path(tmp_path))
    assert original.read_bytes()==before
    assert [p.text for p in Document(target).paragraphs]==["人员履约安排","服务期间，我方将按时缴纳社会保险费。"]
    assert result["provider_calls"]==0 and result["fresh_generation"] is False
    with pytest.raises(RevisionError):
        create_docx_revision(original,target,manifest,run_dir=tmp_path)
    assert target.is_file()

@pytest.mark.parametrize("mutation",["artifact_hash","source_hash","quote","anchor","path_escape","empty_evidence"])
def test_stale_or_unproven_revision_creates_no_artifact(tmp_path,mutation):
    original,target,m=setup_revision(tmp_path);before=original.read_bytes();op=m["operations"][0]
    if mutation=="artifact_hash":m["source_artifact_sha256"]="0"*64
    elif mutation=="source_hash":op["source_evidence"][0]["sha256"]="0"*64
    elif mutation=="quote":op["source_evidence"][0]["quote"]="我方已经完成缴费"
    elif mutation=="anchor":op["anchor_text_sha256"]="0"*64
    elif mutation=="path_escape":op["source_evidence"][0]["file"]="../outside.json"
    else:op["source_evidence"]=[]
    with pytest.raises(RevisionError):
        create_docx_revision(original,target,m,run_dir=tmp_path)
    assert not target.exists() and original.read_bytes()==before

def test_failed_later_operation_does_not_publish_partial_revision(tmp_path):
    original,target,m=setup_revision(tmp_path)
    other=copy.deepcopy(m["operations"][0]);other["kind"]="replace_text"
    other.update(old="不存在的文字",new="假完成");m["operations"].append(other)
    with pytest.raises(RevisionError):
        create_docx_revision(original,target,m,run_dir=tmp_path)
    assert not target.exists()

