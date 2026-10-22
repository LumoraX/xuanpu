# 财务 F3 查验、红字与档案

当前F8按用户要求聚焦发票报销内部流转记录，不做支付。最新契约finance `0.9.0-f8`、74项API、7个Skill；见[F8指南](finance-f8.md)及[F8本轮验收](releases/2026-10-22-finance-f8-acceptance.md)。下文阶段记录保留原有验证范围。

当前专用资金能力与最新包契约入口见[F5指南](finance-f5.md)（finance `0.6.0-f5`、69项API、7个Skill）。本页保留本阶段操作说明、历史版本和验证证据；当前最终门禁、制品与运行状态见[F5验收记录](releases/2026-10-22-finance-f5-acceptance.md)。

F3保持财务服务、PG数据库、原件/物化包、密钥、队列和审计独立。玄圃提供身份、Skill和人工入口，关闭模型仍可通过“功能→财务”处理。F3阶段包提供58项实际API及7个Skill，新增archive-audit。尚未选择税务和档案供应商；本期交付通用接口和隔离闭环，正式税务、正式标准profile及正式档案能力关闭。企业微信/飞书报销适配器仍未接入。

## 用户流程

1. 员工提供发票原件及报销事由，先保存内部记录或票据；截图、解析成功、API受理均不能代替实际查验。
2. 请求查验后，由后台按固定外部请求号取得权威快照。passed、failed、unavailable、pending、stale分别呈现，独立库保留所有原回执；旧回执不能回退新状态。
3. 税务经办准备用途、税务入账或已有红字确认单的计划。真实分派人核对证据后审批，原经办再明确执行。模型只能准备与查询，不能代点执行或把聊天确认变成human。
4. 归档经办准备固定目标的归档计划，专用审批后实际收集原件、结构化来源、报销/支付/凭证、税务、红票、补件和实际人工决定。材料不齐就阻断，不用引用或success代替文件。
5. 档案服务接收实际ZIP。客户端逐对象版本读回每个文件、manifest、seal证明及保留锁，全部匹配后才记录封存事实。后续付款、记账、红冲或补件另做补充包，原包及时间不变。

内部记录不自动获得付款、记账或税务资格。正式执行前仍需公司制度、真实经办/审批岗位、渠道和标准验收。

