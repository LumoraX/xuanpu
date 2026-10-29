# 财务F5本地与隔离验收

日期：2026-10-22（Asia/Shanghai）。范围是预付、员工垫付、供应商委托收款、预付核销和内部退款核对；用户确认的独立存储、7个Skill、进程外API和真实员工HITL边界保持。基线为F4提交`0544eda`，源码位于`feat/finance-f5`；本轮只做本地提交，保留独立工作目录，未推送、合并或部署生产。

## 业务交付

| 场景 | 已交付规则 | 明确边界 |
| --- | --- | --- |
| 预付 | 受控供应商/合同原版本、真实条款材料、额度、五链之一、独立付款与记账计划 | 无票预付不能作为普通应付无票后门 |
| 员工垫付 | 原支付交易和材料、员工本人受控账户、实际发票分配、共享原凭据责任 | 不付款给临时报出的其他账号，不把内部核对当银行事实 |
| 委托收款 | 销方仍是供应商，授权编号/期限/限额/原件及受控受托账户 | 仅供应商授权第三方收款，不处理客户代收或改发票销方 |
| 预付核销 | 原确定已付预付、合同/供应商一致、发票预留及最后人工消耗 | 不再发起付款，与退回合计不能超过原已付 |
| 预付退回 | 真实材料及原退款交易号、人工内部核对、共享防重及余额台账 | 不是发起外部退款，也不伪造银行到账；最终收尾不能普通撤回抹账 |

支持材料走F0真实收件、固定SHA、AES-GCM和持久责任索引，终态`material_received`；不创建假发票、假验真或“缺原件”异常。新增`getFundingMaterial`使当前有权申请人/复核人/有效审批指派人可从独立原件存储分块读取、每块重审授权，页面验证完整SHA后下载。

69项实际API与7个Skill均发布为finance `0.6.0-f5`，可运行包和参考pack同步。模型可准备草稿/分配/预检、读取实际状态，不调用执行、授权撤销或材料base64读取工具；真正决定由员工cookie、CSRF、`origin=human`签名和当前岗位核验。业务批准、付款/记账专用批准、原经办明确执行相互分开。

F3资金归档包含原件、父子单、原授权与撤销决定、修订历史、核销/退回台账、真实审批及渠道原回执；新源事实需补包。PDF/PNG/JPEG使用成熟解析器做有限结构检测，解析在可终止子进程执行，10秒超时拒绝，不宣称官方签章有效。F4资金提醒指向原目标；到票/未结提醒仅基于确定已付和实际未结余额，完成收尾后退出。

## 源码与接口验证

[基线](evidence/finance-f5-2026-10-22/baseline.log)为2332 passed、3个既有非财务skip。核心最终[102项合并回归](evidence/finance-f5-2026-10-22/funding-domain-green.log)覆盖F5及原F1/F2、原F3真实HTTP；三种资金场景实际回环付款/记账、额度/父余额并发、最终收尾不可反转、未知只查原号等均包含其中。领域约束部分使用隔离票据/凭据fixture，实际文件收件、解析、材料下载、封存和恢复有独立字节链路及下述浏览器/Compose证据，不能把fixture当正式票样。

修复并保留可复查的RED→GREEN：

- [供应商版本不重置责任RED](evidence/finance-f5-2026-10-22/supplier-scope-red.log) / [GREEN](evidence/finance-f5-2026-10-22/supplier-scope-green.log)：自然身份稳定、完整版本入正文、合同原版本保留。
- [旧渠道摘要与审批epoch RED](evidence/finance-f5-2026-10-22/provider-epoch-red.log) / [GREEN](evidence/finance-f5-2026-10-22/provider-epoch-green.log)：空资金能力兼容旧摘要，非空能力须新版本；资金单据/计划/支付/关联票据读权不因撤权恢复而复活。
- [材料收件RED](evidence/finance-f5-2026-10-22/material-intake-red.log) / [GREEN](evidence/finance-f5-2026-10-22/material-intake-green.log)，[实际人工原件读取](evidence/finance-f5-2026-10-22/material-read-green.log)。
- [多依据/授权证据修订RED](evidence/finance-f5-2026-10-22/ui-revision-red.log) / [GREEN](evidence/finance-f5-2026-10-22/ui-revision-green.log)，原目录缺失不默认改绑。
- [上传确认失效RED](evidence/finance-f5-2026-10-22/ui-upload-red.log) / [修复回归](evidence/finance-f5-2026-10-22/quality-fixes-green.log)，已有有效材料的草稿仍可保存。
- [解析取消/期限RED](evidence/finance-f5-2026-10-22/parse-cancel-red.log) / [真实PID退出与超时GREEN](evidence/finance-f5-2026-10-22/parse-cancel-green.log)。

首轮全量仅发现控制台资源清单未登记`finance_funding.js`：[中间结果](evidence/finance-f5-2026-10-22/pytest-before-final-fixes.log)，已补齐并通过[资源测试](evidence/finance-f5-2026-10-22/static-resource-green.log)。最终完整测试为 **2441 passed、3 skipped，419.96秒**；3项skip均是既有非财务数据库专项，财务测试无skip，见[最终pytest](evidence/finance-f5-2026-10-22/final-pytest.log)。最终Ruff全绿、strict mypy覆盖287个源码文件；本轮66个变化Python文件的中文docstring/from-future/尺寸检查0违规，最大文件372行、最大函数76行，见[源码约束](evidence/finance-f5-2026-10-22/source-constraints.json)。[Ruff](evidence/finance-f5-2026-10-22/ruff.log)、[strict mypy](evidence/finance-f5-2026-10-22/mypy.log)、[C901](evidence/finance-f5-2026-10-22/complexity.log)、[包校验](evidence/finance-f5-2026-10-22/pack-validation.log)、[锁文件](evidence/finance-f5-2026-10-22/lock-check.log)分别记录，不能由某一个HTTP200或单一绿测替代。

