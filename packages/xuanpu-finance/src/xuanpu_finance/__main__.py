"""独立财务服务命令入口；通过配置文件注入全部运行资源。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import uvicorn

from xuanpu_finance.config import FinanceConfig, load_config
from xuanpu_finance.runtime import open_runtime


async def serve(config: FinanceConfig) -> None:
    """独立部署，不加载平台、模型或业务包执行引擎。"""
    async with open_runtime(config) as app:
        server = uvicorn.Server(
            uvicorn.Config(
                app, host=config.host, port=config.port, access_log=False, log_level="info"
            )
        )
        await server.serve()


def main() -> None:
    """解析显式配置路径；配置检查不连接数据库或执行财务动作。"""
    parser = argparse.ArgumentParser(description="玄圃独立财务 F0/F1/F2")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.check_config:
        print("财务配置结构校验通过；尚未连接外部资源")
        return
    asyncio.run(serve(config))


if __name__ == "__main__":
    main()
