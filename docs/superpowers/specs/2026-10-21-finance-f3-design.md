# 财务 F3 查验、红字与归档实施设计

用户已批准继续F3并确认尚未选定供应商：先通用适配接口及独立隔离闭环；正式税务动作、正式归档缺省关闭。继承总设计与finance-v1/domain-contract；不是实际税务接口、官方标准或法定归档认证。

## 1 范围与选择

交付三块：查验历史/用途及税务入账确认；外部红字确认单同步、确认、红票关联与进项转出跟踪；证据物化、按业务阶段清单、不可变档案、锁定读回、补充包及审计。税务申报、替用户开票或发起新红字确认单不在原已批准动作矩阵中，本期只确认实际取得的确认单，不扩成开票系统。

选择沿用成熟FastAPI/Pydantic/asyncpg/httpx/zipfile/AES-GCM，进程外财务服务持有自己的PG/原件/档案索引、密钥、任务和审计。平台生产代码不导入财务。比较过直接S3 ObjectLock和通用档案HTTP：本期选通用HTTP档案服务接口，避免未选云厂商时绑定bucket语义；接口必须提供具体对象版本及锁读回，不能只返回success。现有本地加密对象/IfNoneMatch不是WORM。未来S3可实现同一归档服务边界。

## 2 版本与存储

复用F2隔离检出，基线75b2552，新分支feat/finance-f3；专属finance_test PG回环60033。F0/F1/F2保持可独立启用和旧配置兼容。

Invoice原票面版本继续作为不可变内容/分票版本；新增独立ComplianceState.version，查验、税务和红字事实递增该版本。getInvoice保留原version，并返回compliance_version、当前正交投影；getInvoiceCompliance的version是F3 invoice目标expected_version。F3计划同时冻结内容版本、合规版本、证据版本和公司配置。这样不改历史分配绑定，也不因正常查验刷新而迫使已批准单据重建；旧执行计划仍因合规快照变化失效。

F3新增独立policy current/revision/generation、provider无凭据快照、typed target状态、计划/流程/步骤/不可变决定/grant、Operation/lease历史、原始receipt/Inbox/异常、Verification/TaxConfirmation/RedLetterCase/TaxAdjustment、ArchivePackage/审计/补充关联。全部tenant/company范围，数据库归属约束与唯一索引；不使用伪报销父单据绕过F2 FK。新表迁移幂等；旧记录不取得新税务或归档资格。

所有新模拟发票明确标记simulation；不得把旧正式发票变为模拟。运行时simulation仅development/test、真实finance_test库及独立对象前缀；模式标记不能切换。正式解析/标准样本及实际渠道未就绪时相应动作阻断。

## 3 受控策略和权限

CompliancePolicy按tenant/company/version/enabled配置：tax_provider/configuration_version，archive_provider/configuration_version，税务/归档实际经办集合、各自专用审批链，open_periods、用途vat_deduction、支持票种/解析profile、查验最长有效期、计划24h上限72h；归档标准profile/version、保留类别、保留天数、起算规则、required_lock_mode（正式COMPLIANCE、隔离SIMULATION）及required_legal_hold均显式配置，不虚构统一法定期限。税务与归档可分别关闭。无配置无能力。

经办为当前公司reviewer且须在对应执行集合；实际审批人为当前approver/reviewer，不得是目标owner或原计划经办。配置/岗位实际启停的generation进入计划；恢复旧配置不复活旧批准。平台管理员没有默认财务权限。

F3实际分派审批人可读取该目标及必要关联证据，无跨公司全票权限；降为collector后旧分派不保留他人资料访问权。审核待办并入listReviewQueue，F3原计划通过listCompliancePlans恢复；getActionPlan/previewFinanceAction/executeFinanceAction/getOperation/cancel/reconcile统一分派，F2旧接口保持行为。

## 4 查验与税务事实

