"""Compare a PPTX with its actual rendered PDF (requires optional PyMuPDF).

Render with LibreOffice or PowerPoint first. This offline check looks for missing
visible text and text outside the page. Slide appearance still needs a visual review.
"""
import argparse
import json
import posixpath
import re
import unicodedata
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def normalized(text):
    return re.sub(r"\s|\u00ad", "", unicodedata.normalize("NFKC", text))


def audit(pptx, rendered_pdf):
    import pymupdf

    errors = []
    text_blocks = 0
    with ZipFile(pptx) as deck, pymupdf.open(rendered_pdf) as pdf:
        rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(deck.read("ppt/_rels/presentation.xml.rels"))}
        slides = ET.fromstring(deck.read("ppt/presentation.xml")).findall("p:sldIdLst/p:sldId", NS)
        if len(slides) != len(pdf):
            errors.append({"rule": "PAGE_COUNT", "pptx": len(slides), "pdf": len(pdf)})
        for i, (node, page) in enumerate(zip(slides, pdf), 1):
            part = rels[node.attrib[f"{{{NS['r']}}}id"]]
            path = part.lstrip("/") if part.startswith("/") else posixpath.normpath(posixpath.join("ppt", part))
            root = ET.fromstring(deck.read(path))
            rendered = normalized(page.get_text())
            for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        x0, y0, x1, y1 = span["bbox"]
                        if normalized(span["text"]) and (x0 < -1 or y0 < -1 or x1 > page.rect.width+1 or y1 > page.rect.height+1):
                            errors.append({"rule": "RENDER_TEXT_OUTSIDE", "page": i, "bbox": [x0,y0,x1,y1]})
            for j, elm in enumerate(root.findall(".//p:sp", NS) + root.findall(".//a:tc", NS), 1):
                text = normalized("".join(t.text or "" for t in elm.findall(".//a:t", NS)))
                if len(text) < 5:  # A headline or sentence; isolated icons and page ticks are not meaningful.
                    continue
                text_blocks += 1
                if text not in rendered:
                    errors.append({"rule": "RENDER_TEXT_MISSING", "page": i, "block": j, "expected_length": len(text)})
    return {"status": "PASS" if not errors else "CHECK", "pages": len(slides), "text_blocks": text_blocks, "findings": errors}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pptx", type=Path)
    parser.add_argument("pdf", type=Path)
    args = parser.parse_args()
    result = audit(args.pptx, args.pdf)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
