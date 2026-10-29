# 财务 F1 报销与真实业务审批

状态：用户已明确授权按完整财务设计继续拆分并开发 F1；本文件细化实现边界，不重新请求已获授权。基线 `5bdff7f`，复用干净隔离检出 `xuanpu-finance-f0`，新分支 `feat/finance-f1`。既有总设计与领域契约继续有效。

## 1 交付与选择

用户补充：当前没有现行报销制度/审批名单，优先轻量内部记录，未来可对接企业微信/飞书。先交付简单分类、可选部门、至少一步实际人审批的内部台账；不预设复杂企业制度。

交付报销草稿、版本修订、受控分类/归属、发票分配与提交占额、原生多步审批、驳回/撤回、异常证据处置、查询恢复，以及不依赖模型的人工审批入口。F0 独立 PG/原件、服务身份与平台签名不变。

选择独立财务领域工作流与数据库事务：复用 FastAPI/Pydantic/asyncpg，不引入工作流引擎或支付适配器。外置通用工作流引擎会额外引入存储和一致性边界，本阶段仅有可确定的顺序审批；只用聊天挂起/工具批准则无法满足持久审批与职责分离。后续可替换流程执行器，业务主键与批准事实仍由财务持有。

F1 发布总计25个操作：F0的11个、finance新增11个、catalog新增3个（补齐listExpenseClaims供刷新/无模型恢复草稿）。只实现 submit_claim/approve/reject/withdraw；其余动作明确拒绝，不注册F2/F3能力。审批通过不等于付款、入账或验真。生产制度和人员尚未提供时，仅开发样例启用，正式公司无显式策略不开写流程。

## 2 模型、规则与配置

财务库新增 ExpenseClaim/current+history、Allocation、Policy/current+immutable revisions、ActionPlan、Workflow/Step/Decision、ExecutionGrant、业务幂等/Operation、Outbox。全部 tenant/company 复合范围，无平台库外键。业务事件沿用财务事件表，状态更新/历史/决定/额度/本地operation/outbox同事务。

金额API两位小数字符串，计算用Decimal/SQL numeric(18,2)，费用明细与分配金额必须大于0；禁止float、科学计数、NaN、负金额和合计溢出。occurred_on仅YYYY-MM-DD，完整日期校验。单据只能本人建/改；审批人由策略指定，不接受请求自报身份。

新增可选 expense_policies，缺省空列表兼容F0。每项：tenant_id/legal_entity_id/version/enabled/categories/cost_centers/steps/exception_owner_user_id/require_verified_invoice/require_cost_center/max_claim_amount/record_mode。record_mode可选invoice_backed（严格分配，兼容原设计）或internal_record（轻量内部台账）；内部台账必须显式require_verified_invoice=false，不授予正式财务动作资格。分类为id/name；归属为id/name/kind/allowed_user_ids；步骤为id/name/approver_user_id/minimum_amount（默认0.00）。minimum_amount按报销总额>=门槛筛选步骤，提交必须有至少一个适用且非owner的审批步骤，空链直接阻断，永不自动approved。启用至少一步，例外负责人必须当前reviewer，指定审批人必须当前 approver 或 reviewer；所有步骤人员不同。collector/reviewer沿用，新增approver角色只看本人及实际分派记录，不因此取得公司全部票据权限。

策略同版本不同内容拒绝覆盖，策略版本快照不可变；完整配置撤销缺失策略的写资格，保留历史。每次命令复核当前授权与策略。审批链提交时冻结，策略变化、审批人撤权或停用使旧计划/执行授权不可使用；历史决定不删除。原等待流程显示阻断，申请人可明确撤回后按当前策略修订重提，不自动批准/自动释放历史占用。

require_verified_invoice默认true；internal_record内部记录或隔离开发样例可显式false验证业务审核，但not_checked/unverified原义不变。F1不授予付款、税务或入账资格，任何后续正式动作仍必须满足自己的查验和批准条件。

## 3 单据与额度