verifyInvoice为受权只读外部查验及本地持久历史受理，不是人工宣称票真，可由Skill发起；VersionRequest使用当前ComplianceState.version。没有通道拒绝，不创建假成功。异步Operation先持久accepted，固定请求号，查验结果记录provider、原始回执/摘要、verified_at/valid_until、方法与simulation。passed、failed、unavailable、stale严格区分，期限由渠道与公司策略共同收紧，不当作法律固定期限。

通用tax快照可同步真实用途/税务入账、确认单和调整事实。authority_revision按公司/provider/票单调递增；旧快照只留原证据不覆盖新事实。新的查验请求不得使已有passed在时效内被pending字符串洗掉；查询时根据真实期限算stale。相同状态仍保留新查验历史，快照版本变更让旧行动计划失效。

confirm_usage、confirm_tax_entry仅invoice目标，参数accounting_period；金额/税额从有效蓝票余额和受控用途规则取，不由聊天指定。当前支持受控增值税抵扣用途，出口退税、收购票和其它未支持分支明确阻断。ERP入账与税务入账不同。动作先不可变计划→专用真实审批→原经办human执行→外部回执才能confirmed；已确认同维度不能重复执行。

## 5 红字与调整

红字确认单必须来自验签回调后的主动查单或受控tax快照，不接受用户创建“已确认/已开红”的事实。每次case持久蓝票、外部confirmation_id、原始发起/截止时间、金额/税额、实际行映射、分支与权威版本；支持多次部分红冲。

confirm_red_letter只确认实际awaiting_confirmation且应由本方确认的单据，参数confirmation_id精确匹配。适用数电票规则下用途或税务入账任一已确认影响对方确认；特殊票种/用途未支持不能推算。期限取权威截止与适用72h、企业计划期限的较早者，不从本地导入或批准重新计时。超期显示expired_pending_check并阻断写入，只有主动权威回查可定expired/cancelled；本地时钟不伪造外部作废或批准。

确认成功不是红票已开具。issued须权威回执；红票独立原件收件/解析/查验，linkRedLetterInvoice要求真实human、同公司、蓝红身份/金额/税额/确认单匹配，不把关联动作当权威开具事实。

所有F3税务/红冲写事务先按invoice id排序锁finance_invoice，再锁合规状态；与F1分票/提交/批准及F2 dispatch共用该蓝票行锁。需要单据锁时遵循单据→排序蓝票→合规行，持蓝票后不得反向获取单据锁。已生效红冲累计/行影响不得超蓝票金额/税额；原票不改。统一有效金额=蓝票减已生效红冲。新分票、F1提交/批准、F2发送与getInvoice均读取同一实时状态：red_locked/fully_red/abnormal阻断；部分红冲按受控规则允许剩余，税额额度分开。已保留/核销超有效额时可用显示0并明确shortfall/冻结和异常，保留历史，不删consumed、不释放未知付款。

已抵扣票发生适用红字确认/开具，产生TaxAdjustment待办，记录待转出税额、来源确认单及红票缺口。人工补件仅变为材料已收到，不能标已申报/已调整；完成须权威调整回执。退款/ERP冲回仍通过F2独立动作，不修改旧成功结果。

## 6 专用HITL、队列与网络恢复

F3 typed target审批机制与F2一致，共用人工入口和公开preview/execute协议；新F3表避免改坏F2单据FK。F3原计划ID前缀f3plan_，Operation前缀f3op_。approve/reject目标action_plan且指F3原计划时交F3流程，不再递归审批。原摘要不含自身审批动态状态。

复用SettlementWorker的成熟租约心跳/在线授权/实时发送门禁，通过ComplianceQueueAdapter映射到独立F3队列；第三任务随服务生命周期关闭，锁丢失三个后台都取消。所有副作用dispatch前保存固定请求和摘要，记录历史lease归属，旧lease只追加真实原任务stale证据，不能改变状态。领域最终提交再验证原dispatch的完整冻结scope，不信provider单字符串。发送后live输入变化不丢弃真实封存结果：登记原snapshot的封存并立即标记supplement_required，新事实不得加入旧包。

