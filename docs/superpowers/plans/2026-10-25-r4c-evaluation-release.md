# R4-C 评测、灰度发布与回退实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans，A/B通过后实施发布路径。

**Goal:** 每次发布引用精确版本和可检查评测证据，支持按员工灰度、升级、停止新受理与未来版本回退。

**Architecture:** 复用已有EvalRunner的正常运行路径与R2任务执行，新增持久评测记录及发布服务。隔离评测与正式配置分别固定，模型评分不代替确定性检查与业务决定。路径/夹具见[总计划](2026-10-25-r4-delivery.md)。

**Tech Stack:** 现有EvalRunner、PG租约、对象制品、固定JSON测试套件、原生管理确认；不采用第二套调度队列。

---

## 评测证据契约

评测记录保存`run_id/capability_revision/package_digest/deployment_config_digest/test_config_digest/substitution_policy_digest/suite_digest/tool_inventory_digest/model_fingerprint/runtime_fingerprint/budget/status`。模型指纹仅含非秘密协议/模型名/路由配置修订，不输出key。归档业务原始报告和独立判定分开，按评测数据ACL读取。

隔离环境不能使用生产凭据或外部写地址。允许的替换只有明确列举的连接地址、秘密引用和测试身份，每项必须绑定同一工具/schema/effect与输入输出契约。报告同时固定“拟部署配置”和“实际测试配置”以及管理员核对的替换策略；测试配置任意其他差异、部署修订变更、工具集变化都要求重新评测。替身ERP/企微结果只能签收接口契约，不是生产供应商验收。

首个办公样板无外部写连接，fixture替换只涉及合成身份、输入/输出命名空间；真实模型调用可独立验收。外部实现无可验证版本或未做真实通道功能验收时，报告显式标`external_implementation_not_verified`；若能力声明要求该通道，则不得发布ready。

发布门禁：必需用例全部实际执行；安全反例、固定数据/成果断言100%通过；声明requires_real_model的套件还需实际模型报告与人工语义判定。缺少模型/预算则candidate保持blocked，不用替身补pass。报告有效期默认7天，平台/沙盒策略指纹变化后重新运行；有效期过后阻止新发布，不自动删除已发生事实。

拟新增接口：POST `/v1/capability-evaluations`持久受理，GET `/v1/capability-evaluations/{id}`查询；POST `/v1/capabilities/{id}/release-previews`固定目标employee/epoch最多100人及旧发布CAS；POST `/v1/capabilities/{id}/releases`明确确认发布；POST `/v1/capability-releases/{id}/retire|revoke`区分正常停止与安全撤销。所有写入沿用request_id及expected_version；candidate不暴露给运行目录。

## C0：有界隔离评测及持久报告

**Files**：Create `S/capabilities/evaluation/{models,suites,service,worker,checks,artifacts,config_binding}.py`、`S/api/capability_evaluations.py`、`S/controlplane/migrations/0022_capability_releases.sql`、`T/r4_contract/{test_evaluation_isolation,test_evaluation_contract,test_evaluation_binding}.py`；Modify `S/evals.py`以可复用方式抽取已有检查，不改其旧响应契约；Modify `S/platform.py`和能力仓储。

- [ ] 套件严格JSON只允许固定contains/notContains、JSON字段相等、CSV行数/金额等预定义检查，无eval/Python插件；工具确认、权限拒绝和无外部写次数由权威轨迹判断，不听模型自报。
- [ ] 每个用例固定身份/输入，隔离任务/对象命名空间；默认并发1、每run总token100000、最长10分钟、最多50用例。创建前显式预算，超限返回blocked/cancelled，不截断成通过。
- [ ] worker使用现有PG持久租约与执行服务，关闭等待在途清理。进程中断的评测标interrupted；不自动重放可能有外部效应的用例。可用新操作号显式重跑，保留原失败轮次。
- [ ] 写回case状态、原始模型输出对象引用与独立判定；列表不包含测试正文、工具参数、密钥或非授权员工资料。
- [ ] Run：`uv run pytest packages/xuanpu-server/tests/r4_contract/test_evaluation_isolation.py packages/xuanpu-server/tests/r4_contract/test_evaluation_contract.py packages/xuanpu-server/tests/r4_contract/test_evaluation_binding.py -q`；至少验证试图访问生产目标被拒、配置替换不符和空套件不得pass。
- [ ] 提交`feat: 保存隔离能力评测与配置证据`。

