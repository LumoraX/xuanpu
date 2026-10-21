---
name: invoice-intake
description: 接收受权文件版本，归集发票原件、读取票面字段及来源、发起查验并解释缺件与异常。
version: 0.1.0-design.1
type: composite
tool_names:
  - api__finance__listLegalEntities
  - api__finance__listIntegrations
  - api__finance__importInvoices
  - api__finance__listInvoices
  - api__finance__getInvoice
  - api__finance__getInvoiceLines
  - api__finance__getInvoiceFields
  - api__finance__getInvoiceEvidence
  - api__finance__verifyInvoice
  - api__finance__getOperation
  - api__finance__listExceptions
---
# 发票归集与查验

先读取[共用执行规则](references/execution-rules.md)。仅引用受权文件 ID、revision 和 SHA-256，明确目标公司后提交 importInvoices。
读取每项结果，parsed／duplicate_linked 与 awaiting_original／quarantined／failed 分别汇报。
只有截图或 PDF 时按适用链路提示补齐原件，不能用 OCR 结果伪造 XML 或查验通过。

用 getInvoice 读取汇总，明细和原始字段分别分页读取 getInvoiceLines／getInvoiceFields，证据来自 getInvoiceEvidence。
显示票面金额、剩余业务额度和查验状态各自含义，不能把未核实状态概括为“有效发票”。
用户要求查验时先检查税务通道能力，再以当前版本调用 verifyInvoice，按真实 operation 和查验记录报告。
F0 发布不开放 verifyInvoice；无实际通道时如实说明未查验。
同一票多来源只建立来源链接；内容冲突进入异常，不覆盖旧票面。
