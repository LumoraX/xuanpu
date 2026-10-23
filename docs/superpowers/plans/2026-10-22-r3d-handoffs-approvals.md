# R3-D 部门受理、交接与组织审批 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans逐项执行。Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 事项交给部门后有唯一真人负责人，能退回/补充/处理/交付，按冻结规则取得组织决定，再由发起人签收成果。

**Architecture:** 新Handoff和ApprovalRequest状态机复用B的协调事务、C的共享材料与统一outbox。部门助理是有真人责任人的受理视图，模型给建议而原生接口决定；不伪造部门usr、不引入无人服务主体或第二模型worker。

**Tech Stack:** PG/memory、严格Pydantic输入、现有通知、R2任务引用、原生JS、SDK/CLI、pytest/CUA。

前置：[B](2026-10-22-r3b-organization-authority.md)+[C](2026-10-22-r3c-shared-matters.md)。统一身份/CAS/幂等和测试fixture见[总计划](2026-10-22-r3-delivery.md)。

## 2026-10-23 到人优先补充

用户明确当前无业务部门，采用[到人优先设计](../specs/2026-10-23-r3-person-first-amendment.md)。以下部门规则继续作为可选分支；当前试点不要求先建部门或临时授予部门职责。

- 交接目标改为互斥的target_kind=user|unit及target_id，固定保存目标员工身份代次。user目标只有本人可查看来件摘要并接收；unit目标仍查当前unit_operator。列表增加view=received，按当前员工过滤，客户端不能另传user覆盖身份。
- 到人submit必须由共享事项owner原生确认目标与精确资料包；接收后只授予该交接的packet读取及publish_result，不提升完整事项editor或租户角色。
- 到人转交由当前事项owner明确选择另一名启用员工并记录原因。旧承办人的当前授权撤销，新承办人重新接收；原责任、交付和决定历史保留。停用或失去权限只阻塞，不自动猜下一人。
- 审批策略步骤支持selector=user（稳定员工标识及授予时身份代次）或selector=unit_role（原部门解析规则）。策略仍由有权管理员预配；申请接口只引用policy_id，不信任客户端另传审批人集合。
- 创建审批冻结策略revision和解析后的1–3名当前启用员工；每一步即时复核身份代次，禁止申请人自批，用户步骤不要求部门任职。身份代次改变的未决步骤blocked，必须明确重配后新建请求。
- 当前真实试点为陈翔发起、drYang承办；drYang提交成果审批申请，陈翔按明确配置的策略审批，随后由承办人交付、陈翔另行签收。审批决定与签收分别记录，不能合并为一次确认。
- 两名真实员工验证到人闭环；部门竞争、四人、两部门分支继续由自动化反例覆盖，真实多部门试点未执行，不标已通过。

## D0：冻结交接与审批的两个契约

Files：Create `handoffs/models.py`、`approvals/models.py`、`tests/r3_contract/test_handoff_contract.py`、`test_approval_contract.py`；Review既有`runtime/approval.py`、`work_tasks/decisions.py`，不借用其个人工具批准当组织批准。

Handoff必存：id、tenant、requester、shared_matter/version、request_type、目标部门、固定材料revision/摘要、due_at、status/version、assignee、blocked_reason、result_refs和责任历史。业务状态见下表；blocked_reason独立，不把无人有资格等同成功/取消。

| 原状态 | 动作→新状态 | 有权者/条件 |
| --- | --- | --- |
| 无 | submit→submitted | 首版发起人必须是共享事项owner，目标部门可受理；原生确认材料范围 |
| submitted | accept→accepted，return→returned | 目标部门当前unit_operator，accept原子认领本人 |
| accepted | start→in_progress | 唯一承办人，资格及材料可见性仍有效 |
| accepted/in_progress | request_information→needs_information | 承办人明确说明缺项，不擅自改材料 |
| needs_information | provide_information→submitted | 发起人发布/引用新固定版本并确认重新提交；承办记录保留，需重新认领 |
| in_progress | deliver→delivered | 当前承办人、固定成果ready、所需组织审批有效 |
| delivered | sign_off→signed_off，dispute→disputed | 发起人或提交时明确指定的签收人，确认精确成果revision |
| disputed | reopen→in_progress | 当前承办人明确重办；更新成果后重新交付/签收 |
| submitted | cancel→cancelled | 原发起人，尚无人认领 |
| accepted/in_progress/needs_information | request_cancel→原状态+cancel_requested，confirm_cancel→cancelled | 发起人请求；承办人或有权部门负责人确认，在此期间禁止新交付 |

