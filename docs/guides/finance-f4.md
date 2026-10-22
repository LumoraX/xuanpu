# 财务F4：提醒、运营与配套恢复

当前F8按用户要求聚焦发票报销内部流转记录，不做支付。最新契约finance `0.9.0-f8`、74项API、7个Skill；见[F8指南](finance-f8.md)及[F8本轮验收](releases/2026-10-22-finance-f8-acceptance.md)。下文阶段记录保留原有验证范围。

当前专用资金能力与最新包契约入口见[F5指南](finance-f5.md)（finance `0.6.0-f5`、69项API、7个Skill）。本页保留本阶段操作说明、历史版本和验证证据；当前最终门禁、制品与运行状态见[F5验收记录](releases/2026-10-22-finance-f5-acceptance.md)。

F4在F0–F3隔离闭环上补齐原生提醒、SQL授权分页、实际接入状态、运营概览及配套备份恢复。F4阶段包为finance 0.5.0-f4，61项API、7个Skill；当时服务2026.10.21.5、console 2026.10.21.6。财务库、原件、提醒、密钥和恢复生命周期仍独立。正式税务、真实WORM、外部消息渠道及企微/飞书报销尚未接入。

## 提醒与人工处理

在“功能→财务→财务提醒”读取本人当前可见事项，未读数量独立于当前页。旧F1/F2 Outbox会消费到独立财务站内inbox，状态为inbox_recorded；这不表示飞书、企业微信或邮件送达。无法映射或无当前收件权限的旧消息有诊断，不伪装投递成功。

当前审批/逾期、F0补件、F2/F3未知结果、红字期限及档案健康/补包缺口有持久提醒。来源ID、实际版本、审批代际、审计与事件事实去重；健康恢复或补包后旧提醒退出当前计数，再次实际故障生成新未读。撤权后旧提醒不保留他人资料访问权。

“核对原事项”重新读取当前待办，打开原单据或原计划；应付使用payable真实类型。已读须本人确认、版本和幂等键，不能产生批准或渠道成功。模型只能查询提醒；审批仍由实际指定人员完成，原经办随后明确执行。未知结果只按原请求查询，不重复付款、税务确认或归档上传。

提醒任务按有界批次和持久游标轮转，不依赖模型。enabled/draining刷新，read_only/disabled不生成新提醒；实例锁丢失或服务退出取消四个任务。提醒任务故障在概览中单独显示，不修改业务结果；F2/F3在途核对仍按原规则运行。

F5新增funding_case提醒目标；funding_due仅在预付已确定付款、到票截止日已过且未结余额为正时产生，完整核销/退回后退出当前提醒。unknown不算已付。F5新表、材料责任及原请求随独立dump/restore保留，恢复仍不启动worker，见[F5指南](finance-f5.md)。

## 运营与查询

“运行概览”仅当前公司reviewer可查看。按独立财务事实统计F0收件、F2结算、F3合规各状态、最早活动队列/未知时间、待审/逾期和当前有效通知；历史终态不拉长当前积压，失效提醒不永久计未读。后台任务状态不代表业务成功。

listIntegrations读取实际启用政策和渠道，区分configured_internal、configured_simulation、formal_unverified、unconfigured、disabled。它表示配置阶段，不表示网络可达或正式验收通过；不返回凭据和端点。

F3列表、统一F2/F3计划和待办在SQL内按同一授权谓词count并取limit+1，count/页面共享repeatable_read。父目标、当前审批epoch、公司及主体游标隔离不放宽；原F0/F1/F2列表继续键集分页。大规模查询仍应结合实际部署数据观察查询计划；本轮未声称生产容量指标。

## 运维备份与恢复

维护CLI随finance wheel提供。需安装与数据库兼容的pg_dump/pg_restore；服务镜像不附带PG客户端，维护应在具备客户端的独立运维环境运行，或显式传入受控工具路径。测试使用专属PG容器的真实工具，不用自造SQL备份。CLI不停止现有服务，运行实例仍持锁时返回INSTANCE_ACTIVE。

```text
xuanpu-finance-maintenance --help
```

完整命令形态如下（路径须替换为本企业独立资源；示例不代表已备份生产）：

```text
xuanpu-finance-maintenance backup --database-dsn-file /run/finance/database-dsn --objects /var/lib/finance/originals --key-file /run/finance/object-key --package /secure-backup/finance-20261021
xuanpu-finance-maintenance restore --database-dsn-file /run/finance/restore-dsn --objects /var/lib/finance-restored/originals --key-file /run/finance/object-key --package /secure-backup/finance-20261021
```

只支持public schema及local加密原件根，S3服务端版本和外部档案服务备份单列未实现。备份前安排停机/在途交接；命令获得与服务相同的独占锁，dump/复制/发布全程核对，丢锁取消工具并禁止完整发布。输出目录必须不存在，密钥/DSN不能位于对象根或与根内文件为同一硬链接，备份目标也不能放进原件根。

备份包含真实pg_dump custom、完整密文目录、文件hash/大小和HMAC清册，不携带密钥/DSN/令牌。原件责任索引与F3固定请求/描述符用于恢复AAD原对象键，逐个真实解密并核对已知明文hash/大小；必需引用缺失或认证失败直接拒绝。prepared尚未写对象与额外无法映射密文单独报告，complete_object_validation=false时不能称完整可用。

恢复只接受明确新建的finance_restore或finance_test前缀空库，目标无用户表/其他schema，对象根不存在；不支持覆盖既有正式库。先校验全部文件、HMAC和密钥指纹，再pg_restore单事务，恢复DB后重新认证原件及归档缓存，成功才发布对象根。失败可能已提交DB时返回明确错误，不自动drop或覆盖；操作员需保全报告并调查目标库。恢复不替换目标环境的部署配置/凭据文件、不启动worker；财务库内岗位和渠道历史随快照保留，未知和已发送请求保留原号；重新启用前仍须只读核对及岗位/渠道复核。

真实测试已演练原件、F1审批、F2/F3未知原请求及F3归档缓存恢复；这是同机隔离证据，不是异机灾备、生产RTO/RPO或真实WORM认证。密钥须另行安全保全，丢失密钥不能靠备份清册解密。

## 验证入口与缺口

```text
uv run xuanpu validate examples/finance
uv run pytest packages/xuanpu-finance/tests/test_f4_notices.py packages/xuanpu-finance/tests/test_f4_pagination.py -q
uv run pytest packages/xuanpu-finance/tests/test_f4_maintenance.py packages/xuanpu-finance/tests/test_f4_overview.py -q
uv run pytest packages/xuanpu-server/tests/identity/test_finance_f4_gateway_e2e.py -q
```

PG用例须显式FINANCE_TEST_DSN指向独立finance_test库；维护演练还需可用PG工具（本轮专用容器xuanpu-finance-f4-test-pg）。[缺口台账](finance-gap-register.md)逐项区分既有本地已验、F5专用业务及外部未验收；[F4验收记录](releases/2026-10-21-finance-f4-acceptance.md)记录最终门禁、镜像、浏览器和清理证据。

当前F6新增正式接入准备检查与可重复隔离验收，总70项API、7个Skill；见[F6指南](finance-f6.md)和[正式接入清单](finance-provider-acceptance.md)。原阶段正式能力未验边界继续保留。
