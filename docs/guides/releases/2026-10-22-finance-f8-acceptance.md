# 财务 F8 发票报销实际使用验收

F8的内部发票报销记录范围已实现，独立存储、Skill/API与HITL沿用原有边界，不做支付。当前业务包finance `0.9.0-f8`、74项API、7个Skill；财务发行`2026.10.22.5`，人工页发行`2026.10.22.4`。

## 交付内容

- 本人支持材料列表和详情：当前租户、公司、owner、用途及当前授权逐次核验，reviewer不获得他人收件目录特权；同一快照统计总数和分页，逐项实际回执责任链决定可选材料，部分失败或取消不抹去已保全项。
- 报销附件卡、内联选择、移除和“我的收件记录”；选择/翻页/取消保留尚未保存的正文与明细。刷新可恢复受理记录，整页刷新不承诺恢复未保存正文；技术ID改为折叠的可选辅助信息。
- 受限本地恢复线索：平台POST前保全原request_id，财务POST前保全固定ref与原幂等键；未知人工恢复、已知op只查询；存储拒绝、损坏、超限或身份变化阻止新请求。发送/重试和迟到查询都重新核对完整lineage，放弃使旧确认失效，旧窗口不能清掉新请求。
- 实际平台扩展文件名优先保留，避免附件都显示download；原件下载URL撤销后旧链接明确失效，重新读取仍须权限和摘要核对。
- 复用本人确认提交、真人顺序审批、带理由退回、本人修订重提、历史与旧版本原件；不引入支付、记账或官方票据验真资格。

## 实际浏览器与独立数据库

[浏览器验收报告](evidence/finance-f8-2026-10-22/browser-acceptance.json)使用实际平台和财务TCP监听、实际员工cookie/CSRF与签名人工出口、独立PostgreSQL及加密原件。登录会话由一次性测试夹具签发，浏览器自动化扮演测试员工；并非真实公司人员审批、生产SSO或正式制度验收。测试PDF格式有效，但不是正式税务发票。

实际页面完成：上传→本人确认独立收件→整页刷新→恢复同一收件批次→填写正文并内联选附件→保存草稿→本人预检及确认提交→负责人读原件并带理由驳回→本人移除当前附件、补充新附件、保存修订并重提→负责人和财务复核人分别预检/确认批准→读取历史与被替换原件。当前单据为`ac7d47fdc1a741aa804b7c2c7c6d971f`、approved、版本6、100.00元。

[实际DB核对](evidence/finance-f8-2026-10-22/browser-database.json)保存6个不可变版本和3次决定：reject、approve、approve，全部`origin=human`，实际操作人均不是申请人；退回意见为“请补充交通行程附件”。完整历史共9条。申报原文含首尾空格/换行原样保留，当前只关联补充附件，旧版本仍关联旧附件。

[实际字节核对](evidence/finance-f8-2026-10-22/browser-bytes.json)确认浏览器下载与有效测试PDF完整SHA相同。独立收件后仅按实际SHA[移除平台两份测试原件](evidence/finance-f8-2026-10-22/browser-source-removal.json)，保留业务包对象；补充附件及旧版本1被替换附件仍能从财务独立副本读取、下载并通过摘要核对。

[运行计数](evidence/finance-f8-2026-10-22/browser-service-evidence.json)：2次材料操作、2个原件回执；正式发票、结算操作、合规操作均为0。平台构造时即注入拒绝模型工厂，实际模型派发为0；后台语义跟进的2次factory尝试全部被拒，尝试如实记录，没有把它们隐藏或当作财务审批。不能据此声称真实模型语义验收。财务手动链路在模型不可用时完整完成。

页面证据：[刷新收件](evidence/finance-f8-2026-10-22/browser-recovered.png)、[审批通过](evidence/finance-f8-2026-10-22/browser-approved.png)、[旧版本原件](evidence/finance-f8-2026-10-22/browser-history-original.png)、[实际退回意见](evidence/finance-f8-2026-10-22/browser-history-reason.png)、[最终DOM](evidence/finance-f8-2026-10-22/browser-dom.txt)。浏览器验收服务已正常退出，ready文件、临时运行目录及两个端口均已清理，见[退出记录](evidence/finance-f8-2026-10-22/browser-shutdown.json)。

## 自动化门禁与边界

| 层面 | 本轮事实 | 边界 |
| --- | --- | --- |
| 全量回归 | 2574 passed、3 skipped、0 failures/errors，421.21秒 | 3项均既有非财务变体；finance无skip |
| 最后下载控件修复 | 全console 16 passed；浏览器复核旧链接失效及重新读取下载成功 | 该小修在全量启动后发生，以随后定向回归和固定quick覆盖，不混称全量启动时源码完全相同 |
| 隔离验收入口 | 22测试通过，含双真实HTTP/持续SSE/SIGTERM退出、模型工厂和真实后台失败后人工仍可写 | 开发夹具，不注入产品登录/证据路由 |
| 权限/接口/幂等 | F8真实PG、签名HTTP、实际响应schema、只读/停用与撤权；初始局部52 passed | 独立测试员工/公司 |
| 恢复与持久性 | 本轮全量覆盖原F7真实CLI三启动、源字节移除、备份/恢复；F8未知/跨窗恢复有红绿行为证据 | 本机测试，不等于异机灾备/RTO/RPO |
| 静态门禁 | packages/scripts ruff F/S108/I/TC通过；mypy 298个src文件；19个变更Python文件future/docstring/≤80行函数/≤800行文件通过 | 不等于生产并发和真实模型表现 |
| 文档/业务包 | 368 passed；74 API与7 Skills、两个包目录一致 | 本地可安装包，未发布到正式平台 |

