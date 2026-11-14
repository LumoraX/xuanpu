# R5-D 匠人编组实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans，依赖 C。

**Goal:** 管家用声明式计划让多个匠人并行、接力或会诊，并裁决分歧。

**Architecture:** 见 [R5-D 设计](../specs/2026-11-14-r5d-artisan-group-design.md)。编组是多个 C 段派工的父记录；跨包并行在任务层，不合并引擎。

**Tech Stack:** C 段派工与唤醒、PG 协调事务、B 段 span。

---

## D0：计划校验与并行、接力

**Files**：Create `S/butler/groups/{__init__,plan,validate,store,memory,postgres,runner}.py`、`S/controlplane/migrations/<编号>_artisan_groups.sql`、`T/butler/groups/{test_plan_validate,test_parallel,test_relay,test_cancel,test_restart}.py`；Modify `S/butler/{tools_dispatch,wakeup,budget}.py`、`S/tracing/collector.py`（`group` span）。

- [ ] 计划模型严格解析：未知字段拒绝；成员数、步数、`when` 只认前一步终态；成员匠人须在本人当前清单内。
- [ ] 并行：全部成员终态后唤醒管家一次；部分失败如实汇报。
- [ ] 接力：传成果引用与摘要并复核下一成员的读取资格；任一步失败即停。
- [ ] 取消编组请求取消全部未终态成员，按真实终态收敛；成员撤权只拒该成员。
- [ ] 在成员完成后、汇总前终止进程：重启后不重复派工、不重复汇总。
- [ ] Run：`uv run pytest packages/xuanpu-server/tests/butler/groups -q`；提交 `feat: 增加并行与接力编组`。

## D1：会诊与分歧裁决

**Files**：Create `S/butler/groups/{consult,divergence,verdict}.py`、`T/butler/groups/{test_consult,test_divergence_paths,test_minority_kept}.py`；Modify `S/butler/{inspector,pending}.py`。

- [ ] 会诊第一步：成员各给结论，互不可见内部过程；全部完成后比对。
- [ ] 分歧三档各走对路径；涉及金额、对外、审批的一律按高分歧交员工裁决；交付里保留每个成员原结论。
- [ ] 分歧比对的模型输出只决定路径，测试用固定替身覆盖边界值。
- [ ] Run 同上目录；提交 `feat: 增加会诊与分歧裁决`。

## D2：会诊消息区

**Files**：Create `S/butler/groups/{messages,mentions}.py`、`S/api/groups.py`、`T/butler/groups/{test_messages_acl,test_mention_rounds,test_message_injection}.py`；Modify `S/api/app.py`、`docs/architecture/butler.md`、`docs/guides/butler.md`、`docs/architecture/http-api.md`。

- [ ] 只追加消息区；成员只见发布内容；@ 追问唤醒被问成员，轮数上限后由管家收口。
- [ ] 消息内容当数据处理；反例：消息里要求成员越权或改计划。
- [ ] 真实模型跑一次三成员会诊，瀑布显示交叠，结果入验收记录。
- [ ] 同步文档；提交 `feat: 增加会诊消息区与定向追问`。
