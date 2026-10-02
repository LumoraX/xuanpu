# P1 用户身份 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

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
| 维护循环 | 对账放在 `identity/reconcile.py` 的独立周期任务，由 `Platform.start()` 启动、`close()` 停止；不塞进 `SessionManager.maintain` |
| 指标 | 新增 `xuanpu_logins_total{result}`（`succeeded` 或失败原因）与 `xuanpu_users_disabled_total{reason}`；不带租户标签 |

---

## Task 1：拆分 `controlplane/postgres.py`（纯搬移）

**Files:** Create `controlplane/postgres_audit.py`；Modify `controlplane/postgres.py`、`controlplane/__init__.py`（若导出）。

- [ ] 把审计（`append_audit`、`query_audit`）与用量（`add_usage`、`get_usage`、`list_usage`）的 SQL 与行转换挪到 `postgres_audit.py`（一个由 `PostgresControlPlane` 组合使用的类或一组函数，选与现有代码风格一致的写法）。对外接口与行为不变。
- [ ] 契约测试 `tests/test_controlplane_contract.py` 全过即证明行为不变；`postgres.py` 降到 500 行以下。
- [ ] 提交：`refactor: 控制面 Postgres 实现拆出审计与用量`

## Task 2：身份模型、存储协议、两个实现、迁移

**Files:** Create `identity/__init__.py`、`identity/models.py`、`identity/store.py`、`identity/memory.py`、`identity/postgres.py`、`controlplane/migrations/0004_identity.sql`；Modify `controlplane/models.py`（`SessionRecord.user_id`、`AuditRecord.user_id`、`AuditQuery.user_id`）、`tenancy.py`（`ApiKeyRecord.can_act_as_user`）、控制面两个实现（新字段的读写）、`controlplane/schema.py`（若迁移列表需要登记）。Test：`tests/test_identity_store_contract.py`（新建，内存与 Postgres 参数化，照 `test_controlplane_contract.py` 的夹具写法）、`tests/test_controlplane_contract.py`（新字段）。

- [ ] 模型按设计 5.1：`UserRecord`、`ExternalIdentity`、`TenantIdentityConfig`、`LoginSession`、`LoginAttempt`、`UserUsageRecord`、对账状态。`user_id` 由 `ids.new_id("usr")` 生成（核对 `ids.py` 的格式，设计要求 `usr_` 加 12 位十六进制——若不一致，以现有 `new_id` 为准并在设计对齐表里记一笔）。
- [ ] `IdentityStore` 协议的方法（以设计第 7 节的流程为准，至少）：用户的建、取、按外部身份取、列（按状态、是否查不到）、改（角色、状态、`unverified_since`、`last_login_at`、显示信息）；外部身份的建与改属性；租户身份配置的取与写；登录会话的建、按摘要取、删一个、删某用户全部、`touch`（`last_seen_at` 最多每 5 分钟写一次的节流由服务层做，存储只管写）；登录尝试的建与「取出即删」（原子）；按用户用量的累加与按日期区间列；对账租约的 `claim_reconcile(tenant_id, owner, now, interval, lease_seconds) -> bool` 与 `finish_reconcile`。全部按租户隔离（规则 3）。
- [ ] 迁移 `0004_identity.sql`：新表（租户前缀照现有迁移写法）；`sessions` 表加 `user_id`、`audit` 表加 `user_id` 并建索引；`api_keys` 表加 `can_act_as_user BOOLEAN NOT NULL DEFAULT FALSE`；`user_usage` 表。`xuanpu migrate` 能把 0003 的库升到 0004（补测试）。
- [ ] 契约测试覆盖每个方法，包括：登录尝试只能取出一次（并发两次取，只有一个拿到）；对账租约两个 owner 并发认领只有一个成功、过期后可被别人认领、`interval` 内不重复认领；跨租户取不到。
- [ ] 提交：`feat: 身份域的模型、存储协议与内存、Postgres 实现`

## Task 3：调用者身份与认证入口

**Files:** Create `identity/cookies.py`、`identity/service.py`（先放解析登录会话与代调用这两块）；Modify `tenancy.py`（`Principal`）、`platform.py`（`authenticate` 扩展或新增 `authenticate_request`）、`api/app.py`（`caller` 依赖）、`config.py`（`public_url`、`identity` 段）、`api/schemas.py`（签发 key 加 `act_as_users`）。Test：`tests/test_identity_auth.py`（新建）。