returned/cancelled/signed_off为终态；returned后重做新handoff引用原单，不复活旧决定。delivered后不提供抹除交付历史的cancel，需dispute。转交是显式transfer动作，部门负责人确认新承办人，原责任历史保留；不静默替换离职员工。

审批独立状态pending/approved/rejected/returned/withdrawn/blocked/superseded；首版1–3步顺序单人，禁止自批。请求固定target digest、policy revision和步骤，只有当前step可决定。退回/驳回后重提创建新请求；不复用旧step回执给新输入。

- [ ] 写每个合法/非法状态转移、无资格动作、同请求重放和不同payload冲突测试；通过参数矩阵覆盖表中全部边。
- [ ] 冻结所有动作的角色、前置version、材料变更及取消规则，确定组织批准与发起人签收分别返回的receipt形状。
- [ ] r3_ids增加handoff、approval、material及两个unit_operator；r3_http新增operator_two客户端；状态fixture必须通过服务创建，不直接改record假装集成。
- [ ] 跑`uv run pytest packages/xuanpu-server/tests/r3_contract/test_handoff_contract.py packages/xuanpu-server/tests/r3_contract/test_approval_contract.py -q`；提交`test: 固定部门交接和组织审批契约`。

## D1：交接存储、唯一认领和材料绑定

Files：Create `handoffs/{store,memory,postgres,service,transitions,views}.py`、`api/handoffs.py`、`controlplane/migrations/0018_handoffs.sql`、`tests/r3_contract/test_handoff_claim.py`、`test_handoff_atomicity.py`。

API：`POST/GET /v1/handoffs`、`GET /{id}`、`POST /{id}/accept|return|start|request-information|provide-information|request-cancel|confirm-cancel|transfer`。列表通过view=sent|department_queue|assigned解释，不接受任意user作为身份覆盖。

- [ ] 写两个接收人并发认领、源共享撤权与认领竞争、packet包含A却泄露同事项B、材料篡改、重复submit、提交后断线、跨租户目标及异部门处理反例。
```python
async def test_exactly_one_department_operator_claims(r3_http, r3_ids):
    import asyncio
    path = '/v1/handoffs/' + r3_ids['handoff'] + '/accept'
    replies = await asyncio.gather(
        r3_http['bob'].post(path, json={'request_id': 'claim-bob', 'expected_version': 1}),
        r3_http['operator_two'].post(path, json={'request_id': 'claim-second', 'expected_version': 1}))
    assert sorted(r.status_code for r in replies) == [200, 409]
    record = (await r3_http['alice'].get('/v1/handoffs/' + r3_ids['handoff'])).json()
    assert record['assignee_id'] in {r3_ids['bob'], r3_ids['operator_two']}
```
- [ ] 协调事务内完成目标范围/共享ACL/材料版本复核、唯一assignee CAS、双方可见事件及幂等回执；通知outbox同事务，失败全回滚。0018预先包含全部handoff通知source约束，之后不可修改已发布SQL。
- [ ] submit原生预览包含范围差异：目标部门有权值守人只见packet摘要；accept同事务给当前承办人精确packet材料的内容权限。复用C的SharedAccessGrant，不给全部门成员整个事项的权限，也不开放同事项未选材料。
- [ ] 目标队列只展示明确授予的事项摘要，缺材料时不能偷读发起人私有文件；重复accept同key返回同assignee，不创建另一R2工作任务。
- [ ] 转交/补充使目标fingerprint变动时，未决审批标superseded，未来决定不能按旧输入执行；已发生的决定留历史。
- [ ] 跑`uv run pytest packages/xuanpu-server/tests/r3_contract/test_handoff_claim.py packages/xuanpu-server/tests/r3_contract/test_handoff_atomicity.py -q`；提交`feat: 按部门职责受理并唯一认领交接`。

