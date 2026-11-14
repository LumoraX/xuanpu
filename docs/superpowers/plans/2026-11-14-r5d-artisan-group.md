# R5-D 匠人编组实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans，依赖 C。

**Goal:** 管家用声明式计划让多个匠人并行、接力或会诊，并裁决分歧。

**Architecture:** 见 [R5-D 设计](../specs/2026-11-14-r5d-artisan-group-design.md)。编组是多个 C 段派工的父记录；跨包并行在任务层，不合并引擎。

**Tech Stack:** C 段派工与唤醒、PG 协调事务、B 段 span。

---

## D0：计划校验与并行、接力

**Files**：Create `S/butler/groups/{__init__,plan,validate,store,memory,postgres,runner}.py`、`S/controlplane/migrations/<编号>_artisan_groups.sql`、`T/butler/groups/{test_plan_validate,test_parallel,test_relay,test_cancel,test_restart}.py`；Modify `S/butler/{tools_dispatch,wakeup,budget}.py`、`S/tracing/collector.py`（`group` span）。

- [x] 计划模型严格解析：未知字段拒绝；成员数、步数、`when` 只认前一步终态；成员匠人须在本人当前清单内。
- [x] 并行：全部成员终态后唤醒管家一次；部分失败如实汇报。
- [x] 接力：传成果引用与摘要并复核下一成员的读取资格；任一步失败即停。
- [x] 取消编组请求取消全部未终态成员，按真实终态收敛；成员撤权只拒该成员。
- [x] 在成员完成后、汇总前终止进程：重启后不重复派工、不重复汇总。
- [x] Run：`uv run pytest packages/xuanpu-server/tests/butler/groups -q`；提交 `feat: 增加并行与接力编组`。

### D0 进度（2026-11-14，macOS arm64，Python 3.12，本机 taifeng 2026.11.14.1 + taifeng-sandbox `af3e545`）

- 已写：`S/butler/groups/{plan,validate,store,memory,postgres,trace,runner,summary,tools}.py`；迁移 `0024_artisan_groups`；`ButlerConfig.max_group_members` / `max_group_steps`；工具 `dispatch_group`、`cancel_group`、`check_group`；`Dispatcher.launch`（不依赖管家一轮的派工入口，编组成员用）；派工记录加 `group_id`；唤醒对编组成员只登记结果不单独交付，并在每轮处理后推进编组；编组 span `gp.<id>`（kind `group`）挂在管家根下，成员派工 span 挂在编组下。
- 与计划的差异：数据未拆成 `group_member` 表，成员执行嵌在编组记录里按版本整体保存（单行事务即可保证推进原子性）；`group` span 由编组存储包装层写入，没有改 `S/tracing/collector.py`；文件 `tools_dispatch` 未新建，编组工具在 `S/butler/groups/tools.py`。
- 重启：用全新执行器实例在「派出成员后、保存前」「写汇总后、标记前」两处模拟终止（`T/butler/groups/test_restart.py`），重启后不重复派工、不重复汇总。不是真实独立进程；执行器不持有进程内状态，真实进程终止测试沿用 C2 的唤醒 worker 未扩展到编组（延后）。
- 门禁：`.venv/bin/pytest packages/xuanpu-server/tests/butler packages/xuanpu-server/tests/tracing …迁移与配置测试` 261 passed, 2 skipped；ruff All checks passed!；mypy Success: no issues found in 543 source files。
- 全量（沙盒外）：3693 passed / 60 failed / 49 errors / 443 skipped，失败集合与 main 同环境已知集合逐条一致（办公容器找不到 docker）。
- 延后：任务承载成员的端到端（本机无法运行办公容器）；真实独立进程终止的编组重启测试；文件类成果随接力传递（会话承载不收材料）。

## D1：会诊与分歧裁决

**Files**：Create `S/butler/groups/{consult,divergence,verdict}.py`、`T/butler/groups/{test_consult,test_divergence_paths,test_minority_kept}.py`；Modify `S/butler/{inspector,pending}.py`。

- [x] 会诊第一步：成员各给结论，互不可见内部过程；全部完成后比对。
- [x] 分歧三档各走对路径；涉及金额、对外、审批的一律按高分歧交员工裁决；交付里保留每个成员原结论。
- [x] 分歧比对的模型输出只决定路径，测试用固定替身覆盖边界值。
- [x] Run 同上目录；提交 `feat: 增加会诊与分歧裁决`。

### D1 进度（2026-11-14，macOS arm64，Python 3.12，本机 taifeng 2026.11.14.1 + taifeng-sandbox `af3e545`）

