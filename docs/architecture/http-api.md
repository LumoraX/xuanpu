# HTTP 接口（活文档）

> 第一版接口。实现在 `packages/xuanpu-server/src/xuanpu_server/api/`。

## 认证

每个请求带 `Authorization: Bearer <凭据>`。凭据有两种：

| 凭据 | 来源 | 身份 |
| --- | --- | --- |
| 根凭据 `xproot_…` | `xuanpu keygen root` 生成，摘要写进配置 | 平台管理员，不属于任何租户 |
| API key `xp_<标识>_<密钥>` | 签发时返回一次，平台只存摘要 | 租户管理员或租户成员 |

各种认证失败对外是同一个 `401`，不透露凭据或租户是否存在。

## 错误

```json
{"error": {"code": "quota_exceeded", "message": "今日 token 用量已达上限 1000000"}}
```

| 状态码 | `code` | 含义 |
| --- | --- | --- |
| 400 | `invalid_request` / `package_invalid` / `secret_missing` | 请求不合法。`package_invalid` 带 `problems` 列表 |
| 401 | `unauthenticated` | 没有凭据或凭据无效 |
| 403 | `permission_denied` | 凭据有效但无权操作 |
| 404 | `not_found` | 资源不存在，或属于别的租户 |
| 409 | `conflict` / `session_busy` | 与现有状态冲突。`session_busy` 带 `owner` |
| 429 | `quota_exceeded` | 超出租户配额 |
| 503 | `runtime_unavailable` | 沙盒、连接器或模型网关不可用 |
| 500 | `internal_error` | 未预料的错误；细节只进服务端日志 |

## 接口

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
| `GET /v1/packages` | 租户成员 | 列出已发布版本 |
| `POST /v1/packages/{name}/activate` | 租户管理员 | 切换生效版本（前进或回滚） |
| `POST /v1/packages/{name}/evals` | 租户管理员 | 运行评测用例，返回报告；可指定 `version`、`names` |
| `POST /v1/sessions` · `GET /v1/sessions?status=&mine=` | 租户成员 | 新建（可带 `metadata`：业务方自己的关联标识，原样返回并进审计）、列出会话（`mine=true` 只看本凭据创建的） |
| `GET /v1/sessions/{id}` · `DELETE /v1/sessions/{id}` | 租户成员 | 取会话、关闭会话 |
| `GET /v1/sessions/{id}/pending` | 租户成员 | 会话当前等待处理的审批或表单请求 |
| `GET /v1/sessions/{id}/events?submission_id=&after_seq=` | 租户成员 | 回放一轮对话已记入审计的事件（断线后补齐） |
| `POST /v1/sessions/{id}/messages` | 租户成员 | 发消息；响应是事件流 |
| `POST /v1/sessions/{id}/resume` | 租户成员 | 送回审批结果；响应是事件流 |
| `GET /v1/audit` | 租户管理员 | 查询审计，按 `next_after_id` 翻页 |
| `GET /v1/usage?since=&until=` | 租户管理员 | 查询用量 |

## 租户与配额

新建租户时可以带配额，三项都可省略，省略表示不限：

```
POST /v1/admin/tenants
{"name": "acme", "quota": {"max_active_sessions": 200, "max_daily_tokens": 5000000, "max_concurrent_turns": 20}}
```

| 字段 | 含义 | 超出时 |
| --- | --- | --- |
| `max_active_sessions` | 同时处于活动状态的会话数 | 新建会话返回 429 |
| `max_daily_tokens` | 每个自然日（UTC）的 token 用量 | 发消息返回 429；拦的是下一轮，当天实际用量可能超出一轮对话的量 |
| `max_concurrent_turns` | 同一时刻进行中的对话数 | 发消息返回 429；只在本实例内计数 |

## 事件流

发消息与送回审批结果的响应都是 Server-Sent Events：

```
event: turn
data: {"session_id": "ses_…", "submission_id": "sub_…"}

event: turn_started
id: 0
data: {"submission_id": "sub_…", "seq": 0, "at": 1759190400.1, "kind": "turn_started", "data": {…}}

event: assistant_text
id: 3
data: {"…": "…", "kind": "assistant_text", "data": {"delta": "您好"}}

event: turn_completed
id: 9
data: {"…": "…", "kind": "turn_completed", "data": {"usage": {…}, "is_root": true}}

event: end
data: {"submission_id": "sub_…", "overflowed": false}
```

- 第一帧 `turn` 给出本轮的 `submission_id`（响应头 `X-Submission-Id` 里也有）。
- 之后每条内核事件一帧，事件名就是事件类型。
- 一轮对话以 `turn_completed`、`turn_failed` 或 `turn_suspended` 结束，最后补一帧 `end`。
- 含完整提示词的事件不外发。

调用方在本轮结束前断开时，用 `GET /v1/sessions/{id}/events?submission_id=…` 回放：里面是已记入审计的事件（不含流式增量），本轮结束时另有一条 `turn_text` 带着拼好的回复全文。

## 审批

需要审批时本轮以 `turn_suspended` 结束，`data.pending` 列出待办：

```json
{"request_id": "req_…", "reason": "permission",
 "detail": {"scope": "tool_use", "target": "mcp__crm__update_ticket", "args": {"id": "T-1"}}}
```

把结果送回：

```
POST /v1/sessions/{id}/resume
{"resolutions": {"req_…": {"granted": true}}}
```

批准后被挂起的那次调用才真正执行；拒绝则不执行，模型收到被拒的结果后继续。