## D2：一到三步组织审批

Files：Create `approvals/{store,memory,postgres,policies,resolver,service,decisions,views}.py`、`api/organizational_approvals.py`、`controlplane/migrations/0019_organizational_approvals.sql`、`tests/r3_contract/test_approval_policy.py`、`test_approval_decision.py`、`test_approval_races.py`。

API：管理`/v1/admin/approval-policies`，员工`/v1/approval-requests`及`/{id}/approve|reject|return|withdraw`。提交参数只有policy_id、target_type=handoff、target_id/version及固定材料；服务器解析审批人，不信客户端给出的approver集合。

- [ ] 测自批、第二步抢先批准、审批人与申请人同一人、岗位多人且无唯一匹配、审批中离职、重复点击、规则改版、输入变化以及另一租户target。
```python
async def test_new_material_cannot_use_old_approval(r3_http, r3_ids):
    response = await r3_http['approver'].post('/v1/approval-requests/' + r3_ids['stale_approval'] + '/approve', json={
        'request_id': 'approve-old-material', 'expected_version': 1})
    assert response.status_code == 409
    handoff = (await r3_http['alice'].get('/v1/handoffs/' + r3_ids['handoff'])).json()
    assert handoff['status'] != 'signed_off'
```
- [ ] 创建审批时只为当前step实际审批人授予approval用途的固定packet材料读取，不依靠部门角色直接获得原事项正文；决定或撤销后移除该临时scope，历史receipt可见性仍经当前内容权限过滤。
- [ ] 每步明确选一个有权人；岗位解析不唯一则要求有权管理员修正规则/指定允许的人后创建新请求，禁止模型或申请人猜一个人。默认申请人不能自批，不能因无审批人自动通过。
- [ ] 决定与当前人员资格、target版本、材料摘要、policy revision、请求version同事务校验；回执与通知同提交。未决审批职责失效进入blocked；已合法完成的历史决定不因日后离职被改写，但不能授权新目标/过期目标。
- [ ] 工具确认、组织批准和成果签收用不同API/receipt，不生成外部写授权、不调用企微审批写接口；0019一次创建审批通知source约束。
- [ ] 跑`uv run pytest packages/xuanpu-server/tests/r3_contract/test_approval_policy.py packages/xuanpu-server/tests/r3_contract/test_approval_decision.py packages/xuanpu-server/tests/r3_contract/test_approval_races.py -q`；提交`feat: 按固定材料执行顺序组织审批`。

## D3：成果交付、签收与争议重办

Files：Create `handoffs/{delivery,signoff,result_checks}.py`、`tests/r3_contract/test_delivery_signoff.py`、`test_handoff_revisions.py`；Modify共享材料API、审批target版本核对及handoffs路由。

API：`POST /v1/handoffs/{id}/deliver|sign-off|dispute|reopen`。deliver引用共享ready成果的material_id/revision/sha256，sign-off引用delivery_version，不允许自由文本“已完成”代替明确成果。

