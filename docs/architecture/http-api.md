# HTTP 接口（活文档）

> 第一版接口。实现在 `packages/xuanpu-server/src/xuanpu_server/api/`。
> 用法与注意事项见[应用接入](../guides/integrating.md)；本文是逐个接口的参考。

## 约定

- 请求体与响应体都是 JSON（发布业务包的请求体除外）。请求体里多余的字段一律拒绝。
- 时间都是 Unix 秒（浮点数）；用量的日期是 `YYYY-MM-DD`（UTC）。
- 框架自动生成的接口描述（`/openapi.json`）与交互式文档页面（`/docs`、`/redoc`）都是关闭的（`404`）：它们不需要凭据，而且不含事件流的帧格式。接口以本文为准。

## 认证

每个请求带 `Authorization: Bearer <凭据>`。凭据有两种：

| 凭据 | 来源 | 身份 |
| --- | --- | --- |
| 根凭据 `xproot_…` | `xuanpu keygen root` 生成，摘要写进配置 | 平台管理员，不属于任何租户 |
| API key `xp_<标识>_<密钥>` | 签发时返回一次，平台只存摘要 | 租户管理员（`tenant_admin`）或租户成员（`tenant_member`） |

下文「需要的身份」一栏里，「租户成员」指任何一把本租户的 API key（管理员也算）。平台管理员不属于任何租户，不能调用租户内的接口。

各种认证失败（格式不对、不存在、已吊销、租户被停用）对外是同一个 `401`，不透露凭据或租户是否存在。

## 错误

```json
{"error": {"code": "quota_exceeded", "message": "今日 token 用量已达上限 1000000"}}
```

| 状态码 | `code` | 含义 | 附带字段 |
| --- | --- | --- | --- |
| 400 | `invalid_request` | 请求不合法 | 字段校验不过时有 `problems` |
| 400 | `package_invalid` | 业务包不符合规范 | `problems`：逐条的问题 |
| 400 | `secret_missing` | 业务包引用的密钥尚未配置 | |
| 401 | `unauthenticated` | 没有凭据或凭据无效 | |
| 403 | `permission_denied` | 凭据有效但无权操作 | |
| 404 | `not_found` | 资源不存在，或属于别的租户 | |
| 409 | `conflict` | 与现有状态冲突（版本已发布、会话已关闭等） | |
| 409 | `session_busy` | 会话正由另一个实例持有 | `owner`：持有者的实例标识 |
| 429 | `quota_exceeded` | 超出租户配额 | |
| 500 | `secret_corrupted` | 密钥的密文解不开（加密它的主密钥已不在配置里，或密文被改动） | |
| 500 | `internal_error` | 未预料的错误；细节只进服务端日志 | |
| 503 | `runtime_unavailable` | 沙盒或连接器不可用，或实例正在停机 | |

`code` 是稳定的，`message` 的文案可能调整。

## 接口一览

| 方法与路径 | 需要的身份 | 说明 |
| --- | --- | --- |
| `GET /healthz` | 无 | 存活探测 |
| `POST /v1/admin/tenants` | 平台管理员 | 新建租户 |
| `GET /v1/admin/tenants` | 平台管理员 | 列出租户 |
| `PUT /v1/admin/tenants/{id}/status` | 平台管理员 | 启用或停用租户 |
| `POST /v1/admin/tenants/{id}/keys` | 平台管理员 | 给租户签发 API key |
| `POST /v1/keys` · `GET /v1/keys` · `DELETE /v1/keys/{id}` | 租户管理员 | 本租户的 API key |
| `PUT /v1/secrets/{name}` · `GET /v1/secrets` · `DELETE /v1/secrets/{name}` | 租户管理员 | 密钥。取值写入后不可再读出 |
| `POST /v1/packages?activate=` | 租户管理员 | 发布业务包，请求体是 zip 归档 |
| `GET /v1/packages?name=` | 租户成员 | 列出已发布版本 |
| `POST /v1/packages/{name}/activate` | 租户管理员 | 切换生效版本（前进或回滚） |
| `POST /v1/packages/{name}/evals` | 租户管理员 | 运行评测用例，返回报告 |
| `POST /v1/sessions` · `GET /v1/sessions?status=&mine=&limit=` | 租户成员 | 新建、列出会话 |
| `GET /v1/sessions/{id}` · `DELETE /v1/sessions/{id}` | 租户成员 | 取会话、关闭会话 |
| `GET /v1/sessions/{id}/pending` | 租户成员 | 会话当前等待处理的审批请求 |
| `GET /v1/sessions/{id}/events?submission_id=&after_seq=` | 租户成员 | 回放一轮对话已记入审计的事件 |
| `POST /v1/sessions/{id}/messages` | 租户成员 | 发消息；响应是事件流 |
| `POST /v1/sessions/{id}/resume` | 租户成员 | 送回审批结果；响应是事件流 |
| `POST /v1/sessions/{id}/cancel` | 租户成员 | 停止生成：取消会话里在途的对话 |
| `GET /v1/audit` | 租户管理员 | 查询审计 |
| `GET /v1/usage?since=&until=` | 租户管理员 | 查询用量 |

