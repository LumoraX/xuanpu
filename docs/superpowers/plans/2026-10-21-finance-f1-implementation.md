# 财务 F1 报销与 HITL Implementation Plan

> 使用 superpowers:subagent-driven-development 的实现/规范复审/质量复审；互不共享文件的平台桥与财务领域按 dispatching-parallel-agents 并行。用户已批准继续开发，执行当前会话内完成，不重复请求流程方式确认。用户随后明确无正式制度，先轻量内部记录，未来可替换接入企业微信/飞书；新增record_mode与可选原件证据，真实HITL和独立存储不变。

**Goal:** 在独立财务库实现报销、原子额度预留、多步真实人审批与无模型手动兜底；维持F0原件及签名边界。
**Architecture:** 财务领域保持进程外；平台只增加通用cookie-only人工HTTP连接器网关和可信人工来源。Skill可准备草稿/预检，业务决定只由真实员工手动入口提交。F2/F3能力不发布。
**Baseline:** `5bdff7f` → `feat/finance-f1`，复用 `/Volumes/Codes/lumorax/lumorax-platform/xuanpu-finance-f0`；原 `xuanpu` 工作区不变。旧临时PG已不存在，新建独立 `xuanpu-finance-f1-test-pg`，仅finance_test数据库和随机schema。不得连接平台/生产库。
**Spec:** [F1详细设计](../specs/2026-10-21-finance-f1-design.md)、既有领域/状态/接口契约。

## 文件与接口锁定

财务领域工作包独占：`packages/xuanpu-finance/src/xuanpu_finance/expenses/**`、`storage/business*.py`、`storage/f1_schema.py`、`storage/{store,common,queries,imports}.py`、`tests/test_expense*.py`、`tests/test_expense*.py`。
平台工作包独占：`xuanpu-server`的签名/通用手动连接器服务/API/接线/测试、`xuanpu-console`静态页面和对应测试。不得import财务运行代码。
集成者独占：财务 `models.py/auth.py/config.py/runtime.py/api.py/routes_f1.py`、root/各包版本和lock、examples/finance、文档、HTTP/进程/浏览器联调测试。除明确分配不交叉编辑。

财务models统一在 `expenses/models.py`（strict extra=forbid）：ExpenseDraft、ExpenseUpdate、AllocationRequest、ActionPreview、ActionExecution、ExceptionResolution、ExpensePolicy。
Store公开接口：

```python
async def configure_expenses(self, policies: list[ExpensePolicy]) -> None: ...
async def create_expense_claim(self, who: FinancePrincipal, body: ExpenseDraft, key: str) -> dict: ...
async def list_expense_claims(self, who: FinancePrincipal, entity: str, cursor=None, limit=50, status=None) -> dict: ...
async def get_expense_claim(self, who: FinancePrincipal, id: str) -> dict: ...
async def update_expense_claim(self, who: FinancePrincipal, id: str, body: ExpenseUpdate, key: str) -> dict: ...
async def create_invoice_allocation(self, who: FinancePrincipal, body: AllocationRequest, key: str) -> dict: ...
async def release_invoice_allocation(self, who: FinancePrincipal, id: str, expected_version: int, key: str) -> dict: ...
async def get_invoice_allocation(self, who: FinancePrincipal, id: str) -> dict: ...
async def list_expense_categories(self, who: FinancePrincipal, entity: str, cursor=None, limit=50) -> dict: ...
async def list_cost_centers(self, who: FinancePrincipal, entity: str, cursor=None, limit=50, kind=None) -> dict: ...
async def preview_finance_action(self, who: FinancePrincipal, body: ActionPreview, key: str) -> dict: ...
async def get_action_plan(self, who: FinancePrincipal, id: str) -> dict: ...
async def execute_finance_action(self, who: FinancePrincipal, body: ActionExecution, key: str) -> dict: ...
async def list_review_queue(self, who: FinancePrincipal, entity: str, cursor=None, limit=50) -> dict: ...
async def resolve_exception(self, who: FinancePrincipal, id: str, body: ExceptionResolution, key: str) -> dict: ...
```

