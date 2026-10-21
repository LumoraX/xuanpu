---
name: posting-reconciliation
description: 按业务批准及专用授权执行付款、ERP入账和税务确认，查询真实回执并核对未知或冲突结果。
version: 0.1.0-design.1
type: composite
tool_names:
  - api__finance__getActionPlan
  - api__catalog__listPayeeAccounts
  - api__finance__listIntegrations
  - api__finance__getExpenseClaim
  - api__finance__getPayable
  - api__finance__getInvoice
  - api__finance__getInvoiceEvidence
  - api__finance__previewFinanceAction
  - api__finance__executeFinanceAction
  - api__finance__getOperation
  - api__finance__getPayment
  - api__finance__getPosting
  - api__finance__getRedLetterCase
---
# 执行与对账

先读取[共用执行规则](references/execution-rules.md)，再查目标公司的通道能力及当前业务记录。
付款用 execute_payment，ERP 入账／冲回用 submit_posting／reverse_posting，税务用途／入账确认用 confirm_usage／confirm_tax_entry。
红字对方确认用 confirm_red_letter。各动作分别预检与授权，不能从报销通过推导付款或税务确认权。

入账前核对账套和期间；期间关闭时展示冲突，不自行改期间。付款前用 listPayeeAccounts 查询该单据允许使用的已核实账户引用。
付款引用已核实账户版本，不使用聊天提供的新账号。
202 仅表示持久受理；读取 operation 的 resources 和业务结果，再查 getPayment／getPosting／getRedLetterCase 核对权威记录。
已受理未成功、部分支付、失败、退回和 outcome_unknown 分别说明；未知结果保留原请求号，转核对，不换 key 重付。
ERP 入账、税务入账确认、用途确认分别解释。部分红冲按行级影响和剩余额度处理，不笼统判整票作废。
红字确认到期先读取通道核实结果，不替用户确认，不根据本地 72 小时计时器直接解锁。