全量入口按真实JUnit把整体标为`partial`，保留3项skip事实，production_status始终not_verified。三个skip为：内存实现不测试真实PG通知行锁、内存实现不测试数据库约束，以及已由共享通知锁测试覆盖的数据库身份行锁变体。见[全量报告](evidence/finance-f8-2026-10-22/full-final/report.json)和[原始输出](evidence/finance-f8-2026-10-22/full-final/pytest.log)。

实际运行的主要命令：

```text
FINANCE_TEST_PG_CONTAINER=xuanpu-finance-f8-pg uv run python scripts/finance_acceptance.py --dsn-file /tmp/xuanpu-finance-f8-dsn --output docs/guides/releases/evidence/finance-f8-2026-10-22/full-final --suite full --timeout 1800
FINANCE_TEST_PG_CONTAINER=xuanpu-finance-f8-pg uv run python scripts/finance_acceptance.py --dsn-file /tmp/xuanpu-finance-f8-dsn --output docs/guides/releases/evidence/finance-f8-2026-10-22/quick-final --suite quick --timeout 1800
uv run ruff check --select F,S108,I,TC packages scripts
uv run mypy packages/*/src
uv lock --check --offline
uv run pytest packages/xuanpu-console/tests -q
uv run pytest packages/xuanpu-cli/tests/test_examples_and_docs.py packages/xuanpu-spec/tests -q
```

## 审查、失败与修复

规格与代码质量审查均Approved；依次修复放弃线索后旧卡仍能发送、恢复标识宽于契约、跨窗口旧查询删新线索、双HTTP长流退出、后台模型绕过运行时阻断、扩展原文件名与失效下载链接。各自保留红绿日志，没有删除后台或放松审批来使验收通过。

首轮全量2569 passed/1 failed/1 error/3 skipped，失败是旧F7标题断言未同步短横字符；error为非财务平台fixture在DROP SCHEMA时与后台扫描发生死锁。修正标题后，受影响三个定向测试实际通过，再次全量2574 passed/3 skipped、0失败/错误；未声称为该偶发fixture死锁修改了产品代码。原输出见full-before-final-review及full-failure-recheck.log。第一次浏览器探查暴露后台ScriptedModels两次被直接调用，不能算无模型验收；其证据独立保留在exploration-before-provider-block。最终重新装配严格拒绝工厂，并重新完成浏览器闭环。

## 制品与当前范围

[制品清册](evidence/finance-f8-2026-10-22/artifacts.json)逐字节核对：finance wheel/sdist 129个源码文件、console wheel/sdist 23个源码/静态文件；finance业务ZIP 20个契约/Skill文件与两个源目录一致。镜像`xuanpu-finance:2026.10.22.5`，ID`sha256:40834c57c6727593b56bb4474e61bfe355ea75fc59a6caccbd9d158c418563e5`，实际UID10001、129个Python源码摘要一致。制品保留于`/tmp/xuanpu-finance-f8-build/`。

当前完成的是内部记录流程及隔离使用验收。真实公司人员/制度部署、正式标准/签名/票据验真、企业微信或飞书审批、生产独立凭据/IAM、异机灾备和生产负载均未验收；支付、记账、税务和法定档案不在本轮范围。未推送、合并或部署生产。

规格见[F8设计](../../superpowers/specs/2026-10-22-finance-f8-design.md)，实施见[计划](../../superpowers/plans/2026-10-22-finance-f8.md)，操作见[指南](../finance-f8.md)，范围见[缺口台账](../finance-gap-register.md)。

## 固定快速验收与清理

最后固定quick实际147 passed、0 skipped、0 failures/errors，覆盖21个固定模块，含全部F8入口、原F0–F7关键链条和最后下载控件修复后的完整财务页面行为。见[快速报告](evidence/finance-f8-2026-10-22/quick-final/report.json)及[实际输出](evidence/finance-f8-2026-10-22/quick-final/pytest.log)。独立复核确认三份浏览器下载分别与create_fixtures确定性重建PDF逐字节一致、pypdf可读取；原始临时fixture路径已随验收进程退出删除，重建件保留于构建目录，不能冒称原路径仍存在。

本轮仅删除自己创建的容器xuanpu-finance-f8-pg及其匿名卷；删除前仅剩两个显式测试库与postgres默认库、唯一非系统角色finance_test、0临时财务schema。临时DSN和本轮生成的下载文件均已清理，51793/53690/53691端口关闭，原检出xuanpu保持干净。见[清理记录](evidence/finance-f8-2026-10-22/cleanup.json)。安装制品、测试PDF重建件及本地镜像保留供审阅。

本地交付分支feat/finance-f8，目录`/Volumes/Codes/lumorax/lumorax-platform/xuanpu-finance-f0`，基线b32a061；中文提交由该分支git log查询。未推送、合并或部署。

原始失败回溯和构建日志按证据目录内精确文件级.gitattributes保留原字节；未改写回溯、未放宽源码空白规则。