- [ ] `Principal` 加 `user_id: str | None = None`，`key_id` 改为 `str | None`；`actor` 按设计 5.2 的表（`key:root`、`key:<id>`、`user:<user_id>`、`key:<id>/user:<user_id>`）。全仓 grep `key_id`、`actor` 的用法，保证现有行为不变（现有测试全过）。
- [ ] 认证顺序按设计 6.1：有 `Authorization` 走 key/根凭据，另带 `X-Xuanpu-Act-As` 走代调用（6.3：key 须 `can_act_as_user`、用户同租户且 `active`，否则 403；生效角色取 key 与用户的较低者）；否则有 `xuanpu_session` cookie 走登录会话（6.2：每个请求查库——会话存在未过期、用户 `active`、租户 `active`，任一不满足 401 并清 cookie；不缓存）；都没有 401。两种失败对外同一个 401。
- [ ] 防跨站请求伪造（6.2）：cookie 认证的非 GET/HEAD/OPTIONS 请求必须带 `X-Requested-By: xuanpu`；带了 `Origin` 的必须等于 `public_url` 的源；不满足 403（错误码按 errors.py 现有的选或新增，写进 http-api.md）。
- [ ] 签发 key：`POST /v1/admin/tenants/{id}/keys` 与 `POST /v1/keys` 加 `act_as_users: bool = false`；只有租户管理员或平台管理员能签发带它的 key（成员签发报 403——核对现有签发规则：成员本来能不能签？按现有规则加限制）。
- [ ] `GET /v1/me`：返回当前调用者（用户：`user_id`、显示名、角色、租户；key：`key_id`、角色、租户；代调用：两者都有）。
- [ ] 配置：`public_url: str | None`（启用飞书登录时必填——在 Task 4 的配置校验里检查）；`identity` 段按设计第 11 节（`session_ttl_seconds`、`cookie_secure`、`reconcile_interval_seconds`、`default_tenant`、`login_landing_path`、`feishu_open_base`、`feishu_accounts_base`）。operations.md 配置表与 `examples/xuanpu.yaml` 同步。
- [ ] 测试：三种调用者各自的 `actor` 与审计；cookie 有效/过期/用户停用/租户停用；写请求缺自定义头、`Origin` 不符被拒；代调用的四种拒绝（key 没能力、用户别的租户、用户停用、不带 key）与角色取较低者；`GET /v1/me` 三种形态。登录会话在测试里直接经存储建（登录流程是 Task 4）。
- [ ] 提交：`feat: 调用者身份带上用户——登录 cookie、系统代员工调用、防跨站请求伪造`

## Task 4：飞书客户端与登录流程

**Files:** Create `identity/feishu.py`、`identity/login.py`（或并入 service——注意行数）、`api/identity.py`；Modify `api/app.py`（挂路由）、`platform.py`/`bootstrap.py`（装配 `IdentityService`）。Test：`tests/test_feishu_client.py`、`tests/test_identity_login.py`（新建）。

- [ ] `FeishuClient(open_base, accounts_base, transport=None, timeout)`：拼授权地址（PKCE S256、`state`、不申请额外 scope）、换令牌（`POST /open-apis/authen/v2/oauth/token`，JSON 体）、取用户信息（`GET /open-apis/authen/v1/user_info`）、取应用身份令牌（`POST /open-apis/auth/v3/tenant_access_token/internal`）、批量查员工（`GET /open-apis/contact/v3/users/batch`，`user_id_type=union_id`，每批 ≤ 50）。错误分两层（HTTP 状态与响应体 `code`），20010 映射为「不在可用范围」。令牌与授权码不进日志与异常文本。设计第 3 节「实现时要再对照文档确认的」三项：实现时查飞书开放平台文档核对，在代码注释与报告里写明核对结果。
- [ ] 登录流程按设计 7.1（9 步与 7 种失败原因）：`GET /v1/auth/feishu/login`、`GET /v1/auth/feishu/callback`、`POST /v1/auth/logout`。`return_to` 只收站内路径；`tenant` 省略时用 `identity.default_tenant`；失败一律 302 落地页带 `?login_error=`，记 `login_failed`；成功记 `login_succeeded`，首次登录记 `user_created`；每次登录签发新令牌；cookie 属性按 6.2。
- [ ] 租户身份配置：`GET /v1/identity/feishu`（不含明文；附回调地址与事件地址）、`PUT /v1/identity/feishu`（引用的三个密钥必须已存在，否则 400；记 `identity_config_updated`）。启用飞书登录但 `public_url` 未配置时：PUT 报 400（或启动时报配置错误——选一个，写清楚）。
- [ ] 测试：飞书客户端用 `httpx.MockTransport`（成功、20010、令牌接口错误码、网络错误）；登录全流程用模拟飞书服务端（首次登录建账号、再次登录刷新信息、7 种失败原因各一条、`return_to` 开放跳转被拒、state 只能用一次、tenant_key 首登绑定后不一致被拒）；登出删会话清 cookie。
- [ ] 指标：`xuanpu_logins_total{result}`。
- [ ] 提交：`feat: 飞书登录——授权、回调、登出与租户的飞书配置`