- [ ] 测无结果交付、错误SHA、审批未过、成果更新后旧签收、承办人代发起人签收、重复签收、两种决定并发和签收事务提交后断线。
- [ ] 成果先通过C的直接上传/共享文本草稿确认路径发布ready，不强制把共享正文复制到承办人私有空间。source_refs保持，publication权限只覆盖本handoff新成果，不允许修改发起材料。
- [ ] 同事务冻结delivery及成果revision、检查审批资格与target digest、写双方receipt；上传/复制在C的publication流程完成，deliver事务不进行大对象I/O。
- [ ] 发起人能查看固定成果后原生签收；dispute保留原交付、不删除历史。reopen明确记录重办责任和新结果，不沿用变更目标的旧审批。
- [ ] R2 task成功只能作为可引用的结果证据，不能直接驱动signed_off；部门模型输出也不能完成交付/签收。关闭handoffs新受理时旧结果仍可查看/签收/提出争议。
- [ ] 跑`uv run pytest packages/xuanpu-server/tests/r3_contract/test_delivery_signoff.py packages/xuanpu-server/tests/r3_contract/test_handoff_revisions.py -q`；提交`feat: 核对固定成果后完成交付签收`。

## D4：部门助理视图、动态、期限与失效传播

Files：Create `handoffs/{assistant,events,deadlines,lifecycle}.py`、`approvals/{events,lifecycle}.py`、`tests/r3_contract/test_department_assistant.py`、`test_handoff_notifications.py`、`test_handoff_lifecycle.py`；Modify `assistant.py`、统一通知及B治理事件消费。

- [ ] 测模型伪造action/actor、非值守成员看队列、shared撤回、承办人停用、未决审批人离职、并行deadline扫描、outbox晚提交、重启重复提醒。
- [ ] 部门助理在当前登录的有权真人上下文读取共享事实、起草回应；没有独立部门登录cookie、用户token或冒充usr。建议只在本人原生确认后提交；普通员工仍以对话为默认入口，部门队列为直接兜底。
- [ ] 期限只产生确定性站内动态，不自动批准/认领/交付；复用现有有界扫描和协调outbox，单批100、无变化不通知，不为部门另开模型worker。
- [ ] B治理事件触发待重新安排、pending approval blocked、受影响上下文失效；接口始终即时验证当前角色/ACL。部门经理显式处理转交，无人有资格时保留blocked及原历史。
- [ ] 跑`uv run pytest packages/xuanpu-server/tests/r3_contract/test_department_assistant.py packages/xuanpu-server/tests/r3_contract/test_handoff_notifications.py packages/xuanpu-server/tests/r3_contract/test_handoff_lifecycle.py -q`；提交`feat: 提供部门受理助理与可靠工作动态`。

## D5：原生多员工闭环、客户端与故障验收

Files：Create console `static/{handoffs,organizational-approvals}.js`、SDK `{handoffs,organizational_approvals}.py`、CLI `collaboration_commands.py`、Node行为测试、`scripts/acceptance/r3_collaboration.py`及`tests/r3_contract/test_collaboration_faults.py`；Modify导航、公共导出和架构/手册。

- [ ] 原生入口覆盖提交、接受、退回、补充、转交、审批、交付、签收、争议；动作按钮来自服务端allowed_actions并在服务端再次校验。断网保持request_id，409刷新，身份切换代际保护。
- [ ] 模型不可用仍可完成全链路；真实模型只能产生有出处的摘要/候选，不能凭文字更改状态。SDK/CLI按本人调用，组织管理员与租户管理员权限不混同。
- [ ] 四名员工/两部门实际验证：发起人、承办人、竞争承办人/无权人、审批人。CUA桌面/窄屏及键盘完成一条闭环；API另验return、needs_information、dispute、离职block全部分支。
- [ ] 真实PG在claim提交后、通知前、approval提交后、delivery提交后SIGKILL；同请求重放只有1assignee/1decision/1sign-off，源outbox回滚不发通知，较小晚提交event不丢。保留输入SHA、对象数、外部写0和完整失败轮次。
- [ ] 全门禁、独立复核、R2回归、当前安装版本/源码核对；提交`feat: 验收部门交接与组织审批闭环`，部署在E实施，不能用源码通过冒充真实部门生产验收。

**D完成定义：** 真人责任、固定材料、组织决定和发起人签收都可追溯；部门助理能协助处理但不拥有无人自动执行的服务授权。真实多员工闭环未验时D保持partial。
