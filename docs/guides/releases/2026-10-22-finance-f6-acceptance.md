# 财务F6本地与隔离验收

日期2026-10-22（Asia/Shanghai）。基线7516a79，分支feat/finance-f6。范围是F0–F5缺口梳理、正式接入准备工具和可重复隔离验收；独立PG/加密原件、进程外API、7个Skill、实际员工HITL及原请求核对保持。本轮不推送、不合并、不部署正式环境。

## 本轮交付

- 离线`--preflight`只读显式固定配置和凭据，验证私密权限、有界普通文件、令牌分离、Ed25519/AES/回调及S3文本格式、DSN/TLS、公司制度及渠道前提；不连接资源或启动worker，错误固定脱敏。
- 新增`getFinanceReadiness`，当前同公司reviewer从只读repeatable_read独立PG快照检查实际当前制度、授权/代数、通道版本/启停、账户、账簿期间、3种可付款资金场景与5种记账场景；摘要绑定scope及当前事实。模式/锁/后台运行态分别反馈，检查不会产生审批、执行或正式许可。
- 业务包finance0.7.0-f6、70项实际API、7个Skill及参考pack同步；只增加finance-review的只读工具，原敏感人工操作未给模型。
- 仓库`scripts/finance_acceptance.py`以显式本机finance_test实库和固定模块执行pytest，清掉隐式平台DB/PG/pytest选择环境，读取实际JUnit，不信suite自报总数；失败/零测/漏模块/财务skip不得passed，full非财务skip显式partial。源码、实际计数、时间、日志/JUnit SHA保全，取消/期限终止整个自有进程组并记录清理要求。
- 临时两库/两个独立非超级角色验证各自可建表读写、双向CONNECT拒绝；严格验证测试DSN且finally删临时库/角色。仅证明本轮隔离机制，不能代替正式凭据/对象IAM验收。

所有准备报告始终保留`production_status=not_verified`。正式制度、真实供应商、官方票样/签章、WORM、异机DR、真实模型及生产负载不能由配置或模拟结果关闭，见[供应商验收清单](../finance-provider-acceptance.md)和[缺口台账](../finance-gap-register.md)。

## 缺陷与复核闭环

[初始readiness RED](evidence/finance-f6-2026-10-22/readiness-red.log)、[规格补测RED](evidence/finance-f6-2026-10-22/readiness-spec-red.log)、[质量补测RED](evidence/finance-f6-2026-10-22/readiness-quality-red.log)保留invoice_backed、精确归档profile及S3文本等真实阻断。修后领域预检/物化/准备报告共用原F3归档资格函数，未扩大正式归档范围；质量独立复验59passed/no skip。

[runner RED](evidence/finance-f6-2026-10-22/runner-review-red.log)证明遗留平台DSN及组长退出后持pipe的后代会越过输入/期限边界；[GREEN](evidence/finance-f6-2026-10-22/runner-review-green.log)验证清除隐式库入口、实际120秒后代按1秒期限退出且PID不存在、真实失败子进程和JUnit脱敏。真实两库演练[2passed](evidence/finance-f6-2026-10-22/isolation-green.log)。Task2质量独立17passed/no skip。

[契约RED](evidence/finance-f6-2026-10-22/contract-red.log) / [19项GREEN](evidence/finance-f6-2026-10-22/contract-green.log)验证实际响应和70路由/参数/工具声明一致；[业务包](evidence/finance-f6-2026-10-22/pack-validation.log)校验通过。三项规格及质量复核见[复核记录](evidence/finance-f6-2026-10-22/reviews.json)。

## 完整门禁与可重复端到端

冻结源码的[quick最终报告](evidence/finance-f6-2026-10-22/quick-final/report.json)为77passed、0fail/error/skip、11个固定模块无遗漏，实际运行57.42秒，见[JUnit](evidence/finance-f6-2026-10-22/quick-final/junit.xml)和[日志](evidence/finance-f6-2026-10-22/quick-final/pytest.log)。

首轮full有2495passed、4failed、3既有非财务skip；4项均是文档测试执行时验收报告尚未落盘导致相对链接失败，保存[原结果](evidence/finance-f6-2026-10-22/full-before-doc-finalization/report.json)和[失败日志](evidence/finance-f6-2026-10-22/full-before-doc-finalization/pytest.log)。已在完整报告落盘后核对148个本地链接0缺失，并通过[全部文档测试](evidence/finance-f6-2026-10-22/doc-links-green.log)；第二轮在报告及证据落盘后完整重跑：[最终full报告](evidence/finance-f6-2026-10-22/full-final/report.json)、[真实JUnit](evidence/finance-f6-2026-10-22/full-final/junit.xml)、[日志](evidence/finance-f6-2026-10-22/full-final/pytest.log)记录 **2500 passed、0 failed/error、3 skipped，402.73秒**。三项skip均为原有非财务数据库专项（日志逐项列出），财务无skip；汇总器因此如实标记partial/有skip，而非零跳过passed。没有用首轮业务绿测掩盖链接失败。

