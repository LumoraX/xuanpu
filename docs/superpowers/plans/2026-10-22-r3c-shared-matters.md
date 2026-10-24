# R3-C 共享事项、材料与权限 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans逐项执行。Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 员工只把明确事项和固定材料版本交给明确参与者；共享读写、撤回、模型上下文和通知都受当前权限约束。

**Architecture:** SharedMatter独立于个人WorkspaceItem。复用B协调事务、原对象存储和R0内容保护；显式共享副本与source dependency防止私人接口和旧模型上下文绕过撤权。不把shared_id自动变成R2个人输入。

**Tech Stack:** FastAPI/Pydantic、PG/memory、既有ObjectStore、public ModelClient包装、原生JS、SDK/CLI、Node行为测试。

前置：[B](2026-10-22-r3b-organization-authority.md)。统一错误/分页/CAS见[总计划](2026-10-22-r3-delivery.md)。

## C0：共享实体、ACL与跨域原子性

Files：Create `shared_matters/{models,store,memory,postgres,permissions,service,views}.py`、`api/shared_matters.py`、`controlplane/migrations/0017_shared_matters.sql`、`tests/r3_contract/test_shared_store.py`、`test_shared_acl.py`；Modify `coordination/`、`bootstrap.py`及公共fixture。

Schema：SharedMatter(shared_id,owner_id,title,status,version,access_epoch)、SharedMember(subject_kind=user|unit,subject_id,role=owner|editor|viewer)、SharedEvent；所有表带tenant及完整外键。共享状态active/closed/archived，关闭不等于交接签收。0017一次建立本包publication/material source_refs/source dependency/消息sources默认空值及shared notification全部结构，后续任务只启用逻辑，不回改已发布SQL。C/D扩展fixture增加alice拥有、bob有权、outsider无权的固定事项。

- [ ] 测私人matter不自动共享、管理员不能看未授权正文、跨租户/跨部门枚举、成员变更CAS、唯一owner、负责人离职与最后owner转移。
```python
async def test_shared_acl_does_not_open_private_workspace(r3_http, r3_ids):
    matter = r3_ids['shared_matter']
    assert (await r3_http['bob'].get(f'/v1/shared-matters/{matter}')).status_code == 200
    assert (await r3_http['bob'].get('/v1/workspace/items/' + r3_ids['alice_private_file'])).status_code == 404
    assert (await r3_http['outsider'].get(f'/v1/shared-matters/{matter}')).status_code == 404
```
- [ ] 建立SharedAccessGrant：user/unit+required_org_roles、purpose=direct/handoff/approval、scope=full/packet、packet_title、精确material revision集合、include_content、epoch/expiry。packet投影不返回原事项其他正文/材料/成员总数；部门来件摘要和审批材料读取只借助此通用scope，不因一个事项ID开放整个事项。
- [ ] 实现`/v1/shared-matters` CRUD、`/{id}/members`及原生转交owner；服务器解析unit当前成员和职责，client提供unit_id不等于获授权。owner转移显式确认，不自动给予新的tenant角色。
- [ ] 领域、成员、历史、请求回执及事件在同一个协调事务；不调用两名员工各自WorkspaceStore.transaction来假装原子。必要的旧文件元数据只在同一DB连接读取，不能在已持有员工锁时嵌套另一连接。
- [ ] 列表/计数/搜索/分页先过滤ACL，默认50上限100；禁止先取全量再在浏览器隐藏。无权对象404，没有员工403。
- [ ] 跑`uv run pytest packages/xuanpu-server/tests/r3_contract/test_shared_store.py packages/xuanpu-server/tests/r3_contract/test_shared_acl.py packages/xuanpu-server/tests/workspace -q`；提交`feat: 增加共享事项和显式成员权限`。

## C1：固定版本材料发布与撤回

Files：Create `shared_matters/{materials,publication,object_recovery}.py`、`tests/r3_contract/test_shared_materials.py`、`test_publication_faults.py`；Modify `api/shared_matters.py`、对象存储授权适配，不改旧私有file下载归属。

API：`POST /v1/shared-matters/{id}/materials`发布本人source_item_id/source_revision，或`POST /v1/shared-matters/{id}/material-uploads`直接上传，均检查expected_version；`GET /.../materials/{material_id}/content?revision=N`；`POST /.../{material_id}/withdraw`；新增版本必须新的原生发布确认。

