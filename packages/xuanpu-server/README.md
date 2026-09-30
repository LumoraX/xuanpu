# xuanpu-server

xuanpu 平台服务：控制面（租户、凭据、业务包版本、密钥、配额、审计、用量）、运行时（按业务包组装 taifeng 引擎、承载会话、处理审批挂起与续跑）与 HTTP 接口。

```python
from xuanpu_server.api import create_app
from xuanpu_server.bootstrap import open_platform
from xuanpu_server.config import load_config

config = load_config(path, variables=environ)
async with open_platform(config) as platform:
    app = create_app(platform)      # 交给任意 ASGI 服务器
```

多实例部署装 `postgres` extra。设计见仓库根目录的 `docs/architecture/`。
