# 财务F4开发验收记录（2026-10-21）

本轮范围为F0–F3内部缺口收敛：独立财务站内提醒、权限SQL分页、配置/运营投影和local配套备份恢复。基线a9543f0，分支feat/finance-f4，复用独立目录xuanpu-finance-f0。验收于北京时间2026-10-22完成，文档与制品沿用本轮2026-10-21阶段标识。本轮完成本地开发及隔离验收，不代表正式供应商或生产验收。

使用指南与剩余范围：[F4指南](../finance-f4.md)、[缺口台账](../finance-gap-register.md)、[设计](../../superpowers/specs/2026-10-21-finance-f4-design.md)、[计划](../../superpowers/plans/2026-10-21-finance-f4.md)。

## 实现与门禁

财务正本、原件密文、通知、审计和恢复生命周期继续位于独立财务存储；平台通过既有人工签名、在线身份/授权和HTTP接口协作。原7个Skill同步到finance 0.5.0-f4，API从58增至61。模型可查询通知；已读、审批、本人执行、运维恢复仍有各自权限边界。通道配置不会自动取得正式执行资格。

| 层次 | 实际结果 | 证据 |
| --- | --- | --- |
| 基线 | 2286 passed、3既有非财务skip，311.39s | [baseline](evidence/finance-f4-2026-10-21/baseline.log) |
| 最终全量 | `FINANCE_TEST_DSN=… uv run pytest -q -ra`：2332 passed、3 skipped，357.05s；财务真实PG用例无跳过 | [full](evidence/finance-f4-2026-10-21/full-final.log) |
| 规范 | `uv run ruff check --select F,S108,I,TC packages`：All checks passed | [ruff](evidence/finance-f4-2026-10-21/ruff-final.log) |
| 类型 | `uv run mypy packages/*/src`：273 source files，无问题 | [mypy](evidence/finance-f4-2026-10-21/mypy-final.log) |
| 规模/复杂度 | 修改生产Python文件22个，中文docstring/行数审计issues为空；C901通过 | [audit](evidence/finance-f4-2026-10-21/source-audit.json)、[C901](evidence/finance-f4-2026-10-21/complexity.log) |
| 契约 | `uv run xuanpu validate examples/finance`通过；API/Skill参考包同步，严格响应模型、human/idem/version验证通过 | [validate](evidence/finance-f4-2026-10-21/package-validation-final.log)、[contract](evidence/finance-f4-2026-10-21/contract-green.log) |
| 独立复核 | Task1–4逐项规格→代码质量复核Approved；缺陷复现后修复 | [review](evidence/finance-f4-2026-10-21/review-record.md) |

3项跳过来自原平台reminder/proactive用例，实际原因见全量末尾；它们不构成财务生产证据。最终文档和契约状态标记更新后另跑文档/包校验及静态门禁，输出保存在同一证据目录。

通知12项真实PG覆盖旧Outbox、本次审批、撤权、代际、未知执行、档案恢复/再次失败、payable目标、并发幂等与有界轮转。分页9项真实PG验证原权限等价、repeatable_read、混合F2/F3及SQL实际limit+1；关联回归84项通过。维护/overview共14项真实PG（首批13项加归档成功恢复1项）覆盖锁、丢锁、篡改、同硬链接秘密文件、真实AAD认证、固定归档请求descriptor及当前有效计数。[维护/概览结果](evidence/finance-f4-2026-10-21/maintenance-overview-green.log)及逐项RED日志均保留。

## 实际人工入口与浏览器

无模型cookie/CSRF用例通过真实人工签名、财务验签、平台在线身份和独立PG，禁止模型运行时兜底。[cookie结果](evidence/finance-f4-2026-10-21/cookie-green.log)、[生命周期](evidence/finance-f4-2026-10-21/lifecycle-green.log)、[Node页面行为](evidence/finance-f4-2026-10-21/console-green.log)。

本机真实控制台以approver和payer两个隔离身份依次操作：未读1→本人标已读后未读0且仍待审批→核对原计划→指定审批人批准→原经办人单独确认→读取渠道确定回执。最终用途confirmed，21次签名请求均为human，模型关闭，已读记录真实持久化。[浏览器状态](evidence/finance-f4-2026-10-21/browser-status.json)。外部税务使用隔离MockTransport；它不代表真实供应商。

