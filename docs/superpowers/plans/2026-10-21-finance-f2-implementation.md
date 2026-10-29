# 财务 F2 付款与记账 Implementation Plan

> 使用superpowers:subagent-driven-development完成独立实现、规格核对和代码质量审阅；provider与领域按明确文件边界并行。用户已批准继续研发并选择先通用接口/隔离闭环，不重复请求实现许可。每项先有意义RED，再GREEN及适用回归。

**Goal:** 将已批准报销/原生应付接到专用人工批准、通用付款/记账/冲回API及独立持久结果闭环。
**Architecture:** 复用F1 FastAPI/Pydantic/asyncpg/httpx；平台只使用现有通用cookie人工网关，不导入财务业务代码。独立队列、Inbox、回执和额度事务；不新增消息中间件。
**Baseline:** 4bc7010→feat/finance-f2，复用检出xuanpu-finance-f0；原xuanpu不改。专属PG xuanpu-finance-f2-test-pg，回环60032，仅finance_test与随机schema。
**Spec:** [F2设计](../specs/2026-10-21-finance-f2-design.md)及原domain-contract。

## 文件边界和接口

领域：expenses/models.py、settlement/models.py、storage/f2_{schema,config,catalog,payables,plans,approval,execution,queue,results,reads,exceptions}.py，必要F1 storage有限hook与store继承/迁移，tests/test_f2_{models,domain,queue,results,guards}.py。文件过500行继续按职责拆分。
Provider：settlement/provider_models.py、providers.py、worker.py及独立HTTP/回调/租约测试。公共ProviderConfig和ProviderResult以设计字段锁定，不改domain storage或root服务接线。
集成：config.py/runtime.py/api.py/dependencies.py/routes_f2.py、routes_f1必要接线，console finance_settlement.js及F1调用hook、example Skill/API/manifest、版本/lock/镜像/文档、签名HTTP与真实平台联调及浏览器。两工作包不交叉写入，不单独提交；全门禁后统一提交。

Store公开配置configure_settlements(policies,business_documents,payee_accounts,providers,*,simulation=False)。providers为无凭据描述，包括固定base_url与版本/能力/环境；相同版本换目标不可覆盖。BusinessRef固定kind/source/id/version字符串/sha256。业务公开get_settlement_options、create/get/list/update_payable，list_business_documents/list_payee_accounts，get/list_payments，get/list_postings，list_action_plans，reconcile_operation；原preview/execute/queue/get/cancel多态扩展。

Worker：SettlementWorker(store,providers,gateway).run_once(*,allow_dispatch=True)。claim_settlement、renew_settlement、prepare_settlement_dispatch、record_settlement_result(job,result,raw)、defer_settlement(job,code,uncertain=...)、reject_settlement_before_dispatch、release_settlement、accept_provider_event按设计签名。job锁定tenant/company/id/lease_token/mode/action/request/actor/provider/config，authorization_user_ids用于在线核对所有原授权人员。

ProviderRegistry(configs,client,*,tokens,callback_secrets)，注入httpx客户端及已读固定凭据；authenticate_callback(provider,configuration_version,headers,raw)返回event_id与strict body。资源由runtime AsyncExitStack关闭，不隐式读环境变量。

## 1 基线与详细规格

- [x] 读取总设计/领域契约与F1源码，复用干净检出，创建F2分支和专属PG。
- [x] 明确用户未选供应商，正式关闭、隔离模拟、原F1内部记录不升级外部执行。
- [x] 独立规格审阅Approved；补能力门禁及stale回执只追加证据，不推进状态。
- [x] 全量基线FINANCE_TEST_DSN显式注入，保留真实输出和跳过边界。

## 2 严格契约、迁移与受控配置

- [x] RED：原ActionPreview不接受F2动作，Payable/账户/策略缺失；严格拒float、指数、错日期、额外身份/状态、错误动作参数。

