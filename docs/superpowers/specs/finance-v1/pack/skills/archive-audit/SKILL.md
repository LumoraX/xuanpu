---
name: archive-audit
description: 查询财务证据链及缺口，按适用清册组织归档，并核对四性检测和存储锁定证据。
version: 0.1.0-design.1
type: composite
tool_names:
  - api__finance__getActionPlan
  - api__finance__getInvoice
  - api__finance__getInvoiceEvidence
  - api__finance__getPayment
  - api__finance__getPosting
  - api__finance__getArchive
  - api__finance__previewFinanceAction
  - api__finance__executeFinanceAction
  - api__finance__getOperation
---
# 归档与审计

先读取[共用执行规则](references/execution-rules.md)。从业务记录和证据接口确认适用归档清册，不自行拼造凭证、回单或标准元素。
action=archive 预检后展示缺件与检测问题，满足资格才执行。
只有 getArchive 的 archived 及其检测、清册摘要和锁定证据一致时报告归档完成。

文件上传、哈希一致或生成 ZIP 均不单独表示合规归档。尚未付款的已入账应付按适用阶段清册判断，不能虚构付款回单凑齐。
归档后出现付款、红冲或冲回，需要追加补充包并保留原包摘要；不能覆盖旧文件后重新称为原始档案。
审计摘要分别列出已核实、未核实和不适用项目，只展示有权访问的脱敏引用。
