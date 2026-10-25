# R4-B 到人分配与执行复核实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans，依赖A契约后实施。

**Goal:** 同一个能力可以明确分配给员工，所有运行路径核验同一授权；部门和岗位不是前置条件。

**Architecture:** 新增服务端能力解析与InvocationBinding，复用现有任务/会话和组织生命周期。目录是解析结果的展示，不能承担唯一鉴权。夹具与路径见[总计划](2026-10-25-r4-delivery.md)。

**Tech Stack:** Pydantic、PG协调事务、现有SessionManager/DurableWorker、taifeng公开模型与工具权限槽位。

---

## 解析和状态矩阵

拟接口：POST `/v1/capabilities/{id}/assignments`，body为`request_id, selector, allowed_revisions, valid_until`；selector严格区分`{kind:user,id,user_epoch}`与`{kind:position|unit,id,target_version}`，与现有记录字段对应。`allowed_revisions`是1–20项明确能力修订，无通配符；新增修订须重新确认权限。CAS撤销用POST `/v1/capability-assignments/{id}/revoke`。分配只授权使用能力，不授予读文件/使用连接/组织审批。直接user引用lifecycle_epoch，position/unit通过当前真实任职及其epoch解析。

`CapabilityDecision`服务内部返回`capability_revision, installation_revision, release_id, release_epoch, assignment_id, assignment_epoch, package_digest, entry, config_digest, tool_inventory_digest`。只能由认证员工和权威记录构造。保存到独立`invocation_binding`表，关联既有session_id/task_id，不让旧R3严格JSON reader读新增字段。执行时同时检查冻结快照与当前有效资格，不用“现在有别的授权”替换失效的原依据。

| 条件 | 新会话/新任务/新后台发生实例 | 已绑定任务恢复/原会话续聊/模型及工具 | 历史/取消/未知核实 | 已有R3审批或签收 |
| --- | --- | --- | --- | --- |
| ready + active发布 + 有效原分配 | 可受理并固定 | 可，逐次复核来源/预算 | 按原权限定 | 仍独立核验R3身份、版本和来源 |
| ready + retired发布（正常升级/回退） | 不按该旧发布新建 | 原绑定可继续，不替换包/配置；原分配及来源仍须有效 | 保留 | 不自动决定 |
| draining安装 | 不新建 | 原绑定可继续合法业务和收尾 | 保留 | 不自动决定 |
| disabled安装 | 不新建 | 阻塞新模型/工具/批准恢复；不丢既有事实 | 保留必要只读、取消和只读核实，不能借核实新写 | 安装停用本身不抹掉独立待办；R3按自身有效资格决定 |
| revoked发布/原分配或员工epoch失效 | 不新建 | 拒绝，终止可取消执行；外部结果未知不自动重放 | 仅当前有权的历史及系统核实 | 不能绕过R3失效；能力撤销不冒充业务驳回 |
| 治理开关关闭 | 已纳管能力拒绝新建；不退回旧全租户入口 | 已绑定工作按上列当前资格收尾，不撤掉检查器 | 保留 | 原R3检查保留 |

原会话续聊仅能继续已绑定发布/入口；切换入口或开启新能力属于新受理。历史查看仍受数据ACL及来源依赖约束，不因上述“保留”获得正文权限。

## B0：分配、当前资格与权限收紧

**Files**：Create `S/capabilities/{assignments,resolver,authorization,bindings,invalidations}.py`、`S/controlplane/migrations/0021_capability_assignments.sql`、`T/r4_contract/{test_assignments,test_assignment_epochs,test_capability_decisions}.py`；Modify A的`store/memory/postgres/service`、`S/organization/service.py`和`S/platform.py`中既有组织维护循环装配，不新增另一套停用循环。

