"""断网沙盒内的 Mermaid 图表语法静态校验脚本（无第三方依赖）。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

_SUPPORTED_HEADERS = (
    "flowchart",
    "graph",
    "sequencediagram",
    "statediagram",
    "statediagram-v2",
    "classdiagram",
    "erdiagram",
    "xychart-beta",
)
_UNQUOTED_PAREN = re.compile(r'\[[^\]"\n]*[()][^\]"\n]*\]')
_HTML_TAG = re.compile(r"<[a-zA-Z][^>]*>")


def check_mermaid_source(source: str) -> dict[str, Any]:
    """检查单段 Mermaid 源码的类型头、未加引号括号标签与禁用 HTML 标签。"""
    warnings: list[str] = []
    lines = [line.strip() for line in source.splitlines() if line.strip() and not line.strip().startswith("%%")]
    if not lines:
        return {"valid": False, "warnings": ["Mermaid 图表源码为空"]}
    first_token = lines[0].split()[0].lower()
    if first_token not in _SUPPORTED_HEADERS:
        warnings.append(f"图表类型 `{lines[0]}` 不在受支持清单内（仅支持 flowchart/graph/sequence/state/class/er/xychart-beta）")
    if _UNQUOTED_PAREN.search(source):
        warnings.append("节点方括号标签包含括号但未加双引号，请改写为 id[\"标签(说明)\"]")
    if _HTML_TAG.search(source):
        warnings.append("检测到 HTML 标签，请改为纯文本换行")
    return {"valid": not warnings, "header": first_token, "warnings": warnings}


def main(argv: list[str]) -> int:
    """命令行入口：接收 Mermaid 文件路径，输出 JSON 校验结果（发现问题返回码 2）。"""
    if len(argv) < 2:
        print(json.dumps({"error": "请传入 Mermaid 文件路径"}, ensure_ascii=False))
        return 1
    result = check_mermaid_source(Path(argv[1]).read_text(encoding="utf-8"))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
