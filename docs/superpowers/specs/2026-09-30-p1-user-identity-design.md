# P1 用户身份：飞书登录 —— 详细设计

- 日期：2026-09-30
- 状态：设计已与用户逐项确认，待写实现计划
- 上游决策：[ADR 0007](../../decisions/0007-workbench-enterprise-apps-and-personal-space.md)（决策 2 与「拍板结果」）、[ADR 0009](../../decisions/0009-small-core-connect-everything.md)、[ADR 0010](../../decisions/0010-user-identity-feishu-login.md)（本设计的关键取舍）
- 读者：实现 P1 的工程师与 agent

> 本文是实现前的设计。实现落地后，当前生效的行为以 `docs/architecture/` 为准。

## 1. 目标与范围

xuanpu 第一版只认 API key，没有「员工」这个概念。P1 让员工用飞书账号登录平台，并让平台里的每一件事都能归到具体的人。后面所有子项目都依赖它。

| 做 | 不做（归属） |
| --- | --- |
| 飞书登录；平台用户；浏览器登录会话 | 部门、用户组与应用可见范围（P2） |
| 调用者身份带上用户；系统代员工调用 | 个人空间的冻结与移交（P3） |
| 会话归属与可见性；审计、用量带上用户 | 员工对数据源的授权与令牌（P6） |
| 离职感知（事件回调 + 每日对账）与停用流水线 | 工作台（P5） |
| 用户管理接口；控制台的最小改动 | 按人设配额；上级或管理员审批 |

P1 提供一个「员工停用」通知点，P3（冻结空间）、P6（撤销令牌）以后挂上去。

## 2. 已确定的前提

| 前提 | 来源 |
| --- | --- |
| 直接接飞书登录；平台内部按「身份源」抽象，以后可以加通用 OIDC | ADR 0007 拍板结果第 1 项 |
| 谁在飞书自建应用的「可用范围」里，谁就能登录；首次登录自动建账号，默认普通成员 | P1 设计确认 |
| 离职感知：订阅飞书离职事件（HTTP 回调）+ 每日对账 | P1 设计确认 |
| 登录会话有效期默认 7 天，可按租户配置 | P1 设计确认 |
| P1 包含控制台的最小改动：飞书登录、用户页 | P1 设计确认 |
| 员工停用：立即撤销全部授权、关闭会话 | ADR 0007 拍板结果第 3 项 |
| 需要审批的操作由员工本人确认 | ADR 0007 拍板结果第 4 项 |

## 3. 核实过的飞书事实

2026-09-30 依据飞书开放平台文档，以及官方 Python SDK `lark-oapi` 1.7.3（MIT）的源码核实：

| 项 | 内容 |
| --- | --- |
| 授权页 | `https://accounts.feishu.cn/open-apis/authen/v1/authorize`，参数 `client_id`、`response_type=code`、`redirect_uri`、`scope`、`state`、`code_challenge`、`code_challenge_method=S256`。授权码 5 分钟有效，只能用一次 |
| 换令牌 | `POST https://open.feishu.cn/open-apis/authen/v2/oauth/token`，请求体是 JSON：`grant_type=authorization_code`、`client_id`、`client_secret`、`code`、`redirect_uri`、`code_verifier`。只有授予 `offline_access` 时才返回 `refresh_token`。错误分两层：HTTP 状态码，加响应体里的 `code` |
| 取用户信息 | `GET https://open.feishu.cn/open-apis/authen/v1/user_info`，用员工令牌。不需要额外权限就能拿到 `name`、`avatar_url`、`open_id`、`union_id`、`tenant_key`。`email`、`user_id` 等要额外权限；飞书还提示：邮箱、手机号是管理员导入的、未经本人验证，不建议当登录凭证 |
| 三种用户标识 | `open_id` 每个应用一套，应用重建后会变；`union_id` 在同一开发商（自建应用就是企业自己）的各个应用之间保持不变；`user_id` 是企业内标识，但管理员可以修改 |
| 可用范围 | 不在自建应用可用范围内的员工无法使用应用，错误码 20010（user not visible to app）；自建应用只能由本企业的账号访问 |
| 离职事件 | `contact.user.deleted_v3`，事件体 `event.object` 里有 `open_id`、`union_id`、`user_id` 与 `status`（`is_frozen`、`is_resigned`、`is_activated`、`is_exited`、`is_unjoin`）。订阅需要 `contact:contact.base:readonly` 等权限之一 |
| 事件回调的加密与签名 | 配置了 Encrypt Key 后，请求体是 `{"encrypt": "<base64>"}`：AES-256-CBC，密钥是 Encrypt Key 的 SHA-256，IV 取密文的前 16 字节，PKCS7 填充。签名在请求头 `X-Lark-Signature`，等于 `sha256(X-Lark-Request-Timestamp + X-Lark-Request-Nonce + Encrypt Key + 原始请求体)` 的十六进制 |
| 长连接 | 不需要公网地址，但 SDK 的长连接客户端在模块导入时绑定全局事件循环、`start()` 一直阻塞，不能放进平台的异步服务里。P1 不用它（见 ADR 0010） |
| 批量查员工 | `GET https://open.feishu.cn/open-apis/contact/v3/users/batch`，支持应用身份令牌，`user_id_type` 可以是 `union_id`，一次最多 50 个。不在应用通讯录权限范围内的人不会返回；离职员工是否返回，文档没写 |