实际执行入口为：

```sh
uv run python scripts/finance_acceptance.py --dsn-file /tmp/finance-f6-dsn --output /tmp/finance-f6-full-final2 --suite full
uv run python scripts/finance_acceptance.py --dsn-file /tmp/finance-f6-dsn --output /tmp/finance-f6-quick-final --suite quick
uv run ruff check --select F,S108,I,TC packages scripts/finance_acceptance.py scripts/acceptance/finance_runner.py
uv run mypy packages/*/src
MYPYPATH=scripts uv run mypy scripts/finance_acceptance.py
uv run ruff check --select C901 --config 'lint.mccabe.max-complexity=10' packages/xuanpu-finance/src scripts/finance_acceptance.py scripts/acceptance/finance_runner.py
uv lock --check
uv run xuanpu validate examples/finance
```

命令中的DSN文件只用于本轮loopback隔离库，收尾删除。Ruff全绿、strict mypy293个源码文件通过，runner单独类型检查通过；21个变化Python文件的future/中文docstring/尺寸均无违规，最大文件311行、最大函数72行，见[Ruff](evidence/finance-f6-2026-10-22/ruff.log)、[mypy](evidence/finance-f6-2026-10-22/mypy.log)、[runner mypy](evidence/finance-f6-2026-10-22/runner-mypy.log)、[复杂度](evidence/finance-f6-2026-10-22/complexity.log)、[尺寸](evidence/finance-f6-2026-10-22/source-constraints.json)、[锁文件](evidence/finance-f6-2026-10-22/lock-check.log)。

实际CLI[离线演练](evidence/finance-f6-2026-10-22/preflight-smoke.json)中，真实私密凭据资源检查通过，缺业务配置明确blocked并退出2，production仍not_verified；不把结构正确或凭据可读当作已连供应商。临时配置和秘密已在演练结束删除。

## 制品与当前镜像实际运行

finance2026.10.22.3 wheel/sdist已实际构建；124个源码文件与两个制品逐字节一致。console本轮没有源码或行为改动，复用F5的2026.10.22.2 wheel/sdist并独立重核21个文件一致，不声称重新开发或重建了前端。镜像xuanpu-finance:2026.10.22.3的124个文件也逐字节匹配，UID10001，ID为`sha256:46927de8ab8a6296c2c9f1ed054e8bef7bb90168e7d9c57e289626b08967c3b2`。见[制品及锁SHA](evidence/finance-f6-2026-10-22/artifacts.json)、[镜像源码](evidence/finance-f6-2026-10-22/image-source.json)、[构建](evidence/finance-f6-2026-10-22/build-finance.log)、[镜像构建](evidence/finance-f6-2026-10-22/image-build.log)。

[当前镜像Compose演练](evidence/finance-f6-2026-10-22/compose-smoke.json)使用专属PG/原件卷和真实回环HTTP，实际重启3次。预付原请求POST1次、未知后重启GET原号1次、隔离已付100.00，原件可读；税务查单和档案只读审计也恢复。F6只读检查实际返回configured_simulation/not_verified，四worker均running；reviewer可读、collector得到403。项目与卷已清理。它不证明真实银行/ERP/税务供应商、正式WORM或生产行为。

F6没有新增控制台页面，本轮通过签名HTTP、真实cookie/CSRF既有端到端和当前镜像验证；F5浏览器截图是历史阶段证据，不充作F6新截图或真实模型验收。源数据含隔离合成票和测试材料，官方真实票样/签章、模型语义、生产负载及异机恢复未运行。

## 收尾状态

[实际清理记录](evidence/finance-f6-2026-10-22/cleanup.json)确认删除前财务临时schema、临时库和隔离角色均为0，核对ID/本轮标签后删除专属PG和匿名卷；Compose容器/卷为空，64677端口关闭，入口DSN文件和离线/Compose临时运行目录已删除。原平台检出干净，构建制品、镜像和F6分支工作目录保留。最终规格及质量复核均Approved，无剩余本轮阻断，见复核记录；[实施计划](../../superpowers/plans/2026-10-22-finance-f6.md)四项任务全部完成。本记录与F6源码同次本地中文提交，实际提交标识由git log -1及交付消息核对，不推送、合并或部署正式环境。
