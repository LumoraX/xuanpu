"""xuanpu-spec —— xuanpu 业务包规范。

定义业务方提交给平台的业务包清单（skills、连接器声明、策略、评测用例）及其版本规则。
它是业务方与平台之间的对外契约，因此不依赖平台的任何其他组件。
"""

from __future__ import annotations

from importlib.metadata import version

__all__ = ["__version__"]

# 版本单一来源是 pyproject.toml：从发行元数据读取，避免两处手改漂移
__version__ = version("xuanpu-spec")
