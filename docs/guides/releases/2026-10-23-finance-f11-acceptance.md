# 财务 F11 内部试用与启动/原件验收记录

本轮范围为发票报销流转记录，不支付。业务包finance `0.12.0-f11`，77项API、24项内部操作、7个Skill；服务/镜像`2026.10.23.2`，人工页面保留`2026.10.23.1`。正式资料和环境未提供，不部署生产。

## F0–F10缺口与实现

补齐正式内部启动的强制凭据门禁与单次快照，以及受信运维停机只读verify原件核对。保持独立数据库、密文原件、HITL和既有权限，未新增API或模型维护权。

| 既有缺口 | 本轮处理 | 含义与边界 |
| --- | --- | --- |
| 离线检查可被实际启动绕过，检查后重读文件 | production/internal在副作用前重新校验，后续全部装配消费同一次不可变凭据快照 | 坏资源CLI固定退出2；成功构造不证明真实TLS、IAM或正式身份 |
| 原件认证依赖备份/恢复 | 停机维护verify在独占锁及只读repeatable_read内核对结构、持久内容和AES原件 | 完整0、不完整2、失败1；只输出十字段计数，不写回业务或API |
| 新门禁和维护入口尚无联合使用证据 | 真实CLI/签名HTTP、收件、两级真人审批与停机核对 | 实际结果下列证据确认后填入；不替代正式环境验收 |

启动红测试先真实复现25项失败、3项兼容通过；修复后F11/F6组合62项通过。verify先因缺入口报红，随后47项真实PG/local/CLI通过；另25项F4/F9维护兼容回归通过。源码及最终整轮规格与质量均独立Approved，见[审查记录](evidence/finance-f11-2026-10-23/reviews.json)；专项原始红绿日志保留。早期非PG探索的跳过不作为最终验收。

[业务包校验](evidence/finance-f11-2026-10-23/skill-validation.json)使用实际玄圃规范及稳定内核读取7个Skill；[契约统计](evidence/finance-f11-2026-10-23/contract-counts.json)为finance72+catalog5项API，内部profile24项。模型shell仍关闭，未添加人工执行、原件读取或维护命令权限。

## 验收状态

全量实际2749 passed、3项既有非财务skip，0失败、0错误；共2752用例，无缺失固定模块、无财务skip。三项skip分别为内存变体不验证PG通知事务、内存变体不验证数据库约束、共享通知锁测试已经覆盖的DB身份锁变体。固定quick实际309 passed、0 skipped/failures/errors，39个模块覆盖全部原35模块及4个F11模块；基线quick226项通过。

[固定quick报告](evidence/finance-f11-2026-10-23/quick-final/report.json)、[最终全量报告](evidence/finance-f11-2026-10-23/full-final/report.json)、[原始输出](evidence/finance-f11-2026-10-23/full-final/pytest.log)、[JUnit提取的实际F11事实](evidence/finance-f11-2026-10-23/f11-evidence.json)保留。full的partial仅对应上述3项跳过，不把它解释为生产合格。

本轮联合进程首轮及最终复跑均2项通过；独立规格2项通过/4.45秒，独立质量2项通过/5.40秒。实际JWT以EdDSA验证来源：agent执行有效批准计划返回403 HUMAN_REQUIRED且单据未变，同一计划随后由human成功执行。两级批准来自数据库决定而非填入设定值。源PDF移除并重启后仍可逐字节读取独立原件；报销原文空格和换行、事由、历史保持。

运行时verify真实退出1/INSTANCE_ACTIVE；停机后真实退出0，固定十字段报告complete、authenticated_objects=1、production_status=not_verified、business_writes=0，全部public表和密文字节前后相同。支付/记账金额均0，正式发票/结算/合规操作三表均0。真实production坏权限启动退出2、stderr为空、不创建原件根、数据库快照不变。两个财务进程真实SIGTERM退出-15，捕获应用清理完成日志，两个loopback端口关闭；未把该退出冒称0。人工页面本轮没有行为改动，未重新进行浏览器操作；旧前端行为保护仍纳入全量与quick。