实际命令：

```sh
FINANCE_TEST_DSN=<本轮专属finance_test地址> uv run pytest -q -ra
uv run ruff check --select F,S108,I,TC packages
uv run mypy packages/*/src
uv run ruff check --select C901 --config 'lint.mccabe.max-complexity=10' packages/xuanpu-finance/src
uv run xuanpu validate examples/finance
uv lock --check
```

## 制品与运行证据

finance wheel/sdist `2026.10.22.1`与console wheel/sdist `2026.10.22.2`已实际构建。finance的118个源码文件、console的21个源码文件与wheel及sdist逐字节匹配；最终镜像`xuanpu-finance:2026.10.22.1`以UID10001运行，118个财务源码文件逐字节匹配。镜像ID为`sha256:435be377d57f4621c7f6b22de00452af4e8ba2f642d2c0efe489280c56d6dc65`，包含pypdf 6.21.2、Pillow 13.0.0。见[制品摘要](evidence/finance-f5-2026-10-22/artifacts.json)、[镜像源码清册](evidence/finance-f5-2026-10-22/image-source.json)、[finance构建](evidence/finance-f5-2026-10-22/build-finance.log)、[console构建](evidence/finance-f5-2026-10-22/build-console.log)、[镜像构建](evidence/finance-f5-2026-10-22/image-build.log)。

[专属Compose](evidence/finance-f5-2026-10-22/compose-smoke.json)使用独立PG/原件卷和隔离HTTP服务，实际重启3次：既有查验未知恢复、档案只读审计，以及新预付付款未知恢复。新预付原请求POST1次、GET1次，重启后同请求完成、已付100.00，原件仍可读取；无重复付款发送。演练的项目/卷和临时配置已清理。此证据只证明隔离渠道和当前镜像，不证明任何真实银行/ERP或正式档案供应商。

[实际归档与新库恢复](evidence/finance-f5-2026-10-22/quality-fixes-green.log)验证真实TCP归档上传/逐版本读回、两份ZIP、PDF及退款材料、后续子单/撤销导致补包、新表与原字节dump/restore；恢复不启动worker、外部副作用为0。它是本机独立数据库及密文副本演练，不是异机故障域或生产RTO/RPO承诺。

浏览器验收使用隔离员工账号、真实平台cookie/CSRF/签名与财务PG，关闭模型；[申请人原件页面](evidence/finance-f5-2026-10-22/browser-owner-materials.png)、[内部审批决定](evidence/finance-f5-2026-10-22/browser-approval-recorded.png)、[付款仍需专用审批](evidence/finance-f5-2026-10-22/browser-payment-approval-required.png)分别记录了实际页面。付款经办不会因为业务批准而自动执行。专用审批通过后，由原付款经办明确确认执行；[可执行原计划](evidence/finance-f5-2026-10-22/browser-payment-ready.png)和[最终已付页面](evidence/finance-f5-2026-10-22/browser-final.png)保留实际结果：隔离已付100.00、未核销余额100.00、尚未入账。浏览器渠道采用隔离MockTransport；真实TCP通道另由前述HTTP和Compose演练覆盖，均不代表真实银行到账。[后端状态](evidence/finance-f5-2026-10-22/browser-status.json)记录46次签名调用均为human、model disabled、业务及付款两次实际审批、渠道POST仅1次、付款succeeded；[浏览器服务测试](evidence/finance-f5-2026-10-22/browser-harness.log)为1 passed。

[清理记录](evidence/finance-f5-2026-10-22/cleanup.json)确认本轮财务schema与维护临时库清零、专属PG及匿名卷删除、Compose容器和卷清空、浏览器专用页关闭、本轮60553/60035端口关闭；原平台检出干净。构建制品、镜像和F5工作目录保留。

## 仍未验收的外部范围

真实支付/ERP/税务供应商未选定，正式能力仍关闭；企业现行正式制度和组织需显式配置。官方票据标准/签名/真实票样、正式WORM和法定四性、S3维护/异机灾备、企微/飞书报销与外部消息、真实模型语义及生产并发均未完成验收。A18内部专用流程已补齐，整体正式能力仍为partial，详见[缺口台账](../finance-gap-register.md)。

当前维护边界是独立服务的本地密文对象与专属PG；平台只提供身份/入口/原始文件网关，财务业务无平台库外键、付款和审批事实由财务独立持有。关闭Skill或渠道不删除业务历史；disabled拒绝员工业务API及新发送，既有固定请求后台安全GET核对沿用F2。

## 复核与源码交付

最终规格与质量复核均为Approved，无剩余本地验收阻断，详见[复核记录](evidence/finance-f5-2026-10-22/reviews.json)。[实施计划](../../superpowers/plans/2026-10-22-finance-f5.md)五项任务已全部勾选。本记录随F5源码一并本地中文提交，提交标识由`git log -1 --oneline`核对；保留`feat/finance-f5`及工作目录，未推送、合并或部署生产。
