---
name: expense-reimbursement
description: 根据事由、费用与可选发票准备本人报销记录，查询进度和修订草稿。
version: 0.2.0-f1.1
entry: false
type: composite
tool_names:
- api__finance__listLegalEntities
- api__finance__createExpenseClaim
- api__finance__listExpenseClaims
- api__finance__getExpenseClaim
- api__finance__updateExpenseClaim
- api__finance__createInvoiceAllocation
- api__finance__releaseInvoiceAllocation
- api__finance__previewFinanceAction
- api__finance__getActionPlan
- api__finance__getOperation
- api__catalog__listExpenseCategories
- api__catalog__listCostCenters
- api__catalog__getInvoiceAllocation
---
# 报销记录

先查询有权公司、分类及可选归属；只在公司/必需费用信息不明确时追问。金额必须是两位小数字符串，日期YYYY-MM-DD；身份、合计、状态由服务端确定。
需要原件时委派invoice-intake，使用真实SourceReceipt作为evidence_ids，不把平台file_id当财务证据ID。PDF可留作内部记录附件，不能伪造结构化发票或验真。
createExpenseClaim只建草稿；修订带最新expected_version。internal_record允许不分票；invoice_backed必须完整分配，任何模式存在分配都必须覆盖总额，预检不占额。
可准备submit_claim/withdraw预检并解释金额、材料缺口、阻断、版本与影响；实际执行由员工进入人工页面确认。不要调用executeFinanceAction；模型来源即使工具确认也会拒绝。
查询getOperation仅说明本地命令结果，再查getExpenseClaim了解真实审批状态。网络结果不明先用原幂等键/计划查询，不换键重新构造业务动作。