**实现时要再对照文档确认的**：应用身份令牌接口（`POST /open-apis/auth/v3/tenant_access_token/internal`，参数 `app_id`、`app_secret`）、URL 验证请求的格式、事件重推的间隔与次数。这几项本次没有逐字核对。

## 4. 模块划分

身份相关的代码集中在一个新目录 `xuanpu_server/identity/`，不塞进已有的大文件（`controlplane/postgres.py` 已有 582 行，上限是 800 行）：

| 模块 | 职责 |
| --- | --- |
| `identity/models.py` | 用户、外部身份、租户身份配置、登录会话、登录尝试、按用户用量这几类记录 |
| `identity/store.py` | `IdentityStore` 协议 |
| `identity/memory.py`、`identity/postgres.py` | 两个实现，对同一套契约测试负责 |
| `identity/feishu.py` | 飞书接口的最小客户端：拼授权地址、换令牌、取用户信息、取应用身份令牌、批量查员工。基地址与 httpx 传输层由构造参数注入，测试时换成模拟服务端 |
| `identity/events.py` | 事件回调：验签、解密、URL 验证、解析离职事件。只做纯计算，不碰存储 |
| `identity/service.py` | `IdentityService`：登录的开始与完成、解析登录会话、停用流水线、对账、用户管理 |
| `identity/cookies.py` | 登录 cookie 与防跨站请求伪造的校验 |
| `api/identity.py` | 身份相关的 HTTP 路由，挂到现有的 FastAPI 应用上 |
| `controlplane/migrations/0004_identity.sql` | 新表，以及会话表、审计表加 `user_id` 列 |

依赖方向不变：`identity/` 只依赖 `tenancy.py`、`secrets.py`、`audit.py` 与控制面模型，不依赖 `runtime/`。停用时关闭会话，由 `bootstrap.py` 把运行时侧的监听者注册进来。

## 5. 数据模型

### 5.1 新增记录（`IdentityStore`）

**用户 `UserRecord`**

| 字段 | 说明 |
| --- | --- |
| `tenant_id` | 所属租户 |
| `user_id` | 平台分配：`usr_` 加 12 位十六进制。平台内部各处一律用它，飞书的标识只作外部映射 |
| `display_name`、`avatar_url` | 每次登录时按飞书返回刷新 |
| `role` | `tenant_admin` 或 `tenant_member`，沿用 `tenancy.Role` |
| `status` | `active` 或 `disabled` |
| `disabled_reason` | `feishu_resigned`、`feishu_frozen`、`feishu_exited`、`admin` |
| `created_at`、`last_login_at`、`disabled_at` | 时间 |
| `unverified_since` | 对账时在飞书查不到此人的起始时间；再次查到时清空 |

**外部身份 `ExternalIdentity`**：`tenant_id`、`provider`（第一版只有 `feishu`）、`subject`（飞书的 `union_id`）、`user_id`、`attributes`（存 `open_id`，供 P6 调飞书接口用）。`(tenant_id, provider, subject)` 唯一。

