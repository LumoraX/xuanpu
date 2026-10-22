# R3-E 统一体验、真实试点与发布 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans逐项执行。Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让真实员工用企业微信只读信息和玄圃协同完成一项内部材料核对，以分层证据发布R3并保留可验证的前向回退。

**Architecture:** 整合A–D已实现的同一原生接口；默认主对话、统一动态和按职责直接入口，不另建审批/交接重复数据库。复用R2构建、灰度、隔离、观测和备份流程。

**Tech Stack:** 现有FastAPI、原生JS、SDK/CLI、pytest/Node/CUA、Linux rootless Docker和当前镜像发布工具。

前置：[A](2026-10-22-r3a-connections-wecom.md)、[B](2026-10-22-r3b-organization-authority.md)、[C](2026-10-22-r3c-shared-matters.md)、[D](2026-10-22-r3d-handoffs-approvals.md)。本计划不授予真实发消息、外部业务写入或共享宿主整机重启权限。

## E0：统一入口、可理解状态及无模型路径

Files：Modify `packages/xuanpu-console/src/xuanpu_console/static/{index.html,workspace.js,workspace-chat.js,notification-alerts.js,style.css}`和A–D控制台模块；Modify对应SDK/CLI、server `assistant.py`；Create `tests/r3_contract/test_collaboration_views.py`及console `r3_journey_behavior.cjs`。

- [ ] 写完整DOM行为反例：模型草稿不能提交、无权按钮不可用但server仍拒绝、总数不随分页变化、旧身份迟到响应不覆盖、断网重试不重复提交/认领/批准/签收。
- [ ] 主对话可发起明确事项与交接候选；统一卡解释“等待部门认领/等待补充/等待第N步审批/结果待签收/权限失效/已签收”。员工不必选择内部package、工具名或密钥。
- [ ] 保留“连接、共享事项、部门队列、待审批”按职责直接入口；审批和签收显示不同动作、材料版本、谁有权决定及结果证据。不能把“审批通过”渲染成“交接已完成”。
- [ ] 模型断网时直接入口能完成全部状态转换，窄屏390px及键盘焦点实测；通知有意义变化才推送，补读按当前ACL过滤，外部渠道默认关闭。
- [ ] 跑`uv run pytest packages/xuanpu-console/tests packages/xuanpu-server/tests/r3_contract/test_collaboration_views.py packages/xuanpu-sdk-py/tests packages/xuanpu-cli/tests -q`；提交`feat: 整合企业连接和协同员工入口`。

## E1：冻结真实试点和跨层证据

Files：Modify `scripts/acceptance/r3_connections.py`、`r3_shared_matters.py`、`r3_collaboration.py`；Create `scripts/acceptance/r3_acceptance.py`；实现时按实际日期新增release验收记录与脱敏JSON。

- [ ] 试点企业先选一项“内部材料核对与结果交付”，冻结真实部门、四名参与者的职责和预期结果；真实企业用户/应用授权需当时明确范围，不借用root或合成Cookie冒充真实企微登录。
- [ ] 固定输入revision、共享成员、审批规则、预计成果；写独立业务检查器，不以模型报告/HTTP200/task succeeded替代事实。目录字段未返回则unknown，不能补造上级或手机号。
- [ ] 分开保存：源码测试、PG故障、真实HTTP、真实模型语义、CUA、真实企微与组织范围、运行镜像。每轮新证据目录，原失败与操作准备失败都保留。
- [ ] 至少覆盖：scope缩小/token更新/在途撤销，旧上下文撤权，单认领，退回补充，输入变更使旧批准失效，离职blocked，审批后重启，固定成果签收及重复请求，外部写调用计数0。
- [ ] 完成真实企微A闭环和真实多员工D闭环后才签收E1；外部配置或真实用户缺失时状态partial，列明缺项，不把mock通过记为整体完成。

## E2：构建、兼容迁移与逐租户灰度

Files：Modify实际变更包的`pyproject.toml`和`uv.lock`、必要`docker/compose.office.yml`/探针；更新架构、HTTP API、员工指南、运维手册和最终release记录。

- [ ] 全量`uv sync --all-packages --all-extras`、`uv run pytest`、`uv run ruff check --select F,S108,I,TC docker packages`、`uv run mypy packages/*/src`、`uv lock --check`、`git diff --check`，记录实际passed/skipped和独立复核。规范字段变化先跑spec旧包兼容测试，不能盲升或不升版本。
- [ ] 按实际日期升CalVer，白名单构建镜像，核对manifest/config digest、lock、全部安装包及源码SHA；不把credentials、企业材料或整个源码树送入镜像上下文。
- [ ] 有部署授权后，停新受理/触发并等待资源收尾、lease归零；备份PG/data/配置/执行服务及完整旧镜像，逐项校验。若旧镜像缺层先修正，不能拿元数据tar当有效离线归档。
- [ ] 先全关闭R3五开关运行兼容reader，应用实际0015起连续迁移，核对R2旧输入/成果/预算/effect原行及对象SHA；平台UID10001、executor UID10002、socket/idmapped和资源上限不放宽。
- [ ] 先隔离合成租户，再经明确授权的真实部门；依次开放connections→organization→shared_matters→handoffs/approvals。每一步保存实际配置范围，避免空名单误全量；旧R2任务不自动变共享或企业审批。
- [ ] 观察至少一个真实到期交接通知周期和一次组织采集/差异预览周期，核对每条业务状态、唯一决定、outbox、权限失败、预算和资源残留；实际多员工闭环不能只看healthz。

## E3：前向回退、清理及最终签收

Files：Update最终release记录及总计划状态；必要扩展验收脚本的cleanup子命令，不在应用业务流程出现运维细节。

- [ ] 演练关闭新连接读取/组织导入/新共享/新交接/新审批提交，保留兼容查询、撤销、材料撤回、取消及已有合法事项的必要完成动作；按每个领域的可用动作表执行，不能一刀关闭所有解困入口。
- [ ] 撤销员工委托、轮换/清除测试token、取消未来触发和提醒、停止临时HTTP侧车、注销登录、吊销临时key并暂停合成租户；末位管理员保护保留。共享对象和决定历史按保留策略保存，不为了清理删业务回执。
- [ ] 记录前后外部只读请求、外部写0、未知效应和预算原账本不变。不能只切回旧R2镜像；只有显式停写并证明无新正式业务/外部影响时才另行讨论快照恢复。
- [ ] 平台重建与执行daemon恢复分别验；若R3改变了启动单元/挂载/密钥加载等开机依赖，再取得共享宿主维护授权后实测整机重启，不沿用R2重启证据证明新变更。无此变更时标明本轮未重复该破坏性检查。
- [ ] 源码、客户端/包、镜像、迁移、真实企微、真实多员工业务、模型、浏览器、运行环境、回退和清理分别签收。尚缺任一强制试点证据不得勾选整体完成；非目标能力明确列未验。
- [ ] 最终提交`feat: 发布并验收R3企业数据与协同`和独立脱敏证据；在有授权的主线集成方式下核对实际源码，更新文档索引，保留可恢复原始证据后清理临时检出。

**整体完成定义：** 企业微信真实只读、组织权威、当前ACL共享、部门交接、独立组织批准与成果签收闭环都具有真实证据。没有外部写/无人部门授权/通用流程引擎的承诺，也不把它们暗藏进“上线完成”。