- [ ] 测随意引用他人file、上传/撤权竞争、复制后提交前崩溃、原文件改版、同key异payload、对象SHA缺失和超额上传。
- [ ] 发布状态pending/ready/failed/withdrawn：协调事务保存固定引用intent→事务外流式复制/验SHA→提交前再核源版本/当前权限/epoch→原子发布。20MiB单材料上限，空间预算按真实副本计；失败intent能安全补读/回收未引用对象。
- [ ] 材料可来自明确个人版本、直接上传或经原生确认的共享文本草稿；受控生成结果保存source_refs并检查其当前权限/撤回状态。承办人的publish_result scope仅允许给本交接新建成果，不允许编辑原材料或取得全事项editor权；不借私有中间文件绕过来源约束。
- [ ] 下载每次查当前ACL、ready及revision，禁用公共永久URL；首版平台代理有界流式传输。撤回后新下载和续传请求拒绝，已开始流尽力取消，不宣称已传字节可追回。
- [ ] 原件删除和共享撤回分别操作；界面先说明共享副本独立保留。旧revision、SHA及已签收证据不可被新文件内容覆盖，历史正文仍按当前权限访问。
- [ ] 跑`uv run pytest packages/xuanpu-server/tests/r3_contract/test_shared_materials.py packages/xuanpu-server/tests/r3_contract/test_publication_faults.py -q`，实际PG/SIGKILL及真实对象读写验证；提交`feat: 固定共享材料版本并支持撤回`。

## C2：受保护的来源依赖和上下文失效

Files：Create `shared_matters/{context_guard,source_dependencies,read_tools}.py`、`runtime/source_guard.py`、`tests/r3_contract/test_shared_context.py`、`test_shared_context_recovery.py`；Modify `assistant.py`、`runtime/{content_access,sessions,models,assembler}.py`、`workspace/{models,service}.py`、`proactive/{inputs,context}.py`；Create `workspace/source_history.py`承接带来源的历史过滤，不虚构现有messages模块。

SourceDependency：tenant/user/session或workspace_message、shared_id/material_id/revision、access_epoch、recorded_at。它是当前读取的证明，不是永久可见授权。

- [ ] 写撤权后继续原对话、引擎空闲重建、重启后继续、摘要缓存、workspace history、session events、主动扫描和模型请求均拒绝旧shared内容的反例；模型替身记录实际请求次数和完整输入，不能只检查返回403。
- [ ] 主对话在有明确shared_matter/handoff上下文时由服务端提供有界当前可见投影，未选目标先澄清；共享受控read工具在返回内容前持久记录source dependency并重查epoch；历史助手消息保留sources引用，不能把共享正文复制到无来源的普通个人消息或永久私有文件。
- [ ] 每次模型请求前通过公开ModelClient.session/stream(ApiRequest)包装检查会话sources；scope/SubmissionIndex取真实身份。current ACL失效则阻塞 affected session，返回`shared_access_revoked`并引导新会话，禁止把旧上下文送往模型。失效关闭可复用引擎/缓存但保留有权限审计，读不到权限存储时fail-closed。guard先于prompt capture/审计快照和实际发送，结果保存/发布前再查；在途stream尽力取消，不宣称已送出字节可回收。已有R2预算包装继续工作。
- [ ] 正文补读、SSE、assistant history、主动扫描/摘要和冷恢复共用同一guard；修正proactive直接读store.messages的路径，不能只保护HTTP入口；R2接受shared_id或未授权shared input应400，不能悄悄复制成私有输入。共享内容进入通用后台沙盒及派生物治理不在本包交付范围。
- [ ] 跑`uv run pytest packages/xuanpu-server/tests/r3_contract/test_shared_context.py packages/xuanpu-server/tests/r3_contract/test_shared_context_recovery.py packages/xuanpu-server/tests/test_content_access.py packages/xuanpu-server/tests/test_employee_execution_isolation.py -q`；提交`feat: 阻断撤权共享内容的上下文重用`。

## C3：共享事件、通知与原生员工入口

Files：Create `shared_matters/{events,notifications}.py`、console `static/shared-matters.js`、SDK `shared_matters.py`、CLI `shared_commands.py`；Modify `reminders/postgres_notifications.py`、`workspace/notifications.py`、console导航和助手建议处理；Test `test_shared_notifications.py`、`packages/xuanpu-console/tests/shared_matter_behavior.cjs`及客户端测试。

