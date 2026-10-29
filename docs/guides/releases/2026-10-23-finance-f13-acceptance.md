# 财务F13原件目录边界与人工续办验收

当前仅作内部发票申报、报销材料和真人审批流转记录，不支付。业务包`0.14.0-f13`，财务服务/镜像`2026.10.23.4`，人工页面保持`2026.10.23.1`。77项API、24内部操作、7个Skill和模型工具列表保持；shell=false。正式公司、人员和环境仍未验收，不推送、合并或部署。

## 本轮实际变更

本地加密原件IO改用标准POSIX目录描述符、不跟随链接标志及相对文件操作，root/shard/leaf中的符号链接与特殊文件明确拒绝。叶子先stat再非阻塞open/fstat，临时文件以私有权限和原子链接创建，重复相同内容保持幂等，不覆盖已有密文。XF01/AES-GCM/AAD、对象键、schema、API、业务状态与S3适配不变。其它真实IO故障继续传播，不把存储故障伪装为成功。

Skill仅说明异常交受信管理员核对独立原件目录，再查原操作并按当前资格补充或续办，不获得读原件、维护或代审批权限。两份业务包20个对应文件逐字节相同，额外4个设计refs保持c239a52字节。真实xuanpu_spec校验、taifeng registry读取和工具名单比较见[契约证据](evidence/finance-f13-2026-10-23/skill-contract-validation.json)。

## 红绿和专项证据

既有基线c239a52固定quick实际332 passed。对象红测与真实PG责任链红测保存在objects-red.log，修复后对象模块43 passed、原件/材料/维护/撤权和强退回归121 passed；任务一独立质量重跑43 passed。测试下一安全shard原先仍有随机碰撞风险，已用仅测试受理ID固定的方式消除，实际责任key、PG持久化与worker均保留；确定碰撞红测与修复后43项复跑分别保存在objects-review-*，不以扩大随机重试冒称确定性。

联合用例在临时产品副本中恢复c239a52对象实现进行真实CLI红测，实际GET返回200且含原件；失败证据只保存状态与正文存在布尔值，不保存原件。当前实现最终专项1 passed，新旧F13/F11/F12/F10进程回归8 passed，任务二独立质量重跑1 passed。首轮探索因新测试错误假设decision.data含operation_id而失败；修正为按plan查询真实business_operation及outbox，不改产品或放宽合同。首轮失败和最终绿测分开保留。历史失败输出曾包含本轮随机测试凭据，仅对该值精确脱敏；XML安全标记修复及前/中/后摘要见[脱敏记录](evidence/finance-f13-2026-10-23/process-failure-redaction.json)，全部process XML可解析，原失败结构保留。

Linux补充检查在既有F12镜像内只读挂载本轮财务源码、network=none、UID10001，验证正常读写/幂等、链接get/put拒绝、外部密文不变及恢复后可读；见[Linux对象检查](evidence/finance-f13-2026-10-23/linux-objects.json)。另在最终F13镜像安装包（不挂载源码）完成同等基础检查，见[最终镜像IO](evidence/finance-f13-2026-10-23/linux-final-image-objects.json)。这些结果不是整套Linux业务、生产或灾备验收。

## 最终全量中的联合事实

以下事实均取自full-final/junit.xml的f13_process属性，逐字提取见[最终事实](evidence/finance-f13-2026-10-23/f13-evidence.json)。

| 检查 | 本轮实际结果 |
| --- | --- |
| 真实签名真人读取异常原件 | HTTP 500 / OBJECT_PATH_UNSAFE；无content_base64、路径或正文 |
| 业务与外部字节 | 79张非replay public表逐表相同；外部目录密文字节不变 |
| 防重放安全记账 | 本次实际EdDSA签名新增1行；删除0行，新增字段匹配实际签名，既有行不变、移除须已到期 |
| 审批 | 实际请求状态[403, 202, 403, 202]，对应agent/human逐级决定；DB实际两级human批准 |
| 最终记录 | approved，原申报与理由保持，paid=0.00、posted=0.00；正式invoice/settlement/compliance均0 |
| 独立原件 | 平台源移除、下载1次；管理员显式恢复原密文后逐级读取及最终复核均为原字节 |
| 停机核对 | verify=complete，authenticated_objects=1，workers_started=false、business_writes=0；全表/密文快照相同 |
| 进程 | 实际退出-15，graceful=True，服务及网关端口已关闭 |

## 全量和固定快速验收

| suite | 用例总数 | passed | skipped | failures/errors |
| --- | --- | --- | --- | --- |
| full | 2827 | 2824 | 3 | 0 / 0 |
| quick | 376 | 376 | 0 | 0 / 0 |

[全量报告](evidence/finance-f13-2026-10-23/full-final/report.json)与[快速报告](evidence/finance-f13-2026-10-23/quick-final/report.json)绑定实际HEAD/工作树、原始pytest/JUnit摘要和独占finance_test库。quick共44模块，原42顺序完整保留；财务无skip，所有固定模块均存在。full的partial仅对应以下既有非财务skip，不计作通过：

- `packages.xuanpu-server.tests.reminders.test_postgres_order.test_same_employee_notification_commit_order[memory]`
- `packages.xuanpu-server.tests.workspace.test_proactive_store.test_database_source_constraints_are_enforced[memory]`
- `packages.xuanpu-server.tests.workspace.test_proactive_store.test_memory_disable_reenable_fences_open_snapshot[postgres]`