固定tax协议POST /v1/requests，GET /v1/requests/{external_request_id}；Bearer/Idempotency-Key服务端注入；继承无重定向、流式上限、重复键/超深JSON拒绝、原字节保全。请求含F2通用冻结scope（protocol_version/tenant/company/op/action/request/provider/adapter/config/simulation/amount/CNY/target/plan_digest/payload_hash）及typed payload。所有通道必须明确幂等/查单能力，缺失关闭。

未发送可取消；已发送/超时/崩溃/not_found均未知，固定1/5/15/60min只读查询后转人工。核对不依赖员工目录在线，绝不自动重发。enabled接收新动作，其余模式继续在途查单/回调。回调独立/v1/compliance-callbacks/{provider}/{configuration_version}，沿用HMAC原字节+时间窗+事件ID，固定路径选密钥，Inbox去重/冲突保全，只触发查单，不能直接宣布税务/归档成功。

## 7 归档内容、检测与锁定

archive允许invoice/expense_claim/payable目标，参数空对象；auditArchive为只读审计受理。按目标/阶段/record_mode建立固定清单。发票档案需要原件及受控标准结构化材料；internal_record仅可按明确内部记录profile归档，不宣称发票税务档案。已付款/入账逐笔需要实际回单/凭证文件；未付款阶段包明确尚未形成，不能写为不适用或伪造齐备。

F2证据object_ref不是文件。EvidenceFetcher只从已冻结provider/config/operation与evidence ID选择固定GET /v1/requests/{request_id}/evidence/{evidence_id}，不访问任意URL；核验真实字节/媒体类型/大小/sha256，存独立财务副本。归档前实际读取收件原件、权威原始回执、业务/审批/支付/凭证快照、调整和审计事件，固定输入版本及摘要；总量100MiB/成员1000有界，路径不能穿越/重复。

ArchivePipeline.preflight(request)在发送前完成材料收集、标准profile及预检测，保存固定暂存bundle到独立加密对象。缺件或检测失败是明确未发送失败/异常，不进入假归档。随后领域事务再次核对原计划/授权/输入和snapshot_hash并dispatch，归档provider提交读取同一固定bundle。

通用归档协议：multipart POST /v1/archives（request JSON＋实际ZIP bundle字节），固定Idempotency-Key；GET /v1/archives/{request_id}查单。单成员读取固定GET /v1/archives/{request_id}/objects/{member_id}?version_id=...，同路径/retention读锁，不访问provider返回任意URL。归档query只能读取，不能重新上传；成功恢复须与submit成功路径一样全量读回每个版本、字节、锁及seal。

manifest只包含内容成员及预检测，不包含自身hash或之后才形成的锁回执。供应商封存后给出manifest及所有内容的immutable version/摘要/大小/锁模式/retain_until/legal_hold，另有独立seal-attestation（manifest_hash、内容版本、检测、锁证据），其本身也锁定。客户端逐版本读回所有字节和锁，再核对独立证明；任何缺文件、版本、hash不符、锁期限不足、模式不符、法律保留不符，均不能archived。

四性逐项保存规则/检测器版本、时间、输入摘要、passed/failed/unavailable/not_checked/not_applicable及证据。真实性区分来源/验签/查验，完整性真实集合摘要闭合，可用性实际解析支持格式及关联取回，安全性核对权限/加密/锁/保留/备份恢复证据。正式标准profile、真实签名或恢复证据未验证时不允许伪造passed。本期隔离profile明确synthetic/simulation，真实官方标准/真实WORM/法律认证不作已完成声称。

