---
name: posting-reconciliation
description: 为已批准报销或应付准备付款、记账和冲回计划，恢复专用审批并按真实回执解释未知结果。
version: 0.4.0-f3
entry: false
type: composite
tool_names:
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
---
# 付款、记账与结果核对

先读[执行规则](references/execution-rules.md)。查目标单据、getSettlementOptions及通道状态；经办资格、开放期间和账套以公司配置为准。没有通道或真实查验时说明阻断，不推定已经具备正式执行能力。
付款先listPayeeAccounts(legal_entity_id,document_kind,document_id)，从已核实脱敏账户选择完整payee_ref；execute_payment只含amount和payee_ref。记账submit_posting只含amount/accounting_period，可附evidence_ids；账套由服务固定，不临时填入其它账套。
reverse_posting以真实Posting为目标，使用其最新version、reversal_available_amount和开放期间，参数amount/accounting_period/reason，可附证据。冲回新建关联凭证，不能覆盖原Posting。
原业务approved只是前提。preview生成固定金额、版本、环境、账户/账套和摘要的原计划及专用工作流；查询listActionPlans/getActionPlan恢复同一计划，待实际分派人finance-review处理。原经办人随后在人工入口明确执行，模型不调用executeFinanceAction。
accepted/awaiting_provider不是成功；outcome_unknown仍占金额，不能换计划或幂等键再付款。getOperation与getPayment/getPosting查询真实进展，资金成功须transaction_id，入账成功须ledger_id/accounting_period/voucher_id及渠道证据。
人工reconcileOperation只排同一请求号查单；未发工作才可取消。两者由真实员工人工入口发起，本Skill不执行。not_found也不能证明可以重发。
simulation=true只属于隔离测试单据与回执，不能写入正式记录；当前无真实供应商上线，不声称到账、正式入账、税务确认或归档。