**租户身份配置 `TenantIdentityConfig`**

| 字段 | 说明 |
| --- | --- |
| `tenant_id` | 租户 |
| `provider` | `feishu` |
| `app_id` | 飞书自建应用的 App ID |
| `app_secret_ref`、`encrypt_key_ref`、`verification_token_ref` | 三个密钥在保管库里的名字。明文由租户管理员经现有的 `PUT /v1/secrets/{name}` 写入。三者都必填：没有 Encrypt Key 就没有签名，事件回调无法验证来源 |
| `feishu_tenant_key` | 首次成功登录时绑定；之后的登录与事件都必须一致 |
| `session_ttl_seconds` | 登录会话有效期；为空时用平台默认值 |

**登录会话 `LoginSession`**：`token_hash`（登录令牌的 SHA-256，令牌本身不落库）、`tenant_id`、`user_id`、`created_at`、`expires_at`、`last_seen_at`。有效期从登录时刻算起，不滑动。`last_seen_at` 最多每 5 分钟更新一次。

**登录尝试 `LoginAttempt`**：`state`（32 字节随机数）、`tenant_id`、`code_verifier`、`return_to`、`created_at`。10 分钟有效；取出时即删除，只能用一次。放在库里是为了让多个实例都能认。

**按用户用量 `UserUsageRecord`**：`tenant_id`、`day`、`user_id`，以及与 `UsageRecord` 相同的计数字段。租户总量那张表照旧。

**对账状态**：每个租户一条，`last_run_at`、`lease_owner`、`lease_expires_at`，用来保证多个实例每天只对一次（见 7.3）。

### 5.2 已有记录的改动

| 记录 | 改动 |
| --- | --- |
| `SessionRecord` | 加 `user_id: str \| None`：谁的会话。API key 直接建的会话为空 |
| `AuditRecord` | 加 `user_id: str \| None`，建索引。`AuditQuery` 加 `user_id` 条件 |
| `ApiKeyRecord` | 加 `can_act_as_user: bool = False` |
| `Principal` | 加 `user_id: str \| None`；`key_id` 改为 `str \| None`（浏览器登录时没有 key） |

`Principal.actor` 的取值：

| 调用者 | `actor` |
| --- | --- |
| 根凭据 | `key:root`（不变） |
| API key | `key:<key_id>`（不变） |
| 浏览器登录 | `user:<user_id>` |
| 系统代员工 | `key:<key_id>/user:<user_id>` |

## 6. 认证与会话

### 6.1 识别调用者

```
请求带 Authorization 头？
  是 → 按现有方式校验 API key / 根凭据
       └─ 另带 X-Xuanpu-Act-As 头？→ 走代员工调用（6.3）
  否 → 请求带登录 cookie？
       是 → 解析登录会话（6.2）
       否 → 401 unauthenticated
```

两种认证失败对外都是同一个 401，不透露是哪一步没过。

### 6.2 浏览器登录会话

- cookie `xuanpu_session`：值是 32 字节随机数；HttpOnly、SameSite=Lax、Path=/；默认 Secure，本地 HTTP 开发时可以用配置关掉。
- 每个请求都查库：登录会话存在且未过期、用户为 `active`、租户为 `active`。三者任一不满足就返回 401，并清掉 cookie。**不做缓存**，停用立即生效。
- **防跨站请求伪造**：用 cookie 认证的非 GET 请求，必须带 `X-Requested-By: xuanpu` 头；请求带了 `Origin` 的，必须等于 `public_url` 的源。平台不开放带凭据的跨域，跨站页面的脚本发不出带自定义头的请求。
- 每次登录都签发新令牌，不复用旧的，以防会话固定攻击。

### 6.3 系统代员工调用

- 只有租户管理员（或平台管理员）能签发带 `can_act_as_user` 的 API key：签发接口加一个 `act_as_users` 字段。
- 调用时带 `X-Xuanpu-Act-As: <user_id>`。这个用户必须属于同一租户，且为 `active`，否则返回 403。
- **生效角色取 key 的角色与用户角色中较低的那个**，防止普通成员的 key 借管理员身份越权。
- 审计里的 `actor` 同时写出 key 和用户，`user_id` 列填这名员工。

