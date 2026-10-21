---
name: expense-reimbursement
description: 根据员工真实费用和发票建立本人报销草稿、费用分摊与金额分配，并在预检通过后提交或撤回。
version: 0.1.0-design.1
type: composite
tool_names:
  - api__catalog__listExpenseCategories
  - api__catalog__listCostCenters
  - api__catalog__getInvoiceAllocation
  - api__finance__getInvoice
  - api__finance__getInvoiceEvidence
  - api__finance__createExpenseClaim
  - api__finance__getExpenseClaim
  - api__finance__updateExpenseClaim
  - api__finance__createInvoiceAllocation
  - api__finance__releaseInvoiceAllocation
  - api__finance__previewFinanceAction
  - api__finance__executeFinanceAction
  - api__finance__getOperation
---
# 员工报销

先读取[共用执行规则](references/execution-rules.md)。本人身份由服务端确定；不替用户编造消费事由、项目、部门或票据。
先查询 listExpenseCategories／listCostCenters 取得真实分类和归属；重新进入用 getInvoiceAllocation 读取已有分配金额与版本。
createExpenseClaim 只建草稿。费用分类可建议，缺少实际事由或受控分类标识时补问，不编造主数据。
getExpenseClaim 取得当前版本后修改，InvoiceAllocation 仅提议分配；不要宣称草稿已经占用发票额度。

提交使用 action=submit_claim，撤回用 withdraw，均预检后执行；服务端在提交事务内校验并占用额度。
预检阻断时展示具体缺口；有权人批准另由 finance-review 处理。
草稿分配可通过 releaseInvoiceAllocation 移除，已提交保留额度不能经此接口绕过撤回或驳回流程。
报销通过不表示已付款，真实付款／凭证进度由 posting-reconciliation 核对。
