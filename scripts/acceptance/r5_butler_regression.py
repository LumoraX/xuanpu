"""R5-C2 管家真实模型回归：逐条对管家说话，按回执统计误派与漏派。

不读环境变量：平台地址、凭据文件与代表的员工由命令行给出。只调用正式接口
``POST /v1/assistant/messages``；每条用独立的 ``request_id``，结果写成 JSON 供验收记录引用。
判定口径：回执里有派工即“派了”；expect 为 dispatch 而没派是漏派，为 self/none/ask 而派了是误派；
dispatch 且派给了别的包也算误派。ask 与 none 的文字质量需人工复核，脚本只统计是否误派。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

from sdk import XuanpuClient

# 管家一轮在服务端最长 90 秒（butler.engine.TURN_SECONDS）；客户端须等得比它久，
# 否则真实模型下安排编组这类慢的一轮会被 SDK 默认的 60 秒先掐断。
CLIENT_TIMEOUT = 120.0

CASES = Path(__file__).parent / "data" / "r5_butler_regression.json"


def judge(case: dict[str, Any], receipt: dict[str, Any]) -> str:
    """一条用例的结论：ok、misdispatch（误派）或 missed（漏派）。"""
    packages = {d["package"] for d in receipt.get("dispatches", [])}
    if case["expect"] == "dispatch":
        if not packages:
            return "missed"
        return "ok" if packages == {case["package"]} else "misdispatch"
    return "misdispatch" if packages else "ok"


async def run(base_url: str, key: str, act_as: str, prefix: str) -> dict[str, Any]:
    """逐条执行并汇总。"""
    cases = json.loads(CASES.read_text())["cases"]
    results = []
    async with XuanpuClient(base_url, key, act_as_user=act_as, timeout_seconds=CLIENT_TIMEOUT) as client:
        for case in cases:
            receipt = await client.send_to_butler(case["text"], request_id=f"{prefix}-{case['id']}")
            results.append({"id": case["id"], "expect": case["expect"], "mode": receipt.get("mode"),
                            "dispatches": receipt.get("dispatches", []),
                            "reply": receipt.get("message"), "verdict": judge(case, receipt)})
    total = len(results)
    return {"total": total, "results": results,
            "misdispatch": sum(r["verdict"] == "misdispatch" for r in results),
            "missed": sum(r["verdict"] == "missed" for r in results)}


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--key-file", required=True, help="权限 600 的凭据文件，只含一行 API key")
    parser.add_argument("--act-as", required=True, help="代表的员工标识")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    key = Path(args.key_file).read_text().strip()
    summary = asyncio.run(run(args.base_url, key, args.act_as, f"r5reg-{int(time.time())}"))
    Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"total={summary['total']} misdispatch={summary['misdispatch']} missed={summary['missed']}")


if __name__ == "__main__":
    main()