## 7. 流程

### 7.1 登录

```
浏览器 → GET /v1/auth/feishu/login?tenant=acme&return_to=/console/
平台   ：租户存在且 active、已配置飞书 → 生成 state 与 PKCE 校验串，写入登录尝试
       → 302 到飞书授权页（redirect_uri = <public_url>/v1/auth/feishu/callback，不申请额外权限）
飞书   → 302 回 /v1/auth/feishu/callback?code=…&state=…（员工拒绝时带 error=access_denied）
平台   ：1. 按 state 取出并删除登录尝试（不存在或已过期 → state_invalid）
         2. 用授权码与校验串换令牌
         3. 用令牌取用户信息
         4. 核对 tenant_key：首次登录则绑定，以后必须一致（不一致 → tenant_mismatch）
         5. 按 (租户, feishu, union_id) 找用户；找不到就新建普通成员，记 user_created
         6. 用户为 disabled → user_disabled
         7. 刷新姓名、头像、open_id；更新 last_login_at
         8. 建登录会话，写 cookie，记 login_succeeded
         9. 302 到 return_to
```

- 登录拿到的飞书令牌用完即丢，不保存。读文档、日历等数据的授权由 P6 另行申请。
- `return_to` 只接受以 `/` 开头、且不以 `//` 开头的站内路径，否则改用 `identity.login_landing_path`，防止被利用做开放跳转。
- `tenant` 参数可以省略，省略时用 `identity.default_tenant`，方便只有一家企业的部署。
- 失败一律 302 到落地页，带 `?login_error=<原因>`，并记 `login_failed`（不记授权码和令牌）：

| 原因 | 含义 |
| --- | --- |
| `feishu_denied` | 员工在飞书授权页取消了 |
| `not_in_app_scope` | 不在飞书应用的可用范围内（错误码 20010） |
| `state_invalid` | 登录链接过期或被重复使用 |
| `tenant_mismatch` | 飞书企业与租户绑定的不一致 |
| `tenant_unavailable` | 租户不存在、已停用或没配置飞书 |
| `user_disabled` | 平台里这名员工已停用 |
| `identity_unavailable` | 调飞书接口失败（网络或飞书服务端错误） |

「不在可用范围」到底是在飞书授权页上直接报错，还是换令牌时才报错，要实机核实（第 14 节）。

### 7.2 停用流水线

入口只有一个：`IdentityService.disable_user(tenant_id, user_id, reason, actor)`。重复调用结果不变。

1. 把用户标为 `disabled`，记下原因和时间。状态真的变了才记一条 `user_disabled` 审计；
2. 删除这名员工的全部登录会话；
3. 依次通知已注册的「员工停用」监听者。P1 只注册一个：关闭他名下所有 `active` 的会话（等同于强制关闭）。监听者必须能重复执行；某个失败时记 `user_cleanup_failed`，不影响其余的。管理员再对这名员工执行一次停用，就会重跑全部监听者。

另外补一条运行时规则：**会话的主人已停用时，任何调用者（包括系统 key）都不能再往这个会话发消息或送审批结果**，返回 403。这样即使第 3 步没执行完，停用员工的会话也不会再往下跑。

管理员重新启用员工，只改状态、记 `user_enabled`，不恢复已关闭的会话。如果员工在飞书里仍是离职状态，下一次对账会再把他停用。

### 7.3 离职事件回调

`POST /v1/identity/feishu/events/{tenant_id}`，不走平台的认证，靠签名验证来源：

1. 取租户的飞书配置，没有则返回 404；
2. 按第 3 节的算法校验 `X-Lark-Signature`，用常数时间比较；不符则返回 401，只写服务端日志，不写审计（防止被刷）；
3. 解密 `encrypt` 字段；
4. 若是 URL 验证请求：核对其中的 token 等于 Verification Token，原样回 `challenge`；
5. 否则核对 `header.token` 等于 Verification Token、`header.app_id` 等于配置、`header.tenant_key` 等于绑定值；
6. `header.event_type` 是 `contact.user.deleted_v3` 时，按 `event.object.union_id` 找到用户，同步执行停用流水线的第 1、2 步，第 3 步放到后台任务；
7. 返回 200。