参数与响应逐项遵守原finance/catalog schemas；仅新增listExpenseClaims与展示workflow/step/version/due_at的可选字段。F1业务Operation使用f1op_前缀，既有getOperation显式路由；不进入F0认领/取消队列。方法实际类型注解用完整Any/Union，不用裸dict，以上仅列调用接口。

## 1 基线与设计审阅

- [x] 读取已批准总设计、领域/F0源码及F1契约，复用干净隔离检出并创建分支。
- [x] 明确人工来源、草稿恢复列表、冻结内容版本与工作流版本、空审批链和撤回例外。
- [x] 独立spec-review修正并Approved，基线全pytest；缺旧测试容器的首次启动中止记录为环境问题，使用新独立PG重跑。

## 2 财务严格模型、迁移与主数据

**Files:** expenses/models.py（金额与策略模型）、storage/f1_schema.py、storage/store.py、test_expense_models.py、test_expense_domain.py。

- [x] RED：

```python
with pytest.raises(ValidationError):
    ExpenseDraft.model_validate({"legal_entity_id":"company-a","reason":"差旅",
        "lines":[{"category_id":"travel","amount":1.5,"occurred_on":"2026-10-21","reason":"交通"}]})
```

同样验证额外身份/状态、负数/指数/日期别名、合计溢出、未配置分类/归属、同策略版本改内容与空审批链拒绝。
- [x] GREEN：模型继承strict BaseModel；Money正则为 `^(0|[1-9][0-9]{0,15})\.[0-9]{2}$`，以Decimal求和并校验界限；日期正则+date.fromisoformat。ExpenseDraft增加可选evidence_ids实际同公司receipt，ExpensePolicy增加record_mode=invoice_backed/internal_record；内部记录须显式不要求查验，可无分配提交，但任何已有分配仍严格Σ==total。ExpensePolicy显式公司/版本/启用、分类、归属及授权用户、固定不同人员的审批步骤/门槛、reviewer异常负责人、查验/归属开关、最大总额；缺省不启用。
- [x] 在同一财务库迁移新增表；升级grant CHECK允许approver，保持collector/reviewer语义。F0迁移与导入数据不删除。策略不可变revision+current指针；每命令查当前权限与策略，变化撤销旧ExecutionGrant，历史待审阻断后由owner撤回重提。
- [x] 运行 `uv run pytest packages/xuanpu-finance/tests/test_expense_models.py packages/xuanpu-finance/tests/test_expense_domain.py -q`，真实PG初始化及F0兼容。

## 3 草稿与分配、原子额度

**Files:** storage/business_{claims,allocations,base,catalog}.py、对应test_expense*.py；必要拆成每文件<500/函数≤80。

- [x] RED：真实PG并发两个600.00元申请争1000.00发票仅一个提交；两票中一票不足全部回滚；税额累计不能双占。

```python
results = await asyncio.gather(submit(first), submit(second), return_exceptions=True)
assert sum(isinstance(value, FinanceError) for value in results) == 1
assert await total_reserved(store, invoice_id) == Decimal("600.00")
```

测试helper具体实现在test文件中按实际公开Store接口预检/执行，不能mock SQL或靠内存锁。
- [x] GREEN：create/get/list/update草稿、受控目录和分配CRUD；所有写入复合幂等与CAS。分配变更递增claim版本；修订释放旧proposed但保留历史。提交锁claim→workflow→排序invoice，复核票/行gross与tax累计、Σallocation==total、授权/策略/正常蓝票；一次事务reserved+workflow+事件+本地Operation+Outbox。
- [x] 实时available_amount读预留/核销，approval不会消费额度；驳回/撤回释放一次。approver仅分派单据/关联票据可读，普通同事和平台admin无隐式权限。

