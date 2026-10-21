# 财务领域与流程契约

本契约属于[财务设计](../2026-10-21-finance-capability-design.md)，定义业务服务应满足的不变量，不表示已有实现。
首期币种为 CNY；数据库使用 numeric，API 使用两位小数字符串。数量和单价按来源精度另存，不由金额精度倒推。

## 1 对象与索引

所有领域主键、外键、唯一索引与查询包含 `tenant_id`；公司业务另含 `legal_entity_id`。
员工本人／部门／项目／财务岗位的可见范围由授权目录计算，不能仅检查同租户或公司。
所有可变聚合有递增 `version`，修改执行 CAS；归档对象和事实事件不可变。

| 对象 | 关键字段 | 约束与来源 |
| --- | --- | --- |
| LegalEntity | id、tax_id、name、ledger_mapping、policy_version | 公司税号唯一；登记与授权由玄圃管理 |
| Integration | id、kind、provider、capabilities、status、last_checked_at | 每公司分别记录 ready／unconfigured／degraded／disabled；未知能力不得默认为支持 |
| SourceReceipt | id、channel、external_id、received_at、file_revision、sha256、object_ref、size、media_type | 先收件后解析；解析失败也保留原件及异常；读取对象必须再次核对隔离范围 |
| Invoice | id、identity_scheme、identity_key、number、date、seller／buyer_tax_id、type、amount／tax／gross、version | 业务身份按票种算法生成；数电票使用号码＋日期＋销方；买方必须与目标公司或获准场景一致 |
| InvoiceLine | id、invoice_id、line_no、item、quantity、unit_price、amount、tax、tax_rate、tax_category | 行号唯一；红冲映射到行；免税、零税率与不征税区别保存 |
| InvoiceField | invoice_id、standard_id／version、element_id、source_path、raw／normalized_value、data_type | 标准元素来自受控目录；业务扩展在独立 namespace，不能伪装标准字段 |
| InvoiceSource | invoice_id、receipt_id、source_kind | 同票多来源；字节相同或身份相同不再创建第二主记录 |
| Verification | id、invoice_id、provider、request_id、result、verified_at、valid_until、raw_response_ref | 区分签名验证、哈希一致、官方／第三方查验；结果保留历史 |
| BusinessDocumentRef | id、kind、origin、source、external_id、source_version、snapshot_hash | 原生报销／应付可直接关联；外部合同订单只能引用实际同步／取得的数据 |
| BusinessLink | invoice_id、document_ref、method、score、evidence_ref、matched_version | 多对多；候选置信度不等于已批准绑定 |
| InvoiceAllocation | id、invoice_id、invoice_line_id、document_id、amount、tax、state、version | 报销／应付金额占用；不等同 BusinessLink，也不等同税额抵扣 |
| ExpenseClaim | id、owner、department／project、reason、lines、total、invoice_allocations、status、version | 草稿不得自行填审批结果；明细金额之和等于合计 |
| Payable | id、supplier_ref、contract／order／acceptance_refs、amount、due_date、allocations、status、version | 外部采购依据必须固定来源版本；供应商主数据可由原生目录维护 |
| ApprovalWorkflow | id、document_id、document_version、policy_version、steps、state | 创建时固定审批顺序／金额阈值和角色；不使用聊天中的姓名证明资格 |
| ApprovalDecision | id、workflow／step_id、actor、decision、reason、evidence_hash、at | 只追加；经办与审核、付款执行分工由公司策略强制 |
| ActionPlan | id、action、target、target_version、effects、rule_version、adapter_version、digest、expires_at | 不可变；digest 覆盖金额、对象、收款账户版本、规则和所需批准 |
| ExecutionGrant | id、plan_digest、decision_ids、scope、amount_limit、expires_at、revoked_at | worker 权限来源；必须在线复核原授权未撤回 |
| Operation | id、plan_id、idempotency_scope／key、request_hash、status、attempts、external_request_id | 状态与外部业务状态分开；一计划同动作唯一执行，不因换 key 绕过 |
| PaymentRecord | id、payable／claim_id、payee_version、amount、channel、external_order／transaction_id、status | 付款订单和实际交易分开；回单关联交易，不用 HTTP 成功表示到账 |
| PostingRecord | id、invoice／document_allocations、ledger_id、period、voucher_id／number、amount、status | 一票可分期／分单记账；凭证号不能在全公司范围假设唯一 |
| TaxConfirmation | id、invoice_id、kind、period、amount、tax、provider、external_id、state | 用途与税务入账确认各自记录；不依赖 ERP POSTED 推断 |
| RedLetterCase | id、blue／red_invoice_ids、confirmation_id、initiated_at、deadline、amount／tax、line_allocations、state | 支持多次部分红冲；每次确认单和红票不覆盖前次 |
| ExceptionCase | id、target、rule_id／version、severity、owner、due_at、evidence、state | 接受例外不能改写事实；需经批准且只适用固定版本 |
| ArchivePackage | id、subject、manifest_ref、manifest_hash、standard_version、tests_ref、lock_receipt、retention_class | manifest、文件和检测齐备且锁定已核实时才已归档 |
| StateEvent | id、aggregate_type／id、version、dimension、from／to、kind、actor、at、source、payload_hash | 与业务状态更新同一事务，只追加；聚合＋版本唯一 |
| Outbox／Inbox | id、aggregate_version、destination／provider、external_event_id、payload_ref、delivery_state | 出站与领域提交原子；入站验证原始字节、时间与供应商认证，去重后再应用 |

