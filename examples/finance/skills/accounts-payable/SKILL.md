---
name: accounts-payable
description: 从真实供应商与合同、订单或验收目录准备原生应付、发票分配及提交或撤回计划。
version: 0.9.0-f8
entry: false
type: composite
tool_names:
- api__finance__listFinanceNotifications
- api__finance__listLegalEntities
- api__finance__listIntegrations
- api__finance__createPayable
- api__finance__listPayables
- api__finance__getPayable
- api__finance__updatePayable
- api__finance__getInvoice
- api__finance__getInvoiceEvidence
- api__finance__createInvoiceAllocation
- api__finance__releaseInvoiceAllocation
- api__finance__previewFinanceAction
- api__finance__getActionPlan
- api__finance__getOperation
- api__catalog__listBusinessDocuments
- api__catalog__getInvoiceAllocation
- api__finance__createFundingCase
- api__finance__listFundingCases
- api__finance__getFundingCase
- api__finance__updateFundingCase
- api__finance__getFundingOptions
- api__finance__importSupportingMaterials
---
# 采购应付

先读[执行规则](references/execution-rules.md)。查询有权公司，以listBusinessDocuments(kind=supplier)选择供应商，再按contract/order/acceptance查询至少一项已取得依据。所有引用保留kind/source/id/version/sha256，不能把聊天叙述登记成受控资料。
createPayable需legal_entity_id、supplier_ref、正amount、due_date、business_refs、reason，可附真实evidence_ids。只建本人草稿，不批准、不付款。修订使用当前expected_version与完整draft，公司和simulation由服务端保持。
发票分配document_kind=payable，固定invoice及单据版本；提议不占额，实际提交才原子保留。缺原件交invoice-intake，缺采购依据只补资料，不拼造外部引用。
准备submit_payable或withdraw预检，展示实际金额、版本、匹配缺口和blockers，交真实员工人工入口决定。应付批准与后续专用付款/记账批准分开。
已有发送、未知或成功外部事实不能靠撤回释放额度。后续付款与凭证核对交posting-reconciliation；账户变更走受控主数据，不在备注中临时替换银行账号。

F4原生提醒由listFinanceNotifications读取当前来源，未读计数独立于当前页；提醒已读不表示审批或渠道成功。模型不调用readFinanceNotification；待审/未知应恢复原目标和原计划，不从提醒正文代批或重发。站内提醒不等于飞书、企业微信或邮件送达。

F5专用资金先读[资金规则](references/funding-rules.md)。材料先真实收件并读取source回执；申报不是核实，模型仅准备草稿/预检，实际决定交员工。资金业务使用funding-cases专用入口，不伪装普通应付。
