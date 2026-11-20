"""R4 真实模型验收：知识问答三问（场景 A）与办公样板四组（场景 B），逐用例存原始结果并确定性判定。

平台：内存控制面、仅监听 loopback；租户 ``acme`` 开启 knowledge、capability_delivery 与持久执行
（能力交办走持久任务）。沙盒默认自动选择：本机 Docker 可用时用办公镜像（``--office-image``，
由 ``docker/office-sandbox.Dockerfile`` 构建），否则退回本机沙盒，场景 B 中需要容器的交办整体标记
``not_executed``（评测与发布链路仍照常跑真实模型）。

模型配置只从 ``--model-env`` 的私有 dotenv 读（与 ``r5_real_model_harness.py`` 同一组键），
不读进程环境变量；凭据与网关地址不写进任何输出。

员工：管理员（经正式接口登记、安装、分配、评测、发布）、知识问答员工、办公员工各一名；
一把可代员工调用的租户管理员 key，代调用时生效角色取两者较低者。用法：

    uv run python scripts/acceptance/r4_real_model_acceptance.py \
        --model-env <私有 .env> --out <目录>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
OFFICE_IMAGE = "xuanpu-office:r1-20261021"
OFFICE_CASES = ("basic", "invalid", "injection", "missing_group")


def docker_status(binary: str | None) -> tuple[bool, str]:
    """本机 Docker 守护进程是否可用；不可用时给出原因（不当作通过）。"""
    if binary is None:
        return False, "找不到 docker 命令"
    try:
        probe = subprocess.run(  # noqa: S603 —— 命令与参数固定，docker 路径来自 PATH 查找
            [binary, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"docker info 失败：{type(exc).__name__}"
    if probe.returncode != 0:
        return False, "docker info 失败：" + probe.stderr.strip().splitlines()[-1][:200]
    return True, "Docker " + probe.stdout.strip()


def sandboxes(choice: str, image: str) -> tuple[Any, bool, str]:
    """按 ``--sandbox`` 选沙盒：docker 须守护进程可用；auto 不可用时退回本机沙盒并说明。"""
    from xuanpu_server.runtime import LocalSandboxProvider
    from xuanpu_server.runtime.sandbox import DockerSandboxProvider

    binary = shutil.which("docker")
    available, reason = (
        docker_status(binary) if choice != "local" else (False, "命令行指定 --sandbox local")
    )
    if choice == "docker" and not available:
        raise SystemExit("指定了 --sandbox docker，但 " + reason)
    if not available or binary is None:
        return LocalSandboxProvider(), False, reason
    provider = DockerSandboxProvider(
        image=image,
        docker_binary=binary,
        memory_mb=512,
        labels={"xuanpu.acceptance": "r4-real-model"},
        docker_env={
            "PATH": "/usr/local/bin:/usr/bin:/bin:" + str(Path(binary).parent),
            "HOME": str(Path.home()),
        },
    )
    return provider, True, reason


async def assemble(
    directory: Path, env: dict[str, str], resources: AsyncExitStack, provider: Any
) -> tuple[Any, str]:
    """内存平台：acme 开启知识、能力交付与持久执行，接真实模型；返回平台与根凭据。"""
    from xuanpu_server.capabilities.config import CapabilityDeliveryConfig
    from xuanpu_server.controlplane import InMemoryControlPlane
    from xuanpu_server.identity import InMemoryIdentityStore
    from xuanpu_server.identity.config import IdentityConfig
    from xuanpu_server.identity.service import IdentitySettings
    from xuanpu_server.knowledge.service import KnowledgeConfig
    from xuanpu_server.objectstore import LocalObjectStore
    from xuanpu_server.platform import Platform
    from xuanpu_server.secrets import EncryptedVault, generate_master_key
    from xuanpu_server.tenancy import generate_root_key
    from xuanpu_server.work_tasks.config import DurableExecutionConfig

    from scripts.acceptance.r5_real_model_harness import gateway

    store = InMemoryControlPlane()
    root_key, root_hash = generate_root_key()
    vault = EncryptedVault(store, keys={"k1": generate_master_key()}, primary="k1")
    (directory / "data").mkdir()
    platform = Platform(
        store=store,
        identity_store=InMemoryIdentityStore(store),
        identity_settings=IdentitySettings.from_config(None, IdentityConfig(cookie_secure=False)),
        vault=vault,
        models=gateway(env, vault),
        sandboxes=provider,
        data_dir=directory / "data",
        root_key_hash=root_hash,
        instance_id="inst-r4-real-model",
        objects=LocalObjectStore(directory / "objects"),
        knowledge=KnowledgeConfig(enabled=True, tenants=("acme",)),
        capability_delivery=CapabilityDeliveryConfig(enabled=True, tenants=("acme",)),
        durable_execution=DurableExecutionConfig(
            enabled=True, tenant_ids=("acme",), verified_text_bounds=True
        ),
    )
    resources.push_async_callback(platform.close)
    await platform.start()
    return platform, root_key


async def provision(platform: Any, base_url: str, root_key: str) -> tuple[str, dict[str, str]]:
    """经正式接口开通合成租户、签发代调用 key；建管理员与两名成员员工。"""
    from xuanpu_sdk import XuanpuClient

    from scripts.acceptance.r5_real_model_harness import new_employee

    async with XuanpuClient(base_url, root_key) as root:
        await root.admin.create_tenant("acme", "R4真实模型验收租户")
        issued = await root.admin.issue_tenant_key(
            "acme", "r4-acceptance", role="tenant_admin", act_as_users=True
        )
    users = {
        "admin": await admin_employee(platform, "R4验收管理员"),
        "knowledge": await new_employee(platform, "R4验收员工-知识"),
        "office": await new_employee(platform, "R4验收员工-办公"),
    }
    return issued.key, users


async def admin_employee(platform: Any, name: str) -> str:
    """建一名启用的管理员员工（能力登记、安装与发布须以员工身份操作）。"""
    import time

    from xuanpu_server.identity import UserRecord, new_user_id

    user = UserRecord(
        tenant_id="acme",
        user_id=new_user_id(),
        display_name=name,
        created_at=time.time(),
        role="tenant_admin",
    )
    await platform.identity.store.create_user(user)
    return user.user_id


def principal(user_id: str) -> Any:
    """验收成员员工的身份。"""
    from xuanpu_server.tenancy import Principal

    return Principal(role="tenant_member", tenant_id="acme", key_id=None, user_id=user_id)


def acting(base_url: str, key: str, user_id: str) -> Any:
    """代某名员工调用的 HTTP 客户端（真实模型回答慢，超时放宽）。"""
    import httpx

    return httpx.AsyncClient(
        base_url=base_url,
        timeout=600.0,
        headers={"Authorization": f"Bearer {key}", "X-Xuanpu-Act-As": user_id},
    )


async def scenarios(
    args: argparse.Namespace,
    platform: Any,
    base_url: str,
    key: str,
    users: dict[str, str],
    docker: bool,
) -> dict[str, Any]:
    """依次跑选定场景；单个场景异常如实记录，不挡另一个。"""
    from scripts.acceptance.r4_knowledge_cases import scenario_knowledge
    from scripts.acceptance.r4_office_cases import scenario_office

    results: dict[str, Any] = {}
    if "knowledge" in args.only:
        async with acting(base_url, key, users["knowledge"]) as employee:
            try:
                results["knowledge"] = await scenario_knowledge(employee)
            except Exception as exc:  # 前置步骤失败：记原因，判失败
                results["knowledge"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(
            "knowledge",
            json.dumps(
                {k: v.get("checks") for k, v in results["knowledge"].get("cases", {}).items()},
                ensure_ascii=False,
            )[:600],
            flush=True,
        )
    if "office" in args.only:
        record = await platform.identity.store.get_user("acme", users["office"])
        async with (
            acting(base_url, key, users["admin"]) as admin,
            acting(base_url, key, users["office"]) as employee,
        ):
            try:
                results["office"] = await scenario_office(
                    platform,
                    admin,
                    employee,
                    principal(users["office"]),
                    int(record.lifecycle_epoch),
                    docker,
                    args.timeout,
                    tuple(args.office_cases),
                )
            except Exception as exc:  # 发布链路失败：记原因，判失败
                results["office"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return results


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """读私有模型配置、选沙盒、起平台并开通，跑场景。"""
    from xuanpu_server.api.app import create_app

    from scripts.acceptance.finance_browser import free_port, listen
    from scripts.acceptance.r5_real_model_harness import load_model_env

    env = load_model_env(args.model_env)
    provider, docker, reason = sandboxes(args.sandbox, args.office_image)
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    summary: dict[str, Any] = {
        "model": env["LLM_BOOTSTRAP_MODEL"],
        "protocol": env["LLM_BOOTSTRAP_PROTOCOL"],
        "sandbox": "docker" if docker else "local",
        "docker": reason,
        "office_image": args.office_image if docker else None,
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    with tempfile.TemporaryDirectory() as raw:
        async with AsyncExitStack() as resources:
            # macOS 的临时目录经符号链接（/var → /private/var）；容器按原路径挂载业务包，
            # 而脚本按解析后的路径启动，两边不一致会找不到脚本目录，这里先解析成真实路径
            directory = await asyncio.to_thread(Path(raw).resolve)
            platform, root_key = await assemble(directory, env, resources, provider)
            async with listen(create_app(platform), port):
                key, users = await provision(platform, base_url, root_key)
                summary.update(await scenarios(args, platform, base_url, key, users, docker))
    summary["ok"] = all(summary[name].get("ok") is True for name in args.only if name in summary)
    return summary


def main() -> None:
    """命令行入口：结果写到 ``--out/r4.json``。"""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--model-env", type=Path, required=True, help="私有模型配置（dotenv，权限 600）"
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--only", nargs="+", choices=("knowledge", "office"), default=["knowledge", "office"]
    )
    parser.add_argument(
        "--office-cases", nargs="+", choices=OFFICE_CASES, default=list(OFFICE_CASES)
    )
    parser.add_argument("--sandbox", choices=("auto", "docker", "local"), default="auto")
    parser.add_argument("--office-image", default=OFFICE_IMAGE, help="办公执行镜像标签")
    parser.add_argument(
        "--timeout", type=float, default=900.0, help="单个评测或交办等终态的上限（秒）"
    )
    args = parser.parse_args()
    summary = asyncio.run(run(args))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "r4.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    )
    print(f"ok={summary['ok']}")


if __name__ == "__main__":
    main()