ApprovalWorkflow.document_id 可指业务单据或行动计划，另有 target_type 与 target_digest；行动计划的审批状态存在工作流，
不修改不可变计划内容或 version=1。ReviewItem 使用 target_type／target_id／target_version 表示这两类目标。
审核行动计划的 approve／reject 预检只验证当前审核步骤及实际角色，不要求又有一套业务审批；原行动计划完成审批后才取得对应 grant。
原行动计划若失效，审核请求返回 plan_expired／plan_invalidated，保留历史决定；原计划不可通过延长 expires_at 或复制批准“复活”。
getActionPlan 返回原参数、金额／账户等影响、目标版本、有效期和 workflow_id；重新进入会话由服务器查询，不依赖此前聊天。

没有 XML 的收件有 SourceReceipt，但不能伪造非空 xml_object_key。解析失败不创建字段不全的已验真 Invoice。
重复原件的来源、上传人和查验差异保留；原件冲突建立异常，禁止覆盖先前事实。
旧式发票、铁路／航空／财政票据使用各自 identity_scheme，不能把 20 位号码要求硬套全部凭证。

## 2 正交状态

| 维度 | 状态 | 变化依据 |
| --- | --- | --- |
| 收件 | received／quarantined／awaiting_original／parsed／rejected | 文件检测、解析／识别结果；不等于验真 |
| 查验 | not_checked／pending／passed／failed／unavailable／stale | 实际渠道回执及可用期；过期需刷新 |
| 票据 | normal／red_locked／partially_red／fully_red／abnormal | 权威通知／回查，不由模型推断 |
| ERP 入账 | not_posted／partially_posted／posted／partially_reversed／reversed | PostingRecord 按当前金额汇总，必须有凭证回执 |
| 税务入账确认 | not_confirmed／pending／confirmed／adjustment_pending／adjusted | TaxConfirmation.kind=tax_entry 的权威结果 |
| 用途确认 | not_confirmed／pending／confirmed／adjustment_pending／adjusted | TaxConfirmation.kind=usage 的权威结果 |
| 付款 | not_requested／pending／partially_paid／paid／returned／outcome_unknown | 渠道结果与对账；实际支付成功后不能直接回退未付 |
| 档案 | not_archived／pending／archived／exception | 检测＋不可变清册＋存储锁回执；后续事件不覆盖已归档包 |

报销／应付的审核状态为 `draft → submitted → under_review → approved/rejected`，rejected 补正创建新版本再提交；
approved 后的实质变更创建修订并作废原执行授权。未执行业务可申请撤回，不能撤销外部已成功结果。
付款与入账维度独立：允许先记应付后付款，允许预付款后取得发票；不能用严格线性流程排除这些场景。

Operation 状态：`accepted → running → dispatching → awaiting_provider → succeeded/failed/outcome_unknown`。
未发送工作可进入 cancelled；等待实际业务审批用 waiting_approval，初次执行资格未满足则直接 409／422，不创建假成功操作。
已有外部副作用的取消、崩溃或超时转 outcome_unknown，不释放占用。outcome_unknown 可经有凭据的查单／对账核实为 succeeded／failed，
期间只允许核对，禁止新幂等键再次付款。成功／确定失败后的修正产生新关联操作，不覆写原始回执。