- [ ] 测撤销成员后旧通知/SSE/导航缓存泄露、晚提交事件丢失、重复事件刷屏、身份切换迟到响应、网络重试重复发布。
- [ ] 通知使用无私有标题的来源引用与稳定semantic key，补读再查当前ACL；启用C0在0017预先建立的通知source约束，不修改已发布SQL。数量也按可见来源统计，不给第三方收件人自动发外部消息。
- [ ] 对话仅产生创建/成员变更/发布草稿，原生确认才生效；独立共享事项入口可直接管理成员、版本和撤回。显示谁可读、固定版本及“共享副本”边界，默认个人主对话不变。
- [ ] SDK/CLI支持当前身份的共享CRUD、材料版本、撤回；窄屏、键盘、无模型直接操作、重试同request_id和409刷新测试用实际DOM/请求验证。
- [ ] 跑`uv run pytest packages/xuanpu-server/tests/r3_contract/test_shared_notifications.py packages/xuanpu-console/tests packages/xuanpu-sdk-py/tests packages/xuanpu-cli/tests -q`；提交`feat: 从员工入口管理共享事项与材料`。

## C4：独立故障、隐私与兼容验收

Files：Create `scripts/acceptance/r3_shared_matters.py`、`tests/r3_contract/test_shared_fault_matrix.py`；Modify受影响架构及手册。

- [ ] 固定Alice私有输入、Bob共享权限、第三人无权、两个材料revision；分别保存输入、参与者、期望ACL、真实请求和对象SHA，不用管理员全权读取代替本人检查。
- [ ] 在发布复制后/提交前、成员撤销后/通知前、模型请求前、流式下载中、进程重启后注入故障；确认无半公开记录、权限漏出或旧内容继续送模型。
- [ ] CUA实际选择材料版本、确认成员、由Bob读取、Alice撤回、Bob刷新/继续旧对话被阻断；桌面/窄屏及无模型原生路径留证。
- [ ] 旧私有workspace及R2记录/成果/预算回归，全门禁/独立复核；关闭新共享后兼容读取与撤回可用，不回退旧R2严格reader；cleanup测试成员和未引用对象，保留共享历史。
- [ ] 提交`feat: 验收共享事项材料与撤权边界`，真实跨员工证据不足不签收C。

**C完成定义：** 共享不扩大个人空间，固定版本可追溯，撤权约束后续服务端读取和模型使用。已交付字节不宣称可追回；R2通用共享材料沙盒处理尚未承诺。


## 2026-10-24 候选实施核验

- C0/C1 已在1edbefb检查点提交。C2/C3现已实现来源依赖、逐次鉴权、受保护阅读、到人员工入口、统一来源通知及SDK/CLI；当前原生邀请优先员工，部门/职责的可选高级授权继续走正式API。
- 最新全量：`uv run pytest -q` → **2382 passed, 12 skipped, 496.36s**；`uv run ruff check --select F,S108,I,TC packages`、`uv run mypy packages/*/src`（285源文件）、`uv lock --check`、`git diff --check`通过。新增隔离进程故障用例中的2项memory分支明确跳过，真实PG分支执行。
- C4：独立PG子进程SIGKILL点为LocalObjectStore.put完成后、ready提交前；恢复唯一正确副本。独立冷解释器从PG拒绝撤权上下文；对象读取期间撤权在HTTP整包外发前拒绝，未宣称网络流开始后的中途取消。单独故障集 **4 passed, 2 skipped**。
- 当前代码的本机合成浏览器完成固定材料上传/到人资料包授权/接收人选择固定版本/独立阅读/撤权后正文清理与旧阅读拒绝。390px窄屏无横向溢出，Tab可到发送按钮。浏览器模型是脚本化替身；不计真实模型语义或真实员工生产验收。
- `scripts/acceptance/r3_shared_matters.py`在全新当前夹具实际执行完成，共享材料SHA、限定权限、模型替身答复、撤权、旧上下文、结束阅读和归档均核对；明确标synthetic。浏览器夹具与故障worker在scripts/acceptance，真实员工脚本只接受私有浏览器会话，不自动登录或同意。
- 独立复核发现并修复多packet正文、授权集合替换确认、任职/职责重授旧上下文、无通知的降权提示、普通历史混入、403后迟到回填和共享草稿恢复；修复复核未发现新增P1/P2。
- 尚未构建/部署候选，生产仍是R3-A。真实陈翔/drYang、真实模型、生产迁移/镜像/回退及清理由R3-E分别验收；不将这些剩余条件勾选为完成。通用R2共享沙盒及真实多部门试点不在当前承诺内。
