---
name: posting-reconciliation
description: 为已批准报销或应付准备付款、记账和冲回计划，恢复专用审批并按真实回执解释未知结果。
version: 0.16.0-f15
entry: false
type: composite
tool_names:
- api__finance__getFinanceCapabilities
- api__finance__listFinanceNotifications
- api__finance__listLegalEntities
- api__finance__listIntegrations
- api__finance__getSettlementOptions
- api__finance__getExpenseClaim
- api__finance__getPayable
- api__finance__getInvoice
- api__finance__getInvoiceEvidence
- api__finance__previewFinanceAction
- api__finance__getActionPlan
- api__finance__listActionPlans
- api__finance__getOperation
- api__finance__listPayments
- api__finance__getPayment
- api__finance__listPostings
- api__finance__getPosting
- api__catalog__listPayeeAccounts
- api__finance__getFundingCase
- api__finance__listFundingCases
- api__finance__getFundingOptions
---
# 付款、记账与结果核对

先调用getFinanceCapabilities；读取失败或字段非法时停止并说明。profile=internal_records时只处理内部收件元数据、普通报销草稿、记录查询和人工动作预检；不调用应付、资金、支付、记账、查验、红字、正式档案或通用接入检查接口。实例能力不是个人授权、审批或上线验收。

## internal_records 分支

profile=internal_records：此实例不处理付款、记账或外部对账。说明内部approved仅是审批完成，交财务入口查询普通报销记录；本Skill停止，不读取执行/资金规则或调用下文general工具。

## general 分支

以下仅在profile=general时执行。

先读[执行规则](references/execution-rules.md)。查目标单据、getSettlementOptions及通道状态；经办资格、开放期间和账套以公司配置为准。没有通道或真实查验时说明阻断，不推定已经具备正式执行能力。
付款先listPayeeAccounts(legal_entity_id,document_kind,document_id)，从已核实脱敏账户选择完整payee_ref；execute_payment只含amount和payee_ref。记账submit_posting只含amount/accounting_period，可附evidence_ids；账套由服务固定，不临时填入其它账套。
reverse_posting以真实Posting为目标，使用其最新version、reversal_available_amount和开放期间，参数amount/accounting_period/reason，可附证据。冲回新建关联凭证，不能覆盖原Posting。
原业务approved只是前提。preview生成固定金额、版本、环境、账户/账套和摘要的原计划及专用工作流；查询listActionPlans/getActionPlan恢复同一计划，待实际分派人finance-review处理。原经办人随后在人工入口明确执行，模型不调用executeFinanceAction。
accepted/awaiting_provider不是成功；outcome_unknown仍占金额，不能换计划或幂等键再付款。getOperation与getPayment/getPosting查询真实进展，资金成功须transaction_id，入账成功须ledger_id/accounting_period/voucher_id及渠道证据。
人工reconcileOperation只排同一请求号查单；未发工作才可取消。两者由真实员工人工入口发起，本Skill不执行。not_found也不能证明可以重发。
simulation=true只属于隔离测试单据与回执，不能写入正式记录；当前无真实供应商上线，不声称到账、正式入账、税务确认或归档。

F4原生提醒由listFinanceNotifications读取当前来源，未读计数独立于当前页；提醒已读不表示审批或渠道成功。模型不调用readFinanceNotification；待审/未知应恢复原目标和原计划，不从提醒正文代批或重发。站内提醒不等于飞书、企业微信或邮件送达。

F5专用资金先读[资金规则](references/funding-rules.md)。材料先真实收件并读取source回执；申报不是核实，模型仅准备草稿/预检，实际决定交员工。资金业务使用funding-cases专用入口，不伪装普通应付。
