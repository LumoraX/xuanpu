# 财务 F2 付款、记账通用接口与持久结果闭环

用户已授权按既有完整财务设计继续F2，明确尚未选供应商，先完成通用接口与隔离闭环。基线4bc7010，复用干净检出xuanpu-finance-f0，新分支feat/finance-f2；未授权真实出款或生产记账。本文件细化已确认设计，不重复请求实现许可。

## 1 范围与方案

实现原生应付及受控业务/账户目录；报销/应付的付款、记账与冲回计划、专用人工审批、执行授权、独立持久队列、HTTP发送/查单/验签回调、结果未知与取消、明细和累计状态查询。沿用F1原件、独立PG、平台cookie人工入口与模型agent身份隔离。外部正式账簿和资金结果以渠道回执为准。

选择成熟httpx/Pydantic/asyncpg/HMAC组件及现有服务内事务队列。直接同步HTTP会在超时/重启时失去发送事实；额外工作流引擎/消息系统会增加独立存储边界，本阶段用单服务有界租约队列即可。F2不扩展税务/红字/正式归档、真实供应商适配或通知发送器。

## 2 独立数据与环境

所有新表及队列、Inbox、回执、事件仍在独立财务库，不新增平台业务表/财务库凭据。保留legacy finance_expense_claim作为通用单据聚合，新增document_kind=expense_claim/payable，原FK继续有效；对外严格分开报销/应付API，不允许从旧报销接口读改应付。原单据和历史不删除，不重标既有环境。

实例execution_simulation缺省false；simulation=true只能development/test、独立finance_test库及独立对象根使用，服务库持久mode marker阻止切换/混用。每个新建业务单据固定simulation标记，修订不改变；旧F1单据缺省false，不能因新配置变成模拟。provider配置、策略、请求、回执、单据的simulation必须一致。F1 internal_record一律不取得外部执行资格，包括模拟执行；隔离闭环显式创建invoice_backed模拟单据与测试发票，允许尚未查验只限这一模拟环境。

正式执行仍要求invoice_backed、正常且有未过期查验证据的实际有效passed、完整原件/分配、有效业务批准与专用动作批准；不从F1 require_verified_invoice=false推导正式资格。本期没有实际查验供应商，正式默认不可用。模拟回执明确simulation=true，不能进入正式记录。

新增：settlement策略current/revision+generation；主数据/账户/供应商绑定版本；f2op队列与租约；Payment/Posting/冲回；专用行动计划工作流/决定/授权；原始回执append-only；Inbox与独立settlement异常。F0/F1表与不变量继续回归。

## 3 受控配置与原生应付

SettlementPolicy按tenant/company/version/enabled、payment_provider/config_version、posting_provider/config_version、ledger_id/open_periods、payment_executor_user_ids/posting_executor_user_ids、payment_steps/posting_steps/reversal_steps、plan_hours（默认24，上限72）配置。付款与记账经办集合不交叉；经办必须当前公司reviewer；步骤人员必须当前approver/reviewer。动作实际审批人不得是原单据owner或计划经办人；空适用链阻断。没有配置即关闭，不凭聊天指定岗位或账户。

配置及角色变化沿用generation/tombstone，使旧计划/工作流/grant永久失效；原样配置重启不失效。配置可以保留旧provider版本供查单，存在在途时禁止移除其必要配置/凭据。只允许新dispatch使用enabled通道；disabled的旧配置仍可核对已发请求。

受控BusinessSnapshot含BusinessRef(kind/source/id/version/sha256)、name、verification_state及公司/人员范围；PayeeAccount含ref(employee_account/supplier_account)、subject_id、account_name、masked_account、provider_account_id、verified及范围。只由显式操作员配置，不提供员工写入“已核实”接口；无匹配目录即补材料，不伪造引用。实际账户令牌不返回给模型/浏览器。

PayableDraft遵守既有契约：legal_entity_id、supplier_ref、amount、due_date、business_refs、reason，可带真实同公司evidence_ids。供应商与采购依据必须准确匹配受控快照及版本摘要，至少一个合同/订单/验收依据；由服务器确定owner/合计/状态。create/get/list/update，修订要求expected_version，可编辑状态及释放旧proposed与F1一致。submit_payable沿用真实顺序业务审批与分票占额，不能自动批准。

## 4 计划、专用HITL与执行

扩展现有ActionPreview动作参数矩阵：execute_payment(amount,payee_ref)，submit_posting(amount,accounting_period[,evidence_ids])，reverse_posting(amount,accounting_period,reason[,evidence_ids])；目标分别expense_claim/payable与posting。禁止其它字段/float/指数/负数/超界，CNY两位小数字符串。

原单据approved及当前expense_approved业务资格是前提，不能替代专用批准。付款收款人必须与员工owner/供应商一致且账户版本当前；账套来自公司配置，期间必须当前开放。请求人必须对应动作经办资格且与业务owner分离。

