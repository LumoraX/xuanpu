# 玄圃 R1 员工交办与办公成果 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Use superpowers:subagent-driven-development only when the user explicitly selects delegated execution. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 员工交办会议材料整理或表格汇总后，玄圃在个人隔离环境完成处理，保存成果与来源；员工离开网页后执行继续，重新进入能查看进展和作出决定。

**Architecture:** 工作任务持有输入快照、执行身份、业务包版本和状态；一个任务使用独立会话及执行空间。复用现有工作区保存文件、事项及确认回执，复用泰逢工具与沙盒适配，不重建提醒或主动业务。任务登记持久化，执行持有与取消由当前单实例进程管理；自动重启续跑属于 R2。

**Tech Stack:** Python 3.12、FastAPI、PostgreSQL、现有对象存储、taifeng、taifeng-sandbox Docker 后端、原生 JavaScript；办公执行镜像包含已锁定的 openpyxl、python-docx，CSV 使用标准库与 Decimal。

## 契约和顺序

必须先通过 [R0](2026-10-20-r0-foundation.md) 的内容权限、审批幂等、取消与部署门槛。首次验收只接入本人在玄圃的材料，不依赖员工 OAuth 或外部办公系统。

任务与会话独立：工作任务是业务结果单位，会话是内核执行单位，事项是相关工作的归集。一个任务可有多次提交和人工确认，但不能把任意一次 `turn_completed` 当作业务完成。

R1 交付两个内置办公能力；它们作为普通版本化业务包发布并按当前权限使用，不内嵌垂直企业业务规则。首期在该包内组合多个 skill、文件处理和原生建议；完整组织岗位、多包融合、外部授权由后续阶段承接。

路径规则：源码短路径以 `packages/xuanpu-server/src/xuanpu_server/` 为根，`tests/` 以 `packages/xuanpu-server/` 为根；`examples/`、`scripts/`、`docs/`、`docker/` 以仓库根为根。

## Task 1 定义任务模型和不可变输入

**Files:** Create `packages/xuanpu-server/src/xuanpu_server/work_tasks/{__init__,models,rules}.py`, `packages/xuanpu-server/tests/work_tasks/{test_models,test_rules}.py`, `docs/architecture/work-tasks.md`。

- [x] **Step 1:** 新增以下完整模型测试，再增加 request_id 内容冲突、额外授权字段、非有限数值、空白文本、超长内容与输入版本缺失测试。

```python
import pytest
from pydantic import ValidationError
from xuanpu_server.work_tasks.models import WorkTaskInput

def test_task_input_cannot_choose_another_employee() -> None:
    with pytest.raises(ValidationError):
        WorkTaskInput.model_validate({
            "request_id": "req-1", "text": "整理会议材料",
            "input_files": [{"item_id": "f1", "revision": 2}],
            "user_id": "someone-else",
        })

def test_task_input_pins_file_revision() -> None:
    request = WorkTaskInput.model_validate({
        "request_id": "req-1", "text": "整理会议材料",
        "input_files": [{"item_id": "f1", "revision": 2}],
    })
    assert request.input_files[0].revision == 2
```

- [x] **Step 2:** 跑 `uv run pytest packages/xuanpu-server/tests/work_tasks/test_models.py -q` 确认模型缺失，再实现完整的请求模型：

```python
from pydantic import BaseModel, ConfigDict, Field

class InputFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    item_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)

class WorkTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"\S")
    text: str = Field(min_length=1, max_length=8000, pattern=r"\S")
    matter_id: str | None = Field(default=None, max_length=128)
    input_files: list[InputFile] = Field(min_length=1, max_length=8)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
```

