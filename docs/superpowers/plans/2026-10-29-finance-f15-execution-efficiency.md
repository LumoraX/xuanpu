# F15 执行逻辑降耗实施计划

> 用户已授权测试优化。本轮沿用现有隔离检出，先测试后实现；不在未完成F15时提交，不询问常规实现选择。

**Goal:** 消除不必要的新会话和整条重跑。每次定点真实测试最多12次；首轮4次定位错误后，设计补充允许至多第二轮，本轮合计至多16次、总上限240不变。实际新增10次，历史答复已实测，后续两项负向请求未执行。

**Architecture:** 完整flow仅调整同角色续会话；新增明确局部回归入口，前置事实来自公开人工HTTP，不直写PG，结果用partial_passed、不算完整模型验收；fixture计数标签独立于one_model_created_claim。预算只收紧。

**Tech Stack:** 现有Python/pytest、Platform公开HTTP、独立PostgreSQL、生产模型工厂。

## 1. RED与最小改动

- [x] 新建`packages/xuanpu-finance/tests/test_f15_execution.py`。用实际driver和离线HTTP事件验证04/06共享经理session，08/09共享本人session，08独立于起草session、07/10各用自己身份；工具与人工决定门槛不删。
- [x] 对Budget增加`max_streams=120`收紧参数写RED：12后派发前停止、重开同名不能改大、非法值不创建账本、global240不变。
- [x] 修改`finance_model_journey.py`两处会话生命周期，Budget添加持久的单旅程上限；默认full行为门槛和自然提示保持。

## 2. 定点入口

- [x] 新建`scripts/acceptance/finance_model_focus.py`：`prepare_query_fixture`使用原upload/manual公共入口创建真实原文/版本，`human_action`从当前队列/当前计划确认提交与三次决定，`query_regression`复用原final_facts/retained_original/challenges。
- [x] `finance_model.py`提取`parse_options(argv=None)`，新增choices full/query-regression默认full。`run`按显式场景选择函数和12/120局部上限，Evidence在组装前标明scenario/fixture_source/counts_as_complete_journey。
- [x] 离线CLI及场景选择测试，真实owned PG+公开HTTP集成测试前置fixture新增模型调用0，拒绝fixture被当full。

## 3. 验证

- [x] 运行定点pytest，保存RED与GREEN；最终离线253 passed、1项PG集成单独运行，完整测试包含该集成。
- [x] 新建本轮owned PG和空库；原私有ledger恢复208/206+2，原models指纹不变。先运行0模型HTTP集成，再受限两轮4+6；本轮10次、累计218/240，无第三轮。
- [x] 更新指南/进展，明确局部验收与完整0/2区分、确定少2次route与待实测全流程节省区分；保留原证据和742文件新源冻结。
- [x] 最终全量3182 passed、3项既有非财务skip、0失败/错误；静态门禁通过。保留首次全量F3构造失败、22项独立通过及同源全量复测结论；原因仍待定位。
- [x] 核秘密/资源、仅清理本轮owned资源，保持不支付、不合并、不部署、不完成提交；证据与账本、三份持久制品保留。
- [x] 最后文档链接检查225 passed，结果归档为docs-final.log/xml。
- [ ] 新源完整真实旅程两次及09/10负向挑战仍待完成，不把局部事后语义核查计入通过。

首轮仅4次定位q类别/事由混淆，保留failed。补两份API参数说明后经TDD验证，人工至多第二次新冻局部≤12，本轮合计≤16/global240不变，禁止第三次或原失败晋升。
