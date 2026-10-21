# 玄圃 R0 基础加固 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Use superpowers:subagent-driven-development only when the user explicitly selects delegated execution. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 在 R1 开放员工执行能力前，建立可核验的版本基线、内容隐私、审批幂等、取消语义和单实例运行约束。

**Architecture:** 管理元数据和个人内容分别授权，审批提交以持久回执认领，结果未知时先核对再执行。沿用现有会话、审计、身份和迁移机制；取消与部分审批等内核语义以实际安装版本的契约回归为依据，缺口回内核补，不访问私有对象。

**Tech Stack:** Python 3.12、FastAPI、asyncpg、PostgreSQL、pytest、Ruff、mypy、taifeng 稳定 API。

## 实施记录（2026-10-21）

R0 七项任务已实施并完成本地候选验收，详见[分层验收回执](../../guides/releases/2026-10-21-r0-acceptance.md)。
勾选表示对应行为和门禁已完成；原建议的逐任务提交合并为规划基线与完整加固交付两个提交，避免跨模块接线的中间状态不完整。
泰逢修复在独立内核工作树完成，发布稳定版本后更新平台锁文件，没有依赖相邻源码覆盖安装。
回执表最终不保存任意 result JSON，以减少内容落入管理元数据；实际 DDL 以0009迁移为准。
未知外部结果不会自动重发；批准后再次挂起路径的自动补结果和恢复保持关闭，并在验收回执中列明。
浏览器使用受控测试身份，真实模型使用临时 PostgreSQL；不代表真实飞书登录或生产部署验收。

## 范围与基线

上位规划及当前提交见[总索引](2026-10-20-r0-r1-delivery.md)。首期生产候选采用单个服务进程、PostgreSQL 控制面、生产隔离后端；多个 ASGI worker 也算多进程，不在 R0 支持范围。macOS 本机隔离只用于开发。

R0 不加入岗位组织、外部员工授权、跨部门流程或通用后台执行。保留已有主动业务功能，并把它纳入回归范围。

路径规则：源码短路径以 `packages/xuanpu-server/src/xuanpu_server/` 为根，`tests/` 以 `packages/xuanpu-server/` 为根；`scripts/`、`docs/`、`docker/` 以仓库根为根。

## Task 1 记录真实运行依赖并建立契约清单

**Files:** Create `scripts/runtime_inventory.py`, `packages/xuanpu-server/tests/test_runtime_inventory.py`, `docs/guides/releases/2026-10-21-r0-baseline.md`; inspect `uv.lock`, `packages/xuanpu-server/pyproject.toml`, `bootstrap.py`, `docker/compose.yml`。

- [x] **Step 1:** 用以下完整测试定义 inventory 的最低要求；`scripts/` 不进入包导入路径，使用 `runpy` 加载。

```python
from pathlib import Path
import runpy

def test_inventory_exposes_versions_and_public_seams() -> None:
    script = Path(__file__).resolve().parents[3] / "scripts/runtime_inventory.py"
    inventory = runpy.run_path(str(script))["collect"]()
    assert inventory["packages"]["taifeng"]["version"]
    assert inventory["public_seams"]["CommandExecutor"]
    assert inventory["public_seams"]["WorkspaceFS"]
    assert "api_key" not in inventory
    assert "environment" not in inventory
```

- [x] **Step 2:** 运行 `uv run pytest packages/xuanpu-server/tests/test_runtime_inventory.py -q`，确认因文件缺失失败。
- [x] **Step 3:** 实现以下完整的采集主体，脚本通过 `__main__` 将结果写到 stdout。调用方用重定向保存到私有验收目录，不扫描或输出环境变量、配置或凭据。

```python
from importlib import metadata, util
import json
import platform
import taifeng

def collect() -> dict[str, object]:
    packages = {}
    for name in ("taifeng", "taifeng-sandbox", "taifeng-stores", "xuanpu-server"):
        module = name.replace("-", "_")
        spec = util.find_spec(module)
        packages[name] = {
            "version": metadata.version(name),
            "origin": None if spec is None else spec.origin,
        }
    seams = ("CommandExecutor", "WorkspaceFS", "LocalWorkspaceFS", "EnginePool",
             "make_shell_exec_tool", "make_file_read_tool", "make_file_write_tool")
    public = set(taifeng.__all__)
    return {"python": platform.python_version(), "packages": packages,
            "public_seams": {name: name in public for name in seams}}

if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False, indent=2))
```