## Task 5：会话归属、可见性、审计与用量带上用户

**Files:** Create `runtime/ownership.py`（可见性与主人状态检查）；Modify `runtime/sessions.py`（只加调用）、`runtime/events.py`（`user_id` 写进引擎事件审计；用量同时写按用户的表）、`runtime/events.py` 的 `SubmissionIndex`（登记会话主人）、`audit.py`（`control` 取 `principal.user_id`；`gate` 取会话主人）、`api/app.py`（`GET /v1/audit?user_id=`、`GET /v1/usage/users`）。Test：`tests/test_session_ownership.py`、`tests/test_user_usage.py`（新建）。

- [ ] 建会话：调用者有 `user_id` 的写进 `SessionRecord.user_id`。
- [ ] 可见性按设计 8.1：普通成员（登录或被代调用）只看自己的会话——列会话强制只列自己的，取别人的会话 404（取、关、发消息、送审批、停止、回放、待办，全部经同一个检查）；管理员（登录）看本租户全部；不带代调用的 API key 看本租户全部（不变）。`mine=true` 对用户按 `user_id`、对 key 按创建者。
- [ ] 运行时规则（设计 7.2 末段）：会话主人已停用时，任何调用者都不能再发消息、送审批、停止（停止可以放行？——停止只会让会话更快停下，放行；在 `ownership.py` 的 docstring 里写明），返回 403。
- [ ] 审计 `user_id`：控制面操作取调用者；内核事件、过闸记录、人工拒绝补记、停止生成、估算入账取会话主人。`AuditQuery.user_id`；`GET /v1/audit?user_id=`（仍只对管理员开放）。
- [ ] 用量：会话有 `user_id` 的同时累加按用户的表（含 P0 的估算入账）；`GET /v1/usage/users?since=&until=`（租户管理员）。
- [ ] 测试：成员只看到自己的会话（列、取、发消息、回放、待办、停止都被挡）；管理员与 key 看全部；代调用以员工身份可见范围生效；主人停用后发消息 403；各类审计记录带对 `user_id`（含过闸与补记）；按用户用量与租户总量一致。
- [ ] 提交：`feat: 会话归属与可见性；审计与用量带上用户`

## Task 6：停用流水线、用户管理、离职事件回调

**Files:** Create `identity/disable.py`（停用流水线与监听者登记）、`identity/listeners.py`（P1 的监听者：关闭他名下所有 active 会话）、`identity/events.py`（验签、解密、URL 验证、解析离职事件——纯计算）；Modify `identity/service.py`、`api/identity.py`、`bootstrap.py`/`platform.py`（注册监听者）。Test：`tests/test_identity_disable.py`、`tests/test_feishu_events.py`（新建）。

