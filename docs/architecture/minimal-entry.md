# 极简入口（活文档）

> R5-E。为什么这么定见 [ADR 0017](../decisions/0017-butler-artisan-orchestration.md) 决策 8；设计与计划见 [R5-E 设计](../superpowers/specs/2026-11-14-r5e-minimal-entry-design.md)、[R5-E 计划](../superpowers/plans/2026-11-14-r5e-minimal-entry.md)。尚未生产部署。

## 概览（`dashboard/`、`api/dashboard.py`）

`GET /v1/dashboard` 一次返回四块，每块是数量加至多 5 条摘要（接口细节见 [HTTP 接口](http-api.md#员工概览r5-e0)）：

- **今天**：本人时区里当天到期的活动提醒、与当天有交集的日程、当天及之前到期的未完成待办（逾期的标 `overdue`）。
- **等我处理**：未处理的助手草稿（`WorkspaceService.pending_proposals`，最多统计最近 100 条）、管家待本人处理项（管家开启时）、`waiting_user` 的交办工作、轮到本人审批的组织审批（审批开启时；读取审批列表与审批页面同口径，会同样把已失效的待审批标为阻塞）。
- **进行中**：本人活动 trace（执行追踪开启时才有这一块）；摘要标出是否有 span 在等本人输入、记录是否完整。
- **未读通知**：通知中心的站内通知（提醒、业务跟进、执行安排等同一张表），总数由存储计算（`ReminderStore.unread_notifications`）；共享事项与协作通知各有自己的流，不计入。

规则：

- 先校验员工身份；机器身份、停用员工、暂停租户整个请求 403。
- 四块并发读取、互不依赖。平台错误把这一块标成 `error` 并给错误码；意外异常记日志「概览块 … 读取失败」后给 `internal_error`。都不影响其他块。
- 全程不调用模型；模型停用时概览照常可用。