- [x] **Step 4:** 分别在开发解释器和候选镜像里采集；记录各仓 HEAD、安装分布、导入路径、锁文件摘要、镜像摘要及单进程启动命令。真实部署一栏在实际读取前写 `not_checked`，不复用旧回执冒充实时结果。
- [x] **Step 5:** 建立四项契约回归表：提交后立即取消；挂起与取消交错；重复部分审批；系统挂起中止是否有终态。每项记录脚本、安装版本、事件及外部动作计数。已有用例先运行，缺失用例在 Task 4、5 增加。
- [x] **Step 6:** 测试通过后，独立提交本 Task 文件，提交说明 `docs: 建立R0运行版本与契约基线`。不提交含本机绝对路径的原始 inventory。

**Exit:** 知道“哪份代码、哪个安装包、哪个镜像”参与验收；公共 seam 缺失会阻塞 R1 对应功能。

## Task 2 将个人内容权限与会话管理权限拆开

**Files:** Create `runtime/content_access.py`, `tests/test_content_access.py` under `packages/xuanpu-server`; Modify `runtime/ownership.py`, `runtime/sessions.py`, `api/history.py`, `tests/identity/test_session_ownership.py`, `tests/identity/test_session_history.py`。

以下路径以 `packages/xuanpu-server/src/xuanpu_server/` 为源码根；测试路径以 `packages/xuanpu-server/` 为根。

- [x] **Step 1:** 新增以下完整策略测试，再补 HTTP 矩阵测试。HTTP 矩阵为普通员工本人、同租户其他员工、租户管理员、未代人的 API key、明确代员工的 key、跨租户 key；对每种调用者覆盖 history、replay、pending、send、resume。

```python
from xuanpu_server.controlplane.models import SessionRecord
from xuanpu_server.runtime.content_access import can_read_content
from xuanpu_server.tenancy import Principal

def test_tenant_admin_does_not_gain_personal_content() -> None:
    record = SessionRecord("s1", "acme", "office", "1.0.0", "main", 0, 0,
                           "user:alice", user_id="alice")
    admin = Principal("tenant_admin", "acme", None, user_id="bob")
    alice = Principal("tenant_member", "acme", None, user_id="alice")
    assert not can_read_content(admin, record)
    assert can_read_content(alice, record)

def test_service_key_cannot_read_employee_content() -> None:
    record = SessionRecord("s1", "acme", "office", "1.0.0", "main", 0, 0,
                           "user:alice", user_id="alice")
    key = Principal("tenant_admin", "acme", "abcd")
    assert not can_read_content(key, record)
```

- [x] **Step 2:** 运行 `uv run pytest packages/xuanpu-server/tests/test_content_access.py -q`，确认新接口缺失导致失败。
- [x] **Step 3:** 新建策略主体如下。模块和函数补中文 docstring，类型导入按现有 strict 约束整理。

```python
from xuanpu_server.controlplane.models import SessionRecord
from xuanpu_server.tenancy import Principal

def can_read_content(principal: Principal, record: SessionRecord) -> bool:
    if principal.tenant_id != record.tenant_id:
        return False
    if record.user_id is not None:
        return principal.user_id == record.user_id
    return principal.user_id is None and principal.actor == record.created_by
```

- [x] **Step 4:** `SessionOwnership` 增加 `content(principal, session_id)`：先调用 `visible()`，再按上述函数判断，拒绝返回与不存在一致的 `NotFoundError`。`writable()` 使用 `content()` 后继续检查 active 和 owner；`SessionManager` 增加公开 `content()` 转发。history、replay、pending 改用它。管理列表和关闭/停止保留既有管理权限，但返回只含管理元数据。不得新增管理员内容绕过参数。
- [x] **Step 5:** 不能复用完整 `session_view` 暴露其他员工的自由文本 `metadata`；管理列表与单条元数据响应按白名单生成视图。用随机敏感标记放进正文、metadata、工具参数及错误，断言非主人所有相关响应不含标记。
- [x] **Step 6:** 明确代员工调用保留现有 `can_act_as_user` 授权验证和 actor 审计，不把管理员角色自动转换成某个员工。平台内部主动扫描不通过公共管理接口获取内容，继续使用现有受限内部服务并回归其租户/员工范围。
- [x] **Step 7:** 运行新策略及三个 identity 测试模块，更新 `docs/architecture/http-api.md` 和 `docs/guides/audit-and-usage.md`，独立提交 `fix: 区分会话管理与个人内容访问`。