## 存活探测

```
GET /healthz
→ {"status": "ok", "version": "2026.9.30.1", "instance": "inst_9c7a5cea5506bdae0d89e8da"}
```

## 租户

```
POST /v1/admin/tenants
{"tenant_id": "acme", "name": "Acme",
 "quota": {"max_active_sessions": 200, "max_daily_tokens": 5000000, "max_concurrent_turns": 20}}

→ 201
{"tenant_id": "acme", "name": "Acme", "status": "active",
 "quota": {"max_active_sessions": 200, "max_daily_tokens": 5000000, "max_concurrent_turns": 20},
 "created_at": 1790753313.904835}
```

| 字段 | 说明 |
| --- | --- |
| `tenant_id` | 3–40 个字符，小写字母开头，只含小写字母、数字、连字符，不以连字符结尾。创建后不可改 |
| `name` | 显示名，1–200 个字符 |
| `quota` | 可省略。三项各自可省略或为 `null`，表示不限 |

| 配额 | 含义 | 超出时 |
| --- | --- | --- |
| `max_active_sessions` | 同时处于活动状态的会话数 | 新建会话返回 429 |
| `max_daily_tokens` | 每个自然日（UTC）的 token 用量 | 发消息返回 429；拦的是下一轮，当天实际用量可能超出一轮对话的量 |
| `max_concurrent_turns` | 同一时刻进行中的对话数 | 发消息、送回审批返回 429；只在本实例内计数 |

`GET /v1/admin/tenants` 返回 `{"tenants": [ … ]}`，每项同上。

```
PUT /v1/admin/tenants/{id}/status
{"status": "suspended"}        # 或 "active"
→ 租户对象
```

停用后该租户的凭据立即失效（`401`）。

目前没有修改配额与删除租户的接口。

## API key

```
POST /v1/admin/tenants/{id}/keys        # 平台管理员，给指定租户签发
POST /v1/keys                           # 租户管理员，给本租户签发
{"name": "backend", "role": "tenant_member"}        # role 省略时是 tenant_member

→ 201
{"key": "xp_be139be29730_…", "key_id": "be139be29730", "tenant_id": "acme",
 "name": "backend", "role": "tenant_member", "created_at": 1790753313.91, "revoked_at": null}
```

`key` 只在这次响应里出现。`role` 只能是 `tenant_admin` 或 `tenant_member`。

`GET /v1/keys` 返回 `{"keys": [ … ]}`（不含 `key`）。`DELETE /v1/keys/{key_id}` 吊销，返回 `{"revoked": true}`；已吊销或不存在是 `404`。

## 密钥

```
PUT /v1/secrets/{name}
{"value": "…"}
→ {"name": "tickets-token"}
```

密钥名：小写字母开头，只含小写字母、数字、连字符，最长 63，与业务包清单里引用的写法一致。取值最大 64 KiB。同名再写就是覆盖。

`GET /v1/secrets` 返回 `{"names": ["tickets-token"]}`，**没有读取取值的接口**。`DELETE /v1/secrets/{name}` 返回 `{"deleted": true}`。

## 业务包

```
POST /v1/packages?activate=true
Content-Type: application/zip
<归档的字节>

→ 201
{"name": "support-desk", "version": "1.0.0", "digest": "sha256:53ad4612…", "active": true,
 "published_at": 1790753314.06, "published_by": "key:be139be29730"}
```

| 情况 | 响应 |
| --- | --- |
| 归档或内容不符合规范，或内核加载不了其中的 skill | `400 package_invalid`，`problems` 列出每个问题 |
| 同名同版本已发布过 | `409 conflict` |
| 归档超过 64 MiB | `400 invalid_request` |

`activate` 省略时只发布、不生效。

`GET /v1/packages` 返回 `{"packages": [ … ]}`：本租户全部已发布的版本，同名的按版本号从低到高；`?name=` 只看一个包。

```
POST /v1/packages/{name}/activate
{"version": "1.0.0"}
→ {"name": "support-desk", "active": "1.0.0"}
```

```
POST /v1/packages/{name}/evals
{"version": "1.0.0", "names": ["greet"]}        # 都可省略：生效版本、全部用例

→ {"package": "support-desk", "version": "1.0.0", "passed": true, "total": 1, "failed": 0,
   "total_tokens": 456,
   "cases": [{"name": "greet", "passed": true, "reasons": [], "reply": "您好！…",
              "outcome": "completed", "total_tokens": 456, "duration_ms": 86,
              "session_id": "ses_…"}]}
```

