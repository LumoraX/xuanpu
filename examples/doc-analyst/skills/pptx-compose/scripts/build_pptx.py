"""断网沙盒内的 PowerPoint (.pptx) 演示文稿生成与自检脚本（优先 python-pptx，兼容零依赖 OOXML 回退）。"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from html import escape
from pathlib import Path
from typing import Any

_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _slide_xml(title: str, bullets: list[str]) -> str:
    """生成单页标准 OOXML 幻灯片 XML。"""
    lines = [title, *[f"• {b}" for b in bullets]]
    paras = "".join(f"<a:p><a:r><a:t>{escape(line)}</a:t></a:r></a:p>" for line in lines)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:sld xmlns:a="{_A_NS}" xmlns:p="{_P_NS}" xmlns:r="{_R_NS}">'
        "<p:cSld><p:spTree>"
        "<p:nvGrpSpPr><p:cNvPr id='1' name=''/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>"
        "<p:grpSpPr/>"
        "<p:sp><p:nvSpPr><p:cNvPr id='2' name='Content'/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>"
        f"<p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/>{paras}</p:txBody></p:sp>"
        "</p:spTree></p:cSld></p:sld>"
    )


def _build_pptx_ooxml(spec: dict[str, Any], dst: Path) -> dict[str, Any]:
    """零依赖生成标准 OOXML .pptx 幻灯片。"""
    slides = list(spec.get("slides") or [{"title": spec.get("title") or "演示汇报", "bullets": []}])
    sld_ids = "".join(f'<p:sldId id="{256 + i}" r:id="rId{i + 1}"/>' for i in range(len(slides)))
    pres_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:presentation xmlns:a="{_A_NS}" xmlns:p="{_P_NS}" xmlns:r="{_R_NS}">'
        f"<p:sldIdLst>{sld_ids}</p:sldIdLst></p:presentation>"
    )
    pres_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{i + 1}.xml"/>'
            for i in range(len(slides))
        )
        + "</Relationships>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>'
        "</Relationships>"
    )
    overrides = "".join(
        f'<Override PartName="/ppt/slides/slide{i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for i in range(len(slides))
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        f"{overrides}</Types>"
    )
    dst.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("ppt/presentation.xml", pres_xml)
        zf.writestr("ppt/_rels/presentation.xml.rels", pres_rels)
        for i, slide in enumerate(slides, start=1):
            zf.writestr(
                f"ppt/slides/slide{i}.xml",
                _slide_xml(str(slide.get("title") or f"第 {i} 页"), [str(b) for b in (slide.get("bullets") or [])]),
            )
    return {"output": str(dst), "slide_count": len(slides), "engine": "ooxml_stdlib"}


def build_pptx(spec: dict[str, Any], dst: Path) -> dict[str, Any]:
    """根据 JSON 大纲生成 .pptx 演示文稿。"""
    try:
        from pptx import Presentation  # type: ignore[import-untyped]
    except ImportError:
        return _build_pptx_ooxml(spec, dst)

    prs = Presentation()
    slides = list(spec.get("slides") or [{"title": spec.get("title") or "演示汇报", "bullets": []}])
    for s in slides:
        layout = prs.slide_layouts[1] if len(prs.slide_layouts) > 1 else prs.slide_layouts[0]
        slide = prs.slides.add_slide(layout)
        if slide.shapes.title:
            slide.shapes.title.text = str(s.get("title") or "")
        if len(slide.placeholders) > 1:
            tf = slide.placeholders[1].text_frame
            for idx, b in enumerate(s.get("bullets") or []):
                p = tf.paragraphs[0] if idx == 0 else tf.add_paragraph()
                p.text = str(b)
    dst.parent.mkdir(parents=True, exist_ok=True)
    prs.save(dst)
    return {"output": str(dst), "slide_count": len(slides), "engine": "python-pptx"}


def inspect_pptx(path: Path) -> dict[str, Any]:
    """提取 .pptx 每页标题与要点文本，用于交付前核对。"""
    with zipfile.ZipFile(path, "r") as zf:
        slide_files = sorted(n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
        pages: list[dict[str, Any]] = []
        for idx, sf in enumerate(slide_files, start=1):
            root = ET.fromstring(zf.read(sf))
            texts = [t.text.strip() for t in root.iter(f"{{{_A_NS}}}t") if t.text and t.text.strip()]
            pages.append({"slide": idx, "lines": texts})
    return {"file": path.name, "slide_count": len(pages), "slides": pages}


def main(argv: list[str]) -> int:
    """命令行入口：支持 ``build <spec.json> <out.pptx>`` 与 ``inspect <in.pptx>``。"""
    if len(argv) < 3:
        print(json.dumps({"error": "用法：build_pptx.py build <spec.json> <out.pptx> | inspect <in.pptx>"}, ensure_ascii=False))
        return 1
    if argv[1] == "build" and len(argv) >= 4:
        spec = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        print(json.dumps(build_pptx(spec, Path(argv[3])), ensure_ascii=False))
        return 0
    if argv[1] == "inspect":
        print(json.dumps(inspect_pptx(Path(argv[2])), ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
