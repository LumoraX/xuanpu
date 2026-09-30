# 应用接入

你的系统（客服工作台、工单系统、内部门户……）怎样调用平台：建会话、发消息、收事件流、处理审批、断线后补齐。

接口的完整清单见 [HTTP 接口](../architecture/http-api.md)。本文讲用法和要注意的地方。例子用 Python 客户端，直接调 HTTP 的写法见[快速上手](quickstart.md)。

## 凭据与角色

每个请求带 `Authorization: Bearer <API key>`。API key 属于一个租户，有两种角色：

| 角色 | 能做什么 | 给谁用 |
| --- | --- | --- |
| `tenant_member` | 建会话、对话、审批、看业务包列表 | 你的应用后端 |
| `tenant_admin` | 以上全部，加上发布业务包、管理密钥与 API key、跑评测、查审计与用量 | 发布流水线、管理后台 |

应用后端用 `tenant_member` 的 key 就够了。API key 不要下发到浏览器或移动端：它能访问本租户的全部会话。前端经你的后端转发。

平台不认识你的终端用户。想把会话和你系统里的用户、客户、工单对应起来，用建会话时的 `metadata`。

## 安装客户端

```bash
uv add xuanpu-sdk-py        # 发布到 PyPI 之前，从本仓库的 packages/xuanpu-sdk-py 安装
```

```python
from xuanpu_sdk import XuanpuClient, XuanpuError

async with XuanpuClient("https://xuanpu.internal", api_key) as client:
    ...
```

客户端是异步的，只依赖 `httpx`。

## 会话

```python
session = await client.sessions.create(
    "support-desk",
    metadata={"customer": "C-42", "ticket": "T-1001"},
)
```

| 参数 | 说明 |
| --- | --- |
| `package` | 业务包名 |
| `version` | 不给就用当前的生效版本。**会话创建时钉住版本**，之后业务包切换版本不影响它 |
| `entry` | 入口 skill。不给就用清单 `entries` 里的第一个 |
| `metadata` | 你自己的关联标识，最多 16 项，键和值都是字符串、各不超过 256 个字符。平台原样保存、原样返回、写进审计，不解释它的含义 |

会话不会自动关闭。用完要关：

```python
await client.sessions.close(session.session_id)
```

没关的会话一直算作「活动会话」，计入租户的 `max_active_sessions` 配额。关闭时如果还有对话在进行，那一轮会被取消；关闭后不能再发消息（`409 conflict`）。

会话一段时间没有对话，平台会释放它占用的内存，下次发消息时自动从存储里续接。这对调用方是透明的，只是续接后的第一轮会慢一点。

## 发消息，收事件流

只要最终回复：

```python
reply, last = await client.sessions.ask(session.session_id, "查一下工单 T-1001")
print(reply)                                  # 工单 T-1001（无法登录）当前优先级 normal，处理时限 24 小时。
print(last.kind)                              # turn_completed
print(last.data["usage"]["total_tokens"])     # 1368
```

要流式展示：

```python
async for event in client.sessions.send(session.session_id, "查一下工单 T-1001"):
    if event.kind == "assistant_text":
        print(event.text_delta, end="", flush=True)
    elif event.kind == "tool_call_started":
        print(f"\n[正在调用 {event.data['name']}]")
```

一条消息开启「一轮对话」：模型可能调几次工具，最后给出回复。这一轮有一个 `submission_id`，事件流里的每条事件都带着它。

### 事件

常用的事件类型：

| 事件 | 含义 | `data` 里常用的字段 |
| --- | --- | --- |
| `turn_started` | 这一轮开始 | `entry_skill_id` |
| `assistant_text` | 回复的一段增量 | `delta` |
| `assistant_reasoning` | 模型的推理过程增量（模型支持时才有） | `delta` |
| `tool_call_started` | 模型发起一次工具调用 | `call_id`、`name`、`arguments` |
| `tool_call_completed` | 工具调用结束 | `call_id`、`name`、`output`、`is_error`、`duration_ms` |
| `turn_completed` | 这一轮正常结束 | `usage`、`iterations`、`duration_ms` |
| `turn_failed` | 这一轮失败 | 失败原因 |
| `turn_suspended` | 这一轮停在审批点 | `pending` |
| `suspension_resolved` | 审批结果已送达，开始续跑 | `request_ids` |

