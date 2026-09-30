"""样例工单系统：给 examples/support-desk 的 HTTP 连接器当被调用方。

只用标准库，数据在内存里，进程退出即丢失。仅用于跑通文档里的例子。

    python3 examples/ticket-api/server.py            # 监听 127.0.0.1:8431
    TICKETS_TOKEN=secret python3 examples/ticket-api/server.py
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

TOKEN = os.environ.get("TICKETS_TOKEN", "demo-token")
TICKETS = {
    "T-1001": {"id": "T-1001", "title": "无法登录", "priority": "normal", "status": "open"},
    "T-1002": {"id": "T-1002", "title": "发票抬头有误", "priority": "low", "status": "open"},
}


class Handler(BaseHTTPRequestHandler):
    """三个接口：列出、查询、修改。"""

    def _send(self, status: int, body: object) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") == f"Bearer {TOKEN}":
            return True
        self._send(401, {"error": "凭据无效"})
        return False

    def do_GET(self) -> None:  # noqa: N802 —— 方法名由标准库规定
        if not self._authorized():
            return
        parts = urlsplit(self.path)
        if parts.path == "/tickets":
            status = parse_qs(parts.query).get("status", [None])[0]
            found = [t for t in TICKETS.values() if status in (None, t["status"])]
            self._send(200, {"tickets": found})
        elif parts.path.removeprefix("/tickets/") in TICKETS:
            self._send(200, TICKETS[parts.path.removeprefix("/tickets/")])
        else:
            self._send(404, {"error": "工单不存在"})

    def do_PATCH(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        ticket = TICKETS.get(urlsplit(self.path).path.removeprefix("/tickets/"))
        if ticket is None:
            self._send(404, {"error": "工单不存在"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        patch = json.loads(self.rfile.read(length) or b"{}")
        ticket.update({key: patch[key] for key in ("priority", "status") if key in patch})
        self._send(200, ticket)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8431), Handler).serve_forever()