```python
ActionPreview(action="execute_payment", target_type="expense_claim", target_id="c", expected_version=1,
    parameters={"amount":"100.00", "payee_ref":{"kind":"employee_account","source":"internal","id":"a","version":"1","sha256":"a"*64}})
```

预期缺少契约时ValidationError；补模型后合法输入通过，amount=1.0和自报status仍失败。
- [x] GREEN：外部动作精确参数矩阵；PayableDraft遵守原契约；SettlementPolicy、BusinessSnapshot、PayeeAccount版本化配置；受控provider descriptor，缺能力拒绝。
- [x] 新表及legacy document_kind/simulation、持久service mode marker；旧数据默认非模拟、旧报销API过滤expense，F0/F1迁移两次不丢收件/异常/历史。
- [x] 配置引用实际公司/人员角色，付款与记账执行集合分开，版本及generation不可覆盖/复活；保留旧配置供在途查单。
- [x] 验证 `uv run pytest packages/xuanpu-finance/tests/test_f2_models.py -q`；真实PG由既有安全fixture保证数据库名。

## 3 原生应付、目录与状态读取

- [x] RED：模型伪造供应商/采购/账户ref拒绝；跨公司、无人员范围、旧版本摘要、不匹配收款人拒绝；旧报销接口不得读改应付。
- [x] GREEN：应付create/list/get/update；supplier及至少一份采购依据真实配置匹配；submit_payable复用F1审核与分票占额。目录仅脱敏投影，不开放员工“核实账户”写入。
- [x] 所有列表过滤后total、稳定游标和1..100限制；Payment/Posting/plan查询验证所属单据权限，服务端累计金额，不靠浏览器算余额。

## 4 专用行动计划审批与人工执行

- [x] RED：报销批准没有付款批准、原经办人/owner自审、错step、agent执行、过期、账户/期间/provider变更、撤权恢复、同key异内容或换key重复执行均拒绝。

```python
plan = await store.preview_finance_action(payer, payment_preview, "prepare")
assert not plan["executable"]
# 必须实际分派人对target_type=action_plan作human决定，再由原payer human执行。
```

- [x] GREEN：默认24h（≤72）原计划摘要固定全部影响；独立action_plan workflow/决定/grant不递归，原digest不含自身审批动态状态；review_queue并入专用待办。
- [x] human执行受理accepted+pending记录+金额占用同事务，不伪造succeeded；原单据版本不随外部receipt变动，正交结果维度独立version。
- [x] F1 internal_record永久无外部资格；simulation/正式不混用，正式缺原件或有效查验证据阻断。

## 5 金额、租约队列、回执与异常

- [x] RED：同父单据并发两600争1000，仅一个外部受理；付款与记账各自独立余额；部分付款/入账与部分冲回，unknown保留占用；事件失败整事务回滚。
- [x] GREEN：父单据锁与固定顺序的invoice锁，成功/在途按维度聚合；确定失败/取消解除动作占用，成功/未知阻止withdraw和释放原invoice额度。
- [x] RED：dispatching后崩溃、租约过期/旧fence、已发取消、not_found、错回执不能再次submit；worker重建只查原请求号。
- [x] GREEN：SKIP LOCKED认领、持久lease/fence/心跳、发送前dispatching；stale原字节仅append证据，禁止状态写回；真实validated结果与operation/payment/posting/事件/Outbox同事务。
- [x] 新独立settlement异常合并可见查询；unknown/冲突不能靠人工改paid，只有查单回执可收敛；有限自动核对后转人工reconcile只读查询。

## 6 HTTP provider与callback

- [x] RED：真实本机HTTP provider返回202 accepted不得paid；缺transaction/voucher、错归属/摘要/币种/金额/simulation、5xx/畸形/重定向均未知且保全raw。
- [x] GREEN：固定POST /v1/requests与GET原请求号；原外部幂等键/冻结请求不改，不做自动submit重试；凭据客户端注入，服务管理关闭。
- [x] RED：错误HMAC/keyid/过期/tampered raw、同event异文、旧回调不回退、新请求不存在不派付款；验证disabled/read_only仍可回调+query。
- [x] GREEN：原始字节验签后Inbox去重与只读查询排程；无可靠sequence回调永不直接改终态；错误匹配留证据建异常。
- [x] 连通真实PG和HTTP测试服务，进程重启/模型不可用/身份服务不可用后的已发核对分别验证。

