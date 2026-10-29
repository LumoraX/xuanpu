# 财务F5：预付、员工垫付与委托收款专用闭环

## 目标与已确认边界

用户授权继续拆计划并开发直到本轮全部完成，沿用独立财务DB/加密原件、进程外HTTP、7个Skill与真实HITL。基线0544eda，复用xuanpu-finance-f0独立目录，分支feat/finance-f5。方案设计、实施、逐项复核和验收在本会话持续完成，不重复请求总体执行批准；实际企业制度/供应商未选的既有边界保持。

“员工垫付”定义为员工先向供应商支付企业费用，企业随后偿还该员工；“委托收款”定义为供应商授权指定第三方账户收取企业应付款，不扩展为对客户代收款的支付清算产品。

采用现有F1顺序审批/F2专用执行授权及幂等查单机制，加专用单据与证据额度台账。只给通用应付加自由备注会丢失主体与核销约束；重建支付/审批引擎会重复成熟机制，因此选择严格类型化的专用业务扩展。

## 三个业务及预付收尾

所有专用单据使用独立API与严格草稿，底层以document_kind=funding_case接入现有单据、历史、决定和行动计划。不允许报销/应付修订入口把专用单据改回普通可付款单据。content.scenario固定为prepayment、employee_advance、entrusted_collection、prepayment_clearance、prepayment_return；后两种是预付收尾，禁止外部再次付款。

1. **预付**：合同/订单及预付条款、供应商、金额、到票截止日、实际材料。允许尚无发票，不创建虚假分配；业务批准→原资金经办预检→专用付款审批→原经办明确执行→F2渠道确定回执。预付款记账仅可使用明确的预付款业务依据与支持该场景的接口，不解释为发票费用/税务入账。
2. **员工垫付**：员工身份来自当前申请人；受控供应商、企业费用依据、原支付材料、原交易号/日期/实付金额和员工本人账户必须明确。完整发票分配及现行票据限制保留；企业付款只能进入该员工受控账户，不能再次支付供应商。原垫付事实是材料申报及内部人工核对，单独标internal_attestation，不伪装银行接口已验证。相同原支付凭据共享额度，不能跨单重复偿还。
3. **委托收款**：供应商债权主体保持不变，发票销方仍匹配原供应商。声明授权编号、有效区间、授权额度、对应合同、受托账户及真实原件，人工明确核对。仅指定的已核实受控账户可收款；授权过期、撤销、材料/账户版本变化及额度不足在预检、执行、发送前都阻断。不能全局放宽普通应付PAYEE_MISMATCH。
4. **预付核销**：原预付已确定付款后，创建明确关联原预付的clearance子单，后取得发票走现有提议/原子预留。提交时同时预留原预付可核销余额；最后一名实际审批人批准才将本地核销及发票consumed同事务提交。驳回/未执行撤回释放两侧预留；核销不会创建新的付款。部分核销可续办，不能超过已付金额或发票有效可用余额，红字/异常和税额约束继续适用。
5. **预付退回**：用实际退款材料、原交易引用、金额及日期建立return子单，经指定人工审批后形成内部退款核对记录并减少未结余额。明确internal_attestation，不生成银行到账成功事实，不发起厂商退款写请求；后续真实入款通道可替换该核对来源。与核销共享原预付余额，防止并发合计超额；退回不恢复原预付款的可再次支付额度。退款原交易身份和原件hash也纳入共享责任与累计已核对额度，同凭据换子单、换编号或材料链接不能重复登记。

核销/退回最后批准即完成不可变本地事实，普通withdraw/reject/修订均不得回退已完成收尾或恢复原额度。差错进入追加人工异常待处理，不能删除历史或静默负数调整；本轮不提供跨已记账期间的财务更正写操作，须另行定义补偿流程。

核销子单及其原预付必须同租户/公司/供应商/合同原版本，父目标和材料快照必须当前可见且按同一锁序锁定。员工垫付、委托收款和核销完整继承受控采购依据、企业买方与供应商销方、实际原件、当前有效查验（正式模式）、硬异常/红冲、金额/税额及行占额限制；无票例外仅适用于预付及有退款凭据的内部退回核对，不可推广到上述发票场景。

## 证据、授权与额度台账

