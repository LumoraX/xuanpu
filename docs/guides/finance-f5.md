# 财务F5：预付、员工垫付与委托收款

当前F14按用户要求仅作内部流转记录，不支付；契约finance `0.15.0-f14`、77项API、7个Skill，见[F14指南](finance-f14.md)及[F14验收](releases/2026-10-23-finance-f14-acceptance.md)。下文保留各阶段事实。

F8阶段按用户要求聚焦发票报销内部流转记录，不做支付。当轮契约finance `0.9.0-f8`、74项API、7个Skill；见[F8指南](finance-f8.md)及[F8本轮验收](releases/2026-10-22-finance-f8-acceptance.md)。下文阶段记录保留原有验证范围。

F5在独立财务PostgreSQL、AES-GCM原件、真实人工审批和F2固定外部请求上增加专用资金单据，覆盖三种付款场景与两种预付收尾。当前包契约为finance `0.6.0-f5`、69项API、7个Skill；服务与console已构建版本分别为`2026.10.22.1`、`2026.10.22.2`，实际制品和验证结果以[F5验收记录](releases/2026-10-22-finance-f5-acceptance.md)为准。版本编号不表示正式通道已启用。

未提供正式企业制度、真实支付/ERP/税务/档案供应商，本轮沿用显式simulation与可替换HTTP，正式能力默认关闭。玄圃持有企业身份与入口，财务独立持有单据、材料、授权、额度、人工决定和外部原回执；外部系统可替换。架构见[当前财务架构](../architecture/finance.md)，工作范围见[实施计划](../superpowers/plans/2026-10-22-finance-f5.md)。

## 五个场景

| scenario | 必须保留的业务事实 | 收款或收尾规则 |
| --- | --- | --- |
| `prepayment` | 受控供应商、合同原版本、预付条款原件、预付金额、到票截止日 | 可无发票；付款给该供应商已核实受控账户。预付款业务依据不能解释为发票费用或税务入账 |
| `employee_advance` | 员工已向供应商支付企业费用；原交易号、日期、实付额度、原支付材料、完整有效发票分配 | 偿还当前申请人本人已核实`employee_account`；不再付给供应商。原支付是`internal_attestation`，不是银行接口验真 |
| `entrusted_collection` | 原供应商债权与发票销方、合同、授权编号/额度/有效区间、授权原件、指定受托账户 | 仅付原授权固定的已核实受控账户；不改变发票销方，不处理客户代收款 |
| `prepayment_clearance` | 同公司/供应商/合同原版本的已确定付款预付父单、后取得的发票与实际分配 | 最后一名实际审批人批准时消耗发票分配并核销原余额，不再付款 |
| `prepayment_return` | 原预付、真实退款材料、原交易号、退款日期/金额 | 内部人工退款核对减少未结余额；不调用退款API，不制造银行到账成功 |

核销与退回共享原预付余额，累计预留和完成金额不得超过原预付的确定已付金额；`outcome_unknown`、在途或单据批准不能算已付。部分收尾后可继续建子单，退回不恢复可再次支付额度。最后批准提交的核销/退款核对事实不能通过普通修订、撤回或驳回回退；差错保留历史并进入追加人工处理，本轮不提供跨已记账期间的资金更正写操作。

员工垫付、委托收款、预付核销仍须满足实际发票原件、受控采购依据、企业买方/供应商销方、正式模式有效查验、红字/硬异常、行金额与税额限制。无票预付不是普通应付的付款捷径。

## 员工与财务人员入口

对话可准备草稿、材料、分配和预检。无模型时使用“功能→财务”的专用资金入口，先选场景，再提供实际材料与受控目录引用。7个Skill名称保持：`finance-assistant`、`invoice-intake`、`expense-reimbursement`、`finance-review`、`accounts-payable`、`posting-reconciliation`、`archive-audit`。

1. 通过支持材料入口保存合同、原付款凭据、授权书或退款材料；查询持久操作确认实际收件，再使用source证据ID建单。
2. 查询`getFundingOptions`，确认本公司显式启用的五场景及各自审批链，选择当前可见受控账户和采购依据。`accounts`只返回`ref/account_name/masked_account/verified/subject_id`；员工账户只列本人，供应商账户限当前授权且已核实。渠道账户令牌、文件凭据不返回。
3. 创建或CAS完整修订草稿。需要发票的场景沿用实际分配提议与原子预留；使用`submit_funding_case`预检并由本人提交，实际分派人逐步审核。
4. 三种付款场景先取得业务批准，再由与申请人分离的资金经办准备专用付款计划；实际专用审批人批准同一固定摘要，原经办再次明确执行。记账有独立岗位、期间、批准和回执。
5. 核销/退回沿本场景业务链完成本地收尾；查询原预付和子单的实际余额及决定。当前提醒可恢复原目标和原计划，已读不产生业务决定。

`listPayeeAccounts(document_kind=funding_case)`按场景过滤：预付只列匹配供应商账户；员工垫付与委托收款只列原凭据固定账户；核销/退回为空。实际付款仍重新核对账户完整版本和声明，不能凭页面曾显示过就绕过约束。

