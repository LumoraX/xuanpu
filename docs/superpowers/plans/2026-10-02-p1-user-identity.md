# P1 用户身份 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 员工用飞书账号登录平台，平台里的每一件事都能归到具体的人；离职自动停用（设计见 [P1 详细设计](../specs/2026-09-30-p1-user-identity-design.md)，用户已逐项确认）。

**Architecture:** 新目录 `xuanpu_server/identity/` 承载身份域（模型、存储协议与两个实现、飞书客户端、事件回调、服务、cookie）；`api/identity.py` 挂身份路由。认证入口扩成三种调用者（API key、浏览器登录 cookie、系统代员工）。会话、审计、用量带上 `user_id`。停用经一个通知点收口，P3/P6/P9 以后挂监听者。内核不改。

**Tech Stack:** Python 3.12、FastAPI、asyncpg、httpx（飞书接口，`MockTransport` 测试）、cryptography（事件回调解密）、标准库 `secrets`/`hashlib`（PKCE）。不引入飞书 SDK 与 OAuth 库（ADR 0010）。

---

## 通用约定（每个任务都适用）

- 仓库 `xuanpu`，分支 `feat/p1-identity`（从合入 P0 后的 `main` 分出）。同级仓 taifeng-sandbox、taifeng-stores 不改。
- 工程约定以 `AGENTS.md` 为准：中文 docstring 与注释；禁止 silent fallback；业务代码不读环境变量；文件 ≤ 800 行（警戒 500）、函数 ≤ 80 行；只 import `taifeng.__all__`。
- **大文件不再变大**：`runtime/sessions.py`（603 行）、`controlplane/postgres.py`（591 行）、`api/app.py` 已超警戒线。本计划的新逻辑放进新模块，这几个文件只加调用；Task 1 先把 `controlplane/postgres.py` 拆开。
- 每个任务结束前门禁全绿：`uv run pytest -q`、`uv run ruff check --select F,S108,I,TC packages`、`uv run mypy packages/*/src`、`uv lock --check`。文档覆盖测试（`packages/xuanpu-cli/tests/test_examples_and_docs.py`）会要求新路由、错误码、配置键、审计类型都写进文档——每个任务把自己新增的这些写进对应文档（http-api.md、operations.md、audit-and-usage.md、examples/xuanpu.yaml），其余文档留给 Task 9。
- commit message 中文，结尾带：

  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  ```
- 子代理执行：可能超过 3 分钟的命令用后台运行加短命令轮询，不要在前台长时间等待。

## 与设计的对齐（P0 之后的变化）

| 设计里写的 | 现在的做法 |
| --- | --- |
| 迁移 `0004_identity.sql` | 编号仍是 0004（现有 0001–0003） |
| `controlplane/postgres.py` 加 `user_id` 列的读写 | Task 1 先把审计、用量的 SQL 拆到 `controlplane/postgres_audit.py`，再加列 |
| 「停用时关闭会话」由 bootstrap 注册监听者 | 监听者调 `SessionManager.close`（走 P0 的 `_retire` 收尾）；`sessions.py` 不加新逻辑，监听者放 `identity/listeners.py` |
| 内核事件与过闸记录的 `user_id` 由事件枢纽填 | 事件枢纽按会话记录填；过闸记录（闸门与人工拒绝补记）经 `SubmissionIndex` 从会话标识查到 `user_id`（登记会话时一并登记主人） |
| 用量入账 | 事件枢纽 `_account` 同时写按用户的表；P0 的估算入账（`usage_estimated`）同样计入按用户的表 |
| 维护循环 | 对账逻辑放 `identity/reconcile.py`，**挂进** Task 4 修正时新建的 `identity/maintenance.py` 周期任务（`IdentityMaintenance`，已每 5 分钟清一次过期登录记录），不另起循环；不塞进 `SessionManager.maintain` |
| 指标 | 新增 `xuanpu_logins_total{result}`（`succeeded` 或失败原因）与 `xuanpu_users_disabled_total{reason}`；不带租户标签。另加 `xuanpu_feishu_events_total{result}`（Task 6）：`url_verification`、`accepted`、`ignored`、`bad_signature`、`rejected`、`bad_request`、`unknown_tenant`、`internal_error`，取值固定，签名不符等不写审计的情况靠它看 |
| `user_id` 为 `usr_` 加 12 位十六进制 | 沿用 `ids.new_id("usr")`（`identity.new_user_id`）：`usr_` 加 24 位十六进制，与 `ses_` 等标识同一格式（Task 2） |
| 登录尝试只有 `created_at`，「10 分钟有效」 | 记录加 `expires_at`，有效期由服务层建记录时算好；存储不读时钟，按调用方给的 `now` 判断过期：`take_login_attempt`、`get_login_session` 对过期的返回空（Task 2） |
| 身份存储全部按租户隔离 | 四个方法发生在知道租户之前或本来就跨租户，不带租户（同 `get_api_key`）：`get_login_session(token_hash)`（请求只带 cookie）、`take_login_attempt(state)`（回调只带 state）、`list_identity_configs(provider)`（对账遍历租户）、`purge_expired_logins(now)`（维护任务清理过期的登录会话与尝试）；其余一律带租户。调用方拿到记录后一律从记录里取租户（Task 2） |
| `public_url` 是「平台对外地址」 | 只写源（`scheme://host[:port]`，不带路径），加载时规整成浏览器 `Origin` 的写法；主机名只收字母、数字、点、连字符或 IP 字面量，端口 1–65535，空白与 `%` 编码一律拒绝。回调地址 `<public_url>/v1/auth/feishu/callback`，不支持挂在反向代理的子路径下（Task 3） |
| cookie 写请求「带了 `Origin` 的必须等于 `public_url` 的源」 | **对设计的加固**：没配 `public_url` 时平台不认登录 cookie（按没有凭据处理，401，不清 cookie），认 cookie 时自定义头与 `Origin` 两道检查总是都生效；Task 4 启用飞书登录时强制要求配置 `public_url`。不满足返回新错误码 `403 csrf_rejected`（Task 3 修正） |
| cookie 名 `xuanpu_session` | **对设计的加固**：`identity.cookie_secure` 为真时名为 `__Host-xuanpu_session`（要求 Secure、Path=/、不带 Domain，浏览器拒收兄弟子域写的同名 cookie），为假（本地 HTTP 开发）时才是 `xuanpu_session`。请求里同名 cookie 不止一个时直接 401、不清 cookie（防止往父域塞 cookie 造成持续强制登出或会话调包）（Task 3 修正） |
| 代员工调用只说「带 `Authorization` 时」 | 用登录 cookie 带 `X-Xuanpu-Act-As` 返回 `400 invalid_request`（请求形状不对），不悄悄忽略；根凭据代调用 `403 permission_denied`。cookie 请求先查形状（代调用头、防跨站请求伪造）再验会话，所以失效 cookie 发缺自定义头的写请求得到 403 而不是 401（Task 3 修正） |
| 「只有租户管理员或平台管理员能签发带 `can_act_as_user` 的 key」 | 现有规则里签发本身就只有这两种调用者能做（成员、被代调用的成员一律 403），不另设规则；有测试守着（Task 3） |
| `GET /v1/me` 按调用者返回不同内容 | 四种调用者同一个形状：`actor`、`role`（生效角色）、`tenant_id`、`key_id`、`user_id`、`display_name`，用不上的为 `null`；代表员工时比认证多查一次员工记录（只在页面加载时调）；通过认证却查不到员工记录按内部错误 500 处理（Task 3） |
| 设计第 11 节的默认值 | 只写在 `identity/config.py` 的 `IdentityConfig` 里；运行时的 `IdentitySettings` 没有默认值，一律经 `IdentitySettings.from_config` 构造（Task 3 修正） |
| 回调「按 state 取出登录尝试」即可 | **对设计的加固**（防登录伪造：别人把自己那次登录的回调链接发给员工）：开始登录时另写登录尝试 cookie（`cookie_secure` 时 `__Host-xuanpu_login`，否则 `xuanpu_login`；HttpOnly、SameSite=Lax、Path=/、10 分钟），值是独立随机数，登录尝试里只存摘要（`LoginAttempt.cookie_hash`，迁移 0004 的 `login_attempt` 表加 `cookie_hash` 列）；回调时缺失、不符、同名不止一个都按 `state_invalid`，且不拿授权码换令牌；回调的响应总清掉它（Task 4） |
| 启用飞书登录但 `public_url` 未配置：PUT 报 400 或启动时报错，选一个 | 选 `PUT /v1/identity/feishu` 报 `400 invalid_request`；开始登录时没配同样按 `tenant_unavailable` 跳回落地页（Task 4） |
| 设计第 3 节「实现时要再对照文档确认的」三项 | 2026-10-02 对照飞书开放平台文档核对：应用身份令牌接口与参数如设计所写，令牌在响应顶层 `tenant_access_token`、`expire`（最长 2 小时）；URL 验证（未加密）是 `{"challenge", "token", "type": "url_verification"}`，加密时整体在 `encrypt` 里，要在 1 秒内回 `{"challenge": …}`；事件推送要在 3 秒内回 HTTP 200，失败按 15 秒、5 分钟、1 小时、6 小时重推，最多 4 次（Task 4 核对，Task 6、7 用） |
| 飞书接口的错误 | `FeishuError` 分三类：`not_in_app_scope`（20010）、`denied`（4xx，或 HTTP 200 而 `code` 不为 0）、`unavailable`（网络、5xx、429、重定向、响应不合预期）。登录里只有第一类是 `not_in_app_scope`，后两类都是 `identity_unavailable`（含授权码无效或过期、App Secret 不对）（Task 4） |
| `login_succeeded`、`login_failed`、`user_created` 由谁写 | 登录走完之前调用者没有 `Principal`，经 `AuditLog.login` 写：认出员工的 `actor` 是 `user:<user_id>`，否则是 `anonymous`。文档覆盖测试同时扫 `.control(` 与 `.login(`（Task 4） |
| 7.1「失败一律记 `login_failed`」 | **对设计的加固**（与 7.3 事件回调「签名不符不写审计，防止被刷」同一口径）：登录接口不需要凭据，只有飞书确实认出了一个人之后的失败才记——`not_in_app_scope`、`tenant_mismatch`、`user_disabled`（要一次真实的飞书登录，匿名伪造不出来）；开始登录阶段的失败，以及回调里的 `feishu_denied`、`state_invalid`（含登录尝试 cookie 不符）、`identity_unavailable` 只计指标、记日志。回调里不是 `access_denied` 的 `error` 记 INFO；飞书出错、App Secret 缺失、排队超时记 WARNING（Task 4 审查修正） |
| 过期的登录尝试与登录会话由谁清 | `identity/maintenance.py` 的周期任务 `IdentityMaintenance`：`Platform.start()` 经 `IdentityService.start()` 启动、`close()` 时取消并等它结束；每 5 分钟（`PURGE_INTERVAL_SECONDS`）调一次 `purge_expired_logins(now)`，一轮出错记日志、不退出。Task 7 的对账挂进同一个周期任务。`return_to` 最长 512；运维手册要求反向代理对 `/v1/auth/feishu/*` 限速（Task 4 审查修正） |
| 未认证的回调能让平台去调飞书 | `state` 不符合 `^[A-Za-z0-9_-]{43}$`（`token_urlsafe(32)` 的形状）直接 `state_invalid`、不查库；`code` 超过 512 个字符或不全是可见 ASCII 按 `identity_unavailable`（选它而不是 `state_invalid`：与「回调没有 code」同一类），不调飞书；每个租户同时在调飞书的登录最多 8 个（`identity/limits.py` 的 `TenantCallLimit`），再来的最多排队 5 秒，超时按 `identity_unavailable`（Task 4 审查修正） |
| 每次登录签发新令牌 | 另加：回调成功时，浏览器带着（恰好一个）有效的旧登录 cookie 的，它对应的旧登录会话作废（Task 4 审查修正） |
| 第 12 节「授权码不进日志」 | 平台自己的日志本来就不记；`xuanpu serve` 另给 `uvicorn.access` 装过滤器，回调请求行的查询串抹成 `?…`。反向代理的访问日志由部署方处理（运维手册）（Task 4 审查修正） |
| `session_ttl_seconds` 只有下限 | 加上限 30 天（2592000）：平台配置 `identity.session_ttl_seconds` 与 `PUT /v1/identity/feishu` 都校验（Task 4 审查修正） |
| 飞书客户端的超时与出口 | 每次请求一个总时限（`asyncio.timeout`，10 秒，含读完响应体）；响应体边读边数，超过 1 MiB 按 `unavailable`；不读环境代理，平台只直连 `feishu_open_base`（授权页由浏览器访问），写进运维手册（Task 4 审查修正） |
| 指标的结果取值 | `metrics.py` 自己写一份 `LoginResult`（`Literal`），运行时不依赖身份域；测试守着它与 `LoginFailureReason` 一致（Task 4 审查修正） |
| `identity_config_updated` 只记名字与 `app_id` | 审计按键名脱敏，会把 `app_secret_ref`、`verification_token_ref` 的取值（这里只是名字）抹掉，`data` 改为 `app_id`、`session_ttl_seconds`、`refs: {app, encrypt, verification}`（Task 4） |
| 飞书客户端的装配 | `Platform` 用 `identity_settings` 的两个基地址建 `FeishuClient`（`feishu_transport` 可注入，`close()` 时关闭），交给 `IdentityService`，登录流程放 `identity/login.py` 的 `FeishuLogin`（`IdentityService.login`）。测试夹具在平台装配后替换 `IdentityService.login.feishu` 接模拟飞书（根 `tests/conftest.py` 不加参数）（Task 4） |
| 8.2「控制面操作取调用者；内核事件与过闸记录取会话上的 `user_id`」 | **改口径**（Task 5 审查拍板）：会话里的一切记录 `user_id` 都取会话主人，包括会话上的控制面操作——`session_created`、`message_sent`、`suspension_resolved_by`、`turn_cancel_requested`、`session_closed`（`AuditLog.control(..., session=record)`，`session_id` 与 `user_id` 都由它定）；`actor` 仍是调用者。与会话无关的控制面操作照旧取调用者。为此 `GET /v1/audit` 另加 `actor=` 条件（精确匹配，审计表不为它建索引：管理员排查用的低频查询），用来查「某个管理员在别人会话里做了什么」（Task 5） |
| 7.3「先验签（第 2 步），再处理 URL 验证（第 4 步）」 | **改顺序**：飞书发来的 URL 验证是加密的、**不带签名头**（官方 SDK `lark-oapi` 的 `EventDispatcherHandler.do` 先处理 URL 验证再验签；openclaw/openclaw#58905 抓包确认），照设计做 URL 验证永远过不了。没有签名头的请求只认「能用 Encrypt Key 解开、`type` 是 `url_verification`、`token` 对」的，别的一概 401，且各步失败的响应逐字节相同（不给 CBC 填充预言机）；带签名头的先验签，签名对了之后内容不合预期才是 400。未加密的请求（含未加密的 URL 验证）一律不认，日志提示去配 Encrypt Key（Task 6）。审查修正：无签名分支兜住**任何**异常转成同一个 401（细节只进日志）；解析时拒收编码不成 UTF-8 的字符串（JSON 转义出的孤立代理项）、`challenge` 最长 256——此前带孤立代理项的 token 会在比较时抛错、被通用处理器翻成 400 并回显编码错误；外层请求体只解析一次 |
| 7.3 时间戳 | 照设计「不去重、不设时间窗」：停用幂等，重推与重放都无害；不清楚飞书重推时是否换时间戳，设窗口可能误拒 1 小时、6 小时后的重推（Task 6） |
| 7.3 未写「租户还没绑定飞书企业」 | 离职事件 200 忽略并记日志，不拿事件绑定（绑定只能由真实登录建立）；此时租户里也没有有飞书身份的员工（Task 6） |
| 7.3 未写停用原因的优先级、都为假时 | `is_resigned` > `is_exited` > `is_frozen`；都不为真按 `feishu_resigned`（事件本身就是离职）。`identity.events.reason_from_status` 留给 Task 7 对账复用（对账时都不为真就不停用）（Task 6） |
| 7.3 未写配置引用的密钥被删、请求体大小 | 密钥不在保管库 → 404（与没配置同一句提示，日志告警）；请求体上限 1 MiB，超出 `400 invalid_request`（与发布业务包超限同一错误码，不新增 413）（Task 6） |
| 7.2 `disable_user(tenant_id, user_id, reason, actor)` | `actor` 是 `Principal`；另加 `keep_an_admin`（管理员接口 True，离职事件与对账 False）。平台内部的调用者是新的「系统调用者」`tenancy.system_principal(租户, 名字)`：`Principal.system` 为名字，`actor` 为 `system:<名字>`，角色 `tenant_admin`、只在本租户、不代表员工、不经任何接口签发——飞书事件 `system:feishu-event`，停用后关会话 `system:user-disabled`，对账 `system:reconcile`（Task 7）（Task 6） |
| 8.2「与会话无关的控制面操作取调用者」 | **补一类**：针对某名员工的操作（`user_role_changed`、`user_disabled`、`user_enabled`、`user_cleanup_failed`）`user_id` 是被操作的员工，`actor` 是谁做的（`AuditLog.control(..., user_id=)`）；按 `user_id` 查一名员工能看到他何时被谁停用（Task 6） |
| 7.2 第 3 步在后台时的收尾 | 任务引用存在 `DisablePipeline` 里，结束时取走异常记日志；`Platform.close()` 在停止身份维护之后等它们最多 5 秒（排在释放会话之前，会话管理还完整），还没完的取消、记告警，之后不再接新的；兜底是主人停用的运行时规则，管理员再停用一次重跑（Task 6）。审查修正：同时在跑的最多 8 个（`BACKGROUND_CONCURRENCY`，信号量），免得批量离职占满连接池；按 `(租户, 员工)` 合并——已有任务在跑时不新建，记下「跑完再来一轮」（正在跑的那一轮可能早于新请求读过会话列表，不能算已覆盖），期间的请求并成一轮、用最新的 `actor`。管理员接口同步的第 3 步不经上限与合并 |
| 7.2 第 1 步「状态真的变了才记 `user_disabled`」的位置 | 审查修正：顺序是改状态 → 删登录会话 → 记审计（审计写不进去时停用效果已全部落下）。**已知限制**：之后重试状态已是停用，这条 `user_disabled` 不会补写，靠错误日志与 `xuanpu_audit_write_failures_total`（审计手册「完整性」）（Task 6） |
| Task 6 的测试文件 | `tests/identity/` 下：`test_identity_disable.py`、`test_disable_background.py`（后台收尾）、`test_user_admin.py`；事件回调在子目录 `tests/identity/feishu_events/`（`test_feishu_events.py` 正常流程、`test_feishu_event_errors.py` 坏请求），样本与把手放在子目录的 `conftest.py`（importlib 模式下测试模块不能互相 import），取值经上一级的 `feishu_app` 夹具拿到（审查修正） |
| 设计第 9 节 `GET /v1/users?status=&unverified=` | 另加 `role=`（`tenant_admin`/`tenant_member`）、`after=`（按 `user_id` 翻页）、`limit=`（默认 100、最大 1000），响应带 `next_after`；与 `UserQuery`、审计的翻页同一做法（Task 6） |
| 停用监听者关会话、会话被别的实例持有 | 监听者 `identity.listeners.CloseUserSessions` 经协议调 `SessionManager.close`（不 import `runtime`）。P0 的 `close` 不需要租约：先放掉本实例上的引擎，再把记录标 `closed`；别的实例持有的照样标 `closed`、立即生效（任何实例都不再接受消息与审批），持有者不受通知，正在跑的那一轮跑完、引擎按空闲回收。控制面列会话没有游标，按「反复取仍是 active 的前 N 个，关不掉的每轮多取同样多个」翻页；`close` 报错、或返回了却仍不是 `closed` 的都算关不掉（审查修正）（Task 6） |
| 用户管理放 service | 放 `identity/users.py`（`UserAdmin`），路由在 `api/users.py`、经 `api/identity.py` 挂上，`api/app.py` 不变；`PUT /v1/users/{id}/status` 的响应另带 `cleanup_failed`（失败的收尾名）（Task 6） |

