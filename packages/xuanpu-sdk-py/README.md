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

## `XuanpuClient(base_url, api_key, *, timeout_seconds=60.0, act_as_user=None)`

`api_key` 是租户的 API key；只调 `admin.create_tenant`、`admin.set_quota`、`admin.issue_tenant_key` 时用根凭据。`timeout_seconds` 约束普通请求，事件流与评测不受它限制。

用 `async with` 管理连接，或者用完调 `await client.close()`。

`act_as_user` 固定本客户端代表的员工，普通请求与 SSE 都带 `X-Xuanpu-Act-As`。key 必须已被授予 `act_as_users`，目标必须是本租户启用的员工；生效角色取 key 与员工中较低者，不能借此提权。代表另一个员工时新建客户端，不在共享客户端里切身份；省略参数保持原 API key 行为。SDK 不管理浏览器 cookie 登录。

| 方法 | 说明 |
| --- | --- |
| `await client.health()` | 存活探测，返回 `{"status", "version", "instance"}` |
| `await client.me()` | 返回 `actor`、`role`、`tenant_id`、`key_id`、`user_id`、`display_name`；不适用的字段为 `null` |

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

代调用普通成员时仅能访问本人会话，访问他人的会话为 404。`mine=True` 对代调用按员工过滤，对普通 API key 按创建者过滤。`Session.user_id` 是会话主人；无主人的系统会话与旧服务端响应均为 `None`。

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

停止生成：在另一个协程里（比如用户点了「停止」）调 `cancel`，被停的那一轮从它自己的事件流照常收尾。下面是一次实际运行：

```python
events = []

async def stream():
    async for event in client.sessions.send(session.session_id, "写一篇长文"):
        events.append(event)

task = asyncio.create_task(stream())
# ……用户点了「停止」
print(await client.sessions.cancel(session.session_id, submission_id=events[0].submission_id))
# ['sub_9d2b8c768998']
await task
last = events[-1]
print(last.kind, last.terminal, last.data["end_reason"])
# turn_completed True cancelled
```

