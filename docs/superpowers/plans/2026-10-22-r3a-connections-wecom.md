# R3-A 连接授权与企业微信只读 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans逐项执行。Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 员工以稳定玄圃身份进入企业微信只读连接，明确委托、查询本人资料、撤销后立即阻止新读取；管理员可预览实际可见目录。

**Architecture:** installation、外部身份、员工consent与应用凭据分别持久化；HTTP沿用现有逐请求取密钥和禁止重定向机制，增加按会话解析的CredentialBroker。复用R2真实执行和失效检查，不注入长期token到工具schema、脚本环境或内核状态。

**Tech Stack:** 现有identity、httpx、cryptography AES-GCM/密钥轮换、PG/memory、FastAPI、SDK/CLI、原生JS。

前置：[R3设计](../specs/2026-10-22-r3-enterprise-collaboration-design.md)、[总计划](2026-10-22-r3-delivery.md)。计划命令与Expected是未来验证目标，本轮未执行这些新功能测试。

## A0：冻结供应商契约及公共测试基础

Files：Create `docs/superpowers/specs/2026-10-22-r3a-wecom-contract.md`、`packages/xuanpu-server/tests/r3_contract/conftest.py`、`test_provider_contract.py`、`data/wecom_fixture_server.py`；Review `identity/feishu.py`、`identity/login.py`、`runtime/http_api.py`及`secrets.py`。

