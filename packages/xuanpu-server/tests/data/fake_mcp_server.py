"""测试用的最小 MCP server：两个工具，回显调用参数与注入的环境变量。

由 conftest 拷进样例业务包，用测试进程的解释器以 stdio 方式运行。
"""

from __future__ import annotations

import json
import os
import sys

TOOLS = [
    {
        "name": "search_customer",
        "description": "按名字查客户",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}},
    },
    {
        "name": "update_ticket",
        "description": "更新工单",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
    },
    {
        "name": "hidden_tool",
        "description": "清单白名单之外的工具",
        "inputSchema": {"type": "object"},
    },
]


def reply(message_id: object, result: dict[str, object]) -> None:
    """回一条 JSON-RPC 结果。"""
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if "id" not in message:
        continue
    if method == "initialize":
        reply(
            message["id"],
            {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-crm", "version": "1.0"},
            },
        )
    elif method == "tools/list":
        reply(message["id"], {"tools": TOOLS})
    elif method == "tools/call":
        params = message["params"]
        log = os.environ.get("CALL_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(params, ensure_ascii=False) + "\n")
        text = json.dumps(
            {
                "tool": params["name"],
                "arguments": params["arguments"],
                "region": os.environ.get("CRM_REGION"),
                "token_length": len(os.environ.get("CRM_TOKEN", "")),
                "inherited": sorted(k for k in os.environ if k.startswith("HOST_ONLY")),
            },
            ensure_ascii=False,
        )
        reply(message["id"], {"content": [{"type": "text", "text": text}]})
    else:
        reply(message["id"], {})
