---
name: invoice-intake
description: 归集受权固定版本发票原件，查询持久进度并为报销或应付处理补件。
version: 0.4.0-f3
entry: false
type: composite
tool_names:
- api__finance__verifyInvoice
- api__finance__getInvoiceCompliance
- api__finance__listInvoiceVerifications
- api__finance__getRedLetterCase
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

先读[执行规则](references/execution-rules.md)。只用用户有权文件的file_id、revision、sha256；明确公司后importInvoices，不生成XML冒充原件。
202表示持久受理；getOperation按项汇报parsed、duplicate_linked、awaiting_original、quarantined、failed、cancelled。summary合计等于total，partial应说明各项缺口。分页跟随next_cursor，total是过滤后的总数。
票面、明细、原始字段和证据分别查询；仅PDF/OFD/图片时保留待补原件。解析成功不等于not_checked已变查验passed。
补件先listExceptions取得open异常ID/version，importInvoices同时携带source_exception_id、expected_exception_version和新固定文件引用。失败保留待办，成功由服务关联关闭。
用户取消F0导入时，先getOperation取得version，再取消未完成项；已完成原件和票据仍保留。f2op_付款/记账操作的取消与核对必须由真实员工在人工入口发起，本Skill不代办。

F3查验和税务动作使用getInvoiceCompliance.version，原getInvoice.version仍是票面内容/分配版本。verifyInvoice只受理实际外查；pending/过期/不可用不是passed。F3原action_plan仍按当前实际步骤预检，实际审批及执行交人工入口。
