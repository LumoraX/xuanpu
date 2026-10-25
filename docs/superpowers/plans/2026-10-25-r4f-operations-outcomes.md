# R4-F 效果、用量与成本面板实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans；归因字段在A冻结，汇总在B/C/E事实可用后实现。

**Goal:** 管理员与员工看得清哪些能力在处理工作、哪些成果已被核实、消耗多少资源，以及当前仍不知道什么。

**Architecture:** 基于InvocationBinding、R2账本及R3真实决定生成受权限约束的观察记录和聚合。保留原始事件为正本，聚合可重建，不读取个人正文做管理统计。路径和夹具见[总计划](2026-10-25-r4-delivery.md)。

**Tech Stack:** PG查询/有界聚合水位、现有审计与用量、原生列表/图表；不新建数据仓库或另付费观测平台。

---

## 指标合同

| 指标 | 分子、分母与未知处理 |
| --- | --- |
| 运行结果 | 按唯一task_id统计accepted/running/waiting/succeeded/failed/cancelled/interrupted/outcome_unknown；attempt单列，不重复当新业务 |
| 技术完成率 | 某受理时间队列中succeeded任务数/受理任务总数；同时展示全部其他状态，不从分母隐去失败或取消 |
| 业务核实通过率 | 预先冻结验收批次中满足验收规则且有权威证据的样本/该批已确认具备前提的全部样本；待验/阻塞/未知各列 |
| 签收率 | 选定发起时间队列中已signed_off交接/该队列全部已提交交接；不能把approved、delivered、通知送达当signed_off |
| 正确率 | 抽检正确数/实际抽检数，显示抽检覆盖与人工判定来源；未抽检为not_checked |
| 资源 | 按调用和重试实际累计tokens、执行秒、耗时P50/P95；估算与真实用量分列 |
| 费用 | 只有锁定价格表修订、币种、计费单位与可核对用量才计算；否则price_unavailable，绝不补0元 |
| 效率收益 | 没有预先定义基线和实际对照，不给“节省X小时”结论 |

统计按受理/发起时间选择队列，状态随观察时间变化；响应固定`window_start/window_end/cohort_basis/observed_at/aggregation_version`，避免用同一时窗的完成数除不同队列受理数。合成、真实模型和真人业务分别标记evidence_kind，不混成一张真实效果成绩单。

员工只看本人有权记录；管理员默认只看运行/用量元数据，不能通过标题、失败原因、模型评分或导出看到他人正文。部门维度可选，按受理时真实任职快照标记，历史无部门显示未分配，不编造当前部门覆盖旧归因。

拟接口：GET `/v1/me/capability-outcomes`；GET `/v1/admin/capability-outcomes`；POST `/v1/admin/acceptance-batches`冻结合成/试点批次及标准；POST `/v1/acceptance-batches/{id}/observations`有权人工登记业务核实结论与证据引用；GET同批次详情。人工观察不是R3代签收端点，不能修改原任务/交接状态。

## F0：归因、观察与可重建聚合

**Files**：Create `S/outcomes/{models,config,store,memory,postgres,attribution,observations,queries,aggregation,pricing}.py`、`S/controlplane/migrations/0024_business_outcomes.sql`、`S/api/outcomes.py`、`T/r4_contract/{test_outcome_attribution,test_outcome_metrics,test_outcome_privacy,test_outcome_rebuild}.py`；Modify `S/workspace/task_usage.py`、`S/api/audit_usage.py`、`S/platform.py`的依赖装配；不改原usage事实。

- [ ] 冻结样本批次的输入、成功标准、前提及证据类型，建批后不可悄悄删除失败样本；变更必须新修订，报告显示版本。
- [ ] 观察记录追加式，重复同操作号回原结果；纠正追加supersedes引用，不回写R2/R3业务状态。生成观察前重查证据权限，管理员内容权限不因报表扩大。
- [ ] 唯一约束避免重复事件/重试重复计数，跨天任务按受理队列归属；用实际observed_at呈现迟到状态。缺失binding的旧工作显示unattributed，不猜能力版本。
- [ ] 原始账本重放可重建相同聚合，重建期间查询返回水位/版本和partial状态，不返回空0冒充已算完。累计token含重试，估算值独立字段。
- [ ] 测试跨租户、两页列表总数、失败/取消/unknown、无价格、无部门、午夜队列、重复事件、观测纠正和权限撤销。
- [ ] Run：`uv run pytest packages/xuanpu-server/tests/r4_contract/test_outcome_attribution.py packages/xuanpu-server/tests/r4_contract/test_outcome_metrics.py packages/xuanpu-server/tests/r4_contract/test_outcome_privacy.py packages/xuanpu-server/tests/r4_contract/test_outcome_rebuild.py -q`；提交`feat: 增加能力成果归因与可重建统计`。

最小分页/未知合同测试，seed冻结101笔Alice工作，其中包含一笔unknown、无价格表：

```python
async def test_totals_ignore_page_and_missing_price(r4_http, r4_ids):
    """分页不改变已授权总数，缺价格不是零成本。"""
    response = await r4_http['alice'].get('/v1/me/capability-outcomes',
        params={'limit': 1, 'batch_id': r4_ids['outcome_batch_id']})
    body = response.json()
    assert body['totals']['tasks'] == 101
    assert len(body['items']) == 1
    assert body['totals']['outcome_unknown'] == 1
    assert body['cost']['status'] == 'price_unavailable'
    assert body['cost']['amount'] is None
```

## F1：原生运营面板与导出

**Files**：Create `C/capability-outcomes.js`、`SDK/outcomes.py`、`CLI/outcome_commands.py`、`T/r4_contract/test_outcome_views.py`、`Q/{r4_outcomes_behavior.cjs,test_r4_outcomes_behavior.py}`；Modify `C/{index.html,workspace.js}`、`SDK/client.py`、`CLI/main.py`。

- [ ] 员工入口“我的能力效果”，管理员入口“能力运营”；先展示证据范围、队列/时间、水位，再显示总数、状态、版本、用量和待核实。列表筛选项显示`名称（数量）`，数量不从当前页计算。
- [ ] 展示技术完成和业务核实两个独立结果；模型评语标辅助判断。价格不可用显示“尚无价格依据”，未抽检显示“尚未核实”，不画成0%正确率。
- [ ] 导出经过与页面相同的ACL、分页水位和字段白名单，CSV防公式执行；不附个人消息、文件名、审批理由、秘密、原始模型协议或不必要员工信息。
- [ ] 模型停用下统计与已有人工观察可用；撤权后首帧清空旧记录、迟到请求不回填；桌面/390px与键盘操作复核。
- [ ] Run：`uv run pytest packages/xuanpu-server/tests/r4_contract/test_outcome_views.py -q`及总门禁；实施时新增`docs/architecture/business-outcomes.md`、`docs/guides/capability-operations.md`；提交`feat: 提供按证据解释的能力运营面板`。

本段不预填真实节省工时、投资回报或部门成绩，不把合成样本的结果投放成生产企业效果。
