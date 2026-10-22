# 财务 F9 内部使用与上线准备验收

F9内部发票报销记录范围的开发与隔离验收已完成，不做支付。独立财务PostgreSQL、加密原件、进程外API、7个Skill和真实人工确认协议保持一致。业务包finance `0.10.0-f9`共76项API；内部记录实例只登记23项内部操作。财务发行`2026.10.22.6`，人工页`2026.10.22.5`。当前没有正式公司、人员或部署环境，本轮未部署生产。

## F0–F8缺口及本轮交付

- 旧列表携带完整发票申报、每页仅一条：新增严格九字段摘要、默认20条/最大50、本人/可见范围、状态、单号/事由字面搜索及独立总数。详情和历史继续读取完整原文；过滤变化和迟到请求隔离，失败按原游标恢复。
- 缺少内部记录专用部署边界：新增显式internal_records配置、固定HTTP白名单、实际计划/目标守卫；不构造外部结算/合规供应商客户端或worker，不登记回调。general继续兼容原阶段。
- 旧库可能继续处理通用历史：实例取锁后、迁移/授权写入和网关客户端创建前，使用同一只读快照核对固定80表/680列及类型、全租户历史、待执行导入、计划和模拟标记；未知结构或非内部业务拒绝，不清理或重写旧历史来放行。
- 实际公司/人员/凭据尚未准备：提供明确待填写的[配置模板](../../../examples/finance-internal-records/config.example.yaml)、专用离线预检及[操作指南](../finance-f9.md)，涵盖只读启动、独立凭据、授权核对、备份、停用和回退。未填模板blocked/退出2；明确测试配置检查通过仍not_verified，不产生批准或上线资格。
- 内部概览和提醒仅调用可用接口；支持材料异常在本人收件元数据中按exception_id定位实际项，最多五批一页，不借通用异常/资金接口，也不把新上传或提醒已读当异常解除。

## 真实浏览器与独立存储

[最终浏览器记录](evidence/finance-f9-2026-10-22/browser-acceptance.json)使用真实loopback平台/财务TCP、实际cookie/CSRF、平台签名出口、FinanceAuthenticator、独立PG和加密原件。平台控制面与测试身份使用隔离夹具，浏览器自动化扮演三个测试员工；并非实际员工决定、生产SSO、正式制度或真实模型验收。UI里的F8测试人员名称来自复用夹具，不代表生产账号。

最终空白库内完成上传→独立收件→整页刷新恢复→原样发票申报及附件草稿→本人/草稿/百分号字面搜索→原文不被列表搜索→本人预检确认提交→负责人读原件并带理由驳回→本人替换附件保存修订重提→负责人和财务复核分别确认批准。单据`a7663bc1890d4de2b58fda021ae448d2`最终approved、版本6、100.00元；reviewer“本人草稿”是0，授权可见+approved筛选是1，内部概览只展示收件/审批/站内提醒。

[实际数据库](evidence/finance-f9-2026-10-22/browser-database.json)保留6个不可变版本、3次决定reject/approve/approve，均origin=human且审批人不同于申请人。真实驳回意见“请补充交通行程附件”由历史读取；申报首尾空格和换行原样保留，当前仅关联补充附件，历史版本1仍关联被替换原件。历史总数9，不从当前状态推算过去决定。

[源文件移除](evidence/finance-f9-2026-10-22/browser-source-removal.json)只移除两份SHA精确匹配的本轮平台测试PDF，保留业务包对象。之后补充附件与版本1旧附件仍能从财务独立副本读取、下载，[三次字节核对](evidence/finance-f9-2026-10-22/browser-bytes.json)与保留的有效PDF完全相等，pypdf均可读取。

另外在[故障注入](evidence/finance-f9-2026-10-22/browser-fault-injection.json)中，平台上传后、财务确认前只移除一份独有测试原件。服务实际产生failed项、异常及站内提醒；页面通过内部收件API定位原批次，显示FILE_DOWNLOAD_FAILED，未调用内部未开放的listExceptions，也未把别的批次当成该异常。

实际计数为4次支持材料操作、3个回执、1项源下载失败和1个异常；正式invoice、settlement_operation、compliance_operation均为0。两份报销附件是有效测试PDF；另一次故障探索中的损坏PDF仅作为原始支持字节保全，未用于报销，保持未查验。material_received表示字节已接收，不能解释为格式或发票验真通过。[模型计数](evidence/finance-f9-2026-10-22/browser-service-evidence.json)实际派发0，后台3次factory尝试全部被阻断；人工流程在模型不可用时完成，不证明真实模型起草质量。

第一次探索在UI审查准备期间超过夹具预发登录会话3600秒期限，补件前发生未连接；[原始记录](evidence/finance-f9-2026-10-22/exploration-before-session-renewal/record.json)与截图保留。已确认期限来自test-only IdentityHarness.login，未修改产品认证或延长真实会话；另开空白库和新夹具会话重新完成上述完整流程，没有把第一次探索算成成功。

页面证据：[刷新恢复](evidence/finance-f9-2026-10-22/browser-recovered.png)、[摘要搜索](evidence/finance-f9-2026-10-22/browser-summary.png)、[本人范围](evidence/finance-f9-2026-10-22/browser-reviewer-mine.png)、[审批完成](evidence/finance-f9-2026-10-22/browser-approved.png)、[状态筛选](evidence/finance-f9-2026-10-22/browser-approved-filter.png)、[历史原件](evidence/finance-f9-2026-10-22/browser-history-original.png)、[历史意见DOM](evidence/finance-f9-2026-10-22/browser-history-dom.txt)、[真实失败定位](evidence/finance-f9-2026-10-22/browser-material-failure.png)。两次浏览器服务均正常退出，ready及临时凭据目录移除、四个服务端口关闭，见[退出记录](evidence/finance-f9-2026-10-22/browser-shutdown.json)。