- [ ] 写user分配、过期、跨租户、停用重启、岗位离任/重新任职反例；没有部门的Alice可正常分配，客户端自报岗位无效。
- [ ] 同一能力允许多项有效分配，但一次绑定固定一个明确有效依据；重复同发布合并展示，命中不同发布时409要求管理员整理，不按最近版本或模型偏好选。
- [ ] 撤销先在事务增加epoch并拒绝下一次调用，再由既有维护循环回收引擎。UI收到权限摘要先清理说明/材料/草稿，再读取当前权限；不要等异步清理完成才拒绝。
- [ ] 实现分配操作幂等与目标epoch CAS；权限仓储断开时返回503/明确阻塞，不使用旧缓存放行。
- [ ] Run：`uv run pytest packages/xuanpu-server/tests/r4_contract/test_assignments.py packages/xuanpu-server/tests/r4_contract/test_assignment_epochs.py packages/xuanpu-server/tests/r4_contract/test_capability_decisions.py -q`；先看到目标失败，再实现并转绿。
- [ ] 提交`feat: 增加到人能力分配与当前资格解析`。

## B1：全入口统一鉴权和版本固定

**Files**：Create `S/capabilities/{admission,runtime_guard,legacy}.py`、`T/r4_contract/{test_capability_entrypoints,test_runtime_revocation,test_legacy_bypass}.py`；Modify `S/assistant.py`的`_catalog/_resolve_business`、`S/runtime/{sessions,assembler,task_policy,approval}.py`的公开装配接缝、`S/work_tasks/{service,capability_runner,recovery}.py`、`S/task_triggers/{service,worker}.py`、`S/packages.py`及`S/api/app.py`中的旧发布/激活路由。

- [ ] 目录从`_catalog(tenant)`改为以Principal解析，并将`_resolve_business`“必须等于全租户active_version”替换为本次服务端决策；不同员工可用不同固定发布。普通问答不强制开启业务包。
- [ ] 显式创建session的package/version/entry、续聊、普通任务、时间/事件触发、批准恢复均要求相同能力决策。R2发生实例在实际发生时解析并固定，不从旧TaskDefinition prepared偷偷使用已撤销能力。
- [ ] 覆盖旧`POST /v1/packages?activate=true`和`POST /v1/packages/{name}/activate`：已纳管包不能绕过C发布门禁；带activate=true在写归档前返回409并指向发布流程，单独activate也409；activate=false发布归档仍可用。未纳管包在兼容模式保留原行为。
- [ ] 包级managed标记永久保留，`package+entry`解析唯一capability_key；首次纳管若存在无绑定在途/会话或未暂停的旧安排则阻塞，不能自动给旧任务补分配。原unknown只读核实沿原账本处理，不重新授权发送。
- [ ] 入库受理与InvocationBinding在同一协调事务；事务外对象读取后再次复核。重启从绑定重建，不按新active指针取包；旧任务若无绑定，仅已明确未纳管的兼容任务可继续，禁止为纳管旧任务猜分配。
- [ ] 运行期用公开model client/permission policy包装复核，覆盖实际模型发送、工具闸门和批准后的实际出口；不能仅在HTTP受理检查。取消、计量、effect未知账本仍沿用R2。
- [ ] 原生“运行能力”与对话同服务，命令/API直调也不能给自己指定higher role、assignment或安装配置。
- [ ] Run：`uv run pytest packages/xuanpu-server/tests/r4_contract/test_capability_entrypoints.py packages/xuanpu-server/tests/r4_contract/test_runtime_revocation.py packages/xuanpu-server/tests/r4_contract/test_legacy_bypass.py -q`；再跑R2/R3回归。
- [ ] 提交`feat: 统一能力调用与恢复出口授权`。

HTTP最小绕过反例（`r4_ids`在B0 seed只给Alice分配review）：