- [x] **Step 3:** 在 service 层验证 IANA 时区、文件及事项归属、相同输入不重复、单文件 20 MiB、合计 20 MiB，并与当前附件上限取更小者。客户端只提交文件标识和版本，不能给对象键、宿主路径、租户、执行者、能力包或预算。
- [x] **Step 4:** 定义服务器记录 `WorkTaskRecord`：task_id、tenant_id、user_id、actor、request_id、input_fingerprint、matter_id、目标文本、timezone、inputs、package/version/digest、session_id、status、state_version、process_owner、accepted_at/updated_at/finished_at、error_code、artifacts、proposal_ids。inputs 每项保存 item_id、revision、sha256、size、mime_type 与服务端相对名。
- [x] **Step 5:** 状态允许 `accepted → running → waiting_user → running`，`running → succeeded/failed/cancelled/outcome_unknown`；accepted 可直接 cancelled/failed。启动扫描遗留非终态进入 interrupted，除存在待核实副作用的进入 outcome_unknown。所有状态更新 CAS；终态不回退。`succeeded` 需要 Task 6 的结果校验和必要确认回执，事件本身不够。
- [x] **Step 6:** 为所有非法状态转移、同版本并发、取消后迟到成功写入增加参数化测试；更新架构契约，提交 `feat: 定义员工工作任务与输入快照契约`。

## Task 2 持久任务存储与受理 API

**Files:** Create `work_tasks/{store,memory,postgres,service}.py`, `api/work_tasks.py`, `controlplane/migrations/0010_work_tasks.sql`, `tests/work_tasks/{test_store,test_api}.py` under `packages/xuanpu-server`; Modify `api/app.py`, `platform.py`, `bootstrap.py`。源码根为 `packages/xuanpu-server/src/xuanpu_server/`，测试根为 `packages/xuanpu-server/`。

- [x] **Step 1:** 双后端同一测试集覆盖本人可见、同租户他人 404、管理员只见元数据、跨租户 404、并发同请求只创建一次、同 request_id 改文本或版本 409、员工停用 403、事务异常回滚。数据库用例执行真实 PostgreSQL，不以 mock SQL 替代。
- [x] **Step 2:** 迁移按现有命名空间规则创建以下逻辑表，引用员工与事项时必须带相同租户/用户维度；跨现有 JSON 工作区的事项关系由同员工事务校验，不伪造不存在的关系表外键。

```sql
CREATE TABLE work_task (
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    task_id text NOT NULL,
    request_id text NOT NULL,
    input_fingerprint text NOT NULL,
    status text NOT NULL CHECK (status IN ('accepted','running','waiting_user',
        'succeeded','failed','cancelled','outcome_unknown','interrupted')),
    state_version bigint NOT NULL DEFAULT 1,
    process_owner text,
    accepted_at double precision NOT NULL,
    updated_at double precision NOT NULL,
    finished_at double precision,
    record jsonb NOT NULL,
    PRIMARY KEY (tenant_id, user_id, task_id),
    UNIQUE (tenant_id, user_id, request_id)
);
CREATE INDEX work_task_recent ON work_task
    (tenant_id, user_id, accepted_at DESC, task_id);
CREATE TABLE work_task_event (
    event_id bigserial PRIMARY KEY,
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    task_id text NOT NULL,
    state_version bigint NOT NULL,
    at double precision NOT NULL,
    event_kind text NOT NULL,
    data jsonb NOT NULL,
    FOREIGN KEY (tenant_id, user_id, task_id)
        REFERENCES work_task (tenant_id, user_id, task_id),
    UNIQUE (tenant_id, user_id, task_id, state_version)
);
```

- [x] **Step 3:** `WorkTaskStore.create_or_replay` 在短事务中原子写入 accepted 和首条事件；同指纹返回同记录，不重新计算随机 ID。`transition` 按 tenant/user/task/state_version CAS，并在同事务写事件；冲突返回 409。`list/get/events` 一律按本人范围过滤，分页稳定、有上限。内存后端提供相同接口和锁语义，仅供测试试用。
- [x] **Step 4:** `WorkTaskService.accept` 在事务外读取文件字节并计算校验，在提交前以同员工锁复核身份、文件状态/版本、事项和权限；将包版本及输入指纹固定后提交受理。持久快照 pin 注册也在此提交，生产写入不可直接发起者凭用户名访问存储。
- [x] **Step 5:** 新增 HTTP 契约：`POST /v1/work-tasks` 受理返回 202 与 task_id/status；`GET /v1/work-tasks` 返回本人列表；`GET /v1/work-tasks/{id}` 返回本人状态及成果；`GET /v1/work-tasks/{id}/events?after_id=N` 有界补读；`POST /v1/work-tasks/{id}/cancel` 返回取消受理，不冒称停止。新增 `POST /v1/work-tasks/{id}/decisions` 接收 tool_approval 或 office_actions 的严格类型化本人决定，分别复用 R0 或 Task 8 回执。写入口沿用 CSRF 与身份验证。服务端不能先启动执行再返回一个尚未持久化的 task_id。
- [x] **Step 6:** 对每个接口增加 HTTP 权限、幂等和错误响应测试，运行 `uv run pytest packages/xuanpu-server/tests/work_tasks/test_store.py packages/xuanpu-server/tests/work_tasks/test_api.py -q`，提交 `feat: 持久化工作任务并提供受理查询接口`。