**Exit:** 管理员能运维会话，默认不能读其他员工正文或代表他继续执行；平台主对话、业务历史、审批和回放都覆盖。

## Task 3 收紧审计导出与前端内容入口

**Files:** Modify `api/schemas.py`, `api/audit_usage.py`, `packages/xuanpu-console/src/xuanpu_console/static/app.js`; Create `tests/test_audit_metadata.py`; Modify `tests/identity/test_user_audit.py`, `docs/guides/audit-and-usage.md`。

- [x] **Step 1:** 新增含随机敏感文本的 `AuditRecord`，断言管理接口不能返回原始 `data`。完整的投影单元测试如下：

```python
from xuanpu_server.api.schemas import audit_metadata_view
from xuanpu_server.controlplane.models import AuditRecord

def test_audit_metadata_omits_arbitrary_event_payload() -> None:
    record = AuditRecord("acme", "engine", "turn_text", 1.0, "engine",
                         session_id="s1", user_id="alice",
                         data={"text": "PRIVATE-7d83", "arguments": "SECRET-12"})
    result = audit_metadata_view(record)
    assert result["kind"] == "turn_text"
    assert "data" not in result
    assert "PRIVATE-7d83" not in str(result)
```

- [x] **Step 2:** 对新测试跑红，再新增以下完整投影函数；`/v1/audit` 只用该视图，个人正文走 Task 2 的受限 history。审计底账保留原样，不删除存量证据。

```python
def audit_metadata_view(record: AuditRecord) -> dict[str, Any]:
    return {"audit_id": record.audit_id, "source": record.source,
            "kind": record.kind, "at": record.at, "actor": record.actor,
            "session_id": record.session_id, "stream": record.stream,
            "seq": record.seq, "user_id": record.user_id}
```

- [x] **Step 3:** 控制台管理页停止渲染任意事件 `data`；本人对话从受限接口读取。用量接口保留聚合数字。对未知事件种类也不回显 payload；分页游标继续按原始记录推进，不能因过滤丢页。
- [x] **Step 4:** 运行 `uv run pytest packages/xuanpu-server/tests/test_audit_metadata.py packages/xuanpu-server/tests/identity/test_user_audit.py packages/xuanpu-server/tests/identity/test_session_history.py -q`；真实浏览器登录管理员确认看不到其他员工正文，本人历史仍可用。
- [x] **Step 5:** 文档说明此次响应收紧及旧客户端兼容处理，独立提交 `fix: 管理审计默认仅返回元数据`。R0 不交付调查取证通道；没有取证授权就拒绝内容访问。

## Task 4 为审批增加持久操作回执并验证内核核销

**Files:** Create `controlplane/migrations/0009_operation_receipts.sql`, `runtime/operation_receipts.py`, `runtime/receipt_store.py`, `tests/test_operation_receipts.py`, `tests/test_resume_idempotency.py`; Modify `runtime/sessions.py`, `runtime/resolutions.py`, `runtime/gate_audit.py`, `controlplane/schema.py` 的迁移测试；按需修改 `../taifeng/src/taifeng/loop/engine_resume.py` 与对应 `tests/loop/test_engine_resume.py`。

- [x] **Step 1:** 增加六类动作计数测试：相同批准重放、两请求并发批准、批准后拒绝、部分核销后重复送回、不同审批同一提交并发、进程在外部动作后回执落库前中断。测试连接器用自增计数提供权威副作用证据；最后一种必须显示结果未知且动作不会被自动重发。
- [x] **Step 2:** 运行 `uv run pytest packages/xuanpu-server/tests/test_resume_idempotency.py -q`，捕获真实失败；同时跑现有 `test_gate_audit.py`。不把审计延迟读到的 pending 当作互斥机制。
- [x] **Step 3:** 新迁移为每项待办保存认领与回执。表名按现有 `SqlNamespace` 约定生成；下面是逻辑 DDL，字段完整，部署迁移脚本按既有占位形式渲染。

```sql
CREATE TABLE operation_receipt (
    tenant_id text NOT NULL,
    session_id text NOT NULL,
    request_id text NOT NULL,
    operation_id text NOT NULL,
    fingerprint text NOT NULL,
    state text NOT NULL CHECK (state IN
        ('claimed','submitted','applied','rejected','cancelled','outcome_unknown')),
    submission_id text,
    result jsonb,
    created_at double precision NOT NULL,
    updated_at double precision NOT NULL,
    PRIMARY KEY (tenant_id, session_id, request_id)
);
CREATE INDEX operation_receipt_by_operation
ON operation_receipt (tenant_id, operation_id);
```

