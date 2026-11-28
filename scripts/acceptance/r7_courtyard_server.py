"""R7 小院本机试用服务：起一个接真实模型的玄圃，供浏览器或桌面客户端登录后亲手试小院。

复用 R5 真实模型环境（``r5_real_model_harness``）：内存平台（退出即清空）、本机沙盒、样例业务包
``support-desk`` 与两位副本（会诊用）、工单接口替身；另有订单助手 ``order-desk`` 与报销助手
``expense-helper``，连 ``examples/demo-office-api`` 的样例办公系统替身。租户 ``acme`` 开启执行追踪、管家与极简入口。
开通一名用户名口令员工，登录后直接进入极简入口。只监听 loopback；模型配置只从 ``--model-env``
读取，凭据不写进输出。Ctrl-C 退出。

    uv run python scripts/acceptance/r7_courtyard_server.py --model-env .env
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT.parent / "lumorax-catalog"
sys.path.insert(0, str(ROOT))

TRY_THESE = (
    "今天下午2点30，我需要到半岛酒店，从我现在的位置出发，帮我看下路线和天气，推算什么时候出发并设置闹钟提醒我出发",
    "还有哪些订单没发货？哪几张最急？",
    "我想报销昨天的客户午餐 186 元，没有发票。（会停下等你批准提交）",
    "帮我查一下工单 T-1001 现在什么状态、该多久处理完，查完简单总结给我。",
    "把工单 T-1002 的优先级调到 high，改完告诉我结果。（会停下等你批准）",
    "请安排三位匠人会诊：工单 T-1001 该不该升级处理？请一线客服、二线技术和质检各自独立给出结论，"
    "全部给完后再比对，不要只交给一位。",
)


async def account(
    platform: object,
    username: str,
    password: str,
    *,
    display_name: str = "小院试用员工",
    role: str = "tenant_member",
) -> bool:
    """以租户管理员身份开通用户名口令账号并设好口令；返回口令是否符合平台规则。

    合规的口令走正常的设置链接兑现。不合规的（如默认的 demo/demo：短于 10 位、与用户名相同）
    只在这个本机内存试用服务里直接写入摘要，平台本身的口令规则不变。
    """
    import time

    from server.core.errors import InvalidRequestError
    from server.domains.identity.passwords import validate_new_password
    from server.core.tenancy import Principal

    admin = Principal(role="tenant_admin", tenant_id="acme", key_id="r7-dev", user_id=None)
    accounts = platform.identity.accounts  # type: ignore[attr-defined]
    user, link = await accounts.create_account(
        admin, username=username, display_name=display_name, role=role
    )
    try:
        validate_new_password(password, username=username)
    except InvalidRequestError:
        credentials = platform.identity.store.credentials  # type: ignore[attr-defined]
        await credentials.set_password("acme", user.user_id, await accounts.hashing.hash(password),
                                       at=time.time())
        return False
    token = urlparse(link.url).fragment.split("=", 1)[1]
    await accounts.redeem_link(token, password)
    return True


# 样例办公系统替身（订单、报销）的公开演示凭据，与 examples/demo-office-api/server.py 一致
OFFICE_TOKEN = "demo-token"  # noqa: S105
DEMO_AMAP_KEY = "demo-amap-key"  # noqa: S105
DEMO_ARTISANS = ("order-desk", "expense-helper")
TRAVEL_ARTISANS = ("travel-planner", "driver-dispatch")


async def publish_demo(origin: str, key: str, work_dir: Path, *, demo_map: bool = False) -> None:
    """发布订单、报销、通用技能库及出行/司机匠人。"""
    import shutil

    import yaml
    from sdk import XuanpuClient
    from spec.archive import pack_package

    async with XuanpuClient(origin, key) as admin:
        await admin.secrets.put("office-token", OFFICE_TOKEN)
        for name in DEMO_ARTISANS:
            await admin.packages.publish(pack_package(CATALOG / "artisans" / name), activate=True)
        # 共享通用技能库（高德地图/定位/天气/出发窗口）：默认直连真实高德，--demo-map 切本地替身
        common_dir = CATALOG / "skills" / "common-skills"
        if demo_map:
            await admin.secrets.put("amap-key", DEMO_AMAP_KEY)
            patched = work_dir / "common-skills-demo"
            shutil.copytree(common_dir, patched, dirs_exist_ok=True)
            manifest_path = patched / "xuanpu.yaml"
            data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            data["spec"]["connectors"][0]["baseUrl"] = "http://127.0.0.1:8433"
            manifest_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            await admin.packages.publish(pack_package(patched), activate=True)
        else:
            await admin.packages.publish(pack_package(common_dir), activate=True)
        for name in TRAVEL_ARTISANS:
            await admin.packages.publish(pack_package(CATALOG / "artisans" / name), activate=True)


async def serve(args: argparse.Namespace) -> None:
    """起平台、开通环境与账号，然后一直服务到 Ctrl-C。"""
    from web import static_dir
    from server.api.app import create_app

    from scripts.acceptance import r5_real_model_harness as harness
    from scripts.acceptance.finance_browser import listen

    env = harness.load_model_env(args.model_env)
    origin = f"http://127.0.0.1:{args.port}"
    password = args.password
    tickets = subprocess.Popen([sys.executable, str(ROOT / "examples" / "ticket-api" / "server.py")])  # noqa: S603
    office = subprocess.Popen([sys.executable, str(ROOT / "examples" / "demo-office-api" / "server.py")])  # noqa: S603
    children = [tickets, office]
    if args.demo_map:
        children.append(subprocess.Popen([sys.executable, str(ROOT / "examples" / "demo-map-api" / "server.py")]))  # noqa: S603
    try:
        with tempfile.TemporaryDirectory() as raw:
            async with AsyncExitStack() as resources:
                platform, root_key = await harness.assemble(Path(raw), env, resources, origin=origin,
                                                           default_tenant="acme")
                from server.domains.marketplace import MarketplaceConfig, MarketplaceService

                platform.marketplace = MarketplaceService(
                    platform.packages,
                    platform.vault,
                    config=MarketplaceConfig(catalog_dirs=(CATALOG, ROOT / "examples")),
                )
                async with listen(create_app(platform, console_dir=static_dir()), args.port):
                    key, _ = await harness.provision(platform, origin, root_key)
                    await harness.publish_variants(origin, key, Path(raw))
                    await publish_demo(origin, key, Path(raw), demo_map=args.demo_map)
                    compliant = await account(platform, args.username, password)
                    await account(platform, "admin", "admin", display_name="租户管理员", role="tenant_admin")
                    map_note = (
                        "本地离线地图替身（已自动写入 demo-amap-key）"
                        if args.demo_map
                        else "高德在线接口（请用管理员登录管理入口 →「密钥」写入 amap-key）"
                    )
                    print(f"""
==================== 小院试用服务已就绪 ====================
模型        {env['LLM_BOOTSTRAP_MODEL']}
员工入口    {origin}/
工作台入口  {origin}/workspace
管理入口    {origin}/admin
租户        acme
员工账号    {args.username} / {password}{"" if compliant else "（仅本机试用）"}
管理员账号  admin / admin（可在顶部「密钥」页配置 amap-key）
地图模式    {map_note}

可以试的话：""" + "".join(f"\n  · {t}" for t in TRY_THESE) + """

内存平台，退出即清空。Ctrl-C 退出。
==========================================================""", flush=True)
                    await asyncio.Event().wait()
    finally:
        for child in children:
            child.terminate()
            child.wait(timeout=10)


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-env", type=Path, required=True, help="私有模型配置（dotenv，权限 600）")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--username", default="demo")
    parser.add_argument("--password", default="demo", help="默认 demo（仅本机试用）")
    parser.add_argument("--demo-map", action="store_true", help="使用本地离线高德兼容替身服务，无需真实高德 Key")
    args = parser.parse_args()
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
