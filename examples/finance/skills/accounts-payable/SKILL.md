---
name: accounts-payable
description: 从真实供应商与合同、订单或验收目录准备原生应付、发票分配及提交或撤回计划。
version: 0.3.0-f2.1
entry: false
type: composite
tool_names:
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
---
# 采购应付

先读[执行规则](references/execution-rules.md)。查询有权公司，以listBusinessDocuments(kind=supplier)选择供应商，再按contract/order/acceptance查询至少一项已取得依据。所有引用保留kind/source/id/version/sha256，不能把聊天叙述登记成受控资料。
createPayable需legal_entity_id、supplier_ref、正amount、due_date、business_refs、reason，可附真实evidence_ids。只建本人草稿，不批准、不付款。修订使用当前expected_version与完整draft，公司和simulation由服务端保持。
发票分配document_kind=payable，固定invoice及单据版本；提议不占额，实际提交才原子保留。缺原件交invoice-intake，缺采购依据只补资料，不拼造外部引用。
准备submit_payable或withdraw预检，展示实际金额、版本、匹配缺口和blockers，交真实员工人工入口决定。应付批准与后续专用付款/记账批准分开。
已有发送、未知或成功外部事实不能靠撤回释放额度。后续付款与凭证核对交posting-reconciliation；账户变更走受控主数据，不在备注中临时替换银行账号。
