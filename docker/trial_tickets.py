"""部署试用工单侧车，保持现有样例包的 loopback 连接器可用。"""
from __future__ import annotations

import importlib.util
from http.server import ThreadingHTTPServer
from pathlib import Path


def main() -> None:
    """令牌只读私有挂载，示例记录随进程重建重置。"""
    spec = importlib.util.spec_from_file_location('tickets', '/opt/xuanpu/ticket-api.py')
    if spec is None or spec.loader is None:
        raise RuntimeError('镜像中的工单样例缺失')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.TOKEN = Path('/run/xuanpu/tickets_token').read_text().strip()
    if not module.TOKEN:
        raise RuntimeError('工单令牌为空')
    ThreadingHTTPServer(('127.0.0.1', 8431), module.Handler).serve_forever()


if __name__ == '__main__':
    main()
