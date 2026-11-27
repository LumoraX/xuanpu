"""断网沙盒内的轻量确定性文档版本比对脚本（Unified Diff 与变更统计，无第三方依赖）。"""

from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path
from typing import Any


def diff_text_files(old_path: Path, new_path: Path) -> dict[str, Any]:
    """比较两份 UTF-8 文本文档，生成 Unified Diff 与新增/删除行统计。"""
    old_lines = old_path.read_text(encoding="utf-8").splitlines(keepends=True)
    new_lines = new_path.read_text(encoding="utf-8").splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=old_path.name,
            tofile=new_path.name,
            n=2,
        )
    )
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    return {
        "old_file": old_path.name,
        "new_file": new_path.name,
        "changed": bool(diff_lines),
        "added_lines": added,
        "removed_lines": removed,
        "unified_diff": "".join(diff_lines),
    }


def main(argv: list[str]) -> int:
    """命令行入口：接收旧版与新版两个文件路径，输出 JSON 差异报告。"""
    if len(argv) < 3:
        print(json.dumps({"error": "请依次传入旧版与新版文件路径"}, ensure_ascii=False))
        return 1
    result = diff_text_files(Path(argv[1]), Path(argv[2]))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
