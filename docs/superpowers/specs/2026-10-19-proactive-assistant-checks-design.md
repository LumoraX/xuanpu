# 玄圃主动检查助理设计

## 目标与首期边界

玄圃是企业核心。主动检查是玄圃原生能力，定期检查本人工作区中的开放待办，识别短期拖延和长期未处理，并通过玄圃通知中心、网页浮层以及用户已启用的外部渠道提醒用户。

首期只处理 `workspace_item.kind=todo`：

- 有截止时间的开放待办：截止后识别为逾期，分阶段提醒。
- 没有截止时间的开放待办：按最后一次有效变更时间识别为长期未处理。
- 不推断未建模的日程“是否参加”、事项“是否需要跟进”，不把这些状态伪装成已支持。
- 系统可以提出下一步建议，但不会未经确认提交报销、发送外部消息或修改企业业务数据。

“短期应完成但长期未完成”的首期可解释信号是：用户创建了开放待办并设置 `due_at`，但超过截止时间仍未完成；没有截止时间的待办按长期未处理规则处理。未来如果引入预计时长或事项策略，再增加新的检查类型和迁移。

## 判定规则

### 有截止时间的逾期待办

只检查 `status=open` 且 `due_at` 已存在的待办。以待办的 `due_at` UTC 时间戳和用户设置中的 IANA 时区计算经过时间；阈值是绝对 elapsed duration，不按服务器本地日历计算，因此不受夏令时跳变影响。

- 到达截止时间：阶段 `overdue_due`，生成一次提醒。
- 逾期 1 小时：阶段 `overdue_1h`。
- 逾期 1 天：阶段 `overdue_1d`。
- 逾期 3 天：阶段 `overdue_3d`。

每一阶段最多生成一条主动检查通知。标记完成、删除或归档后立即停止；延后会推进工作区版本、关闭旧检查代际并按新 `due_at` 重新计算。

### 没有截止时间的长期未处理待办

只检查 `status=open` 且 `due_at` 为空的待办。`last_activity_at` 取创建时间和最近一次有效业务变更时间中的较晚者；修改标题、描述、关联事项、标签、状态、回收/恢复都属于有效变更并重置计时，单纯读取不重置。

- 连续 3 天无变化：阶段 `stale_3d`。
- 连续 7 天无变化：阶段 `stale_7d`。
- 连续 14 天无变化：阶段 `stale_14d`。

阶段使用绝对 elapsed duration。用户选择忽略后，本阶段不再提示；工作区版本变化或进入新阶段后仍可产生新提示。阶段以稳定的 `stage_index` 表示，阈值快照写入检查记录，避免设置变化后改变已产生记录的含义。回收的待办不扫描，恢复后以恢复变更时间重新开始计时。

## 持久化模型

新增迁移表 `proactive_check`，至少包含：

- `tenant_id`, `user_id`, `item_id`；
- `generation`：等于待办有效变更后的 `item_version`，每次有效变更形成新代际；
- `check_type`：`overdue` 或 `stale`；
- `stage_index`：从 0 开始的稳定阶段序号；
- `threshold_seconds`：本代采用的固定阈值快照；
- `basis_at`：本阶段的依据时间；
- `item_version`：候选快照对应的工作区版本；
- `status`：`open`, `notified`, `ignored`, `resolved`, `failed`；
- `first_seen_at`, `notified_at`, `handled_at`；
- `attempts`, `next_attempt_at`, `lease_until`, `claim_token`, `last_error`；
- `created_at`, `updated_at`。

唯一键为 `(tenant_id,user_id,item_id,generation,check_type,stage_index)`。工作区每次有效变更必须推进版本；完成、延后、回收、恢复会在同一协调事务中关闭旧代检查。通知仍保留为历史记录，不因检查关闭而删除。扫描时重新读取并锁定待办，确认版本、状态和时间仍匹配后才写入检查记录。

新增 `proactive_scan_state`，主键为 `(tenant_id,user_id)`，保存 `round_id`、`last_item_id`、`updated_at`。到达末页时开启新的 `round_id` 并清空游标；候选锁定失败不推进游标。

扩展现有 `notification` 表而不新建第二个通知流：`reminder_id` 改为可空，增加 `source`（`reminder` 或 `proactive_check`）、`proactive_check_id` 可空、`body` 可空，保留现有数据库序列 `notification_id` 作为用户范围内的全局单调游标。主动检查通知使用 `source=proactive_check`、`reminder_id=NULL`、`proactive_check_id` 非空；唯一索引为 `(tenant_id,user_id,proactive_check_id)`。现有提醒通知保持 `source=reminder`。`/v1/notifications` 和 SSE 继续使用同一个 `notification_id` 游标，返回 `source`、`body`、`reminder_id`/`proactive_check_id` 和 `delivery`，前端不需要合并两个接口。

现有 `notification_delivery.notification_id` 外键继续复用。主动检查发送使用相同的 `notification_id`、租约、重试字段和 Feishu 幂等 key；发送正文从 `body` 读取，`due_at` 为空时不生成原定时间行。这样外部渠道仍只有一套投递状态和租户/用户隔离。
## 后台协议与并发

新增 `ProactiveCheckStore`，禁止通过 HTTP 入口或伪造员工 Principal 读取工作区。后台查询必须显式带 `tenant_id,user_id`，只枚举 active tenant 和 active user；停用时关闭未处理检查并取消未发送外部投递。

新增 `ProactiveCheckWorker`，与现有 `ReminderWorker` 一样由应用生命周期启动和关闭：