- 飞书要求 3 秒内响应，所以关闭会话放后台。后台任务在崩溃时会丢，但有 7.2 的运行时规则兜底，空闲会话也会被现有的回收机制释放。
- 停用本身可以重复执行，所以不做事件去重，也不设时间窗；飞书的重推只会重复一次无害的操作。
- 其他事件类型一律返回 200，然后忽略。

### 7.4 每日对账

平台实例现有的维护循环，每次检查一遍配置了飞书的租户：

1. **认领**：`IdentityStore.claim_reconcile(tenant_id, owner, now, interval, lease_seconds)`。距上次完成超过 `identity.reconcile_interval_seconds`、且没有别的实例持有未到期的租约时，才认领成功（在存储里做比较后更新）；
2. 用 App ID 与 App Secret 取应用身份令牌，在内存里缓存到过期前 5 分钟；
3. 分页取本租户所有 `active` 且有飞书身份的用户，每 50 人一批调批量查询接口（`user_id_type=union_id`）；
4. 返回了、且状态为离职、暂停或已退出的，按原因（`feishu_resigned`、`feishu_frozen`、`feishu_exited`）走停用流水线；
5. **没返回的，不停用**：设置 `unverified_since`，记 `identity_unverified`，在控制台用户页标出来，由管理员判断。再次查到时清掉标记；
6. 完成后记一条 `identity_reconciled`（检查了多少人、停用了多少、多少人查不到），释放租约。

也可以手工触发：`POST /v1/identity/feishu/reconcile`（租户管理员），它不受周期限制，但仍然要认领租约。

飞书接口失败时，本次对账中止：已经停用的保持停用，其余不动；记 `identity_reconciled` 并注明失败，下个周期再试。

## 8. 可见性、审计与用量

### 8.1 会话可见性

| 调用者 | 可见范围 |
| --- | --- |
| 普通成员（浏览器登录或被代调用） | 只有自己的会话。列会话时强制只列自己的；访问别人的会话返回 404 |
| 管理员（浏览器登录） | 本租户全部 |
| 不带代调用的 API key | 本租户全部（不变） |

建会话时：调用者有 `user_id` 的，把它写进会话。`mine=true` 对用户按 `user_id` 过滤，对 API key 仍按创建者过滤。

### 8.2 审计

- **`user_id` 从哪里来**：控制面操作取调用者身份；内核事件与过闸记录取会话上的 `user_id`，由事件枢纽在写入时填上。
- `GET /v1/audit` 加 `user_id` 查询条件。成员查看自己的记录属于 P6，P1 仍只对管理员开放。
- 新增的审计类型：

| 类型 | 何时 |
| --- | --- |
| `login_succeeded`、`login_failed`、`logout` | 登录与退出 |
| `user_created` | 首次登录自动建账号 |
| `user_role_changed` | 角色变更 |
| `user_disabled`、`user_enabled` | 状态变更 |
| `user_cleanup_failed` | 停用监听者失败 |
| `identity_config_updated` | 飞书配置变更 |
| `identity_unverified` | 对账查不到某人 |
| `identity_reconciled` | 一次对账结束 |

### 8.3 用量

一轮对话结束入账时，会话有 `user_id` 的，同时累加按用户的那张表。新接口 `GET /v1/usage/users?since=&until=`（租户管理员）按天、按用户返回。

## 9. HTTP 接口

