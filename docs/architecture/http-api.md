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
| `POST /v1/sessions` · `GET /v1/sessions` | 租户成员 | 新建、列出会话 |
| `GET /v1/sessions/{id}` · `DELETE /v1/sessions/{id}` | 租户成员 | 取会话、关闭会话 |
| `POST /v1/sessions/{id}/messages` | 租户成员 | 发消息；响应是事件流 |
| `POST /v1/sessions/{id}/resume` | 租户成员 | 送回审批结果；响应是事件流 |
| `GET /v1/audit` | 租户管理员 | 查询审计，按 `next_after_id` 翻页 |
| `GET /v1/usage?since=&until=` | 租户管理员 | 查询用量 |

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
