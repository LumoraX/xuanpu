# 应用接入

你的系统（客服工作台、工单系统、内部门户……）怎样调用平台：建会话、发消息、收事件流、处理审批、停止生成、断线后补齐。

接口的完整清单见 [HTTP 接口](../architecture/http-api.md)。本文讲用法和要注意的地方。例子用 Python 客户端，直接调 HTTP 的写法见[快速上手](quickstart.md)。

## 凭据与角色

每个请求带 `Authorization: Bearer <API key>`。API key 属于一个租户，有两种角色：

| 角色 | 能做什么 | 给谁用 |
| --- | --- | --- |
| `tenant_member` | 建会话、对话、审批、看业务包列表 | 你的应用后端 |
| `tenant_admin` | 以上全部，加上发布业务包、管理密钥与 API key、跑评测、查审计与用量 | 发布流水线、管理后台 |

应用后端用 `tenant_member` 的 key 就够了。API key 不要下发到浏览器或移动端：未代员工调用的 key 能访问本租户全部会话。前端经你的后端转发；需要员工隔离与归属时用[代调用](#代表员工调用)。平台自带控制台也支持[飞书登录](feishu-login.md)，不需要向员工下发 key。

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

没关的会话一直算作「活动会话」，计入租户的 `max_active_sessions` 配额。关闭时如果还有对话在进行，那一轮会被取消；关闭后不能再发消息（`409 conflict`）。只想停掉正在生成的那一轮、会话接着用，见[停止生成](#停止生成)。

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
| `turn_completed` | 这一轮结束 | `usage`、`iterations`、`duration_ms`；被停下时 `end_reason` 是 `cancelled` |
| `turn_failed` | 这一轮失败 | 失败原因 |
| `turn_suspended` | 这一轮停在审批点 | `pending` |
| `suspension_resolved` | 审批结果已送达，开始续跑 | `request_ids` |

事件来自 taifeng 内核，种类会随内核版本增加（比如 `engine_log`、`rewind_checkpoint_recorded`）。**遇到不认识的事件类型请忽略**，不要当成错误。

### 怎样判断一轮结束

一轮对话以这三种事件之一结束：`turn_completed`、`turn_failed`、`turn_suspended`。

有子 skill 参与时，子 skill 自己的完成事件也会出现在流里。区分办法是看 `data.is_root`：`turn_completed` 和 `turn_failed` 只有 `is_root` 为真才是这一轮的结束；`turn_suspended` 还可能来自子Skill，不能看到第一条就停止读取。新消息等根线程挂起；恢复操作等目标叶线程或其原链祖先再次挂起。送回审批（`resume`）没有续跑时，事件流以 `suspension_resolve_rejected`（被拒）或 `suspension_partially_resolved`（只处理了一部分待办）结束。`2026.10.23.1`起SDK的 `event.terminal` 优先读取服务端 `data.operation_terminal` 布尔；新服务端在实时流与审计回放中提供相同操作级标记。旧服务端缺字段时兼容旧规则；旧SDK会将任何挂起视为终态，处理嵌套会话时应升级或让迭代器读到本次HTTP响应结束。

直接读 HTTP 响应时，平台在本操作安全结束后补一帧 `event: end`，读到它才结束。终态前父链与待办审计已经落盘；随后调用 `pending` 读取当前人工待办，纯内部父挂起不会成为人工确认卡。恢复只传决定，目标线程由服务端按本人会话的可信审计定位。

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
- **会话可见性先于审批**：普通员工与代表普通员工的调用只能操作本人会话。未代调用的本租户 API key 仍可调任何可见会话的 `resume`；P1 没有更细的审批人策略，谁能批什么由接入系统把关。主人停用后所有调用者都不能再送审批。审计的 `actor` 记录触发者，`user_id` 记录会话主人。

## 停止生成

用户点了「停止」，或者你的系统不想再等这一轮了，就把它停下。会话保持活动，可以接着发消息：

```python
cancelled = await client.sessions.cancel(session.session_id, submission_id=submission_id)
print(cancelled)                                  # ['sub_9d2b8c768998']
```

`submission_id` 是事件流里每条事件都带着的那个（`event.submission_id`，或第一帧 `turn`、响应头 `X-Submission-Id`）。省略它就停掉这个会话里全部在途的对话。返回的是这次真正提交了取消的那几轮。

接口只是提交取消，不等那一轮收尾；被停的那一轮从**它自己的事件流**照常结束：

```python
print(last.kind, last.terminal, last.data["end_reason"], last.data["cancel_reason"])
# turn_completed True cancelled requested
```

还在排队、没开始跑的那一轮则以 `turn_failed`（`data.kind` 为 `cancelled`）结束。`event.terminal` 两种都算好了。已经输出的那部分文本照样记进审计（回放里的 `turn_text`）。被停的一轮照样计一轮用量；被打断的那次采样网关报不出用量，平台按上下文估算计入输入（见[审计与用量](audit-and-usage.md#用量)）。

停止生成不打断正在进行的连接器调用：模型已经发起的调用要等它跑完（最长到连接器超时）这一轮才结束，调用结果照常记进审计，之后不再采样。

和关闭会话的区别：

| | 停止生成 | 关闭会话 |
| --- | --- | --- |
| 停什么 | 在途的那一轮（或指定的那一轮） | 会话里的一切 |
| 之后 | 会话照常可用，接着发消息 | 不能再发消息（`409 conflict`），不再计入活动会话配额 |

几种碰巧的情况，都按「已经停不了」处理即可：

- **点击到达之前这一轮已经结束**：不带 `submission_id` 返回 `[]`；带了返回 `400 invalid_request`（「不在途（已经结束或不存在）」）。
  ```python
  print(await client.sessions.cancel(session.session_id))   # []
  try:
      await client.sessions.cancel(session.session_id, submission_id=submission_id)
  except XuanpuError as exc:
      print(exc.status, exc.code, exc.message)
  # 400 invalid_request submission 'sub_9d2b8c768998' 不在途（已经结束或不存在）
  ```
- **会话还在准备**（这个实例刚接手会话、第一次组装业务包）：取消返回 `[]`，随后开始的那一轮不受影响。看到 `[]` 而事件流还在走，稍等再停一次。
- **内核还没接手刚发出的那一轮**：平台最多等约 1 秒，到点仍没接手就不提交，返回值里没有它。
- **刚送回批准就停**：被批准的那次调用照样执行（它在续跑那一轮之前执行），停下的只是之后的采样。
- **挂起等审批的那一轮不归它管**：它已经不在途了。要放弃，就把审批拒绝送回。

多实例部署下，会话在另一个实例上时返回 `409 session_busy`（带 `owner`），与发消息一样靠粘性路由避免。未代调用的本租户 API key 能停本租户任意会话；普通员工及代表普通员工的调用只能停本人会话。只想停指定一轮，就带上它的 `submission_id`。审计里的 `turn_cancel_requested` 区分触发者 `actor` 与会话主人 `user_id`。

## 代表员工调用

管理员签发 key 时显式传 `act_as_users: true`，业务后端在每次请求中带 `X-Xuanpu-Act-As: <user_id>`。目标必须是同租户启用员工；没有能力、目标不存在或停用均为 403。角色取 key 和员工中较低者，代普通成员不会获得管理员权限。

```bash
curl -fsS -X POST "$BASE/v1/keys" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"name":"workflow","role":"tenant_member","act_as_users":true}'
# 返回一次性的 key 与 key_id；安全保存，令 DELEGATED_KEY 为该 key
curl -fsS "$BASE/v1/me" \
  -H "Authorization: Bearer $DELEGATED_KEY" -H "X-Xuanpu-Act-As: $MEMBER_USER"
```

这两步已在本地真实 CLI 与模拟员工下运行，`me.role` 为 `tenant_member`，`me.user_id` 为目标员工，`actor` 为 `key:<key_id>/user:<user_id>`。变量来源与员工登录见[接入飞书登录](feishu-login.md)。

Python SDK 在创建客户端时固定身份，普通请求与事件流均使用同一身份头：

```python
async with XuanpuClient(base_url, delegated_key, act_as_user=user_id) as employee:
    me = await employee.me()
    session = await employee.sessions.create("demo")
    assert session.user_id == user_id
    await employee.sessions.close(session.session_id)
```

上例已用本地验收业务包 `demo` 经网络运行。代表另一个员工时另建客户端，不修改共享客户端的请求头。普通成员的会话列表只返回本人会话，访问别人会话为 404；管理员仍可看本租户全部会话。`mine=True` 对代表员工的调用按员工过滤，其他 key 按创建者过滤。

管理员查询员工审计时，`client.admin.audit(user_id=..., actor=...)` 分别筛主人/被操作员工与触发者；`client.admin.user_usage(since, until, user_id=...)` 返回按用户用量。会话的无主记录 `user_id=None`；员工停用后登录和代调用立即拒绝，主人已停用的会话不能继续发消息或送审批。

## 断线之后

调用方断开连接，**这一轮对话不会停**，平台会把它跑完；要让它停下，用[停止生成](#停止生成)。重新连上之后不要重发消息（那会开启新的一轮），用 `submission_id` 回放：

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

`after_seq` 参数只筛选有序号的事件，用来接着上次读到的位置继续。审计合成的 `turn_text` 全文没有流序号，`seq` 为 `null`（SDK为 `None`），总会返回，不用它更新游标。

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
| 503 | `runtime_unavailable` | 沙盒、连接器、对象存储不可用；实例正在停机；或者会话的持有在准备期间中断过（提示里有「请重试」） | 稍后重试；多实例部署下换一个实例。提示「请重试」的直接重试 |
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

- [停止生成](#停止生成)只停在途的那一轮：挂起等审批的那一轮要靠拒绝审批来放弃，已经执行的调用不会撤回。
- 没有 JavaScript 客户端；浏览器端请直接用 `fetch` 读事件流（控制台的 [`app.js`](../../packages/xuanpu-console/src/xuanpu_console/static/app.js) 是一个可以参照的实现）。
- 消息只支持文本，不支持图片与文件。