---

### Task 7 实现选择

| 设计 / 计划 | 实现选择 |
| --- | --- |
| 独立周期启动描述 | 复用 `IdentityMaintenance` 同一维护循环，经 `IdentityService` 随平台启停；关闭先取消维护，再关闭停用链 |
| 长时对账租约 | 新增 `renew_reconcile`、`release_reconcile` 双存储契约；120 秒租约，每批及每用户前续期，仅未过期 owner 可续期；每步 30 秒超时，无整租户时限 |
| 失败与重试 | 失败释放租约但不更新 last_run_at；部分停用保留，后续继续查 active 身份；审计与释放同样有界，存储故障时可等待租约到期接手 |
| 应用令牌缓存 | 内存按租户保存，核对 app_id 与最新密钥摘要，提前五分钟更新；凭据及摘要不出现在审计日志 |
| 审计 / 指标 | 成败统一 identity_reconciled，查不到首次记 identity_unverified；新增 reconcile_runs/result 与 reconcile_users/outcome，无租户标签 |
| 完成审计顺序 | 先完成持久化再写成功审计；完成失败/失租仅记失败；完成后审计写失败不撤销完成时间或补写失败终态。真实 Platform.close 先停身份维护 |
| 用例位置 | `tests/identity/test_reconcile.py` 与 `test_reconcile_store.py`，不扩根夹具及认证大文件 |