| 方法与路径 | 身份 | 说明 |
| --- | --- | --- |
| `GET /v1/auth/feishu/login` | 无 | 开始登录，302 到飞书 |
| `GET /v1/auth/feishu/callback` | 无 | 飞书回调，完成登录，302 到落地页 |
| `POST /v1/auth/logout` | 登录会话 | 删除当前登录会话，清 cookie |
| `GET /v1/me` | 任意 | 当前调用者：用户、角色、租户；API key 调用时返回 key 信息 |
| `GET /v1/users?status=&unverified=` | 租户管理员 | 列出用户 |
| `GET /v1/users/{user_id}` | 租户管理员或本人 | 取用户 |
| `PUT /v1/users/{user_id}/role` | 租户管理员 | `{"role": "tenant_admin" \| "tenant_member"}` |
| `PUT /v1/users/{user_id}/status` | 租户管理员 | `{"status": "active" \| "disabled"}`；停用走 7.2 的流水线，原因记为 `admin` |
| `GET /v1/identity/feishu` | 租户管理员 | 当前配置（不含密钥明文），以及要填进飞书后台的回调地址、事件地址 |
| `PUT /v1/identity/feishu` | 租户管理员 | `{"app_id", "app_secret_ref", "encrypt_key_ref", "verification_token_ref", "session_ttl_seconds"}`；引用的密钥必须已存在 |
| `POST /v1/identity/feishu/events/{tenant_id}` | 飞书签名 | 事件回调（7.3） |
| `POST /v1/identity/feishu/reconcile` | 租户管理员 | 立即对账（7.4） |
| `GET /v1/usage/users` | 租户管理员 | 按用户的用量 |

已有接口的变化：

- `POST /v1/admin/tenants/{id}/keys` 和 `POST /v1/keys` 加 `act_as_users` 字段；
- 所有租户接口都接受登录 cookie；
- 会话相关接口按 8.1 过滤；
- `GET /v1/audit` 加 `user_id` 条件。

**第一个管理员怎么产生**：员工先登录一次成为普通成员；持租户管理员 API key 的人（平台管理员可以用现有接口签发这把 key）调 `PUT /v1/users/{user_id}/role` 把他设为管理员。之后管理员在控制台里互相设置。

## 10. 控制台

- **登录页**：保留粘贴 API key；控制台与平台同源时，另显示「用飞书登录」。租户取自地址栏的 `?tenant=`，没有就用 `identity.default_tenant`。登录失败时，按 `login_error` 显示原因。
- **cookie 模式**：请求不带 `Authorization`，写请求带 `X-Requested-By: xuanpu`；页面上显示当前员工，提供退出按钮。
- **「用户」页**（仅管理员可见）：姓名、角色、状态、最近登录、查不到标记；操作有设为管理员、取消管理员、停用、启用。
- 飞书应用配置走接口，手册里给出完整命令，控制台不做表单。

## 11. 配置

在 `ServerConfig` 里加两处：

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `public_url` | 无 | 平台对外地址。启用飞书登录时必填，用来生成回调地址、事件地址，并校验请求来源 |
| `identity.session_ttl_seconds` | `604800` | 登录会话的默认有效期（7 天） |
| `identity.cookie_secure` | `true` | cookie 是否只走 HTTPS；本地用 HTTP 开发时设为 `false` |
| `identity.reconcile_interval_seconds` | `86400` | 对账周期 |
| `identity.default_tenant` | 无 | 登录链接省略 `tenant` 时用的租户 |
| `identity.login_landing_path` | `/console/` | 登录完成或失败后的默认落地页 |
| `identity.feishu_open_base`、`identity.feishu_accounts_base` | `https://open.feishu.cn`、`https://accounts.feishu.cn` | 飞书接口的基地址；测试时指向模拟服务端，以后也可用于接 Lark 国际版 |

## 12. 错误处理与安全

| 关注点 | 做法 |
| --- | --- |
| 开放跳转 | `return_to` 只收站内路径 |
| 跨站请求伪造 | cookie 为 SameSite=Lax；写请求要求自定义头；校验 `Origin` |
| 会话固定 | 每次登录签发新令牌 |
| 令牌泄露 | 登录令牌只存摘要；飞书的授权码和令牌不进日志、不进审计；登录拿到的飞书令牌用完即丢 |
| 身份冒用 | 按 `union_id` 识别，不用邮箱、手机号；核对 `tenant_key` |
| 回调伪造 | Encrypt Key 必填；签名用常数时间比较；核对 Verification Token、`app_id`、`tenant_key` |
| 越权代调用 | 能力要显式授予；生效角色取两者中较低的；目标用户必须同租户、且为启用状态 |
| 停用不彻底 | 每个请求都查库；主人已停用的会话拒绝继续；监听者失败时留审计，可重跑 |
| 飞书不可用 | 登录失败原因为 `identity_unavailable`；对账中止后下个周期重试；事件回调不依赖对飞书的主动调用 |

