# 财务 F1 轻量报销记录与内部审批

当前F14按用户要求仅作内部流转记录，不支付；契约finance `0.15.0-f14`、77项API、7个Skill，见[F14指南](finance-f14.md)及[F14验收](releases/2026-10-23-finance-f14-acceptance.md)。下文保留各阶段事实。

F8阶段按用户要求聚焦发票报销内部流转记录，不做支付。当轮契约finance `0.9.0-f8`、74项API、7个Skill；见[F8指南](finance-f8.md)及[F8本轮验收](releases/2026-10-22-finance-f8-acceptance.md)。下文阶段记录保留原有验证范围。

当前专用资金能力与最新包契约入口见[F5指南](finance-f5.md)（finance `0.6.0-f5`、69项API、7个Skill）。本页保留本阶段操作说明、历史版本和验证证据；当前最终门禁、制品与运行状态见[F5验收记录](releases/2026-10-22-finance-f5-acceptance.md)。

F1在F0独立原件基座上提供费用记录、可选附件/分票、版本修订、真实人员审批、驳回/撤回及持久待办。当前没有预置正式企业报销制度；先配置最小分类和一名实际审批人即可用内部记录模式。源码与样例包可运行，部署和验收边界见[F1验收记录](releases/2026-10-21-finance-f1-acceptance.md)。

## 员工使用

对话入口读取finance-assistant，用户提供事由、金额、日期与可选发票；Skill查询公司/分类后准备草稿与预检。功能辅助页“财务记录与审批”可直接查看和操作，模型服务故障不影响该入口。

员工核对真实金额、版本、证据和预检影响后，点击“本人确认提交”。实际分派的审批人登录，在自己的待办里核对资料后选择批准或驳回；申请人不能自审。审批通过仍是内部业务记录，不表示发票已查验、资金已支付或会计已入账。内部记录可无结构化发票，资料缺口不会被抹掉。

已驳回/撤回记录可以修订重提。审批完成但尚无后续执行的记录也可由申请人正式撤回；保留历史，释放占用，不删除原件。审批超时、策略变化、审批人撤权会阻断批准，申请人仍可按当前授权撤回清理。没有通知通道时，待办持久保存但不声称已发送消息。

## 最小启用配置

基础凭据和独立数据库配置沿用[F0接入指南](finance-f0.md)。[轻量示例配置](../../docker/finance/internal-record.example.yaml)默认关闭，使用明确的示例人员标识。操作员替换tenant/company/user为平台实际有效员工，赋予公司collector/approver/reviewer角色，将enabled改为true后重启财务服务；不能直接把示例人当真实审批人。

expense_policies缺省空列表时仍可使用F0，新报销写入返回“公司未启用报销制度”。internal_record必须显式require_verified_invoice=false；该设置允许内部记录，不能授予正式付款、税务或入账资格。分类可只设一个“其他费用”，cost_centers可以为空，require_cost_center=false，至少一个非申请人的实际审批步骤。

policy.version为不可变制度版本：改变正文必须升版本。同一配置重启不会使待审丢失；实际策略启停/更改会使旧计划失效，重新启用旧版本也不会复活旧审批。record_mode随单据版本保存；模式改变要修订草稿、重新预检和审批。金额是两位小数字符串，日期YYYY-MM-DD，不接受浮点猜测。

invoice_backed模式要求完整分配、票据正常和默认查验passed；当前官方查验未配置，不能用合成票冒充正式查验。internal_record没有分配时可直接提交；一旦选择分配，仍须完整覆盖总额，累计票/行金额与税额不能越限，整批提交原子占额。

## Skill 与 API

F1新增操作在后续包中继续兼容；F3阶段包为58项操作、7个Skill，付款与记账见[F2指南](finance-f2.md)。模型可创建/修订草稿与生成计划，工具签名始终origin=agent。executeFinanceAction只接受origin=human，不能靠模型参数或工具批准升级来源。

人工页面经通用平台接口POST /v1/integration-actions/{package}/{connector}/{operation}，请求为{arguments,confirmed}。写操作必须confirmed=true；只允许当前员工cookie与CSRF，拒绝root、API key、act-as和混合凭据。平台只访问当前租户已安装、生效包中显式声明的HTTP操作，按最终报文签名，服务密钥不进入浏览器；不会调用模型或加载财务业务代码。

人工签名使用interactive:<服务端关联ID>，单独记录审计，不伪造聊天会话。财务同时核对短时签名、请求摘要、防重放、在线员工与当前公司授权。

previewFinanceAction返回不可变计划及实时blockers；executeFinanceAction只传plan_id/plan_digest/幂等键。结果不明时沿用同一请求与键查询或重试；409后重新读取和预检。业务Operation成功仅表示该本地事务成功，审批状态要另查getExpenseClaim。多步中间批准不改变冻结claim版本，只推进workflow/step；全部步骤实际完成后才approved。

## 存储与后续集成

单据、原件、策略版本、流程步骤、决定、额度、操作回执和通知Outbox均在独立财务PG及原件存储。平台只提供身份、固定文件版本与入口，没有跨库外键或共享存储凭据。移除业务包入口、停用服务不删除财务记录。

企业微信/飞书未来可作为通知、展示或审批执行适配器；公司只能选择一个审批权威。外部实例、人员映射、回执及验签单独记录，不能用一个“已通过”字符串覆盖内部状态。本期尚未实现这两个适配器，真实支付、ERP、税务和正式归档尚未接入；通用付款/记账接口及隔离执行见[F2指南](finance-f2.md)。

F4阶段已补原生站内提醒、SQL权限分页、配置/运营投影和local配套恢复，见[F4指南](finance-f4.md)与[剩余缺口台账](finance-gap-register.md)。F4阶段为61项API、7个Skill；F5阶段为69项API、7个Skill，专用资金见[F5指南](finance-f5.md)。

F6阶段新增正式接入准备检查与可重复隔离验收，当轮70项API、7个Skill；见[F6指南](finance-f6.md)和[正式接入清单](finance-provider-acceptance.md)。原阶段正式能力未验边界继续保留。

当前内部记录的摘要检索、专用部署配置与上线准备见[F9指南](finance-f9.md)，原阶段验收事实仍以对应记录为准。