人工执行与撤销授权只接受真实员工cookie、CSRF与平台签名的human origin，不能通过API key、root、act-as或模型伪造。模型工具不含`executeFinanceAction`、`revokeFundingAuthority`；预检不产生决定。撤销须当前有权reviewer、非相关申请人、明确理由、版本和幂等键；只阻断未发送资格，已付历史和未知原请求继续保留。

## 支持材料与API示例

新增8项API：`importSupportingMaterials`、`createFundingCase`、`updateFundingCase`、`getFundingCase`、`listFundingCases`、`getFundingOptions`、`revokeFundingAuthority`、`getFundingMaterial`；统一动作/待办/账户/档案API扩展`funding_case`。以下是请求体结构示例，ID、版本和摘要须来自当前实际目录与收件结果，不能直接作为业务凭据提交；认证沿用[F0签名入口](finance-f0.md)和[F1人工入口](finance-f1.md)。

支持材料：`POST /v1/finance/supporting-material-imports`，携带独立`Idempotency-Key`：

```json
{
  "legal_entity_id": "company-a",
  "files": [{"file_id": "contract-original", "revision": 1, "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]
}
```

202只表示受理。此入口复用F0持久收件队列、固定版本文件桥、责任索引、独立加密副本和恢复机制，持久`purpose=supporting_material`，逐项成功终态为`material_received`，汇总为`summary.materials`。`result_refs`返回`kind=source`且`verification_state=not_checked`；收件不伪造invoice，也不因材料不是发票而制造缺票异常。保存后恢复不再要求平台源附件仍存在，实际原件摘要/版本不符仍拒绝。

实际审批人通过人工辅助页下载原件核对。`getFundingMaterial`使用`GET /v1/finance/funding-cases/{id}/materials/{receipt_id}?offset=0&limit=131072`，单块上限262144字节，响应含`document_id/version/receipt_id/sha256/media_type/size/offset/content_base64/next_offset`。仅当前同公司owner、reviewer或epoch有效的实际分派人可读该单据关联材料；服务从独立加密objects解密并核对整体hash，页面合并完整分块后再次SHA校验再提供下载，不依赖源平台附件。此入口供human辅助页读取；模型仅查证据元数据，不提供材料base64工具。

预付草稿：`POST /v1/finance/funding-cases`：

```json
{
  "legal_entity_id": "company-a",
  "scenario": "prepayment",
  "reason": "按合同预付设备采购款",
  "amount": "100.00",
  "due_date": "2026-11-30",
  "supplier_ref": {"kind": "supplier", "source": "internal", "id": "supplier-a", "version": "1", "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
  "business_refs": [{"kind": "contract", "source": "internal", "id": "contract-a", "version": "1", "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}],
  "evidence_ids": ["source-receipt-a"],
  "authority": {"kind": "prepayment_terms", "reference": "CONTRACT-A-PREPAYMENT", "amount_limit": "100.00", "evidence_ids": ["source-receipt-a"]}
}
```

`FundingDraft`只允许`legal_entity_id/scenario/reason/amount/due_date/supplier_ref/business_refs/evidence_ids/source_case_id/authority`；`business_refs`必须有合同，材料ID不可重复。`source_case_id`仅两种收尾场景必填，其余禁止；`prepayment_clearance`禁止authority，其余场景必须匹配下表。CAS修订使用`PUT /v1/finance/funding-cases/{id}`，体为`{"expected_version": 1, "draft": <完整FundingDraft>}`。不能提交owner、approved、paid、verified、simulation等服务端事实。

| authority.kind | 对应场景 | 除reference/amount_limit/evidence_ids外的场景字段 |
| --- | --- | --- |
| `prepayment_terms` | 预付 | 不接收日期或账户字段 |
| `employee_payment` | 员工垫付 | `occurred_on`、本人`employee_account`完整`account_ref` |
| `collection_mandate` | 委托收款 | `valid_from`、`valid_until`、指定`supplier_account`完整`account_ref` |
| `return_evidence` | 预付退回 | `occurred_on` |

`FundingAuthoritySpec`中的`reference`为原交易/授权/条款标识，`amount_limit`为原凭据正金额额度；其`evidence_ids`须属于草稿实际材料，且为成功收件的supporting材料。日期格式为`YYYY-MM-DD`，不适用字段应省略。授权有效期按`Asia/Shanghai`自然日，包含`valid_until`当天，到次日00:00失效；不依赖宿主机日期。

提交预检复用`previewFinanceAction`：

```json
{
  "action": "submit_funding_case",
  "target_type": "funding_case",
  "target_id": "funding-case-a",
  "expected_version": 1,
  "parameters": {}
}
```

随后通过真实人工入口读取原计划、确认影响和执行。批准使用实际workflow_step_id，外部付款使用固定amount/payee_ref；都须重走原计划和版本门禁，不从聊天生成审批事实。

## 配置、共享额度和停发

