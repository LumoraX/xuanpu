# 财务F14维护凭据安全与恢复后真人续办验收

当前仅作内部发票申报、报销材料和真人审批流转记录，不支付。业务包`0.15.0-f14`，财务服务/镜像`2026.10.23.5`，人工页面保持`2026.10.23.1`。77项API、24内部操作、7个Skill、模型工具名单和shell=false保持。正式公司、人员和环境仍未验收；本轮仅本地提交，不推送、合并或部署。

## 本轮实际变更

按[F0–F14缺口台账](../finance-gap-register.md)与[F14设计](../../superpowers/specs/2026-10-23-finance-f14-design.md)，优先补齐备份/恢复维护凭据安全及恢复后人工续办。复用既有read_fixed/read_text_secret：拒绝不安全叶子权限、符号链接、非普通文件及超限输入，在连接、工具和暂存之前在线程中单次捕获DSN/AES。32字节长度与维护DSN TLS白名单错误沿用既有契约；verify继续消费已捕获快照。未知维护异常只返回固定JSON错误码，保留恢复可能已提交的原错误。

无新表、API、业务状态、支付或模型维护权限。两份业务包20个对应文件逐字节同步；额外4个设计refs保持da624a9字节。Skill前后独立模拟应用见[baseline](evidence/finance-f14-2026-10-23/skill-scenario-baseline.json)与[forward](evidence/finance-f14-2026-10-23/skill-scenario-forward.json)；baseline原通用边界已正确，本轮补维护凭据与有限失败细节，不把模拟答复当真实产品模型验收。真实xuanpu_spec和taifeng registry验证见[契约证据](evidence/finance-f14-2026-10-23/skill-contract-validation.json)。管理员维护不等同业务批准；恢复后仍须按当前角色由真人确认。

## 基线、红绿与专项证据

干净da624a9固定quick基线376 passed。实现前凭据红测56失败/11通过；修正测试夹具后隔离基线重放仍为56失败/11通过，当前新模块67 passed。初始34字节公开哨兵、Unix socket路径长度探索失败均保留，未算作产品回归；完整过程及14份日志/XML摘要见[凭据证据](evidence/finance-f14-2026-10-23/credentials-evidence.json)。F4/F5/F10/F11/F13顺序真实PG回归105 passed；任务一质量独立复跑见[质量审查](evidence/finance-f14-2026-10-23/task1-quality.json)。TLS仅验证参数解析兼容，未作真实握手验收。元数据初轮35通过、2项因未供PG跳过；随后明确排除这两项的纯合同35通过只算无PG检查，最终full已实际执行这两项且财务无skip，详情见[阶段证据](evidence/finance-f14-2026-10-23/metadata-docs.json)。

联合专项将maintenance/CLI在隔离副本中退回da624a9，真实坏凭据CLI断言失败；当前实现专项通过。首轮红测长traceback含测试凭据，已按实际值精确脱敏并保存前后摘要，记录见[过程证据](evidence/finance-f14-2026-10-23/process-evidence.json)与[脱敏记录](evidence/finance-f14-2026-10-23/process-redaction.json)，不把失败计作通过。真实CLI坏凭据拒绝、有效配套备份/恢复、目标新库与独立原件根重启、恢复后两级真人审批及停机只读核对的事实，均取自最终full JUnit属性，见[最终联合事实](evidence/finance-f14-2026-10-23/f14-evidence.json)。断言覆盖坏输入有限错误与源/目标逐表和密文不变、实际PG工具、备份认证、原operation/receipt保持、平台源移除后仍可读、逐级agent拒绝和human批准、最终approved且paid/posted为0.00、正式三表0、verify无业务写和worker，以及真实进程退出和端口关闭。独立规格审查发现初版暂存断言扫错父目录，已改为核对实际backup/restore父目录并重新运行专项；初版结论及修正证据保留。联合中的human是隔离测试签名身份，正式人员尚未验收。Task2专项与独立复跑结果见[任务二规格](evidence/finance-f14-2026-10-23/task2-spec.json)及[质量](evidence/finance-f14-2026-10-23/task2-quality.json)。