[制品清册](evidence/finance-f11-2026-10-23/artifacts.json)和[独立字节复核](evidence/finance-f11-2026-10-23/artifact-quality.json)已逐字节核对wheel/sdist、ZIP与镜像。冻结[712个文件](evidence/finance-f11-2026-10-23/frozen-source.json)覆盖产品、测试、脚本及发布契约，后续收尾仅更新验收文档与证据。业务发布20文件与设计对应文件相等，设计额外4个refs与基线相同；finance源码141文件、console23文件的安装包均同源。镜像`xuanpu-finance:2026.10.23.2`，ID`sha256:d7a59db804e62c314c17da26eb91b7e1b437869996c5bd435af3fddb19df1f51`，实际UID10001。制品目录`/tmp/xuanpu-finance-f11-build/`。

[最终文档检查](evidence/finance-f11-2026-10-23/docs-final.log)202项通过。静态检查实际通过：`uv run ruff check --select F,S108,I,TC packages scripts`；`uv run mypy packages/*/src`检查310个源文件；`uv lock --check --offline`解析109依赖；[25个变更Python文件约束](evidence/finance-f11-2026-10-23/changed-python-constraints.json)及圈复杂度通过。

## 实际入口与交付

```bash
FINANCE_TEST_PG_CONTAINER=xuanpu-finance-f11-pg uv run python scripts/finance_acceptance.py --dsn-file /tmp/xuanpu-finance-f11-dsn --output docs/guides/releases/evidence/finance-f11-2026-10-23/full-final --suite full --timeout 1800
FINANCE_TEST_PG_CONTAINER=xuanpu-finance-f11-pg uv run python scripts/finance_acceptance.py --dsn-file /tmp/xuanpu-finance-f11-dsn --output docs/guides/releases/evidence/finance-f11-2026-10-23/quick-final --suite quick --timeout 1800
uv run ruff check --select F,S108,I,TC packages scripts
uv run mypy packages/*/src
uv lock --check --offline
uv build --package xuanpu-finance --out-dir /tmp/xuanpu-finance-f11-build
uv build --package xuanpu-console --out-dir /tmp/xuanpu-finance-f11-build
uv run python docker/finance/build_image.py
```

维护与服务CLI均已在本地交付镜像中以`--network none`运行`--help`并退出0，见[入口检查](evidence/finance-f11-2026-10-23/image-entrypoints.json)；这只证明入口能加载，不代表业务运行或生产部署。实际维护完整/不完整/失败报告来自真实PG测试和联合进程JUnit。

目录`/Volumes/Codes/lumorax/lumorax-platform/xuanpu-finance-f0`，分支`feat/finance-f11`，基线`3b50c95`；中文本地提交在本分支git log查询。保持原xuanpu检出干净，不推送、合并或部署。[清理记录](evidence/finance-f11-2026-10-23/cleanup.json)确认只删除本轮自建PG容器及其匿名卷、私有DSN文件，端口52262关闭，F11标记容器已不存在；独立夹具库及调试时留在基准库的默认迁移状态随容器删除。未清理其它项目容器/数据。联合进程和网关端口关闭事实来自本轮真实JUnit；无浏览器页面需要清理。原xuanpu检出干净，制品、镜像和分支保留。

[凭据扫描](evidence/finance-f11-2026-10-23/secret-scan.json)在删除DSN前对102个变更文件执行，未发现本轮连接正文或密码。最终验收文档/证据在冻结后更新，712个产品/测试/脚本/契约文件保持逐字节一致。

## 未验边界

测试使用独占loopback PostgreSQL、明确测试身份/文件网关和local密文。真实公司/人员/制度、生产SSO、独立凭据/IAM/网络、真实模型语义、生产负载、S3/异机恢复仍not_verified。未推送、合并或部署。

设计见[F11规格](../../superpowers/specs/2026-10-23-finance-f11-design.md)，步骤见[实施计划](../../superpowers/plans/2026-10-23-finance-f11.md)，使用见[F11指南](../finance-f11.md)，剩余边界见[缺口台账](../finance-gap-register.md)。


[原始日志格式说明](evidence/finance-f11-2026-10-23/log-whitespace.json)：pytest红测试跟踪和Docker输出自带行尾空格，原样保全。首次本地提交未因该格式检查退出2而停下，随后已确认全部告警仅在三份原始日志；源码和文档的非日志diff检查退出0。产品、测试和制品未因此改动。
