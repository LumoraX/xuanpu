# 能力运营统计与验收核实

> 给员工与平台管理员。设计见[能力运行结果与业务核实](../architecture/business-outcomes.md)。当前为源码实现，尚未生产部署；原生面板尚未提供，目前经 HTTP 接口使用。

## 看统计

- 员工：`GET /v1/me/capability-outcomes`（可选 `since`、`until` 秒级时间戳按受理时间选队列，`limit`、`offset` 分页）。总数与当前页无关；列表只有任务的能力、版本、状态、时间与 token。
- 管理员：`GET /v1/admin/capability-outcomes`，按能力与修订给计数与用量，看不到任务内容或具体员工。
- 同样的统计可用 Python SDK（`my_capability_outcomes`、`admin_capability_outcomes`、`create_acceptance_batch`、`observe_acceptance`、`get_acceptance_batch`）或命令行 `xuanpu knowledge --credentials <私有JSON> outcomes|admin-outcomes|batch <批次ID>` 读取；命令行只读，不登记核实结论。
- 读法：技术完成率只说明任务跑完了，不等于业务做对了；token 分「实际」与「占用上界」两栏，没有记录的任务单独计数；没有价格表时费用显示为空，不是 0 元。

## 验收核实（管理员）

1. 建批次：`POST /v1/admin/acceptance-batches`，写明能力修订、验收标准与样本任务（须是用这个能力修订跑的任务）。批次建后不能改；要调整就另建一批并写 `supersedes`。
2. 逐个样本登记结论：`POST /v1/acceptance-batches/{id}/observations`，结论为通过、失败或阻塞，并给出证据引用（例如成果文件与核对记录）。写错了再记一条并在 `supersedes` 里指明被纠正的那条。
3. 看结果：`GET /v1/acceptance-batches/{id}`。通过率只按已有结论的样本计算，阻塞与未核实的样本单列。登记核实结论不会改动任务、交接或审批的状态。
