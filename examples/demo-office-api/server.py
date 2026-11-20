"""样例办公系统：给 examples/order-desk 与 examples/expense-helper 的 HTTP 连接器当被调用方。

只用标准库，数据在内存里，进程退出即丢失。仅用于本机试用小院（R7）。

    python3 examples/demo-office-api/server.py        # 监听 127.0.0.1:8432
    OFFICE_TOKEN=secret python3 examples/demo-office-api/server.py
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

TOKEN = os.environ.get("OFFICE_TOKEN", "demo-token")
TODAY = date.today()


def _ago(days: int) -> str:
    """几天前的日期。"""
    return (TODAY - timedelta(days=days)).isoformat()


ORDERS = {
    "O-2001": {"id": "O-2001", "customer": "云杉科技", "item": "工牌打印机 ×2", "amount": 3600,
               "paid_on": _ago(4), "status": "paid", "urgent": False, "tracking": None},
    "O-2002": {"id": "O-2002", "customer": "青柳设计", "item": "会议麦克风 ×5", "amount": 2450,
               "paid_on": _ago(6), "status": "shipped", "urgent": False, "tracking": "SF1234567890"},
    "O-2003": {"id": "O-2003", "customer": "云杉科技", "item": "条码扫描枪 ×10", "amount": 5800,
               "paid_on": _ago(1), "status": "paid", "urgent": True, "tracking": None},
    "O-2004": {"id": "O-2004", "customer": "北辰物流", "item": "热敏标签纸 ×40 卷", "amount": 960,
               "paid_on": _ago(2), "status": "paid", "urgent": False, "tracking": None},
    "O-2005": {"id": "O-2005", "customer": "青柳设计", "item": "无线投屏器 ×3", "amount": 1770,
               "paid_on": _ago(9), "status": "shipped", "urgent": False, "tracking": "YT9876543210"},
}
EXPENSES = {
    "E-3001": {"id": "E-3001", "title": "客户拜访打车", "amount": 46, "spent_on": _ago(8),
               "has_invoice": True, "status": "paid"},
    "E-3002": {"id": "E-3002", "title": "团队午餐", "amount": 380, "spent_on": _ago(3),
               "has_invoice": True, "status": "submitted"},
}


class Handler(BaseHTTPRequestHandler):
    """订单：列出、查询、发货报表、标记发货；报销：列出、提交。"""

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

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:  # noqa: N802 —— 方法名由标准库规定
        if not self._authorized():
            return
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if parts.path == "/orders":
            status = query.get("status", [None])[0]
            self._send(200, {"orders": [o for o in ORDERS.values() if status in (None, o["status"])]})
        elif parts.path == "/reports/shipping":
            waiting = [o for o in ORDERS.values() if o["status"] == "paid"]
            days = [(TODAY - date.fromisoformat(o["paid_on"])).days for o in waiting]
            self._send(200, {"today": TODAY.isoformat(), "waiting": len(waiting),
                             "shipped": sum(o["status"] == "shipped" for o in ORDERS.values()),
                             "urgent_waiting": sum(o["urgent"] for o in waiting),
                             "longest_wait_days": max(days, default=0)})
        elif parts.path.removeprefix("/orders/") in ORDERS:
            self._send(200, ORDERS[parts.path.removeprefix("/orders/")])
        elif parts.path == "/expenses":
            self._send(200, {"expenses": list(EXPENSES.values())})
        else:
            self._send(404, {"error": "不存在"})

    def do_PATCH(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        order = ORDERS.get(urlsplit(self.path).path.removeprefix("/orders/"))
        if order is None:
            self._send(404, {"error": "订单不存在"})
            return
        patch = self._body()
        if patch.get("status") == "shipped":
            order.update(status="shipped", tracking=patch.get("tracking") or "待补")
        self._send(200, order)

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        if urlsplit(self.path).path != "/expenses":
            self._send(404, {"error": "不存在"})
            return
        claim = self._body()
        new_id = f"E-{3001 + len(EXPENSES)}"
        EXPENSES[new_id] = {"id": new_id, "title": str(claim.get("title", "")),
                            "amount": claim.get("amount"), "spent_on": claim.get("spent_on") or TODAY.isoformat(),
                            "has_invoice": bool(claim.get("has_invoice")), "status": "submitted",
                            "note": str(claim.get("note", ""))}
        self._send(201, EXPENSES[new_id])


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8432), Handler).serve_forever()
