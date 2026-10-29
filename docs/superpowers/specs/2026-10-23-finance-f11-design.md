# 财务 F11 正式启动门禁与独立原件核对设计

用户授权持续完成F11，范围仍为发票报销流转记录、不支付、独立存储、Skill/API和真人HITL。真实公司、人员、正式环境仍未提供；本轮仅代码和隔离验收，不推送、合并或部署生产。复用独立检出xuanpu-finance-f0，从3b50c95建立feat/finance-f11，原xuanpu检出不变。

## F0–F10剩余缺口与选择

F10已完成内部公司只读核对、真实CLI配套恢复、模式/撤权及人工续办。仍有两个可直接关闭的运维缺口：F6/F9离线预检并不是正式启动门禁，runtime在检查之外用普通read_text重新读取凭据，可能绕过已有文件权限、类型、令牌分离和TLS规则；实际原件认证仅在备份/恢复时运行，无法独立只读检查。

选择复用现有预检规则和F4维护锁/原件认证器，新增少量组合逻辑。仅加强操作说明不能阻止直接启动绕过；重建部署/审批/存储子系统会重复已验机制且超出范围。当前不补生产SSO、真实模型、外部供应商或异机灾备，不增加数据库表、API操作、页面、支付能力或模型维护工具。

## 正式内部实例启动门禁

仅`profile=internal_records AND environment=production`的实际open_runtime，在任何DB连接、迁移、配置写入、客户端/对象/worker构造之前执行已有配置和资源规则。general及显式development/test保持兼容；改变environment是受信操作员选择，不是通过检查自动切换，隔离验证不能冒充生产。

复用有界固定文件读取：普通文件、拒绝符号链接/特殊文件、私有权限、最大64KiB、UTF-8去首尾空白的非空文本；AES原始32字节；可解析Ed25519公钥；服务和每租户网关令牌不复用inode或规范化字节；S3必要固定凭据；公司租户网关映射齐备。生产网关与S3端点必须HTTPS。保留既有DSN约束（明确PostgreSQL/用户名/主机/数据库、无fragment/query）；不把这个语法检查当作数据库TLS或实际独立凭据验收。

本轮一次有界读取并生成不参与repr/log的内存凭据快照；正式内部runtime之后仅消费该快照的DSN、service token、tenant tokens、公钥、AES和S3凭据，不再按路径打开文件。连接池与独占锁使用同一DSN，禁止预检通过后文件变化导致消费未验字节；资源失败、错误公钥/文本、同令牌、HTTP或缺映射均在首个副作用前拒绝。调用方配置仍先经Pydantic重新校验，使用现有TLS/凭据校验函数，避免离线与实际启动规则分叉。

失败固定FinanceError `INTERNAL_STARTUP_BLOCKED`，不回显路径、DSN、令牌、公钥正文或异常。真实CLI捕获该特定门禁错误，输出脱敏JSON(status=blocked, production_status=not_verified, code=INTERNAL_STARTUP_BLOCKED)、退出2、无traceback；其它错误不宽泛吞掉。check-config仍只检查结构且不证明可启动，preflight成功仍not_verified，不自动开启业务。初始化后内存快照不会热刷新；凭据轮换由操作员更新私有文件并重启，旧实例不暗中读取新内容。

## 独立只读原件核对

维护CLI新增action `verify`，必填database-dsn-file/objects/key-file；verify禁止package和pg-tool，backup/restore维持必须package及原有pg-tool语义。明确仅local/public schema内部记录库。新入口复用有界固定读取预先校验DSN/32字节key文件并只消费本次字节，不启动业务runtime或模型，不建目录、不调用PG外部工具、不产生备份包、不迁移或改公司/业务。

复用维护独占锁，与正在运行的财务实例互斥；同一readonly repeatable_read事务中核对完整内部schema/内容兼容性，再调用现有authenticate_objects实际AES-GCM/AAD解密和明文hash/大小比对，结束前再核对锁。空白或通用库拒绝；原件根必须已存在且安全，不能因缺根而建空目录伪装通过。新空内部库配已存在空根可核对0项，但不证明密钥正确、可写或RTO/RPO。

返回固定有限字段：status=completed、verification_status=complete|incomplete、profile=internal_records、production_status=not_verified、authenticated_objects、prepared_missing_count、unmapped_objects_count、complete_object_validation、workers_started=false、business_writes=0。不输出对象键、材料/目录/数据库名称、原件、清册正文或秘密。完整核对退出0；prepared缺件或未映射密文保留incomplete、退出2，只返回计数；必须原件缺失/损坏、非内部/未知结构、锁失效等失败固定code并退出1，不返回部分成功。查询错误必须失败、不能因异常给空清册。

原件认证是停机时刻的本地完整性核对，不是法定归档、WORM、票据查验、生产存储/IAM证明。当前API getInternalRecordsReadiness继续只描述公司配置和运行态，不把本CLI结果写入API或视为上线批准；核对命令不批准单据、解除异常或复活旧计划。

## Skill、API与发布

保留77项API、24项内部操作和7个Skill，capabilities与精确报告schema不变。Skill不得调用维护命令，不增加shell、人工执行或原件工具；finance-review可解释检查的配置/运行与本地原件层次。finance业务包0.12.0-f11，服务/镜像2026.10.23.2；console没有行为变化，保留2026.10.23.1。提前同步当前README、CLI标题、架构/指南/台账及F10指针，避免制品构建后再改文档元数据。

## 验收与边界

先TDD：正式内部配置坏资源在asyncpg/httpx/objects/worker任一调用前拒绝；预检与实际门禁同规则、无秘密回显；实际CLI退出2。验证快照后替换文件，所有构造参数仍取已验字节，连接池和锁不读取不同DSN；明确test/general兼容。生产成功装配只证明规则和构造，不证明真实TLS网络或正式员工。

verify真实PG/local测试涵盖有效PDF独立收件、完整只读核对、无写入/无工具/无目录、缺/坏必需密文、prepared缺件、未映射密文、空根/不存在根、普通库/空白库、活动实例锁、锁失效和读故障。真实CLI输出逐项验证有界字段、正确退出码和脱敏。真实HTTP内部报销、只读/恢复/撤权/两级human继续用固定quick和全量执行；本轮可在停机前后调用新verify，将已收原件、人工历史和业务表0外部操作断言关联，不伪造receipt/decision。

最终full/quick、ruff/mypy、源码约束、文档和发布契约通过，构建finance包/ZIP/镜像并逐字节核当前源；独立规格再质量审查。清理本轮自建测试库/容器/私有连接文件，保留证据/制品，中文本地提交。真实公司/制度/人员、生产SSO/网络/独立凭据/IAM、真实模型、生产负载、异机恢复与外部能力仍not_verified，不用隔离测试关闭这些正式待验项。
