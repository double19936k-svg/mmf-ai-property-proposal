"""Attach the editable Excel data sheets missing from a native chart export."""
from __future__ import annotations

import io
import posixpath
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TAG = lambda name: f"{{{C}}}{name}"


def _workbook(unit: str, labels: list[str], values: list[str]) -> bytes:
    def string_cell(addr, value):
        return f'<c r="{addr}" t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>'

    rows = [f'<row r="1">{string_cell("A1", "指标")}{string_cell("B1", unit)}</row>']
    rows.extend(f'<row r="{i}">{string_cell(f"A{i}", label)}<c r="B{i}"><v>{val}</v></c></row>'
                for i, (label, val) in enumerate(zip(labels, values, strict=True), 2))
    result = io.BytesIO()
    with ZipFile(result, "w", ZIP_DEFLATED) as book:
        book.writestr("[Content_Types].xml", f'<Types xmlns="{CT}">'
                      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                      '<Default Extension="xml" ContentType="application/xml"/>'
                      f'<Override PartName="/xl/workbook.xml" ContentType="{XLSX}.main+xml"/>'
                      '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                      '</Types>')
        book.writestr("_rels/.rels", f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Type="{R}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        book.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Type="{R}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        book.writestr("xl/workbook.xml", f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="数据" sheetId="1" r:id="rId1"/></sheets></workbook>')
        book.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{S}"><sheetData>{"".join(rows)}</sheetData></worksheet>')
    return result.getvalue()


def _attach_data(root: ET.Element) -> bytes:
    series = root.findall(f".//{TAG('ser')}")
    if len(series) != 1:
        raise ValueError("Project evidence charts must contain one series")
    series = series[0]
    cat, val, name = [series.find(TAG(k)) for k in ("cat", "val", "tx")]
    raw_cat, raw_val = cat.find(TAG("strLit")), val.find(TAG("numLit"))
    labels = [node.text or "" for node in raw_cat.findall(f"{TAG('pt')}/{TAG('v')}")]
    values = [node.text or "" for node in raw_val.findall(f"{TAG('pt')}/{TAG('v')}")]
    if not labels or len(labels) != len(values) or not all(re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:[Ee][+-]?\d+)?", v) for v in values):
        raise ValueError("Project evidence chart data is invalid")

    def reference(parent, old, kind, formula):
        parent.remove(old)
        ref = ET.SubElement(parent, TAG(kind + "Ref"))
        ET.SubElement(ref, TAG("f")).text = formula
        old.tag = TAG(kind + "Cache")
        ref.append(old)

    count = len(values) + 1
    reference(cat, raw_cat, "str", f"'数据'!$A$2:$A${count}")
    reference(val, raw_val, "num", f"'数据'!$B$2:$B${count}")
    unit = name.findtext(TAG("v")) or ""
    cache = ET.Element(TAG("strCache"))
    ET.SubElement(cache, TAG("ptCount"), {"val": "1"})
    ET.SubElement(ET.SubElement(cache, TAG("pt"), {"idx": "0"}), TAG("v")).text = unit
    name.remove(name.find(TAG("v")))
    name.append(cache)
    reference(name, cache, "str", "'数据'!$B$1")
    external = ET.SubElement(root, TAG("externalData"), {f"{{{R}}}id": "rIdWorkbook"})
    ET.SubElement(external, TAG("autoUpdate"), {"val": "0"})
    return _workbook(unit, labels, values)


def embed_chart_data(input_path: Path, output_path: Path) -> int:
    """Rewrite only chart parts and OPC relationships, leaving slide XML untouched."""
    for prefix, namespace in (("c", C), ("r", R), ("a", "http://schemas.openxmlformats.org/drawingml/2006/main")):
        ET.register_namespace(prefix, namespace)
    chart_count = 0
    with ZipFile(input_path) as incoming, ZipFile(output_path, "w", ZIP_DEFLATED) as outgoing:
        for entry in incoming.infolist():
            name = entry.filename
            raw = incoming.read(entry)
            if re.fullmatch(r"ppt/(?:slides/)?charts/chart\d+\.xml", name):
                chart_count += 1
                chart = ET.fromstring(raw)
                workbook = _attach_data(chart)
                part = f"ppt/embeddings/mmf-chart-data-{chart_count}.xlsx"
                directory, filename = posixpath.split(name)
                target = posixpath.relpath(part, directory)
                outgoing.writestr(part, workbook)
                outgoing.writestr(f"{directory}/_rels/{filename}.rels",
                                  f'<Relationships xmlns="{REL}"><Relationship Id="rIdWorkbook" Type="{R}/package" Target="{target}"/></Relationships>')
                raw = ET.tostring(chart, encoding="utf-8", xml_declaration=True)
            elif name == "[Content_Types].xml":
                raw = raw.replace(b"</Types>", f'<Default Extension="xlsx" ContentType="{XLSX}"/></Types>'.encode(), 1)
            outgoing.writestr(entry, raw)
    return chart_count


if __name__ == "__main__":
    embed_chart_data(Path(sys.argv[1]), Path(sys.argv[2]))