## Task 1：拆分 `controlplane/postgres.py`（纯搬移）

**Files:** Create `controlplane/postgres_audit.py`；Modify `controlplane/postgres.py`、`controlplane/__init__.py`（若导出）。

- [x] 把审计（`append_audit`、`query_audit`）与用量（`add_usage`、`get_usage`、`list_usage`）的 SQL 与行转换挪到 `postgres_audit.py`（一个由 `PostgresControlPlane` 组合使用的类或一组函数，选与现有代码风格一致的写法）。对外接口与行为不变。
- [x] 契约测试 `tests/test_controlplane_contract.py` 全过即证明行为不变；`postgres.py` 降到 500 行以下。
- [x] 提交：`refactor: 控制面 Postgres 实现拆出审计与用量`

## Task 2：身份模型、存储协议、两个实现、迁移

**Files:** Create `identity/__init__.py`、`identity/models.py`、`identity/store.py`、`identity/memory.py`、`identity/postgres.py`、`controlplane/migrations/0004_identity.sql`；Modify `controlplane/models.py`（`SessionRecord.user_id`、`AuditRecord.user_id`、`AuditQuery.user_id`）、`tenancy.py`（`ApiKeyRecord.can_act_as_user`）、控制面两个实现（新字段的读写）、`controlplane/schema.py`（若迁移列表需要登记）。Test：`tests/test_identity_store_contract.py`（新建，内存与 Postgres 参数化，照 `test_controlplane_contract.py` 的夹具写法）、`tests/test_controlplane_contract.py`（新字段）。