- [ ] 停用流水线按设计 7.2：`disable_user(tenant_id, user_id, reason, actor)` 幂等；状态真的变了才记 `user_disabled`；删全部登录会话；依次通知监听者，失败记 `user_cleanup_failed`、不影响其余；再次停用重跑全部监听者。重新启用只改状态、记 `user_enabled`。
- [ ] 用户管理接口：`GET /v1/users?status=&unverified=`、`GET /v1/users/{id}`（管理员或本人）、`PUT /v1/users/{id}/role`（记 `user_role_changed`；不能把自己降级到没有管理员——若租户只剩这一个管理员，拒绝并说明原因）、`PUT /v1/users/{id}/status`（停用走流水线，原因 `admin`）。
- [ ] 事件回调按设计 7.3：`POST /v1/identity/feishu/events/{tenant_id}`；签名不符 401（只写日志不写审计）；解密；URL 验证；核对 token、`app_id`、`tenant_key`；`contact.user.deleted_v3` → 同步执行停用第 1、2 步，第 3 步（监听者）放后台任务（任务引用保存、异常取走记日志）；其他事件 200 忽略；3 秒内响应。
- [ ] 测试：流水线三步、幂等、监听者失败留审计、重跑；用户管理的权限与最后一个管理员保护；事件回调：URL 验证、签名不符、token/`app_id`/`tenant_key` 不符、离职事件导致停用且会话被关、未知事件忽略；测试里按设计第 3 节的算法用 `cryptography` 加密出样本（含填充错误、长度错误的坏样本）。
- [ ] 指标：`xuanpu_users_disabled_total{reason}`。
- [ ] 提交：`feat: 员工停用流水线、用户管理与飞书离职事件回调`

## Task 7：每日对账

**Files:** Create `identity/reconcile.py`；Modify `platform.py`（启动、停止周期任务）、`api/identity.py`（`POST /v1/identity/feishu/reconcile`）。Test：`tests/test_identity_reconcile.py`（新建）。

- [ ] 按设计 7.4：周期任务遍历配置了飞书的租户，`claim_reconcile` 认领；应用身份令牌在内存缓存到过期前 5 分钟；分页取 `active` 且有飞书身份的用户，每 50 人批量查；离职/暂停/已退出按原因停用；查不到的设 `unverified_since`、记 `identity_unverified`（不停用）；再次查到清标记；完成记 `identity_reconciled`（检查、停用、查不到的人数）；飞书报错中止本次、记 `identity_reconciled` 注明失败。手工触发接口不受周期限制，但仍要认领租约。
- [ ] 周期任务的异常不能让循环退出（记日志、下个周期再试）；`Platform.close()` 时取消并等待它结束。
- [ ] 测试：三种状态被停用；查不到只标记；再次查到清标记；两个实例只有一个认领成功；飞书报错中止且已停用的保持；手工触发。
- [ ] 提交：`feat: 每日对账——按飞书通讯录停用离职员工`

## Task 8：控制台

**Files:** Modify `xuanpu-console/src/xuanpu_console/static/{index.html,app.js,style.css}`；Test：`xuanpu-console/tests/test_static.py`。

- [ ] 按设计第 10 节：登录页保留粘贴 API key，同源时另显示「用飞书登录」（`?tenant=` 或默认租户）；按 `login_error` 显示原因；cookie 模式（请求不带 `Authorization`，写请求带 `X-Requested-By: xuanpu`）；页面显示当前员工与退出；「用户」页（仅管理员）：姓名、角色、状态、最近登录、查不到标记；设为/取消管理员、停用、启用。
- [ ] 静态测试覆盖新页面与登录按钮；用 Playwright 在真实浏览器里走一遍（本地起平台与模拟飞书服务端，后台起、用完杀掉）：飞书登录 → 看到自己的会话 → 退出；管理员在用户页停用一名员工。
- [ ] 提交：`feat(console): 飞书登录与用户页`

## Task 9：文档

- [ ] 按设计第 15 节：http-api.md（新接口、两种认证方式、代员工调用、可见性规则、新错误原因——前面任务已补的核对一遍）；架构总览新增「身份」一节、租户隔离表加用户、已知限制（对账查不到不自动停用、事件回调要求公网可达、管理员仍可看全部会话正文——规则 7 要在 P6 前收紧）；新手册 `docs/guides/feishu-login.md`「接入飞书登录」（建自建应用、重定向与事件地址、权限、可用范围与通讯录权限范围、Encrypt Key；写密钥、调配置接口、指定第一个管理员；**实机核对清单**——设计第 14 节的 4 项）；operations.md（配置、对账、停用流水线）；audit-and-usage.md（新审计类型、按用户用量）；integrating.md（代员工调用）；console README。命令与输出要实际跑过（用模拟飞书服务端）。
- [ ] 设计文档状态改为「已实现（feat/p1-identity）」，列出与设计不同的地方。
- [ ] 提交：`docs: P1 用户身份——接入飞书登录手册、接口与运维文档`