## 3 金额分配与并发

发票票面原金额不改写，红票作为独立负值票及关联保存。计算可报销／应付基础额度时使用蓝票有效金额减已生效红冲影响，
再按业务规则扣除已核销与有效保留；税额可抵扣额度单独计算，不与报销额度共用。
草稿分配是建议，不占用余额；提交时原子 `proposed → reserved`；驳回／撤回且无在途外部动作时 `reserved → released`；
成功完成对应业务后为 consumed。解除保留和退款／冲回不同，后者必须保留原 consumed 并追加调整记录。

单事务按排序后的 invoice_id 锁定所有相关发票／余额行，校验公司、版本和使用权限，计算可用金额，
全批成功后写占用＋事件＋单据状态。任一超额整批回滚，避免部分提交。查询返回额度时必须注明版本，不能以旧查询结果执行。
`reserved + consumed` 不得超过有效额度；部分红冲使已有占用超额时冻结新分配并建异常，保留历史，不直接制造负数可用额。
客户端金额字符串只接受两位小数的有界十进制，禁用浮点、NaN、Infinity、科学计数、负报销金额和超过 18 位精度的值。
票面红票金额按适配器有符号解析保存。含税／未税／税额关系、明细舍入和允许尾差按标准及企业规则核验，禁止静默补平。

例如蓝票含税 1000.00 元，报销 A 已保留 600.00 元，则另一单最多可保留 400.00 元。
若先有 300.00 元红冲，新的有效额度为 700.00 元，新占用仅剩 100.00 元。
若红冲发生在 A 核销之后导致超额，由异常流程组织冲回／退款及税务调整，不能改 A 的历史金额。

## 4 匹配与规则

规则集合分为不可豁免硬约束、业务策略阻断、可经角色授权接受的告警；都保存 rule_id、version、输入快照、
实际值、期望值、证据与判断。配置只允许受控 DSL／JSON 条件，不接收 Python、SQL 或模型代码执行。
规则至少含公司、有效期间、业务类型、适用票种、金额／时间容差、豁免角色与理由要求，启用前跑回归样本并记录发布者。

| 类别 | 规则 | 结果 |
| --- | --- | --- |
| H01 | 跨租户、公司或无业务数据权限 | 404／403，拒绝读取和执行 |
| H02 | 原件字节摘要不符、受控解析失败 | 隔离并阻断后续动作，保留原件 |
| H03 | 同一身份不同关键票面内容 | 不创建新主记录，建冲突异常 |
| H04 | 可用金额不足、重复付款／入账、旧版本执行 | 409／422，整批回滚 |
| H05 | 外部结果未知、无批准、批准失效／撤回 | 禁止新副作用，先核对／重新审批 |
| P01 | 指定业务必须具备有效查验、匹配和原件 | 不满足时阻断对应动作，缺通道不可人为标通过 |
| P02 | 收款方与供应商／员工账户不一致 | 阻断付款，经过账户主数据变更及重新审批 |
| P03 | 已关账期间请求入账／冲回 | 进入财务调整流程，不能由模型改到其它期间 |
| W01 | 疑似重复、连号、敏感销方、跨期、时间窗口 | 异常记录及按规则所需人工决定 |
| W02 | 垫付、预付、委托收款的主体／时间差异 | 使用场景证据及专门规则，不全局放行 |

## 5 外部执行与通知

### 5.1 动作参数矩阵

ActionPreviewRequest.parameters 严格按下表验证；仅接受必需字段与该行可选字段，其它字段 422。
正金额动作必须大于 0，供应商／员工账户引用必须为已核实的 supplier_account／employee_account 版本。
所有目标版本、公司、数据范围和规则在预检及执行时分别复核；API schema 通过不能替代这些交叉字段约束。