最终Linux安装镜像、UID10001、network=none的8项坏凭据CLI拒绝演练见[Linux基础检查](evidence/finance-f14-2026-10-23/linux-final-image-credentials.json)。这仅覆盖基础入口拒绝，不代表整套Linux业务、真实TLS、浏览器或生产验收。

以下联合事实直接取自最终full JUnit，不以专项历史结果代填：

| 检查 | 最终full实际结果 |
| --- | --- |
| 坏凭据实际CLI | 8组全部退出1 / MAINTENANCE_INPUT_INVALID，PG工具0调用；实际父目录暂存清单前后均空 |
| 配套维护 | backup退出0；restore退出0；恢复前全表/密文相等，HMAC与原件认证通过 |
| 实际确认 | 状态[403, 202, 403, 202]，来源['agent', 'human', 'agent', 'human']；目标库两级human决定 |
| 最终记录 | approved，paid=0.00、posted=0.00；正式三表均0 |
| 原件与停机 | 下载1次，原operation/receipt保留；verify complete、1对象、0写/0worker；进程实际退出[-15, -15]且两端口关闭 |

## 全量与固定快速验收

| suite | 用例总数 | passed | skipped | failures/errors |
| --- | --- | --- | --- | --- |
| full | 2899 | 2896 | 3 | 0 / 0 |
| quick | 444 | 444 | 0 | 0 / 0 |

[全量报告](evidence/finance-f14-2026-10-23/full-final/report.json)与[快速报告](evidence/finance-f14-2026-10-23/quick-final/report.json)绑定实际HEAD/工作树及原始pytest/JUnit摘要。quick共46模块，原44个模块顺序保持、前置两项F14模块；财务无skip/fail/error/missing。full的partial仅对应以下非财务skip，未计作通过：

- `packages.xuanpu-server.tests.reminders.test_postgres_order.test_same_employee_notification_commit_order[memory]`
- `packages.xuanpu-server.tests.workspace.test_proactive_store.test_database_source_constraints_are_enforced[memory]`
- `packages.xuanpu-server.tests.workspace.test_proactive_store.test_memory_disable_reenable_fences_open_snapshot[postgres]`

静态门禁见[Ruff](evidence/finance-f14-2026-10-23/static-ruff.log)、[strict Mypy](evidence/finance-f14-2026-10-23/static-mypy.log)、[离线lock](evidence/finance-f14-2026-10-23/static-lock.log)、[Python约束](evidence/finance-f14-2026-10-23/source-constraints.json)及[文档检查](evidence/finance-f14-2026-10-23/docs-final.log)。首轮收尾文档检查为213通过/1链接失败，原因是汇总器误将设计审查的verdict当作status、未生成清册；已显式适配真实字段并重新生成清册，最终214项文档检查全部通过。原始失败与摘要见[收尾诊断](evidence/finance-f14-2026-10-23/closing-orchestration-diagnostic.json)，产品与冻结输入未改。冻结726文件，比F13的722文件仅新增四个F14测试/辅助文件，无删除；构建及最终复核同时比较完整文件集合和摘要。

## 制品与镜像

- `finance-0.15.0-f14.zip`：55274 字节，SHA-256 `31cdddfbee7b39e81a5618d8e5e46a98ef4ddbd1f45af4c85f9e3a218c1902a6`。
- `xuanpu_console-2026.10.23.1-py3-none-any.whl`：110516 字节，SHA-256 `0c340e0112933f1550fa7e0686bcefd2e380e9f150a1956f5b89093c6767066b`。
- `xuanpu_console-2026.10.23.1.tar.gz`：134531 字节，SHA-256 `8f1c34d598852449482566bf7371b163ab29a83d20908eac0bd2fab07469fc9d`。
- `xuanpu_finance-2026.10.23.5-py3-none-any.whl`：348164 字节，SHA-256 `ac3f96199ca7ab38c046dd73fffb3fd7da9da6f87a1313c9112e31f9a5bfd703`。
- `xuanpu_finance-2026.10.23.5.tar.gz`：463215 字节，SHA-256 `ef11346ded998a4eda7d5d8418a5e5f1f2d72d8894cadb0c3df52a8baa4d4854`。

