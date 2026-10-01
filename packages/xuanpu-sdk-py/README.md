# xuanpu-sdk-py

xuanpu 平台的 Python 客户端。异步，只依赖 `httpx`，不依赖平台的其他包。

用法与注意事项（审批、断线补齐、错误处理）见[应用接入](../../docs/guides/integrating.md)；本文是方法清单。

```python
from xuanpu_sdk import XuanpuClient

async with XuanpuClient("http://localhost:8420", api_key) as client:
    session = await client.sessions.create("support-desk")
    reply, last = await client.sessions.ask(session.session_id, "你好")
    print(reply)
```

## `XuanpuClient(base_url, api_key, *, timeout_seconds=60.0)`

`api_key` 是租户的 API key；只调 `admin.create_tenant`、`admin.issue_tenant_key` 时用根凭据。`timeout_seconds` 约束普通请求，事件流与评测不受它限制。

用 `async with` 管理连接，或者用完调 `await client.close()`。

| 方法 | 说明 |
| --- | --- |
| `await client.health()` | 存活探测，返回 `{"status", "version", "instance"}` |

## `client.sessions`

| 方法 | 返回 | 说明 |
| --- | --- | --- |
| `await create(package, *, version=None, entry=None, metadata=None)` | `Session` | 新建会话 |
| `await get(session_id)` | `Session` | 取会话 |
| `await list(*, status=None, mine=False, limit=50)` | `list[Session]` | 列出会话，最近活动的在前 |
| `await close(session_id)` | `Session` | 关闭会话 |
| `send(session_id, text)` | `AsyncIterator[Event]` | 发消息，逐条产出这一轮的事件 |
| `await ask(session_id, text)` | `(str, Event)` | 发消息并等到这一轮结束，返回拼好的回复与终结事件 |
| `resume(session_id, resolutions)` | `AsyncIterator[Event]` | 送回审批决定，逐条产出续跑部分的事件 |
| `await cancel(session_id, *, submission_id=None)` | `list[str]` | 停止生成：取消在途的对话，返回停了哪些 submission |
| `await pending(session_id)` | `list[dict]` | 当前等待处理的审批请求 |
| `await replay(session_id, submission_id, *, after_seq=-1)` | `list[dict]` | 回放一轮对话已记入审计的事件 |

`send` 与 `resume` 不是协程，直接 `async for`：

```python
async for event in client.sessions.send(session.session_id, "把它调成高优先级"):
    if event.kind == "assistant_text":
        print(event.text_delta, end="", flush=True)
    elif event.kind == "turn_suspended":
        for request in event.data["pending"]:
            decision = {request["request_id"]: {"granted": True}}
            async for resumed in client.sessions.resume(session.session_id, decision):
                print(resumed.text_delta, end="", flush=True)
```

`ask` 遇到审批时不会卡住：它返回挂起前的文本，终结事件是 `turn_suspended`。

## `client.packages`

| 方法 | 返回 | 说明 |
| --- | --- | --- |
| `await publish(archive, *, activate=False)` | `PackageVersion` | 发布业务包归档（`bytes`） |
| `await list(name=None)` | `list[PackageVersion]` | 列出已发布版本 |
| `await activate(name, version)` | `None` | 切换生效版本 |
| `await evaluate(name, *, version=None, names=None)` | `dict` | 运行评测，返回报告。用例多时会比较久，不设超时 |

```python
from pathlib import Path
from xuanpu_spec import pack_package      # 打包在 xuanpu-spec 里

await client.packages.publish(pack_package(Path("./support-desk")), activate=True)
```

## `client.secrets`

| 方法 | 返回 | 说明 |
| --- | --- | --- |
| `await put(name, value)` | `None` | 写入或覆盖 |
| `await names()` | `list[str]` | 列出密钥名。取值读不回来 |
| `await delete(name)` | `None` | 删除 |

## `client.admin`

| 方法 | 需要的身份 | 返回 | 说明 |
| --- | --- | --- | --- |
| `await create_tenant(tenant_id, name, *, max_active_sessions=None, max_daily_tokens=None, max_concurrent_turns=None)` | 平台管理员 | `dict` | 新建租户 |
| `await issue_tenant_key(tenant_id, name, *, role="tenant_member")` | 平台管理员 | `IssuedKey` | 给某个租户签发 API key |
| `await issue_key(name, *, role="tenant_member")` | 租户管理员 | `IssuedKey` | 给本租户签发 API key |
| `await revoke_key(key_id)` | 租户管理员 | `None` | 吊销 |
| `audit(*, session_id=None, kind=None, source=None, since=None, until=None, limit=100)` | 租户管理员 | `AsyncIterator[AuditRecord]` | 查审计，自动翻页；`limit` 是每页条数 |
| `await usage(since, until)` | 租户管理员 | `list[Usage]` | 一段日期内的用量，日期写 `YYYY-MM-DD` |

`IssuedKey.key` 是完整的 API key，只在签发时出现一次。

## 数据类型

| 类型 | 字段 |
| --- | --- |
| `Session` | `session_id`、`package`、`version`、`entry`、`status`、`owner`、`created_at`、`updated_at`、`created_by`、`metadata` |
| `Event` | `kind`、`submission_id`、`seq`、`at`、`data`；属性 `terminal`（是否结束了这一轮）、`text_delta`（文本增量，不是文本事件时为空串） |
| `PackageVersion` | `name`、`version`、`digest`、`active`、`published_at`、`published_by` |
| `AuditRecord` | `audit_id`、`source`、`kind`、`at`、`actor`、`session_id`、`stream`、`seq`、`data` |
| `Usage` | `day`、`input_tokens`、`output_tokens`、`total_tokens`、`turns` |
| `IssuedKey` | `key`、`key_id`、`tenant_id`、`name`、`role` |

## 异常

| 异常 | 什么时候 | 属性 |
| --- | --- | --- |
| `XuanpuError` | 平台返回了错误响应 | `status`、`code`、`message`、`details`（如业务包校验的 `problems`、会话持有者 `owner`） |
| `XuanpuTransportError` | 连不上平台、事件流中途断开、响应不是平台的格式。是 `XuanpuError` 的子类，`status` 为 0，`code` 为 `transport_error` | 同上 |

错误码的含义见 [HTTP 接口](../../docs/architecture/http-api.md#错误)。

## 没有封装的接口

列出租户、启停租户、列出本租户的 API key，客户端没有对应的方法，需要时直接调 HTTP 接口。