`outcome` 是 `completed`、`failed`、`suspended` 或 `error`（用例没能跑起来）。用例同时最多跑 4 个；请求要等全部跑完才返回，客户端的超时要留够。

## 会话

```
POST /v1/sessions
{"package": "support-desk", "version": "1.0.0", "entry": "triage", "metadata": {"customer": "C-42"}}

→ 201
{"session_id": "ses_1197ee27deb1aa819440d23c", "package": "support-desk", "version": "1.0.0",
 "entry": "triage", "status": "active", "owner": null,
 "created_at": 1790753324.73, "updated_at": 1790753324.73,
 "created_by": "key:be139be29730", "metadata": {"customer": "C-42"}}
```

| 字段 | 说明 |
| --- | --- |
| `package` | 必填 |
| `version` | 省略用生效版本。会话创建时钉住版本 |
| `entry` | 省略用清单 `entries` 的第一个；不在 `entries` 里是 `400` |
| `metadata` | 业务方自己的关联标识：最多 16 项，键值都是字符串、各 1–256 个字符。原样返回并进审计 |

响应里的 `status` 是 `active` 或 `closed`；`owner` 是当前持有会话的实例，无人持有为 `null`。

`GET /v1/sessions` 返回 `{"sessions": [ … ]}`，最近活动的在前。`status=active|closed` 过滤状态，`mine=true` 只看本凭据创建的，`limit` 默认 50、最大 200。

