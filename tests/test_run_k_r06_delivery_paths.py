from pathlib import Path
import run_layout
from filesystem_paths import filesystem_path as fs

def test_deep_provider_copy_preserves_bytes(tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'result.json').write_bytes(b'{"value":1}')
    target=tmp_path/('x'*65)/('y'*65)/'provider_result'/'provider_recognition'
    run_layout._copy_if_exists(source,target)
    assert fs(target/'result.json').read_bytes()==b'{"value":1}'

def test_finalize_does_not_copy_its_archive_into_itself(tmp_path,monkeypatch):
    from types import SimpleNamespace
    run=tmp_path/'runs'/'r';run.mkdir(parents=True)
    source=run/'provider_recognition';source.mkdir();(source/'answer.json').write_text('{}')
    monkeypatch.setattr(run_layout.paths,'current',lambda:SimpleNamespace(runs_dir=run.parent,output_root=tmp_path/'output'))
    import longform.run_state
    monkeypatch.setattr(longform.run_state,'finalize_delivery_state',lambda *a,**kw:{'derived':{
        'delivery_status':'NEEDS_REVIEW','public_phase':'COMPLETED_WITH_WARNINGS','internal_phase':'COMPLETED'}})
    run_layout.finalize_run('r')
    run_layout.finalize_run('r')
    assert fs(run/'provider_result/provider_recognition/answer.json').is_file()
    assert not fs(run/'provider_result/provider_result').exists()
