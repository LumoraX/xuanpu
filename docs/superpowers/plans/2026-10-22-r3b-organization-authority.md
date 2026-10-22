# R3-B 组织岗位与来源权威 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans逐项执行。Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 玄圃拥有稳定组织岗位和业务职责，外部目录仅经显式来源规则进入，调岗/撤权不会残留隐式授权。

**Architecture:** 新组织域与跨员工CoordinationStore共用既有控制面连接池；外部采集、差异预览、应用三阶段分离，local优先字段不能被下一次供应商登录覆盖。身份角色、组织管理、承办和审批职责分开。

**Tech Stack:** 现有identity/PG/memory、asyncio、Pydantic、统一审计/outbox、原生JS、SDK/CLI。

前置：[A的可信身份与只读目录](2026-10-22-r3a-connections-wecom.md)、[R3设计](../specs/2026-10-22-r3-enterprise-collaboration-design.md)。可先写纯领域测试，但无真实目录范围证据不能签收组织同步。

## B0：协调事务和组织实体

Files：Create `coordination/{models,store,memory,postgres,receipts,outbox}.py`、`organization/{models,store,memory,postgres,service}.py`、`controlplane/migrations/0016_organization_authority.sql`、`tests/r3_contract/test_coordination.py`、`test_organization_store.py`；Modify `controlplane/store.py`、`bootstrap.py`、`platform.py`。

实体：OrgUnit(id,parent_id,name,status,version)、Position(id,unit_id,name)、Membership(user_id,unit_id,position_id,valid_from/until,status)、RoleAssignment(actor,scope,role,epoch)、FieldAuthority(object_type,field,source)、SourceMapping(source,external_id,local_id)、SyncPreview(id,source_revision,complete,status)。全部含tenant和审计时间。

- [ ] 在memory/PG写角色与同名部门冲突、父子循环、跨租户外键、异常回滚和两员工相反锁序并发反例。r3_ids新增department、position和两名部门成员，由服务端seed生成。
```python
async def test_cyclic_org_change_rolls_back(r3_http, r3_ids):
    root, child = r3_ids['department'], r3_ids['child_department']
    response = await r3_http['manager'].patch(f'/v1/admin/organization/units/{root}', json={
        'request_id': 'cycle-1', 'expected_version': 1, 'parent_id': child})
    assert response.status_code == 409
    current = await r3_http['manager'].get(f'/v1/admin/organization/units/{root}')
    assert current.json()['parent_id'] is None
```
- [ ] CoordinationStore短事务固定租户协调锁→参与员工按ID排序→领域实体→outbox；一个PG connection提交领域、回执、事件，memory深拷贝同样回滚。组织字段枚举和结构上限：层级≤16、每次变更≤100成员、每页≤100；大导入拆成显式批次，不假装全量原子。
- [ ] organization的memory/PG仓储接收协调事务的工作单元；PG使用同一connection、memory使用同一待提交快照，不能另开事务或独立提交组织变更。
- [ ] identity停用原事务先提交，再执行协调域收尾；不在旧WorkspaceStore事务中嵌套协调事务，不在DB锁内调用HTTP/模型。增加超时、反向并发与取消回滚测试，不能靠全局进程dict实现正确性。
- [ ] 角色限定org_manager/unit_operator/unit_approver；组织管理与tenant_admin分离，管理员默认只有组织配置和数量，无个人正文。指定scope必须是当前用户被授权的组织范围。
- [ ] 跑`uv run pytest packages/xuanpu-server/tests/r3_contract/test_coordination.py packages/xuanpu-server/tests/r3_contract/test_organization_store.py -q`；补ADR草案说明复用PG而不引入通用工作流引擎，提交`feat: 建立组织实体与跨员工协调事务`。

## B1：组织API、职责与来源配置

Files：Create `api/organization.py`、`organization/{permissions,authority,views}.py`、`tests/r3_contract/test_org_permissions.py`、`test_field_authority.py`；Modify `identity/{login,service,users}.py`及A的WeCom适配；新增console组织配置页与SDK/CLI对应接口。

API：管理员`/v1/admin/organization/units|positions|memberships|roles|authority`，成员`/v1/organization/me|directory`。写入使用总计划CAS/幂等；被授权org_manager不等于租户管理员，接口按具体动作检查scope，不仅检查URL前缀。

