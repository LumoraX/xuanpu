# 审计与用量

面向安全、合规与运营：平台记下了什么、没记什么，怎么查、怎么导出，用量怎么算。

## 一条审计记录

```json
{
  "audit_id": 18,
  "source": "gate",
  "kind": "connector_call",
  "at": 1790910685.734703,
  "actor": "engine",
  "session_id": "ses_2846973c9d4bdf55f28591d9",
  "stream": null,
  "seq": null,
  "data": {
    "connector": "tickets", "transport": "http", "tool": "updateTicket", "effect": "write",
    "decision": "allowed", "is_error": false, "detail": "", "call_id": "call_3",
    "thread_id": "thr_96548b0d001a65d6",
    "arguments_digest": "sha256:ab129ec1c7282db457ccb63ffaa5de0ae0929b62aba56e9f0a3fe52d84f7929f",
    "submission_id": "sub_6adfb91f890e", "duration_ms": 1
  },
  "user_id": "usr_5f0c1b2e7d9a4c3b8e6f1a20"
}
```

这是一次批准后才执行的写操作（样例包改工单）：`submission_id` 是送回批准的那一轮；会话是员工 `usr_5f0c…` 的，所以这条过闸记录归到他名下。

| 字段 | 含义 |
| --- | --- |
| `audit_id` | 平台分配的递增编号，也是翻页位置 |
| `source` | 来源，三种之一：`control`、`gate`、`engine` |
| `kind` | 事件类型 |
| `at` | 发生时间，Unix 秒 |
| `actor` | 谁触发的。用 API key 的是 `key:<API key 标识>`（根凭据是 `key:root`）；浏览器登录的员工是 `user:<员工标识>`；代员工调用是 `key:<API key 标识>/user:<员工标识>`（见[认证](../architecture/http-api.md#认证)）；模型驱动的是 `engine`；飞书登录失败、还没认出是哪名员工时是 `anonymous`；平台自己发起的是 `system:<名字>`：`system:feishu-event`（飞书离职事件停用员工）、`system:user-disabled`（员工停用后关闭他的会话） |
| `session_id` | 关联的会话；与会话无关的操作为空 |
| `stream`、`seq` | 只有内核事件有：事件流的标识与流内序号 |
| `data` | 事件内容 |
| `user_id` | 这条记录归到哪名员工名下（见下文「记录归到哪名员工」）；与员工无关的为空 |

记录只追加，平台没有修改或删除审计的接口。

### 记录归到哪名员工

`actor` 说的是谁触发的，`user_id` 说的是这条记录归到哪名员工名下，两者不一定相同：

| 记录 | `user_id` |
| --- | --- |
| 会话里的一切记录：会话上的控制面操作（`session_created`、`message_sent`、`suspension_resolved_by`、`turn_cancel_requested`、`session_closed`）、内核事件（`source=engine`，含平台补的 `turn_text`、`usage_estimated`、`audit_gap`）、过闸记录（`source=gate`，含人工拒绝的补记） | 会话的主人，不论是谁触发的（是谁触发的看 `actor`） |
| 针对某名员工的操作：`user_role_changed`、`user_disabled`、`user_enabled`、`user_cleanup_failed` | 被操作的这名员工，不论是谁做的（是谁做的看 `actor`：管理员、API key，或 `system:feishu-event`） |
| 其余与会话无关的控制面操作（签发 key、写密钥、发布业务包、退出登录等） | 调用者代表的员工：浏览器登录的员工，或代员工调用时被代的员工；用 API key、根凭据直接调的为空 |
| 飞书登录的几种记录 | 认出的员工，见下文 |

会话的主人是建会话时调用者代表的员工；API key 直接建的会话没有主人，会话里的记录 `user_id` 都为空。比如管理员用自己的 API key 批准了员工张三会话里的一次写操作：`suspension_resolved_by` 的 `actor` 是那把 key、`user_id` 是张三；随后执行的那次调用的过闸记录 `actor` 是 `engine`、`user_id` 也是张三——两条都是张三会话里的记录。所以按 `user_id=张三` 能看到他会话里的全部经过（包括别人在里面做的事）；要查「这个管理员在别人的会话里做了什么」，按 `actor` 查（见下文「查询」）。

## 三个来源

### `control`：人做了什么

每次控制面操作一条，同步写入：

| `kind` | 什么时候记 | `data` |
| --- | --- | --- |
| `tenant_created` | 新建租户 | `name` |
| `tenant_status_changed` | 停用或启用租户 | `status` |
| `tenant_quota_changed` | 修改租户配额（平台管理员） | `before`、`after`：改之前与改之后的 `max_active_sessions`、`max_daily_tokens`、`max_concurrent_turns`（`null` 是不限） |
| `api_key_issued` | 签发 API key | `key_id`、`name`、`role`、`act_as_users`（是否授予代员工调用的能力） |
| `api_key_revoked` | 吊销 API key | `key_id` |
| `secret_written` | 写入或覆盖密钥 | `name`（只有名字） |
| `secret_deleted` | 删除密钥 | `name` |
| `package_published` | 发布业务包版本 | `name`、`version`、`digest` |
| `package_activated` | 切换生效版本 | `name`、`version`、`previous` |
| `eval_completed` | 跑完一次评测 | `package`、`version`、`passed`、`failed`、`total_tokens` |
| `session_created` | 新建会话 | `package`、`version`、`entry`、`metadata` |
| `assistant_route_sampled` | 直接对话入口完成或中断业务识别；计入租户与员工 token 用量，不增加业务轮数 | `estimated`、`input_tokens`、`output_tokens`、`total_tokens`、`turns=0`；不记录识别提示词与模型原文 |
| `session_closed` | 关闭会话。员工停用后由平台关掉的，`actor` 是 `system:user-disabled` | — |
| `message_sent` | 用户发出一条消息 | `submission_id`、`text` |
| `suspension_resolved_by` | 送回审批决定 | `submission_id`、`decisions`（待办标识 → 批准与否） |
| `turn_cancel_requested` | 停止生成（确有在途对话被停时才记）。与会话上的其他操作一样，`user_id` 是会话的主人，是谁停的看 `actor` | `submission_ids`：停了哪些 |
| `user_created` | 员工首次[飞书登录](../architecture/http-api.md#飞书登录)，平台自动给他建账号 | `provider`（`feishu`）、`display_name`、`role`（总是 `tenant_member`） |
| `login_succeeded` | 飞书登录成功 | `provider` |
| `login_failed` | 飞书登录失败，而且飞书确实认出了一个人：`not_in_app_scope`（不在应用的可用范围内）、`tenant_mismatch`（飞书企业不符）、`user_disabled`（员工已停用）。别的失败原因（员工拒绝授权、登录链接失效、租户不可用、调飞书失败）只计指标 `xuanpu_logins_total`、记平台日志，不写审计——登录接口不需要凭据，否则谁都能往审计里灌记录 | `reason`：失败原因，含义见[接口文档的 `login_error` 表](../architecture/http-api.md#飞书登录) |
| `logout` | 员工退出浏览器登录 | — |
| `identity_config_updated` | 写入或替换租户的[飞书配置](../architecture/http-api.md#飞书配置) | `app_id`、`session_ttl_seconds`、`refs`（App Secret、Encrypt Key、Verification Token 在保管库里的名字：`app`、`encrypt`、`verification`），没有明文 |
| `user_role_changed` | [改员工的角色](../architecture/http-api.md#员工)（角色真的变了才记） | `before`、`after`：`tenant_admin` 或 `tenant_member` |
| `user_disabled` | 员工被停用：管理员停用，或飞书离职事件（`actor` 是 `system:feishu-event`）。只在状态确实从启用变成停用时记，重复停用不记 | `reason`：`admin`、`feishu_resigned`（离职）、`feishu_frozen`（暂停）、`feishu_exited`（主动退出）；飞书事件另有 `event_id`（飞书的事件标识，重推时不变） |
| `user_enabled` | 管理员重新启用员工（已是启用的不记）。已关闭的会话不恢复 | `previous_reason`：停用时的原因 |
| `user_cleanup_failed` | 员工停用之后的某项收尾失败了（现在唯一的一项是关闭他名下的会话）。再停用他一次会重跑全部收尾，见[运维手册](operations.md#员工停用与离职) | `listener`：哪一项收尾（`close_sessions`）；`error`：异常类型，如 `SessionCleanupError`。异常文本不进审计，在平台日志里 |

员工管理的四种记录（`user_role_changed`、`user_disabled`、`user_enabled`、`user_cleanup_failed`）的 `user_id` 是被操作的员工，`actor` 是谁做的：按 `user_id` 查一名员工，就能看到他何时、因为什么、被谁停用；飞书离职事件签名不符、来源对不上的请求不写审计（只计指标、记日志），防止被刷。

飞书登录的三种记录（`user_created`、`login_succeeded`、`login_failed`）发生在调用者有身份之前：认出了员工的，`actor` 是 `user:<员工标识>`、`user_id` 是他；飞书认出了人、平台里还对不上是谁的（不在可用范围、飞书企业不符）`actor` 是 `anonymous`、`user_id` 为空。授权码、飞书令牌与 App Secret 一概不进审计。

### `gate`：连接器被调用了

模型每调一次连接器工具（stdio MCP、远端 MCP 或 HTTP），记一条 `connector_call`，同步写入：

| `data` 字段 | 含义 |
| --- | --- |
| `connector`、`transport`、`tool` | 哪个连接器的哪个工具，走的什么传输（`stdio`、`remote`、`http`） |
| `effect` | 副作用分类：`read` / `write` / `external` |
| `decision` | `allowed`：放行并已调用。`denied` 留给策略直接拒绝的情况。`rejected`：人工拒绝（送回审批时 `granted` 不为真），调用没有发出——这条由平台在内核确认核销了这次拒绝之后补记，只对连接器工具补（脚本、命令被拒绝没有过闸记录） |
| `is_error`、`detail` | 调用是否出错；出错时的简述（超时是 `timeout`）。调用已经发出、结果回来之前这一轮被强行中止（关闭会话、持有权丢了之后强制收尾、停机时等不到在途对话跑完）的，`detail` 是 `cancelled`、`is_error` 为 `true`：外部系统可能已经执行了，结果平台不知道。人工拒绝的 `detail` 是 `rejected_by_user`，`is_error` 为 `false` |
| `duration_ms` | 耗时；人工拒绝的为 0 |
| `arguments_digest` | 调用参数的 SHA-256 摘要。闸门发起审批时就算好、放进待办的 `detail.arguments_digest`，人工拒绝的补记用的是同一个值 |
| `call_id`、`thread_id`、`submission_id` | 用来和内核事件对上。`submission_id` 是调用归属的那一轮：审批通过后才执行的调用，是送回审批的那一轮（同 `suspension_resolved_by` 的 `submission_id`）；人工拒绝的，是挂起的那一轮；内核自己发起的重跑没有，为空 |

`decision` 为 `allowed` 的记录意味着：**调用确实发出去了**。需要审批的调用在批准或拒绝之前不会有过闸记录。

人工拒绝补记的那条（同一个试用实例里，把同样的改工单操作拒绝了一次；`data` 部分）：

```json
{"connector": "tickets", "transport": "http", "tool": "updateTicket", "effect": "write",
 "decision": "rejected", "is_error": false, "detail": "rejected_by_user", "call_id": "call_0",
 "thread_id": "thr_5c029b6613981225",
 "arguments_digest": "sha256:ab129ec1c7282db457ccb63ffaa5de0ae0929b62aba56e9f0a3fe52d84f7929f",
 "submission_id": "sub_30ced7b66d23", "duration_ms": 0}
```

### `engine`：模型做了什么

taifeng 内核的事件，成批写入（最迟半秒或攒够 64 条）。一轮对话的典型序列：

| `kind` | 含义 | `data` 里有什么 |
| --- | --- | --- |
| `turn_started` | 一轮开始 | 入口 skill、线程标识 |
| `tool_call_started` | 模型发起工具调用 | 工具名、**完整的调用参数** |
| `tool_call_completed` | 工具调用结束 | **工具的输出**、是否出错、耗时 |
| `engine_log` | 内核日志，如脚本执行的开始与结束；停止生成时另有一条（见下文「停止生成在审计里」） | 脚本名、耗时、输出字节数；`level`、`message` |
| `turn_suspended` | 停在审批点 | 待办列表，含被挂起的调用的参数 |
| `suspension_resolved` | 审批决定已送达 | 待办标识 |
| `turn_completed` / `turn_failed` | 一轮结束 | token 用量、往返次数、耗时 |
| `turn_text` | 平台在一轮结束时补的一条 | **这一轮回复的全文** |
| `audit_gap` | 平台发现内核丢了事件 | 期望与实际收到的投递序号 |
| `usage_estimated` | 平台在被停下的一轮结束时补的一条（见下文「用量」） | `submission_id`、`estimated_input_tokens`、`reason`（`cancelled`）；拿不到估算时 `estimated_input_tokens` 为 0，另有 `error` |

内核事件的种类随内核版本增加，上表不是全集。

### 停止生成在审计里

停下一轮对话会留下四条记录，靠被停的那一轮的 `submission_id` 串起来。下面是一次实际停止（只列 `kind` 与 `data` 里的关键字段）：

```json
{"source": "control", "kind": "turn_cancel_requested", "actor": "key:c4c3635d7b6b",
 "data": {"submission_ids": ["sub_b622812d2c61"]}}
{"source": "engine", "kind": "engine_log",
 "data": {"submission_id": "sub_4ad4ee8a83ea", "level": "info", "message": "cancelled turn sub_b622812d2c61", "extra": {}}}
{"source": "engine", "kind": "turn_completed",
 "data": {"submission_id": "sub_b622812d2c61", "end_reason": "cancelled", "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, …}, …}}
{"source": "engine", "kind": "usage_estimated",
 "data": {"submission_id": "sub_b622812d2c61", "estimated_input_tokens": 903, "reason": "cancelled"}}
```

| 记录 | 说明 |
| --- | --- |
| `turn_cancel_requested` | 谁停的（`actor`）、停了哪几轮；`user_id` 与会话里别的记录一样是会话的主人。只在确有在途的对话被提交了取消时记 |
| `engine_log` | 内核受理取消时发的日志。它挂在取消请求**自己的**提交上（平台不对外给出这个标识），被停的那一轮写在 `message` 里 |
| `turn_completed` | 被停的那一轮的结束：`end_reason` 为 `cancelled`、`success` 为 `false`、`cancel_reason` 为 `requested`；还在排队时被停的是 `turn_failed`，`data.kind` 为 `cancelled`。`usage` 不含被打断的那次采样 |
| `usage_estimated` | 平台补的估算，见下文「用量」 |

被停之前已经输出的文本照样在这一轮的 `turn_text` 里。

两类事件不进审计：

- 流式增量（`assistant_text`、`assistant_reasoning`）：量大，内容已经在 `turn_text` 里。
- 发给模型的完整请求（`llm_request_recorded`）：默认不记。部署配置里打开 `audit.capture_requests` 才记，打开后完整的提示词与对话历史都会进审计表。

## 审计里有哪些敏感内容

直接说结论：**审计里有对话正文和工具的输入输出**。把它当成和业务数据同等敏感的东西来保护。

| 内容 | 在不在审计里 | 在哪 |
| --- | --- | --- |
| 用户说的话 | 在 | `message_sent` 的 `text` |
| 模型的回复 | 在 | `turn_text` 的 `text` |
| 工具的调用参数 | 在 | `tool_call_started` 的 `arguments`；需要审批时还在 `turn_suspended` 里 |
| 工具的返回内容 | 在 | `tool_call_completed` 的 `output` |
| 发给模型的完整提示词 | 默认不在 | 打开 `capture_requests` 后在 `llm_request_recorded` |
| 租户密钥的明文 | 平台不放进去 | 明文只在发请求或启动连接器进程的那一刻取出，不经过事件。外部系统把密钥写进返回内容时：远端 MCP 的结果与错误、stdio 连接器的错误文本在交给模型之前按值脱敏；**stdio 连接器成功的结果与 HTTP 连接器的响应不脱敏**，会原样进 `tool_call_completed` |
| API key、根凭据 | 不在 | 平台只存摘要，审计里只有 key 的标识 |

平台在写入前做两件兜底的事：

- **按键名脱敏**：键名里含 `secret`、`password`、`passwd`、`token`、`apikey`、`api_key`、`authorization`、`credential` 这类词的字符串取值，替换成 `[已脱敏]`。数字不脱敏（`input_tokens` 这样的用量字段不受影响）。
- **截断**：超过 8000 个字符的字符串只留开头，并注明原长度。

脱敏只是兜底，有一处它管不到：`tool_call_started` 里的 `arguments` 是一整段 JSON 文本，不是结构化的字段，按键名脱敏对它不起作用。也就是说，**如果模型把敏感值当参数传给工具，它会原样出现在审计里**。业务包的设计上要避免让模型经手凭据——凭据走连接器的密钥引用，不走对话。

连接器的过闸记录（`gate`）是特意只记参数摘要的：想做不含业务数据的调用统计或对账，用它。

## 查询

需要租户管理员的凭据，只能查本租户的：

```bash
curl "https://xuanpu.internal/v1/audit?session_id=ses_…&limit=200" -H "Authorization: Bearer $KEY"
```

| 参数 | 含义 |
| --- | --- |
| `session_id` | 只看某个会话 |
| `kind` | 只看某种事件 |
| `source` | `control` / `gate` / `engine` |
| `since`、`until` | 时间范围，Unix 秒；`since` 含，`until` 不含 |
| `user_id` | 只看归到某名员工名下的：他名下会话里的一切记录（不论是谁触发的）、针对他的员工管理记录（改角色、停用、启用），以及他做的与会话无关的控制面操作 |
| `actor` | 只看某个触发者的，精确匹配：`key:<API key 标识>`、`user:<员工标识>`、`key:<API key 标识>/user:<员工标识>`（代员工调用，与那把 key 自己的是两个值）、`engine`、`system:<名字>`。低频的排查查询，审计表上没有为它建索引，记录多时会慢 |
| `after_id` | 只看编号大于它的，翻页用 |
| `limit` | 每页条数，默认 100，最大 1000 |

条件之间是「并且」。响应里的 `next_after_id` 不为空就还有下一页，把它作为 `after_id` 再查。

Python 客户端会自动翻页：

```python
async for record in client.admin.audit(session_id=session_id, source="gate"):
    print(record.kind, record.data["tool"], record.data["decision"], record.data["duration_ms"])
# connector_call listTickets allowed 1
# connector_call updateTicket allowed 0
# connector_call updateTicket rejected 0
```

（一个会话里查了一次工单、批准了一次改工单、拒绝了一次改工单。）

控制台的「审计」页签能按会话、事件类型、来源筛选。

### 顺序

记录按 `audit_id` 返回，那是**写入顺序，不完全是发生顺序**：`control` 与 `gate` 的记录是同步写的，`engine` 的记录是成批写的，所以同一轮对话里，一条过闸记录的编号可能排在比它更早发生的内核事件前面。

- 要还原时间线，按 `at` 排序。
- 内核事件之间的先后，以同一个 `stream` 内的 `seq` 为准。

### 常见问题怎么查

| 想知道 | 怎么查 |
| --- | --- |
| 某个会话从头到尾发生了什么 | `session_id=…`，按 `at` 排序 |
| 用户问了什么、模型答了什么 | `kind=message_sent` 与 `kind=turn_text`，用 `data.submission_id` 配对 |
| 调过哪些外部接口 | `source=gate` 里 `decision=allowed` 的（`rejected` 的没有调用） |
| 某次写操作是谁批准的 | `kind=suspension_resolved_by`：`actor` 是送回决定的调用者（API key、员工或代员工调用），`data.decisions` 是每个待办的结论。再用 `kind=turn_suspended` 里同一个待办标识找到当时要批的操作与参数 |
| 被拒绝的操作 | 同上，`decisions` 里取值不为 `true` 的。其中连接器工具另有一条 `decision=rejected` 的 `connector_call`（调用没有执行） |
| 谁在什么时候发布、切换了业务包 | `kind=package_published`、`kind=package_activated` |
| 谁改过密钥 | `kind=secret_written`、`kind=secret_deleted` |
| 某一轮用了多少 token | `kind=turn_completed` 的 `data.subtree_usage`（含子 skill）；没有这个字段时看 `data.usage` |
| 某名员工的会话里发生了什么、他做了什么 | `user_id=usr_…`；员工查看自己的记录以后再开放，现在只有租户管理员能查 |
| 某个管理员或某把 key 在别人的会话里做了什么 | `actor=user:usr_…`（浏览器登录的管理员）或 `actor=key:…`，再加 `user_id=` 只看某名员工会话里的 |
| 某名员工是什么时候、因为什么、被谁停用的 | `kind=user_disabled&user_id=usr_…`：`data.reason` 是原因，`actor` 是谁（`system:feishu-event` 是飞书离职事件）；停用后哪些会话被关了看 `actor=system:user-disabled&user_id=usr_…`，收尾失败看 `kind=user_cleanup_failed` |
| 谁改过员工的角色 | `kind=user_role_changed`，`data.before`、`data.after` |

一轮对话的所有记录靠 `data.submission_id` 串起来。送回审批决定后的续跑是一个新的 `submission_id`；要把「挂起的那一轮」和「续跑的那一轮」对上，用待办标识或 `call_id`。

## 完整性

| 情况 | 平台怎么处理 |
| --- | --- |
| 同一条内核事件被写了两次 | 按 `(stream, seq)` 去重，只留一条 |
| 内核因队列满丢了事件 | 记一条 `audit_gap`，审计不完整不会悄悄发生 |
| 内核事件写库失败 | 记错误日志，留在内存里等下一次重试 |
| 控制面操作或过闸记录写库失败 | 该操作本身报错。例外是人工拒绝的补记：它在内核确认核销之后才写，那时送回已经交给内核，写不进去只记错误日志并计入指标 `xuanpu_gate_backfill_failures_total`，送回照常续跑 |
| `user_disabled` 写库失败 | 这次停用报错，但员工已经停用、登录会话已经删掉（审计排在这两步之后）。重试时状态已是停用，**这条记录不会补写**：平台日志里有错误、`xuanpu_audit_write_failures_total` 计一次，按日志补查。停用之后的收尾照样可以再停用一次重跑 |
| 实例崩溃 | 还没写出去的那一批内核事件（最多半秒或 64 条）会丢。`control` 与 `gate` 的记录是同步写的，不受影响 |

## 导出

平台没有专门的导出接口，用查询接口增量拉取：记住上次拉到的最大 `audit_id`，下次从它开始。示意：

```python
cursor = load_cursor()                       # 上次的位置，首次为 0
while True:
    page = await http.get("/v1/audit", params={"after_id": cursor, "limit": 1000})
    records = page.json()["records"]
    if not records:
        break
    ship_to_siem(records)
    cursor = records[-1]["audit_id"]
    save_cursor(cursor)
```

平台管理员的根凭据不属于任何租户，不能调查询接口。要跨租户汇总，直接读库：

| 表 | 内容 |
| --- | --- |
| `<schema>.xp_audit` | 审计。列与上面的字段一一对应，另有 `tenant_id`；`data` 是 JSONB |
| `<schema>.xp_usage` | 用量，主键 `(tenant_id, day)` |
| `<schema>.xp_user_usage` | 按员工的用量，主键 `(tenant_id, day, user_id)` |

审计表没有任何更新路径。建议数据库层面也不给平台的连接账号 `UPDATE` / `DELETE` 这张表的权限。

`state.backend` 为 `postgres` 时，会话主存（内核续接会话用的对话消息）也在这个库里：每个租户一组表，表名前缀是租户标识的摘要，结构见 taifeng-stores 的文档。它是内核的工作数据，不是审计，没有 `actor`、来源这些字段；查谁在什么时候说了什么，以审计为准。

### 留存

审计表只增不减，平台不做清理、归档或分区。按你们的留存要求在数据库层面处理（按时间分区、定期转存冷存储）。

## 用量

```bash
curl "https://xuanpu.internal/v1/usage?since=2026-09-01&until=2026-09-30" -H "Authorization: Bearer $KEY"
```

```json
{"usage": [{"day": "2026-09-30", "input_tokens": 2940, "output_tokens": 252, "total_tokens": 3192, "turns": 5}]}
```

| 规则 | 说明 |
| --- | --- |
| 粒度 | 每个租户每天一条。`since`、`until` 是日期，两端都含；没有用量的日子不出现 |
| 「天」 | 按 UTC 划分 |
| 什么时候入账 | 一轮对话结束时（完成、失败、挂起、被停下都算）。数字来自模型网关返回的用量。写库失败时这一轮照常结束，用量不再补记：记错误日志，指标 `xuanpu_usage_write_failures_total` 计一次（见[运维手册](operations.md)） |
| 被停下的一轮 | 被打断的那次采样没有收尾，网关报不出它的用量，但照样按 prompt 收费。平台按内核的上下文估算把它计入 `input_tokens`（同样计入 `max_daily_tokens`），并记一条 `usage_estimated`；拿不到估算时按 0 计并记警告 |
| `turns` | 对话轮数。一轮被审批打断的对话算两轮：挂起前一轮，续跑一轮 |
| 子 skill | 算在内 |
| 评测 | 算在内。评测走的是真实路径 |

`max_daily_tokens` 配额对照的就是当天的 `total_tokens`。因为用量在一轮结束时才入账，配额拦的是「下一轮」：当天的实际用量可能超出上限一轮对话的量。

### 按员工

```bash
curl "https://xuanpu.internal/v1/usage/users?since=2026-10-01&until=2026-10-31" -H "Authorization: Bearer $KEY"
```

```json
{"usage": [{"day": "2026-10-02", "user_id": "usr_364b889313336be0226bae9e", "input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "turns": 1}]}
```

（员工 `usr_364b…` 浏览器登录后在自己的会话里说了一句话。）

| 规则 | 说明 |
| --- | --- |
| 粒度 | 每天每名员工一条，按日期、员工排序；`since`、`until` 与上面相同，`user_id` 可选，只看这名员工的 |
| 算谁的 | 会话的主人（建会话时调用者代表的员工），不论这一轮是谁发起的。API key 直接建的会话没有主人，只算进租户总量，所以各员工之和不大于租户总量 |
| 什么时候入账 | 与租户总量同一时刻、同样的数（含被停下的一轮按估算计入的输入）。先记租户总量，记成了才记按员工的：租户那笔写库失败时员工那笔也不记；员工那笔失败时租户总量照记。失败的那一笔不再补记，同样记错误日志、`xuanpu_usage_write_failures_total` 计一次 |
| 谁能查 | 租户管理员 |

`max_daily_tokens` 只对照租户总量，按员工的用量不设配额。

### 其他维度

平台只按租户、按员工按天汇总。要按会话、按业务包、按入口拆分，从审计里算：取一轮结束的记录（`turn_completed`、`turn_failed`、`turn_suspended`），`data.subtree_usage`（没有时用 `data.usage`）是这一轮的用量，`session_id` 对到会话，再由会话的 `session_created` 记录对到业务包、版本与 `metadata`。


### 通讯录对账审计

Python SDK 现在提供 `client.admin.audit(user_id=..., actor=...)`（条件在翻页时保留）与 `client.admin.user_usage(since, until, user_id=...)`。`AuditRecord.user_id` 与 `UserUsage.user_id` 保留归属；旧版服务端的审计响应没有 `user_id` 时 SDK 解析为 `None`，不伪造归属。成员查询自身审计仍留待 P6，P1 这两个接口都需要租户管理员。

对账以 `system:reconcile` 为 actor。`identity_unverified` 仅首次设置 `unverified_since` 时记录，关联被标记的 `user_id`；再次查不到不重复记录，再查到清除标记。

每次已认领的对账记录 `identity_reconciled`：`result` 为 `succeeded` 或 `failed`，带 `checked`、`disabled`、`unverified` 计数；失败额外带异常类型 `error`（取消为 `CancelledError`），不含飞书响应或凭据。失败计数仅表示已处理部分，不代表整租户完成。停用仍有既有 `user_disabled` 审计。存储不可达时审计也可能失败，结合平台日志及 `xuanpu_reconcile_runs_total` 排查。

成功先持久化对账完成时间，再写成功审计；若完成存储失败或失租，仅记录失败终态。完成后的审计写入失败不会撤销完成时间或补写失败终态，此时对账已生效、成功指标已计数，但成功审计可能缺失，接口可能报错；结合日志与完成状态判断，勿将接口错误直接视为扫描失败。
