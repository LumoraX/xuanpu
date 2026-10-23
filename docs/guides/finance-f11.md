# F11 内部试用：启动门禁与独立原件核对

当前聚焦发票申报、报销及人工审批记录，不支付。业务包finance `0.12.0-f11`保持77项API、24项内部操作、7个Skill；独立财务服务/镜像`2026.10.23.2`，人工页面仍`2026.10.23.1`。正式公司、人员、制度和部署环境尚未提供，本轮仅配置与隔离验收，见[F11验收](releases/2026-10-23-finance-f11-acceptance.md)。

## 员工与复核人

员工在对话中提交发票信息、报销事由和附件，Skill准备草稿；申报文字按原样保存，尚未查验。本人确认提交，分派给实际审批人逐级预检并确认批准或驳回。也可通过“功能 → 财务”辅助入口操作；两种入口共用独立财务数据、授权及历史。approved只表示内部审批完成，不表示付款、入账或税务认证。

finance-review可读取F10公司内部试用检查。停机原件核对是受信运维入口，Skill不能发起维护shell、代替真人确认，不能用本地完整性结果改写上线就绪结论。

## 正式内部启动

沿用[内部记录模板](../../examples/finance-internal-records/config.example.yaml)及[F9配置](finance-f9.md)。没有真实资料时保持模板关闭，不能用测试公司和账号冒充正式授权。

以下仓库命令已实际执行：

```bash
uv run python -m xuanpu_finance --config examples/finance-internal-records/config.example.yaml --preflight
```

未填写模板返回blocked、production_status=not_verified，退出2；不会连接数据库或创建原件目录。

production/internal_records在打开数据库和创建原件存储前强制复验结构、独立固定凭据和映射。私有文件应为无符号链接的普通文件，权限600或更严格、至多64KiB；令牌去除首尾空白后不得为空或相同，也不能复用同一inode。AES为32原始字节，公钥为Ed25519；所有公司必须映射可信网关租户。正式网关和S3端点须HTTPS。服务捕获一次凭据并用于全部构造，轮换后须重启。

门禁失败输出固定INTERNAL_STARTUP_BLOCKED并退出2，不回显路径或凭据。check-config只验证结构，preflight只离线核对；通过仍not_verified。服务启动继续使用独立PG、原件和实例互斥锁；read_only会执行迁移/配置注入与签名防重放记账，不等于数据库零写入。正式凭据权限、网络、身份、备份和操作授权仍由正式环境验收。

## 停机只读verify

受信操作员关闭财务服务后，可通过独立维护入口核对local密文原件。DSN和32字节AES私有文件仅安全读取一次；凭据不得贴到聊天或命令参数正文。verify不接受package和pg-tool参数，backup/restore仍使用[F4既有步骤](finance-f4.md)。实际隔离命令和输出保留在本轮验收证据。

verify要求已初始化的public内部记录库和存在的安全原件目录。活动服务、空白未初始化库、general数据、未知结构、符号链接、缺失必须原件或AES认证失败都会失败。成功只输出对象数量、prepared缺件数量和未映射密文数量；无文件名、对象键、正文。只读repeatable_read事务持有维护独占锁，结束前复核锁，不迁移、建目录、调用备份程序、启动任务或写业务。

完整核对退出0，verification_status为complete；prepared缺件或未映射密文退出2，为incomplete；失败退出1，只返回固定code。已初始化的零对象库可以完整核对，但不能证明所给密钥正确或存储可写。结果始终production_status=not_verified，不批准异常或单据、不写回API就绪报告、不启用实例。

## 验收仍需补齐的资料

独立存储、Skill、API和HITL保持一致。真实公司/制度/人员、生产SSO、独立凭据与IAM/网络、真实模型语义、生产负载、S3/异机恢复和正式部署均未验收。票据官方查验、税务、法定归档、飞书/企业微信和ERP均可后续替换接入，不是当前记录流程的必需依赖。