- [ ] 列出当前企业微信自建应用的官方文档、核对日期、应用类型、可信域名/IP、CorpID/AgentID、实际授权模式、允许endpoint/字段、错误码、token有效期和范围证据。secret仅给私有文件路径。未能读现行官方资料或真实后台时状态为unverified，不以历史SDK代替现行权限。
- [ ] 冻结第一批operation：`self_profile.read`、`directory_preview.read`。候选接口为gettoken、code身份、成员读取和部门/成员分页；只有核对通过的接口才进入allowlist。不能顺手接入消息、审批修改、会话存档或完整文档读取。
- [ ] 定义[公共fixture](2026-10-22-r3-delivery.md#4-公共测试脚手架a0定义后续按包扩展seed)，两企业、三员工、两个同名员工、只对一名员工可见的应用。测试HTTP返回可控，但真实集成栏保持not_checked。
- [ ] 写契约反例：应用凭据不是个人refresh_token、外企业code不可绑定、provider缺字段不补造；文档核定后为每项provider能力生成`available/unsupported/unverified`能力表。
- [ ] 跑`uv run pytest packages/xuanpu-server/tests/r3_contract/test_provider_contract.py -q`，记录先红后绿；提交`test: 固定企业微信只读契约与R3测试基座`。

退出标准：真实验收前提和受支持接口可逐项核对；不满足条件只阻塞真实集成签收，mock不得掩盖该缺口。

## A1：连接、委托与有范围的凭据存储

Files：Create `connections/{models,store,memory,postgres,service,package_bindings,credential_vault}.py`、`controlplane/migrations/0015_connection_identity.sql`、`api/connections.py`；Modify `controlplane/{store,memory,postgres}.py`、`config.py`、`bootstrap.py`、`platform.py`；Test `tests/r3_contract/test_connection_store.py`、`test_consent.py`、`test_credential_vault.py`。

- [ ] 写下面的API反例及双后端事务测试，确认安装范围外同意、过期consent、CAS冲突、撤销重放、明文读出均失败。
```python
async def test_member_cannot_borrow_connection(r3_http, r3_ids, r3_now):
    cid = r3_ids['installation']
    response = await r3_http['alice'].post(f'/v1/me/connections/{cid}/consents', json={
        'request_id': 'consent-self-1', 'operations': ['self_profile.read'],
        'expires_at': r3_now + 3600.0, 'purpose': '本人资料核对'})
    assert response.status_code == 201
    response = await r3_http['bob'].get(f'/v1/me/connections/{cid}/consents/' + response.json()['consent_id'])
    assert response.status_code == 404
    assert (await r3_http['machine'].get('/v1/me/connections')).status_code == 403
```
- [ ] 实现Installation状态disabled/active，EmployeeConsent状态active/revoked/expired，均有版本/epoch及稳定请求回执；默认同意上限24小时，可由管理员调低，不能永久同意。授权页展示实际app/data/operation范围及到期，scope扩大必须新同意。创建/扩大consent要求已登录员工本人浏览器会话，机器key和act-as不得冒充本人的同意；后台仅消费已持久化的有效委托。
- [ ] 应用secret复用现有SecretVault引用；派生token及未来员工credential放独立内部加密表，AAD绑定tenant/installation/subject/kind/revision，复用现有密钥配置与轮换组件。普通`/v1/secrets`不能读写内部credential记录；不另发明加密协议。
- [ ] 管理端额外登记ConnectionBinding：installation、不可变package digest、connector_name、operation allowlist、固定endpoint/HTTP method、server_bound_parameters和effect=read。仅对已验证包启用，包升级需重新确认；同一operation不能同时启用旧静态auth与员工managed auth。无需先改manifest格式来塞安装ID或凭据。
- [ ] 安装管理走`/v1/admin/connections`，员工走`/v1/me/connections`；查询只返回状态、自然名称、允许用途和有效期。删除/撤销保留tombstone，不暴露token或原始供应商响应。
- [ ] 跑三项测试及既有`test_redaction.py/test_content_access.py`，检查0015及旧config默认兼容；提交`feat: 增加员工连接委托与范围凭据存储`。

## A2：真实身份适配、绑定与登录

Files：Create `identity/{wecom,wecom_login,wecom_config}.py`、`connections/account_bindings.py`、`tests/identity/test_wecom_login.py`、`tests/r3_contract/test_external_binding.py`；Modify `identity/{models,service,store,memory,postgres}.py`、`api/identity.py`及控制台登录提示。

- [ ] 写错误企业、同名合并、重复code、过期state、跨浏览器state、回调URL注入、绑定抢占及管理员提权反例；先验证失败。
- [ ] 在ExternalIdentity中支持wecom；原TenantIdentityConfig仍只服务Feishu，WeCom使用独立配置并引用installation。稳定映射为tenant/provider/外部企业/外部成员→usr，复用现有唯一索引语义，不产生第二套玄圃身份。
- [ ] 增加`/v1/auth/wecom/start|callback`及本人连接绑定入口。state随机、300秒有效、一次消费并绑定发起浏览器和固定站内返回路径；只有供应商实际支持时才配置PKCE，不能无条件宣称PKCE已验。
- [ ] 默认不JIT授管理员。管理员预先明确映射已有usr；若允许新员工首次登录创建普通成员，必须通过租户的受支持corp/app范围策略。账户链接要求当前平台身份或明确管理员确认；不按名称/邮箱猜测；缺少真实姓名时保留缺字段事实，若创建账户必须有显示名则使用显式本地别名而不冒充企业姓名。
- [ ] 跑`uv run pytest packages/xuanpu-server/tests/identity packages/xuanpu-server/tests/r3_contract/test_external_binding.py -q`，确认Feishu、Cookie、act-as、最后管理员保护回归；提交`feat: 接入企业微信身份并保留稳定员工映射`。

退出标准：真实企业OAuth尚未执行前只称代码通过；扫码/客户端授权与合成Cookie分别留证。

## A3：按调用授权、token更新及只读投影

Files：Create `connections/{broker,tokens,call_guard,providers/wecom,projections}.py`、`examples/wecom-readonly/{xuanpu.yaml,api.yaml,skills/read/SKILL.md}`；Modify `runtime/{http_api,connectors,assembler,gate_audit}.py`、`identity/disable.py`；Test `tests/r3_contract/test_broker.py`、`test_token_races.py`、`test_wecom_reads.py`。

- [ ] 写并发Alice/Bob、scope复用、伪造userid/corpid/token参数、403、token过期、刷新与撤销竞争、恶意重定向和secret回显测试。
- [ ] CredentialBroker从SubmissionIndex/受保护会话取真实usr；每次重新核对员工、installation、binding、consent epoch和operation/resource范围。复用HTTP现取凭据，不缓存员工授权结论；stdio/远端MCP不因本包自动获得员工连接支持。
- [ ] 复用HTTP工具编译和transport；binding声明的userid/access_token不向模型暴露，broker根据已验证subject填入受保护参数，再构建真实请求。拒绝模型覆盖这些参数或混用另一包的binding；self_profile不接收任意他人userid。目录预览仅开放有权管理员，B未完成前不提供普通员工任意成员查询。
- [ ] 应用token按installation+credential revision单次更新合并：短事务取得refresh lease→事务外请求→CAS存密文。读取接口遇已核定的失效码，最多取新token并重试一次；网络/5xx只做有界退避，不能忙循环或重试写操作。
- [ ] 查询参数注入须在server侧，日志/trace移除access_token、corpsecret、code及user_ticket。固定HTTPS目的端、不跟随重定向、不允许普通参数覆盖认证字段。检查HTTP与供应商业务errcode，200但非成功errcode仍失败；只返回A0批准的字段白名单、读时刻、来源与缺字段说明，不透传响应正文或异常URL。
- [ ] 撤销以epoch提交为线性化点：之后不准新派发；已在途请求尽力取消，返回时再校验、丢弃内容并记元数据。不能声称已发出的只读请求从未到达供应商。
- [ ] 跑`uv run pytest packages/xuanpu-server/tests/r3_contract/test_broker.py packages/xuanpu-server/tests/r3_contract/test_token_races.py packages/xuanpu-server/tests/r3_contract/test_wecom_reads.py packages/xuanpu-server/tests/test_http_connector.py -q`；提交`feat: 按员工委托校验企业微信只读调用`。

## A4：R2后台、失效传播与原生入口

Files：Create `connections/task_bindings.py`、`packages/xuanpu-console/src/xuanpu_console/static/connections.js`、`packages/xuanpu-sdk-py/src/xuanpu_sdk/connections.py`、`packages/xuanpu-cli/src/xuanpu_cli/connection_commands.py`；Modify `work_tasks/{models,execution_models,service,capability_runner,views}.py`、`assistant.py`、`bootstrap.py`和各包导出；Test `test_connection_recovery.py`、`packages/xuanpu-console/tests/connection_behavior.cjs`及SDK/CLI测试。

- [ ] 写旧R2记录无connection字段仍可读、新任务固定installation/consent引用、撤销后冷恢复拒绝、重新同意不复用旧批准、模型不可用直接撤销、身份切换不显示迟到结果等反例。
- [ ] WorkTask/ExecutionGrant新增默认空`connection_refs`，只保存引用及epoch，不保存token。恢复和发起工具调用都复核；授权不足显示`connection_reauthorization_required`，新同意需本人显式继续，沿用预算/attempt上限。
- [ ] 控制台显示“企业应用已配置”和“本人已同意”两种状态；只读本人资料有直接按钮，对话可以引导同意但模型不能自行提交consent。管理员只读目录预览单独入口，不向普通员工泄露成员资料。
- [ ] SDK/CLI查询/读取/撤销只操作当前本人；创建/扩大委托只给出浏览器确认入口，不自动批准。private credentials文件供CLI读取；无命令行tenant/user覆盖，不在输出显示token。撤销后停止后台新读取，停用监听幂等，源端撤权导致明确失效状态。
- [ ] 跑`uv run pytest packages/xuanpu-server/tests/r3_contract/test_connection_recovery.py packages/xuanpu-console/tests packages/xuanpu-sdk-py/tests packages/xuanpu-cli/tests -q`；提交`feat: 从员工入口使用并撤销只读连接`。

## A5：独立真实验收和兼容试点

Files：Create `scripts/acceptance/r3_connections.py`；实现时写实际日期release记录；Modify受影响`docs/architecture/`与`docs/guides/`，各包按实际日期升版本/更新uv.lock。

- [ ] 脚本显式接收private credentials、独立evidence目录、staging/production、隔离tenant；只允许冻结的本人读取/目录预览，不自动扩供应商权限，不配置假成功身份。
- [ ] 双后端与真实HTTP负向矩阵通过后，再用真实企微员工完成登录/绑定→同意→实际本人读取→撤销→旧网页/R2恢复拒绝。至少两个真实账号，其中一个不在应用允许范围；真实条件不足标partial。
- [ ] 单独验证app token更新、app权限缩小、在途撤销、只读外部调用回执、管理员元数据隐私；真实企微响应业务字段与平台白名单逐项核对，写API实际调用0。
- [ ] 全门禁、镜像已安装源码/版本、三段灰度；保留R2旧数据/对象/unknown及预算，先forward-disable新读取，兼容查询/撤销仍可用；收尾临时key、测试consent、登录和token。
- [ ] 独立复核后提交`feat: 发布并验收企业微信只读连接`，另提交脱敏证据。未通过真实企业验收时不能勾选本Task完成。

**A完成定义：** 实际企业身份及本人只读闭环通过，默认未授权无外部调用，撤销/范围变化与R2恢复守住权限。通讯录预览不是组织导入；平台委托撤销不是供应商全局应用撤销。

## 定向红测试补充（分配到对应Task，不是另一个实现入口）

A2回调前置fixture注入有效code但跨浏览器state，A3注入共享runtime和并发token更新，A4撤销后模拟进程重建；A0的provider server需用其正常HTTP路由控制这些场景，下面只调用产品接口：

```python
async def test_actor_parameter_is_not_an_auth_override(r3_http, r3_ids):
    response = await r3_http['alice'].post('/v1/me/connections/' + r3_ids['installation'] + '/read', json={
        'operation': 'self_profile.read', 'parameters': {'userid': r3_ids['bob']}})
    assert response.status_code == 400

async def test_revoked_consent_cannot_resume_old_read(r3_http, r3_ids):
    # fixture已通过产品接口撤销consent并重新创建平台对象，保留旧R2记录。
    response = await r3_http['alice'].post('/v1/work-tasks/' + r3_ids['revoked_connection_task'] + '/decisions', json={
        'kind': 'tool_approval', 'request_id': 'old-read-approval',
        'state_version': r3_ids['revoked_task_version'],
        'decisions': {r3_ids['old_pending_request']: {'granted': True}}})
    assert response.status_code == 409
```

`POST /v1/me/connections/{id}/read`是A3/A4共用原生读取入口，参数仅operation及经过严格schema的parameters；不存在的/已撤销consent统一拒绝，读本人资料的parameters必须为空。该接口与模型工具调用同一个broker。
