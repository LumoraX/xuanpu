"""断网沙盒内的安全独立 HTML 报告渲染脚本（纯转义输出，零外链，零第三方依赖）。"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path
from typing import Any


def render_markdown_to_safe_html(markdown_text: str, *, title: str = "分析报告") -> str:
    """将常见 Markdown 标题、列表、表格与代码块转为安全转义、零外链的独立 HTML。"""
    body_parts: list[str] = []
    in_code = False
    code_lines: list[str] = []
    in_list = False
    table_rows: list[list[str]] = []

    def flush_list() -> None:
        nonlocal in_list
        if in_list:
            body_parts.append("</ul>")
            in_list = False

    def flush_table() -> None:
        if not table_rows:
            return
        rows_html = ["<table>"]
        for idx, cells in enumerate(table_rows):
            tag = "th" if idx == 0 else "td"
            row_cells = "".join(f"<{tag}>{html.escape(c)}</{tag}>" for c in cells)
            rows_html.append(f"<tr>{row_cells}</tr>")
        rows_html.append("</table>")
        body_parts.append("\n".join(rows_html))
        table_rows.clear()

    for raw in markdown_text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("```"):
            flush_list()
            flush_table()
            if not in_code:
                in_code = True
                code_lines = []
            else:
                body_parts.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
                in_code = False
            continue
        if in_code:
            code_lines.append(raw)
            continue
        if not stripped:
            flush_list()
            flush_table()
            continue
        if stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 2:
            flush_list()
            inner = stripped[1:-1]
            if all(ch in "-:| " for ch in inner):
                continue
            table_rows.append([cell.strip() for cell in inner.split("|")])
            continue
        flush_table()
        if stripped.startswith(("# ", "## ", "### ")):
            flush_list()
            level = len(stripped) - len(stripped.lstrip("#"))
            text = stripped[level:].strip()
            body_parts.append(f"<h{level}>{html.escape(text)}</h{level}>")
        elif stripped.startswith(("- ", "* ")):
            if not in_list:
                body_parts.append("<ul>")
                in_list = True
            body_parts.append(f"<li>{html.escape(stripped[2:].strip())}</li>")
        else:
            flush_list()
            body_parts.append(f"<p>{html.escape(stripped)}</p>")

    if in_code:
        body_parts.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
    flush_list()
    flush_table()

    safe_title = html.escape(title)
    content = "\n".join(body_parts)
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{safe_title}</title>\n"
        "<style>"
        "body{font-family:system-ui,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem;line-height:1.6;color:#1f2328}"
        "table{border-collapse:collapse;width:100%;margin:1rem 0}"
        "th,td{border:1px solid #d0d7de;padding:.45rem .7rem;text-align:left}"
        "th{background:#f6f8fa}"
        "pre{background:#f6f8fa;padding:.8rem;border-radius:6px;overflow:auto}"
        "</style>\n</head>\n<body>\n"
        f"{content}\n"
        "</body>\n</html>\n"
    )


def main(argv: list[str]) -> int:
    """命令行入口：接收 Markdown 输入路径与 HTML 输出路径，写出独立 HTML 并打印 JSON 回执。"""
    if len(argv) < 3:
        print(json.dumps({"error": "请依次传入 Markdown 输入路径与 HTML 输出路径"}, ensure_ascii=False))
        return 1
    src = Path(argv[1])
    dst = Path(argv[2])
    title = argv[3] if len(argv) >= 4 else src.stem
    rendered = render_markdown_to_safe_html(src.read_text(encoding="utf-8"), title=title)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(rendered, encoding="utf-8")
    summary: dict[str, Any] = {"output": str(dst), "bytes": len(rendered.encode("utf-8")), "title": title}
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