制品保留在`/tmp/xuanpu-finance-f14-build`。finance/console的wheel、sdist源码、包内README/元数据与冻结文件逐项一致；ZIP与20个发布文件及设计对应文件一致。sdist完整非生成集合还逐字核对打包工具继承的仓库根.gitignore；初次核对器遗漏该已知来源的期待项，诊断及修正见[核对诊断](evidence/finance-f14-2026-10-23/artifacts-check-diagnostic.json)，未修改产品、冻结源或重建制品。[制品清册](evidence/finance-f14-2026-10-23/artifacts.json)保存实际SHA及冻结集合检查。

镜像`xuanpu-finance:2026.10.23.5`，ID `sha256:76a607ac212c7e15c392f927e36a8b52b88424152768c8ef5691743bea8b54a8`，实际UID10001、版本2026.10.23.5，财务源码142文件与冻结源相同。本轮未改人工页面行为、未重跑浏览器；固定quick/全量保护现有行为，历史浏览器证据不能替代正式环境验收。

## 审查、资源清理与交付

任务一/二独立规格和质量、整轮规格/质量及制品审查结果见[审查清册](evidence/finance-f14-2026-10-23/reviews.json)，各记录分别写明阅读证据、实际复跑与未验范围，不能把独立专项复跑说成独立重跑全量。

仅清理本轮匹配ID/label的专用PG、卷、私有DSN及自建进程，实际资源审计与清理结果见[资源审计](evidence/finance-f14-2026-10-23/test-resource-audit.json)、[清理记录](evidence/finance-f14-2026-10-23/cleanup.json)。原xuanpu检出保持干净。[精确凭据扫描](evidence/finance-f14-2026-10-23/secret-scan.json)记录扫描范围；扫描后仅补充不含凭据的收尾报告和审查元数据。原始失败日志/XML的空白诊断逐项列入[空白清册](evidence/finance-f14-2026-10-23/log-whitespace.json)，其它staged检查为0。

本地目录`/Volumes/Codes/lumorax/lumorax-platform/xuanpu-finance-f0`，分支`feat/finance-f14`，基线`da624a9`。交付范围为中文本地提交，最终提交记录以该分支git log为准；没有推送、合并或部署。真实公司/制度/人员、SSO/正式凭据/IAM/网络、真实模型语义、生产负载、S3/异机恢复及部署仍not_verified。固定凭据的可信祖先目录、管理员独占权限和多文件非原子轮换边界见[F14指南](../finance-f14.md)。

## 实际执行入口

```bash
NO_PROXY=127.0.0.1,localhost,::1 no_proxy=127.0.0.1,localhost,::1 FINANCE_TEST_PG_CONTAINER=xuanpu-finance-f14-pg uv run python scripts/finance_acceptance.py --dsn-file /tmp/xuanpu-finance-f14-dsn --output docs/guides/releases/evidence/finance-f14-2026-10-23/full-final --suite full --timeout 1800
NO_PROXY=127.0.0.1,localhost,::1 no_proxy=127.0.0.1,localhost,::1 FINANCE_TEST_PG_CONTAINER=xuanpu-finance-f14-pg uv run python scripts/finance_acceptance.py --dsn-file /tmp/xuanpu-finance-f14-dsn --output docs/guides/releases/evidence/finance-f14-2026-10-23/quick-final --suite quick --timeout 1800
uv run ruff check --select F,S108,I,TC packages scripts
uv run mypy packages/*/src
uv lock --check --offline
uv build --package xuanpu-finance --out-dir /tmp/xuanpu-finance-f14-build
uv build --package xuanpu-console --out-dir /tmp/xuanpu-finance-f14-build
uv run python docker/finance/build_image.py
```