1. 每轮按租户、用户分页读取候选，批次上限 100，并使用 `proactive_scan_state(tenant_id,user_id,round_id,last_item_id)` 的稳定 `(tenant_id,user_id,item_id)` 游标继续扫描；到达末页后开启新 round 并清空游标，避免大租户饥饿。
2. 用 `FOR UPDATE SKIP LOCKED` 或等价租约 claim 候选；锁顺序固定为租户 → 用户 → 工作区项目 → 检查记录，和工作区写事务一致。扫描 claim 失败不推进游标，下一轮从原位置重试。
3. 在短事务内重新校验版本、状态、`due_at`、`last_activity_at`，插入或更新 `proactive_check`，必要时创建 `notification` 和 `notification_delivery`，并推进扫描游标，然后提交。
4. `proactive_check` 保存 `attempts`、`next_attempt_at`、`lease_until`、`claim_token`、`last_error`；数据库错误不推进游标，候选按 1/5/30 分钟退避，超过 5 次进入 `failed` 并保留人工可重试入口。
5. 不在事务内调用模型或外部网络。外部发送沿用 `notification_delivery` 的 lease、最多五次可重试和幂等 key；站内通知写成功不受飞书失败影响。
6. 模型润色失败只保留模板正文并记录错误，不阻塞检查落库；结构化检查记录可单独重试。

主动检查每 60 秒扫描一次；这不是硬实时承诺。服务重启、扫描中断或多实例并发后依靠持久记录、锁和唯一约束恢复。

## 用户动作与接口

通知和对话建议卡展示：事项标题、检查类型、阶段、依据时间和操作入口。动作均带 `request_id` 与 `expected_item_version`。新增 `WorkspaceCheckCoordinator`，与 WorkspaceService 共用同一数据库事务和锁顺序，在一次事务内完成工作区更新、旧 generation 关闭、新 generation 创建和动作审计；内存实现使用同一把用户锁。通过现有工作区/提醒服务执行并记录审计：

- `open`: 打开待办详情，不改状态；
- `complete`: 将待办改为 `done`，检查标记 `resolved`；
- `postpone`: 写入新的 `due_at`，关闭旧代并创建新代；
- `set_reminder`: 复用 `ReminderService`，仍需用户确认提醒时间和渠道；
- `ignore_stage`: 将当前检查标记 `ignored`；
- `ignore_type`: 用户设置中关闭同类主动检查，可设置过期时间。

版本冲突返回 409，前端重新读取待办和检查状态；同一 `request_id` 重放返回第一次结果，不重复写入。

## 用户设置

扩展 `SettingsData`，向后兼容缺省值；首期阈值固定，不开放用户自定义阶段，避免阶段语义漂移：

```json
{
  "proactive_checks": {
    "enabled": true,
    "quiet_hours": {"enabled": false, "start": "22:00", "end": "08:00"},
    "ignored_types": [{"type": "stale", "until": "2026-11-01T00:00:00+08:00"}]
  }
}
```

`ignored_types` 使用对象数组，`until` 为含偏移的 ISO8601 时间，可为空表示持续忽略，用户重新开启或明确设置截止时间后恢复。IANA 时区来自用户设置，免打扰按该时区解释并支持跨午夜；免打扰只延迟网页浮层和外部投递，通知仍立即写入玄圃并保持未读。关闭主动检查后不创建新检查；重新开启时只评估当前阶段，每个待办最多补发一条当前阶段通知，不补发历史阶段，避免通知洪泛。

## 通知、模型与渠道

玄圃通知中心是权威结果。网页通过同一个游标接口补读 `reminder` 和 `proactive_check` 两类通知，来源字段用于展示“定时提醒”或“主动检查”。网页打开时显示右侧浮层，关闭网页后通知仍持久保存。

模型只负责对结构化检查结果生成简短中文建议；模型不可用时使用确定性的模板，不影响阶段判定、通知落库或动作幂等。飞书是可选出口，沿用已有投递状态和重试；飞书失败不撤销玄圃通知。

## 验收

1. 有截止待办在截止、1 小时、1 天、3 天四个固定阶段各最多一条通知；重复扫描、服务重启、多实例并发均只产生一条。
2. 无截止待办在 3 天、7 天、14 天无有效变化阶段各最多一条通知；标题、描述、标签、关联、回收/恢复会重置计时。
3. 标记完成、删除、归档后不再检查；延后后旧代停止，新 `due_at` 重新计算。
4. 忽略当前阶段后不重复提示；进入新阶段或版本代际变化后按规则恢复检查。
5. 版本冲突返回 409；同一 `request_id` 重放不产生第二次动作。
6. 关闭主动检查不生成新检查，已有未读通知仍可查看；免打扰期间延迟投递但不丢失。
7. 停用租户/员工后不产生新检查，外部 delivery 被取消或标为 skipped；重新启用不跨租户读取数据。
8. 数据库重启、扫描中断、批次超过 100 条、游标继续扫描后最终覆盖全部候选。
9. 用户时区跨 DST 时，绝对 elapsed duration 不重复也不跳过阶段。
10. 站内通知写入成功但飞书发送失败时，站内仍可见，delivery 状态可重试。
11. 全量 `pytest`、Ruff、mypy 通过，并在部署环境完成一轮真实短期逾期检查。

## 后续扩展

日程的“开始后未确认”、事项的长期无进展、预计时长和企业业务流程需要独立数据契约与验收，不在本首期实现中承诺。