外部动作preview固定金额、单据版本/环境、分配/票与证据、账户ref/映射版本、账套/期间、策略/角色generation、provider/adapter/config版本、原posting等，默认24h不可变计划。创建以原plan.digest为目标的持久工作流，并出现在同一review-queue。原计划摘要不包含随后变化的审批步骤状态，避免审批自己使计划失效。

实际分派人用target_type=action_plan、target_id=原计划ID、expected_version=1预检并human执行approve/reject；审批动作不递归创建审批。最终批准生成只匹配该plan_digest/action/amount/target/expiry的专用grant。不能批准approve/reject计划，不能迁移旧决定或延长旧计划。

只有原经办人human明确执行原计划才能创建f2op accepted、Payment/Posting pending及金额占用；未获批准/已过期/版本或策略变化直接409/422，不造假Operation。模型Skill仅准备/查询，execute仍由真实cookie人工网关签发；付款、记账、冲回不能由工具确认升级human。发送前worker还须在线复核执行人和所有实际批准人员、角色/策略/账户/provider版本及grant，再在事务中置dispatching；已发送后的查单保存回执不依赖员工目录在线。

## 5 金额、正交状态与撤回

同父单据锁下：payment可用=total-已成功付款-付款在途；posting独立=total-(成功入账-成功冲回)-入账在途。冲回按原PostingRecord amount-成功/在途冲回校验。accepted/running/dispatching/awaiting_provider/outcome_unknown均占用，确定failed/cancelled才解除该动作占用；两份计划不同key也不能超总额。发票reserved/consumed不双重计费，部分外部完成不得直接把整张分配核销；满足完整对应业务后才转consumed，冲回保留consumed历史并追加调整记录。

原单据审核状态与payment_status/paid_amount/payment_pending_amount、posting_status/posted_amount/reversed_amount等结果维度分开；外部回执不改变冻结单据内容版本，各Payment/Posting/Operation有独立version。支付部分完成与记账顺序互不强制。退款/退票不实现，不把外部returned直接抹成未付。

任何已发送、结果未知或已成功外部动作阻止原单据withdraw/修订和释放invoice占额。cancelOperation扩展f2op且必须human：未dispatch可取消并解除动作占用；已dispatch只能转未知/核对，不能声称已撤款。外部成功通过新的冲回动作处理，不覆写原结果。

## 6 HTTP适配协议

ProviderConfig由操作员固定tenant/company/provider/configuration_version/adapter_version/base_url/token_file/callback_secret_file/callback_key_id/simulation/enabled及显式幂等/查单能力。base_url是固定无凭据origin，正式HTTPS，测试loopback/容器测试host可HTTP；不接受模型URL、不跟重定向、不隐式读取宿主代理。provider+configuration_version全实例唯一以消除回调选密钥歧义。任一缺少幂等或查单能力，均拒绝新外部预检、执行受理及dispatch；不是仅展示能力字段。凭据由运行时注入，不入业务参数/日志。

POST /v1/requests，GET /v1/requests/{external_request_id}；Bearer服务凭据，Idempotency-Key固定external_request_id。请求字段protocol_version='1'、tenant_id/legal_entity_id/operation_id/action/external_request_id/provider/adapter_version/configuration_version/payload_hash/simulation/amount/currency='CNY'/target{id,type,version}/plan_digest；payment带payee_ref/provider_account_id，posting带ledger_id/accounting_period，reverse带original_voucher_id/reason。payload_hash为不含自身字段的规范JSON摘要，重启后不换请求号/金额/收款方。

响应严格回显上述归属/摘要/金额/币种/环境与status=accepted|pending|succeeded|failed|unknown|not_found。succeeded付款必须外部order/transaction ID及证据；succeeded记账/冲回必须匹配账套/期间、voucher_id及证据。原始有限字节回执与SHA256先保留，业务状态与事实同事务；错归属/金额/摘要、无凭证、畸形或冲突均不可当成功。

202/accepted仅awaiting_provider。发送后超时、断线、5xx、取消、崩溃或无法确定是否发送，保留outcome_unknown及占用；只按原请求号查单，not_found也不是安全重发凭据。本期不自动重试任何submit。已知发送前授权拒绝是确定失败，无外部请求；读查询可有界重试，未知1/5/15/60分钟核对，仍未知创建人工核对异常并停止自动查询，人工reconcileOperation只触发只读查询。

## 7 队列、回调、模式与租约

worker与F0导入队列分开。claim采用FOR UPDATE SKIP LOCKED、持久lease_token/lease_until与心跳fencing；prepare才固定dispatching。expired running可重新认领未发送工作；expired dispatching只可unknown/query。旧租约不能改变Operation/Payment/Posting/余额，续租丢失立即终止网络工作；已取得原始HTTP字节可只追加stale标记的证据，不能推进状态。畸形/错归属响应同样保全raw与摘要，使用可信job范围的unknown+validation_error记录，不采纳不可信业务字段。事务内Operation/Payment/Posting/决定/额度/事件与相应Outbox一致，通知仍pending不称已送达。