只有全部检测、清册、证明与锁读回通过，领域事务才追加ArchivePackage archived。原包/清册/检测/封存事实不可变。后续付款/记账/红冲/冲回/税务调整通过当前输入digest差异标记supplement_required，新的归档计划引用父包及manifest_hash，固定source事件去重，保留原字节、归档时间及检测。后续审计失败追加审计及当前健康告警，不改写历史封存事实。保留到期不自动删除。

## 8 API、Skill、人工页

新增20操作（F0–F3合计58）：verifyInvoice/listInvoiceVerifications/getInvoiceCompliance/getComplianceOptions/listCompliancePlans/listTaxConfirmations/listRedLetterCases/getRedLetterCase/refreshRedLetterCase/linkRedLetterInvoice/listTaxAdjustments/recordTaxAdjustmentEvidence/listArchives/getArchive/getArchiveManifest/getArchiveTests/getArchiveEvidence/listArchiveSupplements/auditArchive/getArchiveAudits。既有preview/execute/reviewQueue/getOperation/cancel/reconcile扩F3，callback不注册工具。

加入archive-audit，共7个Skill。模型可查验、查询缺口、准备税务/归档计划；不获得executeFinanceAction、红票人工关联、调整人工补件或人工reconcile/cancel工具权限。人工页增加发票与税务、红字/调整、档案/检测/审计及F3原计划，账户/身份/公司/参数变化撤销旧确认；不让人输入JSON、伪造查验结果或锁回执。模型禁用仍可由真实cookie+CSRF入口完整操作。

## 9 实施分工与验收

按独立文件边界：领域F3 typed target/审批/税务/金额/队列；归档Pipeline/材料/HTTP锁读回；root通用provider/API/runtime/配置/包/页面/实际联调。先RED→GREEN，独立规格复核再质量复核。F0-F2完整回归不得以旧红为由跳过。

真实独立PG验证事务/并发/CAS/权限/防复活/迁移；真实HTTP隔离tax与archive服务验证实际字节、wrong scope/过期/unknown只查、锁全覆盖与补充包；实际平台cookie→human签名→财务验签/PG→专用审批；Node与实际浏览器无模型闭环；安装包和镜像构建、Compose重启、逐文件制品匹配；全量pytest、ruff F/S108/I/TC、strict mypy、复杂度≤10/函数≤80/文件≤800、文档链接、锁及git diff全部通过。保留失败证据和真实跳过原因，清理本任务资源，不推送/合并/部署生产。

## 10 实时核对的外部依据

2026-10-21复核：国家税务总局2024年第11号公告（2024-11-12成文，2024-12-01施行）第八/十/十一条支持用途或税务入账、适用数电确认单72小时、进项转出及平台查验；不证明有公开可调用API，也不扩展到所有票种。[税务官方全文](https://zhejiang.chinatax.gov.cn/art/2024/11/13/art_24085_629347.html)。

AWS官方说明锁保护具体对象版本，同名新版本/delete marker不等于旧版失效，Governance有绕过权限；需逐版本读回和企业批准的保护级别，不能外推任意S3兼容服务。[官方Object Lock说明](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html)。本期HTTP档案协议要求同等可核验证据，尚无选定真实存储的验收。

## 验收后明确的执行细节

归档实际封存时间sealed_at必须来自渠道回执并保全，retention.anchor_rule在固定快照中明确；archived_at规则的最短保留按max(冻结起算点, sealed_at)+规定天数核对。manifest和seal均作为固定版本对象逐字节读回并读取保留锁，不依赖渠道自报readback_verified。

空、截断、HTML及错误回执的实际原字节按受控raw receipt种类保存，可使用application/octet-stream；仅原始回执允许零字节，普通原件和归档材料仍拒绝空内容。原回执的provider/configuration_version来源必须由其真实operation取证，不能按当前配置推定。

归档证据闭包含蓝票、已关联红票各自原件/结构/查验、已有F1/F2和F3税务人工决定、实际调整材料原字节；归档自己的未来回执和自己的审计不进入自身快照。补包追加实际来源事件及父包hash，保留原包快照。