不给 `submission_id` 就停掉这个会话里全部在途的对话。那一轮已经结束时，不给返回 `[]`，给了抛 `XuanpuError`（`400 invalid_request`）。竞态与注意事项见[应用接入](../../docs/guides/integrating.md#停止生成)。

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
| `await set_quota(tenant_id, *, max_active_sessions=None, max_daily_tokens=None, max_concurrent_turns=None)` | 平台管理员 | `dict` | 整体替换租户的配额，返回租户对象。没给的项恢复为不限，不保留原值 |
| `await issue_tenant_key(tenant_id, name, *, role="tenant_member", act_as_users=False)` | 平台管理员 | `IssuedKey` | 给某个租户签发 API key，可显式授予代调用能力 |
| `await issue_key(name, *, role="tenant_member", act_as_users=False)` | 租户管理员 | `IssuedKey` | 给本租户签发 API key |
| `await revoke_key(key_id)` | 租户管理员 | `None` | 吊销 |
| `audit(*, session_id=None, kind=None, source=None, user_id=None, actor=None, since=None, until=None, limit=100)` | 租户管理员 | `AsyncIterator[AuditRecord]` | 查审计，自动翻页；`limit` 是每页条数；user_id 是归属员工，actor 精确匹配触发者 |
| `await usage(since, until)` | 租户管理员 | `list[Usage]` | 一段日期内的用量，日期写 `YYYY-MM-DD` |
| `await user_usage(since, until, *, user_id=None)` | 租户管理员 | `list[UserUsage]` | UTC 日期范围内按天、按员工的用量；可筛一个员工；不含无主人的系统会话 |

`IssuedKey.key` 是完整的 API key，只在签发时出现一次。

`set_quota` 是整体替换，没给的项恢复为不限：

```python
async with XuanpuClient("http://localhost:8420", root_key) as root:
    tenant = await root.admin.set_quota("acme", max_active_sessions=200, max_daily_tokens=10_000_000)
    print(tenant["quota"])
# {'max_active_sessions': 200, 'max_daily_tokens': 10000000, 'max_concurrent_turns': None}
```

## 数据类型

| 类型 | 字段 |
| --- | --- |
| `Session` | `session_id`、`package`、`version`、`entry`、`status`、`owner`、`created_at`、`updated_at`、`created_by`、`metadata`、`user_id` |
| `Event` | `kind`、`submission_id`、`seq`、`at`、`data`；属性 `terminal`（是否结束了这一轮）、`text_delta`（文本增量，不是文本事件时为空串） |
| `PackageVersion` | `name`、`version`、`digest`、`active`、`published_at`、`published_by` |
| `AuditRecord` | `audit_id`、`source`、`kind`、`at`、`actor`、`session_id`、`stream`、`seq`、`data`、`user_id`（旧服务端缺此字段时为 `None`） |
| `Usage` | `day`、`input_tokens`、`output_tokens`、`total_tokens`、`turns` |
| `UserUsage` | `Usage` 的全部字段，另加 `user_id` |
| `IssuedKey` | `key`、`key_id`、`tenant_id`、`name`、`role` |

## 异常

| 异常 | 什么时候 | 属性 |
| --- | --- | --- |
| `XuanpuError` | 平台返回了错误响应 | `status`、`code`、`message`、`details`（如业务包校验的 `problems`、会话持有者 `owner`） |
| `XuanpuTransportError` | 连不上平台、事件流中途断开、响应不是平台的格式。是 `XuanpuError` 的子类，`status` 为 0，`code` 为 `transport_error` | 同上 |

错误码的含义见 [HTTP 接口](../../docs/architecture/http-api.md#错误)。

## 没有封装的接口

列出租户、启停租户、列出本租户的 API key、用户管理、飞书配置与手工对账，客户端没有对应的方法，需要时直接调 HTTP 接口。飞书浏览器登录见[接入手册](../../docs/guides/feishu-login.md)。

## 员工办公交办

使用获准代本人调用的凭据构造 `XuanpuClient(..., act_as_user=employee_id)`。工作任务接口只接受本人材料的文件标识和固定版本；普通员工不能指定执行身份、能力包、宿主路径或预算。

```python
# 同一交办重试必须保留 request_id、目标和输入版本。
task = await client.create_work_task(
    "按部门汇总金额",
    [{"item_id": file_id, "revision": 1}],
    request_id="office-request-1", timezone="Asia/Shanghai",
)
current = await client.get_work_task(task.task_id)
page = await client.list_work_tasks(limit=50)
events = await client.work_task_events(task.task_id, after_id=0)
stop = await client.cancel_work_task(task.task_id)
# waiting_user 时读取当前状态版本，提交本人选择；决定 request_id 也必须固定。
result = await client.decide_work_task(task.task_id, {
    "kind": "office_actions", "request_id": "office-decision-1",
    "state_version": current.state_version, "choices": [],
})
```

`WorkTask` 直接映射服务端 `task_id/status/state_version`，完整响应保存在 `record`。`accepted` 表示已受理；`succeeded` 才表示成果与必要本人决定已完成。取消响应只说明已登记的停止状态，SDK不会推断停止成功，也不会重试未知结果的写操作。403、404、409照常抛出 `XuanpuError`。


## 组织与职责

客户端提供organization_me、organization_directory、list_organization、get_organization、create_organization、change_organization、organization_authority、preview_organization_authority、apply_organization_authority、preview_organization_import、get_organization_import和apply_organization_import。写入须传固定request_id，变更须传expected_version。目录应用只提交明确selected_changes及source_revision，不自动应用全量，也不改变客户端身份。

## 共享协作（R3 候选，尚未生产发布）

`XuanpuClient`提供 `create_shared_matter`、`list_shared_matters`、`get_shared_matter`、`change_shared_matter`；
`list_shared_members`、`invite_shared_member`、`revoke_shared_member`、`transfer_shared_owner`、`shared_matter_events`；
`list_shared_materials`、`publish_shared_material`、`upload_shared_material`、`download_shared_material`、`withdraw_shared_material`、`shared_publication`、`publish_shared_text`；
`create_shared_context`、`list_shared_contexts`、`get_shared_context`、`read_shared_context`、`close_shared_context`、`shared_source_reply`；
`shared_notifications`、`read_shared_notification`。准确签名见[客户端实现](src/xuanpu_sdk/shared_matters.py)。

JSON写入保留调用者的操作号、版本及确认，不自动补 `confirmed`。下载在20MiB限制内校验服务端SHA，返回字节，不写本地或私人空间。
共享历史与上下文不参与普通个人对话；来源失效的SSE `error`帧转成带状态和错误码的 `XuanpuError`，不会误当成内核业务事件。
`organization_people`仅列本企业可选启用员工的稳定编号和姓名，协作不要求部门。