回调POST /v1/provider-callbacks/{provider}/{configuration_version}，独立供应商认证，不用员工FinanceCaller，不受业务disabled入口拦截。X-Finance-Timestamp、X-Finance-Event-Id、X-Finance-Key-Id、X-Finance-Signature为sha256=后接HMAC十六进制值；签名串timestamp+'.'+event_id+'.'+raw，±300s，恒时比较，body≤1MiB，验签后parse。固定path找配置而非body挑密钥。Inbox按provider/config/event唯一；同id异raw创建冲突异常。无可靠sequence，所有有效回调只排原请求号主动查单，不直接根据回调字符串修改业务终态。未知请求保留拒绝证据，不排新付款。

enabled可新受理和dispatch；draining停止新外部动作、只核对已发送并处理合法取消；read_only/disabled不发新副作用但继续已发送查询/验签回调，不遗失在途结果。查询链路不依赖模型/员工在线。没有旧provider配置时明确失败关闭，不换新provider重发。

## 8 接口、Skill与无模型入口

既有preview/getPlan/execute/reviewQueue/getOperation/cancelOperation扩展；新增create/list/get/updatePayable、get/listPayments、get/listPostings、listActionPlans、reconcileOperation、getSettlementOptions、listBusinessDocuments、listPayeeAccounts。所有列表有权限/过滤后total、keyset cursor、limit1..100。getPayment/getPosting严格验证所属单据权限，账户仅脱敏。

业务包只发布F0-F2已实现操作；加入accounts-payable与posting-reconciliation共6个Skill，保留standard工具批准及模型无execute。人工页面提供应付与结果列表、从实际目录选账户/开放期间/金额、预检和专用审批、原经办人明确执行、结果未知核对与取消，不让用户伪造回执或输入API/JSON。身份/公司切换、409、参数变化清空旧确认；结果不明保留原键与计划。返回accepted不能显示为付款/记账成功。

## 9 文件分工与公开接口

领域agent独占expenses/models.py、settlement/models.py、storage/f2_*.py及必要F1 storage hooks、FinanceStore迁移/继承与领域测试。provider agent独占settlement/provider*.py/worker*.py及provider/worker测试。集成者独占config/runtime/api/routes_f2.py、routes_f1接线、前端、Skill/API/schema/metadata/docs与端到端。provider和领域不交叉写文件，不提交；总门禁后由集成者提交。

领域公共配置：configure_settlements(policies,business_documents,payee_accounts,providers,*,simulation=False)，provider只传无凭据public descriptor。create/get/list/update_payable、list_business_documents、list_payee_accounts、get/list_payment(s)、get/list_posting(s)、list_action_plans、reconcile_operation；复用preview/execute/get_operation/cancel_operation及review_queue。具体类型与返回结构在开发前锁定。

worker协议：claim_settlement(*,allow_dispatch=True)->job|None；renew_settlement(job)；prepare_settlement_dispatch(job)->request；record_settlement_result(job,result,raw:bytes)；defer_settlement(job,code,*,uncertain:bool)；reject_settlement_before_dispatch(job,code)；release_settlement(job)；accept_provider_event(provider,configuration_version,event_id,raw_hash,raw)->dict。job包含id(f2op)/tenant_id/legal_entity_id/lease_token/mode(submit|query)/action/request/actor_id/provider/configuration_version。外部result状态用第6节枚举。SettlementWorker(store,providers,gateway).run_once(*,allow_dispatch=True)->bool。

## 10 验收与边界

真实独立PG：F1迁移保留、应付与目录版本校验、专用审批禁自审/禁代理/策略与角色恢复不复活、跨租户公司、两600争1000、付款记账独立、部分完成/冲回、未知占用与重复key/多计划限制、发送前撤权、取消竞态、原始回执/事件失败回滚、租约失效与重启后仅查单。

真实本机HTTP测试provider：受理未完成、付款成功/记账凭证/冲回、发送后断连、query not_found、错金额/币种/归属、缺凭证、验签失败/重放/异文、旧回调、目录停机后已发核对、read_only/disabled不dispatch。真实平台cookie与财务认证/PG联调专用批准，模型禁用；真实浏览器走隔离资金/凭证模拟流程，逐层区分代码/实际HTTP/PG/镜像/生产/真实供应商。

全量pytest、ruff F/S108/I/TC、strict mypy、lock、声明包与稳定内核Skill、文档链接及diff检查；版本、镜像和独立Compose烟测与证据更新。保留独立分支不推送/合并/部署，清理本次临时PG/卷/密钥。不称已真实付款、正式入账、已接供应商或具备税务/归档合规。