createExpenseClaim只创建draft，总额服务端求和。ExpenseDraft可带最多100个唯一evidence_ids，服务校验实际有权、同公司SourceReceipt；PDF等awaiting_original材料可作为内部记录附件，不创建或伪造结构化Invoice。附件可选，纯文本内部记录保留材料缺口。BusinessDocument显式返回record_mode；该模式随每个claim内容版本持久保存并进入ActionPlan摘要，不能由当前策略动态重标历史。模式变更须通过可编辑状态的新版本修订并重新预检/审批；提交时冻结模式必须与当前策略一致。草稿可重建查询；更新需expected_version且公司不可换，draft/rejected/withdrawn允许本人修订，新版本使旧proposed分配释放；既有历史保留。禁止改owner/status/total/审批结果。

createInvoiceAllocation必须同时核对票版本与单据版本、票和单据的数据权限、同公司、正常蓝票及明细归属。草稿分配proposed不占额；创建/解除提议递增单据版本，使旧预检失效。税额>=0且<=金额/可分配票面税额，不代表税额抵扣。

internal_record且没有有效提议分配时可无结构化发票提交，计划/界面明确内部记录及缺票/待核实，额度不发生变化。只要存在分配，或record_mode=invoice_backed，仍严格要求Σallocation==total及全部占额不变量；不能以内部记录模式部分占额后忽略差额。

submit_claim预检不占额。执行时先锁单据，再按invoice_id排序锁票，重新汇总reserved+consumed与本单proposed。对于invoice_backed或存在提议分配的单据，Σproposed.amount必须严格等于报销total，超额也拒绝；任何一票不足整批回滚。每次执行同时校验所有reserved+consumed+本单提议的票/行金额和税额累计，不以创建时校验代替提交复核。成功后proposed→reserved，单据进入under_review，持久工作流冻结目标版本、策略、步骤、金额和证据。记录draft→submitted→under_review事实，返回本地操作回执；操作提交成功不表示审批通过。

撤回的例外：当前仍有公司授权的owner可生成新的withdraw计划清理旧流程，即使旧策略已停用/升级或原审批人撤权，仍允许释放自己的未执行额度；不复用旧计划或放宽其它权限。reject或withdraw在同事务取消未决步骤、失效旧计划/授权、reserved→released，保留旧决定及原件。最后一步approve单据变approved，额度继续reserved，不冒充consumed；F2真正业务完成才可核销。F1没有外部在途动作，不能利用取消导入operation替代撤回报销。

新增GET /v1/finance/expense-claims、listExpenseClaims，供本人/有权人按状态恢复草稿与单据，避免依赖聊天记忆或浏览器本地ID。列表按当前权限及过滤条件计独立total，稳定keyset游标含租户/公司/用户/筛选scope；F0发票available_amount读取实时占用而不覆盖原票面。approver仅可查看实际分派单据及它关联的票据/证据。

## 4 真正 HITL 与可信人工来源

保留平台standard工具确认。工具确认只允许模型调用，不能变成业务审批。平台工具签名始终origin=agent；新通用人工HTTP网关仅接受真实员工登录cookie并过CSRF，拒绝root、API key、act-as、cookie+Authorization、自报origin。

人工网关解析租户当前已安装生效业务包，必须HTTP、显式工具白名单、contextAudience及操作员固定origin/audience许可，不接受任意URL/路径/服务密钥。复用HttpApiClient最终Request签名发送，无模型/沙盒/业务模块导入。人工签发使用独立HumanContextSigner；origin=human不可从请求参数指定。session_id用明确的interactive:<服务端关联ID>表示经当前登录核对的人工交互，审计保存该关联；不伪造聊天会话，也不复用模型SessionRecord绕过检查。签名算法/60秒期限/实际请求绑定及财务在线员工复核不变。

FinancePrincipal增加origin（旧缺失值仅视作agent以兼容F0，未知值拒绝）；防重放绑定包含origin。approve/reject执行必须human，预检可由Skill准备；提交/撤回也要求human明确确认以对应员工提交节点。模型不能通过参数、调用确认或多次执行把agent升级human。

每一步只允许实际分派者、当前有效公司授权、非报销owner、非既有决定重复身份，审核目标版本和step必须一致。中间approve只递增Workflow/Step版本、追加决定，不改变冻结的claim.version；最终approve/reject/withdraw才递增claim.version。计划另外绑定Workflow/Step版本，旧步骤计划不能重用。通过只推进下一步，全部步骤通过才approved；驳回必须有原因。超时只产生阻断/待办，不批准。平台/模型/会话重启后查询同一财务workflow，human登录后继续决定。