## 门禁、审查与制品

| 层面 | 本轮实际结果 | 边界 |
| --- | --- | --- |
| 全量pytest | 2628 passed、3 skipped、0 failures/errors，389.07秒 | 三项既有非财务变体；整体report为partial |
| 固定quick | 196 passed、0 skipped、0 failures/errors，31个固定模块 | 财务关键路径与全部F9及页面行为 |
| 页面 | 全console17 passed；新增CJS18组 | 含Unicode、范围/查询、并发、分页故障重试、内部异常匹配 |
| 权限与持久性 | 真实PG、签名接口、完整结构拒绝、真实CLI两启动续审、外部计划守卫 | 隔离配置和员工，不是正式权限/IAM |
| 响应大小 | 50条、2000控制字符事由、128字符ID/公司/owner及20k原文夹具，经真实cookie签名出口完整返回且truncated=false | ASGI仅替代网络；真实TCP/browser链另验 |
| 静态门禁 | packages/scripts ruff F/S108/I/TC通过；mypy304个src文件通过；uv lock离线一致 | 13个变更Python源通过future/中文docstring/大小约束 |
| 包和镜像 | finance135/console23文件wheel+sdist逐字节同源；发布ZIP20文件对应examples及设计包一致；镜像UID10001同源 | 设计包有4个基线已存在的额外引用文件，未冒称全树完全相等 |

[全量报告](evidence/finance-f9-2026-10-22/full-final/report.json)、[原始输出](evidence/finance-f9-2026-10-22/full-final/pytest.log)、[快速报告](evidence/finance-f9-2026-10-22/quick-final/report.json)、[实际网关上限验证](evidence/finance-f9-2026-10-22/max-response-gateway.log)、[独立制品复核](evidence/finance-f9-2026-10-22/artifact-quality.json)保留分层证据。三个skip分别是内存实现不测PG通知行锁、内存实现不测DB约束，以及被共享锁测试覆盖的DB身份锁变体；财务无skip。368个产品/脚本/发布文件在全量、quick、浏览器及制品期间冻结且结束时摘要一致。

主要实际命令：

```text
FINANCE_TEST_PG_CONTAINER=xuanpu-finance-f9-pg uv run python scripts/finance_acceptance.py --dsn-file /tmp/xuanpu-finance-f9-dsn --output docs/guides/releases/evidence/finance-f9-2026-10-22/full-final --suite full --timeout 1800
FINANCE_TEST_PG_CONTAINER=xuanpu-finance-f9-pg uv run python scripts/finance_acceptance.py --dsn-file /tmp/xuanpu-finance-f9-dsn --output docs/guides/releases/evidence/finance-f9-2026-10-22/quick-final --suite quick --timeout 1800
uv run ruff check --select F,S108,I,TC packages scripts
uv run mypy packages/*/src
uv lock --check --offline
uv run pytest packages/xuanpu-console/tests -q
uv build --package xuanpu-finance --out-dir /tmp/xuanpu-finance-f9-build
uv build --package xuanpu-console --out-dir /tmp/xuanpu-finance-f9-build
uv run python docker/finance/build_image.py
```

[审查记录](evidence/finance-f9-2026-10-22/reviews.json)中规格与代码质量均Approved。实际修复包括旧库缺必要列仍写授权、合法Unicode/空白事由被误拒、内部异常提醒错误调用通用接口及其新增分页失败后无法重试，红绿日志保留。早期局部测试与独立review并跑时碰到设计中的数据库独占锁，43通过/1失败；停止重叠后原运行用例2通过，随后完整full和quick无失败。未修改独占机制来掩盖测试冲突。

[制品清册](evidence/finance-f9-2026-10-22/artifacts.json)：镜像`xuanpu-finance:2026.10.22.6`，ID`sha256:2e1c41ef01ac990e7f9cf4bab3548ee9780ea5186fb037979dc2ed9e1ca5ed74`。安装包、业务ZIP、镜像源码摘要及测试PDF/下载副本保留于`/tmp/xuanpu-finance-f9-build/`，不包含运行凭据。

## 上线边界

正式公司/制度/人员、生产SSO/网络/独立凭据/IAM、真实模型语义、生产负载与异机灾备均仍not_verified。支付、ERP、官方查验/税务、法定归档、飞书/企业微信审批不属于当前记录流程依赖；内部approved不取得这些资格。仅配置检查通过不能上线，也不能代替逐单人工决定。

本地分支feat/finance-f9，目录`/Volumes/Codes/lumorax/lumorax-platform/xuanpu-finance-f0`，基线16875fb；最终中文提交由该分支git log查询。未推送、合并或部署生产。最终规格、质量和分层证据复核均通过。[清理记录](evidence/finance-f9-2026-10-22/cleanup.json)确认仅删除本轮隔离容器及其匿名卷、测试库、私有连接文件和匹配摘要的测试下载；全部五个测试端口关闭，原检出保持干净。安装包、样例副本和本地镜像保留。

设计见[F9规格](../../superpowers/specs/2026-10-22-finance-f9-design.md)，执行见[实施计划](../../superpowers/plans/2026-10-22-finance-f9.md)，剩余项见[缺口台账](../finance-gap-register.md)。
