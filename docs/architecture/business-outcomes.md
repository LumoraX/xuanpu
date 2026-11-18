# 能力运行结果与业务核实

> R4-F。计划见 [R4-F 计划](../superpowers/plans/2026-10-25-r4f-operations-outcomes.md)。当前源码实现到 F0（统计与验收批次）与 F1 的一部分：管理员原生视图「能力配置 → 运行结果与发布」（`console/static/capability-operations.js`，只显示计数与用量，完成率无数据显示「暂无」、无价格表不估算费用）、SDK `OutcomeClient` 与命令行 `xuanpu knowledge outcomes|admin-outcomes|batch`；员工本人统计页面、验收批次页面与导出未实现。尚未生产部署。

## 运行结果与资源（`outcomes/service.py`）

- 正本是调用绑定（`capability_binding` 的任务行：哪次受理用了哪个能力修订与发布）与 R2 任务账本；统计每次现算，可随时重建，不另存聚合。没有绑定的旧工作不在统计里，不猜能力版本。
- 队列按受理时间（`accepted_at`）选 `[since, until)`；响应固定 `window_start/window_end/cohort_basis/observed_at/aggregation_version/evidence_kind`。
- 状态逐项计数（accepted、running、waiting_user、succeeded、failed、cancelled、outcome_unknown、interrupted），读不到的任务单列 `unreadable`/`missing`。技术完成率 = succeeded / 队列全部任务，不从分母隐去失败与未知。
- token：已结算预留按实际用量（`settled`），仍占用的预留只给上界（`held_upper_bound`）；没有预留记录的任务计入 `tasks_without_records`，不当 0。
- 费用：没有锁定的价格表，一律 `price_unavailable`、金额为空，绝不补 0 元。效率收益不计算。
- 员工只看本人任务的元数据（任务、能力、修订、发布、状态、时间、token）；管理员只看按能力与修订的计数与用量，不含任务正文、标题、失败原因或员工明细。

## 验收批次与人工核实（`outcomes/acceptance.py`，迁移 0031）

- 批次冻结能力修订、名称、验收标准、样本（1–500 个绑定到该修订的任务）与证据类别（synthetic/real_model/pilot）；建后不可改，变更另建新批次并写 `supersedes`，旧批次与其失败样本照旧可查。
- 观察只追加：每个样本以现行（未被取代）观察为准；纠正须指明被纠正的现行观察；同操作号回原结果。观察不修改任务、交接或审批状态，不是代签收。
- 核实通过率 = 通过 / 已有结论（通过或失败）的样本；阻塞与未核实（`not_checked`）单列。
- 仅管理员可建批次、登记与查看；观察者记为当前操作者。