静态门禁见[Ruff](evidence/finance-f13-2026-10-23/static-ruff.log)、[strict Mypy](evidence/finance-f13-2026-10-23/static-mypy.log)、[离线lock](evidence/finance-f13-2026-10-23/static-lock.log)和[Python约束](evidence/finance-f13-2026-10-23/source-constraints.json)；文档检查见[docs-final.log](evidence/finance-f13-2026-10-23/docs-final.log)。源码冻结722文件，相对F12的717文件仅新增清册列出的5文件，未删除文件；构建和最终复核同时比较完整文件集合与摘要。

## 制品与镜像

- `finance-0.14.0-f13.zip`：54950 字节，SHA-256 `57009f597688a28ac28c8d59745f0929af2717f05f4687759b4172e795c22a46`。
- `xuanpu_console-2026.10.23.1-py3-none-any.whl`：110516 字节，SHA-256 `0c340e0112933f1550fa7e0686bcefd2e380e9f150a1956f5b89093c6767066b`。
- `xuanpu_console-2026.10.23.1.tar.gz`：134531 字节，SHA-256 `8f1c34d598852449482566bf7371b163ab29a83d20908eac0bd2fab07469fc9d`。
- `xuanpu_finance-2026.10.23.4-py3-none-any.whl`：347262 字节，SHA-256 `0f6e7c0c3369f5f5fd1d3cd1470d15f95bad8caa97cc6fbab72af6e4d5173f77`。
- `xuanpu_finance-2026.10.23.4.tar.gz`：453388 字节，SHA-256 `06c6c4923f28371f20c5f0f2e5b312cca12153b3358b22e57871e8ce84db9d2d`。

制品保留在`/tmp/xuanpu-finance-f13-build`。finance/console的wheel、sdist源码、README/元数据与当前冻结文件核对，业务ZIP与20个发布文件及设计对应文件一致。[制品清册](evidence/finance-f13-2026-10-23/artifacts.json)包含逐文件SHA和完整冻结集合检查。

镜像`xuanpu-finance:2026.10.23.4`，ID `sha256:e4f8b67986e25fcf10db286f6f1e978bfcca1f6511ee877c83015cbd19192122`，实际UID10001、版本2026.10.23.4，财务源码142文件与冻结源摘要相同。network=none下服务和维护help均退出0，证明入口可调用；不表示正式镜像部署或生产业务演练。本轮人工页面未改行为、未重跑浏览器，行为保护由固定quick和全量执行；历史浏览器证据不能替代本轮生产验收。

## 审查、清理与交付

任务一/二规格及质量均独立Approved，审查记录区分只读证据核验与实际PG复跑。整轮审查状态以[审查清册](evidence/finance-f13-2026-10-23/reviews.json)的最终结果为准。

本轮独占PG容器、匹配ID的专用卷与私有DSN已清理，数据库端口关闭；测试库、其它会话及锁清理前均0，原xuanpu检出核对结果见[清理记录](evidence/finance-f13-2026-10-23/cleanup.json)。[清理前资源审计](evidence/finance-f13-2026-10-23/test-resource-audit.json)与[凭据扫描](evidence/finance-f13-2026-10-23/secret-scan.json)保留实际范围和结果；扫描后仅收尾无凭据的报告及审查元数据。分支、制品和镜像保留。

本地目录`/Volumes/Codes/lumorax/lumorax-platform/xuanpu-finance-f0`，分支`feat/finance-f13`，基线`c239a52`；本轮中文本地提交可在该分支git log查询。没有推送、合并或部署。真实公司/制度/人员、SSO/独立凭据/IAM/网络、真实模型语义、生产负载、S3/异机恢复与正式部署继续not_verified。配置根祖先、受信运维独占权限、本机管理员/硬链接/挂载更换、掉电和Windows的边界见[F13指南](../finance-f13.md)与[F13设计](../../superpowers/specs/2026-10-23-finance-f13-design.md)。

## 实际执行入口

```bash
NO_PROXY=127.0.0.1,localhost,::1 no_proxy=127.0.0.1,localhost,::1 FINANCE_TEST_PG_CONTAINER=xuanpu-finance-f13-pg uv run python scripts/finance_acceptance.py --dsn-file /tmp/xuanpu-finance-f13-dsn --output docs/guides/releases/evidence/finance-f13-2026-10-23/full-final --suite full --timeout 1800
NO_PROXY=127.0.0.1,localhost,::1 no_proxy=127.0.0.1,localhost,::1 FINANCE_TEST_PG_CONTAINER=xuanpu-finance-f13-pg uv run python scripts/finance_acceptance.py --dsn-file /tmp/xuanpu-finance-f13-dsn --output docs/guides/releases/evidence/finance-f13-2026-10-23/quick-final --suite quick --timeout 1800
uv run ruff check --select F,S108,I,TC packages scripts
uv run mypy packages/*/src
uv lock --check --offline
uv build --package xuanpu-finance --out-dir /tmp/xuanpu-finance-f13-build
uv build --package xuanpu-console --out-dir /tmp/xuanpu-finance-f13-build
uv run python docker/finance/build_image.py
```

后续F14维护凭据安全、配套恢复和真人续办见[F14指南](../finance-f14.md)与[F14验收](2026-10-23-finance-f14-acceptance.md)；本页F13版本、制品、数字及历史证据保持，不代表F14最终结果。