| action | 首次阶段与 target_type | 必需参数 | 可选参数 |
| --- | --- | --- | --- |
| submit_claim | F1 expense_claim | 空对象 | 无 |
| submit_payable | F2 payable | 空对象 | 无 |
| approve | F1 expense_claim；F2 payable；action_plan 随原动作阶段 | workflow_step_id | reason、evidence_ids |
| reject | F1 expense_claim；F2 payable；action_plan 随原动作阶段 | workflow_step_id、reason | evidence_ids |
| withdraw | F1 expense_claim；F2 payable | reason | 无 |
| execute_payment | F2 expense_claim／payable | amount、payee_ref | 无 |
| submit_posting | F2 expense_claim／payable | amount、accounting_period | evidence_ids |
| reverse_posting | F2 posting | amount、accounting_period、reason | evidence_ids |
| confirm_usage | F3 invoice | accounting_period | 无 |
| confirm_tax_entry | F3 invoice | accounting_period | 无 |
| confirm_red_letter | F3 red_letter_case | confirmation_id | 无 |
| archive | F3 invoice／expense_claim／payable | 空对象 | 无 |

部分付款／入账每次计划固定 amount，服务端根据原业务余额与已经执行／在途的金额计算上限；
同一目标不同计划也不能超额。提交记账时账套来自公司受控映射并写入计划，不能由模型临时指定其它账套。
撤回仅适用于尚无外部在途副作用且策略允许的单据版本；撤回失效所有旧计划和授权，并在同一事务释放可释放保留。
approve／reject 动作本身要求当前审核步骤的实际人员资格，不要求“先批准这次批准”的循环；
后续付款、记账或税务动作仍要求其专用业务授权与当前执行资格。

### 5.2 受控目录与恢复处理

catalog 连接器和 finance 连接器访问同一服务，适用相同签名与权限。listExpenseCategories 返回公司启用的费用分类及制度版本；
listCostCenters 返回可用部门／项目；listBusinessDocuments 查询已经同步或原生建立的 supplier／contract／order／acceptance 引用及版本摘要；
listPayeeAccounts 只返回指定报销／应付业务允许的、已核实账户的脱敏快照及 employee_account／supplier_account 引用。
这些查询都按公司和人员数据范围过滤，最多每页 100 条，不能把模型自报内容登记为已核实主数据。
分类为空时先由有权财务人员配置制度；查不到合同或账户时只补材料，不拼造外部引用。
getInvoiceAllocation 返回 id、version、state、invoice_id、document_id、amount 和 tax，读取权限同时校验票与单据；
原单据 allocation_ids 可用于恢复每项分配，删除草稿分配用该分配版本执行。

### 5.3 提交、查单和回调

发送前固定 provider、adapter_version、payload_hash、external_idempotency_key。渠道不支持幂等与查单时，只允许人工处理，
不启用自动支付／税务写入；能查单但不能确定未受理的，状态保持未知，不重发。
付款与记账记录只使用真实渠道产生的外部 ID；测试模式单独数据库／对象空间，回执带 simulation 标识，禁止进入正式单据。
自动重试采用最多 5 次、指数退避加抖动、最大间隔 5 分钟；适配器可收紧，不能扩大业务失败重试范围。
未知结果按 1、5、15、60 分钟查单，仍不确定则转人工核对异常，之后人工触发只读回查。
接收回调先验签原始字节与供应商事件 ID，再 inbox 去重、校验对象归属、金额／币种和合法状态转换。
无可靠顺序号的回调只触发主动查单；禁止按到达时间回退已确认状态。冲突回调进入异常，不丢弃原证据。

## 6 归档与补充证据

归档计划根据业务类型固定必选清单，不能仅按通用“五类文件”判齐；已付款应有实际回单，已入账应有正式凭证，
尚未付款的已入账应付单允许按适用规则形成阶段档案，并明确付款证据不适用／尚未形成，不能伪造齐备。
包包含接收原件、标准结构化文件、业务／审批／资金／记账关联快照、四性检测报告、清册和审计元数据。
真实性报告记录来源验签或验真证据；完整性检查清册、引用和摘要；可用性验证解析／渲染及关联检索；安全性核实访问、存储、锁定和备份策略。
任一必选检测失败进入 exception。档案存储完成后回读清册与锁定配置并核对所有摘要，才提交 archived 事件。
F0 普通对象存储不作为档案 WORM；未提供锁定／保留回执的适配器不能标记已归档。
后续付款、红冲、冲回等事实用补充包引用原包摘要，补充包同样检测和锁定；原包内容、归档时间和检测结果不修改。