- 已写：`S/butler/groups/{consult,divergence,verdict,rater}.py`；`StepMode` 加 `consult`；`GroupRecord.verdicts`；`GroupRunner` 在会诊一步完成后先比对并保存裁决（`_judge`）、高分歧建裁决项并等员工（`rule`）、取消时关闭裁决项；管家工具 `rate_divergence`（只在 `purpose=consult_compare` 的比对轮可用，`_refusal` 统一按轮次限制工具）；待处理项新增 `kind=ruling`，`PendingService.answer` 对裁决项只记进编组、不转给匠人；`trace_usage` 从 `Reviewer` 抽出供比对轮核预算；汇总每步先写综合或裁决，再列各位匠人的原结论。
- 与计划的差异：`S/butler/inspector.py` 未改（验收逻辑不变）；裁决项复用 `butler_pending` 表（挂在该步第一位成员的派工上，`source_key=ruling:<编组>:<步>`），没有新迁移；模型评级不另建引擎，跑在管家引擎上（比对轮只允许 `rate_divergence`）。
- 测试：`T/butler/groups/test_divergence_paths.py`（敏感话题边界、三档选路、固定替身覆盖评低/中/高/评不出/模型失败/依据越界/未装配、敏感话题压过低评级、文本保留原结论）；`test_consult.py`（低分歧综合与成员互不可见、高分歧裁决与幂等/冲突、金额强制交员工、等裁决时取消、比对后建裁决项前终止重启只一份）；`test_minority_kept.py`（中分歧保留少数意见）；D0 的会诊拒绝用例改为未知模式。
- 门禁：`.venv/bin/pytest packages/xuanpu-server/tests/butler packages/xuanpu-server/tests/tracing packages/xuanpu-server/tests/test_config.py -q` → 279 passed, 2 skipped；`ruff check --select F,S108,I,TC packages` → All checks passed!；`mypy packages/*/src` → Success: no issues found in 547 source files。全量随 D2 提交前跑。

## D2：会诊消息区

**Files**：Create `S/butler/groups/{messages,mentions}.py`、`S/api/groups.py`、`T/butler/groups/{test_messages_acl,test_mention_rounds,test_message_injection}.py`；Modify `S/api/app.py`、`docs/architecture/butler.md`、`docs/guides/butler.md`、`docs/architecture/http-api.md`。

- [x] 只追加消息区；成员只见发布内容；@ 追问唤醒被问成员，轮数上限后由管家收口。
- [x] 消息内容当数据处理；反例：消息里要求成员越权或改计划。
- [ ] 真实模型跑一次三成员会诊，瀑布显示交叠，结果入验收记录。（**延后**：本环境没有真实模型凭据，未执行；脚本 `scripts/acceptance/r5_consult_acceptance.py` 已就绪，判定口径有单测）
- [x] 同步文档；提交 `feat: 增加会诊消息区与定向追问`。

### D2 进度（2026-11-14，macOS arm64，Python 3.12，本机 taifeng 2026.11.14.1 + taifeng-sandbox `af3e545`）

- 已写：`S/butler/groups/{messages,message_postgres,mentions,board}.py`、`S/api/groups.py`；迁移 `0025_group_messages`；`GroupRecord.followups`（`FollowUp`）；`ButlerConfig.max_consult_rounds`（默认 2，0–5）；`GroupRunner` 在会诊一步完成后、比对前推进消息区（发布结论 → 等追问 → 派出下一次追问 → 超限收口），追问的结果与摘要经唤醒登记（`_with_result`），取消时一并停止进行中的追问；`ControlPlaneStore.group_message_store`；SDK `get_butler_group`、`list_group_messages`；验收脚本 `scripts/acceptance/r5_consult_acceptance.py`。
- 与计划的差异：追问不复用被问成员的原会话，而是对同一匠人的一次新派工（被问成员只拿到已发布内容与追问原文；不依赖两个引擎常驻，任务承载与会话承载同一路径）；每次追问计入 `max_artisans_per_trace`。只有全部成员完成的会诊步才开消息区；有成员失败的会诊步照实汇报，不比对。
- 测试：`T/butler/groups/test_messages_acl.py`（只追加、按键去重、并发序号连续、按员工隔离，memory 与 PG；接口本人可读、他人 404、机器身份 403）；`test_mention_rounds.py`（只唤醒被问成员、被问成员只见已发布内容、一轮上限收口、两轮放行、追问派出后保存前终止重启不重复）；`test_message_injection.py`（不存在的成员与非成员名字被忽略、对真实成员的越权要求只成为一次数据形式的追问、计划与匠人不变、涉及审批付款仍交员工）；`test_consult_acceptance_script.py`（验收脚本判定口径）。
- 门禁：`.venv/bin/pytest packages/xuanpu-server/tests/butler packages/xuanpu-server/tests/tracing packages/xuanpu-server/tests/test_config.py packages/xuanpu-server/tests/test_controlplane_migrations.py packages/xuanpu-server/tests/test_optional_identity_store.py packages/xuanpu-sdk-py/tests -q` → 338 passed, 2 skipped；`ruff check --select F,S108,I,TC packages` → All checks passed!；`mypy packages/*/src` → Success: no issues found in 552 source files。
- 全量（沙盒外）：`.venv/bin/pytest -q -rfE packages` → 3735 passed / 60 failed / 49 errors / 443 skipped（18:15），失败集合与 main 同环境已知集合逐条一致（办公容器找不到 docker）。
- 延后：真实模型三成员会诊（无凭据）；编组的真实独立进程终止测试（同 D0）；任务承载成员与追问的端到端（本机无法运行办公容器）。