- [x] 模型按设计 5.1：`UserRecord`、`ExternalIdentity`、`TenantIdentityConfig`、`LoginSession`、`LoginAttempt`、`UserUsageRecord`、对账状态。`user_id` 由 `ids.new_id("usr")` 生成（核对 `ids.py` 的格式，设计要求 `usr_` 加 12 位十六进制——若不一致，以现有 `new_id` 为准并在设计对齐表里记一笔）。
- [x] `IdentityStore` 协议的方法（以设计第 7 节的流程为准，至少）：用户的建、取、按外部身份取、列（按状态、是否查不到）、改（角色、状态、`unverified_since`、`last_login_at`、显示信息）；外部身份的建与改属性；租户身份配置的取与写；登录会话的建、按摘要取、删一个、删某用户全部、`touch`（`last_seen_at` 最多每 5 分钟写一次的节流由服务层做，存储只管写）；登录尝试的建与「取出即删」（原子）；按用户用量的累加与按日期区间列；对账租约的 `claim_reconcile(tenant_id, owner, now, interval, lease_seconds) -> bool` 与 `finish_reconcile`。全部按租户隔离（规则 3）。
- [x] 迁移 `0004_identity.sql`：新表（租户前缀照现有迁移写法）；`sessions` 表加 `user_id`、`audit` 表加 `user_id` 并建索引；`api_keys` 表加 `can_act_as_user BOOLEAN NOT NULL DEFAULT FALSE`；`user_usage` 表。`xuanpu migrate` 能把 0003 的库升到 0004（补测试）。
- [x] 契约测试覆盖每个方法，包括：登录尝试只能取出一次（并发两次取，只有一个拿到）；对账租约两个 owner 并发认领只有一个成功、过期后可被别人认领、`interval` 内不重复认领；跨租户取不到。
- [x] 提交：`feat: 身份域的模型、存储协议与内存、Postgres 实现`