功能辅助页提供最小报销/审批入口，查询真实分类、单据、队列、金额、证据和计划影响；明确显示待查验等缺口。先预检并展示服务返回的金额/版本/影响，再由本人选择提交/批准/驳回/撤回；不自动默认批准，不让员工填写API名或JSON。数据变化/409/身份切换清空旧确认，重新查询与预检；模型完全不可用时仍可用。平台网关保持通用，薄页面通过声明式包配置/通用调用接独立财务服务，不把财务存储引入平台。

## 5 预检、计划和本地回执

ActionPlan不可变version=1，摘要包含主体/公司/action/target_version/金额/完整分配及票版本/证据/参数/策略版本/审批step，15分钟有效。getActionPlan每次重算executable与blockers，不改变原计划正文/摘要/到期。预检和执行分别校验；审批计划不再套娃审批。

executeFinanceAction仅plan_id/plan_digest，幂等作用域tenant+actor+company+operationId。一个plan最多执行一次，即使换key也只返回原操作或冲突，不重复决定/占额。不同内容同key409；到期、摘要不符、旧版本、自审、错步骤、已决状态、策略变化、非human均拒绝。

F1动作均为财务库本地事务，执行提交同事务写业务状态/StateEvent/决定/业务Operation/Outbox后返回202；本地Operation可已succeeded，含resources，items为空。持久审批待办由review-queue提供，下一步直接按已提交决定推进，无需模型续跑。通知Outbox持久待派送，未配置通知通道不声称已通知；不把它当业务成功条件。

业务operation独立命名f1op_<uuid>，getOperation显式路由读取，F0导入队列不认领业务操作。业务幂等表独立于F0导入key空间。cancelOperation不撤回已成功本地业务，须走withdraw动作。

## 6 异常与证据

resolveException校验当前版本、真实证据ID及同公司/实际可见范围。attach_evidence仅追加，不能把材料缺失、隔离、摘要不符、超额等hard_block改成成功；ORIGINAL_REQUIRED仍必须F0成功补件关闭。accept_warning/resolve仅对服务识别的可处置warning、授权reviewer、human明确原因/证据开放，保存版本化决定；不得修改发票查验/税务状态。

## 7 验收

真实独立PG：同票并发两个报销只允许额度内提交；多票一票不足整批回滚；旧版本/政策/计划过期拒绝；审批人/owner/租户/公司隔离；驳回/撤回释放一次，批准不核销；换key不重复决定；事件失败全事务回滚；F0→F1迁移保留原件/异常及业务历史。

签名与UI：agent在已获工具确认后仍不能提交/approve；cookie手动路径才human；机器/act-as/跨站拒绝，私钥/服务凭据不进入页面；无模型完成草稿/提交/两步批准或驳回。真实HTTP+PG完整流程、人工来源伪造、重启待审继续、真实浏览器交互分别留证。

门禁：全pytest、ruff F/S108/I/TC、strict mypy、锁文件、相对链接与diff检查。镜像/Compose、真实模型、真实制度/票据/生产分别报告；不借F1宣称F0官方格式或F2支付/记账已完成。


## 8 企业微信/飞书的后续适配边界

内部单据ID、原件、版本和审批事实由独立财务库持有，当前workflow provider固定internal。未来适配器可先同步台账/通知/跳转，再按公司选择单一外部审批权威；不能内部和外部两条链并行各自批准。外部实例ID、人员映射、版本、原始回执、查单与签名回调单独落映射，不覆盖内部原件或历史决定。

飞书已有原生审批实例创建/查询与三方审批中心接入文档：[创建实例](https://open.feishu.cn/document/server-docs/approval-v4/instance/create)、[查询实例](https://open.feishu.cn/document/server-docs/approval-v4/instance/get)、[三方审批集成说明](https://open.feishu.cn/solutions/detail/leave_connector?lang=zh-CN)。三方审批展示不等于把流程主权迁给飞书。企业微信的官方审批文档本轮访问失败，具体模板、权限、接口和回调保障留待真实租户单独核实，不能据此声称已接通。

本轮不实现外部供应商提交/回调；只交付真实内部人工审批、独立记录与可替换的API边界。后续接入必须处理幂等、验签、乱序、旧版本、撤回冲突与真实审批人映射，不能用外部“已通过”字符串直接修改内部状态，更不能当作付款成功。
