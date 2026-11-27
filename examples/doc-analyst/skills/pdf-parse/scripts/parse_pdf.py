"""断网沙盒内的 PDF 文本与结构化元信息解析脚本（优先使用 pypdf）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def parse_pdf_file(path: Path, *, max_pages: int = 50) -> dict[str, Any]:
    """解析 PDF 页数、文档元信息与逐页文本内容，输出结构化 JSON 摘要。"""
    if not path.is_file():
        raise FileNotFoundError(f"PDF 文件不存在：{path}")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("当前沙盒未安装 pypdf，请使用 office-sandbox 镜像执行 PDF 解析") from exc

    reader = PdfReader(str(path))
    total_pages = len(reader.pages)
    meta = reader.metadata or {}
    pages: list[dict[str, Any]] = []
    for idx in range(min(total_pages, max(1, max_pages))):
        text = (reader.pages[idx].extract_text() or "").strip()
        pages.append({"page": idx + 1, "char_count": len(text), "text": text})
    return {
        "file": path.name,
        "total_pages": total_pages,
        "parsed_pages": len(pages),
        "truncated": total_pages > len(pages),
        "metadata": {
            "title": str(meta.get("/Title") or ""),
            "author": str(meta.get("/Author") or ""),
        },
        "pages": pages,
    }


def main(argv: list[str]) -> int:
    """命令行入口：接收 PDF 文件路径并输出 JSON 解析结果。"""
    if len(argv) < 2:
        print(json.dumps({"error": "请传入 PDF 文件路径"}, ensure_ascii=False))
        return 1
    max_pages = int(argv[2]) if len(argv) >= 3 else 50
    result = parse_pdf_file(Path(argv[1]), max_pages=max_pages)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
