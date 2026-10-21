---
name: expense-reimbursement
description: 根据费用事实准备本人报销记录与真实分票，查询进度、修订草稿及准备提交或撤回计划。
version: 0.5.0-f4
entry: false
type: composite
tool_names:
- api__finance__listFinanceNotifications
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

先查询有权公司、费用分类及可选归属，只在必需事实不明确时追问。金额用两位小数字符串，日期YYYY-MM-DD；身份、合计、状态由服务端确定。
原件交invoice-intake归集，附件evidence_ids只能用真实财务SourceReceipt，不能拿平台file_id替代。PDF可作内部附件，不能伪造结构化发票或验真。
createExpenseClaim只建草稿；修订带最新expected_version。internal_record允许没有分票；invoice_backed及任何存在分配的单据都须完整覆盖总额，预检不占额，实际提交才原子保留。
准备submit_claim/withdraw预检后，解释材料缺口、阻断、版本、金额及影响，交本人在人工入口决定，不调用executeFinanceAction。
internal_record一律不取得外部付款或记账资格，simulation也不例外。业务approved仍不能替代外部专用批准；外部动作交posting-reconciliation准备。
网络结果不明用原幂等键、计划和getOperation恢复，再查原单据实际状态，不换键重复提交。

F4原生提醒由listFinanceNotifications读取当前来源，未读计数独立于当前页；提醒已读不表示审批或渠道成功。模型不调用readFinanceNotification；待审/未知应恢复原目标和原计划，不从提醒正文代批或重发。站内提醒不等于飞书、企业微信或邮件送达。
