"""``xuanpu`` 命令行。

业务方用它校验与打包业务包；部署方用它生成凭据、迁移表结构、启动平台服务。
命令行是进程入口，环境变量只在这里读取，再显式传给平台代码。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from xuanpu_spec import (
    ArchiveError,
    archive_digest,
    manifest_json_schema,
    pack_package,
    validate_package,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

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
    """生成平台根凭据或主密钥。"""
    from xuanpu_server.secrets import generate_master_key
    from xuanpu_server.tenancy import generate_root_key

    if args.kind == "root":
        key, digest = generate_root_key()
        print("根凭据（只显示这一次，请妥善保存）：")
        print(f"  {key}")
        print("写进配置文件的 root_key_hash：")
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


async def _serve(config: ServerConfig) -> int:
    """启动平台服务，直到收到终止信号。"""
    import uvicorn
    from xuanpu_server.api import create_app
    from xuanpu_server.bootstrap import open_platform

    async with open_platform(config) as platform:
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(platform),
                host=config.listen.host,
                port=config.listen.port,
                log_level="info",
            )
        )
        await server.serve()
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    """启动平台服务。"""
    return asyncio.run(_serve(_load(args)))


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

    keygen = commands.add_parser("keygen", help="生成平台根凭据或主密钥")
    keygen.add_argument("kind", choices=["root", "master"])
    keygen.set_defaults(handler=cmd_keygen)

    migrate = commands.add_parser("migrate", help="迁移控制面的表结构")
    migrate.add_argument("--config", required=True, help="配置文件")
    migrate.set_defaults(handler=cmd_migrate)

    serve = commands.add_parser("serve", help="启动平台服务")
    serve.add_argument("--config", required=True, help="配置文件")
    serve.set_defaults(handler=cmd_serve)
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
