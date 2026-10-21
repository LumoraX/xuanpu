"""``xuanpu`` 命令行。

业务方用它校验与打包业务包；部署方用它生成凭据、迁移表结构、启动平台服务。
命令行是进程入口，环境变量只在这里读取，再显式传给平台代码。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import math
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xuanpu_spec import (
    ArchiveError,
    archive_digest,
    manifest_json_schema,
    pack_package,
    validate_package,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    import uvicorn
    from fastapi import FastAPI
    from xuanpu_server.config import ServerConfig

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def _print_report(root: Path) -> bool:
    """校验业务包并打印结果；返回是否可以发布。"""
    report = validate_package(root)
    for warning in report.warnings:
        print(f"提醒：{warning}")
    for error in report.errors:
        print(f"错误：{error}", file=sys.stderr)
    if report.ok and report.manifest is not None:
        print(f"{report.manifest.name} {report.manifest.version}：校验通过")
    return report.ok


def cmd_validate(args: argparse.Namespace) -> int:
    """校验业务包目录。"""
    return EXIT_OK if _print_report(Path(args.package)) else EXIT_FAILED


def cmd_pack(args: argparse.Namespace) -> int:
    """校验并打包业务包。"""
    root = Path(args.package)
    if not _print_report(root):
        return EXIT_FAILED
    try:
        data = pack_package(root)
    except ArchiveError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_FAILED
    output = Path(args.output)
    output.write_bytes(data)
    print(f"已写入 {output}（{len(data)} 字节，{archive_digest(data)}）")
    return EXIT_OK


def cmd_schema(_: argparse.Namespace) -> int:
    """打印业务包清单的 JSON Schema。"""
    print(json.dumps(manifest_json_schema(), ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_keygen(args: argparse.Namespace) -> int:
    """生成平台根凭据、指标抓取令牌或主密钥。"""
    from xuanpu_server.secrets import generate_master_key
    from xuanpu_server.tenancy import generate_metrics_token, generate_root_key

    if args.kind == "root":
        key, digest = generate_root_key()
        print("根凭据（只显示这一次，请妥善保存）：")
        print(f"  {key}")
        print("写进配置文件的 root_key_hash：")
        print(f"  {digest}")
    elif args.kind == "metrics":
        token, digest = generate_metrics_token()
        print("指标抓取令牌（只显示这一次，交给 Prometheus）：")
        print(f"  {token}")
        print("写进配置文件的 metrics.token_hash：")
        print(f"  {digest}")
    else:
        print("写进配置文件 master_keys 的取值（base64）：")
        print(f"  {base64.b64encode(generate_master_key()).decode('ascii')}")
    return EXIT_OK


def _load(args: argparse.Namespace) -> ServerConfig:
    """读取配置文件，用进程的环境变量展开其中的 ``${NAME}``。"""
    from xuanpu_server.config import load_config

    return load_config(Path(args.config), variables=dict(os.environ))


async def _migrate(config: ServerConfig) -> int:
    """迁移控制面的表结构。"""
    import asyncpg
    from taifeng_stores import SqlNamespace
    from xuanpu_server.controlplane.schema import apply_migrations

    if config.control_plane.postgres is None:
        print("控制面用的是内存存储，没有需要迁移的表")
        return EXIT_OK
    settings = config.control_plane.postgres
    pool = await asyncpg.create_pool(settings.dsn, min_size=1, max_size=1)
    try:
        executed = await apply_migrations(
            pool, SqlNamespace(schema=settings.schema_name, prefix="xp_")
        )
    finally:
        await pool.close()
    if not executed:
        print("表结构已是最新")
    for migration in executed:
        print(f"已执行 {migration.version:04d}_{migration.name}")
    return EXIT_OK


def cmd_migrate(args: argparse.Namespace) -> int:
    """迁移控制面的表结构。"""
    return asyncio.run(_migrate(_load(args)))


def _console_dir() -> Path | None:
    """控制台的静态目录；没装 xuanpu-console 就不挂。"""
    try:
        from xuanpu_console import static_dir
    except ImportError:
        return None
    return static_dir()


FEISHU_CALLBACK_PATH = "/v1/auth/feishu/callback"
"""飞书登录的回调路径：查询串里有授权码，访问日志里要抹掉。

与 ``identity.urls.CALLBACK_PATH`` 相同（有测试守着）。"""


class CallbackQueryFilter(logging.Filter):
    """把 uvicorn 访问日志里飞书登录回调的查询串抹成 ``?…``：那里有授权码与 state。

    uvicorn 记访问日志时，参数依次是客户端地址、方法、带查询串的路径、HTTP 版本、状态码；这里只改
    第三个，记录照常输出。授权码 5 分钟有效、只能用一次，换令牌还要平台自己保管的 PKCE 校验串，
    但「授权码不进日志」（设计第 12 节）不该靠这些兜底。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """抹掉回调请求行的查询串；别的记录原样放行。"""
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            path, sep, _ = args[2].partition("?")
            if sep and path == FEISHU_CALLBACK_PATH:
                record.args = (args[0], args[1], f"{path}?…", args[3], args[4])
        return True


def install_access_log_filter() -> None:
    """给 ``uvicorn.access`` 装上 ``CallbackQueryFilter``；已经装过就不再装。"""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, CallbackQueryFilter) for item in access.filters):
        access.addFilter(CallbackQueryFilter())