F5归档新增funding_case及原预付父子、实际材料、原授权与撤销决定、核销/退回责任及付款/记账回执；新增源事实仍触发补包。PDF/PNG/JPEG有限结构检测及其边界见[F5指南](finance-f5.md#未知结果归档与恢复)。

## 配置与版本

基础配置沿用[F0](finance-f0.md)、[F1](finance-f1.md)及[F2](finance-f2.md)，新增[关闭状态片段](../../docker/finance/compliance.example.yaml)。compliance_policies和compliance_providers缺省为空；政策按公司版本管理，通道按provider/configuration_version固定。经办必须有当前reviewer岗位并在对应执行集合；审批人来自专用链，owner和preparer不能自审。撤销后恢复同名配置或角色不会复活旧批准。

每个通道配置显式kind=tax或archive、固定origin、独立token_file/callback_secret_file、adapter_version、simulation、幂等和查单能力。税务还需supports_state_revision；档案需supports_object_versions和supports_retention_readback。凭据不进入计划、回执或公共API。相对密钥路径以配置所在目录为根。

Invoice.version仍是票面内容/分票版本；getInvoiceCompliance.version是税务计划和查验用的expected_version。F3计划同时冻结内容、合规版本、证据及当前配置。权威revision按票、provider、配置版本及授权代数分别单调；切换通道后旧来源在途只留证据，不覆盖新来源。当前政策收紧查验期限立即影响投影，pending不会刷新旧passed的真实观察时刻。

simulation仅允许development/test、真实finance_test库及独立对象前缀，来源在新收件时持久固定，旧正式票不能转换。当前唯一归档profile为simulation-archive-v1/version1，明确使用SIMULATION锁。正式启用被COMPLIANCE_FORMAL_UNVERIFIED或ARCHIVE_PROFILE_UNVERIFIED阻断，不存在默认可用的真实税务/WORM服务。

## 通用渠道协议

税务发送固定POST /v1/requests，查单GET /v1/requests/{external_request_id}。信封冻结protocol_version、租户/公司、操作/动作/目标及版本、provider/adapter/configuration_version、simulation、amount/CNY、plan_digest、payload_hash。通道回显同一scope；成功还需严格TaxSnapshot和证据。请求金额由有效票据推导，聊天不能指定。

confirm_usage、confirm_tax_entry参数仅accounting_period，增值税抵扣之外的分支尚未支持。ERP记账与税务入账分别记录。confirm_red_letter仅确认已经取得的实际确认单，参数confirmation_id精确匹配；本期不提供新建确认单、开红票或税务申报接口。适用确认期限取原权威发起72h、权威截止及计划期限的较早值；本地过期只显示expired_pending_check，实际expired/cancelled须权威回查。confirmed不等于issued；实际红票须独立原件、解析、查验及本人关联。补件received不等于已申报或已调整。

实际红冲累计扣减蓝票毛额、税额和明细额度。原内容不改，预留/已用超额时冻结新动作并显示shortfall，历史占用不删除。所有F1分票/提交/审批、F2发送及F3税务应用共用排序的finance_invoice行锁，单据锁先于票锁。同税务维度或同父包来源事件在途/unknown不能换计划重复执行。

回调POST /v1/compliance-callbacks/{provider}/{configuration_version}与员工身份分离，使用固定配置密钥的HMAC-SHA256，覆盖timestamp + '.' + event_id + '.' + raw_body，时窗±300秒。必须有X-Finance-Timestamp/Event-Id/Key-Id/Signature，重复header拒绝。Inbox同事件异文保全冲突；合法回调只唤起原请求GET，不直接推进成功。

## 归档内容和保留锁

archive支持invoice、expense_claim、payable，固定输入包含目标/内容/合规版本、事件sequence截点、结构化明细与字段来源、实际材料清册、标准profile、保留策略、来源摘要和可选父manifest。关联红票、税务调整补件、此前真实税务人工决定也纳入。归档/审计自身未来回执不反向加入本包，避免自引用。

渠道证据从已保存的provider/request/evidence选择固定GET /v1/requests/{request_id}/evidence/{evidence_id}，不跟随object_ref或任意URL。成功付款/记账必须有可取得的实际回单/凭证及大小、媒体类型、sha256。历史截断JSON或错误页按受控raw receipt字节成员保留，不伪称JSON解析成功，也不因历史故障永久阻断后续归档。

preflight在事务外读全部实际文件、校验、物化并保存加密固定ZIP；已有固定包重启优先校验复用。单件20MiB、总包100MiB、总成员1000有界，拒绝重复或穿越路径。受理后发送前再次事务校验固定计划与授权。尚未持久dispatching时可复核后首次发送；持久dispatching后任意中断只GET，不能重新上传。

档案协议multipart POST /v1/archives包含request JSON与bundle.zip实际bytes；GET /v1/archives/{request_id}只查原包。对象读取GET /v1/archives/{request_id}/objects/{member_id}?version_id=...，锁读同路径/retention；每个内容、manifest和seal都要有具体不可变version_id、sha256、size、lock_mode、retained_until、legal_hold。

manifest包含内容清册、完整业务快照和预检测，不能含自身hash或尚未形成的锁回执。独立seal-attestation核对manifesthash、内容版本、四性报告、锁证据及sealed_at；seal自身也逐版本读回并核锁。聚合保留期等于全部对象的最短期限。retention_anchor明确取实际开票日、实际税务/记账期末或archived_at；最后一项至少按max(冻结起点,实际sealed_at)+days核验，不能从预检时间偷减保留期。没有统一法定年限默认保证，示例天数仅待替换配置；到期不自动删除。

四性报告逐项保留实际检测器版本、真实检测时间、输入hash和证据。当前simulation报告只覆盖来源标识、字节集合、支持格式解析/原始证据读取、独立加密和模拟锁协议；不证明官方签名、正式标准、真实税务效力、生产WORM或灾备恢复。

封存后新事实通过实际事件sequence和关联单据闭包检测，supplement_required另建HITL补包，绑定parent_manifest_hash/source_event_ids并去重。发送后新事实不丢弃已经真实封存的旧snapshot；登记原包后要求补包。审计只读全部原版本及锁，失败追加audit及当前健康告警，不改写历史封存。

## 独立开发验证

已运行的核心验证入口：

```text
uv run xuanpu validate examples/finance
uv run pytest packages/xuanpu-finance/tests/test_f3_http_e2e.py -q
uv run pytest packages/xuanpu-server/tests/identity/test_finance_f3_gateway_e2e.py -q
uv run pytest packages/xuanpu-console/tests/test_finance_behavior.py -q
```

PG用例须显式FINANCE_TEST_DSN指向独立finance_test库。实际HTTP闭环覆盖所有20个新增API响应契约；平台cookie/CSRF链在模型不可用时验证实际专用审批。部署/全量门禁及正式未验收边界见[F3验收记录](releases/2026-10-21-finance-f3-acceptance.md)。

当前F4已补原生站内提醒、SQL权限分页、配置/运营投影和local配套恢复，见[F4指南](finance-f4.md)与[剩余缺口台账](finance-gap-register.md)。F4阶段为61项API、7个Skill；当前F5为69项API、7个Skill，专用资金见[F5指南](finance-f5.md)。

当前F6新增正式接入准备检查与可重复隔离验收，总70项API、7个Skill；见[F6指南](finance-f6.md)和[正式接入清单](finance-provider-acceptance.md)。原阶段正式能力未验边界继续保留。

当前内部记录的摘要检索、专用部署配置与上线准备见[F9指南](finance-f9.md)，原阶段验收事实仍以对应记录为准。
