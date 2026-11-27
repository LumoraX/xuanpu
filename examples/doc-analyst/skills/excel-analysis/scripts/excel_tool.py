"""断网沙盒内的 Excel (.xlsx) 生成与解析工具（优先 openpyxl，兼容零依赖标准 OOXML 回退）。"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from html import escape
from pathlib import Path
from typing import Any

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _col_letter(index_zero: int) -> str:
    """将 0 起始列号转换为 Excel 列标（A, B, ..., Z, AA）。"""
    out = ""
    n = index_zero + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _build_xlsx_ooxml(spec: dict[str, Any], dst: Path) -> dict[str, Any]:
    """零依赖生成符合 OOXML 规范的真实 .xlsx 文件（支持文本、数值与公式）。"""
    sheet_name = str(spec.get("sheet") or "Sheet1")[:31]
    headers: list[str] = [str(h) for h in (spec.get("headers") or [])]
    rows: list[list[Any]] = [list(r) for r in (spec.get("rows") or [])]
    all_rows: list[list[Any]] = ([headers] if headers else []) + rows

    row_xml_parts: list[str] = []
    for r_idx, row in enumerate(all_rows, start=1):
        cells_xml: list[str] = []
        for c_idx, val in enumerate(row):
            ref = f"{_col_letter(c_idx)}{r_idx}"
            if isinstance(val, bool):
                cells_xml.append(f'<c r="{ref}" t="b"><v>{1 if val else 0}</v></c>')
            elif isinstance(val, int | float):
                cells_xml.append(f'<c r="{ref}"><v>{val}</v></c>')
            elif isinstance(val, str) and val.startswith("="):
                cells_xml.append(f'<c r="{ref}"><f>{escape(val[1:])}</f></c>')
            else:
                text = escape(str(val if val is not None else ""))
                cells_xml.append(f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>')
        row_xml_parts.append(f'<row r="{r_idx}">{"".join(cells_xml)}</row>')

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_MAIN_NS}"><sheetData>{"".join(row_xml_parts)}</sheetData></worksheet>'
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{_MAIN_NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    dst.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return {"output": str(dst), "sheet": sheet_name, "row_count": len(rows), "engine": "ooxml_stdlib"}


def build_xlsx(spec: dict[str, Any], dst: Path) -> dict[str, Any]:
    """根据 JSON 规范生成真实 .xlsx 工作簿（支持动态公式）。"""
    try:
        import openpyxl  # type: ignore[import-untyped]
    except ImportError:
        return _build_xlsx_ooxml(spec, dst)

    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = str(spec.get("sheet") or "Sheet1")[:31]
    headers = list(spec.get("headers") or [])
    if headers:
        ws.append(headers)
    rows = list(spec.get("rows") or [])
    for row in rows:
        ws.append(list(row))
    dst.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dst)
    return {"output": str(dst), "sheet": ws.title, "row_count": len(rows), "engine": "openpyxl"}


def summarize_xlsx(path: Path) -> dict[str, Any]:
    """读取 .xlsx 工作簿的工作表列表、行列数、公式清单与样本数据。"""
    try:
        import openpyxl
    except ImportError:
        return _summarize_xlsx_ooxml(path)

    wb = openpyxl.load_workbook(path, data_only=False)
    sheets_out: list[dict[str, Any]] = []
    for name in wb.sheetnames:
        ws = wb[name]
        rows = [[cell.value for cell in row] for row in ws.iter_rows()]
        formulas = [
            f"{cell.coordinate}:{cell.value}"
            for row in ws.iter_rows()
            for cell in row
            if isinstance(cell.value, str) and cell.value.startswith("=")
        ]
        sheets_out.append({
            "name": name,
            "row_count": len(rows),
            "col_count": max((len(r) for r in rows), default=0),
            "formulas": formulas[:50],
            "preview_rows": rows[:10],
        })
    return {"file": path.name, "sheets": sheets_out, "engine": "openpyxl"}


def _summarize_xlsx_ooxml(path: Path) -> dict[str, Any]:
    """零依赖解析标准 .xlsx 压缩包中的工作表文本、数值与公式。"""
    with zipfile.ZipFile(path, "r") as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            sroot = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in sroot.findall(f"{{{_MAIN_NS}}}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_MAIN_NS}}}t")))
        sheet_names = sorted(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        sheets_out: list[dict[str, Any]] = []
        for idx, sname in enumerate(sheet_names, start=1):
            root = ET.fromstring(zf.read(sname))
            rows: list[list[Any]] = []
            formulas: list[str] = []
            for r_el in root.findall(f".//{{{_MAIN_NS}}}row"):
                row_vals: list[Any] = []
                for c_el in r_el.findall(f"{{{_MAIN_NS}}}c"):
                    ctype = c_el.attrib.get("t", "")
                    ref = c_el.attrib.get("r", "")
                    f_el = c_el.find(f"{{{_MAIN_NS}}}f")
                    v_el = c_el.find(f"{{{_MAIN_NS}}}v")
                    if f_el is not None and f_el.text:
                        formula = "=" + f_el.text
                        formulas.append(f"{ref}:{formula}")
                        row_vals.append(formula)
                    elif ctype == "inlineStr":
                        txt = "".join(t.text or "" for t in c_el.iter(f"{{{_MAIN_NS}}}t"))
                        row_vals.append(txt)
                    elif ctype == "s" and v_el is not None and v_el.text and v_el.text.isdigit():
                        row_vals.append(shared[int(v_el.text)])
                    elif v_el is not None and v_el.text is not None:
                        try:
                            num = float(v_el.text)
                            row_vals.append(int(num) if num.is_integer() else num)
                        except ValueError:
                            row_vals.append(v_el.text)
                    else:
                        row_vals.append(None)
                rows.append(row_vals)
            sheets_out.append({
                "name": f"Sheet{idx}",
                "row_count": len(rows),
                "col_count": max((len(r) for r in rows), default=0),
                "formulas": formulas[:50],
                "preview_rows": rows[:10],
            })
    return {"file": path.name, "sheets": sheets_out, "engine": "ooxml_stdlib"}


def main(argv: list[str]) -> int:
    """命令行入口：支持 ``build <spec.json> <out.xlsx>`` 与 ``summarize <in.xlsx>``。"""
    if len(argv) < 3:
        print(json.dumps({"error": "用法：excel_tool.py build <spec.json> <out.xlsx> | summarize <in.xlsx>"}, ensure_ascii=False))
        return 1
    cmd = argv[1]
    if cmd == "build" and len(argv) >= 4:
        spec = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        print(json.dumps(build_xlsx(spec, Path(argv[3])), ensure_ascii=False))
        return 0
    if cmd == "summarize":
        print(json.dumps(summarize_xlsx(Path(argv[2])), ensure_ascii=False))
        return 0
    print(json.dumps({"error": f"不支持的子命令：{cmd}"}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