## C1：灰度、升级、权限差异与回退

**Files**：Create `S/capabilities/{releases,release_previews,release_checks,release_events}.py`、`S/api/capability_releases.py`、`T/r4_contract/{test_release_gates,test_release_cas,test_release_rollback,test_release_scopes}.py`；Modify B的`resolver/legacy/bindings`及A安装状态接口。

- [ ] staged安装只允许隔离评测；有效发布事务确立ready及active。发布前核对ready前提、报告摘要及有效期、当前身份/分配、工具集合、目标人数与当前发布，避免“必须先ready才能评测”的循环。任何新增权限必须单独安装预检确认，并与同一修订评测绑定。
- [ ] 首次纳管预览整个包受影响入口、无绑定在途任务/会话及旧后台安排；要求先排空/暂停并重新预检。提交时原子写managed标记、安装ready及release，不接受自动补授权或只在前端挡住旧入口。
- [ ] 发布预览列出每个员工当前→目标版本、权限差异、未完成任务数及其固定版本；用户必须确认精确名单。实际发布锁定该能力发布epoch，事务内检查目标epoch和CAS，原子写事件/回执。
- [ ] 同时为不同员工发布不同版本可行；同员工同能力若匹配两个有效release，拒绝409而不是选择最高版本。全量只是逐个明确扩展固定员工清单，不把空名单解释为全企业。
- [ ] 回退作为新发布动作引用旧的已验证能力/安装快照，仍需当前前提和证据；旧任务继续原包/原配置。不能修改旧任务、旧批准、effect、任务预算、原input版本或财务领域状态。
- [ ] 安全revoke递增epoch，后续模型与工具出口拒绝；普通retire/draining按B矩阵保留旧任务合法继续。策略/存储故障时关新受理，不直接启动旧R3 reader。
- [ ] Run：`uv run pytest packages/xuanpu-server/tests/r4_contract/test_release_gates.py packages/xuanpu-server/tests/r4_contract/test_release_cas.py packages/xuanpu-server/tests/r4_contract/test_release_rollback.py packages/xuanpu-server/tests/r4_contract/test_release_scopes.py -q`；先用下方反例证明门禁生效。
- [ ] 提交`feat: 增加证据绑定的员工灰度与版本回退`。

```python
async def test_release_requires_matching_evaluation(r4_http, r4_ids):
    """另一配置的通过报告不能授权当前发布。"""
    response = await r4_http['admin'].post(
        f"/v1/capabilities/{r4_ids['capability_id']}/release-previews", json={
            'request_id': 'wrong-report', 'installation_id': r4_ids['installation_id'],
            'evaluation_run_id': r4_ids['different_config_run_id'],
            'targets': [{'user_id': r4_ids['alice_id'], 'user_epoch': 1}]})
    assert response.status_code == 409
```

`different_config_run_id`由C0 seed创建为同包、不同部署配置digest的成功报告；测试不能通过直接伪造数据库生产报告来发正式能力。

## C2：原生操作、故障与SDK/CLI

**Files**：Create `C/capability-releases.js`、`Q/{r4_releases_behavior.cjs,test_r4_releases_behavior.py}`、`T/r4_contract/test_release_faults.py`、`scripts/acceptance/r4_release_worker.py`；Modify `C/capabilities.js`、`SDK/capabilities.py`、`CLI/capabilities.py`和实施中的能力手册。

- [ ] 原生可发起评测、查看缺口、下载当前有权的报告、确认灰度、查看版本差异、停止新受理、安全撤销及回退；普通员工不填模型/密钥/包路径。
- [ ] 模型不可用仍能查报告、配置、停止新受理、取消和回退到满足当前证据的旧版；需真实模型的新版本保持blocked。
- [ ] PG进程故障覆盖发布提交前/后、评测制品写完未登记、数据库成功响应丢失；重放一个发布事实，不删除被其他记录引用的制品。
- [ ] 并发批准旧任务/发布新版/撤权，按冻结绑定与当前资格判定；有未核实外部操作不产生新发送。
- [ ] Run：`uv run pytest packages/xuanpu-server/tests/r4_contract/test_release_faults.py -q`，全量门禁和桌面/390px原生复核；提交`feat: 提供评测发布回退原生管理`。

本段只能说明指定能力及配置在对应层通过；不表示所有业务包、所有远端实现或真实员工已经通过。
