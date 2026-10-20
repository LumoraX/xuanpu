"""真实 CLI 与浏览器验收启动器，保留配置、日志、截图供复查。

用法：uv run python tests/e2e/run_console.py --output /tmp/xuanpu-e2e \
  --playwright-module /path/to/node_modules/playwright --browser /path/to/chromium
额外传 --serve-only 时等候手工/文档验收，Ctrl-C 会回收两个服务。
测试依赖已有 Node/Playwright，不新增任何生产依赖。
"""

from __future__ import annotations

import argparse
import base64
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from xuanpu_server.secrets import generate_master_key
from xuanpu_server.tenancy import generate_root_key
from xuanpu_spec import pack_package

ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    """向内核申请一个本机端口，服务只绑定回环地址。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def ready(url: str, process: subprocess.Popen[bytes]) -> None:
    """限时等候服务就绪，进程早退立即报错。"""
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("验收服务提前退出，请检查输出目录的日志")
        try:
            if httpx.get(url + "/healthz", timeout=1, trust_env=False).is_success:
                return
        except httpx.TransportError:
            time.sleep(0.1)
    raise TimeoutError("服务在 10 秒内未就绪")


def setup_config(output: Path, platform_port: int, mock_port: int) -> tuple[Path, str]:
    """生成临时 memory/local 配置与根凭据；凭据只留在验收目录。"""
    root, digest = generate_root_key()
    mock = f"http://127.0.0.1:{mock_port}"
    config = {
        "data_dir": str(output / "data"), "root_key_hash": digest,
        "master_keys": {"k1": base64.b64encode(generate_master_key()).decode("ascii")}, "primary_master_key": "k1",
        "listen": {"host": "127.0.0.1", "port": platform_port},
        "models": {"base_url": mock + "/v1", "api_key": "mock",
                   "tiers": {tier: {"model": "mock"} for tier in ("fast", "balanced", "deep")}},
        "public_url": f"http://127.0.0.1:{platform_port}", "shutdown_drain_seconds": 1,
        "identity": {"cookie_secure": False, "default_tenant": "acme",
                     "feishu_open_base": mock, "feishu_accounts_base": mock},
    }
    path = output / "config.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    return path, root


def bootstrap(base: str, root: str, output: Path) -> dict[str, str]:
    """只经真实 HTTP API 建租户、管理员 key、保管库密钥、飞书配置和业务包。"""
    with httpx.Client(base_url=base, trust_env=False, timeout=20) as client:
        client.headers["Authorization"] = "Bearer " + root
        response = client.post("/v1/admin/tenants", json={"tenant_id": "acme", "name": "验收企业"})
        response.raise_for_status()
        response = client.post("/v1/admin/tenants/acme/keys", json={"name": "验收管理员", "role": "tenant_admin"})
        response.raise_for_status()
        key = response.json()["key"]
        client.headers["Authorization"] = "Bearer " + key
        for name, value in {"app-secret": "demo-secret", "encrypt-key": "demo-encrypt", "verification-token": "demo-token"}.items():
            client.put("/v1/secrets/" + name, json={"value": value}).raise_for_status()
        client.put("/v1/identity/feishu", json={"app_id": "demo-app", "app_secret_ref": "app-secret",
                   "encrypt_key_ref": "encrypt-key", "verification_token_ref": "verification-token"}).raise_for_status()
        package = output / "package"
        skill = package / "skills" / "greet"
        skill.mkdir(parents=True, exist_ok=True)
        (package / "xuanpu.yaml").write_text("apiVersion: xuanpu/v1\nkind: Package\nmetadata:\n  name: demo\n  version: 1.0.0\nspec:\n  skills:\n    entries: [greet]\n  model:\n    tier: balanced\n")
        (skill / "SKILL.md").write_text("---\nname: greet\ndescription: 验收问候\ntype: composite\nentry: true\nchild_skills: [faq]\n---\n你好，欢迎使用玄圃。\n")
        child = package / "skills" / "faq"
        child.mkdir(exist_ok=True)
        (child / "SKILL.md").write_text("---\nname: faq\ndescription: 验收知识\n---\n你好。\n")
        archive = pack_package(package)
        published = client.post("/v1/packages?activate=true", content=archive, headers={"Content-Type": "application/zip"})
        if not published.is_success:
            raise RuntimeError(published.text)
    return {"base": base, "key": key, "root": root}


def main() -> None:
    """后台启动服务、执行浏览器验收，无论成功失败都终止服务。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--playwright-module", default="playwright")
    parser.add_argument("--browser")
    parser.add_argument("--serve-only", action="store_true")
    parser.add_argument("--scenario", choices=("console", "proactive"), default="console")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    platform_port, mock_port = free_port(), free_port()
    config, root = setup_config(output, platform_port, mock_port)
    if args.scenario == "proactive":
        data = json.loads(config.read_text())
        data.update(proactive_scan_seconds=1, proactive_interval_seconds=60)
        config.write_text(json.dumps(data))
    processes = []
    logs = []
    try:
        commands = [("feishu", [sys.executable, str(Path(__file__).with_name("mock_feishu.py")), str(mock_port)]),
                    ("platform", ["uv", "run", "xuanpu", "serve", "--config", str(config)])]
        for name, command in commands:
            log = (output / (name + ".log")).open("wb")
            logs.append(log)
            processes.append(subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT))
        base = f"http://127.0.0.1:{platform_port}"
        mock = f"http://127.0.0.1:{mock_port}"
        ready(mock, processes[0])
        ready(base, processes[1])
        values = {**bootstrap(base, root, output), "mock": mock}
        state = output / "state.json"
        state.write_text(json.dumps(values))
        state.chmod(0o600)
        print(f"平台 {base}/console/；验收状态 {state}", flush=True)
        if args.serve_only:
            while all(process.poll() is None for process in processes):
                time.sleep(1)
        else:
            subprocess.run(["node", str(Path(__file__).with_name(args.scenario + ".cjs")), str(state),
                            args.playwright_module, args.browser or ""], check=True, timeout=180)
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