事件来自 taifeng 内核，种类会随内核版本增加（比如 `engine_log`、`rewind_checkpoint_recorded`）。**遇到不认识的事件类型请忽略**，不要当成错误。

### 怎样判断一轮结束

一轮对话以这三种事件之一结束：`turn_completed`、`turn_failed`、`turn_suspended`。

有子 skill 参与时，子 skill 自己的完成事件也会出现在流里。区分办法是看 `data.is_root`：`turn_completed` 和 `turn_failed` 只有 `is_root` 为真才是这一轮的结束；`turn_suspended` 出现即结束。客户端的 `event.terminal` 已经按这个规则算好了。

直接读 HTTP 响应的话更简单：平台在一轮结束后补一帧 `event: end`，读到它就结束。

同一个会话请串行发送：等上一轮结束再发下一条。

### 直接读 HTTP 事件流时要注意

- 第一帧是 `event: turn`，给出 `submission_id`；响应头 `X-Submission-Id` 里也有。先把它记下来，断线后靠它补齐。
- 长时间没有事件时，平台每 15 秒发一行 `: keepalive` 注释。按 SSE 规范，以冒号开头的行应当忽略。
- 反向代理要关掉对这条响应的缓冲，读超时要大于 15 秒。平台已经带了 `X-Accel-Buffering: no`。
- 最后一帧 `end` 里的 `overflowed` 为真，表示你读得太慢、平台放弃了这条流（缓冲上限 8192 条事件）。这一轮对话本身不受影响，用回放接口补齐。

## 审批

