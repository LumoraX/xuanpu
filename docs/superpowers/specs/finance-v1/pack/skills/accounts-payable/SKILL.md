---
name: accounts-payable
description: 根据真实供应商、合同、订单与验收材料起草应付及发票分配，提交审核并组织分期付款需求。
version: 0.1.0-design.1
type: composite
tool_names:
  - api__catalog__listBusinessDocuments
  - api__catalog__getInvoiceAllocation
  - api__finance__listIntegrations
  - api__finance__getInvoice
  - api__finance__getInvoiceEvidence
  - api__finance__createPayable
  - api__finance__getPayable
  - api__finance__updatePayable
  - api__finance__createInvoiceAllocation
  - api__finance__releaseInvoiceAllocation
  - api__finance__previewFinanceAction
  - api__finance__executeFinanceAction
  - api__finance__getOperation
---
# 采购应付

先读取[共用执行规则](references/execution-rules.md)。供应商、合同、订单和验收引用必须来自实际原生或同步记录，并固定来源版本与摘要。
用 listBusinessDocuments 查询真实引用，已有分配用 getInvoiceAllocation 读取。
无法取得真实引用时只说明待补资料，不把自然语言描述伪装成外部单据。
创建应付草稿，展示匹配断点与付款需求，预付款、分期和委托收款按对应场景规则判断。

提交应付用 submit_payable，撤回用 withdraw；指定当前版本预检并按实际资格执行。
发票分配只提议，服务端提交时占用。付款需求记录与审批通过分开，真实付款交给 posting-reconciliation。
用户要求变更银行账号时转受控账户主数据变更；不能在应付备注或付款参数中临时替换账户。