独立财务原件必须可通过现有受限文件网关取得固定版本并加密保全。新增supporting-material-imports入口，复用F0持久收件队列和原件责任索引，以明确purpose保存合同/支付凭据/授权书，不把支持材料送入发票解析或制造缺票异常。受理不代表材料审核通过。不可访问的文件、摘要/版本冲突和跨环境来源均拒绝；保存后恢复/停用不再依赖平台原件仍存在。

严格FundingDraft含legal_entity_id/scenario/reason/amount/due_date/supplier_ref/business_refs/evidence_ids/source_case_id及场景专属authority。普通请求不能提交owner、approved、paid、verified、simulation。authority是声明与实际材料绑定：kind、reference、amount_limit、valid_from/valid_until、occurred_on、account_ref、evidence_ids；场景约束决定必填。首次建立不可变授权/凭据责任记录，后续引用必须正文及原材料摘要完全一致；供应商、合同、员工/受托账户、版本及原件hash进入摘要。

预付条款按公司+供应商+合同原版本确定共享责任范围；员工原支付按公司+员工+原交易号及实际材料确定，委托按公司+供应商+授权编号确定。责任索引同时防止同一原凭据换编号重复使用；实际材料存在且不是仅URL字符串。提交/审批按稳定锁序检查共享额度（under_review/approved及在途/成功不随意释放）。所有付款仍在父单据锁下扣除成功和unknown/dispatching在途金额。

授权材料的内部核对来自真实finance_decision及所附证据，不因保存声明自动获得执行资格。提供human+reviewer+CAS+幂等的撤销授权命令，禁止申请人自撤销他人审查权或模型代做；撤销是追加决定/事件，不删除历史或宣称撤回已付资金。撤销后未发送动作不能执行，已发送/未知只能按原请求核对。

## 制度、HITL与执行接口

本期CNY/中国企业流程的日期类授权按Asia/Shanghai业务日期判断，valid_until包含截止日，到该日24:00失效；时间戳仍以UTC保存，不依赖宿主机日历日期。

新增版本化FundingPolicy（tenant/company/version/enabled、各scenario的business_steps及到期要求）；缺配置、空审批链、申请人自审、人员撤权、策略或材料变化不能自动通过。配置显式注入，独立DB保存revision+digest+generation；支持禁用后恢复使旧授权失效。沿用ExpensePolicy的组织/公司基础及F2资金、记账经办分离规则，专用流程的实际批准同时绑定FundingPolicy版本/摘要/代数。普通内部记录不能借FundingCase取得执行资格。

统一previewFinanceAction/getActionPlan/executeFinanceAction和review-queue扩展funding_case及submit_funding_case。approve/reject/withdraw仍经固定计划和真实人工执行；模型可建草稿、提议分配、预检和查询，不可批准、执行、已读或撤销授权。每次计划包括场景摘要、主体、账户、原件、原预付及核销影响；读取/执行不延长有效期。

F2 ProviderTarget扩展funding_case；ProviderConfig新增显式supported_funding_scenarios，默认空。未声明支持的旧通道不能接新场景。请求带固定funding上下文/摘要（场景、原预付或授权引用、材料摘要、记账依据类型），金额/主体/账户不可由适配器自行替换。继续复用原固定外部请求、受理/查询/回调/唯一交易与凭证核对，unknown不可新POST。正式通道仍需实际公司配置、匹配能力与正式验收，本轮只验证可替换HTTP隔离闭环。

生命周期矩阵：enabled允许经过资格检查的新建、支持材料收件、预检、审批及明确执行；draining阻止F5新建/收件/新预检/新批准和新外部执行，仅允许原单据withdraw/reject、受权撤销和原请求查单；read_only拒绝新写/发送但保留只读和既有未知请求的安全核对；disabled拒绝员工业务API并停止新收件、新受理和新发送，不删除持久记录；既有已发送固定请求仍可由后台安全GET核对，不能称停止全部后台，后续按只读/排空恢复员工核对。新F5动作及API必须在入口和领域执行分支沿用统一模式检查，不能利用旧approve的cancel标签提升F5资格。最后发送窗口再次复核当前FundingPolicy代数、授权未撤销/未过期、账户和材料原版本；重启不使旧grant复活，已发送/unknown始终仅按原请求核对。

