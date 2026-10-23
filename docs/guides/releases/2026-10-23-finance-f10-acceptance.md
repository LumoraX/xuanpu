# 财务 F10 内部试用与恢复验收记录

本轮完成发票报销流转记录的内部试用检查与隔离恢复验收，不支付。正式公司、人员、制度与部署环境仍未提供，因此本轮没有部署生产、接入真实SSO或生成正式凭据。业务包finance `0.11.0-f10`发布77项API、7个Skill；内部profile开放24项，service/console/镜像均为`2026.10.23.1`。

## F0–F9剩余项与本轮实现

F7–F9的原样申报、独立收件、摘要、刷新恢复、历史原件和实际HITL保留。本轮补齐适用于internal_records的在线公司核对，以及恢复后的只读、模式和撤权续办证据；不把F6通用支付/税务/档案前提套入内部记录。

`getInternalRecordsReadiness`仅当前公司reviewer可读；授权、当前制度及grant/epoch在同一readonly repeatable_read快照中核对。外部存在性直接查询当前指针，孤立启用revision不能被INNER JOIN漏掉。固定12项检查和有限runtime不输出账号、税号、URL、路径、秘密、报销原文或原件；全部通过仍not_verified。运行态核对实际任务、锁与外部执行器缺席；health新增通知故障和锁丢失503。

辅助页面按需只读查询，普通申请人403说明岗位，支持重试并保护pending、公司/身份/页面过期成功与失败。finance-review Skill按需读取相同报告，不能批准或上线。7个Skill保持capabilities先行，当前内部approved不产生支付、记账、官方验真或法定档案资格。

## 实际恢复与人工续办

[本轮全量JUnit提取证据](evidence/finance-f10-2026-10-23/recovery-evidence.json)来自真实产品CLI、签名HTTP、PostgreSQL与local密文，平台身份/文件边界采用明确的测试网关，不声称正式SSO。

1. 原库实际接收有效测试PDF、原样保存带首尾空白与换行的申报，本人预检/提交进入两级待审。取得真实第一审批人可执行的批准计划，停机后使用现有维护CLI及成熟pg_dump备份。
2. 使用维护CLI/pg_restore恢复到第二空白独立库和新原件根；实际密文AAD认证。恢复后数据库公司、角色、代际、制度、单据、历史、工作流/步骤/决定与回执完整快照等于原库。
3. 使用相同公司/grant/制度/密钥，仅替换DSN/原件根和mode，以internal_records+read_only启动。公司配置摘要不变；读取原文、历史与原件字节相等；创建与执行备份前有效批准计划均409。启动前后完整业务快照一致。read_only仍允许启动迁移/显式配置注入和签名防重放记账，不能解释为数据库零写入。
4. 原平台测试源字节已移除。显式隔离enabled重启后，实际撤权拒绝，恢复授权也使旧计划409；原单仍under_review。申请人撤回、重新提交取得新分派，manager与finance-reviewer分别人工预检/确认批准，单据approved/v5。数据库批准决定恰为2项、origin均human且非owner；invoice、settlement_operation、compliance_operation均0。最后disabled真实重启读取503。

本轮是同机两个测试数据库的恢复证明；不是异机灾备、生产RTO/RPO、真实原件标准或正式凭据隔离证明。既有维护工具的篡改、错密钥、非空目标、缺原件及维护锁拒绝用例在full/quick继续回归。

## 实际浏览器与只读边界

最终夹具使用空白`finance_test_f10_browser_final`、真实loopback平台/财务服务、实际cookie/CSRF、人工连接器签名及财务验签，模型全部禁用。显式测试人员仍沿用F8用户/F9公司标签，不是实际公司或员工。

新会话实际完成：申请人读取403→重试仍403且保持登录；复核人读取12项通过→重新检查，配置摘要稳定，页面明确正式未验。数据库10张业务表均0，包括报销、行动计划、历史、决定、收件、正式invoice、结算、合规及通知；只有既有鉴权防重放产生10行。最终模型attempts/calls均0，不证明真实模型的起草或语义质量。

[实际浏览器数据库/模型证据](evidence/finance-f10-2026-10-23/browser-acceptance.json)、[检查页面](evidence/finance-f10-2026-10-23/browser-final-readiness.png)、[完整DOM](evidence/finance-f10-2026-10-23/browser-final-readiness-dom.txt)、[申请人拒绝页面](evidence/finance-f10-2026-10-23/browser-final-forbidden.png)、[拒绝及重试DOM](evidence/finance-f10-2026-10-23/browser-final-forbidden-dom.txt)保留。新会话最终取证时约93秒，全部检查通过仍not_verified。

首次探索已取得复核通过和申请人403，后续重试前夹具预发会话超过3600秒，被通知轮询401注销；[会话到期记录](evidence/finance-f10-2026-10-23/browser-expiry.json)与截图保留。该重试不计成功，未修改产品认证或延长真实会话，另起空白库/新夹具完成上述复验。两个服务进程最终均退出0、四端口关闭；首次清理检查过早于优雅退出，已等待进程结束再确认，见[退出记录](evidence/finance-f10-2026-10-23/browser-shutdown.json)。

## 测试、审查与制品