```python
async def test_unassigned_user_cannot_create_explicit_version(r4_http, r4_ids):
    """知道包名和精确版本也不能绕过员工分配。"""
    response = await r4_http['bob'].post('/v1/sessions', json={
        'package': 'office-review', 'version': '1.0.0', 'entry': 'review'})
    assert response.status_code == 404


async def test_managed_package_cannot_use_legacy_activation(r4_http, r4_ids):
    """旧管理激活接口不绕过评测和发布证据。"""
    response = await r4_http['admin'].post('/v1/packages/office-review/activate',
        json={'version': '1.0.0'})
    assert response.status_code == 409
```

## B2：文件型能力的受控任务适配

**Files**：Create `S/capabilities/completion_profiles.py`、`S/work_tasks/capability_admission.py`、`T/r4_contract/test_file_capability_tasks.py`；Modify `S/work_tasks/{service,routing,runner,artifact_rules,models}.py`、`S/task_triggers/service.py`及`S/api/work_tasks.py`。

- [ ] 先写反例：当前`accept`硬编码office-assistant、`accept_capability`固定capability_receipt，office-review不应因根轮完成而假成功。新增服务端completion profile映射：本样板`outputs.contract=office_files, output_kind=table_summary`，输入仅CSV；普通只读能力仍为capability_receipt。
- [ ] profile只能来自已评测的能力修订/安装绑定，不接受客户端或模型传execution_kind、completion_policy和output_kind。受理文件型能力时服务端保存`execution_kind=office, completion_policy=office_files, output_kind=table_summary`及固定entry/digest；保留原WorkTaskRecord字段契约。
- [ ] 复用现有runner的只读输入、任务专用会话、outputs/manifest.json、四角色成果校验和预算账本，不能凭HTTP读取回执判定文件成果。profile输入只允许CSV，故不进入硬编码meeting-minutes提取脚本路径。
- [ ] 现有table_summary四类`summary_csv/summary_xlsx/report/evidence`必须齐全；缺任一失败，不弱化旧artifact_rules。原office-assistant受理及只读企微能力回执保持原语义。
- [ ] 测试原生能力手动运行与R2时间/事件发生实例走同一profile，冷恢复保持原profile/包/entry；客户端伪造profile字段422，无分配404，根轮完成但缺文件失败，正确四成果无需HTTP读取工具也成功。
- [ ] Run：`uv run pytest packages/xuanpu-server/tests/r4_contract/test_file_capability_tasks.py packages/xuanpu-server/tests/work_tasks/test_capability_tasks.py -q`；提交`feat: 接入受控文件型能力完成契约`。

## B3：原生分配、状态与冷恢复反例

**Files**：Create `C/capability-assignments.js`、`Q/{r4_assignments_behavior.cjs,test_r4_assignments_behavior.py}`、`T/r4_contract/{test_binding_faults,test_binding_views}.py`、`scripts/acceptance/r4_binding_worker.py`；Modify `C/capabilities.js`、`SDK/capabilities.py`、`CLI/capabilities.py`、实施中的能力文档。

- [ ] 默认直接选员工；只有实际存在部门/岗位才展示可选选择器，展示明确分配范围/期限，不把“可用能力”写成“可访问所有资料”。
- [ ] 实际PG子进程在受理提交前/后SIGKILL：至多一个task/session及对应binding；撤权后冷恢复拒绝，不重置预算、不迁到新grant。
- [ ] 挂起等待工具决定时升级或正常回退，旧目标仍固定；改变权限后旧工具决定不授权扩大操作。关新受理后取消/只读核实仍可完成。
- [ ] 原生两用户、不同发布及390px测试；复核身份切换、迟到响应清理和反复点击，留本机合成证据。
- [ ] Run：`uv run pytest packages/xuanpu-server/tests/r4_contract/test_binding_faults.py packages/xuanpu-server/tests/r4_contract/test_binding_views.py -q`及总门禁；提交`feat: 提供能力分配与绑定恢复记录`。

本段不将任何真实员工升级为管理员，不代替真人批准/签收；历史任务可查不等于允许重跑。