## 4 不可变计划与真实审批决定

**Files:** storage/business_{plans,execution,workflow,exceptions}.py、test_expense_domain.py/test_expense_guards.py。

- [x] RED：非human、owner自审、非分派人、旧step/claim/策略、到期、摘要不符和同key异内容全部拒绝。一次plan换key也不能重复决定；事件写失败整批回滚。

```python
agent = FinancePrincipal("tenant-a", "reviewer", origin="agent")
with pytest.raises(FinanceError, match="HUMAN_REQUIRED"):
    await store.execute_finance_action(agent, execution, "approve-agent")
```

- [x] GREEN：15分钟不可变计划固定actor/company/action/content version/allocations/invoice versions/rules/workflow step+version/evidence/parameters。getPlan只重算executable/blockers。submit/approve/reject/withdraw执行要求human；预检允许agent准备。动作参数按矩阵严格匹配，不实现F2/F3。
- [x] 中间审批只更新workflow/step版本，claim冻结；最终批准改claim approved、创建仅expense_approved用途grant并继续reserved。reject需原因；withdraw当前owner可撤旧策略流程；两者取消未决步骤、撤授权、释放预留并保留事件。
- [x] 原子业务Operation直接记录本地提交结果并返回202；待审不是模型挂起。Outbox无通知适配器保持pending，不伪称通知成功。异常证据只接受实际有权receipt；hard_block不手动改成功，warning需human reviewer+理由。

## 5 通用人工HTTP网关与可信origin

**Files:** server/runtime/{call_context,http_api}.py、manual_connectors.py、api/integration_actions.py、app/platform接线、test_manual_connectors.py、test_call_context.py。

- [x] RED：真实cookie+CSRF可用；root/key/act-as/cookie+Authorization/跨站/缺CSRF均拒绝；model gate即使已工具批准仍origin=agent；客户端自报origin不能升级。
- [x] GREEN：新增cookie-only HumanCaller，校验principal.key_id/system/user/tenant。人工签名直接基于当前登录核对及服务端interactive关联ID并审计，独立于model/session持久聊天字段；共用最终Request摘要/EdDSA/固定origin许可。原工具signer始终agent。
- [x] 只解析当前生效已安装包、HTTP+明确tools白名单+contextAudience，拒绝任意URL/目录/凭据；复用HttpApiClient，不构建RuntimeAssembler/模型/沙盒。写入需明确人工confirmed，带相同幂等键重试，不自动重发超时写。
- [x] HTTP返回结构化status/body/is_error/truncated，错误不带服务密钥；审计actor/package版本/operation/参数摘要/关联ID/origin。所有client关闭。

## 6 财务API和运行配置集成

**Files:** models.py/auth.py/config.py/runtime.py/api.py/routes_f1.py、test_f1_api.py、test_f1_contract.py及server/tests/identity/test_finance_gateway_e2e.py。

- [x] RED：实际Ed25519 JWT的agent无法approve/submit；缺origin仅兼容为agent，未知origin401；签发human+真实角色及有效计划才能决定。
- [x] GREEN：FinancePrincipal增加origin默认agent，Auth严格取签名字段、防重放主体含origin；body不能指定。配置增加expense_policies默认空，审批人/异常负责人/公司grant交叉验证。启动configure_expenses并保留F0功能开关。
- [x] 14个新路由：严格body、Idempotency-Key、CAS，amount只字符串。分别挂finance/catalog组，GET目录/操作可只读，写操作仍受enabled/draining/read_only/disabled约束。draining允许已有审批决定与清理，禁新业务；政策使能与平台包开关不删除数据。
- [x] 运行 `FINANCE_TEST_DSN=<独立库> uv run pytest packages/xuanpu-finance/tests/test_f1_api.py packages/xuanpu-finance/tests/test_f1_contract.py packages/xuanpu-server/tests/identity/test_finance_gateway_e2e.py -q`。

## 7 Skill、声明式入口与人工页面

