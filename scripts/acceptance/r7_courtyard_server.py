"""R7 小院本机试用服务：起一个接真实模型的玄圃，供浏览器或桌面客户端登录后亲手试小院。

复用 R5 真实模型环境（``r5_real_model_harness``）：内存平台（退出即清空）、本机沙盒、样例业务包
``support-desk`` 与两位副本（会诊用）、工单接口替身。租户 ``acme`` 开启执行追踪、管家与极简入口。
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
sys.path.insert(0, str(ROOT))

TRY_THESE = (
    "帮我查一下工单 T-1001 现在什么状态、该多久处理完，查完简单总结给我。",
    "把工单 T-1002 的优先级调到 high，改完告诉我结果。（会停下等你批准）",
    "请安排三位匠人会诊：工单 T-1001 该不该升级处理？请一线客服、二线技术和质检各自独立给出结论，"
    "全部给完后再比对，不要只交给一位。",
)


async def account(platform: object, username: str, password: str) -> bool:
    """以租户管理员身份开通用户名口令员工并设好口令；返回口令是否符合平台规则。

    合规的口令走正常的设置链接兑现。不合规的（如默认的 demo/demo：短于 10 位、与用户名相同）
    只在这个本机内存试用服务里直接写入摘要，平台本身的口令规则不变。
    """
    import time

    from xuanpu_server.errors import InvalidRequestError
    from xuanpu_server.identity.passwords import validate_new_password
    from xuanpu_server.tenancy import Principal

    admin = Principal(role="tenant_admin", tenant_id="acme", key_id="r7-dev", user_id=None)
    accounts = platform.identity.accounts  # type: ignore[attr-defined]
    user, link = await accounts.create_account(admin, username=username, display_name="小院试用员工")
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


async def serve(args: argparse.Namespace) -> None:
    """起平台、开通环境与账号，然后一直服务到 Ctrl-C。"""
    from xuanpu_console import static_dir
    from xuanpu_server.api.app import create_app

    from scripts.acceptance import r5_real_model_harness as harness
    from scripts.acceptance.finance_browser import listen

    env = harness.load_model_env(args.model_env)
    origin = f"http://127.0.0.1:{args.port}"
    password = args.password
    tickets = subprocess.Popen([sys.executable, str(ROOT / "examples" / "ticket-api" / "server.py")])  # noqa: S603
    try:
        with tempfile.TemporaryDirectory() as raw:
            async with AsyncExitStack() as resources:
                platform, root_key = await harness.assemble(Path(raw), env, resources, origin=origin,
                                                           default_tenant="acme")
                async with listen(create_app(platform, console_dir=static_dir()), args.port):
                    key, _ = await harness.provision(platform, origin, root_key)
                    await harness.publish_variants(origin, key, Path(raw))
                    compliant = await account(platform, args.username, password)
                    print(f"""
==================== 小院试用服务已就绪 ====================
模型        {env['LLM_BOOTSTRAP_MODEL']}
地址        {origin}/console/
租户        acme
用户名      {args.username}
口令        {password}{"" if compliant else "（不合平台口令规则，仅本机试用服务这样开通）"}

可以试的话：""" + "".join(f"\n  · {t}" for t in TRY_THESE) + """

内存平台，退出即清空。Ctrl-C 退出。
==========================================================""", flush=True)
                    await asyncio.Event().wait()
    finally:
        tickets.terminate()
        tickets.wait(timeout=10)


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-env", type=Path, required=True, help="私有模型配置（dotenv，权限 600）")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--username", default="demo")
    parser.add_argument("--password", default="demo", help="默认 demo（仅本机试用）")
    args = parser.parse_args()
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
