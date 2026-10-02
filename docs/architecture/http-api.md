# HTTP 接口（活文档）

> 第一版接口，加上 P0 新增的停止生成、修改配额与指标端点，以及 P1 的调用者身份（浏览器登录 cookie、系统代员工调用、`GET /v1/me`）、飞书登录与租户的飞书配置、会话的归属与[可见范围](#可见范围)、审计与用量带上员工（`GET /v1/audit?user_id=`、`GET /v1/usage/users`）、[员工管理](#员工)与[飞书事件回调](#飞书事件回调)（离职停用）。实现在 `packages/xuanpu-server/src/xuanpu_server/api/`。
> 用法与注意事项见[应用接入](../guides/integrating.md)；本文是逐个接口的参考。

## 约定

- 请求体与响应体都是 JSON（发布业务包的请求体除外）。请求体里多余的字段一律拒绝。
- 时间都是 Unix 秒（浮点数）；用量的日期是 `YYYY-MM-DD`（UTC）。
- 框架自动生成的接口描述（`/openapi.json`）与交互式文档页面（`/docs`、`/redoc`）都是关闭的（`404`）：它们不需要凭据，而且不含事件流的帧格式。接口以本文为准。

## 认证

平台按下面的顺序认出调用者：

1. 请求带 `Authorization: Bearer <凭据>`：按凭据认证（下表前两种）。另带 `X-Xuanpu-Act-As: <员工标识>` 的，是[系统代员工调用](#系统代员工调用)。
2. 没有 `Authorization`、带登录 cookie：[浏览器登录](#浏览器登录)的员工。带了 `Authorization` 就只按它认证，它无效时也不再看 cookie。**没有配置 `public_url` 时平台不认登录 cookie**，按没有凭据处理。
3. 两样都没有：`401`。

| 凭据 | 来源 | 身份 |
| --- | --- | --- |
| 根凭据 `xproot_…` | `xuanpu keygen root` 生成，摘要写进配置 | 平台管理员，不属于任何租户 |
| API key `xp_<标识>_<密钥>` | 签发时返回一次，平台只存摘要 | 租户管理员（`tenant_admin`）或租户成员（`tenant_member`） |
| 登录 cookie `__Host-xuanpu_session`（本地 HTTP 开发时是 `xuanpu_session`） | 员工经[飞书登录](#飞书登录)后平台写入浏览器，平台只存令牌的摘要 | 员工，角色是他在平台里的角色（`tenant_admin` 或 `tenant_member`） |

下文「需要的身份」一栏里，「租户成员」指本租户的任何调用者：任何一把 API key、浏览器登录的员工、代员工调用（管理员也算）；「租户管理员」指生效角色为 `tenant_admin` 的调用者。平台管理员不属于任何租户，不能调用租户内的接口。

各种认证失败（格式不对、不存在、已吊销、租户被停用；登录会话不存在或过期、员工被停用）对外是同一个 `401`（`{"error": {"code": "unauthenticated", "message": "凭据无效"}}`），不透露凭据、员工或租户是否存在。

调用者在审计记录里的 `actor`：

| 调用者 | `actor` | 审计的 `user_id`（与会话无关的操作） |
| --- | --- | --- |
| 根凭据 | `key:root` | 空 |
| API key | `key:<key_id>` | 空 |
| 浏览器登录 | `user:<员工标识>` | 这名员工 |
| 系统代员工 | `key:<key_id>/user:<员工标识>` | 这名员工 |
| 平台自己（不经接口，如[飞书离职事件](#飞书事件回调)停用员工、停用后关会话） | `system:<名字>`：`system:feishu-event`、`system:user-disabled` | 空 |

`user_id` 一栏只说与会话无关的控制面操作；针对某名员工的操作（改角色、停用、启用）`user_id` 是被操作的员工，见[员工](#员工)。会话里的一切记录——内核事件、过闸记录，以及建会话、发消息、送审批结果、停止生成、关闭会话这些会话上的操作——不论是谁触发的，`user_id` 都是会话的主人，`actor` 仍是触发者，见[审计](#审计)。

### 浏览器登录

- cookie 的值是 32 字节随机数；`HttpOnly`、`SameSite=Lax`、`Path=/`，不带 `Domain`。平台只存它的 SHA-256 摘要。名字随配置 `identity.cookie_secure`：

  | `identity.cookie_secure` | cookie 名 | `Secure` |
  | --- | --- | --- |
  | `true`（默认） | `__Host-xuanpu_session` | 带。`__Host-` 前缀要求 `Secure`、`Path=/`、不带 `Domain`，浏览器因此拒收兄弟子域或父域写来的同名 cookie |
  | `false`（本地用 HTTP 开发） | `xuanpu_session` | 不带 |

  平台只认当前配置的那个名字。
- **没有配置 `public_url` 时不认登录 cookie**：带 cookie 的请求按没有凭据处理（`401`，不清 cookie）。飞书登录要求 `public_url`，没有它就不会签发 cookie；这样认 cookie 时，下面防跨站请求伪造的两条检查总是都生效。本地开发配 `public_url: http://127.0.0.1:8420` 与 `identity.cookie_secure: false`。
- **同名 cookie 不止一个**（可能是别的子域往父域塞了一个）：`401`，**不清 cookie**——清掉会让塞 cookie 的人能持续把员工踢下线；也不挑一个用，免得会话被调包。
- **每个请求都查库**：登录会话存在且没过期、员工为启用、租户为启用，三者任一不满足就是 `401`，响应同时清掉 cookie（`Set-Cookie: __Host-xuanpu_session=""; …; Max-Age=0`，名字与属性和写入时相同）。不做缓存，员工或租户一停用，下一个请求就被拒。
- 登录会话有效期从登录时刻算起，不随使用延长。

**先检查请求的形状，再验会话**。下面几项只看请求本身、不查库，在验会话之前做：形状不对的请求（包括伪造的跨站请求）不必走到查库那一步。所以带着失效 cookie、又缺 `X-Requested-By` 的写请求，得到的是 `403 csrf_rejected` 而不是 `401`——这是有意的。

| 要求 | 不满足时 |
| --- | --- |
| 不带 `X-Xuanpu-Act-As`：代员工调用只给 API key | `400 invalid_request` |
| 写请求（`GET`、`HEAD`、`OPTIONS` 以外的方法）带 `X-Requested-By: xuanpu` 头 | `403 csrf_rejected` |
| 写请求带了 `Origin` 头的，它必须等于配置 `public_url` 的源（如 `https://xuanpu.example.com`） | `403 csrf_rejected` |

后两条是防跨站请求伪造。平台不开放跨域（不发任何 `Access-Control-Allow-*` 头），跨站页面的脚本发不出带自定义头的请求，表单与页面跳转又带不了自定义头，所以自定义头本身就挡住了跨站请求伪造，`Origin` 是第二道防线。用 API key 的请求不受这些限制（浏览器不会自动带上 `Authorization`）。

### 系统代员工调用

后台系统（如工作流）以某名员工的身份调平台，请求带 API key 与 `X-Xuanpu-Act-As: <员工标识>`：

```
GET /v1/me
Authorization: Bearer xp_7a1c03d2e9f4_…
X-Xuanpu-Act-As: usr_5f0c1b2e7d9a4c3b8e6f1a20
```

- 只有签发时带 `"act_as_users": true` 的 API key 能这么用（见 [API key](#api-key)）。
- **生效角色取 key 的角色与员工角色中较低的那个**：管理员的 key 代普通成员调用，只有成员的权限；成员的 key 代管理员调用，也只有成员的权限。
- 审计里 `actor` 同时写出 key 和员工；与会话无关的操作 `user_id` 是这名员工，会话里的记录 `user_id` 是会话的主人（见[审计](#审计)）。

| 情况 | 响应 |
| --- | --- |
| key 无效 | `401 unauthenticated`（先认 key，再看代调用） |
| key 签发时没有授予这项能力 | `403 permission_denied` |
| 员工不存在、属于别的租户、已停用（同一句提示） | `403 permission_denied` |
| 用根凭据代员工调用 | `403 permission_denied` |

## 错误

```json
{"error": {"code": "quota_exceeded", "message": "今日 token 用量已达上限 1000000"}}
```

| 状态码 | `code` | 含义 | 附带字段 |
| --- | --- | --- | --- |
| 400 | `invalid_request` | 请求不合法（包括用登录 cookie 时带 `X-Xuanpu-Act-As`） | 字段校验不过时有 `problems` |
| 400 | `package_invalid` | 业务包不符合规范 | `problems`：逐条的问题 |
| 400 | `secret_missing` | 业务包或[飞书配置](#飞书配置)引用的密钥尚未配置 | |
| 401 | `unauthenticated` | 没有凭据或凭据无效 | |
| 403 | `permission_denied` | 凭据有效但无权操作（包括不允许的[代员工调用](#系统代员工调用)） | |
| 403 | `csrf_rejected` | 用登录 cookie 的写请求缺 `X-Requested-By: xuanpu` 头，或 `Origin` 与 `public_url` 不符（见[浏览器登录](#浏览器登录)） | |
| 403 | `session_owner_disabled` | 会话的主人（员工）已停用：任何调用者都不能再往这个会话发消息、送审批结果（见[主人停用之后](#主人停用之后)）；换一把凭据也没用 | |
| 404 | `not_found` | 资源不存在，或属于别的租户；普通成员访问别人的会话也是它（见[可见范围](#可见范围)） | |
| 409 | `conflict` | 与现有状态冲突（版本已发布、会话已关闭、要降级或停用的是租户里最后一名启用的管理员等） | |
| 409 | `session_busy` | 会话正由另一个实例持有 | `owner`：持有者的实例标识 |
| 429 | `quota_exceeded` | 超出租户配额 | |
| 500 | `secret_corrupted` | 密钥的密文解不开（加密它的主密钥已不在配置里，或密文被改动） | |
| 500 | `internal_error` | 未预料的错误；细节只进服务端日志 | |
| 503 | `runtime_unavailable` | 沙盒、连接器或对象存储不可用；实例正在停机；会话的持有在准备期间中断过（`message` 里说「请重试」，直接重试即可） | |

`code` 是稳定的，`message` 的文案可能调整。

## 接口一览

| 方法与路径 | 需要的身份 | 说明 |
| --- | --- | --- |
| `GET /healthz` | 无 | 存活探测 |
| `GET /v1/me` | 任意 | 当前调用者 |
| `GET /v1/auth/feishu/login?tenant=&return_to=` | 无 | 开始飞书登录，302 到飞书授权页 |
| `GET /v1/auth/feishu/callback` | 无 | 飞书授权页跳回，完成登录，302 到 `return_to` 或落地页 |
| `POST /v1/auth/logout` | 浏览器登录 | 退出：删掉当前登录会话，清 cookie |
| `GET /v1/identity/feishu` · `PUT /v1/identity/feishu` | 租户管理员 | 本租户的飞书配置 |
| `POST /v1/identity/feishu/events/{tenant_id}` | 飞书的签名 | 飞书事件回调：URL 验证、员工离职 |
| `GET /v1/users?status=&role=&unverified=&after=&limit=` | 租户管理员 | 列出员工 |
| `GET /v1/users/{user_id}` | 租户管理员或本人 | 取员工 |
| `PUT /v1/users/{user_id}/role` · `PUT /v1/users/{user_id}/status` | 租户管理员 | 改员工的角色；停用或启用员工 |
| `POST /v1/admin/tenants` | 平台管理员 | 新建租户 |
| `GET /v1/admin/tenants` | 平台管理员 | 列出租户 |
| `PUT /v1/admin/tenants/{id}/status` | 平台管理员 | 启用或停用租户 |
| `PUT /v1/admin/tenants/{id}/quota` | 平台管理员 | 整体替换租户的配额 |
| `POST /v1/admin/tenants/{id}/keys` | 平台管理员 | 给租户签发 API key |
| `POST /v1/keys` · `GET /v1/keys` · `DELETE /v1/keys/{id}` | 租户管理员 | 本租户的 API key |
| `PUT /v1/secrets/{name}` · `GET /v1/secrets` · `DELETE /v1/secrets/{name}` | 租户管理员 | 密钥。取值写入后不可再读出 |
| `POST /v1/packages?activate=` | 租户管理员 | 发布业务包，请求体是 zip 归档 |
| `GET /v1/packages?name=` | 租户成员 | 列出已发布版本 |
| `POST /v1/packages/{name}/activate` | 租户管理员 | 切换生效版本（前进或回滚） |
| `POST /v1/packages/{name}/evals` | 租户管理员 | 运行评测用例，返回报告 |
| `POST /v1/sessions` · `GET /v1/sessions?status=&mine=&limit=` | 租户成员 | 新建、列出会话（只列[看得见的](#可见范围)） |
| `GET /v1/sessions/{id}` · `DELETE /v1/sessions/{id}` | 租户成员 | 取会话、关闭会话 |
| `GET /v1/sessions/{id}/pending` | 租户成员 | 会话当前等待处理的审批请求 |
| `GET /v1/sessions/{id}/events?submission_id=&after_seq=` | 租户成员 | 回放一轮对话已记入审计的事件 |
| `POST /v1/sessions/{id}/messages` | 租户成员 | 发消息；响应是事件流 |
| `POST /v1/sessions/{id}/resume` | 租户成员 | 送回审批结果；响应是事件流 |
| `POST /v1/sessions/{id}/cancel` | 租户成员 | 停止生成：取消会话里在途的对话 |
| `GET /v1/audit?session_id=&kind=&source=&since=&until=&user_id=&actor=&after_id=&limit=` | 租户管理员 | 查询审计 |
| `GET /v1/usage?since=&until=` | 租户管理员 | 查询用量 |
| `GET /v1/usage/users?since=&until=&user_id=` | 租户管理员 | 按天、按员工查询用量 |
| `GET /metrics` | 抓取令牌 | Prometheus 指标；配置了 `metrics.token_hash` 才开 |

## 存活探测

```
GET /healthz
→ {"status": "ok", "version": "2026.9.30.1", "instance": "inst_9c7a5cea5506bdae0d89e8da"}
```

## 当前调用者

```
GET /v1/me

→ {"actor": "user:usr_5f0c1b2e7d9a4c3b8e6f1a20", "role": "tenant_member", "tenant_id": "acme",
   "key_id": null, "user_id": "usr_5f0c1b2e7d9a4c3b8e6f1a20", "display_name": "张三"}
```

任何调用者都能调，四种调用者同一个形状，用不上的字段为 `null`：

| 调用者 | `role` | `tenant_id` | `key_id` | `user_id`、`display_name` |
| --- | --- | --- | --- | --- |
| 浏览器登录 | 员工的角色 | 员工的租户 | `null` | 这名员工 |
| API key | key 的角色 | key 的租户 | key 的标识 | `null` |
| 系统代员工 | 生效角色（key 与员工中较低的） | key 的租户 | key 的标识 | 被代的员工 |
| 根凭据 | `platform_admin` | `null` | `root` | `null` |

`actor` 与审计记录里的写法相同（见[认证](#认证)）。

## 飞书登录

员工用飞书账号登录平台。前提：平台配置了 `public_url`，租户[配置了飞书](#飞书配置)。下面两个接口不需要凭据、都回 `302`，是给浏览器跳转用的（控制台的「用飞书登录」按钮就是一个指向第一个接口的链接），不是给程序调的。

```
GET /v1/auth/feishu/login?tenant=acme&return_to=/console/

→ 302
  Location: https://accounts.feishu.cn/open-apis/authen/v1/authorize?client_id=cli_a1b2c3d4e5f6
            &response_type=code&redirect_uri=https%3A%2F%2Fxuanpu.example.com%2Fv1%2Fauth%2Ffeishu%2Fcallback
            &state=…&code_challenge=…&code_challenge_method=S256
  Cache-Control: no-store
  Set-Cookie: __Host-xuanpu_login=…; HttpOnly; Max-Age=600; Path=/; SameSite=lax; Secure
```

| 参数 | 说明 |
| --- | --- |
| `tenant` | 登录哪个租户；省略时用配置 `identity.default_tenant` |
| `return_to` | 登录完成后跳去的站内路径：以 `/` 开头，不以 `//` 或 `/\` 开头，不含反斜杠、空白与控制字符，最长 512。不合要求的（包括 `https://…` 这类别的站点）一律改用 `identity.login_landing_path`，防止被利用做开放跳转 |

平台生成 `state`（32 字节随机数）与 PKCE 校验串（S256），写一条登录尝试（10 分钟有效，取出即删，多个实例都认），不申请额外的飞书权限（不带 `scope`）。同时写**登录尝试 cookie** `__Host-xuanpu_login`（本地 HTTP 开发时是 `xuanpu_login`，属性与登录 cookie 相同，10 分钟有效）：值是另一个随机数，登录尝试里只存它的摘要。回调时浏览器必须带着它，否则别人可以把自己那次登录的回调链接发给员工，员工一点就以别人的身份登录了（登录伪造）。

```
GET /v1/auth/feishu/callback?code=…&state=…        # 员工在授权页点了拒绝时是 ?error=access_denied&state=…
Cookie: __Host-xuanpu_login=…

→ 302
  Location: /console/
  Cache-Control: no-store
  Set-Cookie: __Host-xuanpu_login=""; expires=…; HttpOnly; Max-Age=0; Path=/; SameSite=lax; Secure
  Set-Cookie: __Host-xuanpu_session=…; HttpOnly; Max-Age=604800; Path=/; SameSite=lax; Secure
```

回调依次：

1. 按 `state` 取出并删除登录尝试（`state` 不是平台发出的那种 43 个字符，直接拒绝、不查库）；核对浏览器带来的登录尝试 cookie（缺失、不符、同名不止一个都不行，这时不拿授权码去换令牌）。租户一律取自登录尝试，不看请求参数；
2. 用授权码与校验串换飞书令牌，再取员工信息。授权码超过 512 个字符或不全是可见 ASCII 的不拿去换；每个租户同时在调飞书的登录最多 8 个，再来的最多排队 5 秒；
3. 核对飞书企业：首次成功登录时把飞书的 `tenant_key` 绑定到租户，以后必须一致；
4. 按飞书的 `union_id` 找员工，找不到就新建普通成员（`tenant_member`）——谁在飞书自建应用的可用范围里，谁就能登录；
5. 员工在平台里已停用的，拒绝；
6. 刷新姓名、头像与 `open_id`，记下登录时间；
7. 建登录会话，写登录 cookie（有效期取租户飞书配置的 `session_ttl_seconds`，没配则取 `identity.session_ttl_seconds`），跳去 `return_to`。浏览器还带着（恰好一个）旧的登录 cookie 的，它对应的登录会话随之作废——新 cookie 覆盖了旧的，不作废它就只能等过期。

每次登录都签发新令牌，不复用旧的；登录拿到的飞书令牌用完即丢，不保存。回调的响应总会清掉登录尝试 cookie。同一个浏览器同时开始两次登录（两个标签页）时，只有后开始的那次能完成。

失败一律 `302` 到落地页 `identity.login_landing_path`，带 `?login_error=<原因>`（如 `/console/?login_error=state_invalid`），不写登录 cookie：

| `login_error` | 含义 |
| --- | --- |
| `feishu_denied` | 员工在飞书授权页点了拒绝 |
| `not_in_app_scope` | 员工不在飞书自建应用的可用范围内（飞书错误码 20010） |
| `state_invalid` | 登录链接过期（10 分钟）、被重复使用或 `state` 形状不对；或浏览器没带对登录尝试 cookie——别人发来的回调链接、开始登录与回调不在同一个浏览器，或开始登录时打开的不是 `public_url` 上的地址（cookie 只写给那次打开的主机） |
| `tenant_mismatch` | 员工所在的飞书企业与租户绑定的不一致 |
| `tenant_unavailable` | 租户不存在、已停用、没配置飞书（或 App Secret 已从保管库删掉），或平台没配置 `public_url`；省略 `tenant` 又没配 `identity.default_tenant` 也是它 |
| `user_disabled` | 这名员工在平台里已停用 |
| `identity_unavailable` | 调飞书接口失败：网络错误、超时、飞书服务端错误，或飞书拒绝了换令牌（授权码无效或过期、App Secret 不对等）；本租户同时在调飞书的登录太多、排队超时；回调既没有 `code` 也不是员工拒绝，或 `code` 形状不对（这时不调飞书）也算 |

- 审计：成功记 `login_succeeded`，首次登录另记 `user_created`。失败只在飞书确实认出了一个人之后才记 `login_failed`（`data.reason` 是原因）：`not_in_app_scope`、`tenant_mismatch`、`user_disabled`——它们要一次真实的飞书登录，匿名请求伪造不出来。开始登录阶段的失败，以及 `feishu_denied`、`state_invalid`、`identity_unavailable`，只计指标、记平台日志，不写审计：这两个接口不需要凭据，否则谁都能往租户的审计里灌记录（与事件回调签名不符时不写审计同一口径）。授权码、飞书令牌、App Secret 不进审计，也不进平台日志。见[审计与用量](../guides/audit-and-usage.md)。
- 指标 `xuanpu_logins_total{result}` 按结果计数（`succeeded` 或上表的原因）。
- 授权码在回调地址的查询串里。`xuanpu serve` 的访问日志把回调这一行的查询串抹成 `?…`；反向代理的访问日志要部署方自己处理（见[运维手册](../guides/operations.md#public_url-与-identity)）。即使漏出去，它 5 分钟内有效、只能用一次，换令牌还要平台自己保管的 PKCE 校验串与 App Secret，单凭它换不到令牌。
- 这两个接口不需要凭据，部署时在反向代理上按来源地址限速。

### 退出登录

```
POST /v1/auth/logout
Cookie: __Host-xuanpu_session=…
X-Requested-By: xuanpu

→ {"logged_out": true}
  Set-Cookie: __Host-xuanpu_session=""; expires=…; HttpOnly; Max-Age=0; Path=/; SameSite=lax; Secure
```

删掉这个 cookie 对应的登录会话（同一员工在别处的登录不受影响），清 cookie，记 `logout`。它是写请求，要过[浏览器登录](#浏览器登录)的防跨站请求伪造检查。用 API key 调是 `400 invalid_request`（API key 不需要退出）；会话已失效是 `401`。

## 飞书配置

租户管理员配置本租户的飞书自建应用。三个密钥先经 `PUT /v1/secrets/{name}` 写进保管库，这里只引用名字：

```
PUT /v1/identity/feishu
{"app_id": "cli_a1b2c3d4e5f6", "app_secret_ref": "feishu-app-secret",
 "encrypt_key_ref": "feishu-encrypt-key", "verification_token_ref": "feishu-verification-token",
 "session_ttl_seconds": 28800}

→ {"configured": true, "app_id": "cli_a1b2c3d4e5f6", "app_secret_ref": "feishu-app-secret",
   "encrypt_key_ref": "feishu-encrypt-key", "verification_token_ref": "feishu-verification-token",
   "session_ttl_seconds": 28800, "feishu_tenant_key": null,
   "callback_url": "https://xuanpu.example.com/v1/auth/feishu/callback",
   "event_url": "https://xuanpu.example.com/v1/identity/feishu/events/acme", "hint": null}
```

| 字段 | 说明 |
| --- | --- |
| `app_id` | 飞书自建应用的 App ID，1–128 个字符 |
| `app_secret_ref`、`encrypt_key_ref`、`verification_token_ref` | App Secret、Encrypt Key、Verification Token 在保管库里的名字（命名规则同[密钥](#密钥)）。三个都必填，而且必须已经写入 |
| `session_ttl_seconds` | 本租户登录会话的有效期（秒），60 到 2592000（30 天）；省略或为 `null` 时用平台默认 `identity.session_ttl_seconds` |

这是**整体替换**：省略的 `session_ttl_seconds` 恢复为平台默认。已绑定的飞书企业（`feishu_tenant_key`，首次成功登录时绑定）保留不变，配置接口改不了它。

`GET /v1/identity/feishu` 返回同样的形状。还没配置时 `configured` 为 `false`、各项为 `null`，`callback_url` 与 `event_url` 照样给出——先把它们填进飞书后台：`callback_url` 是重定向地址，`event_url` 是事件订阅的请求地址（见[飞书事件回调](#飞书事件回调)）。平台没配置 `public_url` 时两个地址为 `null`，`hint` 说明原因。响应里只有密钥的名字，没有明文。

| 情况 | 响应 |
| --- | --- |
| 不是租户管理员（包括浏览器登录的普通成员） | `403 permission_denied` |
| 平台没配置 `public_url`：启用飞书登录必须有它（`PUT`） | `400 invalid_request` |
| 引用的密钥还不在保管库里（`PUT`） | `400 secret_missing`，`message` 里列出缺的名字 |
| 密钥名不合法、`session_ttl_seconds` 不在 60 到 2592000 之间、缺字段或有多余的字段（`PUT`） | `400 invalid_request` |

审计记 `identity_config_updated`：`data` 是 `app_id`、`session_ttl_seconds` 与 `refs`（三个密钥名：`app`、`encrypt`、`verification`），没有明文。

## 飞书事件回调

飞书把订阅的事件推到 `GET /v1/identity/feishu` 返回的 `event_url`，也就是这个接口。它**不走平台的认证**（不带也不认 API key、登录 cookie），靠飞书的签名认来源；平台用它在员工离职时停用他。

```
POST /v1/identity/feishu/events/acme
X-Lark-Request-Timestamp: 1790900000
X-Lark-Request-Nonce: 8b1e2f…
X-Lark-Signature: 3f9a0c…（64 位十六进制）
{"encrypt": "FcR0+…"}

→ {"msg": "success"}
```

- **必须加密**：平台要求配置 Encrypt Key（飞书配置的三个密钥都必填），请求体是 `{"encrypt": "<base64>"}`：AES-256-CBC，密钥是 Encrypt Key 的 SHA-256，IV 是密文的前 16 字节，PKCS7 填充。没加密的请求一律不认。
- **签名**：`X-Lark-Signature` 等于 `sha256(X-Lark-Request-Timestamp + X-Lark-Request-Nonce + Encrypt Key + 原始请求体)` 的十六进制，常数时间比较。
- **URL 验证**：在飞书后台填请求地址时，飞书发一个加密的 URL 验证请求（解密后是 `{"challenge", "token", "type": "url_verification"}`），**不带签名头**。平台解开、核对 `token` 等于 Verification Token，原样回 `{"challenge": "…"}`（飞书要求 1 秒内）。所以先在飞书后台配好 Encrypt Key，再填请求地址。
- **事件**：其余请求都必须带对签名；解开之后再核对 `header.token` 等于 Verification Token、`header.app_id` 等于配置的 App ID、`header.tenant_key` 等于租户绑定的飞书企业。飞书要求 3 秒内回 `200`，否则按 15 秒、5 分钟、1 小时、6 小时重推，最多 4 次。

| 情况 | 响应 |
| --- | --- |
| 请求体超过 1 MiB | `400 invalid_request`（不读完、不验签） |
| 租户不存在、标识不合法、没有配置飞书，或配置引用的 Encrypt Key、Verification Token 不在保管库里 | `404 not_found` |
| 没有签名头：加密的 URL 验证，Verification Token 对得上 | `200`，`{"challenge": "…"}` |
| 没有签名头的其余一切：不是 URL 验证、Token 不对、没加密、解不开、字段里有编码不成 UTF-8 的字符、`challenge` 过长 | `401 unauthenticated`，**不论哪一步没过都是同一个响应**（否则会成为能逐字节猜出明文的填充预言机） |
| 带签名头：签名不符，或缺 `X-Lark-Request-Timestamp`、`X-Lark-Request-Nonce` | `401 unauthenticated` |
| 签名对，但请求体不是 `{"encrypt": …}`、解不开（base64 非法、长度不是 16 的倍数、填充不合 PKCS7）或解出来的内容不合预期（缺字段；字段里有编码不成 UTF-8 的字符，如 JSON 转义出的孤立代理项 `"\ud800"`；`challenge` 超过 256 个字符） | `400 invalid_request` |
| 签名对，`header` 里的 Verification Token、App ID 或飞书企业对不上 | `401 unauthenticated` |
| 租户还没绑定飞书企业（还没有员工登录过） | `200`，不处理：核对不了 `tenant_key`，也不拿事件去绑定——绑定只能由真实的登录建立；这时租户里也还没有有飞书身份的员工 |
| `contact.user.deleted_v3`（员工离职），平台里有这名员工 | `200`，停用他（见下） |
| 平台里没有这名员工；别的事件类型；1.0 版事件 | `200`，忽略 |
| 平台内部出错（存储报错、保管库里的密文解不开） | `500`（飞书会重推） |

**员工离职**：按 `event.object.union_id` 找到员工，同步做[停用](#员工)的前两步（标停用、删掉他的登录会话：浏览器里的 cookie 立即失效），关闭他名下的会话放到后台（不让响应等它；每个实例同时最多 8 个后台收尾，同一名员工的合并成一个），回 `200`。停用原因按 `event.object.status`：`is_resigned` 为真是 `feishu_resigned`，否则 `is_exited` 为真是 `feishu_exited`，否则 `is_frozen` 为真是 `feishu_frozen`，都不为真也按 `feishu_resigned`（这个事件本身就是离职）。审计 `user_disabled` 的 `actor` 是 `system:feishu-event`，`data` 是 `reason` 与 `event_id`。**离职的管理员也会被停用**，哪怕他是最后一名管理员（兜底见[运维手册](../guides/operations.md#员工停用与离职)）。

- 签名不符、来源对不上的请求**只写平台日志、不写审计**（防止被刷），计指标 `xuanpu_feishu_events_total{result}`（取值见运维手册）。
- **不去重、不设时间窗**：停用可以重复执行，飞书重推、有人重放，都只会重复一次无害的停用（状态不变、不再记 `user_disabled`，后台的收尾重跑一遍）。
- 这个地址要能从飞书的服务器访问到（公网可达），部署时在反向代理上按来源地址限速。

## 员工

员工是飞书登录时自动建的（见[飞书登录](#飞书登录)），平台里没有新建、删除员工的接口。

```
GET /v1/users?status=active&limit=100

→ {"users": [{"user_id": "usr_5f0c1b2e7d9a4c3b8e6f1a20", "display_name": "张三",
              "avatar_url": "https://…/avatar.png", "role": "tenant_member", "status": "active",
              "disabled_reason": null, "created_at": 1790900000.0, "last_login_at": 1790903600.0,
              "disabled_at": null, "unverified_since": null}],
   "next_after": null}
```

| 参数 | 说明 |
| --- | --- |
| `status` | `active` 或 `disabled` |
| `role` | `tenant_admin` 或 `tenant_member` |
| `unverified` | `true` 只看对账时在飞书查不到的（`unverified_since` 不为空），`false` 只看查得到的 |
| `after` | 翻页：只看 `user_id` 大于它的 |
| `limit` | 默认 100，最大 1000 |

按 `user_id` 升序。`next_after` 不为 `null` 时还可能有下一页，把它作为 `after` 再查。

| 字段 | 说明 |
| --- | --- |
| `user_id` | 平台的员工标识，`usr_` 加 24 位十六进制 |
| `display_name`、`avatar_url` | 每次飞书登录时刷新 |
| `role` | `tenant_admin` 或 `tenant_member` |
| `status`、`disabled_reason`、`disabled_at` | `active` 或 `disabled`；停用的原因（`admin`、`feishu_resigned`、`feishu_frozen`、`feishu_exited`）与时间，启用时为 `null` |
| `created_at`、`last_login_at` | 建账号（首次登录）与最近一次登录的时间 |
| `unverified_since` | 对账时在飞书查不到此人的起始时间；查得到为 `null` |

响应里没有飞书的标识、登录会话之类的东西。`GET /v1/users/{user_id}` 返回同样的员工对象：租户管理员取谁都行；别人只能取自己（浏览器登录，或代员工调用时被代的那名员工）。

```
PUT /v1/users/{user_id}/role
{"role": "tenant_admin"}        # 或 "tenant_member"
→ 员工对象

PUT /v1/users/{user_id}/status
{"status": "disabled"}          # 或 "active"
→ 员工对象，另带 "cleanup_failed": []
```

- **停用**走停用流水线（原因 `admin`）：标停用；删掉他的全部登录会话（cookie 立即 `401`，代他调用 `403`），之后记 `user_disabled`；关闭他名下的全部活动会话。三步都做完才返回；`cleanup_failed` 列出失败的收尾（现在只有 `close_sessions` 一项），失败的另记 `user_cleanup_failed`。对已停用的员工再停用一次：状态不变、不再记 `user_disabled`，收尾重跑一遍——看到 `cleanup_failed` 不为空时这样重试。停用之后他名下会话的去向见[主人停用之后](#主人停用之后)。
- **启用**只改状态、记 `user_enabled`：已关闭的会话不恢复，删掉的登录会话也不恢复（员工重新登录即可）。`cleanup_failed` 总是空列表。
- **改角色**：角色真的变了才记 `user_role_changed`（`data` 是 `before`、`after`）。成员升为管理员，下一个请求就有管理员权限。
- **保住最后一名管理员**：降级或停用会让租户里一名启用的管理员都不剩时，`409 conflict`，`message` 说明原因（「这是租户里最后一名启用的管理员，不能降级或停用」），什么也不改；两名管理员同时互相降级，至多一个成功。飞书离职事件不受这一条限制（见[飞书事件回调](#飞书事件回调)）。
- 这四种记录的 `user_id` 都是被操作的员工，`actor` 是调用者。

| 情况 | 响应 |
| --- | --- |
| 不是租户管理员（列员工、改角色、改状态；包括浏览器登录的普通成员） | `403 permission_denied` |
| 员工不存在；普通成员取别人（同一句提示） | `404 not_found` |
| 要降级或停用的是最后一名启用的管理员 | `409 conflict` |
| 取值不在上面列的范围里、缺字段或有多余的字段 | `400 invalid_request` |

**第一个管理员**：员工先飞书登录一次（成为普通成员），持租户管理员 API key 的人（平台管理员可以用 `POST /v1/admin/tenants/{id}/keys` 签发这把 key）调 `PUT /v1/users/{user_id}/role` 把他设为管理员；之后管理员之间互相设置。最后一名管理员离职被停用时也这样重新指定。

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

### 修改配额

```
PUT /v1/admin/tenants/{id}/quota
{"max_active_sessions": 200, "max_daily_tokens": 10000000}

→ {"tenant_id": "acme", "name": "Acme", "status": "active",
   "quota": {"max_active_sessions": 200, "max_daily_tokens": 10000000, "max_concurrent_turns": null},
   "created_at": 1790898324.358114}
```

请求体与新建租户时的 `quota` 对象相同。这是**整体替换**：没给的项（或给了 `null`）恢复为不限，不保留原值——只想改一项，也要把另外两项按原值带上。`{}` 就是全部不限。

| 情况 | 响应 |
| --- | --- |
| 改成功 | `200`，租户对象。配额每次检查都现读，下一次建会话、发消息就按新值；已经超出新上限的（比如活动会话已经比新的 `max_active_sessions` 多）不受影响，只拦之后的 |
| 取值不是正整数，或有多余的字段 | `400 invalid_request`，`problems` 指出哪一项 |
| 不是平台管理员 | `403 permission_denied` |
| 租户不存在 | `404 not_found` |

审计里记一条 `tenant_quota_changed`，`data` 里是改之前（`before`）与改之后（`after`）的三项。

目前没有删除租户的接口。

## API key

```
POST /v1/admin/tenants/{id}/keys        # 平台管理员，给指定租户签发
POST /v1/keys                           # 租户管理员，给本租户签发
{"name": "backend", "role": "tenant_member"}        # role 省略时是 tenant_member

→ 201
{"key": "xp_be139be29730_…", "key_id": "be139be29730", "tenant_id": "acme",
 "name": "backend", "role": "tenant_member", "act_as_users": false,
 "created_at": 1790753313.91, "revoked_at": null}
```

`key` 只在这次响应里出现。`role` 只能是 `tenant_admin` 或 `tenant_member`。

`act_as_users` 省略时为 `false`；为 `true` 时这把 key 能[代员工调用](#系统代员工调用)。签发本身只有平台管理员与租户管理员能做，所以这项能力也只有他们能授予：成员（包括被代调用的成员）签发是 `403 permission_denied`。审计 `api_key_issued` 里记下 `act_as_users`。

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
{"name": "support-desk", "version": "1.0.0", "digest": "sha256:71887b9e…", "active": true,
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
 "created_by": "key:be139be29730", "metadata": {"customer": "C-42"}, "user_id": null}
```

| 字段 | 说明 |
| --- | --- |
| `package` | 必填 |
| `version` | 省略用生效版本。会话创建时钉住版本 |
| `entry` | 省略用清单 `entries` 的第一个；不在 `entries` 里是 `400` |
| `metadata` | 业务方自己的关联标识：最多 16 项，键值都是字符串、各 1–256 个字符。原样返回并进审计 |

响应里的 `status` 是 `active` 或 `closed`；`owner` 是当前持有会话的实例，无人持有为 `null`；`user_id` 是会话的主人：建会话的调用者代表的员工（浏览器登录或[代员工调用](#系统代员工调用)），API key 直接建的为 `null`。

`GET /v1/sessions` 返回 `{"sessions": [ … ]}`，只列调用者[看得见的](#可见范围)，最近活动的在前。`status=active|closed` 过滤状态，`limit` 默认 50、最大 200。`mine=true` 只看自己的：代表员工的调用者（浏览器登录、代员工调用）按员工过滤，同一名员工经浏览器、经代调用建的会话都算；不代表员工的 API key 按创建者（`created_by`）过滤。普通成员总是只列自己的，`mine` 对他不起作用（`mine=false` 也只列自己的，不报错）。

`DELETE /v1/sessions/{id}` 关闭会话，返回会话对象。进行中的对话会被取消。重复关闭不报错。只想停掉进行中的那一轮、会话接着用，见[停止生成](#停止生成)。

### 可见范围

| 调用者 | 能看见的会话 |
| --- | --- |
| 普通成员：浏览器登录或被代调用，且生效角色是 `tenant_member` | 只有自己的（会话的 `user_id` 是他） |
| 管理员：浏览器登录或被代调用，且生效角色是 `tenant_admin` | 本租户全部 |
| 不带 `X-Xuanpu-Act-As` 的 API key（不论角色） | 本租户全部 |

- 会话的每个接口——取、关、发消息、送审批、停止、回放、待办——都按这张表检查。看不见的会话与不存在的是同一个 `404 not_found`（同一句提示），不透露它是否存在。
- 代员工调用按员工的可见范围生效，角色取 key 与员工中较低的那个：管理员的 key 代成员调用只看见这名成员的会话；成员的 key 代管理员调用，生效角色是成员，也只看见这名管理员自己的。
- 不代表员工的 API key 看得见全部，是给后台系统用的；把成员的 key 交给员工本人用，他就能看见全租户的会话——员工请用浏览器登录，或由系统代他调用。

### 主人停用之后

会话的主人已停用时，**任何调用者**（包括管理员与系统 key）往这个会话发消息、送审批结果都是 `403 session_owner_disabled`，什么也不提交。这是兜底：员工停用时平台会关掉他名下的全部会话（[员工](#员工)停用的第 3 步，`session_closed` 的 `actor` 是 `system:user-disabled`；关掉之后再发是 `409 conflict`），这一条管的是还没关掉的时候——飞书离职事件触发的关会话在后台跑，后台任务也可能被停机取消。

会话正由别的实例持有时照样关得掉：关闭以控制面里的记录为准、立即生效，之后任何实例都不再接受它的消息与审批结果；持有它的那个实例不会收到通知，正在跑的那一轮跑完为止，引擎按空闲回收或停机时释放（与在别的实例上调 `DELETE /v1/sessions/{id}` 相同）。

```
POST /v1/sessions/ses_c79729649fb194db4eee9ff4/messages
{"text": "继续"}

→ 403
{"error": {"code": "session_owner_disabled",
           "message": "会话 'ses_c79729649fb194db4eee9ff4' 的主人已停用：不能再发消息或送审批结果（停止生成、关闭会话不受影响）"}}
```

- 停止生成、关闭会话照常可用（它们只会让会话更快停下）；取会话、回放、待办这些只读的接口也照常。
- 平台每个请求都查一次主人的状态，不缓存：停用立即生效，重新启用也立即恢复。
- API key 直接建的会话没有主人，不受这一条约束。

### 发消息

```
POST /v1/sessions/{id}/messages
{"text": "查一下工单 T-1001"}
→ 事件流
```

`text` 不能为空，最长 200 000 个字符。会话已关闭是 `409 conflict`；会话的主人已停用是 `403 session_owner_disabled`（见[主人停用之后](#主人停用之后)）。

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
{"request_id": "req_4c060f5db252bc57", "reason": "permission",
 "payload_schema": {"type": "object", "properties": {"granted": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["granted"]},
 "related_call_id": "call_3",
 "detail": {"scope": "tool_use", "target": "api__tickets__updateTicket",
            "reason": "调用连接器 tickets 的 updateTicket（write）",
            "call_chain": [], "call_id": "call_3", "thread_id": "thr_96548b0d001a65d6",
            "args": {"id": "T-1001", "body": {"priority": "high"}},
            "arguments_digest": "sha256:ab129ec1c7282db457ccb63ffaa5de0ae0929b62aba56e9f0a3fe52d84f7929f"},
 "ttl_seconds": null, "on_expire": "abort"}
```

连接器工具的审批，`detail` 里是要调的工具（`target`）、调用参数（`args`）与参数摘要（`arguments_digest`，与这次调用的过闸记录里的是同一个值）。

`GET /v1/sessions/{id}/pending` 随时返回当前的待办：`{"pending": [ … ]}`，没有待办时是空列表。

把结果送回：

```
POST /v1/sessions/{id}/resume
{"resolutions": {"req_4c060f5db252bc57": {"granted": true}}}
→ 事件流
```

拒绝是 `{"granted": false, "reason": "…"}`。批准后被挂起的那次调用才真正执行；拒绝则不执行，模型收到被拒的结果后继续。`resolutions` 不能为空。会话的主人已停用时是 `403 session_owner_disabled`，什么也不提交（见[主人停用之后](#主人停用之后)）。

`resolutions` 里有这次挂起中已经处理过的待办（之前送回过、已核销的）时，整次送回 `400 invalid_request`，消息里列出是哪几个，什么也不提交：只送回了一部分待办之后，内核会照样受理已经处理过的那几个、再做一遍，批准过的调用会再执行一次。平台按审计判断是否处理过，审计略有滞后，刚送回就紧跟着再送同一个时可能拦不住。提交之前读不到审计（库不可用）时，整次送回以 `500 internal_error` 失败，什么也没有提交给内核，可以原样重试。

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

看得见这个会话的调用者（见[可见范围](#可见范围)）都能停里面的对话，与 `resume` 一样不区分对话是谁发起的；审计里的 `turn_cancel_requested` 的 `actor` 记下是谁停的，`user_id` 与会话里别的记录一样是会话的主人。只想停自己发起的那一轮，就带上它的 `submission_id`。会话的主人已停用也能停。

| 情况 | 响应 |
| --- | --- |
| 没有在途的对话（包括会话已关闭） | `200`，`cancelled` 为空列表 |
| 这一轮还没开始（会话还在准备引擎，或内核约 1 秒内仍没接手） | `200`，`cancelled` 里没有它 |
| 给了 `submission_id`，但它不在途（已经结束或不存在） | `400 invalid_request` |
| `submission_id` 为空串或超过 64 个字符 | `400 invalid_request` |
| 会话正由另一个实例持有 | `409 session_busy`，带 `owner` |
| 会话不存在、属于别的租户，或调用者看不见它 | `404 not_found` |

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
GET /v1/audit?session_id=&kind=&source=&since=&until=&user_id=&actor=&after_id=&limit=

→ {"records": [{"audit_id": 51, "source": "gate", "kind": "connector_call", "at": 1790753335.73,
                "actor": "engine", "session_id": "ses_…", "stream": null, "seq": null, "data": {…},
                "user_id": "usr_5f0c1b2e7d9a4c3b8e6f1a20"}],
   "next_after_id": null}
```

| 参数 | 说明 |
| --- | --- |
| `session_id`、`kind` | 精确匹配 |
| `source` | `engine`、`gate`、`control` |
| `since`、`until` | Unix 秒；`since` 含，`until` 不含 |
| `user_id` | 只看归到这名员工名下的：他名下会话里的一切记录（不论是谁触发的），以及他做的与会话无关的控制面操作 |
| `actor` | 只看某个触发者的，精确匹配（如 `key:7a1c03d2e9f4`、`user:usr_…`；代员工调用是 `key:<key_id>/user:<员工标识>`，与那把 key 自己的 `key:<key_id>` 是两个值）。用来查「某个管理员或某把 key 在别人的会话里做了什么」，可以与 `user_id` 组合。这是低频的排查查询，审计表上没有为它建索引，记录多时会慢 |
| `after_id` | 只返回编号更大的记录，默认 0 |
| `limit` | 默认 100，最大 1000 |

记录按 `audit_id` 升序。`next_after_id` 不为 `null` 表示可能还有下一页。记录的 `user_id` 是它归到哪名员工名下，`actor` 是谁触发的：会话里的一切记录（内核事件、过闸记录、会话上的控制面操作）是会话的主人，不论是谁触发的；与会话无关的控制面操作是调用者代表的员工；与员工无关的为 `null`。员工查看自己的记录以后再开放，现在仍只有租户管理员能查。各字段与各种事件的含义见[审计与用量](../guides/audit-and-usage.md)。

## 用量

```
GET /v1/usage?since=2026-09-01&until=2026-09-30

→ {"usage": [{"day": "2026-09-30", "input_tokens": 2940, "output_tokens": 252,
              "total_tokens": 3192, "turns": 5}]}
```

两个参数都必填，两端都含。没有用量的日子不出现。

按员工：

```
GET /v1/usage/users?since=2026-10-01&until=2026-10-31

→ {"usage": [{"day": "2026-10-02", "user_id": "usr_364b889313336be0226bae9e", "input_tokens": 100,
              "output_tokens": 20, "total_tokens": 120, "turns": 1}]}
```

每天每名员工一条，按日期、员工排序；`since`、`until` 同上，`user_id` 可选，只看这名员工的。只含有主人的会话（API key 直接建的会话只算进上面的租户总量），所以各员工之和不大于租户总量。

## 指标

```
GET /metrics
Authorization: Bearer <抓取令牌>

→ 200，Content-Type: text/plain; version=0.0.4; charset=utf-8
# HELP xuanpu_turns_total 结束的对话轮数，按结局分
# TYPE xuanpu_turns_total counter
xuanpu_turns_total{outcome="completed"} 2.0
xuanpu_turns_total{outcome="cancelled"} 0.0
…
xuanpu_connector_calls_total{decision="allowed",transport="http"} 1.0
…
```

- 只有配置了 `metrics.token_hash` 才开；没配时是 404。
- 凭据是单独的抓取令牌（不是 API key，也不是根凭据）；不带或不对都是 401，错误体与其他接口一样。
- 每个实例只报自己的数；指标不带租户标签，按租户看用量用上面的 `/v1/usage`。
- 指标的含义见[运维手册的监控一节](../guides/operations.md#监控与排查)。
