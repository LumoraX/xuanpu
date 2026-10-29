# 财务 F2 开发验收记录（2026-10-21）

范围：原生应付、通用付款/记账/冲回接口、专用人工批准及独立结果闭环。用户尚未选供应商，采用隔离模拟。基线4bc7010，分支feat/finance-f2，复用xuanpu-finance-f0；未推送、未合并、未部署生产。原xuanpu工作区保持不动。

## 交付

应付及受控采购/账户目录、付款与凭证独立余额、部分执行与冲回、固定原计划专用审批、原经办人工确认、外部持久队列/租约/历史fencing、原始回执、验签Inbox、未知查单与异常均保存在独立财务库。F1内部记录始终不具备外部执行资格。

发布38项实际API操作及6个Skill，模型只准备和查询；人工cookie+CSRF网关仍是人工来源入口。发布包版本0.3.0-f2.1。独立辅助页支持应付、专用待办、付款/入账/冲回和核对，不依赖模型。

## 已取得证据

| 层次 | 验证及边界 |
| --- | --- |
| 基线 | 全量2052 passed、3 skipped。[原输出](evidence/finance-f2-2026-10-21/baseline-tests.txt)。 |
| 独立数据库 | 使用专属loopback PostgreSQL16测试容器，实际finance_test库及每例随机schema。并发金额、分离岗位、计划过期、角色/制度/目录撤销恢复、旧F1迁移、回执事务回滚、stale不可推进、重复渠道事实、取消竞态均有实际PG回归。 |
| 实际HTTP | Ed25519签名/防重放+真实PG+实际TCP隔离渠道验证付款受理→disabled合法回调→只读查询→付款600，以及入账1000→冲回250；错误金额回执raw保全→unknown→人工reconcile→重建worker只GET，POST一次。[输出](evidence/finance-f2-2026-10-21/http-e2e-green.txt)。 |
| 平台联调 | 真实平台cookie/CSRF、HumanContextSigner、FinanceAuthenticator及在线实际平台身份桥，模型组装器强制失败；真实业务批准与专用批准分开，原payer确认、查单完成。渠道使用受控HTTP transport；实际TCP另由上项证明。[输出](evidence/finance-f2-2026-10-21/platform-cookie-e2e.txt)。 |
| 浏览器 | 本机实际控制台HTTP、真实平台cookie/签名与独立PG。批准前执行按钮禁用→approver批准固定600计划→payer恢复原计划并确认→渠道受理→人工按原请求查单→交易/证据展示。36项实际签名请求均human，外部POST一次。预置合成发票和已批准应付，外部渠道为模拟；不证明真实供应商。[状态](evidence/finance-f2-2026-10-21/browser-status.json)、[截图](evidence/finance-f2-2026-10-21/browser-payment-confirmed.png)。 |
| Skill/契约 | 真实规范校验、稳定内核加载6个Skill/读取材料、严格动作参数及实际PG响应schema；F0/F1原有响应契约回归。尚未运行真实模型能力评测。 |
| 前后端包 | finance 2026.10.21.3和console 2026.10.21.4的sdist/wheel均构建成功；财务56个源码文件、控制台18个文件逐字节匹配轮子，镜像内56个源码文件也与工作区摘要相同。服务端平台仅新增测试，无生产代码变更。控制台为静态脚本，无独立前端打包器。[制品摘要](evidence/finance-f2-2026-10-21/package-artifacts.json)。 |
| 最终镜像/Compose | 镜像sha256:ea79dfdab958b825989f845f36fad482a8f0e3c10aae20649d047a6fc5a798cf，uid10001。专属Compose真实数据库/原件卷及HTTP渠道：错金额回执→unknown→重启同服务→人工reconcile→付款600，付款POST仍1次；再入账1000、冲回250，净入账750，4份原始回执。所有结果simulation=true。使用缓存PG16覆盖正式样例PG17；平台身份使用隔离stub。项目卷/网络和临时密钥均已删除。[结果](evidence/finance-f2-2026-10-21/compose-smoke.json)。 |
| 独立复核 | 规格与质量Approved。质量回归81项及极深JSON2项通过，当前无剩余P1/P2。[记录](evidence/finance-f2-2026-10-21/review-record.md)。 |

最终全量 **2190 passed、3 skipped（250.48秒）**；3项跳过是既有非财务PG专用用例，财务PG用例全部实际执行。[最终输出](evidence/finance-f2-2026-10-21/final-full-tests.txt)。首轮2187项及中间2190项通过的输出分别保留，补充真实十三项F2 HTTP契约遍历、F1重复迁移及维护性拆分后已重新全量验证。ruff门禁、strict mypy225源文件、复杂度/长度及uv lock107包均通过；发送准备分支按维护约束拆分后，40项provider/worker及最终Compose重新通过。

## 复核中发现及修正

- 读取部分回执后断线保留已收字节；极深JSON转受控未知而不停止所有查单。
- 人员核对及prepare后的发送许可重新检查，丢实例锁取消两个后台。
- stale回执仅保存原证据，不改变余额；使用持久历史lease与操作归属阻断伪job挂错证据。
- 应付采购目录generation进入计划，撤销恢复不复活旧批准；历史审批人降collector后列表和单读权限一致。
- 原单据撤回会同步取消未发送明细/事件/Outbox；已发送取消保持未知及占额。
- 已成功外部交易/凭证不能重复计入另一个Operation，冲突保存raw并待核对。
- 公开Integration/ResourceRef/记录状态schema对齐实际响应；本地审批成功不再显示为渠道完成。

初次HTTP测试脚本遗漏enabled和错误地小写了Bearer值，修正测试后重跑。回调域方法尚未实现时保留真实RED输出，后续接入通过。容器首轮身份stub使用错误header导致身份失败，修正验收脚本读取X-Xuanpu-Act-As后以同一最终镜像重跑通过；产品未因该脚本错误更改。迁移测试首轮写错旧表名也保留，修正后真实迁移通过。没有覆盖失败日志为成功。[原失败](evidence/finance-f2-2026-10-21/http-callback-red.txt)。

## 未实现或未验收

真实付款/ERP系统、企业微信/飞书报销适配器、官方税务查验、正式企业制度、真实模型评测、生产部署及法定归档均未验收。正式执行缺省关闭，production禁止simulation且必须有效查验与原件；模拟通过不自动获得正式资格。通知Outbox尚未接实际投递通道。外部证据引用不等于独立财务已保存供应商原文件或具备WORM。

[实施计划](../../superpowers/plans/2026-10-21-finance-f2-implementation.md)、[使用指南](../finance-f2.md)。

## 最终执行门禁

显式注入独立FINANCE_TEST_DSN后实际运行：

```text
uv run pytest -q -ra
2190 passed, 3 skipped in 250.48s
uv run ruff check --select F,S108,I,TC packages
All checks passed!
uv run mypy packages/*/src
Success: no issues found in 225 source files
uv lock --check
Resolved 107 packages
```

最终镜像、安装包与源码逐文件一致，验收仅为本机隔离环境；真实渠道/生产边界保持上述限制。

最终独立测试库剩余随机schema=0后已移除本任务PG容器及匿名卷；验收Compose容器/卷/网络均为0，浏览器服务已停止，原xuanpu检出仍干净。[清理结果](evidence/finance-f2-2026-10-21/cleanup.json)。最终文档与示例链接测试148项通过。
