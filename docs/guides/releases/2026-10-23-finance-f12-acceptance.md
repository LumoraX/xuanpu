# 财务F12中途撤权与故障续办验收记录

本轮范围为内部发票报销流转记录，不支付。finance业务包`0.13.0-f12`，77 API/24内部操作/7 Skills；财务服务/镜像`2026.10.23.3`，人工页面保持`2026.10.23.1`。正式资料仍未提供，不推送、合并或部署。

## F0–F11剩余项与本轮范围

本轮优先关闭内部材料收件中途撤权的事务复核与单项隔离，以及强制退出、并发/重复真人确认和已提交但响应未知的隔离验收空白。不新增表、API、页面或模型维护/人工执行权限。一般发票finish_item_parsed的权限拒绝隔离不在当前内部材料范围。

## 执行状态

基线quick实际309 passed、0 skipped/failure/error，原39模块全部覆盖。真实强退/并发专项及发布制品已通过；最终full实际2776 passed、3项既有非财务skip，quick实际332 passed、0 skipped；均无失败、错误及缺失模块。

收件权限红测真实5 failed/11 passed，复现三个中途撤权窗口停止worker及两个direct store未拒绝。修复后19项专项通过，原test_worker/F7/F8等7模块63项回归通过；独立质量审查实际重跑19项通过。数据库真实事件与状态见[专项JUnit提取](evidence/finance-f12-2026-10-23/worker-evidence.json)，原始红绿日志和独立复测JUnit保留。

| 撤权时点 | 当前项实际结果 | 保留回执/目标密文 | 同一worker下一合法项 |
| --- | --- | --- | --- |
| 下载后、prepare前 | failed/COMPANY_FORBIDDEN | 0 / 0 | succeeded/material_received |
| 密文写后、receive前 | failed/COMPANY_FORBIDDEN | 0 / 1 | succeeded/material_received |
| receive提交后、完成前 | failed/COMPANY_FORBIDDEN | 1 / 1 | succeeded/material_received |

失败材料仍不满足F8 selectable条件。真实PG故障回滚、失租约与无关异常传播亦覆盖；不是将所有异常转为权限拒绝。授权恢复不会把失败项自动变为成功，已有原件和回执不删除。

## 未验边界

真实公司/制度/人员、生产SSO/独立凭据/IAM/网络、真实模型语义、生产负载、S3/异机恢复仍not_verified。崩溃测试只覆盖对象写完成后的产品进程强退，不宣称写入中临时文件修复、主机掉电或正式灾备指标。

[F12规格](../../superpowers/specs/2026-10-23-finance-f12-design.md) · [实施计划](../../superpowers/plans/2026-10-23-finance-f12.md) · [使用指南](../finance-f12.md) · [缺口台账](../finance-gap-register.md)


[Skill/契约校验](evidence/finance-f12-2026-10-23/skill-contract-validation.json)使用实际xuanpu_spec和taifeng registry读取7个Skill，finance72+catalog5=77 API，内部24，shell=false；没有添加真人执行或维护工具。[设计及阶段审查](evidence/finance-f12-2026-10-23/reviews.json)区分静态审查与独立PG复测，不将实施者报告当独立证据。


真实进程首轮4项失败单列保留：原计划将人工执行HTTP误写200，而既有routes_f1/OpenAPI合同一直为202；三项人工验收因此使用错误预期，崩溃链路末尾则因测试密钥文件默认0644被F11安全读取拒绝。只修正新验收和私有夹具为0600，没有改变产品/接口或放宽凭据规则；这些探索失败不作为产品缺陷红测。设计修正已独立复核，后续完整绿测另列。


## 真实进程与人工确认专项

两新进程模块实际4 passed/69.11秒，旧F11/F10进程兼容组合6 passed/17.49秒；阶段一质量审查另独立复跑HITL三项3 passed/5.89秒。具体签名来源、实际操作ID、DB决定与版本来自[专项JUnit](evidence/finance-f12-2026-10-23/process-junit.xml)，并非预设业务状态。

| 场景 | 本轮专项实际事实 | 证据边界 |
| --- | --- | --- |
| 密文落盘后、回执提交前强退 | 真实CLI退出-9；新CLI自然等租约，从原认领起60.108524秒恢复，333次核对租约未改 | 测试PG trigger只制造检查点，平台源已移除，不手改lease/claim/receipt |
| 恢复后原材料及审批 | 下载、回执、prepared/received/material事件各1；原幂等操作相同；原件字节可读；两级human决定，approved | paid/posted=0，正式invoice/settlement/compliance三表0 |
| 8个同计划同键真人请求 | 8个不同EdDSA上下文/nonce实际均202，同operation；决定、执行、outbox各1；同计划换键仍原operation | HTTP202是受理；经理步骤推进workflow/step一次，末级才推进claim版本 |
| 同版本批准/驳回竞争 | 专项[409,202]驳回胜出，独立质量复跑[202,409]批准胜出；每次唯一决定，失败方无operation | 两个相反分支都实际观察，不指定胜者顺序 |
| 上游提交而响应丢失 | 测试代理原字节转发且取得实际202后向调用方返回503；同原键/计划/digest及新签名，直连和CLI重启后均原operation | agent先执行有效计划实际403且状态未变；后续财务复审仍需真人 |

