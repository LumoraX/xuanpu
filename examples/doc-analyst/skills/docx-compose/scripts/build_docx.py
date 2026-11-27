"""断网沙盒内的 Word (.docx) 正式文档生成与校验脚本（优先 python-docx，兼容零依赖 OOXML 回退）。"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from html import escape
from pathlib import Path
from typing import Any

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _build_docx_ooxml(spec: dict[str, Any], dst: Path) -> dict[str, Any]:
    """零依赖生成标准 OOXML .docx 文档（标题、段落、要点与表格）。"""
    title = str(spec.get("title") or "业务报告")
    blocks: list[str] = [f"<w:p><w:r><w:rPr><w:b/><w:sz w:val='32'/></w:rPr><w:t>{escape(title)}</w:t></w:r></w:p>"]
    para_count = 1
    for sec in spec.get("sections") or []:
        heading = str(sec.get("heading") or "").strip()
        if heading:
            blocks.append(f"<w:p><w:r><w:rPr><w:b/><w:sz w:val='26'/></w:rPr><w:t>{escape(heading)}</w:t></w:r></w:p>")
            para_count += 1
        for p in sec.get("paragraphs") or []:
            blocks.append(f"<w:p><w:r><w:t>{escape(str(p))}</w:t></w:r></w:p>")
            para_count += 1
        for b in sec.get("bullets") or []:
            blocks.append(f"<w:p><w:r><w:t>• {escape(str(b))}</w:t></w:r></w:p>")
            para_count += 1
        tbl = sec.get("table")
        if isinstance(tbl, dict) and tbl.get("headers"):
            rows = [tbl["headers"], *(tbl.get("rows") or [])]
            tr_xml = "".join(
                "<w:tr>" + "".join(
                    f"<w:tc><w:p><w:r><w:t>{escape(str(cell))}</w:t></w:r></w:p></w:tc>"
                    for cell in row
                ) + "</w:tr>"
                for row in rows
            )
            blocks.append(f"<w:tbl>{tr_xml}</w:tbl>")

    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W_NS}"><w:body>{"".join(blocks)}<w:sectPr/></w:body></w:document>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    dst.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("word/document.xml", doc_xml)
    return {"output": str(dst), "title": title, "paragraph_count": para_count, "engine": "ooxml_stdlib"}


def build_docx(spec: dict[str, Any], dst: Path) -> dict[str, Any]:
    """根据 JSON 结构化提纲生成 .docx 正式报告。"""
    try:
        import docx  # type: ignore[import-untyped]
    except ImportError:
        return _build_docx_ooxml(spec, dst)

    doc = docx.Document()
    title = str(spec.get("title") or "业务报告")
    doc.add_heading(title, level=0)
    for sec in spec.get("sections") or []:
        if sec.get("heading"):
            doc.add_heading(str(sec["heading"]), level=1)
        for p in sec.get("paragraphs") or []:
            doc.add_paragraph(str(p))
        for b in sec.get("bullets") or []:
            doc.add_paragraph(str(b), style="List Bullet")
        tbl = sec.get("table")
        if isinstance(tbl, dict) and tbl.get("headers"):
            headers = [str(h) for h in tbl["headers"]]
            rows = [list(r) for r in (tbl.get("rows") or [])]
            t = doc.add_table(rows=1 + len(rows), cols=len(headers))
            for idx, h in enumerate(headers):
                t.cell(0, idx).text = h
            for r_idx, row in enumerate(rows, start=1):
                for c_idx, cell in enumerate(row[: len(headers)]):
                    t.cell(r_idx, c_idx).text = str(cell)
    dst.parent.mkdir(parents=True, exist_ok=True)
    doc.save(dst)
    return {"output": str(dst), "title": title, "paragraph_count": len(doc.paragraphs), "engine": "python-docx"}


def read_docx(path: Path) -> dict[str, Any]:
    """读取 .docx 全部段落文本用于交付前确定性核验。"""
    with zipfile.ZipFile(path, "r") as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    paras: list[str] = []
    for p in root.iter(f"{{{_W_NS}}}p"):
        text = "".join(t.text or "" for t in p.iter(f"{{{_W_NS}}}t")).strip()
        if text:
            paras.append(text)
    return {"file": path.name, "paragraph_count": len(paras), "paragraphs": paras}


def main(argv: list[str]) -> int:
    """命令行入口：支持 ``build <spec.json> <out.docx>`` 与 ``read <in.docx>``。"""
    if len(argv) < 3:
        print(json.dumps({"error": "用法：build_docx.py build <spec.json> <out.docx> | read <in.docx>"}, ensure_ascii=False))
        return 1
    if argv[1] == "build" and len(argv) >= 4:
        spec = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        print(json.dumps(build_docx(spec, Path(argv[3])), ensure_ascii=False))
        return 0
    if argv[1] == "read":
        print(json.dumps(read_docx(Path(argv[2])), ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
