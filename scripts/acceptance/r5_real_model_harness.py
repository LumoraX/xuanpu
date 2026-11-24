"""R5 真实模型验收的本机环境：起一个内存平台，接真实模型，跑管家回归、审批写入与三成员会诊。

模型配置只从 ``--model-env`` 指定的私有 dotenv 读取（键与真实模型测试一致：
``LLM_BOOTSTRAP_PROTOCOL``、``LLM_BOOTSTRAP_BASE_URL``、``LLM_BOOTSTRAP_API_KEY``、
``LLM_BOOTSTRAP_MODEL``），不读进程环境变量，凭据不写进输出。

平台：内存控制面、本机沙盒、仅监听 loopback；租户 ``acme`` 开启执行追踪、管家与极简入口，
发布样例业务包 ``support-desk`` 并起样例工单接口替身；一名测试员工，一把可代员工调用的
租户管理员 key（代调用时生效角色取较低者，即成员）。之后以子进程方式运行
``r5_butler_regression.py`` 与 ``r5_consult_acceptance.py``，各自输出 JSON 到 ``--out``。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
ACCEPTANCE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
REQUIRED = ("LLM_BOOTSTRAP_PROTOCOL", "LLM_BOOTSTRAP_BASE_URL", "LLM_BOOTSTRAP_API_KEY",
            "LLM_BOOTSTRAP_MODEL")


def load_model_env(path: Path) -> dict[str, str]:
    """读取私有模型配置；缺项直接报错，不用默认值顶替。"""
    raw = dotenv_values(path)
    missing = [name for name in REQUIRED if not raw.get(name)]
    if missing:
        raise SystemExit(f"{path} 缺少：{', '.join(missing)}")
    return {name: str(raw[name]) for name in REQUIRED}


def gateway(env: dict[str, str], vault: Any) -> Any:
    """按私有配置建真实模型网关；三档都用同一个模型。"""
    from server.engine.runtime.models import GatewayConfig, GatewayModelClientFactory, ModelProfile

    profile = ModelProfile(env["LLM_BOOTSTRAP_MODEL"], timeout_seconds=300)
    config = GatewayConfig(
        base_url=env["LLM_BOOTSTRAP_BASE_URL"], api_key=env["LLM_BOOTSTRAP_API_KEY"],
        protocol=env["LLM_BOOTSTRAP_PROTOCOL"],  # type: ignore[arg-type]
        profiles={tier: profile for tier in ("fast", "balanced", "deep")},
    )
    return GatewayModelClientFactory(config, vault)


async def assemble(directory: Path, env: dict[str, str], resources: AsyncExitStack,
                   origin: str | None = None, connections: bool = False,
                   default_tenant: str | None = None) -> tuple[Any, str]:
    """内存平台：acme 开启追踪、管家与极简入口，接真实模型；返回平台与根凭据。

    ``origin`` 给了时平台认浏览器登录 cookie（浏览器验收用）；不给只认 API key。
    ``connections`` 为真时 acme 开启企业连接（R3），对话里才能打开本人连接页。
    ``default_tenant`` 给了时登录页不带 ``?tenant=`` 也登录到这个租户（本机试用服务用）。
    """
    from server.engine.butler.config import ButlerConfig
    from server.domains.connections.models import ConnectionConfig
    from server.core.controlplane import InMemoryControlPlane
    from server.domains.dashboard.config import MinimalEntryConfig
    from server.domains.identity import InMemoryIdentityStore
    from server.domains.identity.config import IdentityConfig
    from server.domains.identity.service import IdentitySettings
    from server.core.objectstore import LocalObjectStore
    from server.platform import Platform
    from server.engine.runtime import LocalSandboxProvider, RuntimeDefaults
    from server.core.secrets import EncryptedVault, generate_master_key
    from server.core.tenancy import generate_root_key
    from server.engine.tracing.config import ExecutionTraceConfig

    store = InMemoryControlPlane()
    root_key, root_hash = generate_root_key()
    vault = EncryptedVault(store, keys={"k1": generate_master_key()}, primary="k1")
    (directory / "data").mkdir()
    platform = Platform(
        store=store, identity_store=InMemoryIdentityStore(store),
        identity_settings=IdentitySettings.from_config(
            origin, IdentityConfig(cookie_secure=False, default_tenant=default_tenant)),
        vault=vault, models=gateway(env, vault), sandboxes=LocalSandboxProvider(),
        data_dir=directory / "data", root_key_hash=root_hash, instance_id="inst-r5-real-model",
        objects=LocalObjectStore(directory / "objects"),
        defaults=RuntimeDefaults(execution_trace=ExecutionTraceConfig(enabled=True, tenants=("acme",))),
        butler=ButlerConfig(enabled=True, tenants=("acme",)),
        minimal_entry=MinimalEntryConfig(enabled=True, tenants=("acme",)),
        connections=ConnectionConfig(enabled=connections, tenants=("acme",) if connections else ()),
    )
    resources.push_async_callback(platform.close)
    await platform.start()
    return platform, root_key


async def provision(platform: Any, base_url: str, root_key: str) -> tuple[str, str]:
    """经正式接口开通租户、签发代调用 key、存密钥并发布样例包；建一名员工。返回 key 与员工标识。"""
    from sdk import XuanpuClient
    from spec.archive import pack_package

    async with XuanpuClient(base_url, root_key) as root:
        await root.admin.create_tenant("acme", "R5真实模型验收租户")
        issued = await root.admin.issue_tenant_key("acme", "r5-acceptance", role="tenant_admin",
                                                   act_as_users=True)
    async with XuanpuClient(base_url, issued.key) as admin:
        await admin.secrets.put("tickets-token", TICKETS_TOKEN)
        await admin.packages.publish(pack_package(ROOT / "examples" / "support-desk"), activate=True)
    return issued.key, await new_employee(platform, "验收员工甲")


async def new_employee(platform: Any, name: str) -> str:
    """建一名启用的成员员工（员工只由飞书登录产生，验收环境直接经身份存储建）。"""
    from server.domains.identity import UserRecord, new_user_id

    user = UserRecord(tenant_id="acme", user_id=new_user_id(), display_name=name,
                      created_at=time.time(), role="tenant_member")
    await platform.identity.store.create_user(user)
    return user.user_id


# 样例工单接口替身的固定地址与凭据（examples/ticket-api/server.py）
TICKETS_URL = "http://127.0.0.1:8431"
TICKETS_TOKEN = "demo-token"  # noqa: S105 —— 样例替身的公开演示凭据
CONSULT_TEXT = ("请安排三位匠人会诊：工单 T-1001 该不该升级处理？请一线客服、二线技术和质检各自独立给出结论，"
                "全部给完后再比对，不要只交给一位。")
# 会诊需要三位不同的匠人：样例包的两个副本，只改包名、显示名与说明，连接器与权限完全相同。
CONSULT_VARIANTS = (
    ("support-tier2", "二线技术", "Tier-2 Support",
     "二线技术支持：从技术角度评估工单的影响面与是否需要升级"),
    ("support-qa", "工单质检", "Support QA",
     "工单质检：从服务质量与时限角度评估工单处理是否到位、是否需要升级"),
)


async def publish_variants(base_url: str, key: str, workdir: Path) -> list[str]:
    """会诊前再发布两位工单匠人（样例包副本）；回归集只针对 support-desk，故放在回归之后。"""
    import shutil

    from sdk import XuanpuClient
    from spec.archive import pack_package

    names = []
    async with XuanpuClient(base_url, key) as admin:
        for name, zh_name, en_name, description in CONSULT_VARIANTS:
            target = workdir / name
            shutil.copytree(ROOT / "examples" / "support-desk", target)
            manifest = target / "xuanpu.yaml"
            text = manifest.read_text()
            for old, new in (("name: support-desk", f"name: {name}"),
                             ("description: 客服工单助手：查工单、给出处理时限、改派优先级",
                              f"description: {description}"),
                             ("    zh-CN: 工单助手\n    en-US: Support Desk",
                              f"    zh-CN: {zh_name}\n    en-US: {en_name}")):
                if old not in text:
                    raise SystemExit(f"样例包清单已变，找不到：{old}")
                text = text.replace(old, new, 1)
            manifest.write_text(text)
            await admin.packages.publish(pack_package(target), activate=True)
            names.append(name)
    return names


def run_script(name: str, base_url: str, key_file: Path, user_id: str, out: Path, *extra: str) -> int:
    """以子进程运行一个验收脚本，输出原样转到本进程。"""
    command = [sys.executable, str(ACCEPTANCE / name), "--base-url", base_url,
               "--key-file", str(key_file), "--act-as", user_id, "--out", str(out), *extra]
    print(f"$ {name} --out {out}", flush=True)
    return subprocess.run(command, cwd=ROOT, check=False).returncode  # noqa: S603


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """起平台与工单替身，开通环境，依次跑选定的验收脚本。"""
    from server.api.app import create_app

    from scripts.acceptance.finance_browser import free_port, listen

    env = load_model_env(args.model_env)
    args.out.mkdir(parents=True, exist_ok=True)
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    tickets = subprocess.Popen([sys.executable, str(ROOT / "examples" / "ticket-api" / "server.py")])  # noqa: S603
    codes: dict[str, int] = {}
    try:
        with tempfile.TemporaryDirectory() as raw:
            async with AsyncExitStack() as resources:
                platform, root_key = await assemble(Path(raw), env, resources)
                async with listen(create_app(platform), port):
                    key, user_id = await provision(platform, base_url, root_key)
                    key_file = Path(raw) / "key"
                    key_file.write_text(key)
                    key_file.chmod(0o600)
                    print(f"平台 {base_url}，员工 {user_id}，模型 {env['LLM_BOOTSTRAP_MODEL']}", flush=True)
                    if "regression" in args.only:
                        codes["regression"] = await asyncio.to_thread(
                            run_script, "r5_butler_regression.py", base_url, key_file, user_id,
                            args.out / "regression.json")
                    if "approval" in args.only:
                        # 单独一名员工：回归里的改优先级用例可能留下别的审批提示
                        approver = await new_employee(platform, "验收员工乙")
                        codes["approval"] = await asyncio.to_thread(
                            run_script, "r5_approval_acceptance.py", base_url, key_file, approver,
                            args.out / "approval.json", "--tickets-url", TICKETS_URL,
                            "--tickets-token", TICKETS_TOKEN)
                    if "consult" in args.only:
                        variants = await publish_variants(base_url, key, Path(raw))
                        print(f"会诊匠人：support-desk、{'、'.join(variants)}", flush=True)
                        codes["consult"] = await asyncio.to_thread(
                            run_script, "r5_consult_acceptance.py", base_url, key_file, user_id,
                            args.out / "consult.json", "--timeout", str(args.consult_timeout),
                            "--text", CONSULT_TEXT)
    finally:
        tickets.terminate()
        tickets.wait(timeout=10)
    return {"model": env["LLM_BOOTSTRAP_MODEL"], "protocol": env["LLM_BOOTSTRAP_PROTOCOL"],
            "exit_codes": codes}


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-env", type=Path, required=True, help="私有模型配置（dotenv，权限 600）")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="+", choices=("regression", "approval", "consult"),
                        default=["regression", "approval", "consult"])
    parser.add_argument("--consult-timeout", type=float, default=900.0)
    args = parser.parse_args()
    summary = asyncio.run(run(args))
    (args.out / "harness.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