`FinanceConfig.funding_policies`缺省为空，专用新业务关闭。实际模型为`FundingPolicy`，配置字段为`tenant_id/legal_entity_id/version/enabled/steps`，其中`steps`必须显式给出全部五个scenario的非空`ApprovalStep`列表；每步使用`id/name/approver_user_id/minimum_amount`，同链步骤ID和审批人不能重复。不能用设计早稿中的`business_steps`作为配置字段。

启用须已有当前公司、已启用`record_mode=invoice_backed`基础`expense_policies`及当前公司approver/reviewer岗位；基础内部记录模式不取得专用执行资格。实际审批不能申请人自审，资金经办和记账经办沿F2分离。政策revision正文不可覆盖，version/digest/generation与实际角色epoch进入计划和批准；停用、撤权后恢复旧配置不会复活旧批准，也不会复活对单据、计划、支付和相关票据的旧分派读权。

`ProviderConfig.supported_funding_scenarios`缺省为空，旧通道不能自动获得新场景。显式支持场景后仍须满足F2幂等、查单、公司/环境、当前岗位及固定请求要求。空能力列表在public descriptor中省略，保持旧通道配置hash兼容；不能以新增字段默认值使旧配置摘要变化。正式模式必须另行取得企业制度、真实通道和实际验收，不能把simulation改名当正式能力。

共享额度由独立财务库锁和责任台账约束。同一原凭据hash或同一自然责任范围不能通过换单据、换编号、换材料链接重复申请。供应商自然身份只用稳定`kind/source/id`；完整目录版本和摘要进入不可变正文。预付条款范围仍绑定合同完整原版本；员工原支付绑定员工、原交易号与原件；委托绑定原供应商与授权号；退款亦绑定原交易与原件。审查中、批准、发送中和未知额度不能随意释放，提交和最终批准均原子复核。

| mode | 专用资金行为 |
| --- | --- |
| `enabled` | 按实际授权允许新建/支持材料收件/预检/审批/明确执行 |
| `draining` | 阻止上述新业务及新发送；允许有权撤回/驳回、撤销授权与原请求核对 |
| `read_only` | 拒绝新写和发送，保留授权读取与既有未知请求安全核对 |
| `disabled` | 拒绝员工业务API，保留持久事实；已发送请求仍遵循F2安全查单规则 |

## 未知结果、归档与恢复

外部动作沿[F2协议](finance-f2.md)：发送前持久`dispatching`、原请求号和固定请求体，第一次仅一次POST；超时、断网、重启、撤权或不匹配成功证据后，只GET原请求，不重发。最后发送窗口重审当前FundingPolicy、实际批准人、授权期限/撤销、账户、材料、额度和通道。收到202、材料已收或人工原支付核对均不是银行成功。旧未发送授权失效后须重新预检、审批，不能用恢复配置重新发送。

F3档案现在接收`funding_case`，闭包包含预付父子单、实际材料字节、授权及撤销决定、核销/退款reservation事实、实际人工审批、付款/记账原回执。新增收尾、材料或撤销等源事件会产生补包缺口，原包及封存时间不覆盖。PDF用pypdf、PNG/JPEG用Pillow做有限结构可读性检查，在可终止进程（10秒期限）运行；输入50MiB、PDF 2000页、图片2000万像素上限。该检查不证明官方电子签章、材料业务真实性或法定四性，正式WORM和档案仍未验收。

F4提醒保留真实`funding_case`目标。`funding_due`仅针对已确定付款、到票日已过且仍有未结余额的预付，全部核销/退回后退出当前提醒；unknown不制造到票催办事实。资金列表与total继续SQL授权过滤、同快照计数和有界分页。

[F4维护流程](finance-f4.md)使用真实独立dump/restore，保全新表、原件责任索引/AAD、授权、决定、余额及原请求；恢复只到新空库和新对象根，不自动启动worker。重新启用前复核当前配置与在途原请求。此机制不等于S3服务端版本备份或异机灾备证明。

## 验证与验收边界

本轮验证入口：

```text
uv run xuanpu validate examples/finance
uv run pytest packages/xuanpu-finance/tests/test_f5_contract.py packages/xuanpu-finance/tests/test_f5_config.py --collect-only -q
```

独立PG的域、额度并发、撤权epoch、真实回环HTTP、真实cookie/CSRF人工入口、材料/档案、备份恢复及完整门禁结果由[F5验收记录](releases/2026-10-22-finance-f5-acceptance.md)逐层记录；收集到测试用例不代表已执行通过，未配置独立`FINANCE_TEST_DSN`的skip不能证明PG行为。

真实供应商、正式制度、税务官方票样/签章、真实WORM、外部企微/飞书报销、异机DR、真实模型专项评测和生产并发均未验。完整遗留边界见[缺口台账](finance-gap-register.md)，不以本阶段编号或隔离HTTP成功代替这些证据。

F6阶段新增正式接入准备检查与可重复隔离验收，当轮70项API、7个Skill；见[F6指南](finance-f6.md)和[正式接入清单](finance-provider-acceptance.md)。原阶段正式能力未验边界继续保留。

当前内部记录的摘要检索、专用部署配置与上线准备见[F9指南](finance-f9.md)，原阶段验收事实仍以对应记录为准。
