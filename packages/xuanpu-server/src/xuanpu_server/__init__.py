"""xuanpu-server —— xuanpu 平台服务：运行时与控制面。

- 控制面：租户、凭据、业务包版本、密钥、配额、审计、用量；
- 运行时：按业务包组装内核引擎，承载会话，分发事件流，处理审批挂起与续跑；
- 接入层：HTTP 接口（``xuanpu_server.api``）。

平台代码只依赖 taifeng 的稳定层（红线 3）。
"""

from __future__ import annotations

from importlib.metadata import version

__all__ = ["__version__"]

# 版本单一来源是 pyproject.toml：从发行元数据读取，避免两处手改漂移
__version__ = version("xuanpu-server")