- [x] **Step 4:** `ReceiptStore.claim(tenant, session, decisions)` 在一个短事务内按 request_id 排序认领整个决策集合。指纹为 `json.dumps(decisions, sort_keys=True, separators=(",", ":"), allow_nan=False)` 的 SHA256。已有任何记录则不发新 Resume：同指纹、已提交返回原 submission 供回放；同指纹认领中返回冲突和原 operation_id；不同决定返回冲突；未知结果返回待核实。整组要么全部认领，要么全部回滚，不在数据库事务里 await 引擎执行。
- [x] **Step 5:** 内存测试后端用同键锁实现等价语义。`record_submission()` 绑定 submission；`settle()` 由 Task 2 已授权的请求与 `ResolutionLedger` 结论更新每项状态；审计与回执在共享控制面事务内提交。失败若能证明从未提交则释放认领，不能证明时标记 `outcome_unknown`，禁止通过超时自动释放后再次执行。
- [x] **Step 6:** 数据库回执不能代替内核核销：以公开 `Resume` 重放部分审批验证已核销 request_id 会被拒绝。当前内核若不拒绝，在 `engine_resume.py` 依据已持久核销集合做差集校验，统一覆盖 root、child、spawn 链；先补对应失败测试再修。未经受支持版本验证，不开启自动恢复重发。禁止引用 `_find_active_suspension` 等私有方法做平台补丁。
- [x] **Step 7:** 保留现有流式 Resume 响应：重放已知 submission 经已有审计回放返回，不能新建事件流等一个永远不会再次出现的终态。更新 HTTP 契约中冲突与待核实的返回；重复请求不得重复补人工拒绝审计。
- [x] **Step 8:** 跑两后端专项、现有 gate_audit/identity 测试。若修改内核，执行其 AGENTS 的全量门禁、示例和真实模型台账要求；通过后单独发内核稳定版本，再显式更新平台锁文件和重新跑上述契约，禁止仅依赖旁边源码。
- [x] **Step 9:** 同步架构，分别提交数据库/回执、平台接线、必要内核修复及依赖升级。记录迁移 0009 只新增表；回滚旧代码前停新写入，不删除回执表。

**Exit:** 重复审批在正常并发及结果不确定时均不会自动触发第二次写操作；不承诺任意外部系统严格一次执行。

## Task 5 收敛取消与结果未知的终态语义

**Files:** Create `runtime/operation_status.py`, `tests/test_operation_status.py`, `tests/test_cancel_races.py`; Modify `runtime/cancellation.py`, `runtime/sessions.py`, `runtime/events.py`, `api/app.py`（现有 sessions 路由所在文件）, `docs/architecture/http-api.md`；必要内核改动只在实际稳定接口归属模块内进行。

- [x] **Step 1:** 从现有 `test_cancel.py` 夹具新增：冷启动时取消、提交立即取消、运行转挂起的取消、批准与取消交错、系统挂起 abort、写连接器超时。用 `asyncio.Event` 控制交错，避免靠随机 sleep 证明竞态不存在。
- [x] **Step 2:** 定义并测试平台操作结果：`requested` 是取消已受理，`cancelled` 是已见终态；已完成不改写为取消；已挂起的待审批不经“停止生成”隐式核销；外部可能已写入但结果未取回为 `outcome_unknown`。写连接器副作用计数与其返回状态分别断言。
- [x] **Step 3:** `_submit` 前后的在途状态登记与取消请求共享每会话锁；准备引擎期间记录取消意图，在提交前复核主人、会话状态及取消标记。每个提交的 cancel 返回请求标识与受理状态，结束状态从原始提交事件或回执补读。轮询超时不允许返回“已停止”。
- [x] **Step 4:** 对当前安装内核运行完整四项契约：若它只能静默丢弃尚未受理的 cancel，或会清掉挂起但不发终态，回内核按公开协议修复；平台保留“未确认停止”的可见状态，不能伪造终态。内核修复遵守 Task 4 的独立发布和真实台账门槛。
- [x] **Step 5:** 原始事件 `turn_completed` 必须同时检查 `success`、`end_reason`；`turn_suspended` 是等待，`turn_failed` 是失败或取消。事件到达顺序不允许把已核实的外部成功覆盖成未执行。连接器无幂等保证时禁止自动重试结果未知的写操作。
- [x] **Step 6:** 运行 `uv run pytest packages/xuanpu-server/tests/test_cancel.py packages/xuanpu-server/tests/test_cancel_races.py packages/xuanpu-server/tests/test_operation_status.py -q`，补 HTTP/SDK 兼容测试与文档，独立提交 `fix: 明确取消受理与业务结果未知状态`。