`DELETE /v1/sessions/{id}` 关闭会话，返回会话对象。进行中的对话会被取消。重复关闭不报错。只想停掉进行中的那一轮、会话接着用，见[停止生成](#停止生成)。

### 发消息

```
POST /v1/sessions/{id}/messages
{"text": "查一下工单 T-1001"}
→ 事件流
```

`text` 不能为空，最长 200 000 个字符。会话已关闭是 `409 conflict`。

### 事件流

发消息与送回审批结果的响应都是 Server-Sent Events：

```
event: turn
data: {"session_id": "ses_…", "submission_id": "sub_…"}

event: turn_started
id: 0
data: {"submission_id": "sub_…", "seq": 0, "at": 1790753324.76, "kind": "turn_started", "data": {…}}

event: assistant_text
id: 14
data: {"…": "…", "kind": "assistant_text", "data": {"delta": "您好"}}

event: turn_completed
id: 16
data: {"…": "…", "kind": "turn_completed", "data": {"usage": {…}, "is_root": true}}

event: end
data: {"submission_id": "sub_…", "overflowed": false}
```

- 第一帧 `turn` 给出本轮的 `submission_id`（响应头 `X-Submission-Id` 里也有）。
- 之后每条内核事件一帧，事件名就是事件类型，`id` 是事件序号。
- 一轮对话以 `turn_completed`、`turn_failed`（两者要 `data.is_root` 为真）或 `turn_suspended` 结束，最后补一帧 `end`。送回审批的那次请求没有续跑时，以 `suspension_resolve_rejected`（被拒，`data.reason` 说明原因）或 `suspension_partially_resolved`（只核销了一部分待办）结束，见[审批](#审批)。
- 没有事件时每 15 秒发一行 `: keepalive` 注释。
- 含完整提示词的事件不外发。
- `end` 里的 `overflowed` 为真，表示调用方读得太慢、这条流被平台放弃（缓冲 8192 条）；对话本身不受影响。

调用方在本轮结束前断开，对话会继续跑完；要让它停下，用[停止生成](#停止生成)。

### 审批

需要审批时本轮以 `turn_suspended` 结束，`data.pending` 列出待办：

```json
{"request_id": "req_7fcbe0950662111f", "reason": "permission",
 "payload_schema": {"type": "object", "properties": {"granted": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["granted"]},
 "related_call_id": "call_3",
 "detail": {"scope": "tool_use", "target": "api__tickets__updateTicket",
            "reason": "调用连接器 tickets 的 updateTicket（write）",
            "args": {"id": "T-1001", "body": {"priority": "high"}}}}
```

`GET /v1/sessions/{id}/pending` 随时返回当前的待办：`{"pending": [ … ]}`，没有待办时是空列表。

把结果送回：

```
POST /v1/sessions/{id}/resume
{"resolutions": {"req_7fcbe0950662111f": {"granted": true}}}
→ 事件流
```

拒绝是 `{"granted": false, "reason": "…"}`。批准后被挂起的那次调用才真正执行；拒绝则不执行，模型收到被拒的结果后继续。`resolutions` 不能为空。

不是每次送回都会续跑：

| 情况 | 事件流的最后一条 |
| --- | --- |
| 送回之后全部待办都处理了 | 续跑的那一轮照常结束（`turn_completed` 等） |
| 只送回了一部分待办 | `suspension_partially_resolved`：送回的已处理（批准的调用已执行），其余还在 `pending` 里等着；全部送回之后才续跑 |
| 被内核拒绝（没有活跃的挂起、答复不合格、同一挂起正在处理等） | `suspension_resolve_rejected`，`data.reason` 说明原因；什么也没执行 |

后两种没有采样，不记一轮用量，回放里也没有 `turn_text`。

### 停止生成

```
POST /v1/sessions/{id}/cancel
{"submission_id": "sub_…"}        # 可省略（整个请求体也可省略）：停掉会话里全部在途的对话
→ {"cancelled": ["sub_…"]}
```

`cancelled` 是这次提交了取消的那几轮的 `submission_id`。平台先等内核受理了那一轮（最多约 1 秒）再提交取消：刚发出、内核还没接手的一轮会等它接手；等待期间已经收尾或挂起的、到点仍没受理的，不提交，也不出现在 `cancelled` 里。接口只是提交取消，不等那一轮收尾；被停的那一轮的事件流照常结束：

| 被停时 | 事件流的最后一条 |
| --- | --- |
| 正在跑 | `turn_completed`，`data.end_reason` 为 `cancelled`、`data.success` 为 `false`、`data.cancel_reason` 为 `requested` |
| 还在排队（前一轮没结束） | `turn_failed`，`data.kind` 为 `cancelled` |

会话保持活动，可以接着发消息。被停的那一轮照样记一轮用量，排队中被停、没开始跑的那一轮也计一轮（token 为 0）；卡在半路的那次采样没有收尾，网关报不出它的 token，平台按上下文估算计入输入（审计里记一条 `usage_estimated`，见[审计与用量](../guides/audit-and-usage.md)）。

本租户任何一把 API key 都能停任何会话里的对话，与 `resume` 一样不区分对话是谁发起的；审计里的 `turn_cancel_requested` 记下是哪把 key 停的。只想停自己发起的那一轮，就带上它的 `submission_id`。

| 情况 | 响应 |
| --- | --- |
| 没有在途的对话（包括会话已关闭） | `200`，`cancelled` 为空列表 |
| 这一轮还没开始（会话还在准备引擎，或内核约 1 秒内仍没接手） | `200`，`cancelled` 里没有它 |
| 给了 `submission_id`，但它不在途（已经结束或不存在） | `400 invalid_request` |
| `submission_id` 为空串或超过 64 个字符 | `400 invalid_request` |
| 会话正由另一个实例持有 | `409 session_busy`，带 `owner` |
| 会话不存在或属于别的租户 | `404 not_found` |

挂起等审批的那一轮已经不在途，不归这个接口管：要放弃就把审批拒绝送回（见[审批](#审批)）。送回批准之后停掉续跑的那一轮，被批准的那次调用照样执行（它在续跑之前执行），停下的只是之后的采样。

### 回放

```
GET /v1/sessions/{id}/events?submission_id=sub_…&after_seq=13

→ {"submission_id": "sub_…",
   "events": [{"kind": "turn_completed", "seq": 16, "at": 1790753324.87, "data": {…}},
              {"kind": "turn_text", "seq": null, "at": 1790753324.87, "data": {"text": "工单 T-1001…"}}]}
```

返回这一轮已记入审计的事件：不含流式增量；一轮结束时另有一条 `turn_text` 带着回复全文。`after_seq` 省略时返回全部，给了就只返回序号更大的（`turn_text` 没有序号，总会返回）。

## 审计

```
GET /v1/audit?session_id=&kind=&source=&since=&until=&after_id=&limit=

→ {"records": [{"audit_id": 51, "source": "gate", "kind": "connector_call", "at": 1790753335.73,
                "actor": "engine", "session_id": "ses_…", "stream": null, "seq": null, "data": {…}}],
   "next_after_id": null}
```

| 参数 | 说明 |
| --- | --- |
| `session_id`、`kind` | 精确匹配 |
| `source` | `engine`、`gate`、`control` |
| `since`、`until` | Unix 秒；`since` 含，`until` 不含 |
| `after_id` | 只返回编号更大的记录，默认 0 |
| `limit` | 默认 100，最大 1000 |

记录按 `audit_id` 升序。`next_after_id` 不为 `null` 表示可能还有下一页。各字段与各种事件的含义见[审计与用量](../guides/audit-and-usage.md)。

## 用量

```
GET /v1/usage?since=2026-09-01&until=2026-09-30

→ {"usage": [{"day": "2026-09-30", "input_tokens": 2940, "output_tokens": 252,
              "total_tokens": 3192, "turns": 5}]}
```

两个参数都必填，两端都含。没有用量的日子不出现。
