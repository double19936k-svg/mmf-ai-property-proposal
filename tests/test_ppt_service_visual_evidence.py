"""PPT evidence stays grounded and survives as editable data in the landed file."""
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from ppt_final_verify import verify_pptx

APP = Path(__file__).resolve().parents[1] / "app"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="PPT renderer requires Node")


def test_source_conflicts_and_unrelated_numbers_do_not_become_charts():
    brief = {
        "requirements": "建筑面积约30000㎡，其中地上面积约28000㎡。旧楼10层。",
        "functional_composition": "建筑面积约35000㎡，其中地上面积约28000㎡。项目拟降低能耗10%。",
    }
    entry = (APP / "ppt_renderer_v2/service_evidence.mjs").as_uri()
    text = subprocess.check_output([NODE, "--input-type=module", "-e",
                                   f"import {{projectEvidence}} from '{entry}';"
                                   f"console.log(JSON.stringify(projectEvidence({json.dumps(brief)})))"],
                                   text=True, encoding="utf-8", timeout=30)
    assert json.loads(text)["charts"] == []


@pytest.mark.skipif(not (APP / "node_modules/@oai/artifact-tool").exists(), reason="renderer runtime missing")
def test_native_chart_and_visible_approval_condition_survive_export(tmp_path):
    version = "profile-p-service-v1"
    overview = {"slide_id":"SL-1","topic_id":"project_overview","layout":"modules","title":"项目规模",
                "modules":[{"title":"规模","body":"项目建筑面积约30000㎡。"},{"title":"分区","body":"地上面积约28000㎡。"}]}
    boundary = {"slide_id":"SL-2","topic_id":"public_interface","layout":"modules","title":"责任界面",
                "modules":[{"title":"分区界面","body":"由双方现场核对后，待甲方确认后执行。"},
                           {"title":"技术资料","body":"由专业单位核实技术资料。"}]}
    artifact = {"title":"园区服务","profile_p_contract":version,"slides":[overview,boundary],
                "output_contract":{"min_total_pages":1,"min_visual_page_ratio":0.30}}
    content = tmp_path/"input.json"
    content.write_text(json.dumps({"brief":{"project_name":"示例园区","requirements":
        "建筑面积约30000㎡，其中地上面积约28000㎡。"},"artifact":artifact},ensure_ascii=False), encoding="utf-8")
    result = tmp_path/"result.pptx"
    completed = subprocess.run([NODE,str(APP/"build_ppt.mjs"),str(content),str(result),str(tmp_path/"qa")],
        env={**os.environ,"RUNTIME_NODE_MODULES":str(APP/"node_modules"),"MMF_PPT_PYTHON":sys.executable},
        capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=90)
    assert completed.returncode==0, completed.stderr
    report=verify_pptx(result,artifact)
    assert report["status"]=="PASS", report["findings"]
    assert report["editable_chart_count"]==1 and len(report["business_diagram_pages"])==1
    assert not report["picture_pages"]
    with ZipFile(result) as deck:
        book=next(n for n in deck.namelist() if n.endswith(".xlsx"))
        with ZipFile(io.BytesIO(deck.read(book))) as sheet:
            tree=ET.fromstring(sheet.read("xl/worksheets/sheet1.xml"))
            assert [e.text for e in tree.findall(".//{*}v")]==["3","2.8"]
        assert any("由甲方确认后实施" in deck.read(n).decode() for n in deck.namelist() if n.endswith("/slide4.xml"))