## 查询、辅助页、Skill及审计恢复

新增create/update/get/listFundingCases、getFundingOptions和revokeFundingAuthority API；支持材料导入新增1项，统一动作扩展，不新增第二套审批引擎。列表SQL权限过滤+独立total+limit+1稳定游标，当前主体/公司/审批epoch校验沿用。普通应付和报销列表不混入专用单据。

7个Skill保持，accounts-payable/expense-reimbursement/finance-assistant/finance-review/posting-reconciliation/archive-audit/invoice-intake按职责提供新工具与证据要求；同一实际OpenAPI与参考pack同步。模型工具不包含executeFinanceAction/revokeFundingAuthority。先选场景再补必要材料/事实，不要求用户理解内部表、scope或策略摘要。

无模型功能页提供专用资金列表、新建/修订、材料收件、原件/授权/原支付/核销余额与当前人工待办，原经办恢复实际原计划。通知含原目标funding_case；审批/未知沿用F4，新增到票/未结提醒。运行概览仍来自独立库。F3归档可包含funding_case、授权/垫付/核销/退款材料与决定；核销/材料/授权撤销追加事件导致已归档单据需补包。配套dump/restore保留新表、实际材料原AAD和原请求，恢复不启动worker。

## 验收与完成定义

按域、执行、入口、制品分别取证：真实PG验证三种主体、无票预付、普通应付不放宽、人工步骤、跨租户/撤权/代际、材料冲突、额度并发、部分核销/退回/释放/红冲；真实回环HTTP验证三种付款/记账及未知重启只GET；无模型cookie/CSRF与浏览器验证提醒→原计划→本人审批→原经办执行、核销不能付款；实际原件/归档ZIP/补包、dump/新库restore及在途请求保持。规格复核后质量复核，所有问题闭环。

遵守AGENTS：全量pytest、ruff F/S108/I/TC、strict mypy、C901/大小/中文docstring、可执行包校验、wheel/镜像字节与实际运行全部通过后本地中文提交。更新活架构/指南/原A18缺口台账与验收记录，清理本轮专属资源。无推送、合并或生产部署；未选真实供应商/官方格式/WORM/异机DR/真实模型等既有缺口不能靠本轮编号填为已验。

账户选择契约：`getFundingOptions.accounts`仅提供当前授权且已核实的`ref/account_name/masked_account/verified/subject_id`，员工账户仅本人。创建前可选择固定账户，渠道令牌不返回。`listPayeeAccounts(document_kind=funding_case)`按预付供应商、员工原绑定、委托原绑定分流，核销/退回为空。供应商责任自然键只用稳定kind/source/id；完整版本/摘要进入正文摘要，预付合同仍绑定完整原版本。

归档材料可读性增量：旧检测器仅支持XML/JSON等，不能验证合同PDF及付款图片。采用成熟pypdf与Pillow检查PDF页结构、非加密可读、PNG/JPEG实际结构及像素限制，不自研解析器；检测在可终止进程执行，10秒期限，现有50MiB输入上限、2000页及2000万像素限制明确。该检查只证明有限结构可读，不证明电子签章、官方票据效力或材料业务真实性；隔离四性报告保留simulation边界。

HITL可审阅性补齐：只看EvidenceRef摘要无法实际核对材料，新增只读`getFundingMaterial`（GET /funding-cases/{id}/materials/{receipt_id}，offset/limit分块），因此本轮总API69。当前单据owner/reviewer/有效epoch分派人每块重新校验，从独立加密原件取实际字节并核对整体SHA和责任链，返回document_id/document_version/receipt_id/sha256/media_type/size/offset/content_base64/next_offset；默认128KiB、最大256KiB。页面合并分块并验证整体SHA后下载，服务及页面不执行材料脚本。模型Skill不加载该原件base64工具；正式税务/官方签名边界不改变。

辅助页修订必须保留完整受控采购依据集合和原authority.evidence_ids子集；目录不再提供原引用时标识原版本并保留原值，不静默改绑第一个当前资料。员工可明确调整依据列表，服务端再次校验责任正文和当前有效性；新增支持材料不自动改写既有原授权证据。
