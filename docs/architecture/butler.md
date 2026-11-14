# 管家（活文档）

> R5-C。为什么这么定见 [ADR 0017](../decisions/0017-butler-artisan-orchestration.md)；设计与计划见 [R5-C 设计](../superpowers/specs/2026-11-14-r5c-butler-design.md)、[R5-C 计划](../superpowers/plans/2026-11-14-r5c-butler.md)。当前实现到 C1（自办与单匠人派工）；唤醒、验收、代答（C2）与编组（R5-D）尚未实现。尚未生产部署。

员工对管家说话（`POST /v1/assistant/messages`）。管家能自己办的自己办（回答、只读查询、内置能力的待确认草稿），办不了的派给**一位**匠人——本人此刻可用的已发布业务包入口——并立刻答复，不等匠人结果。

## 开关

- `butler.allows(租户)` 与 `execution_trace.allows(租户)` 同时成立才走管家；配置加载时校验管家名单内的租户必须同时开启追踪（派工预算与执行树依赖追踪）。
- 未开启的租户、不代表员工的 API key：同一入口原样走既有分流（`/v1/assistant/route` 的逻辑），回执 `mode=route`。

## 引擎装配（`butler/engine.py`）

- 每个租户一个内核 `EnginePool`，只装平台内置技能 `butler/skills/butler/SKILL.md` 与管家工具。不加载业务包代码（红线 1），只用 `taifeng.__all__`（红线 3）。
- 没有沙盒、脚本执行器（`script_executors={}`）、连接器、文件、命令或网络工具。技能声明 `strict_tool_names`，模型可见的工具只有 `propose`、`read_personal`、`list_artisans`、`dispatch_artisan`、`check_dispatch`；内核默认的 `read_skill`、`call_skill`、`run_script` 留在注册表里但对模型不可见。
- 模型档位 `balanced`；每轮最多 12 次迭代、会话 token 上限 20 万、整轮 90 秒。
- **每次输入一轮新引擎**：建引擎 → 接上与业务会话同一个事件枢纽（审计、按员工计量、执行追踪，根 span 类型 `butler`、trace 标识预先分配）→ 提交 → 读到本轮终态 → 释放引擎。员工的长期对话是主对话的持久记录（工作台消息），每轮把最近 6 条连同当前时间、时区、原话、材料清单作为 **JSON 数据** 交给管家，不依赖引擎线程跨进程续接。
- 工具处理函数只拿得到内核线程标识；平台按线程标识找回本轮上下文（`butler/turn.py`），身份一律取自本轮，不取自模型参数。

## 工具（`butler/tools.py`）

| 工具 | 作用 | 约束 |
| --- | --- | --- |
| `propose` | 内置能力（提醒、工作站、业务跟进、执行安排、交接、共享、连接）的待确认草稿或打开列表 | 与分流共用 `assistant_proposals.resolve_native_choice`，卡片同形；每轮最多一个；业务包选择被拒 |
| `read_personal` | 本人进行中的提醒、本人工作任务 | 只读，最多 20 条，只给标题与状态 |
| `list_artisans` | 本人此刻可用的匠人 | 经 `ArtisanResolver`（现状：租户生效版本） |
| `dispatch_artisan` | 派给一位匠人 | 见下节 |
| `check_dispatch` | 查本人一次派工的状态 | 他人的与不存在的一样不可见 |

工具的平台错误与参数错误作为工具错误交给模型（`<错误码>: <文案>`），不中断本轮。

## 派工（`butler/dispatcher.py`）

1. 任务说明 1–4000 字；材料只能是员工本次请求里明确提供的文件版本，否则拒绝。
2. 经解析接缝取匠人；不可用一律 404。会话承载的匠人暂不支持附带材料。
3. 预算：本 trace 已有派工数、已结束执行的 token 合计（进行中的尝试结束后才计入；已结束执行用量未知时不放行）、租户进行中 trace 数；存储在租户级临界区内再核一次容量。超限 `butler_budget_exceeded`，不建记录。
4. 建派工记录：`request_id` = `<员工请求标识>:d<序号>`，同一输入重试回放原派工，不重复启动；`trace_id` 为本次请求的 trace，`parent_span_id` 为管家根 span（`ex.<execution_id>`）。
5. 启动承载：持久任务（`accept_capability`，请求标识 `butler:<派工>`）或会话（钉住派工时的版本建会话并发出说明；这一轮在后台读完）。成功推进到 `running` 并记 `host_ref`；启动失败记 `failed`（`start_failed`）后把错误交给模型。
6. 立即返回；匠人结束后的唤醒与验收在 C2。

## 入口与幂等（`butler/service.py`、`api/butler.py`）

- `request_id` 必填。先把原话写进主对话（`<request_id>:user`），任何后续失败输入都还在。
- 按 `butler:<request_id>` 认领（复用工作台的路由认领与 60 秒租约）：同键同体回原回执，同键异体 409，并发同键 409。
- 失败（模型不可用、本轮失败或挂起、超时、回复为空）统一 503 `runtime_unavailable`「输入已保留，请稍后重试」，释放认领；不自动选任何业务。同一标识重试会重新跑一轮，已建的派工按派工请求标识回放。
- 成功后写助手消息（`<request_id>:assistant`，元数据 `route` 与分流同形、另带 `butler.trace_id`），草稿因此可经既有 `/v1/assistant/proposals` 确认或取消。

## 已知限制

- 管家不能取消派工、不能代答匠人提问、不会在匠人结束后被唤醒（C2）。
- 管家每轮新建引擎，内核 `todo_write` 清单不跨轮；跨轮记忆只有主对话最近 6 条。
- 持久任务承载的派工路径只有单元级覆盖（本机测试环境无法运行办公容器）；会话承载有端到端测试。
- 真实模型下的误派、漏派率尚未测量（C2 回归集）。