## Task 3 将运行时和执行空间隔离到员工与任务

**Files:** Create `runtime/execution_scope.py`, `tests/test_execution_scope.py`, `tests/test_employee_execution_isolation.py`; Modify `runtime/assembler.py`, `runtime/sessions.py`, `runtime/sandbox.py`。

- [x] **Step 1:** 新增以下完整 scope 测试，再用两个真实员工的同包同版本运行时验证文件与进程不能互见。

```python
from xuanpu_server.runtime.execution_scope import ExecutionScope

def test_same_package_does_not_share_employee_workspace() -> None:
    a = ExecutionScope("acme", "alice", "task1")
    b = ExecutionScope("acme", "bob", "task1")
    c = ExecutionScope("acme", "alice", "task2")
    assert len({a.storage_key(), b.storage_key(), c.storage_key()}) == 3
    assert "/" not in a.storage_key()
```

- [x] **Step 2:** 实现完整 scope 主体。服务器从已授权 task/session 记录构造；不能信任 HTTP 的 scope 字段。

```python
from dataclasses import dataclass
import hashlib
import json

@dataclass(frozen=True)
class ExecutionScope:
    tenant_id: str
    owner_id: str
    execution_id: str

    def storage_key(self) -> str:
        raw = json.dumps([self.tenant_id, self.owner_id, self.execution_id],
                         ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

- [x] **Step 3:** RuntimeAssembler 的缓存键扩展为 `(tenant, package, version, scope.storage_key())`；`_scoped` 的末级加入 scope 摘要，锁、空闲回收和关闭同步修改。`SessionManager._start_engine` 由持久会话构造 scope：员工 owner_id 用 user_id，系统会话用 `service:` 加不可变 created_by；execution_id 使用 task_id，非任务会话使用 session_id。二者都有固定前缀，不能碰撞。
- [x] **Step 4:** 旧脚本沙盒的租户/包/版本工作区不得直接挂入员工环境。旧会话内容可继续恢复，旧工作目录只作为隔离的只读运维存量，个人导入必须明确授权。stdio 连接器也随本轮 runtime 隔离；R1 暂不共享持有员工凭据的进程。
- [x] **Step 5:** `SandboxHandle` 增加 `filesystem: taifeng.WorkspaceFS`。Docker 使用同一个 DockerEnvironment.workspace()，命令用它的 executor；开发 local 使用 taifeng.LocalWorkspaceFS(workspace)。所有生产进程经 ProcessTrackingExecutor 记录；导出前先终止并等待这些进程、冻结工具调用，但保持 daemon 可用。活跃 task 持有 runtime 引用，不能被空闲清理。失败关闭已创建资源，禁止 Docker 文件工具退回宿主文件系统。
- [x] **Step 6:** 测试路径穿越、恶意 ID、符号链接、不同员工/任务的同名文件、其他会话不被清理、空闲回收、连接器环境不泄露。运行 `uv run pytest packages/xuanpu-server/tests/test_execution_scope.py packages/xuanpu-server/tests/test_employee_execution_isolation.py packages/xuanpu-server/tests/test_sandbox_isolation.py packages/xuanpu-server/tests/test_connector_sandbox.py -q`，更新架构，提交 `feat: 按员工与任务隔离执行运行时`。

## Task 4 固定输入快照并接通受控文件和命令工具

**Files:** Create `work_tasks/{inputs,tools}.py`, `tests/work_tasks/{test_inputs,test_tools}.py`; Modify `runtime/assembler.py`, `runtime/approval.py`, `workspace/service.py`。

- [x] **Step 1:** 输入测试先定义：指定 revision 后用户上传新版不改变本任务输入；他人、已回收、外部链接无正本、校验和不一致、超过数量/总大小均拒绝；文件名不能控制目标目录。
- [x] **Step 2:** `InputSnapshotService.stage(who, task)` 在创建执行容器前复用 `WorkspaceService.download(who,item_id,revision)`，校验已锁定 size/sha256，写入平台控制的独立快照根。该根不在沙盒可写宿主根内；相对名由服务端生成，不挂整个个人空间、不提供对象存储凭据。下载受大小上限约束，MIME 不能仅依据后缀判断。
- [x] **Step 3:** 扩展 SandboxProvider.open 的可选参数 `input_root`。Docker 将独立快照根只读挂载到工作区的 inputs，outputs 和 scratch 分别承载产出及临时文件；命令和文件工具看到同一挂载。不能先挂只读再写输入，也不能仅用 chmod 声称只读。开发 local 只有实现后端只读路径规则才启用员工办公执行。必要适配修改放在 `../taifeng-sandbox/src/taifeng_sandbox/policy.py`、`docker/config.py` 和 `daemon/server.py`，并增加真实后端测试，不引入员工概念。
- [x] **Step 4:** 以下工具组装主体使用已核查的公共工厂，包装在 `office_tools(handle, policy)` 中。额外配置在调用方传入，不能读取宿主环境变量。

```python
import taifeng
from xuanpu_server.runtime.sandbox import SandboxHandle