- [ ] 先测普通成员不能授角色、部门经理不能改其他部门、供应商字段不能授admin、本地名称不会被Feishu/WeCom登录刷新覆盖。
- [ ] 实现local或指定installation的字段权威，不支持同字段无条件双向覆盖；切换来源要预览影响并显式CAS确认，保留旧映射与历史，不复用已停用部门ID。
- [ ] 名称/组织成员与审批职责分开。部门目录只返回当前策略可见的最小资料，手机号/邮箱不随角色继承；组织统计独立分页且不含私有事项标题。
- [ ] 岗位变更递增相关授权epoch并写治理事件，历史责任人保持真实记录；不得把未决业务自动指派给新部门负责人。
- [ ] 跑`uv run pytest packages/xuanpu-server/tests/r3_contract/test_org_permissions.py packages/xuanpu-server/tests/r3_contract/test_field_authority.py packages/xuanpu-server/tests/identity -q`；提交`feat: 按组织职责管理字段权威与授权`。

## B2：企业微信只读目录预览与确认应用

Files：Create `organization/{sync,previews,mapping,provider_snapshots}.py`、`tests/r3_contract/test_org_sync.py`、`test_org_sync_faults.py`；Modify `connections/providers/wecom.py`及组织控制台。

API：`POST /v1/admin/organization/import-previews`引用installation；`GET /.../{id}`返回scope、完整性、分页错误和增改冲突；`POST /.../{id}/apply`提交expected_version及明确selected_changes，禁止默认全量应用。

- [ ] 测分页断线、同名不同ID、外部范围缩小、成员缺失、重复apply、生成preview后源变化、主管外字段冲突及中途崩溃。不得直接把供应商404/空数组当离职。
```python
async def test_partial_directory_cannot_disable_missing_users(r3_http, r3_ids):
    preview = await r3_http['manager'].get('/v1/admin/organization/import-previews/' + r3_ids['partial_preview'])
    assert preview.json()['complete'] is False
    response = await r3_http['manager'].post('/v1/admin/organization/import-previews/' + r3_ids['partial_preview'] + '/apply', json={
        'request_id': 'partial-apply', 'expected_version': preview.json()['version'],
        'selected_changes': [{'change_id': r3_ids['missing_user_change'], 'action': 'disable'}]})
    assert response.status_code == 409
```
- [ ] 在事务外分页采集，记录provider scope/采集起止/各页摘要；source完整性为partial时只能提出明确实际观察到的增改，不得推导删除、停用或缩权。
- [ ] apply先核source_revision、源摘要、本地version、字段权威，再在协调事务分批落地映射+数据+回执+事件；每批固定selected_changes，失败批重试不重复，已成功批不宣称全局回滚。
- [ ] 显式权威停用接既有identity流水线；来源离职状态不明确时仅标unverified，由人工核定。此阶段不向企微修改成员/部门。
- [ ] 跑`uv run pytest packages/xuanpu-server/tests/r3_contract/test_org_sync.py packages/xuanpu-server/tests/r3_contract/test_org_sync_faults.py -q`，真实目录只读采集/预览与本地应用分别验；提交`feat: 预览并确认企业微信组织差异`。

## B3：调岗、停用、职责失效与独立验收

Files：Create `organization/{lifecycle,events}.py`、`tests/r3_contract/test_org_revocation.py`、`test_org_lifecycle_races.py`；Modify `identity/disable.py`、`bootstrap.py`、`task_triggers/worker.py`及受影响架构/手册。

- [ ] 先测岗位撤销与认领/审批同刻、停用与source sync竞争、角色回调重复/乱序、服务重启后的漏收尾，以及最后管理员保护。
- [ ] 权限读取以当前membership/epoch为准；持久治理事件负责取消和重分配提醒，不把进程内listener当权限本身。旧批准/工作责任历史不改写；待处理且无人有资格的工作进入明确blocked。
- [ ] 预留C/D消费者的稳定事件：membership_changed、role_revoked、user_disabled，带server provenance、entity_version、稳定event_id；逐条ack，不能按最大序号跳过晚提交事件。
- [ ] 真实企微目录→预览→本地确认→岗位撤销/重启再读全链路；归档原数据数量和mapping，外部写0；关闭organization新写后兼容目录/必要取消可用。
- [ ] 全门禁、双后端契约、身份回归、独立复核、实际CalVer/迁移和源码核对；提交`feat: 完成组织职责失效与目录同步验收`。

**B完成定义：** 组织权威在玄圃，来源可替换；部分同步不批量停用，职责与私人正文隔离，撤权不靠后台清理才生效。C/D尚未完成时不能称为企业协同整体交付。
