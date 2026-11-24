"""R5-D2 真实模型验收：一次三成员会诊，核对瀑布里成员确有时间交叠、用量记录完整。

不读环境变量：平台地址、凭据文件、代表的员工与请求原文由命令行给出。只调用正式接口：
``POST /v1/assistant/messages`` 提交请求，``GET /v1/assistant/groups/{id}`` 与消息区跟进度，
``GET /v1/traces/{id}/waterfall`` 读瀑布。高分歧时脚本不代员工裁决，只记录裁决项并结束，
由验收人在界面上裁决后再跑 ``--group`` 补读。结果写成 JSON 供验收记录引用。
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

DEFAULT_TEXT = ("请安排三位匠人会诊：这个工单该不该升级处理？各自给出结论，"
                "全部给完后再比对，不要只交给一位。")


def overlaps(rows: list[dict[str, Any]], kind: str = "dispatch") -> int:
    """瀑布里同类行两两时间交叠的对数（运行中的行按到现在为止计）。"""
    spans = []
    for row in rows:
        if row.get("kind") != kind:
            continue
        start = int(row["offset_ms"])
        end = start + int(row["duration_ms"]) if row.get("duration_ms") is not None else None
        spans.append((start, end))
    count = 0
    for i, (a_start, a_end) in enumerate(spans):
        for b_start, b_end in spans[i + 1:]:
            a_stop = a_end if a_end is not None else float("inf")
            b_stop = b_end if b_end is not None else float("inf")
            if a_start < b_stop and b_start < a_stop:
                count += 1
    return count


# 用量按模型调用的尝试记录，只有执行类 span（管家、匠人）有用量；工具、派工、编组、等待行
# 本来就没有尝试，恒为 usage_complete=false（见执行追踪架构文档），不参与判定。
USAGE_KINDS = frozenset({"butler", "artisan"})


def usage_complete(rows: list[dict[str, Any]]) -> bool:
    """已结束的执行行（管家、匠人）都有完整用量，且至少有一行。"""
    ended = [r for r in rows if r.get("kind") in USAGE_KINDS and r.get("duration_ms") is not None]
    return bool(ended) and all(r.get("usage_complete") for r in ended)


async def follow(client: XuanpuClient, group_id: str, deadline: float) -> dict[str, Any]:
    """轮询编组直到汇总或进入等员工裁决；超时返回最后一次读取。"""
    while True:
        group = await client.get_butler_group(group_id)
        waiting = any(v["level"] == "high" and not v["ruled"] and v["pending_id"]
                      for v in group["verdicts"])
        if group["summarized"] or waiting or time.monotonic() > deadline:
            return group
        await asyncio.sleep(3)


async def run(args: argparse.Namespace, key: str) -> dict[str, Any]:
    """提交（或补读）一次会诊并汇总验收结论。"""
    async with XuanpuClient(args.base_url, key, act_as_user=args.act_as,
                            timeout_seconds=CLIENT_TIMEOUT) as client:
        group_id = args.group
        if group_id is None:
            receipt = await client.send_to_butler(args.text, request_id=f"r5consult-{int(time.time())}")
            if not receipt.get("groups"):
                return {"ok": False, "reason": "管家没有安排编组", "receipt": receipt}
            group_id = receipt["groups"][0]
        group = await follow(client, group_id, time.monotonic() + args.timeout)
        rows = (await client.trace_waterfall(group["trace_id"]))["rows"]
        messages = await client.list_group_messages(group_id)
    consult = [s for s in group["steps"] if s["mode"] == "consult"]
    return {
        "ok": bool(consult) and len(consult[0]["members"]) >= 3 and overlaps(rows) > 0,
        "group": group, "messages": messages, "overlapping_pairs": overlaps(rows),
        "usage_complete": usage_complete(rows), "rows": rows,
    }


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--key-file", required=True, help="权限 600 的凭据文件，只含一行 API key")
    parser.add_argument("--act-as", required=True, help="代表的员工标识")
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--group", help="补读已有编组（例如员工裁决之后）")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    key = Path(args.key_file).read_text().strip()
    summary = asyncio.run(run(args, key))
    Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"ok={summary['ok']} overlapping_pairs={summary.get('overlapping_pairs')} "
          f"usage_complete={summary.get('usage_complete')}")


if __name__ == "__main__":
    main()
