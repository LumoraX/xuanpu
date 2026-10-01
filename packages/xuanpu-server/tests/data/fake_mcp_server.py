"""测试用的最小 MCP server：两个工具，回显调用参数与注入的环境变量。

由 conftest 拷进样例业务包，在连接器沙盒里以 stdio 方式运行：本机沙盒用测试进程的
解释器，容器沙盒用镜像里的 ``python3``。调用日志写在 ``$HOME``（连接器的工作目录）下；
回包里另外报告工作目录之外能否写。参数里带了探测项（试读文件、试连端口）时，探测结果
写进 ``$HOME/probe.json``——结果可能很长，事件里的工具输出只留预览，放不下。
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


def can_write_outside() -> bool:
    """试着在工作目录（HOME）的上一级写文件：沙盒里应当写不了。"""
    target = os.path.join(os.path.dirname(os.environ["HOME"]), "escape.txt")
    try:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("escaped")
    except OSError:
        return False
    return True


def probe(arguments: dict[str, object]) -> dict[str, object]:
    """按参数里的 ``read``（路径列表）试读文件、按 ``connect`` 试连本机端口，返回结果。

    读到的文件给出开头一段，读不到的记 None。
    """
    found: dict[str, object] = {}
    paths = arguments.get("read")
    if isinstance(paths, list):
        read: dict[str, object] = {}
        for path in paths:
            try:
                with open(str(path), encoding="utf-8") as handle:
                    read[str(path)] = handle.read()[:40]
            except OSError:
                read[str(path)] = None
        found["read"] = read
    port = arguments.get("connect")
    if isinstance(port, int):
        import socket

        try:
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
            found["connect"] = True
        except OSError:
            found["connect"] = False
    return found


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
        # 调用日志写在连接器自己的工作目录（HOME）里：平台给连接器的环境里 HOME 指向它
        log_path = os.path.join(os.environ["HOME"], "mcp-calls.jsonl")
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(json.dumps(params, ensure_ascii=False) + "\n")
        probes = probe(params["arguments"])
        if probes:
            probe_path = os.path.join(os.environ["HOME"], "probe.json")
            with open(probe_path, "w", encoding="utf-8") as handle:
                json.dump(probes, handle, ensure_ascii=False)
        text = json.dumps(
            {
                "tool": params["name"],
                "arguments": params["arguments"],
                "region": os.environ.get("CRM_REGION"),
                "token_length": len(os.environ.get("CRM_TOKEN", "")),
                "inherited": sorted(k for k in os.environ if k.startswith("HOST_ONLY")),
                "home": os.environ.get("HOME"),
                "can_write_outside": can_write_outside(),
            },
            ensure_ascii=False,
        )
        reply(message["id"], {"content": [{"type": "text", "text": text}]})
    else:
        reply(message["id"], {})