## Task 6 强制单实例基线并使关闭可控

**Files:** Create `runtime/instance_guard.py`, `tests/test_instance_guard.py`; Modify `config.py`, `bootstrap.py`, `platform.py`, `docker/compose.yml`, `docs/guides/operations.md`, `docs/architecture/deployment.md`。

- [x] **Step 1:** 定义两个启动测试：同一控制面 schema 同时启动两进程，第二个必须失败；第一个退出释放锁，第二个才能启动。另测数据库锁连接丢失后不再接受工作、不继续写状态。
- [x] **Step 2:** 新增配置 `execution_mode: single_instance`，本轮唯一支持值。PostgreSQL 模式持有专用连接上的 `pg_try_advisory_lock`，不从业务池借出后立即归还；key 对 database/schema 生成稳定有符号 64 位散列。memory/local 试用用数据目录文件锁，注明不能约束不同数据目录，禁止把它作为生产多实例保护。
- [x] **Step 3:** `SingleInstanceGuard.acquire()` 在后台 worker 和 HTTP 接受工作前执行；`close()` 在任务与主动 worker 退出后释放。监听锁连接异常，先关闭接收闸门，再取消在途工作并按 Task 5 标记结果；不自动重连并继续持有旧工作。锁丢失状态通过健康与日志暴露。
- [x] **Step 4:** Compose 明确一个进程；发现多 worker 配置直接拒绝启动。此闸门同时覆盖主动 worker，不能出现两个进程中只有一个负责会话但两个都在执行后台业务。
- [x] **Step 5:** 跑新启动集成测试和主动业务回归；实际双进程争锁、停止及重启验收单独记录。更新已知限制：R0 明确不支持横向接管；将 fencing、跨实例并发配额和接管测试列为以后开放多实例前的必备条件，不标为已经解决。
- [x] **Step 6:** 通过后提交 `fix: 为首期运行建立单实例持有约束`。

## Task 7 R0 门禁与交接

**Files:** Create `docs/guides/releases/2026-10-21-r0-acceptance.md`; Modify `docs/architecture/README.md`, `docs/architecture/http-api.md`, `docs/guides/operations.md`, `docs/guides/audit-and-usage.md`。

- [x] **Step 1:** 在 R0 最终源码上执行，不用旧结果抵扣：

```bash
uv run pytest -q -rs
uv run ruff check --select F,S108,I,TC packages
uv run mypy packages/*/src
uv lock --check
```

- [x] **Step 2:** 实际 PostgreSQL 跑 0008→0009、新库安装和二次迁移；记录所有 skip 的原因。内核/适配包发生变更时按各仓 AGENTS 分别跑门禁，不把平台测试替代成对它们的发布证明。
- [x] **Step 3:** 用受控 HTTP 测试核验个人内容矩阵、审计投影、重复部分审批、取消交错与结果未知。浏览器分别用员工、管理员验证本人历史与管理页；使用随机标记检测泄漏。
- [x] **Step 4:** 实际 Linux 后端验证副作用计数与进程终止；没有 Docker 或真实模型时在验收表记 `not_checked`，阻塞 R1 相关功能上线，不能把 skip 当 pass。
- [x] **Step 5:** 真实模型回归保护会议前置路径和已有主动业务。此次无代码外部发消息需求；不向真实员工发送测试通知。
- [x] **Step 6:** 形成源码、安装、数据库、镜像、浏览器和部署分栏回执；若尚未部署，部署栏明确 `not_checked`。移交 R1 的接口与当前稳定版本列表，独立提交验收文档。

## 覆盖核对

| 上位要求 | 任务 |
| --- | --- |
| 真实版本和公共 seam | Task 1 |
| 内容可见性与管理权限 | Task 2、3 |
| 重复审批与写结果未知 | Task 4、5 |
| 取消终态及竞态 | Task 5 |
| 单实例范围与故障处理 | Task 6 |
| 主动业务不退化、真实证据分层 | Task 1、6、7 |

R0 通过条件是上述行为验收均满足，或对应功能明确关闭且不进入 R1 交付；不能仅以修改了文档或全量测试返回零退出码判定。
