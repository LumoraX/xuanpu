---
name: finance-review
description: 查询本人有权审核的财务单据和异常，展示规则及证据，并执行明确授权的审核决定或异常处置。
version: 0.1.0-design.1
type: composite
tool_names:
  - api__finance__getActionPlan
  - api__catalog__getInvoiceAllocation
  - api__finance__listReviewQueue
  - api__finance__getExpenseClaim
  - api__finance__getPayable
  - api__finance__getInvoice
  - api__finance__getInvoiceEvidence
  - api__finance__listExceptions
  - api__finance__resolveException
  - api__finance__previewFinanceAction
  - api__finance__executeFinanceAction
  - api__finance__getOperation
---
# 财务审核与异常

先读取[共用执行规则](references/execution-rules.md)。从 listReviewQueue 读取实际授权待审步骤，不能根据职位称呼推定审批权。
队列 target_type=action_plan 时用 getActionPlan 读取原计划，以 action_plan 为审核目标；不能把它当成报销单 ID。
读取对应单据版本、发票证据和异常，分别展示硬阻断、制度阻断与可授权接受的告警。
有权人明确决定后，以 workflow_step_id、目标版本和必要理由预检 approve／reject；执行仍由服务端验证职责分离。

未获得具体业务决定时仅形成审核建议。调用确认卡不能代替经办人、主管或财务的业务审批。
resolveException 可以附证据、接受可豁免告警或在事实条件满足后关闭；不得修改查验／支付／ERP 权威结果或接受不可豁免硬约束。
“这批全部通过”仍需逐项取得版本和计划，报告部分拒绝／冲突，不把一项批准当作整批授权。