## Task 3：调用者身份与认证入口

**Files:** Create `identity/cookies.py`、`identity/service.py`（先放解析登录会话与代调用这两块）；Modify `tenancy.py`（`Principal`）、`platform.py`（`authenticate` 扩展或新增 `authenticate_request`）、`api/app.py`（`caller` 依赖）、`config.py`（`public_url`、`identity` 段）、`api/schemas.py`（签发 key 加 `act_as_users`）。Test：`tests/test_identity_auth.py`（新建）。

- [x] `Principal` 加 `user_id: str | None = None`，`key_id` 改为 `str | None`；`actor` 按设计 5.2 的表（`key:root`、`key:<id>`、`user:<user_id>`、`key:<id>/user:<user_id>`）。全仓 grep `key_id`、`actor` 的用法，保证现有行为不变（现有测试全过）。
- [x] 认证顺序按设计 6.1：有 `Authorization` 走 key/根凭据，另带 `X-Xuanpu-Act-As` 走代调用（6.3：key 须 `can_act_as_user`、用户同租户且 `active`，否则 403；生效角色取 key 与用户的较低者）；否则有 `xuanpu_session` cookie 走登录会话（6.2：每个请求查库——会话存在未过期、用户 `active`、租户 `active`，任一不满足 401 并清 cookie；不缓存）；都没有 401。两种失败对外同一个 401。
- [x] 防跨站请求伪造（6.2）：cookie 认证的非 GET/HEAD/OPTIONS 请求必须带 `X-Requested-By: xuanpu`；带了 `Origin` 的必须等于 `public_url` 的源；不满足 403（错误码按 errors.py 现有的选或新增，写进 http-api.md）。
- [x] 签发 key：`POST /v1/admin/tenants/{id}/keys` 与 `POST /v1/keys` 加 `act_as_users: bool = false`；只有租户管理员或平台管理员能签发带它的 key（成员签发报 403——核对现有签发规则：成员本来能不能签？按现有规则加限制）。
- [x] `GET /v1/me`：返回当前调用者（用户：`user_id`、显示名、角色、租户；key：`key_id`、角色、租户；代调用：两者都有）。
- [x] 配置：`public_url: str | None`（启用飞书登录时必填——在 Task 4 的配置校验里检查）；`identity` 段按设计第 11 节（`session_ttl_seconds`、`cookie_secure`、`reconcile_interval_seconds`、`default_tenant`、`login_landing_path`、`feishu_open_base`、`feishu_accounts_base`）。operations.md 配置表与 `examples/xuanpu.yaml` 同步。
- [x] 测试：三种调用者各自的 `actor` 与审计；cookie 有效/过期/用户停用/租户停用；写请求缺自定义头、`Origin` 不符被拒；代调用的四种拒绝（key 没能力、用户别的租户、用户停用、不带 key）与角色取较低者；`GET /v1/me` 三种形态。登录会话在测试里直接经存储建（登录流程是 Task 4）。
- [x] 提交：`feat: 调用者身份带上用户——登录 cookie、系统代员工调用、防跨站请求伪造`

