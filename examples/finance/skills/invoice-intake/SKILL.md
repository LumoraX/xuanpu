---
name: invoice-intake
description: 使用受权固定版本发票原件，归集、查询持久进度并处理补件待办。
version: 0.1.0-f0.1
type: composite
tool_names:
  - api__finance__listLegalEntities
  - api__finance__listIntegrations
  - api__finance__importInvoices
  - api__finance__getOperation
  - api__finance__cancelOperation
  - api__finance__listInvoices
  - api__finance__getInvoice
  - api__finance__getInvoiceLines
  - api__finance__getInvoiceFields
  - api__finance__getInvoiceEvidence
  - api__finance__listExceptions
---
# 发票归集与补件

先读取 [执行规则](references/execution-rules.md)。只使用用户可访问文件的 file_id、revision、sha256，明确公司后调用 importInvoices。
202 表示已持久受理；继续 getOperation，按每项 parsed/duplicate_linked/awaiting_original/quarantined/failed/cancelled 汇报。
summary 各分类合计必须等于 total；partial 应明确成功项和未完成项，不能概括为全部完成。
票面、明细、原始字段和证据分别调用对应接口，分页跟随 next_cursor；total 是过滤后的总数。
只有 PDF、OFD 或图片时保留待补件，说明需要合适原件；不能生成 XML 冒充原件。
补件先 listExceptions 取得当前 open 异常 ID/version，再调用 importInvoices，同传 source_exception_id、expected_exception_version 和新文件引用。
失败时原待办仍存在；成功解析后服务负责关联并关闭，不调用不存在的“通过审批”工具。
用户取消时先 getOperation 取得当前 version，用独立幂等键取消；已完成项仍保留。