停机后实际维护verify退出0，固定十字段complete、authenticated_objects=1、workers_started=false、business_writes=0；全部public表及原密文前后相同。产品正常终止实际-15，日志含应用清理完成；与强退-9、pytest退出0分别记录。财务、网关与代理端口关闭，测试trigger/function/guard lock清除。此次自然租约恢复时间仅对应隔离条件，不是生产RTO。

## 静态检查与制品

实际`uv run ruff check --select F,S108,I,TC packages scripts`通过；`uv run mypy packages/*/src`为310源码文件无问题；`uv lock --check --offline`解析109包。本轮18个变更Python文件future、中文docstring、800行/80行及C901≤10约束通过。717文件冻结清册覆盖产品、测试、脚本和两份契约，构建后仍一致。

finance/console各wheel和sdist，以及finance ZIP共5个制品保存在`/tmp/xuanpu-finance-f12-build`，逐文件匹配当前源码；finance141文件、console23文件，两份业务包对应20文件与ZIP一致，设计额外4个refs与af96bb4基线原字节一致。[制品清册](evidence/finance-f12-2026-10-23/artifacts.json)保留SHA-256。

财务镜像`xuanpu-finance:2026.10.23.3`真实UID10001、版本2026.10.23.3，141源码文件摘要匹配；镜像ID为`sha256:5a650c24a0fba3729f94dad8ff9bba2d99c959e9a57d39f3c228260033e26126`。在network=none下实际运行服务与维护help均退出0，仅证明入口可调用，不是镜像生产业务演练。本轮人工页面无行为变化、未重跑浏览器，既有前端行为保护仍在固定quick与全量中。


## 全量首轮兼容失败的处理

首轮全量实际2779用例，其中2775 passed、1 failed、3项既有非财务skip，完整保留在[full-initial](evidence/finance-f12-2026-10-23/full-initial/report.json)。唯一失败是F5旧材料测试仍要求撤权时整个worker抛ExceptionGroup、项停在received；这是本轮已修复行为的旧断言漏同步，不跳过或回退产品。只更新该函数，保留真实get后撤权屏障，断言worker正常、当前operation/item failed/COMPANY_FORBIDDEN、成功材料事件0、回执/密文各1且原件可读；F5模块与F12 worker组合实际25 passed/8.33秒。原冻结清册和制品清册以initial文件保留，随后重新冻结并重建finance发行包及整轮验收。initial制品清册仅记录历史构建摘要，不代表现存文件；同名finance发行包已由最终重建覆盖，当前字节以artifacts.json为准。


## 第二轮非财务CLI超时诊断

第二轮全量仍2779用例，2775 passed/1 failed/3既有非财务skip；23项F12及修正后的F5兼容测试全部通过。唯一失败改为平台CLI真实进程测试在签发租户凭据时ReadTimeout，完整结果保留在full-proxy-attempt。堆栈发现本机请求经环境代理；显式本机绕过后单项仍同位置超时，故代理不是充分根因，未以环境偶发直接忽略。诊断与后续重跑另列。

失败上下文包含隔离CLI测试当场生成的临时根凭据，两个日志/JUnit仅对该值替换脱敏，其余异常、断言和字节保留；清册保存脱敏前后摘要与替换计数。此处是凭据保密例外，不是以日志尾空格为由改写原始输出。该测试服务已按fixture停止；没有使用或记录生产凭据。


CLI根因经两次PIPE/两次文件输出对照、实际512字节匿名管道容量与系统采样确认：已有469字节，下一条84字节日志使总需求553，889个采样均阻塞于flush→write；只排空日志便恢复请求，Uvicorn实际在发响应头前写访问日志。初轮为何曾通过未记录当时容量，保持not_checked。修复限定测试夹具TemporaryFile，不改业务或延长时限。修正前固定quick已实际332 passed、0 skip/failure/error、42模块无缺失，保存在quick-pre-harness；最终重新冻结后的结果另列。


CLI夹具最小修正的完整模块实际10 passed/1.64秒；设计及实现规格均独立Approved。修改仅TemporaryFile日志承接、真实失败日志读取与kill后回收；所有HTTP/业务断言和原时限保留。该文件不属于finance/console发行包或镜像内容，最终重新冻结后仍会逐字节复核现有制品，不以此额外重建未变的业务代码。


## 最终全量与固定快速验收

