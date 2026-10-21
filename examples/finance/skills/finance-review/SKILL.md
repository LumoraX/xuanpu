---
name: finance-review
description: 查询实际分派的内部审批待办和证据，准备审核计划供本人决定。
version: 0.2.0-f1.1
entry: false
type: composite
tool_names:
- api__finance__listLegalEntities
- api__finance__listReviewQueue
- api__finance__getExpenseClaim
- api__finance__getInvoice
- api__finance__getInvoiceEvidence
- api__finance__getInvoiceLines
- api__finance__previewFinanceAction
- api__finance__getActionPlan
- api__finance__listExceptions
- api__finance__getOperation
---
# 内部审批

从listReviewQueue获取当前员工实际分派的步骤，查询单据、证据和流程版本。不能替他人审批，不能自审；reviewer公司查看权不是任意审批权。
只针对当前workflow_step_id与target_version准备approve/reject预检，驳回须有明确原因。展示缺材料/not_checked/unverified、计划blockers与金额，不补造制度或批准结论。
将计划交给当前员工在“功能 → 报销记录与内部审批”查看并决定。禁止模型执行executeFinanceAction，禁止把平台工具批准写成财务审批。
一步批准只推进下一步，全部实际步骤通过才approved；待办重启后仍在独立财务库。超过时限或通知未派送不会自动批准。
