"""``xuanpu serve``：把平台服务作为真实进程启动，经网络访问。"""

from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import httpx
import pytest
from xuanpu_server.tenancy import generate_root_key

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def free_port() -> int:
    """取一个空闲端口。"""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[str, str]]:
    """启动服务进程，返回 ``(地址, 根凭据)``；用例结束时发终止信号并确认它正常退出。"""
    root_key, root_hash = generate_root_key()
    port = free_port()
    config = tmp_path / "xuanpu.yaml"
    config.write_text(
        json.dumps(
            {
                "data_dir": str((tmp_path / "data").resolve()),
                "root_key_hash": root_hash,
                "master_keys": {"k1": base64.b64encode(b"k" * 32).decode()},
                "primary_master_key": "k1",
                "listen": {"port": port},
                "models": {
                    "base_url": "http://127.0.0.1:1/v1",
                    "api_key": "${GATEWAY_KEY}",
                    "tiers": {
                        "fast": {"model": "s"},
                        "balanced": {"model": "m"},
                        "deep": {"model": "l"},
                    },
                },
            }
        )
    )
    process = subprocess.Popen(  # noqa: S603 —— 参数全部来自测试代码
        [sys.executable, "-m", "xuanpu_cli.main", "serve", "--config", str(config)],
        env={**os.environ, "GATEWAY_KEY": "sk-test"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            if process.poll() is not None:
                raise RuntimeError(f"服务进程提前退出：\n{process.stdout.read()}")  # type: ignore[union-attr]
            try:
                if httpx.get(url + "/healthz", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("服务进程没有在 30 秒内就绪")
            time.sleep(0.1)
        yield url, root_key
    finally:
        process.send_signal(signal.SIGTERM)
        try:
            output, _ = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            raise
        # uvicorn 优雅关闭之后会把终止信号重新抛出，所以退出码是「被该信号终止」；
        # 是否优雅，看关闭流程有没有走完
        assert process.returncode in (0, -signal.SIGTERM), output
        assert "Application shutdown complete" in output, output


def test_serve_end_to_end(served: tuple[str, str]) -> None:
    """真实进程：存活探测、用根凭据开通租户、租户凭据可用、错误凭据被拒。"""
    url, root_key = served
    root = {"Authorization": f"Bearer {root_key}"}
    with httpx.Client(base_url=url, timeout=10) as http:
        assert http.get("/healthz").json()["status"] == "ok"
        assert http.get("/v1/admin/tenants").status_code == 401

        created = http.post(
            "/v1/admin/tenants", headers=root, json={"tenant_id": "acme", "name": "Acme"}
        )
        assert created.status_code == 201, created.text
        issued = http.post(
            "/v1/admin/tenants/acme/keys",
            headers=root,
            json={"name": "admin", "role": "tenant_admin"},
        )
        admin = {"Authorization": f"Bearer {issued.json()['key']}"}
        assert http.get("/v1/sessions", headers=admin).json() == {"sessions": []}
        assert (
            http.put("/v1/secrets/crm-token", headers=admin, json={"value": "v"}).status_code == 200
        )
        kinds = [r["kind"] for r in http.get("/v1/audit", headers=admin).json()["records"]]
        assert kinds == ["tenant_created", "api_key_issued", "secret_written"]