业务包把某些操作定为需要人批准（见[编写业务包](writing-packages.md#审批强度)）。模型要做这类操作时，这一轮以 `turn_suspended` 结束，操作**尚未执行**：

```python
async for event in client.sessions.send(session.session_id, "把它调成高优先级"):
    if event.kind == "turn_suspended":
        for request in event.data["pending"]:
            detail = request["detail"]
            print("需要审批：", detail["target"], detail["args"])
            # 需要审批： api__tickets__updateTicket {'id': 'T-1001', 'body': {'priority': 'high'}}
```

`pending` 里每一项是一个待办：

| 字段 | 含义 |
| --- | --- |
| `request_id` | 待办的标识，送回决定时用 |
| `reason` | `permission` 表示审批 |
| `detail.scope` | 操作的种类：`tool_use`（连接器工具）、`script_exec`（脚本）、`shell_exec`（命令） |
| `detail.target` | 具体是哪个工具或脚本 |
| `detail.reason` | 为什么要审批 |
| `detail.args` | 连接器工具的调用参数，给审批人看的依据 |
| `payload_schema` | 决定的形状，可以据此渲染表单 |

把这些展示给有权批准的人，拿到决定后送回：

```python
decision = {request["request_id"]: {"granted": True}}
async for event in client.sessions.resume(session.session_id, decision):
    print(event.text_delta, end="", flush=True)
# 已把工单 T-1001 调为高优先级，处理时限缩短为 4 小时。
```

拒绝：`{"granted": False, "reason": "夜间不改派"}`。操作不会执行，模型收到被拒绝的结果后继续，通常会如实告诉用户。

要知道的几件事：

- **等待不限时**，也不占用平台资源。审批人第二天才处理也可以；正式部署下（控制面在 Postgres）期间平台重启也不影响。
- **待办可以随时再查**：`await client.sessions.pending(session_id)`。审批界面和发起对话的页面不是同一个时用它。
- 续跑的事件流里，被批准的那次调用不会再出现 `tool_call_completed` 事件。它是否执行、结果如何，以审计里的 `connector_call` 记录为准（见[审计与用量](audit-and-usage.md)）。
- **平台不区分「谁有权审批」**：本租户任何一把 API key 都能调 `resume`。谁能批什么，由你的系统在调用之前把关。审计会记下是哪把 key 送回的决定。

## 断线之后

调用方断开连接，**这一轮对话不会停**，平台会把它跑完。重新连上之后不要重发消息（那会开启新的一轮），用 `submission_id` 回放：

```python
events = await client.sessions.replay(session.session_id, submission_id)
for event in events:
    print(event["kind"])
# …
# turn_completed
# turn_text

print(events[-1]["data"]["text"])    # 这一轮回复的全文
```

回放的是已经写进审计的事件，有两点和实时流不同：

- 没有 `assistant_text` 这样的流式增量。一轮结束时另有一条 `turn_text`，里面是拼好的回复全文；
- 审计是成批写入的，一轮还没结束时回放到的事件可能比实时流少一点。看到终结事件才算完整。

`after_seq` 参数可以只取序号大于某个值的事件，用来接着上次读到的位置继续。

## 错误

错误响应的格式统一：

```json
{"error": {"code": "not_found", "message": "会话 'ses_nope' 不存在"}}
```

客户端把它变成异常：

```python
try:
    await client.sessions.get("ses_nope")
except XuanpuError as exc:
    print(exc.status, exc.code, exc.message)    # 404 not_found 会话 'ses_nope' 不存在
```

按 `code` 处理，不要解析 `message`（文案会改）：

| 状态码 | `code` | 什么时候出现 | 怎么办 |
| --- | --- | --- | --- |
| 400 | `invalid_request` | 参数不对。字段校验不过时 `details["problems"]` 里有明细 | 修正请求 |
| 400 | `secret_missing` | 业务包引用的密钥还没配置 | 让租户管理员配上密钥后重试 |
| 401 | `unauthenticated` | 凭据无效、已吊销，或租户被停用 | 检查凭据 |
| 403 | `permission_denied` | 角色不够 | 换用有权限的凭据 |
| 404 | `not_found` | 资源不存在，或者属于别的租户 | — |
| 409 | `conflict` | 会话已关闭、版本已发布过等 | — |
| 409 | `session_busy` | 会话正由另一个平台实例持有。`details["owner"]` 是那个实例的标识 | 见下 |
| 429 | `quota_exceeded` | 超出租户配额：活动会话数、当天 token、同时进行的对话数 | 退避后重试，或关闭不用的会话 |
| 503 | `runtime_unavailable` | 沙盒、连接器不可用，或者实例正在停机 | 稍后重试；多实例部署下换一个实例 |
| 500 | `internal_error` | 平台内部错误 | 重试；持续出现找平台管理员 |

连不上平台、或者事件流中途断开时，客户端抛 `XuanpuTransportError`（`code` 是 `transport_error`）。

关于 `session_busy`：多实例部署时，一个会话同一时刻只由一个实例处理。请求落到别的实例上会得到这个错误。部署方通常在负载均衡上按会话标识做粘性路由来避免它；如果你直接面对多个实例，按 `owner` 把请求发给持有者，或者稍后重试（持有者空闲后会放手）。

一轮对话内部的失败（模型调用失败、预算用完）不走 HTTP 错误：请求本身是 `200`，事件流以 `turn_failed` 结束。

## 限制一览

| 项 | 上限 |
| --- | --- |
| 单条消息 | 200 000 个字符 |
| `metadata` | 16 项，键和值各 256 个字符 |
| 会话累计 token、单轮往返次数 | 业务包与平台配置里的预算 |
| 单轮事件缓冲 | 8192 条（读得太慢才会碰到） |
| 列会话 | 每次最多 200 个 |

## 目前做不到的

- 不能只取消进行中的那一轮对话，只能关闭整个会话。
- 没有 JavaScript 客户端；浏览器端请直接用 `fetch` 读事件流（控制台的 [`app.js`](../../packages/xuanpu-console/src/xuanpu_console/static/app.js) 是一个可以参照的实现）。
- 消息只支持文本，不支持图片与文件。
