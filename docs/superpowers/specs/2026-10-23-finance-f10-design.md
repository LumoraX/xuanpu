# 财务 F10 内部试用核对与恢复验收设计

用户授权持续完成F10；沿用不支付、独立存储、Skill/API与真人HITL。正式公司/人员/环境仍未提供，当前交付为可填写配置和隔离验收，不上线生产。复用独立检出xuanpu-finance-f0，从aa5dba4建立feat/finance-f10，原xuanpu检出不修改。

## F0–F9核对与方案

F7–F9的记录、原件保全、摘要查询、刷新恢复、驳回补件、真实人工批准已验。当前内部profile仅有离线配置报告；F6在线readiness要求支付/税务/档案配置，内部路由不暴露它，不能直接拿来当内部准备情况。F4已有配套备份恢复工具，但尚缺内部profile在恢复后只读核对、模式切换和撤权后续办的本阶段端到端证据。health还未覆盖站内提醒worker故障与实例锁丢失。

选择复用现有只读公司快照和成熟PG备份工具，增加小型内部报告及自动恢复验收。仅补操作文档无法发现当前撤权或worker故障；另外建立审批/上线管理子系统会重复权限和流程且超出范围。本轮不新增数据库表、部署系统、付款、外部审批、数据导出或生产凭据。

## 内部只读检查

新增GET `/v1/finance/internal-records-readiness`，operationId `getInternalRecordsReadiness`，必填legal_entity_id，加入内部白名单；general下返回403 INTERNAL_RECORDS_ONLY。可信身份认证后，仅当前公司reviewer可读；collector、approver、平台管理员和跨租户/公司不可读。查询使用同一readonly repeatable_read快照，复用F6公司、当前grant/epoch与制度/渠道快照，不写业务、批准、异常、收件或通知；签名鉴权的重放记账仍按现有机制执行。

返回固定字段：legal_entity_id、profile=internal_records、generated_at、status=blocked|not_verified、production_status=not_verified、checks、versions、configuration_digest、external_verifications、runtime。checks仅固定code/status/detail，versions仅expense的version/generation/enabled/digest；digest绑定该公司当前配置及授权代际，不包含业务记录或运行时间，稳定且撤权/恢复/制度变化时改变。不得输出公司税号、姓名/账号、岗位user_id、URL、路径、凭据、原始配置、报销正文或原件。

配置检查覆盖：公司资料；启用的internal_record、require_verified_invoice=false的有效ExpensePolicy及启用制度内至少一个有效分类；审批与异常责任岗位当前有效；存在审批链之外collector；所有现行grant与epoch的role一致且generation正；本公司无启用的外部制度/渠道/目录且模拟关闭；外部存在性直接读取当前指针表的enabled，不能由INNER JOIN的revision投影推断不存在，孤立的启用指针也必须阻断。已撤销的epoch墓碑可保留，只核对现行grant有匹配epoch；无grant墓碑不误报。验证使用已有Pydantic制度模型，不吞错或用空值伪装通过。失效配置报告blocked；数据库查询故障正常报错，不输出部分成功。

runtime只返回mode、instance_lock_lost、receipt_worker与notification_worker的running/failed/not_started、external_workers的absent/present。真实任务未启动/故障/已退出、锁丢失、内部实例装配外部worker或registry均阻断；read_only是合法只读验证状态，不自动成为执行许可。运行状态与公司配置摘要分别展示；所有checks通过仍not_verified。外部待验固定为正式制度、生产身份、数据库与原件隔离、异机恢复、真实模型及生产负载。health新增notification_worker_error及instance_lock_lost故障判定，HTTP503而不输出诊断正文。

## 页面与Skill/API

内部财务辅助入口增加“内部试用检查”，供有权限者主动读取；403明确说明财务复核岗位要求，不回退通用readiness。不主动轮询，不执行任何写操作。请求保护pending、公司/身份/页面代际和revision，过期成功或失败均不得覆盖新页面；失败可重试，格式错误不能显示全通过。显示模式、检查项、当前制度版本、摘要、运行态和固定未验边界，不提供批准/上线按钮。普通员工对话为默认入口，手动入口继续作无模型兜底。

finance-review Skill在internal_records分支可调用新报告；其他Skill保持capabilities先行与内部流程边界，不增加人工执行或原件工具。业务包0.11.0-f10/77项API/7 Skills，财务服务2026.10.23.1，console2026.10.23.1，镜像同步。规范和examples逐文件对应，同步当前架构、指南、缺口台账及F9指针。

## 隔离端到端与关闭条件

使用本轮自建loopback finance_test库、私有DSN、测试员工和有效PDF；保留红绿测试。真实签名API验证公司权限、当前撤权/恢复代际及脱敏，read_only可读/写拒绝、disabled拒绝，旧批准不因恢复授权复活。真实CLI启动内部实例：接收原件、保存原文、本人提交到待审、停止服务，用现有维护入口备份到私有目录，恢复到第二个空白独立测试库和原件根；恢复启动使用与备份时相同的公司、grant、制度和密钥配置，仅修改DSN/原件根与mode；启动前后比较配置摘要、grant代际和历史，明确read_only仍允许启动迁移/配置注入及签名重放记账，业务写命令拒绝。以internal_records+read_only启动恢复实例，实际读取报告、历史和原件SHA，拒绝写入；显式隔离enabled重启后，由原分派人逐步预检/人工批准，保留human决定且invoice/settlement/compliance为0。损坏清册/密文拒绝由既有维护回归和本轮恢复覆盖。

在实际浏览器验证新检查页、权限拒绝、重试/过期响应通过行为测试；既有完整报销链由本轮固定quick和全量重新执行。全部模型入口在隔离流程禁用，真实模型语义不借自动化夹具宣称通过。执行full/quick、ruff/mypy/源码约束、文档契约、构建包与镜像字节复核及独立审查。最终清理自有测试资源、保留证据/制品、中文本地提交，不推送/合并/部署。

生产SSO、实际公司/制度/人员、正式独立凭据/IAM/网络、真实模型、生产负载及异机灾备仍not_verified。当前可开发项完成并隔离验收，不把缺少真实资料的项目标记完成。
