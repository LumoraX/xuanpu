"""``xuanpu serve``：把平台服务作为真实进程启动，经网络访问。"""

from __future__ import annotations

import base64
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from tempfile import TemporaryFile
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi import FastAPI
from xuanpu_cli.main import FEISHU_CALLBACK_PATH, CallbackQueryFilter, uvicorn_config
from xuanpu_server.config import ServerConfig
from xuanpu_server.identity.urls import CALLBACK_PATH
from xuanpu_server.tenancy import generate_root_key

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def server_config(tmp_path: Path, **overrides: object) -> ServerConfig:
    """一份最小的合法配置，``overrides`` 覆盖顶层的键。"""
    return ServerConfig.model_validate(
        {
            "data_dir": str(tmp_path.resolve()),
            "root_key_hash": "0" * 64,
            "master_keys": {"k1": base64.b64encode(b"k" * 32).decode()},
            "primary_master_key": "k1",
            "models": {
                "base_url": "http://127.0.0.1:1/v1",
                "api_key": "sk-test",
                "tiers": {
                    "fast": {"model": "s"},
                    "balanced": {"model": "m"},
                    "deep": {"model": "l"},
                },
            },
            **overrides,
        }
    )


def _access_record(path: str) -> logging.LogRecord:
    """uvicorn 写访问日志时那样的一条记录。"""
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:5000", "GET", path, "1.1", 302),
        None,
    )


@pytest.mark.parametrize(
    ("path", "logged"),
    [
        (
            "/v1/auth/feishu/callback?code=secret-code&state=s1",
            '10.0.0.1:5000 - "GET /v1/auth/feishu/callback?… HTTP/1.1" 302',
        ),
        (
            "/v1/auth/feishu/callback",
            '10.0.0.1:5000 - "GET /v1/auth/feishu/callback HTTP/1.1" 302',
        ),
        (
            "/v1/auth/feishu/login?tenant=acme",
            '10.0.0.1:5000 - "GET /v1/auth/feishu/login?tenant=acme HTTP/1.1" 302',
        ),
        (
            "/v1/sessions?code=x",
            '10.0.0.1:5000 - "GET /v1/sessions?code=x HTTP/1.1" 302',
        ),
    ],
)
def test_access_log_hides_the_callback_query(path: str, logged: str) -> None:
    """访问日志里飞书登录回调的查询串（授权码、state）抹成 ``?…``，别的请求行原样。"""
    record = _access_record(path)
    assert CallbackQueryFilter().filter(record) is True
    assert record.getMessage() == logged
    assert FEISHU_CALLBACK_PATH == CALLBACK_PATH


def test_serve_installs_the_access_log_filter(tmp_path: Path) -> None:
    """构造 HTTP 服务器配置时给 ``uvicorn.access`` 装上过滤器，装一次（重复构造也只有一个）。"""
    uvicorn_config(FastAPI(), server_config(tmp_path))
    uvicorn_config(FastAPI(), server_config(tmp_path))
    access = logging.getLogger("uvicorn.access")
    assert [type(item) for item in access.filters].count(CallbackQueryFilter) == 1


@pytest.mark.parametrize(("drain", "expected"), [(30.0, 35), (0.0, 5), (7.5, 13)])
def test_graceful_shutdown_has_a_deadline(tmp_path: Path, drain: float, expected: int) -> None:
    """停机时等已打开的响应（事件流）发完有时限：``shutdown_drain_seconds`` 再加 5 秒。

    uvicorn 的这一项只收整秒：带小数的向上取整，只会多等、不会少等。
    """
    listen = {"host": "10.0.0.8", "port": 9000}
    config = server_config(tmp_path, shutdown_drain_seconds=drain, listen=listen)
    app = FastAPI()
    built = uvicorn_config(app, config)
    assert built.timeout_graceful_shutdown == expected
    assert (built.app, built.host, built.port) == (app, "10.0.0.8", 9000)
    assert built.log_level == "info"


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
    # 持续承接日志，避免未读取的有限管道阻塞服务器发送响应。
    with TemporaryFile(mode="w+t", encoding="utf-8") as logs:
        process = subprocess.Popen(  # noqa: S603 —— 参数全部来自测试代码
            [sys.executable, "-m", "xuanpu_cli.main", "serve", "--config", str(config)],
            env={**os.environ, "GATEWAY_KEY": "sk-test"},
            stdout=logs,
            stderr=subprocess.STDOUT,
            text=True,
        )
        url = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None:
                    logs.seek(0)
                    raise RuntimeError(f"服务进程提前退出：\n{logs.read()}")
                try:
                    if httpx.get(url + "/healthz", timeout=1).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() > deadline:
                    logs.seek(0)
                    raise RuntimeError(f"服务进程没有在 30 秒内就绪：\n{logs.read()}")
                time.sleep(0.1)
            yield url, root_key
        finally:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
            logs.seek(0)
            output = logs.read()
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


def test_service_entry_has_one_worker_even_when_environment_requests_more(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """受支持服务入口固定一个进程，外部 worker 环境值不能扩大执行面。"""
    from fastapi import FastAPI
    monkeypatch.setenv("WEB_CONCURRENCY", "3")
    built = uvicorn_config(FastAPI(), server_config(tmp_path))
    assert built.workers == 1