| 层面 | 本轮实际结果 | 边界 |
| --- | --- | --- |
| 全量pytest | 2662 passed、3 skipped、0 failures/errors，673.11秒 | 3项既有非财务变体；整体report明确partial |
| 固定quick | 226 passed、0 skipped/failures/errors，35个固定模块 | F10全部模块与F0–F9关键保护继续执行 |
| Console | 18 passed；新增CJS8组 | 包含严格字段、Unicode、literal文本、403/网络重试、pending和7类过期响应 |
| F10专用验收 | 29 passed，无warning/skip | 最终full/quick再覆盖真实响应schema、只读有效计划、恢复与人工链 |
| 静态门禁 | ruff F/S108/I/TC通过；mypy307个src文件通过；uv lock离线一致 | 20个变更Python文件future/docstring/800行/函数80行约束通过 |
| 包和镜像 | finance138/console23个src文件wheel+sdist逐字节同源；ZIP20文件同examples及设计对应文件；镜像UID10001同源 | console的23含21静态文件及两个包文件；设计另4个既有refs未冒称全树相同 |

[全量报告](evidence/finance-f10-2026-10-23/full-final/report.json)、[原始输出](evidence/finance-f10-2026-10-23/full-final/pytest.log)、[quick报告](evidence/finance-f10-2026-10-23/quick-final/report.json)、[ruff](evidence/finance-f10-2026-10-23/ruff.log)、[mypy](evidence/finance-f10-2026-10-23/mypy.log)与[变更源码约束](evidence/finance-f10-2026-10-23/changed-python-constraints.json)均为本轮实际结果。三项skip分别是内存变体不验证PG通知行锁、内存变体不验证数据库约束，以及由共享通知锁测试覆盖的DB身份锁变体；财务没有skip。

主要实际命令：

```text
FINANCE_TEST_PG_CONTAINER=xuanpu-finance-f10-pg uv run python scripts/finance_acceptance.py --dsn-file /tmp/xuanpu-finance-f10-dsn --output docs/guides/releases/evidence/finance-f10-2026-10-23/full-final --suite full --timeout 1800
FINANCE_TEST_PG_CONTAINER=xuanpu-finance-f10-pg uv run python scripts/finance_acceptance.py --dsn-file /tmp/xuanpu-finance-f10-dsn --output docs/guides/releases/evidence/finance-f10-2026-10-23/quick-final --suite quick --timeout 1800
uv run ruff check --select F,S108,I,TC packages scripts
uv run mypy packages/*/src
uv lock --check --offline
uv run pytest packages/xuanpu-cli/tests/test_examples_and_docs.py packages/xuanpu-spec/tests -q
uv build --package xuanpu-finance --out-dir /tmp/xuanpu-finance-f10-build
uv build --package xuanpu-console --out-dir /tmp/xuanpu-finance-f10-build
uv run python docker/finance/build_image.py
```

TDD/审查发现并修复：错误scope制度、合法JSONB非object在Pydantic前TypeError、完整日期校验和制度代际标注；设计也修正分类无独立enabled、孤立外部指针与恢复启动写入边界。恢复验证补备份前计划executable断言，避免错误计划本身409造成假通过。实际红日志保留。[审查记录](evidence/finance-f10-2026-10-23/reviews.json)规格与质量最终Approved；早期静态契约因未接PG的4项skip仅作探索，最终full/quick无财务skip。通用Codex Skill校验器不识别玄圃version/entry/type/tool_names平台frontmatter，采用[实际玄圃规范与稳定内核读取验证](evidence/finance-f10-2026-10-23/skill-validation.json)，不删除这些必要字段来通过异构校验器。

[制品清册](evidence/finance-f10-2026-10-23/artifacts.json)与[独立字节复核](evidence/finance-f10-2026-10-23/artifact-quality.json)确认full/quick验证时冻结702个产品/测试/脚本/契约文件；收尾仅更正finance README首段遗留的F9标题/版本/API数量与指南链接，701个其余文件完全不变，产品Python/JavaScript、测试和契约均未改。原测试清册与最终清册均保留，[唯一文档修正](evidence/finance-f10-2026-10-23/post-test-documentation.json)记录前后摘要；文档门禁重验、finance安装包和镜像重新构建并复核。wheel/sdist、业务ZIP及本地镜像保留于`/tmp/xuanpu-finance-f10-build/`；镜像`xuanpu-finance:2026.10.23.1`，ID`sha256:378b141c6bf51bc32b42007565d8a2c7b60722f364c9c34e3bdad6e60a0afba3`。独立全源检查单列基线settlement/__init__.py只有中文docstring、缺future但无注解或执行内容；与aa5dba4字节相同，本轮未动，F10所有变更源约束通过。

## 上线边界与交付

正式公司、制度、人员、生产SSO/网络/独立凭据/IAM、真实模型语义、生产负载与异机灾备仍not_verified。支付、ERP、官方查验/税务、法定归档、飞书/企业微信审批不属于当前内部记录依赖；不由内部approved、配置检查或本轮开发完成取得资格。

目录`/Volumes/Codes/lumorax/lumorax-platform/xuanpu-finance-f0`，分支feat/finance-f10，基线aa5dba4；最终中文本地提交由该分支git log查询。未推送、合并或部署生产；[清理记录](evidence/finance-f10-2026-10-23/cleanup.json)确认仅删除本轮自建PG及匿名卷、三个私有连接文件；四个测试库及临时角色随容器清理，五个测试端口关闭、两服务退出0、测试页关闭，原xuanpu检出保持干净。制品与本地镜像保留。

设计见[F10规格](../../superpowers/specs/2026-10-23-finance-f10-design.md)，步骤见[实施计划](../../superpowers/plans/2026-10-23-finance-f10.md)，使用见[F10指南](../finance-f10.md)，剩余边界见[缺口台账](../finance-gap-register.md)。