## 13. 依赖

- **不引入飞书 SDK。** 长连接用不上（第 3 节）；剩下的是 4 个接口调用（换令牌、取用户信息、取应用身份令牌、批量查员工）和回调的验签解密，用现有的 `httpx` 与 `cryptography` 就能写完，比引入整个 SDK（它会带进 `requests`、`pycryptodome`、`websockets`）小。
- **不引入 OAuth 库。** 飞书的令牌接口要 JSON 请求体，不是标准写法；PKCE 用标准库的 `secrets` 与 `hashlib` 即可。
- **内核不需要改动。**

这一段依 ADR 0003 的要求说明，并记入 ADR 0010。

## 14. 测试

| 层 | 内容 |
| --- | --- |
| 纯函数 | PKCE 校验串与 challenge；`return_to` 校验；签名计算；解密（测试里按第 3 节的算法用 `cryptography` 加密出样本，包括填充错误、长度错误等坏样本） |
| 飞书客户端 | 用 httpx 的 `MockTransport` 做模拟服务端：成功、20010、令牌接口的错误码、网络错误 |
| 存储契约 | `IdentityStore` 的全部方法对内存与 Postgres 实现同跑，包括登录尝试只能用一次、对账租约的并发认领 |
| 接口 | 登录全流程（首次登录建账号、再次登录、各种失败原因）；cookie 认证；写请求缺自定义头或 `Origin` 不符被拒；代员工调用与角色取较低者；成员只能看到自己的会话；用户管理；飞书配置引用不存在的密钥被拒 |
| 停用 | 流水线三步；重复调用无副作用；监听者失败留审计；主人已停用的会话拒绝发消息 |
| 事件回调 | URL 验证；签名不符；token、`app_id`、`tenant_key` 不符；离职事件导致停用；未知事件被忽略 |
| 对账 | 状态为离职、暂停、已退出的被停用；查不到的只标记；再次查到时清标记；两个实例只有一个认领成功；飞书报错时中止 |
| 控制台 | 静态页面测试覆盖新页面与登录按钮 |
| 文档 | 现有文档测试会要求新路由、错误码、配置键、审计类型都写进文档 |

**只能实机核实的**，写成手册里的核对清单，需要一个测试用的飞书自建应用：

1. 用户信息接口在不加权限时实际返回哪些字段；
2. 不在可用范围时，报错出现在授权页还是换令牌时；
3. 离职员工在批量查询接口里还返回不返回；
4. 事件回调的 URL 验证与离职事件，实际报文是否与第 3 节一致。

## 15. 文档（随实现同步）

| 文档 | 改动 |
| --- | --- |
| `docs/architecture/http-api.md` | 新接口；两种认证方式；代员工调用；可见性规则；新的错误原因 |
| `docs/architecture/README.md` | 新增「身份」一节；租户隔离表加上用户；已知限制（对账查不到不自动停用、事件回调要求公网可达） |
| `docs/guides/` 新增「接入飞书登录」 | 在飞书后台建自建应用、配重定向地址与事件地址、开权限、设可用范围与通讯录权限范围、配 Encrypt Key；写密钥、调配置接口、指定第一个管理员；实机核对清单 |
| `docs/guides/operations.md` | 新配置项；对账；停用流水线 |
| `docs/guides/audit-and-usage.md` | 新审计类型；按用户的用量 |
| `docs/guides/integrating.md` | 代员工调用 |
| `packages/xuanpu-console/README.md` | 飞书登录与用户页 |

## 16. 建议的实现顺序

1. 模型、`IdentityStore` 协议、内存实现与契约测试；迁移脚本与 Postgres 实现；
2. `Principal` 与认证入口的改动（cookie、代员工调用、防跨站请求伪造）；
3. 飞书客户端与登录流程；
4. 会话归属、可见性、审计与用量带上用户；
5. 停用流水线、事件回调；
6. 对账；
7. 控制台；
8. 文档与实机核对清单。

每一步结束时，pytest、ruff、mypy 都要全绿。
