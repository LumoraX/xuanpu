# xuanpu-sdk-py

xuanpu 平台的 Python 客户端。

```python
from xuanpu_sdk import XuanpuClient

async with XuanpuClient("http://localhost:8420", api_key) as client:
    session = await client.sessions.create("support-desk")
    async for event in client.sessions.send(session.session_id, "你好"):
        if event.kind == "assistant_text":
            print(event.data["delta"], end="")
        elif event.kind == "turn_suspended":
            request_id = event.data["pending"][0]["request_id"]
            async for _ in client.sessions.resume(
                session.session_id, {request_id: {"granted": True}}
            ):
                pass
```

接口说明见仓库根目录的 `docs/architecture/http-api.md`。