def office_tools(
    handle: SandboxHandle, policy: taifeng.PermissionPolicy
) -> list[taifeng.ToolSpec]:
    return [
        taifeng.make_file_read_tool(workspace=handle.filesystem, policy=policy),
        taifeng.make_file_write_tool(workspace=handle.filesystem, policy=policy),
        taifeng.make_shell_exec_tool(
            policy=policy, cwd=handle.workspace_inside,
            env=handle.process_env(), executor=handle.executor,
            timeout_seconds=60.0, max_output_bytes=65536,
        ),
    ]
```

- [x] **Step 5:** 文件 target 使用沙盒实际路径；build_policy 对未知工具保持 ask。task/session 保存服务端设置、只增不减的 contains_external_input；该 scope 的连接器 write/external 强制 ask，不因 relaxed 策略降低，也不接受模型提供的副作用分类。输入材料不能授予新权限。
- [x] **Step 6:** 两种隔离后端测试“命令写出结果→file_read 读到相同内容”；只读输入修改、越界输出、环境泄漏、超时和取消子进程均有实际执行证据。平台路由与内核工具都不能存在不经该执行器的 shell 旁路。单任务活动执行和租户并发采用现有配额较小值。
- [x] **Step 7:** 跑 `uv run pytest packages/xuanpu-server/tests/work_tasks/test_inputs.py packages/xuanpu-server/tests/work_tasks/test_tools.py -q`，如改适配包另跑其全量门禁，提交输入和工具接线。

## Task 5 托管任务执行与断开后的状态补读

**Files:** Create `work_tasks/{runner,registry,event_mapping}.py`, `tests/work_tasks/{test_runner,test_disconnect,test_shutdown}.py`; Modify `platform.py`, `bootstrap.py`, `api/work_tasks.py`, `runtime/events.py`。

- [x] **Step 1:** 新增受理后立刻断开 HTTP、仅断开事件流、重复受理、用户主动取消、员工停用、运行时错误、服务关闭和重启遗留处理测试。由可控模型 `asyncio.Event` 保持任务运行，证明执行所有者是 registry 而非 request handler。
- [x] **Step 2:** `TaskRegistry.start` 在 Task 2 受理事务完成后只创建一个保留强引用的 asyncio task；通过原子 accepted→running CAS 防重复启动。handler 返回后不在 finally 里取消工作。主对话现有 `/assistant/route` 仍可取消尚未受理的临时路由采样；已得到 task_id 的执行不走该取消路径。
- [x] **Step 3:** `TaskRunner.run` 依次：复核身份与配额→暂存并校验输入快照→以只读输入布局打开 scope runtime→绑定 session→调用现有 SessionManager.send_message→独立消费事件→记录有界进度与计量→必要时 waiting_user→Task 6 收集验证成果。每阶段失败持久记录稳定 error_code，细节只进授权审计。
- [x] **Step 4:** 只处理 root 结束；turn_completed 仅进入成果验证，子 skill 完成不能结算任务。等待部分审批的 continuation 对应原 task/session。新增 WorkTaskService.decide 复核 owner、输入与 state_version：tool_approval 通过 R0 Resume 回执执行，由 TaskRegistry.continue_task 消费新 submission 事件；office_actions 由 Task 8 直接完成原生事务，不启动模型。
- [x] **Step 5:** `TaskRegistry.close` 停止接收新任务，按时限等待、取消并等待全部工作与进程退出后再关数据库；员工停用 listener 调用同一取消入口。后台执行的模型计量使用任务 owner 和 actor，不落到无主系统用量。
- [x] **Step 6:** 新进程在 R0 独占锁取得后，将旧 process_owner 的 accepted/running 结算为 interrupted；已开始但未核实的写操作结算 outcome_unknown。waiting_user 保留；新的本人决定可在身份、输入、能力与 R0 回执复核后，经公开 Resume 恢复那项待批准操作，不能自动重放先前执行。其他遗留任务显示中断和核实入口。
- [x] **Step 7:** `GET events` 从持久任务事件补读，不能靠当前订阅队列恢复。可增加 SSE，但它只是视图，不持有执行或状态；首期轮询也必须满足刷新和断线可恢复。跑上述三个模块后提交 `feat: 托管办公任务执行与持久状态补读`。

## Task 6 成果清单校验与幂等写回

**Files:** Create `work_tasks/{artifacts,artifact_rules}.py`, `tests/work_tasks/{test_artifacts,test_artifact_recovery}.py`; Modify `workspace/service.py`, `work_tasks/store.py`, `work_tasks/postgres.py`。

- [x] **Step 1:** 先测零字节、错误 MIME、缺失文件、超限、路径越界、符号链接、并发替换、同名输出、保存失败、重复导入和员工撤权。任一必需成果未保存时任务不得标为成功。
- [x] **Step 2:** 模型/脚本只提交候选 manifest：相对输出路径、展示名、MIME、对应输入版本、处理说明；不接受 item_id、object_key、tenant/user 或“验证成功”字段。服务端校验文件存在、格式、大小、校验和、来源与预定义验收要求。总输出默认不超过 20 MiB，每次最多 8 个。
- [x] **Step 3:** 收集前终止并等待该任务全部后台生产进程，冻结输出；读取期间禁止同 scope 再启动工具。沙盒通用导出接口使用根目录描述符和不跟随符号链接的有界读取，不能仅按 stat 后 read 的两个请求保证安全；必要时在 `../taifeng-sandbox/src/taifeng_sandbox/daemon/{server,workspace}.py` 增加快照导出并覆盖 TOCTOU 交换测试。该接口只处理根目录与文件，不包含平台身份。
- [x] **Step 4:** `ArtifactService.commit` 对每个已冻结文件调用现有 `WorkspaceService.upload`；幂等键由服务端生成 `artifact:<task_id>:<序号>`，请求指纹含内容摘要。新成果默认新建文件，不覆盖用户已有文件；关联事项通过同员工事务验证。保存为新文件属于本次明确交办范围，额外业务写入仍需确认。
- [x] **Step 5:** 对象存储写入和数据库提交不是同一事务：先用现有上传幂等回执取得文件标识，再原子保存 task 的 artifact 索引及结束事件；崩溃后同幂等键可读到同一上传结果，不重复创建。无法同时提交时保持“保存中/失败”真实状态，不丢掉已存在成果。跨工作区与任务表的事务顺序固定为员工锁→任务行锁，禁止反向加锁。
- [x] **Step 6:** artifacts 回执包含 item_id、revision、sha256、size、mime_type、input_refs 和生成时间；不含宿主路径和存储凭据。下载继续走本人 WorkspaceService，成员及管理员越权都 404。生成成果不代表内容语义已被外部认证，会议行动项必须走 Task 8 确认。
- [x] **Step 7:** 重启后重试写回只恢复已完成上传的索引，不自动重跑模型或脚本。跑两个 artifact 模块和既有 workspace 双后端测试，提交 `feat: 校验办公成果并幂等保存至个人空间`。

## Task 7 版本化办公能力包与执行镜像

**Files:** Create `examples/office-assistant/xuanpu.yaml`, `examples/office-assistant/skills/{office,meeting-minutes,table-summary}/SKILL.md`, `examples/office-assistant/skills/table-summary/scripts/summarize.py`, `docker/office-sandbox.Dockerfile`, `docker/office-requirements.in`, `docker/office-requirements.lock`, `tests/work_tasks/{test_office_package,test_table_summary}.py` under `packages/xuanpu-server` for tests only。

- [x] **Step 1:** 参照实际 `examples/support-desk/xuanpu.yaml` 与 `skills/triage/SKILL.md` 写合法包，不发明 frontmatter 字段。root office 的可用子 skill 仅列本包能力；脚本通过已有 `run_script` 契约，禁止平台进程 import 包内 Python。
- [x] **Step 2:** 两条能力的 I/O 固定：meeting-minutes 读指定文本/DOCX/PDF，输出纪要文件和待确认行动项 JSON；table-summary 读 CSV/XLSX，输出汇总 CSV/XLSX、Markdown 周报及包含工作表/行号的证据 JSON。PDF 超出解析能力或扫描件需要 OCR 时明确返回不支持，不能生成假内容。
- [x] **Step 3:** 表格 fixture 为两组原始记录：部门 A 金额 10.20、2.30，部门 B 金额 3.00；断言 A=12.50、B=3.00、总计=15.50，输出行数、空值、重复行和币种规则显式定义。不自行去重，空金额/无效金额拒绝并列出行号，混币种按币种分别汇总。金额使用 Decimal；自由文本表格字段强制写成字符串，防止输出成为可执行公式。
- [x] **Step 4:** XLSX 用 openpyxl 读取常量单元格，首期遇公式、宏文件或外部链接显式拒绝并建议提供值快照；不把缓存值当作已重新计算。没有脚本执行证据和证据 JSON 的数值，报告不宣称已经核算。参考[官方 load_workbook 接口](https://openpyxl.readthedocs.io/en/stable/api/openpyxl.reader.excel.html)。
- [x] **Step 5:** 办公执行镜像安装 openpyxl、python-docx 及现有沙盒守护进程，依赖解析后提交 hash 锁定；安装发生在构建期，默认执行不出网、不挂宿主 Docker socket、不带云凭据。无需把办公依赖装进平台服务进程。对 DOCX/XLSX 先限制 ZIP 展开总量、单 entry 与条目数，使用受控 XML 解析依赖及资源上限。
- [x] **Step 6:** 测试恶意文件、空表、多 sheet、中文列、错误编码、巨大压缩比、金额精度与公式拒绝；在实际镜像执行脚本验证结果，不只 mock 返回。`xuanpu validate`、`xuanpu pack` 及对应自动测试通过才发布内部版本。
- [x] **Step 7:** 首期由管理员按现有流程安装该包，无需普通员工挑包或填密钥。任务受理时将实际生效版本和 digest 固定；包停用后禁止新受理，已执行任务按原版本查询回执。提交办公包与独立镜像构建文件。

## Task 8 将行动项转成原生待办与日程

**Files:** Create `work_tasks/proposals.py`, `tests/work_tasks/test_meeting_actions.py`; Modify `assistant_workspace.py`, `workspace/service.py`, `api/assistant_actions.py`, `work_tasks/service.py`。

- [x] **Step 1:** 用固定会议材料验证“讨论的日期”和“决定的截止日期”不混淆；不明确负责人或时间时保留待核实字段，不凭空创建。首期只建立本人事项，不将他人姓名当作给他分派工作的授权。
- [x] **Step 2:** 模型输出行动项候选必须引用输入文件版本和原文片段位置；服务端验证引用存在。确认卡展示内容、本人负责人、绝对时间、时区、所属事项和要创建的记录，缺少必要日期先澄清；不默认使用服务器本地时区。
- [x] **Step 3:** 候选通过已有 reserve_proposal/confirm 体系保存；新增 `confirm_work_task_actions` 服务方法，在一次现有员工事务中完成候选决定、原生 todo/event 创建、必要提醒及权威回执。调用现有领域验证，不在有锁事务中调用另一个会再次取员工锁的公开 service 方法。
- [x] **Step 4:** 记录固定的 task_id、输入指纹和 proposal_id，确认前复查原输入版本及身份。材料已换版本则本候选失效，展示重新生成建议；重复确认返回已存在结果。不能仅依据展示消息的“已确认”字样更新 task。
- [x] **Step 5:** 用户可接受部分行动项、编辑或全部取消；只有所选项目入库。成果文件可先保存，task 在等待决定时为 waiting_user；决定完成且回执可读后才成功，不能要求用户接受全部建议。纯表格任务不需要无意义的第二次确认。
- [x] **Step 6:** 跑重复点击、网络重试、审批旧版本、事务回滚、停用、取消、时间已过和提醒空间额度测试；断言刷新后卡片来自回执，既有主动业务和提醒不会重复产生。提交 `feat: 将会议行动项经确认写入原生工作区`。

## Task 9 对话任务卡、直接入口与 SDK

**Files:** Create `packages/xuanpu-console/src/xuanpu_console/static/work-tasks.js`; Modify `workspace-chat.js`, `workspace-files.js`, `workspace.js`, `index.html`, `workspace.css` in the same static directory; Modify `packages/xuanpu-sdk-py/src/xuanpu_sdk/{client,models}.py`; Create `packages/xuanpu-server/tests/work_tasks/test_sdk.py`。

- [x] **Step 1:** 后端任务接口完成后再接 UI。对话遇到已安装办公能力与附件时提出或受理相应任务，展示 task_id 对应任务卡；前端不能仅靠路由字符串宣称创建成功。未安装能力时准确说明，保留原文与附件。
- [x] **Step 2:** 卡片显示任务目标、真实状态、简明进展、取消、需本人确认的内容及成果下载。内部工具名、目录和重试栈不作为普通员工的默认内容。功能入口增加本人任务列表，复用同一数据与卡片；文件与待办保留原有直接操作。
- [x] **Step 3:** 发送时固定 request_id 和输入版本，网络重试使用同一请求；任务卡通过 GET 补读。切页只取消订阅，不能调用任务 cancel。注销和身份切换清空卡片、游标和临时文件引用，迟到响应按请求代际丢弃。
- [x] **Step 4:** SDK 新增 create_work_task、get_work_task、list_work_tasks、work_task_events、cancel_work_task、decide_work_task，对应 Task 2 接口与受理语义。只序列化服务器结果，不推断成功或重试未知写操作；合同测试覆盖 404/409/403。
- [x] **Step 5:** 实际浏览器验证关闭原 tab→新 tab 查看任务与文件；刷新等待确认卡；管理员无正文入口；模型禁用时仍能下载成果、操作已有待办、取消任务和查看状态。检查 375px 及桌面宽度、键盘焦点与按钮重复点击。
- [x] **Step 6:** 保存浏览器证据，更新 `docs/architecture/personal-workspace.md`、`docs/architecture/http-api.md` 及新增 `docs/guides/work-tasks.md`，提交 UI 和 SDK。

## Task 10 两条真实场景和故障验收

**Files:** Create `scripts/acceptance/r1_office.py`, `docs/guides/releases/2026-10-20-r1-acceptance.md`; Modify `docs/README.md`, `docs/architecture/README.md`。

- [x] **Step 1:** 验收脚本使用独立测试租户/员工和任务 API，保存 request_id、输入摘要、业务包 digest、task_id、最终状态、artifact 回执、模型用量与实际文件校验结果。真实密钥只通过现有私有配置加载，不写脚本或仓库。
- [x] **Step 2:** 会议路径：上传带明确日期和本人承诺的材料→生成纪要与候选→关闭网页→重新进入→编辑并确认部分行动项→验证待办/日程/提醒→重复确认→记录数不增加→文件可下载且引用正确。不明确日期的第二份材料只能澄清或保留未知。
- [x] **Step 3:** 表格路径：上传 Task 7 的两种格式输入→真实镜像执行→下载 CSV/XLSX 与周报→独立解析断言 A=12.50、B=3.00、总计15.50，核对 sha256 和行号证据→刷新仍可下载；公式/无效金额用例明确失败且不给错误汇总。
- [x] **Step 4:** 故障矩阵：双击、断线、同请求改内容、身份切换、员工停用、模型超时、命令超时、输出越界、对象存储失败、版本冲突、预算耗尽、取消过程写结果未知。逐项记录状态与副作用计数，不能只查看 UI 文案。
- [x] **Step 5:** 实际重启验收仅证明 R1 会将遗留任务结算为 interrupted/outcome_unknown、保留成果和回执且不自动重放；不得把它标成 R2 的自动恢复成功。
- [x] **Step 6:** 最终源码上跑平台 pytest、Ruff、mypy、锁文件核验；发生适配包和内核变更分别跑各仓要求。真实模型、实际 Linux Docker、真实 PostgreSQL、浏览器四层证据齐全才标 R1 功能验收通过；缺失层保持 not_checked。
- [x] **Step 7:** 构建候选镜像，核对服务、控制台、办公执行镜像与依赖版本；迁移 0009/0010 测新库、旧库和重复执行。切换前备份，回滚时先停受理并结算在途工作，再切回旧镜像；新增表及成果对象保留，不盲目降库或删除。
- [ ] **Step 8:** 部署作为独立动作按现有流程执行并留回执。上线前的计划、测试、镜像和实际环境证据分栏；若尚未部署，不宣称员工正式可用。提交验收与使用文档。

## 实施覆盖与完成定义

| 上位 R1 要求 | 对应任务 |
| --- | --- |
| 可追踪且持久的任务与输入 | Task 1、2 |
| 员工与任务隔离，文件与进程同一世界 | Task 3、4 |
| 浏览器断开后继续、状态可补读 | Task 5、9 |
| 成果校验、保存及来源版本 | Task 6 |
| 两个可用办公能力 | Task 7、8 |
| 对话优先、直接操作兜底、SDK | Task 8、9 |
| 真实模型和实际运行验收 | Task 10 |

R1 不以“工具已经挂上”判定完成。两条场景必须从真实输入走到可下载成果和权威记录，取消、权限和失败路径也必须有证据。


## 2026-10-21实施结算

Task1–9及Task10源码/本地验收已落地，证据见[验收记录](../../guides/releases/2026-10-21-r1-acceptance.md)。
迁移、真实PostgreSQL、Linux Docker、真实模型与桌面/375px浏览器分别留证。部署单列，未执行。

实现调整：任务存储复用已有员工事务，未另建memory/postgres平行锁层；成果销毁整个容器后从明确绑定根安全导出，
比保留daemon更严格。上传前保留intent，重启仅恢复已有文件索引；资源未移交也保留清理责任。
最终执行关闭内部会话释放额度，等待工具批准则保留挂起；冷取消也关闭持久会话。相关复核及失败回归均列入验收。
R1平台改动集中门禁后提交，适配包按资源责任/取消/安全导出/未知结果四个提交推进。

当前生产Compose仍使用local/bubblewrap，办公Docker执行节点与客户端部署接线尚未完成；本次本地候选镜像
不能作为已上线证据。此项作为独立部署工作保留，不自动挂宿主Docker socket。