## Task 4：飞书客户端与登录流程

**Files:** Create `identity/feishu.py`、`identity/login.py`（或并入 service——注意行数）、`api/identity.py`；Modify `api/app.py`（挂路由）、`platform.py`/`bootstrap.py`（装配 `IdentityService`）。Test：`tests/test_feishu_client.py`、`tests/test_identity_login.py`（新建）。

- [x] `FeishuClient(open_base, accounts_base, transport=None, timeout)`：拼授权地址（PKCE S256、`state`、不申请额外 scope）、换令牌（`POST /open-apis/authen/v2/oauth/token`，JSON 体）、取用户信息（`GET /open-apis/authen/v1/user_info`）、取应用身份令牌（`POST /open-apis/auth/v3/tenant_access_token/internal`）、批量查员工（`GET /open-apis/contact/v3/users/batch`，`user_id_type=union_id`，每批 ≤ 50）。错误分两层（HTTP 状态与响应体 `code`），20010 映射为「不在可用范围」。令牌与授权码不进日志与异常文本。设计第 3 节「实现时要再对照文档确认的」三项：实现时查飞书开放平台文档核对，在代码注释与报告里写明核对结果。
- [x] 登录流程按设计 7.1（9 步与 7 种失败原因）：`GET /v1/auth/feishu/login`、`GET /v1/auth/feishu/callback`、`POST /v1/auth/logout`。`return_to` 只收站内路径；`tenant` 省略时用 `identity.default_tenant`；失败一律 302 落地页带 `?login_error=`，记 `login_failed`；成功记 `login_succeeded`，首次登录记 `user_created`；每次登录签发新令牌；cookie 属性按 6.2。
- [x] 租户身份配置：`GET /v1/identity/feishu`（不含明文；附回调地址与事件地址）、`PUT /v1/identity/feishu`（引用的三个密钥必须已存在，否则 400；记 `identity_config_updated`）。启用飞书登录但 `public_url` 未配置时：PUT 报 400（或启动时报配置错误——选一个，写清楚）。
- [x] 测试：飞书客户端用 `httpx.MockTransport`（成功、20010、令牌接口错误码、网络错误）；登录全流程用模拟飞书服务端（首次登录建账号、再次登录刷新信息、7 种失败原因各一条、`return_to` 开放跳转被拒、state 只能用一次、tenant_key 首登绑定后不一致被拒）；登出删会话清 cookie。
- [x] 指标：`xuanpu_logins_total{result}`。
- [x] 提交：`feat: 飞书登录——授权、回调、登出与租户的飞书配置`

