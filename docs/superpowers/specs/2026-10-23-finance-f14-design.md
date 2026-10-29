# 财务F14维护凭据安全读取与恢复后真人续办设计

用户授权继续梳理F0–F13并制定、执行F14，优先内部试用及上线前验收；不支付、独立财务存储、实际真人审批不变。沿用独立检出xuanpu-finance-f0，从干净da624a9建立feat/finance-f14；仅本地提交，不推送、合并或部署。

## 剩余缺口与本期范围

F0–F13已有持久收件、独立密文、内部记录profile、历史原文、材料目录、两级HITL、撤权代际、并发确认、强退续办、配套备份恢复、停机verify及POSIX原件目录边界。真实公司/制度/人员、生产SSO/独立凭据/IAM/网络、真实模型语义、生产负载、S3/异机恢复与正式部署仍需真实资料，不能用隔离结果关闭。支付、ERP、官方票据/税务、法定档案、外部审批不是当前内部记录依赖。通用发票finish_item_parsed撤权隔离仍属未开放通用范围。Linux当前只有镜像入口、源码与基础文件IO证据，整套Linux业务和浏览器新一轮验收尚未完成。

本轮实测维护会话：使用自建finance_test_f14数据库，0644的DSN、0644的AES文件以及两类叶子符号链接均被maintenance_session接受并连接真实PG。该诊断只取得/释放维护实例锁，不生成备份或业务事实。启动和verify已使用read_fixed，backup/restore仍直接Path.read_text/read_bytes，导致安全读取不一致；旧CLI对backup/restore未知异常还会抛出原堆栈。

方案一复用readiness.credentials.read_text_secret/read_fixed，在维护会话首次连接前形成一次固定凭据快照；方案二只补运维说明；方案三重做维护工具/存储。选择一，使用现有标准POSIX安全读取和成熟pg_dump/pg_restore，保留备份格式、AES/AAD、锁、恢复目标约束与成功回执。同步将维护CLI未知故障输出为有限失败JSON，不隐藏失败或回滚承诺。

## 安全读取与兼容性

在maintenance.py增加小型私有捕获函数，调用read_text_secret(DSN)和read_fixed(AES)，捕获OSError/ValueError/UnicodeError转MAINTENANCE_INPUT_INVALID，密钥长度仍KEY_INVALID。复用pg_environment对维护DSN的既有选项规则进行连接前检查，保留已有sslmode/sslrootcert/sslcert/sslkey、编码密码及端口语义；不得误用verify的valid_dsn将所有query禁掉。实际TLS握手、证书链和生产IAM仍未验。

maintenance_session无注入快照时通过asyncio.to_thread调用捕获函数，之后只消费所得dsn/key；verify已有credentials参数与capture_verification_credentials语义保持，不二次读取、不改变verify的DSN规则与十字段结果。检查禁止符号链接/非普通文件、按既有MAX_SECRET_BYTES有界读取、检查私密权限，并在asyncpg.connect、PG工具、临时备份/恢复目录和任何数据写入之前拒绝坏凭据。backup已有只读路径/目标冲突检查顺序保留，不承诺坏凭据优先于参数错误。

配置凭据祖先路径由受信运维管理；此次仅沿用固定叶子安全读取，不承诺阻止恶意管理员、祖先替换或多文件原子轮换。每个文件在本次维护中只读取一次；运维应通过受控部署管理跨文件的一致轮换。读取后文件改动不影响本次已捕获凭据。真实IO/DB/PG工具失败不得变成成功。

旧F4测试夹具默认创建0644密钥，将合法夹具及错内容但合法权限的wrong-key显式设600，让原KEY_MISMATCH等断言继续验证原机制；不放宽新产品规则。只有存在同类合法输入时才同步其他夹具。

## CLI失败契约

已知MaintenanceError保留稳定代码及退出1，参数错误保留argparse退出2，verify incomplete仍退出2。未知程序异常：verify仍VERIFICATION_FAILED，backup/restore返回MAINTENANCE_FAILED，均仅status/code、退出1且不输出原异常、凭据、路径或堆栈。成功JSON不改；RESTORE_FAILED_DATABASE_MAY_BE_COMMITTED仍保留，不能将已尝试恢复误报安全回滚。模型不能调用维护、读取密钥、改权限或代审批。

## 验收与交付

先红测0644/链接/目录/空白/过大/坏编码/密钥长度、FIFO与socket有界拒绝，边界间IO异常、描述符回收、一次快照以及维护DSN已支持TLS选项兼容；无效输入的DB与工具调用为0，业务/对象不变。异常输出用仅测试秘密哨兵证明脱敏，不记录实际凭据。与旧F4/F5备份、F10恢复、F11verify及F13进程逐项回归；PG独占顺序使用。

真实CLI联合：两个独立public库、实际内部服务/签名HTTP收件并本人提交，停止服务后保存全库/密文快照。使用坏凭据运行实际backup/restore，固定JSON失败、无目标/暂存发布，原库/恢复目标与密文不变；观察边界严格区分单测DB调用0与真实CLI数据不变，不仅凭行快照断言从未连接。修复测试自己的凭据后，成熟工具实际备份/恢复、全表与密文一致，平台源移除；目标库启用后每级agent先403/状态不变，再由manager及finance-reviewer实际human批准。最终approved、原文/理由保留、paid/posted及正式invoice/settlement/compliance均0，下载只1。最终停机verify complete、全表/密文不变、无worker或业务写，进程/网关端口全部关闭。

业务包0.15.0-f14，finance/镜像2026.10.23.5，console2026.10.23.1。77 API/24内部操作/7 Skills及shell=false、工具列表不变；两份pack20文件同步，额外4refs对da624a9不变。Skill只说明维护异常交受信管理员与原请求核对。quick原44完整顺序前置本轮两个模块为46。冻结全部产品/测试/脚本/pack/dockerfinance/uv.lock，独占全量/quick、静态、文档、发行包/ZIP/镜像逐字节核对，独立规格/质量复核后中文本地提交。保留红绿及失败证据、精确凭据脱敏清册和原始空白例外，仅清理本轮匹配ID/label的PG/卷/DSN/进程/端口。真实模型、员工、生产存储与灾备继续not_verified。
