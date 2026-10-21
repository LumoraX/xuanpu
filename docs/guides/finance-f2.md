# 财务 F2 应付、付款及记账接口

F2保持财务服务、数据库、原件、密钥及备份独立。玄圃提供身份、对话和人工入口；财务保存业务规则、审批、执行计划、结果和证据。首期没有选定付款或记账供应商，采用可替换HTTP协议，正式执行默认关闭。企业微信/飞书报销适配器尚未实现。

## 员工与财务人员

对话可以创建报销/应付草稿、查材料及状态、准备计划。辅助入口“财务记录与审批”支持同样的数据和权限，模型不可用时仍可操作。应付须从受控目录选供应商和采购资料；付款账户从已核实目录选择，只显示脱敏值。

内部记录审批与付款、记账是不同授权。`internal_record`即使批准，也不能取得付款或入账资格；隔离测试同样不放宽该限制。应付或`invoice_backed`报销须先完成发票分配及业务审批，再由配置的付款经办/记账经办提出具体金额计划。两类经办岗位分离。

专用审批人看到的是同一不可变计划：目标版本、金额、账户/账簿/期间、策略和通道版本、有效期与摘要。申请人、经办人不能自审。审批人必须通过真实员工入口批准，批准后原经办人再确认执行；工具确认、原单据批准、模型输出均不替代这两个人工决定。计划默认24小时、最多72小时有效，人员撤权、资料/策略/通道变化会阻断旧计划；重新启用旧配置不会复活旧授权。

执行返回202仅代表财务库已持久受理。付款在取得精确匹配的渠道交易及证据后才为成功；记账必须取得匹配账簿、期间和凭证的证据。支持部分付款、部分入账、部分冲回，资金和凭证余额分别计算。冲回保留原凭证及关联，须重新走专用审批。

## 结果未知与恢复

所有发送先持久保存固定请求号和请求摘要。超时、断线、错误归属/金额、缺失成功证据、发送时停机均保留占额，进入`outcome_unknown`；恢复只查原请求，不自动重发。原请求号用于`Idempotency-Key`，外部系统必须支持幂等和查单，否则预检、执行、发送均关闭。

自动查单按1、5、15、60分钟退避，达到次数后产生人工核对异常。人工“核对”只发起只读查单；不能把未知手改为已付。发送前取消释放该动作占额，发送后取消仍需核对，不能回滚渠道事实。已经成功或仍在途的动作会阻断单据撤回和发票额度释放。

`enabled`允许新执行；`draining`、`read_only`、`disabled`停止新发送，在途查单及验签回调继续。禁用时员工业务接口关闭，数据保留。实例锁丢失会取消全部后台任务（F4当前为四个）；新实例按持久事实恢复。停用服务/移除包不会删除财务历史。

## 配置和隔离模拟

沿用[F0配置](finance-f0.md)和[F1制度配置](finance-f1.md)，新增字段均缺省为空或关闭：

- `settlement_policies`：每公司版本化策略、付款/记账经办、三种专用审批链、账簿、开放期间及固定通道版本。
- `business_documents`：有来源、版本、摘要和授权范围的供应商、合同、订单、验收快照。
- `payee_accounts`：已核实员工或供应商账户，主体归属、脱敏展示及供应商账户令牌。
- `settlement_providers`：公司范围、provider/configuration_version、协议版本、固定origin、独立token及回调密钥文件、幂等/查单能力。
- `execution_simulation`：缺省false。仅development/test允许true，实际库名必须以`finance_test`开头，原件使用`simulation/`独立前缀。数据库环境标记持久化，禁止切换或混用旧正式单据。

正式环境要求HTTPS、实际有效发票查验及原件。本期未接官方验真和真实渠道，因此不能据隔离测试开通正式付款。模拟用合成票和测试人员，所有新单据、计划、操作、凭证保留simulation标记。参考[关闭状态的配置片段](../../docker/finance/settlement.example.yaml)，替换真实人员和受控资料后才能显式启用。回调密钥启动时要求至少32字节，凭据文件相对路径以配置目录为根。

## 通用渠道协议

提交固定`POST /v1/requests`，查单固定`GET /v1/requests/{external_request_id}`。Bearer token由服务注入，不进入浏览器或工具参数；禁止重定向，响应上限1MiB，超限仅保留上限内字节并标记拒绝，拒绝重复JSON字段。

请求携带`protocol_version="1"`、tenant/company、operation_id/action、external_request_id、provider/adapter_version/configuration_version、simulation、两位小数字符串amount、CNY、target的id/type/version、plan_digest及payload_hash。付款另含payee_ref/provider_account_id；记账含ledger_id/accounting_period；冲回还含original_voucher_id/reason。响应必须完整回显固定范围，status为accepted/pending/succeeded/failed/unknown/not_found。

付款成功另需external_order_id、transaction_id及evidence；记账/冲回成功另需ledger_id、accounting_period、voucher_id及evidence。证据含id、sha256、media_type、object_ref。原响应字节保存在独立库，外部对象引用只是渠道证据定位，不等同于财务已保存原件或法定归档。

回调为`POST /v1/provider-callbacks/{provider}/{configuration_version}`，与员工认证分开。发送`X-Finance-Timestamp`、`X-Finance-Event-Id`、`X-Finance-Key-Id`、`X-Finance-Signature`。签名为`sha256=`加HMAC-SHA256(timestamp + '.' + event_id + '.' + raw_body)十六进制，时差±300秒。固定路径选择密钥；body不能选择其他公司/密钥。事件去重，同事件异内容冲突。合法回调只唤起查单，不能直接推进成功；原字节和事件保全于独立inbox。

## Skill 与API边界

F2交付38项操作和6个Skill；当前F3发布包扩至58项操作和7个Skill，见[F3指南](finance-f3.md)；新增`accounts-payable`与`posting-reconciliation`。模型可准备外部计划、查询付款和凭证，但所有Skill均不拥有`executeFinanceAction`工具。该操作仍在连接器显式声明，供已确认的人工cookie+CSRF入口调用；财务验签再次要求origin=human。

`getSettlementOptions`返回当前员工可用的账簿、开放期间及动作许可，目录查询只返回受权资料和脱敏账户。`listActionPlans`可恢复原执行计划。`reconcileOperation`和取消均要求expected_version、幂等键和真实人工。正式业务、真实供应商及上线证据见独立验收记录，不以接口200或模型回答代替。

当前F4已补原生站内提醒、SQL权限分页、配置/运营投影和local配套恢复，见[F4指南](finance-f4.md)与[剩余缺口台账](finance-gap-register.md)。历史阶段的工具数量以对应阶段为准，当前包61项API、7个Skill。