截图：[未读](evidence/finance-f4-2026-10-21/browser-unread.png)、[已读但未批准](evidence/finance-f4-2026-10-21/browser-read-not-approved.png)、[核对原计划](evidence/finance-f4-2026-10-21/browser-original-plan.png)、[审批已记录](evidence/finance-f4-2026-10-21/browser-approval-recorded.png)、[原经办执行及回执](evidence/finance-f4-2026-10-21/browser-execution-confirmed.png)、[运行概览](evidence/finance-f4-2026-10-21/browser-final.png)。该页面测试由独立测试循环驱动合规/通知，未启动完整服务生命周期，overview显示任务未启动符合测试装配；四个真实服务后台任务running由下一节最终镜像Compose证明。

## 制品与实际运行

finance wheel/sdist 2026.10.21.5、console wheel/sdist 2026.10.21.6构建成功。finance wheel的104个源码文件、console wheel的20个源码文件、安装镜像的104个财务源码文件逐字节匹配当前源码。[制品hash与一致性](evidence/finance-f4-2026-10-21/artifacts.json)、[镜像源码清册](evidence/finance-f4-2026-10-21/image-source.json)、[finance构建](evidence/finance-f4-2026-10-21/build-finance.log)、[console构建](evidence/finance-f4-2026-10-21/build-console.log)、[镜像构建](evidence/finance-f4-2026-10-21/image-build.log)。

最终镜像`xuanpu-finance:2026.10.21.5`，ID `sha256:e6671f05e11063d5d0323feae167c8b0401eb3cce838a9cfed0bb7e0f0b413d3`，运行UID10001。最终镜像在专属Compose项目、独立PG及加密原件卷上经过2次重启：查验POST首次结果未知，重启后只按原请求GET，原POST次数保持1；随后实际内部审批/原经办确认、用途confirmed、归档上传/逐字节和保留锁读回，第二次重启后审计只GET、0次POST。四个后台任务running，原生通知2条，5条回执，总外部POST3次。[最终Compose结果](evidence/finance-f4-2026-10-21/compose-smoke.json)。早期镜像记录仅为中间证据，最终以本段ID和最终结果为准。

## 配套恢复与清理

真实pg_dump/pg_restore同机独立恢复演练通过：恢复2条人工决定、4个行动计划、2个工作流、1条分配、1个原件收件和2张发票；1个真实原件经原AAD解密，清册HMAC及全部对象验证通过。恢复后F2/F3原请求hash相同、任务均为query，恢复工具未启动worker、0次外部执行。本次恢复演练只验证查单任务模式，没有真正调用provider GET；实际重启GET核对由Compose独立证明。[恢复结果](evidence/finance-f4-2026-10-21/restore-drill.json)。测试维护工具使用专属PG容器中的成熟客户端；服务镜像未内置PG客户端，运维部署须另行具备兼容客户端。[CLI帮助](evidence/finance-f4-2026-10-21/cli-help.log)。

另补完整归档及补包的成功恢复：真实签名PG/回环HTTP生成原归档及补包，dump→新空库restore后，31个缓存以原AAD解密并逐字节一致，33个对象认证通过；两个ZIP完整，分别12/22个成员，数据库原请求、模板、状态逐行一致。恢复未启动worker、0次外部执行。新增测试单独1 passed，最终2332项全量含该项。[归档恢复结果](evidence/finance-f4-2026-10-21/archive-restore-drill.json)、[目标测试](evidence/finance-f4-2026-10-21/archive-restore-green.log)。此前只有缺descriptor拒绝测试，最终复核发现成功路径证据不足后已补齐。两个早期静默演练脚本的stdout日志为空，不作为运行过程证据；结果以对应JSON和可复跑测试为准。

已关闭浏览器/HTTP测试服务、清理本轮Compose项目及卷、演练专属临时目录/秘密文件、测试PG及匿名卷；删除前非系统schema及维护临时库均为0。60034/60552端口关闭，原xuanpu检出保持干净。未声称清空所有pytest scratch文件。[删除前数据库](evidence/finance-f4-2026-10-21/database-before-cleanup.json)、[清理结果](evidence/finance-f4-2026-10-21/cleanup.json)。

## 保留边界

正式税务/付款/记账供应商、真实WORM、S3配套备份、外部消息/企微或飞书报销、官方格式样本、异机生产DR/RTO/RPO和真实模型端到端评测均未验收。预付、员工垫付、委托收款专用流程未实现，不能从应付闭环推断覆盖。原A01–A38逐项状态见缺口台账。本轮仅本地提交，无推送、主线合并或生产部署。