| 本轮命令suite | 用例总数 | passed | skipped | failure/error | 实际耗时 |
| --- | --- | --- | --- | --- | --- |
| full | 2779 | 2776 | 3 | 0 / 0 | 592.005秒 |
| quick | 332 | 332 | 0 | 0 / 0 | 674.877秒 |

两轮均使用本轮独占PG，实际退出0，财务skip为0，固定42模块无缺失（原39顺序完整保留，前置3个F12模块）。full的partial仅对应三项既有非财务skip：内存变体不验证PG通知事务、内存变体不验证数据库约束，以及共享通知锁测试已覆盖的DB身份锁变体。不能把partial或内部approved解释为生产批准。

[full报告](evidence/finance-f12-2026-10-23/full-final/report.json)、[quick报告](evidence/finance-f12-2026-10-23/quick-final/report.json)绑定实际HEAD/工作树与原始pytest/JUnit摘要；[最终F12事实](evidence/finance-f12-2026-10-23/f12-evidence.json)逐字提取本轮JUnit，不将首次探索数据冒充最终重跑。冻结清册约束的源码、测试、脚本与业务包另经字节核对；报告与证据收尾不改这些文件。

最终full的真实强退恢复从原认领起60.237821秒，359次核对原租约未改，原操作幂等一致、下载/回执/材料成功各一次；两级human已批准、支付/记账金额为0，停机verify完整且业务与密文快照不变。专项与最终复跑的实际时长分别保存，均不作正式恢复指标。

实际执行入口：

```bash
NO_PROXY=127.0.0.1,localhost,::1 no_proxy=127.0.0.1,localhost,::1 FINANCE_TEST_PG_CONTAINER=xuanpu-finance-f12-pg uv run python scripts/finance_acceptance.py --dsn-file /tmp/xuanpu-finance-f12-dsn --output docs/guides/releases/evidence/finance-f12-2026-10-23/full-final --suite full --timeout 1800
NO_PROXY=127.0.0.1,localhost,::1 no_proxy=127.0.0.1,localhost,::1 FINANCE_TEST_PG_CONTAINER=xuanpu-finance-f12-pg uv run python scripts/finance_acceptance.py --dsn-file /tmp/xuanpu-finance-f12-dsn --output docs/guides/releases/evidence/finance-f12-2026-10-23/quick-final --suite quick --timeout 1800
uv run ruff check --select F,S108,I,TC packages scripts
uv run mypy packages/*/src
uv lock --check --offline
```

完整pytest由固定入口运行`python -m pytest packages -q -ra --junitxml=...`，没有选择性剔除失败模块。真实未填写模板preflight退出2、blocked/configuration_invalid且production_status=not_verified，不创建DB或对象根。生产与真实模型、真实员工试用继续未验。


最终quick实耗674.877秒，较夹具修正前219.249秒慢；期间实际只读采样确认进程仍存活，随后正常退出0。耗时差异原因未确认，保持not_checked，未延长原1800秒验收上限，也不由该结果推导生产性能或SLA。

## 资源清理与本地交付

[清理前资源核对](evidence/finance-f12-2026-10-23/test-resource-audit.json)实际临时库、其它数据库会话、advisory锁及基准库public表/临时schema均0。[清理记录](evidence/finance-f12-2026-10-23/cleanup.json)确认只移除本轮有标签且ID匹配的PG容器、其独占匿名卷与私有DSN文件，51812端口关闭；其它项目资源未清理，原xuanpu检出仍干净。F12真实进程/网关/代理端口与测试触发器清理由JUnit逐项记录。代码分支、5个制品和镜像保留，不推送、合并或部署。

[清理前凭据扫描](evidence/finance-f12-2026-10-23/secret-scan.json)覆盖当时所有变更及证据的实际DSN/密码、临时CLI根凭据、私钥、JWT和原始PDF流，无命中；之后仅收尾无凭据的报告/审查元数据。原始pytest日志和三份失败JUnit保留原有异常空白；[空白检查清册](evidence/finance-f12-2026-10-23/log-whitespace.json)精确列明例外，源代码、文档、JSON及其它JUnit必须检查0。临时根凭据替换与前后摘要单列，未因格式要求修剪原始异常。

目录`/Volumes/Codes/lumorax/lumorax-platform/xuanpu-finance-f0`，分支`feat/finance-f12`，基线`af96bb4`；本轮中文本地提交可在该分支git log查询。最终整轮规格与质量均独立Approved，见审查清册与[独立制品质量记录](evidence/finance-f12-2026-10-23/artifact-quality.json)。文档检查`uv run pytest -q packages/xuanpu-cli/tests/test_examples_and_docs.py`实际206 passed；最后收尾仅更新审计元数据和计划状态，不改冻结源码。真实公司/人员/制度、SSO/IAM/网络、真实模型、正式部署与异机灾备继续not_verified。

CLI夹具另经质量审查独立复跑完整模块10 passed/1.16秒；最终full/quick的质量结论为独立重算证据而非另起PG复测，两者已明确区分。