## Task 5：会话归属、可见性、审计与用量带上用户

**Files:** Create `runtime/ownership.py`（可见性与主人状态检查）；Modify `runtime/sessions.py`（只加调用）、`runtime/events.py`（`user_id` 写进引擎事件审计；用量同时写按用户的表）、`runtime/events.py` 的 `SubmissionIndex`（登记会话主人）、`audit.py`（`control` 取 `principal.user_id`；`gate` 取会话主人）、`api/app.py`（`GET /v1/audit?user_id=`、`GET /v1/usage/users`）。Test：`tests/test_session_ownership.py`、`tests/test_user_usage.py`（新建）。

- [x] 建会话：调用者有 `user_id` 的写进 `SessionRecord.user_id`。
- [x] 可见性按设计 8.1：普通成员（登录或被代调用）只看自己的会话——列会话强制只列自己的，取别人的会话 404（取、关、发消息、送审批、停止、回放、待办，全部经同一个检查）；管理员（登录）看本租户全部；不带代调用的 API key 看本租户全部（不变）。`mine=true` 对用户按 `user_id`、对 key 按创建者。
- [x] 运行时规则（设计 7.2 末段）：会话主人已停用时，任何调用者都不能再发消息、送审批、停止（停止可以放行？——停止只会让会话更快停下，放行；在 `ownership.py` 的 docstring 里写明），返回 403。
- [x] 审计 `user_id`：控制面操作取调用者；内核事件、过闸记录、人工拒绝补记、停止生成、估算入账取会话主人。`AuditQuery.user_id`；`GET /v1/audit?user_id=`（仍只对管理员开放）。
- [x] 用量：会话有 `user_id` 的同时累加按用户的表（含 P0 的估算入账）；`GET /v1/usage/users?since=&until=`（租户管理员）。
- [x] 测试：成员只看到自己的会话（列、取、发消息、回放、待办、停止都被挡）；管理员与 key 看全部；代调用以员工身份可见范围生效；主人停用后发消息 403；各类审计记录带对 `user_id`（含过闸与补记）；按用户用量与租户总量一致。
- [x] 提交：`feat: 会话归属与可见性；审计与用量带上用户`

## Task 6：停用流水线、用户管理、离职事件回调

**Files:** Create `identity/disable.py`（停用流水线与监听者登记）、`identity/listeners.py`（P1 的监听者：关闭他名下所有 active 会话）、`identity/events.py`（验签、解密、URL 验证、解析离职事件——纯计算）；Modify `identity/service.py`、`api/identity.py`、`bootstrap.py`/`platform.py`（注册监听者）。Test：`tests/test_identity_disable.py`、`tests/test_feishu_events.py`（新建）。

