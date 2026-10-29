# 发票报销执行逻辑降耗测试（2026-10-29）

本轮优化验收执行逻辑，原240次总上限、单会话20万tokens不变。新增真实调用10次，累计218次、216次有usage、2次历史未知仍保留；剩22次未消费。不支付、独立PG/AES、人工决定边界保持，未提交、推送、合并或部署。F15完整真实旅程仍为0/2。

## 改动与实际收益

- 完整验收中，负责人04驳回与06再审批复用同一身份会话；08仍新建本人回查会话，09在该会话继续。07财务、10无权人保持独立身份。确定少2次主路由；当前队列、详情、历史、版本、模型预检及独立人工确认未删除。复用会携带更多历史，因此不保证总token必然下降，尚待完整模型流程实测。
- 新增显式`--scenario query-regression`，默认仍`full`。前置申报/驳回/重提/批准均由公开人工HTTP准备，真实PG/签名和独立原件验证保留，准备阶段新增模型调用0。模型只做08回查及后续安全请求，单次硬限12。局部结果用`partial_passed`且`counts_as_complete_journey=false`，不计完整双轮；本轮实际两次都保留原failed。
- 将q的既有筛选规则放进模型真正看到的参数说明。类别/旧事由/指代线索用本人无q查询；用户明确给当前单号或事由字面片段仍按原规则筛选。只改两份OpenAPI的description，schema、服务过滤与77 API/24内部操作/7 Skills不变。

## 两次受限实测

首次209–212共4次，模型错误地用q=交通筛当前事由，结果0条后停止；实际事由已改为客户拜访内容。保留[原失败](evidence/finance-f15-execution-2026-10-29/query-regression/result.json)。

修正工具参数说明后，第二次213–218共6次，实际q=null，读取真实详情和两页历史，最终答全驳回及两条批准意见、128.50元、已批准、付款和记账均0。验收器却把“已付款：0.00元”的字段名误判为付款成功，原[第二报告](evidence/finance-f15-execution-2026-10-29/query-regression-fixed/result.json)仍保留failed，不补写通过。

对同一原始答复另作[事后语义核查](evidence/finance-f15-execution-2026-10-29/semantic-reassessment.json)：仅确认08内容在修正判定下正确，09/10未执行，不计局部或完整通过。修正仅豁免完整零金额字段；叙述性付款、非零/负金额、后续肯定执行声明仍拒绝，固定词形矩阵离线验证，不再追加模型调用。

同08阶段与旧final8的[独立计量对照](evidence/finance-f15-execution-2026-10-29/comparable-query-cost-review.json)：

| 样本 | 模型调用 | input+output tokens |
| --- | --- | --- |
| 旧final8回查 | 7 | 54,957 |
| 本轮修复后回查 | 6 | 49,304 |

少1次无效筛选调用，tokens少5,653（约10.29%，cached input仍计入）。这是同一自然请求的两个合成样本，身份和前置数据来源不同，不是稳定平均收益，也不能将整条43次与定点6次直接当成生产流程降幅。

这里的6次是实际模型请求：[逐次明细](evidence/finance-f15-execution-2026-10-29/query-request-breakdown.json)依次为主路由、能力与公司同轮读取、本人摘要、详情与历史同轮读取、第二页历史、最终答复。业务工具共6次、子Skill委派0次。第二页保留了所问的旧驳回和批准意见，不能为了降低次数删掉这部分核查。

## 证据与边界

已保留RED/GREEN、实际HTTP前置fixture、源冻结、两次原始模型事件、预算恢复与原计数历史。此前全量因独立审查要求收紧判定而受控取消，单独保留，不计最终通过。

[设计](../../superpowers/specs/2026-10-29-finance-f15-execution-efficiency-design.md)、[实现审查](evidence/finance-f15-execution-2026-10-29/implementation-review.json)和[原判定问题审查](evidence/finance-f15-execution-2026-10-29/zero-label-review.json)可核查。本轮没有运行第三次模型测试。完整新源双轮、09/10负向模型验收，以及[正式公司制度与人员资料](../finance-company-readiness.md)仍未完成。

## 最终工程验证与清理

最终以显式独立测试DSN运行`uv run python scripts/finance_acceptance.py --dsn-file /tmp/xuanpu-finance-execution-20261029/suite-dsn --output docs/guides/releases/evidence/finance-f15-execution-2026-10-29/full --suite full --timeout 1800`，并设置本轮专属`FINANCE_TEST_PG_CONTAINER`。入口实际执行`python -m pytest packages -q -ra`。最终[全量报告](evidence/finance-f15-execution-2026-10-29/full/report.json)为3185项：3182 passed、3项既有非财务skip、0 failures/errors，无财务skip或缺失模块；报告如实标partial、production_status=not_verified。

同源首次全量曾出现1项F3补件构造失败，发生在被测worker守卫之前；[原始失败](evidence/finance-f15-execution-2026-10-29/full-first-f3-failure/report.json)不覆盖。该模块22项独立运行和第二次全量均通过，未修改F3代码、fixture或断言。根因仍未定位，不推断为环境时钟问题，也不将复测通过称为该问题已修复，见[调查记录](evidence/finance-f15-execution-2026-10-29/f3-failure-investigation.json)。

`uv run ruff check --select F,S108,I,TC packages scripts`及模型验收相关C901通过；`uv run mypy packages/*/src`为311 source files无问题；`uv lock --check --offline`通过。21份改动Python文件的中文文档、future annotations、文件/函数行数符合约束，742份冻结源码无漂移，见[静态与源码核对](evidence/finance-f15-execution-2026-10-29/static-and-source-audit.json)。最后文档检查使用`uv run pytest packages/xuanpu-cli/tests/test_examples_and_docs.py -q`，225 passed，输出单独保留为`docs-final.log/xml`；这些工程测试均不调用真实模型。

已重建并逐字节核对财务wheel、sdist和业务ZIP三份[制品](evidence/finance-f15-execution-2026-10-29/artifacts.json)，持久保存在仓库外`finance-execution-artifacts-20261029`目录；本轮没有新构建镜像或部署。实际网关、三份临时DSN、测试令牌和AES密钥的[秘密扫描](evidence/finance-f15-execution-2026-10-29/secret-scan.json)无命中。只清理本轮标签/ID及卷均匹配的测试PG、三份DSN和两份状态目录；清理前无遗留客户端或咨询锁，模型配置未改、原xuanpu检出干净，见[清理报告](evidence/finance-f15-execution-2026-10-29/cleanup.json)。证据、累计账本和三份持久制品保留。

执行逻辑优化已取得局部真实计量证据；完整真实验收仍0/2，首次全量偶发失败仍待定位，因此不作F15完成提交。没有支付、推送、合并或部署。