## 7 API、运行配置与资源生命周期

- [x] root先写F2路由/config/api模式测试RED，再实现create/list/get/updatePayable、get/listPayments、get/listPostings、listActionPlans、reconcileOperation和两个catalog查询；严格body/CAS/幂等/signature与FinanceCaller复用。
- [x] 原route preview/execute扩ActionPreview，不放宽origin=human；F2取消只接受human；draining不收新外部操作，read_only/disabled仍接供应商callback，worker只允许query。
- [x] FinanceConfig增加execution_simulation、settlement_policies/business_documents/payee_accounts/settlement_providers默认空，production禁止simulation，模拟库名finance_test且对象独立。相对token/callback路径固定解析，不接受员工路径。
- [x] runtime配置store后构造ProviderRegistry/SettlementWorker；_lifespan分别停止两个后台、pool/http资源可靠关闭；lock丢失暂停新dispatch但保留在途核对能力。
- [x] 执行 `uv run pytest packages/xuanpu-finance/tests/test_f2_api.py packages/xuanpu-finance/tests/test_f2_config.py -q`；具体用例真实记录body/signature拒绝非human不冒充DB测试。

## 8 Skill、声明包和无模型辅助页

- [x] 保留F1四Skill，加入accounts-payable/posting-reconciliation；操作白名单只开放已实现F0-F2，F3不发布。Skill可准备/查进度，不获execute权限；unknown先查不新建付款。
- [x] 真实package validate、稳定内核read_skill及实际响应schema验证；金额/状态/账户的实际契约与展示一致。
- [x] 无模型页面应付列表与草稿、付款/记账/冲回预检、专用待办与人工决定、原经办人确认执行、结果查询/未知核对/取消；账户与期间来自目录，读写同API，页面无凭据/API名/JSON输入。
- [x] Node行为测试覆盖受理不称成功、身份/公司/参数变更失效旧确认、409重读、未知保留原键、禁自审；实际浏览器隔离模拟流程留截图。

## 9 验收、发布与清理

- [x] 先独立spec-compliance审阅，再quality-review，修复P1/P2后重新验证。
- [x] 真实平台cookie→签名→财务认证/PG→专用审批/执行→HTTP provider/回调查单；模拟结果明确标记、不宣称真实支付或正式凭证。
- [x] 全量 `uv run pytest -q -ra`、`uv run ruff check --select F,S108,I,TC packages`、`uv run mypy packages/*/src`、`uv lock --check`、文档/relative links与`git diff --check`；跳过原因分层记录。
- [x] 更新finance/console及实际受影响包版本、镜像tag/独立锁；build+独立Compose模拟HTTP闭环、重启未知只查单，测试项目资源/凭据完整清理。
- [x] 更新architecture/finance、guides/finance-f2与releases验收，分别说明源码/测试/镜像/本机HTTP/真实模型/真实供应商/正式制度/生产边界。保持独立分支不推送或合并，原工作区不改。

## 实施结果

已完成规格与质量独立Approved、实际浏览器模拟付款、最终镜像Compose重启unknown只查单以及付款600/入账1000/冲回250。最终全量2190 passed、3项既有非财务PG专用用例跳过；13项新增实际HTTP契约遍历、F1→F2重复迁移及维护性拆分后全部重新验证。最终证据、准确数量、源码/构建/生产边界见[验收记录](../../guides/releases/2026-10-21-finance-f2-acceptance.md)。

实际领域文件按职责为storage/f2_catalog、f2_documents、f2_checks、f2_plans、f2_execution、f2_queries、f2_results、f2_worker、f2_inbox、f2_schema；未新增中间件。平台生产代码无财务导入。保持feat/finance-f2独立本地分支，不推送或合并。