GRACEFUL_SHUTDOWN_MARGIN_SECONDS = 5
"""HTTP 服务器停机时等已打开的响应发完的时限，比 ``shutdown_drain_seconds`` 多出的余量。"""


def uvicorn_config(app: FastAPI, config: ServerConfig) -> uvicorn.Config:
    """HTTP 服务器的配置。

    停机时 uvicorn 先等已打开的响应（事件流）发完，再让平台关闭（``Platform.close`` 再给在途
    对话 ``shutdown_drain_seconds``）。前一段原本没有时限，一轮对话跑多久就等多久；这里给它
    ``shutdown_drain_seconds`` 加 5 秒，到期 uvicorn 取消还没发完的响应，那几轮对话转到后台，
    由平台关闭时的那一段收尾。uvicorn 的这一项只收整秒，带小数的向上取整（只会多等，不会少等）。

    访问日志装上 ``CallbackQueryFilter``（飞书登录回调的授权码不进日志）。uvicorn 在构造配置时
    重设日志，所以过滤器在构造之后再装。
    """
    import uvicorn

    built = uvicorn.Config(
        app,
        host=config.listen.host,
        port=config.listen.port,
        log_level="info",
        workers=1,
        timeout_graceful_shutdown=math.ceil(config.shutdown_drain_seconds)
        + GRACEFUL_SHUTDOWN_MARGIN_SECONDS,
    )
    install_access_log_filter()
    return built


async def _serve(config: ServerConfig) -> int:
    """启动平台服务，直到收到终止信号。"""
    import uvicorn
    from xuanpu_server.api import create_app
    from xuanpu_server.bootstrap import open_platform

    async with open_platform(config) as platform:
        app = create_app(platform, console_dir=_console_dir())
        server = uvicorn.Server(uvicorn_config(app, config))
        await server.serve()
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    """启动平台服务。"""
    return asyncio.run(_serve(_load(args)))


def format_eval_report(report: dict[str, Any]) -> str:
    """把评测报告排成可读的文本。"""
    lines = [
        f"{report['package']} {report['version']}：{report['total']} 个用例，"
        f"{report['failed']} 个未通过，共 {report['total_tokens']} token"
    ]
    for case in report["cases"]:
        mark = "通过" if case["passed"] else "未通过"
        lines.append(
            f"  [{mark}] {case['name']}  {case['outcome']}  "
            f"{case['total_tokens']} token  {case['duration_ms']} ms"
        )
        for reason in case["reasons"]:
            lines.append(f"      - {reason}")
    return "\n".join(lines)


async def _eval(args: argparse.Namespace, api_key: str) -> int:
    """经平台接口运行评测并打印报告。"""
    from xuanpu_sdk import XuanpuClient, XuanpuError

    async with XuanpuClient(args.url, api_key) as client:
        try:
            report = await client.packages.evaluate(
                args.package, version=args.version, names=args.case or None
            )
        except XuanpuError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return EXIT_FAILED
    print(format_eval_report(report))
    return EXIT_OK if report["passed"] else EXIT_FAILED


def cmd_eval(args: argparse.Namespace) -> int:
    """在平台上运行业务包的评测用例。"""
    api_key = args.key or os.environ.get("XUANPU_API_KEY")
    if not api_key:
        print("错误：请用 --key 或环境变量 XUANPU_API_KEY 提供凭据", file=sys.stderr)
        return EXIT_USAGE
    return asyncio.run(_eval(args, api_key))


def build_parser() -> argparse.ArgumentParser:
    """命令行参数定义。"""
    parser = argparse.ArgumentParser(prog="xuanpu", description="xuanpu 平台命令行")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="校验业务包目录")
    validate.add_argument("package", help="业务包目录")
    validate.set_defaults(handler=cmd_validate)

    pack = commands.add_parser("pack", help="校验并打包业务包")
    pack.add_argument("package", help="业务包目录")
    pack.add_argument("-o", "--output", required=True, help="输出的归档文件")
    pack.set_defaults(handler=cmd_pack)

    schema = commands.add_parser("schema", help="打印业务包清单的 JSON Schema")
    schema.set_defaults(handler=cmd_schema)

    keygen = commands.add_parser("keygen", help="生成平台根凭据、指标抓取令牌或主密钥")
    keygen.add_argument("kind", choices=["root", "metrics", "master"])
    keygen.set_defaults(handler=cmd_keygen)

    migrate = commands.add_parser("migrate", help="迁移控制面的表结构")
    migrate.add_argument("--config", required=True, help="配置文件")
    migrate.set_defaults(handler=cmd_migrate)

    serve = commands.add_parser("serve", help="启动平台服务")
    serve.add_argument("--config", required=True, help="配置文件")
    serve.set_defaults(handler=cmd_serve)

    evaluate = commands.add_parser("eval", help="在平台上运行业务包的评测用例")
    evaluate.add_argument("--url", default="http://127.0.0.1:8420", help="平台地址")
    evaluate.add_argument("--key", help="凭据；缺省读环境变量 XUANPU_API_KEY")
    evaluate.add_argument("--package", required=True, help="业务包名")
    evaluate.add_argument("--version", help="版本；缺省用生效版本")
    evaluate.add_argument("--case", action="append", help="只跑这些用例，可重复")
    evaluate.set_defaults(handler=cmd_eval)
    from xuanpu_cli.work_commands import add_work_commands

    add_work_commands(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。"""
    from xuanpu_server.config import ConfigError
    from xuanpu_server.errors import SchemaError

    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (ConfigError, SchemaError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
