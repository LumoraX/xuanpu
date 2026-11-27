"""断网沙盒内的轻量确定性 CSV 统计分析脚本（无第三方依赖）。"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any


def analyze_csv_file(path: Path) -> dict[str, Any]:
    """读取 UTF-8 CSV 文件并计算总行数与各数值列的均值/最小/最大值。"""
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        headers = list(reader.fieldnames or [])
    numeric_stats: dict[str, dict[str, float | int]] = {}
    for col in headers:
        nums: list[float] = []
        for row in rows:
            raw = (row.get(col) or "").strip()
            if not raw:
                continue
            try:
                nums.append(float(raw))
            except ValueError:
                nums = []
                break
        if nums:
            numeric_stats[col] = {
                "count": len(nums),
                "sum": round(sum(nums), 4),
                "mean": round(sum(nums) / len(nums), 4),
                "min": min(nums),
                "max": max(nums),
            }
    return {"file": path.name, "row_count": len(rows), "columns": headers, "numeric_stats": numeric_stats}


def main(argv: list[str]) -> int:
    """命令行入口：接收 CSV 路径并输出 JSON 统计摘要。"""
    if len(argv) < 2:
        print(json.dumps({"error": "请传入 CSV 文件路径"}, ensure_ascii=False))
        return 1
    result = analyze_csv_file(Path(argv[1]))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
