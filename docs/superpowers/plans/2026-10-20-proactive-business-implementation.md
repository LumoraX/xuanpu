# 主动业务助理实施计划

> **For agentic workers:** 使用 superpowers:subagent-driven-development 执行有界任务与代码审查；部署由主任务统一执行。已获用户直接实现、全流程验证和通过后部署授权。

**Goal:** 从真实对话识别未登记的承诺，主动核实和安排后续，结合持久规则检查实现完成后停止的业务闭环。

**Architecture:** 工作区事务扩展主动检查、语义跟进和评估状态，复用员工锁、通知游标、渠道 outbox。模型经现有工厂、配额、审计调用，在事务外执行，提交时复核证据和版本。对话和功能页共用本人接口。

**Tech Stack:** Python/asyncio、Pydantic、PostgreSQL/asyncpg、现有 taifeng 模型适配、原生 JavaScript、Docker Compose。

## 任务 1：持久存储与统一通知

- [x] 在 `packages/xuanpu-server/tests/workspace/test_proactive_store.py` 先增加内存/数据库契约测试：回滚、租户隔离、同一通知只写一次、分页及重启读取。
- [x] 在 `workspace/models.py` 增加主动设置与工作区事务中的 `checks`、`followups`、`scan_state`、`pending_notifications`；增加后台 owners 分页协议。
- [x] 新增 `controlplane/migrations/0008_proactive.sql`（以实际当前末版本递增为准），在 `workspace/postgres.py`、`workspace/memory.py` 同事务保存检查、跟进、评估游标、动作审计与通知。
- [x] 扩展 `reminders/models.py`、Postgres 列投影、发送器，保留原提醒兼容与通知序列；主动通知附来源、正文、关联 ID 与 `alert_after`。
- [x] 待办维护独立业务进展时间和目标代际，元数据更新不重置进展。

## 任务 2：规则和 LLM 主动业务

- [x] 在 `tests/proactive/` 先验证完整闭环、规则阶段、引用校验、忽略、延后、版本冲突、预算失败、重启和并发认领。
- [x] 新增 `proactive/models.py`、`rules.py`、`service.py`、`worker.py`：规则检查与模型评估独立循环，持久租约、分页、退避，提交复核，单事项合并去重。
- [x] 新增 `proactive/semantic.py`，输出严格 JSON 候选及真实证据引用；从增量主对话与关联本人业务会话提取承诺和进展，不要求预建待办。
- [x] 在 `assistant.py` 复用模型工厂、计量、配额和并发准入，提供单独的主动评估提示；模型失败不伪造成功。
- [x] 在 `platform.py` 注册生命周期和关闭顺序；配置管理员频率/预算边界，普通用户不填密钥。

## 任务 3：业务处理入口

- [x] 新增 `api/proactive.py`：本人检查/跟进查询与带 `request_id`、版本的完成/延后/忽略/设置提醒动作；通过工作区事务协调原子状态变化。
- [x] 新增前端 `proactive.js`：通知中的处理入口、对话建议卡和功能页兜底，共用后端；标题正文使用 textContent。
- [x] 免打扰延迟悬浮提示与外部发送，站内通知仍可查看；设置后明确展示等待时间。
- [x] 用户后续自然语言决定须引用真实跟进并走现有确认；不让模型自行决定完成或发送业务消息。

## 任务 4：验收、发布和服务器检查

- [x] 执行 `uv run pytest`、`uv run ruff check --select F,S108,I,TC packages`、`uv run mypy packages/*/src`；新增前端交互脚本检查真实按钮与刷新恢复。
- [x] 用真实 gpt-5.6-luna 完成“只在对话表达承诺 → 主动核实 → 设置短期安排 → 到期再跟进 → 标记完成 → 不再通知”，保留不含凭据的回执。
- [x] 同步架构与使用说明，审查规格覆盖与代码质量后合入 main。
- [x] 按 `docker/deploy.sh` 本机构建并发布 image；备份服务器 PostgreSQL/配置、迁移、更新 Compose 版本，等待健康后验证公开接口与数据持久。
- [x] 通过当前正式域名验证通知、跟进动作和刷新；发布回执记录镜像、摘要、迁移版本与实测边界。