**Files:** examples/finance/{xuanpu.yaml,apis/finance.yaml,apis/catalog.yaml,skills/expense-reimbursement,skills/finance-review}、console/static/{finance.js,index.html,app.js,workspace.js}及测试。

- [x] 先以用例固定：分类/归属必须来自目录；draft不占额；预检不等于提交；not_checked不称有效；审批只能分派本人，human步骤不能由Skill execute。
- [x] 发布25操作，增加listExpenseClaims schema；F2/F3移除。四个Skill职责为入口+invoice-intake+expense-reimbursement+finance-review；Skill工具不开放human execute代点，提供真实待确认计划及辅助入口。
- [x] 无模型页面至少：本人草稿列表/新建/编辑、按发票ID创建分配、详情/证据、审批队列、预检影响、明确提交/批准/驳回/撤回按钮。调用通用manual gateway，无直接财务数据库/凭据；普通员工不见API名/JSON。
- [x] 金额/版本来自服务快照；身份epoch切换清空，409与请求改动清空旧确认；超时保留原key/计划并查询，不提示已批准。脚本必须通过 `node --check` 和现有console契约测试；实际浏览器走一遍独立测试环境。
- [x] 实际 `uv run xuanpu validate examples/finance`，当前内核加载4个Skill及references；真实模型另记，不能把受控模型算正式验收。

## 8 联调、故障与交付

- [x] 独立spec-review→修正→quality-review→修正，未解决P1/P2不得交付。
- [x] 真实平台cookie→manual signer→财务ASGI/HTTP→真实PG：创建草稿/分配/提交→两步不同人审批，第一步后重建服务仍第二步待审→最终approved且reserved；另一路驳回→修订重提、撤回释放；关模型仍可操作。
- [x] 测试角色撤销、策略变更、跨租户/公司、自审、并发双审、同票超分、旧计划、原件保留、F0兼容及迁移保留数据。
- [x] 全量 `uv run pytest -q -ra`、`uv run ruff check --select F,S108,I,TC packages`、`uv run mypy packages/*/src`、`uv lock --check`、`git diff <baseline> --check`；记录真实输出/跳过原因。
- [x] 升各包独立版本与镜像标签，白名单重建镜像/独立Compose烟测，真实浏览器与API证据入 `docs/guides/releases/2026-10-21-finance-f1-acceptance.md`；架构/指南同步。
- [x] 保留独立分支，未推送/合并/生产部署；清理本任务临时容器/卷/密钥。真实制度、官方票据/税务、真实模型与F2/F3分别标未验收/未实现。

## 实施结果与路径调整

领域实现按职责拆入storage/business_base/catalog/claims/allocations/plans/workflow/execution/exceptions.py及expenses/models.py；计划中的expenses/policy/money等模块名由上述实际文件承载。tests为test_expense_models/domain/guards、test_f1_auth/config/api/contract和server identity下真实gateway联调。公共接口签名及数据所有权不变。

策略与授权增加持久generation/tombstone，防止停止后恢复旧策略/人员复活旧决定；原样配置重启保持generation，业务待办可持续。独立规格和质量审阅均在修正后Approved。

浏览器验收已实际完成25元内部记录保存→本人确认提交→审批人从队列预检并批准→报销人刷新恢复approved；另验证approved撤回的预检/放弃及“尚未执行”提示。跨服务签名/身份/PG联调覆盖两步审批第一步后重建再由第二人继续；真实模型、vendor与生产未执行。最终门禁和容器烟测见[验收记录](../../guides/releases/2026-10-21-finance-f1-acceptance.md)。

最终独立镜像与Compose烟测已通过，容器重启保留同一单据、流程和待办步骤后完成实际人工批准。临时动态端口检测错误已定位为烟测脚本问题，失败/诊断保留，最终镜像重跑通过；PG16/stub在线身份与真实平台cookie联调分别留证，不冒充PG17或生产验收。