- [x] 停用流水线按设计 7.2：`disable_user(tenant_id, user_id, reason, actor)` 幂等；状态真的变了才记 `user_disabled`；删全部登录会话；依次通知监听者，失败记 `user_cleanup_failed`、不影响其余；再次停用重跑全部监听者。重新启用只改状态、记 `user_enabled`。
- [x] 用户管理接口：`GET /v1/users?status=&unverified=`、`GET /v1/users/{id}`（管理员或本人）、`PUT /v1/users/{id}/role`（记 `user_role_changed`；不能把自己降级到没有管理员——若租户只剩这一个管理员，拒绝并说明原因）、`PUT /v1/users/{id}/status`（停用走流水线，原因 `admin`）。
- [x] 事件回调按设计 7.3：`POST /v1/identity/feishu/events/{tenant_id}`；签名不符 401（只写日志不写审计）；解密；URL 验证；核对 token、`app_id`、`tenant_key`；`contact.user.deleted_v3` → 同步执行停用第 1、2 步，第 3 步（监听者）放后台任务（任务引用保存、异常取走记日志）；其他事件 200 忽略；3 秒内响应。
- [x] 测试：流水线三步、幂等、监听者失败留审计、重跑；用户管理的权限与最后一个管理员保护；事件回调：URL 验证、签名不符、token/`app_id`/`tenant_key` 不符、离职事件导致停用且会话被关、未知事件忽略；测试里按设计第 3 节的算法用 `cryptography` 加密出样本（含填充错误、长度错误的坏样本）。
- [x] 指标：`xuanpu_users_disabled_total{reason}`。
- [x] 提交：`feat: 员工停用流水线、用户管理与飞书离职事件回调`

## Task 7：每日对账

**Files:** Create `identity/reconcile.py`；Modify `platform.py`（启动、停止周期任务）、`api/identity.py`（`POST /v1/identity/feishu/reconcile`）。Test：`tests/test_identity_reconcile.py`（新建）。

- [x] 按设计 7.4：周期任务遍历配置了飞书的租户，`claim_reconcile` 认领；应用身份令牌在内存缓存到过期前 5 分钟；分页取 `active` 且有飞书身份的用户，每 50 人批量查；离职/暂停/已退出按原因停用；查不到的设 `unverified_since`、记 `identity_unverified`（不停用）；再次查到清标记；完成记 `identity_reconciled`（检查、停用、查不到的人数）；飞书报错中止本次、记 `identity_reconciled` 注明失败。手工触发接口不受周期限制，但仍要认领租约。
- [x] 周期任务的异常不能让循环退出（记日志、下个周期再试）；`Platform.close()` 时取消并等待它结束。
- [x] 测试：三种状态被停用；查不到只标记；再次查到清标记；两个实例只有一个认领成功；飞书报错中止且已停用的保持；手工触发。
- [x] 提交：`feat: 每日对账——按飞书通讯录停用离职员工`

## Task 8：控制台

**Files:** Modify `xuanpu-console/src/xuanpu_console/static/{index.html,app.js,style.css}`；Test：`xuanpu-console/tests/test_static.py`。

- [x] 按设计第 10 节：登录页保留粘贴 API key，同源时另显示「用飞书登录」（`?tenant=` 或默认租户）；按 `login_error` 显示原因；cookie 模式（请求不带 `Authorization`，写请求带 `X-Requested-By: xuanpu`）；页面显示当前员工与退出；「用户」页（仅管理员）：姓名、角色、状态、最近登录、查不到标记；设为/取消管理员、停用、启用。
- [x] 静态测试覆盖新页面与登录按钮；用 Playwright 在真实浏览器里走一遍（本地起平台与模拟飞书服务端，后台起、用完杀掉）：飞书登录 → 看到自己的会话 → 退出；管理员在用户页停用一名员工。
- [x] 提交：`feat(console): 飞书登录与用户页`

## Task 9：文档

- [x] 按设计第 15 节：http-api.md（新接口、两种认证方式、代员工调用、可见性规则、新错误原因——前面任务已补的核对一遍）；架构总览新增「身份」一节、租户隔离表加用户、已知限制（对账查不到不自动停用、事件回调要求公网可达、管理员仍可看全部会话正文——规则 7 要在 P6 前收紧）；新手册 `docs/guides/feishu-login.md`「接入飞书登录」（建自建应用、重定向与事件地址、权限、可用范围与通讯录权限范围、Encrypt Key；写密钥、调配置接口、指定第一个管理员；**实机核对清单**——设计第 14 节的 4 项）；operations.md（配置、对账、停用流水线）；audit-and-usage.md（新审计类型、按用户用量）；integrating.md（代员工调用）；console README。命令与输出要实际跑过（用模拟飞书服务端）。
- [x] 设计文档状态改为「已实现（feat/p1-identity）」，列出与设计不同的地方。
- [x] 提交：`feat(sdk): 补齐用户身份并完成 P1 接入文档`（本文所在提交）。

- [x] Python SDK 身份补齐与独立文件测试；先红 8 failed，后 SDK/真实 API 定向 20 passed。
- [x] 真实 CLI + 模拟飞书手册验收；配置、首位管理员、代调用、对账、加密 URL 验证、签名离职事件、回调日志；测试服务已回收。
- [x] Task 9 SDK 与文档独立审查通过；主代理最终四门禁通过：pytest 1130 passed in 57.66s，Ruff 全绿，mypy 90 源文件无问题，锁文件检查 102 packages。

Task 7 提交 `3dc24fb`，Task 8 提交 `862c73f`；Task 9 随本文所在提交完成，最终结果见[进度记录](2026-10-02-p1-progress.md)。真实飞书五项核对保持未完成，见接入手册；本轮未推送、未发布、未合 main。
