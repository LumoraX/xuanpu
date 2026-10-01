# P0 底座升级 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让多实例不再共享数据目录、能停止生成、连接器进沙盒、支持远端 MCP，并收掉五处已知限制（设计见 [P0 详细设计](../specs/2026-10-02-p0-foundation-upgrade-design.md)）。

**Architecture:** 内核不改。taifeng-stores 让 `PostgresMessageStore` 能当 `EnginePool` 的会话主存；taifeng-sandbox 让执行器返回可持续读写的进程、让守护进程的文件视图满足内核 `WorkspaceFS`；xuanpu 加对象存储抽象、把业务包归档放进去、把会话主存接进 Postgres、加停止生成、把 stdio 连接器放进单独的连接器沙盒、加远端 MCP 传输、补齐过闸审计与几处运维接口。

**Tech Stack:** Python 3.12、asyncio、asyncpg、FastAPI/uvicorn、pydantic、taifeng 2026.10.1.10（同级目录 `../taifeng`）、boto3（S3 兼容，阿里云 OSS）、moto（测试用 S3）、prometheus_client。

---

## 通用约定（每个任务都适用）

- 三个仓都在分支 `feat/p0-base`（已从 `feat/v1-foundation` 分出）。
- 中文 docstring 与注释，英文标识符；文件 ≤ 800 行，函数 ≤ 80 行；禁止 silent fallback；业务代码不读环境变量。
- 每个任务结束前在**该仓**跑门禁并全绿：
  - taifeng-stores / taifeng-sandbox：`uv run pytest -q`、`uv run ruff check --select F,S108,I,TC src tests`、`uv run mypy src/`
  - xuanpu：`uv run pytest -q`、`uv run ruff check --select F,S108,I,TC packages`、`uv run mypy packages/*/src`
- commit message 用中文，结尾带：

  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  ```
- 只 import `taifeng.__all__` 里的名字。
- 各仓有文档覆盖测试（`tests/test_docs.py` 或 `packages/xuanpu-cli/tests/test_examples_and_docs.py`）：新增公开名字、线协议方法、HTTP 接口时同步文档，否则测试会红。

---

## A. taifeng-stores

### Task A1：依赖新内核，`ThreadInfo` 从顶层引入

**Files:**
- Modify: `taifeng-stores/pyproject.toml`（依赖下限、`[tool.uv.sources]`）
- Delete: `taifeng-stores/src/taifeng_stores/_kernel_types.py`
- Modify: `taifeng-stores/src/taifeng_stores/postgres/message_store.py:24`
- Modify: `taifeng-stores/tests/test_smoke.py`（`test_signature_types_pinned`）

- [ ] **Step 1：改依赖。** `pyproject.toml` 里 `taifeng>=2026.9.30.1` 改为 `taifeng>=2026.10.1.10`，并加：

  ```toml
  # 内核 2026.10.1.10 发布到 PyPI 之前，开发期从同级目录取源码。发布后删掉这一段。
  [tool.uv.sources]
  taifeng = { path = "../taifeng", editable = true }
  ```

  运行 `uv lock && uv sync --extra dev`，确认 `uv pip show taifeng` 显示 `2026.10.1.10`。

- [ ] **Step 2：跑测试看哪里红。** `uv run pytest -q`。预期 `test_smoke.py::test_signature_types_pinned` 失败（它断言 `ThreadInfo` 不在 `taifeng.__all__`）。其余应通过；若有别的失败，先查清是否由内核升级引起并修掉。

- [ ] **Step 3：改引入。** `message_store.py` 第 24 行改为 `from taifeng import ThreadInfo, ThreadMetadata`（合并到现有的 `from taifeng import ThreadMetadata`）。删除 `_kernel_types.py`，`grep -rn _kernel_types src tests docs` 清掉所有引用。

- [ ] **Step 4：改冒烟测试。** 把 `test_signature_types_pinned` 改成：

  ```python
  def test_signature_types_come_from_the_stable_layer() -> None:
      """稳定协议签名里出现的类型都在内核稳定层：适配包不再经单点模块引入（ADR 0005）。"""
      import taifeng

      assert "ThreadInfo" in taifeng.__all__
      assert not (Path(taifeng_stores.__file__).parent / "_kernel_types.py").exists()
  ```

  （按文件里已有的 import 风格补 `Path`、`taifeng_stores`。）

- [ ] **Step 5：门禁全绿后提交。**

  ```bash
  git add -A && git commit -m "chore: 依赖内核 2026.10.1.10，ThreadInfo 改从稳定层引入"
  ```

### Task A2：接管会话时回放顺序不乱

**Files:**
- Modify: `taifeng-stores/src/taifeng_stores/postgres/messages.py`（`PositionClock`）
- Modify: `taifeng-stores/src/taifeng_stores/postgres/message_store.py`（`load_thread`、`append_batch`）
- Test: `taifeng-stores/tests/postgres/test_message_store.py`

背景：位置序号取墙钟纳秒。节点 B 接管节点 A 的会话时，B 的时钟若比 A 慢，B 新写的消息位置会小于 A 写过的，回放时排到前面。作为镜像可接受，作为主存不行。

- [ ] **Step 1：写失败的测试。** 在 `test_message_store.py` 加（复用文件里已有的 `store`/`pg_pool`/`namespace` 夹具与 `_load` 帮助函数；`make_item` 用文件里已有的构造消息的帮助函数，没有就按 `taifeng.user_message(...)` 构造）：

  ```python
  async def test_takeover_with_a_slower_clock_keeps_order(
      pg_pool: asyncpg.Pool, namespace: SqlNamespace
  ) -> None:
      """接管方的时钟比原持有方慢：回放后再写的消息仍排在已有消息之后。"""
      ahead = PostgresMessageStore(pg_pool, namespace=namespace)
      thread_id = await ahead.create_thread(entry_skill_id="helper")
      await ahead.append(taifeng.user_message(thread_id=thread_id, text="接管之前"))

      # 接管方的时钟停在 1970 年：不做处理的话，它写的消息会排到最前面
      behind = PostgresMessageStore(pg_pool, namespace=namespace, now_ns=lambda: 1)
      replayed = [item async for item in await behind.load_thread(thread_id)]
      assert [item.payload["text"] for item in replayed] == ["接管之前"]
      await behind.append(taifeng.user_message(thread_id=thread_id, text="接管之后"))

      texts = [item.payload["text"] for item in await _load(behind, thread_id)]
      assert texts == ["接管之前", "接管之后"]
  ```

  先确认 `taifeng.user_message` 的实际签名（`python -c "import taifeng, inspect; print(inspect.signature(taifeng.user_message))"`），按实际参数写。

- [ ] **Step 2：确认失败。** `uv run pytest tests/postgres/test_message_store.py -k takeover -v` → 失败（构造函数没有 `now_ns`）。

- [ ] **Step 3：实现。**
  - `PositionClock.__init__(self, now_ns: Callable[[], int] = time.time_ns)`；新增 `observe(self, thread_id: str, position: int)` 与 `next(self, thread_ids: Iterable[str]) -> int`：

    ```python
    def observe(self, thread_id: str, position: int) -> None:
        """记下某线程库里已有的最大位置：之后对它的写入一定排在这之后。"""
        if position > self._floors.get(thread_id, -1):
            self._floors[thread_id] = position

    def next(self, thread_ids: Iterable[str] = ()) -> int:
        """下一个位置序号：不小于墙钟，严格大于上一次取值与这些线程已有的最大位置。"""
        floor = max((self._floors.get(tid, -1) for tid in thread_ids), default=-1)
        self._last = max(self._now_ns(), self._last + 1, floor + 1)
        for tid in thread_ids:
            self._floors[tid] = self._last
        return self._last
    ```

    `_floors` 用一个有上限（如 100 000）的 `dict`，超出时按插入顺序淘汰最早的（与 xuanpu 的 `SubmissionIndex` 同一手法），并在 docstring 写明：被淘汰的线程下次写入前一定会先回放（接管必先回放），所以淘汰不影响正确性。
  - `select_items` 之外新增 `select_max_position(conn, table, thread_id) -> int | None`；`load_thread` 在同一连接里取完消息后取最大位置并 `self._clock.observe(...)`。也可以直接用已取回的行——但 `row_to_item` 不带 position，改 `select_items` 多返回一列更省一次查询：给 `select_items` 加 `with_position: bool = False`，或新增 `select_items_with_max_position`。选后者，保持 `PostgresMessageMirror` 的调用不变。
  - `append_batch` 里 `position = self._clock.next(thread_ids)`，仍在第一个 `await` 之前。
  - `PostgresMessageStore.__init__` 加关键字参数 `now_ns: Callable[[], int] | None = None`（只为测试注入时钟；None 用 `time.time_ns`），docstring 写明用途。
  - 更新 `PositionClock` 的类 docstring：去掉「镜像本来就只是视图」那段，改为说明接管时如何靠 `observe` 保序。

- [ ] **Step 4：确认通过**，并跑全量 `uv run pytest -q`。

- [ ] **Step 5：提交。** `git commit -m "fix: 接管会话时以库里已有的最大位置续排，时钟偏差不再打乱回放顺序"`

### Task A3：经 `EnginePool` 用作会话主存

**Files:**
- Test: `taifeng-stores/tests/postgres/test_message_store.py`
- Modify: `taifeng-stores/src/taifeng_stores/postgres/message_store.py`（模块 docstring）

- [ ] **Step 1：写测试。** 用内核稳定层的 `taifeng.SimClient`/`taifeng.SimTurn`（Chat 协议默认能力），两个池先后接同一个线程：

  ```python
  async def test_engine_pool_uses_it_as_the_main_store(
      pg_pool: asyncpg.Pool, namespace: SqlNamespace, tmp_path: Path
  ) -> None:
      """EnginePool 以它为主存：第一个池跑一轮后关闭；第二个池（另一个存储实例）续接同一线程。"""
      skills = write_skills(tmp_path)

      async def pool_with(store: PostgresMessageStore, reply: str) -> taifeng.EnginePool:
          return await taifeng.EnginePool.create(
              skills_dir=skills,
              model_client=taifeng.SimClient(turns=[taifeng.SimTurn(text=reply)]),
              message_store=store,
              thread_directory=store.directory,
              compressors=[],
          )

      first = await pool_with(PostgresMessageStore(pg_pool, namespace=namespace), "第一轮")
      engine = await first.get_or_create(session_id="s1", entry_skill_id="helper")
      thread_id = engine.thread_id
      assert (await run_turn(engine, "你好"))[-1].msg.kind == "turn_completed"
      await first.close()

      second_store = PostgresMessageStore(pg_pool, namespace=namespace)
      second = await pool_with(second_store, "第二轮")
      engine = await second.get_or_create(
          session_id="s1", entry_skill_id="helper", resume_thread_id=thread_id
      )
      assert (await run_turn(engine, "还在吗"))[-1].msg.kind == "turn_completed"
      await second.close()

      texts = [item.payload.get("text") for item in await _load(second_store, thread_id)]
      assert texts == ["你好", "第一轮", "还在吗", "第二轮"]
  ```

  `write_skills`、`run_turn`、`_load` 是文件里已有的帮助函数；`run_turn` 若只接 `AgentEngine` 的写法不同，按它的实际签名调。`EnginePool.close()` 会调 `store.close()`，现有实现不关连接池，正好。

- [ ] **Step 2：跑它。** 预期直接通过（内核已支持）；不通过就按报错修存储实现，不改测试的期望。

- [ ] **Step 3：改模块 docstring。** 「适用范围」改为：可交给 `EnginePool.create(message_store=..., thread_directory=store.directory)` 当会话主存（不给 `storage_dir`）；同一命名空间不要再挂 `PostgresMessageMirror`（写同一张表）；只配 Chat 协议的模型客户端——Responses 协议要 `AtomicBatchMessageStore`，本实现没有，内核在建池时会报错；`list_threads` 的 `item_count` 恒为 0。

- [ ] **Step 4：门禁全绿后提交。** `git commit -m "test: PostgresMessageStore 经 EnginePool 当会话主存，换池续接"`

### Task A4：文档与 ADR

**Files:**
- Create: `taifeng-stores/docs/decisions/0005-main-store-for-engine-pool.md`
- Modify: `taifeng-stores/docs/decisions/README.md`、`README.md`、`docs/architecture/README.md`、`docs/reference.md`（以 Explore 报告为准：架构第 27/74/78 行，接口参考第 90–91 行）

- [ ] **Step 1：写 ADR 0005**（状态 Accepted，日期 2026-10-02）。决策：①签名类型从顶层引入，取代 ADR 0003 的单点模块；②`PostgresMessageStore` 可作 `EnginePool` 主存，用法同 A3 docstring；③接管时以库里最大位置续排，补 ADR 0004 的回放顺序；④不实现原子批量写，理由：唯一使用方 xuanpu 只用 Chat 协议，等有 Responses 需求再加。后果：只配 Chat。
- [ ] **Step 2：更新 README、架构、接口参考**里「`EnginePool` 不接受外部 store」「经单点模块引入」「只适合直接构造 `AgentEngine`」的说法；接口参考里 `PostgresMessageStore` 构造参数加 `now_ns`。
- [ ] **Step 3：`uv run pytest tests/test_docs.py -q` 与门禁全绿后提交。** `git commit -m "docs: ADR 0005 —— PostgresMessageStore 当 EnginePool 主存；同步说明与参考"`

---

## B. taifeng-sandbox

### Task B1：依赖新内核；本机后端返回可持续读写的进程

**Files:**
- Modify: `taifeng-sandbox/pyproject.toml`
- Modify: `taifeng-sandbox/src/taifeng_sandbox/local/process.py`
- Modify: `taifeng-sandbox/src/taifeng_sandbox/local/executor.py`（两处 `spawn_group` 调用）
- Test: `taifeng-sandbox/tests/local/test_streaming.py`（新建）

- [ ] **Step 1：改依赖**，同 A1 Step 1（下限 `taifeng>=2026.10.1.10`，`[tool.uv.sources]` 指 `../taifeng`）。`uv lock && uv sync --extra dev && uv run pytest -q`，记下现状。

- [ ] **Step 2：写失败的测试。**

  ```python
  """本机后端的流式进程：持续写 stdin、按行读 stdout；按组杀进程。"""

  from __future__ import annotations

  import asyncio
  import sys
  from typing import TYPE_CHECKING

  import pytest
  import taifeng

  from taifeng_sandbox import SandboxPolicy
  from taifeng_sandbox.local import create_local_executor

  if TYPE_CHECKING:
      from pathlib import Path

  pytestmark = pytest.mark.skipif(
      sys.platform not in ("darwin", "linux"), reason="只在有本机隔离后端的平台上跑"
  )

  ECHO = "import sys\nfor line in sys.stdin:\n    sys.stdout.write('got:' + line)\n    sys.stdout.flush()\n"


  async def test_stdin_and_stdout_stream(tmp_path: Path) -> None:
      """stdin=True 时可以一行一行地对话。"""
      script = tmp_path / "echo.py"
      script.write_text(ECHO)
      executor = create_local_executor(SandboxPolicy.workspace_write(tmp_path))
      proc = await executor.start(
          taifeng.CommandSpec(
              command=f"/usr/bin/env python3 {script}", shell=False, cwd=str(tmp_path),
              env={"PATH": "/usr/bin:/bin"}, stdin=True,
          )
      )
      assert isinstance(proc, taifeng.StreamingCommandProcess)
      assert proc.stdin is not None and proc.stdout is not None
      for word in ("a", "b"):
          proc.stdin.write(f"{word}\n".encode())
          await proc.stdin.drain()
          assert await asyncio.wait_for(proc.stdout.readline(), 10) == f"got:{word}\n".encode()
      proc.stdin.close()
      assert await asyncio.wait_for(proc.wait(), 10) == 0


  async def test_without_stdin_flag_stdin_is_none(tmp_path: Path) -> None:
      """不要 stdin 的命令读到 EOF，stdin 属性为 None。"""
      executor = create_local_executor(SandboxPolicy.workspace_write(tmp_path))
      proc = await executor.start(
          taifeng.CommandSpec(command="cat", shell=True, cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"})
      )
      assert getattr(proc, "stdin", None) is None
      assert await asyncio.wait_for(proc.communicate(), 10) == (b"", b"")


  async def test_kill_reaches_children_after_the_shell_exits(tmp_path: Path) -> None:
      """shell 先退出、它派生的子进程还占着管道：kill 仍按组杀到（内核 ADR 0108）。"""
      executor = create_local_executor(SandboxPolicy.workspace_write(tmp_path))
      proc = await executor.start(
          taifeng.CommandSpec(
              command="sleep 30 & echo started", shell=True, cwd=str(tmp_path),
              env={"PATH": "/usr/bin:/bin"},
          )
      )
      await asyncio.wait_for(proc.wait(), 10)  # shell 已退出，sleep 还在
      proc.kill()
      out, _ = await asyncio.wait_for(proc.communicate(), 10)  # sleep 被杀，管道关闭
      assert out == b"started\n"
  ```

  macOS seatbelt 下 `/usr/bin/env python3` 若不可用，改用 `sys.executable` 并把 `Path(sys.executable).resolve().parents[1]` 加进策略的可读范围；以实际能跑为准，不跳过断言。

- [ ] **Step 3：确认失败。** `uv run pytest tests/local/test_streaming.py -v`：第一个用例 `isinstance` 失败；第三个用例 `communicate` 超时（kill 在 `returncode` 已有值时直接返回）。

- [ ] **Step 4：实现。**
  - `spawn_group(argv, *, cwd, env, stdin: bool = False)`：`stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL`。两个执行器调用时传 `stdin=spec.stdin`。
  - `ProcessGroup` 照内核 `taifeng/tool/command_executor.py` 的 `_LocalProcess`：加 `_collected` 标志（`communicate` 收完后置位），`kill` 只看 `_collected`，不看 `returncode`；加 `stdin`/`stdout`/`stderr` 三个属性，类型标注用 `taifeng.CommandInput | None` / `taifeng.CommandOutput | None`（放在 `TYPE_CHECKING` 下引入）。类 docstring 写明满足 `StreamingCommandProcess`。

- [ ] **Step 5：确认通过**，全量测试与门禁全绿后提交。`git commit -m "feat: 本机后端支持持续读写的进程；kill 在 shell 退出后仍按组杀"`

### Task B2：线协议 v2 —— 守护进程侧

**Files:**
- Modify: `taifeng-sandbox/src/taifeng_sandbox/daemon/protocol.py`
- Modify: `taifeng-sandbox/src/taifeng_sandbox/daemon/server.py`
- Test: `taifeng-sandbox/tests/daemon/test_protocol.py`、`tests/daemon/test_server_stdin.py`（新建）

- [ ] **Step 1：协议常量。** `PROTOCOL_VERSION = 2`；新增 `METHOD_PROCESS_WRITE = "process/write"`、`METHOD_PROCESS_CLOSE_STDIN = "process/closeStdin"`，进 `__all__`；模块 docstring 的「版本 1」改「版本 2」，并写一句 v2 相对 v1 的变化（`process/start` 的 `stdin` 参数、两个新方法）。`server.py` 里的常量副本同步改。跑 `tests/daemon/test_protocol.py`，按它的检查方式（两边一致）补上新方法名。

- [ ] **Step 2：写失败的测试**（新文件，按 `tests/daemon/` 里已有用例的方式经 `sys.executable -c daemon_source()` 拉起真守护进程、用 `DaemonClient` 直接发请求；照搬已有夹具）：

  ```python
  async def test_process_with_stdin_round_trip(daemon_client: DaemonClient, tmp_path: Path) -> None:
      """stdin=True 启动后，process/write 写入、process/closeStdin 关闭，输出照常经通知回来。"""
      chunks: list[bytes] = []
      exited = asyncio.Event()
      daemon_client.on_notification("process/output", "p1", lambda p: chunks.append(base64.b64decode(p["data"])))
      daemon_client.on_notification("process/exited", "p1", lambda p: exited.set())
      await daemon_client.request("process/start", {
          "processId": "p1", "argv": [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
          "env": {}, "stdin": True,
      })
      await daemon_client.request("process/write", {"processId": "p1", "data": base64.b64encode(b"hi").decode()})
      await daemon_client.request("process/closeStdin", {"processId": "p1"})
      await asyncio.wait_for(exited.wait(), 10)
      assert b"".join(chunks) == b"HI\n"


  async def test_write_to_a_process_without_stdin_is_an_error(daemon_client: DaemonClient) -> None:
      """没要 stdin 的进程不能写：返回 ERROR_INVALID_PARAMS，而不是静默丢弃。"""
      await daemon_client.request("process/start", {"processId": "p2", "argv": ["sleep", "5"], "env": {}})
      with pytest.raises(SandboxRemoteError) as caught:
          await daemon_client.request("process/write", {"processId": "p2", "data": ""})
      assert caught.value.code == protocol.ERROR_INVALID_PARAMS
      await daemon_client.request("process/kill", {"processId": "p2"})


  async def test_write_to_unknown_process(daemon_client: DaemonClient) -> None:
      """未知进程：ERROR_PROCESS_UNKNOWN。"""
      with pytest.raises(SandboxRemoteError) as caught:
          await daemon_client.request("process/write", {"processId": "nope", "data": ""})
      assert caught.value.code == protocol.ERROR_PROCESS_UNKNOWN
  ```

- [ ] **Step 3：确认失败**（未知方法）。

- [ ] **Step 4：实现（`server.py`，保持只用标准库、兼容 3.9）。**
  - `ProcessService.start`：读可选布尔参数 `stdin`（非布尔报 `ERROR_INVALID_PARAMS`），为真时 `stdin=asyncio.subprocess.PIPE`。
  - 新增 `write(params)`：`processId` 必填、`data` 是 base64（可空）；进程不存在 → `ERROR_PROCESS_UNKNOWN`；`proc.stdin is None` → `ERROR_INVALID_PARAMS`；写入后 `await proc.stdin.drain()`；对端已关（`BrokenPipeError`/`ConnectionResetError`）→ `ERROR_IO`。返回 `{"bytesWritten": n}`。
  - 新增 `close_stdin(params)`：进程不存在 → `ERROR_PROCESS_UNKNOWN`；有 stdin 就 `close()`，没有也不算错；返回 `{}`。
  - 方法表登记两个新方法。
  - `_kill_group` 去掉 `returncode is not None` 的提前返回：进程只在监视任务收完两路输出并退出后才从表里移除，表里还在就说明组里可能还有成员，照样按组杀（`ProcessLookupError` 视为已干净）。
  - 宿主写入的顺序由宿主保证（每次写都等到响应才发下一次），在 `write` 的 docstring 里写明。

- [ ] **Step 5：确认通过**，全量与门禁全绿后提交。`git commit -m "feat(daemon): 线协议 v2 —— 进程可接标准输入（process/write、process/closeStdin）"`

### Task B3：宿主侧流式进程，MCP 经守护进程跑通

**Files:**
- Create: `taifeng-sandbox/src/taifeng_sandbox/daemon/streaming.py`
- Modify: `taifeng-sandbox/src/taifeng_sandbox/daemon/executor.py`、`daemon/__init__.py`
- Test: `taifeng-sandbox/tests/daemon/test_streaming_process.py`（新建）

- [ ] **Step 1：写失败的测试。**

  ```python
  MCP_SERVER = r'''
  import json, sys
  for line in sys.stdin:
      msg = json.loads(line)
      if "id" not in msg:
          continue
      if msg["method"] == "initialize":
          result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                    "serverInfo": {"name": "t", "version": "1"}}
      elif msg["method"] == "tools/list":
          result = {"tools": [{"name": "ping", "description": "p", "inputSchema": {"type": "object"}}]}
      elif msg["method"] == "tools/call":
          result = {"content": [{"type": "text", "text": "pong"}]}
      else:
          result = {}
      sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
      sys.stdout.flush()
  '''


  async def test_mcp_stdio_client_through_the_daemon(daemon_client: DaemonClient, tmp_path: Path) -> None:
      """内核的 McpStdioClient 经守护进程执行器起 MCP server，握手、列工具、调用都通。"""
      (tmp_path / "server.py").write_text(MCP_SERVER)
      executor = DaemonCommandExecutor(daemon_client, default_cwd=str(tmp_path))
      client = await taifeng.McpStdioClient.spawn(
          [sys.executable, str(tmp_path / "server.py")], env={}, executor=executor
      )
      try:
          assert [tool["name"] for tool in await client.list_tools()] == ["ping"]
          result = await client.call_tool("ping", {})
          assert result["content"][0]["text"] == "pong"
      finally:
          await client.close()


  async def test_unread_output_over_the_limit_kills_and_fails(daemon_client: DaemonClient) -> None:
      """读取方不读、输出超过上限：杀掉进程，读取方拿到错误而不是被截断的数据。"""
      executor = DaemonCommandExecutor(daemon_client, max_buffer_bytes=1024)
      proc = await executor.start(taifeng.CommandSpec(
          command=f"{sys.executable} -c \"import sys; sys.stdout.write('x' * 100000); sys.stdin.read()\"",
          shell=True, cwd=None, env={}, stdin=True,
      ))
      assert isinstance(proc, taifeng.StreamingCommandProcess)
      await asyncio.wait_for(proc.wait(), 10)
      assert proc.stdout is not None
      with pytest.raises(SandboxError, match="缓冲上限"):
          await proc.stdout.read()
  ```

- [ ] **Step 2：确认失败**（`TypeError`：进程没有流）。

- [ ] **Step 3：实现 `streaming.py`。**
  - `_StreamOutput`（满足 `taifeng.CommandOutput`）：内部 `bytearray`、`asyncio.Event` 表示有新数据、`_eof`、`_error`；`feed(chunk)`、`feed_eof()`、`fail(exc)`；`readline()` 读到 `\n` 或 EOF；`read(n=-1)` 读到 EOF 或 n 字节；有 `_error` 时抛它。未读字节数超过上限时由宿主进程对象调 `fail` 并 kill。
  - `_StreamInput`（满足 `taifeng.CommandInput`）：`write` 只追加到待发缓冲；`drain` 持锁把缓冲整块经 `process/write` 发出，等到响应；`close` 置位并在后台任务里先等未完成的 `drain` 再发 `process/closeStdin`；`is_closing` 返回是否已调 `close`。连接断开或进程已退出后 `drain` 抛 `BrokenPipeError`（让 MCP 客户端按管道断开处理）。
  - `StreamingRemoteProcess(RemoteProcess)`：输出通知喂进两个 `_StreamOutput`；退出通知与连接断开时两路 `feed_eof()`；`communicate()` 读完两路返回；属性 `stdin`/`stdout`/`stderr`。复用 `RemoteProcess` 的登记、kill、退出码逻辑（把 `RemoteProcess` 里处理输出的那一步抽成可覆盖的方法 `_deliver(stream, chunk)`）。
  - `DaemonCommandExecutor.start`：`spec.stdin` 为真时构造 `StreamingRemoteProcess` 并在 `process/start` 参数里带 `"stdin": True`；否则照旧。
  - `daemon/__init__.py` 导出 `StreamingRemoteProcess`。

- [ ] **Step 4：确认通过**，全量与门禁全绿后提交。`git commit -m "feat(daemon): 宿主侧流式进程，内核 McpStdioClient 可经守护进程执行器起 MCP server"`

### Task B4：`DaemonWorkspace` 满足内核 `WorkspaceFS`

**Files:**
- Modify: `taifeng-sandbox/src/taifeng_sandbox/daemon/workspace.py`、`daemon/server.py`（写文件原子化、`NotADirectoryError`）
- Test: `taifeng-sandbox/tests/daemon/test_workspace_fs.py`（新建）

- [ ] **Step 1：写失败的测试。**

  ```python
  async def test_daemon_workspace_is_a_workspace_fs(daemon_client: DaemonClient, daemon_root: Path) -> None:
      """满足内核协议，内核的文件工具可以直接用它。"""
      ws = DaemonWorkspace(daemon_client)
      assert isinstance(ws, taifeng.WorkspaceFS)
      assert ws.root == str(daemon_root.resolve())
      assert ws.resolve("a/../b.txt") == str(daemon_root.resolve() / "b.txt")
      with pytest.raises(taifeng.WorkspacePathError):
          ws.resolve("../outside")

      await ws.write_bytes("notes/a.txt", b"hello")
      info = await ws.metadata("notes/a.txt")
      assert isinstance(info, taifeng.WorkspaceFileInfo) and info.is_file and info.size == 5
      entries = await ws.list_directory("notes")
      assert entries == [taifeng.WorkspaceEntry(name="a.txt", is_directory=False, is_file=True)]
      assert (await ws.metadata("notes/a.txt/deeper")).exists is False

      tool = taifeng.make_file_read_tool(workspace=ws)
      assert tool.name == "file_read"


  async def test_out_of_root_access_raises_workspace_path_error(daemon_client: DaemonClient) -> None:
      """越界一律 WorkspacePathError（不必先调 resolve）。"""
      ws = DaemonWorkspace(daemon_client)
      with pytest.raises(taifeng.WorkspacePathError):
          await ws.read_bytes("/etc/passwd")


  async def test_whole_file_write_is_atomic(daemon_client: DaemonClient, daemon_root: Path) -> None:
      """覆盖写入不会留下半截文件：写入经临时文件再替换，目录里不残留临时文件。"""
      ws = DaemonWorkspace(daemon_client)
      await ws.write_bytes("f.txt", b"old")
      await ws.write_bytes("f.txt", b"new")
      assert (daemon_root / "f.txt").read_bytes() == b"new"
      assert sorted(p.name for p in daemon_root.iterdir()) == ["f.txt"]
  ```

  `daemon_root` 夹具若不存在，在 `tests/daemon/conftest.py` 里按现有拉起守护进程的夹具补一个，返回 `--root` 目录。工具名以 `make_file_read_tool` 实际返回为准。

- [ ] **Step 2：确认失败。**

- [ ] **Step 3：实现。**
  - `DaemonWorkspace.root`：`client.server_info["root"]`（缺失时抛 `SandboxProtocolError`，不给默认值）。
  - `resolve(path)`：同步、纯路径计算——相对路径拼到 root，`posixpath.normpath`，不在 root 之内抛 `taifeng.WorkspacePathError`；返回字符串。docstring 写明不跟随符号链接，守护进程端还会按真实路径再校验。
  - 所有文件方法先 `self.resolve(path)` 再发请求（每个方法自己校验边界）；`_translate` 把 `ERROR_ACCESS_DENIED` 映射为 `taifeng.WorkspacePathError`。
  - `metadata` 返回 `taifeng.WorkspaceFileInfo`，`list_directory` 返回 `list[taifeng.WorkspaceEntry]`；删除 `FileMetadata`、`DirectoryEntry` 两个自有类型并从 `__all__` 去掉（第一版未发布，不留兼容层），全仓 grep 清掉引用。
  - `server.py`：`get_metadata` 遇到 `NotADirectoryError` 也返回 `{"exists": False}`；`write_file` 非追加模式改为在同目录写临时文件（`tempfile.mkstemp(dir=...)`）后 `os.replace`，失败时删掉临时文件；追加模式不变。
  - 模块 docstring 去掉「WorkspaceFS 协议尚未落地」。

- [ ] **Step 4：确认通过**，全量与门禁全绿后提交。`git commit -m "feat(daemon): DaemonWorkspace 满足内核 WorkspaceFS；整文件写入改为原子替换"`

### Task B5：文档与 ADR

**Files:**
- Create: `taifeng-sandbox/docs/decisions/0004-streaming-processes-and-workspace-fs.md`
- Modify: `docs/decisions/README.md`、`docs/architecture/protocol.md`、`docs/reference.md`、`docs/architecture/README.md`、`README.md`

- [ ] **Step 1：ADR 0004**（Accepted，2026-10-02）：线协议 v2 与流式进程（为什么写入要等响应：保序与背压；为什么超上限要杀而不是丢）、`DaemonWorkspace` 对齐内核 `WorkspaceFS`、按组杀与内核 ADR 0108 对齐、整文件写入原子化。后果：v1 宿主与 v2 守护进程不互通（源码随包注入，总是同一版本）。
- [ ] **Step 2：`protocol.md`** 按 v2 改：`process/start` 的 `stdin`、`process/write`、`process/closeStdin` 的请求、响应、错误码，`fs/getMetadata` 的 `NotADirectory` 行为、`fs/writeFile` 的原子性。
- [ ] **Step 3：`reference.md`、架构、README** 去掉「WorkspaceFS 待落地」「非流式」的说法；加 `StreamingRemoteProcess`；`DaemonWorkspace` 的返回类型改为内核类型。
- [ ] **Step 4：`uv run pytest tests/test_docs.py -q` 与门禁全绿后提交。** `git commit -m "docs: ADR 0004 —— 流式进程与工作区文件协议；线协议文档升到 v2"`

---

## C. xuanpu

> C1 之后各任务按顺序做；C4 依赖 A 完成，C6 依赖 B 完成。

### Task C1：依赖新内核

**Files:**
- Modify: `xuanpu/pyproject.toml`（`[tool.uv.sources]` 加 `taifeng = { path = "../taifeng", editable = true }`，注释同 A1）
- Modify: `xuanpu/packages/xuanpu-server/pyproject.toml`（`taifeng>=2026.10.1.10`）

- [ ] **Step 1：改依赖**，`uv lock && uv sync --all-packages`，确认内核版本。
- [ ] **Step 2：跑全量测试。** 逐个查失败原因（升级说明里的破坏性变更：ADR 0073 `apply_patch` 权限、ADR 0108 本机执行器返回包装对象、ADR 0112 stdin 为 DEVNULL、ADR 0069 MCP 分页抛错、`experimental` 里移出的名字发 `DeprecationWarning`）。修平台代码，不改测试期望；确有期望过时的，在提交说明里写清楚为什么改。
- [ ] **Step 3：门禁全绿后提交。** `git commit -m "chore: 依赖内核 2026.10.1.10（开发期从同级目录取）"`

### Task C2：对象存储抽象

**Files:**
- Create: `xuanpu/packages/xuanpu-server/src/xuanpu_server/objectstore/__init__.py`、`base.py`、`local.py`、`s3.py`
- Modify: `xuanpu/packages/xuanpu-server/src/xuanpu_server/config.py`（`ObjectStoreConfig`）、`xuanpu/packages/xuanpu-server/pyproject.toml`（可选依赖 `s3 = ["boto3>=1.34"]`）、`xuanpu/pyproject.toml`（dev 组加 `boto3>=1.34`、`moto[server]>=5.0`、`boto3-stubs[s3]`）
- Test: `xuanpu/packages/xuanpu-server/tests/test_objectstore.py`、`tests/test_config.py`

- [ ] **Step 1：协议（`base.py`）。**

  ```python
  class ObjectNotFoundError(PlatformError):  # 继承 errors.py 里的基类，code="object_not_found"，status=404
      """对象不存在。"""


  @runtime_checkable
  class ObjectStore(Protocol):
      """对象存储：文件内容与业务包归档放在这里（ADR 0009、ADR 0011 决策 8）。

      键是以 ``/`` 分隔的相对路径，由调用方保证以租户开头（规则 3）。实现不做租户判断。
      """

      async def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None:
          """写入（覆盖）一个对象。"""
          ...

      async def get(self, key: str) -> bytes:
          """读出整个对象。不存在抛 ``ObjectNotFoundError``。"""
          ...

      async def exists(self, key: str) -> bool:
          """对象是否存在。"""
          ...

      async def delete(self, key: str) -> None:
          """删除对象；不存在不算错。"""
          ...


  def validate_key(key: str) -> str:
      """键的字符集与形状：非空、不以 / 开头、不含 .. 段与反斜杠、每段不为空。不合法抛 ValueError。"""
  ```

  `errors.py` 里先看基类的写法再定义 `ObjectNotFoundError`。

- [ ] **Step 2：写失败的测试**——同一组用例在两个实现上参数化：

  ```python
  @pytest.fixture(params=["local", "s3"])
  async def objects(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[ObjectStore]:
      """本机目录实现，与经 moto 起的 S3 服务上的 S3 实现。"""
      if request.param == "local":
          yield LocalObjectStore(tmp_path / "objects")
          return
      from moto.server import ThreadedMotoServer

      server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
      server.start()
      host, port = server.get_host_and_port()
      try:
          store = S3ObjectStore(S3Settings(
              endpoint=f"http://{host}:{port}", bucket="xuanpu-test", region="us-east-1",
              access_key_id="test", secret_access_key="test", prefix="p0/", addressing_style="path",
          ))
          await store.create_bucket_for_tests()
          yield store
      finally:
          server.stop()


  async def test_round_trip(objects: ObjectStore) -> None:
      await objects.put("tenants/acme/a.bin", b"\x00\x01")
      assert await objects.exists("tenants/acme/a.bin")
      assert await objects.get("tenants/acme/a.bin") == b"\x00\x01"
      await objects.put("tenants/acme/a.bin", b"v2")
      assert await objects.get("tenants/acme/a.bin") == b"v2"
      await objects.delete("tenants/acme/a.bin")
      await objects.delete("tenants/acme/a.bin")  # 不存在不算错
      assert not await objects.exists("tenants/acme/a.bin")


  async def test_missing_object(objects: ObjectStore) -> None:
      with pytest.raises(ObjectNotFoundError):
          await objects.get("tenants/acme/nope")


  @pytest.mark.parametrize("key", ["", "/abs", "a/../b", "a//b", "a\\b"])
  async def test_bad_keys_are_rejected(objects: ObjectStore, key: str) -> None:
      with pytest.raises(ValueError):
          await objects.put(key, b"x")
  ```

  `create_bucket_for_tests` 不放进产品代码——改成在夹具里直接用 `boto3.client(...).create_bucket(Bucket="xuanpu-test")`。

- [ ] **Step 3：确认失败。**

- [ ] **Step 4：实现。**
  - `LocalObjectStore(root: Path)`：键映射为 `root / key`；写入先写同目录临时文件再 `os.replace`；阻塞 IO 放 `asyncio.to_thread`。
  - `S3Settings`（frozen dataclass）：`endpoint, bucket, region, access_key_id, secret_access_key, prefix="", addressing_style: Literal["virtual","path"]="virtual"`；`__repr__` 不含两个密钥（用 `field(repr=False)`）。
  - `S3ObjectStore(settings)`：构造时建 `boto3.session.Session(...).client("s3", endpoint_url=..., region_name=..., config=botocore.config.Config(s3={"addressing_style": ...}, retries={"max_attempts": 3, "mode": "standard"}))`；四个方法各自 `await asyncio.to_thread(...)`；`get` 遇到 `NoSuchKey`/404 抛 `ObjectNotFoundError`，其他 `ClientError` 原样上抛（不吞）；键加 `prefix`。`boto3` 在函数内引入（可选依赖），没装时抛 `ConfigError("object_store.backend 为 s3 需要安装 xuanpu-server[s3]")`。
  - `config.py`：

    ```python
    class LocalObjectStoreConfig(_Section):
        root: Path | None = None  # None 表示 <data_dir>/objects

    class S3ObjectStoreConfig(_Section):
        endpoint: str
        bucket: str
        region: str
        prefix: str = ""
        access_key_id: str
        secret_access_key: str
        addressing_style: Literal["virtual", "path"] = "virtual"

    class ObjectStoreConfig(_Section):
        """对象存储。``local`` 落本地目录，只用于试用与单机；多实例用 ``s3``（阿里云 OSS 等）。"""
        backend: Literal["local", "s3"] = "local"
        local: LocalObjectStoreConfig = LocalObjectStoreConfig()
        s3: S3ObjectStoreConfig | None = None
        # 校验：backend 为 s3 时必须给 s3 段
    ```

    `ServerConfig` 加 `object_store: ObjectStoreConfig = ObjectStoreConfig()`。`test_config.py` 加：缺 s3 段报错；`${OSS_KEY}` 展开；默认 local。
  - `bootstrap.py` 加 `_object_store(config) -> ObjectStore`（此任务只建对象，C3 再接到业务包）。

- [ ] **Step 5：确认通过**，门禁全绿后提交。`git commit -m "feat: 对象存储抽象（本机目录、S3 兼容），配置新增 object_store 段"`

### Task C3：业务包归档进对象存储，用到时解到本地缓存

**Files:**
- Create: `xuanpu/packages/xuanpu-server/src/xuanpu_server/package_cache.py`
- Modify: `packages.py`、`platform.py`（构造参数 `objects: ObjectStore | None = None`，None 用 `LocalObjectStore(data_dir / "objects")`）、`bootstrap.py`、`controlplane/models.py`（`location` 的 docstring 改为「归档在对象存储里的键」）、`runtime/assembler.py`、`evals.py`（若直接用 `record.location`）
- Test: `xuanpu/packages/xuanpu-server/tests/test_package_storage.py`（新建）；`tests/conftest.py` 的 `World` 加 `objects` 字段

- [ ] **Step 1：写失败的测试。**

  ```python
  async def test_published_archive_lives_in_object_storage(world: Any) -> None:
      """发布后归档在对象存储里，记录的 location 是对象键。"""
      await world.publish()
      record = (await world.platform.packages.list_versions("acme", "support-desk"))[0]
      assert record.location == "tenants/acme/packages/support-desk/1.0.0.zip"
      assert await world.objects.exists(record.location)


  async def test_another_instance_unpacks_from_object_storage(world: Any, tmp_path: Path) -> None:
      """另一个实例（不同的数据目录，同一个控制面与对象存储）拿到同一个包，从对象存储解到自己的缓存。"""
      await world.publish()
      other_dir = (tmp_path / "other-data").resolve()
      other = Platform(
          store=world.platform.store, vault=world.platform.vault, models=world.models,
          sandboxes=LocalSandboxProvider(), data_dir=other_dir,
          root_key_hash=generate_root_key()[1], objects=world.objects,
      )
      resolved = await other.packages.resolve("acme", "support-desk")
      assert resolved.root.is_relative_to(other_dir / "cache" / "packages")
      assert (resolved.root / "xuanpu.yaml").is_file()


  async def test_tampered_archive_is_refused(world: Any, tmp_path: Path) -> None:
      """对象存储里的归档与登记的指纹不符：拒绝解包，报 package_invalid。"""
      await world.publish()
      record = (await world.platform.packages.list_versions("acme", "support-desk"))[0]
      await world.objects.put(record.location, b"not the archive")
      other = Platform(  # 同上，另一个数据目录
          store=world.platform.store, vault=world.platform.vault, models=world.models,
          sandboxes=LocalSandboxProvider(), data_dir=(tmp_path / "o2").resolve(),
          root_key_hash=generate_root_key()[1], objects=world.objects,
      )
      with pytest.raises(PackageInvalidError):
          await other.packages.resolve("acme", "support-desk")


  async def test_concurrent_resolves_unpack_once(world: Any) -> None:
      """缓存被清空后并发解析同一个包只解一次，结果一致。"""
      await world.publish()
      shutil.rmtree(world.data_dir / "cache")
      roots = await asyncio.gather(*[world.platform.packages.resolve("acme", "support-desk") for _ in range(5)])
      assert len({item.root for item in roots}) == 1
  ```

  `conftest.py` 的 `world` 夹具：建 `objects = LocalObjectStore(tmp_path / "objects")` 传给 `Platform`，放进 `World.objects`。

- [ ] **Step 2：确认失败。**

- [ ] **Step 3：实现。**
  - `package_cache.py`：

    ```python
    class PackageCache:
        """业务包的本地解包缓存。缓存可以随时清空，用到时从对象存储重建。"""

        def __init__(self, objects: ObjectStore, root: Path) -> None: ...

        def directory(self, record: PackageVersionRecord) -> Path:
            """<root>/<租户目录>/<名>/<版本>。租户目录用 runtime.assembler.tenant_directory。"""

        async def ensure(self, record: PackageVersionRecord) -> Path:
            """缓存里有（目录下有 .digest 文件且内容等于 record.digest）就直接返回；
            否则在按 (租户, 名, 版本) 的锁里：取归档 → 核对 archive_digest → 解到暂存目录
            → 写 .digest → 改名到位。指纹不符抛 PackageInvalidError。"""

        def adopt(self, record: PackageVersionRecord, unpacked: Path) -> Path:
            """发布时已经解过、校验过的暂存目录直接转正进缓存（省一次下载）。"""
    ```

    `tenant_directory` 从 `runtime/assembler.py` 移到 `ids.py` 或 `tenancy.py`（被两处用），原处改为引入。
  - `PackageRegistry.__init__(store, audit, *, data_dir, objects, now)`：暂存目录仍在 `data_dir/packages/.staging`；`_commit` 改为：查重 → `objects.put(key, archive, content_type="application/zip")` → `cache.adopt(...)` → `store.add_package_version(record)`，登记失败时 `objects.delete(key)` 并删缓存目录再上抛。`key = f"tenants/{tenant_id}/packages/{name}/{version}.zip"`。
  - `ResolvedPackage` 改为 `record`、`manifest`、`root: Path` 三个字段（`root` 不再从 `location` 推出）；`resolve` 里 `root = await self._cache.ensure(record)`。
  - 全仓 grep `ResolvedPackage(` 与 `.location` 改掉。`docs/` 里提到「业务包解在数据目录 packages/ 下」的地方在 C10 统一改。

- [ ] **Step 4：确认通过**，全量与门禁全绿后提交。`git commit -m "feat: 业务包归档进对象存储，各实例用到时解到本地缓存并核对指纹"`

### Task C4：会话主存进 Postgres

**Files:**
- Modify: `runtime/assembler.py`（`StateBackends`、`_create_pool`）、`bootstrap.py`（`PostgresStateBackends`）
- Test: `xuanpu/packages/xuanpu-server/tests/test_multi_instance.py`（新建）

- [ ] **Step 1：写失败的测试（M0 验收）。** 需要 Postgres（用 conftest 的 `postgres_dsn` 夹具；没有就跳过）。两个 `Platform`：共享同一个 `PostgresControlPlane`、同一个 `PostgresStateBackends`（同一 schema）、同一个 `LocalObjectStore`，**数据目录不同**：

  ```python
  async def test_second_instance_takes_over_without_a_shared_data_dir(
      two_instances: Any, step: Any, read_turn: Any
  ) -> None:
      """A 跑一轮后放手；B 用另一个数据目录接手同一会话，模型看得到上一轮。"""
      a, b, models = two_instances
      await a.publish()
      session = await a.platform.sessions.create(a.member, package="support-desk")
      models.script(step(text="第一轮"))
      await read_turn(await a.platform.sessions.send_message(a.member, session.session_id, "记住 42"))
      await a.platform.sessions.shutdown()  # 放手：释放引擎与租约

      models.script(step(text="第二轮"))
      events = await read_turn(await b.platform.sessions.send_message(b.member, session.session_id, "刚才是几"))
      assert events[-1].msg.kind == "turn_completed"
      assert "记住 42" in models.last_prompt()
      assert not (b.data_dir / "threads").exists()
  ```

  `two_instances` 夹具写在这个测试文件里：照 `world` 夹具的做法建租户、凭据、密钥（只建一次，两个平台共用控制面），返回两个带 `platform`、`member`、`data_dir`、`publish` 的小对象与共享的 `ScriptedModels`。如果 conftest 里的 `World` 构造逻辑可以抽成函数复用，就抽出来，避免复制。

- [ ] **Step 2：确认失败**（B 找不到线程：主存还在 A 的数据目录里）。

- [ ] **Step 3：实现。**
  - `StateBackends` 加 `message_store: Callable[[], taifeng.MessageStore] | None = None`，docstring：给了它，引擎池以它为会话主存，不再用数据目录；`thread_directory` 应与之配套。
  - `_create_pool`：`states.message_store` 不为 None 时传 `message_store=states.message_store()`，不传 `storage_dir`、不建 `threads` 目录；否则照旧。
  - `PostgresStateBackends.for_tenant`：建 `make_store = lambda: PostgresMessageStore(self._pool, namespace=namespace)`；返回 `StateBackends(thread_directory=PostgresThreadDirectory(...), message_store=make_store)`，**不再给 `index_hook`**（镜像与主存写同一张表）。类 docstring 改为「线程目录与会话主存放进 Postgres」。
  - `config.py` 的 `StateConfig` docstring 与字段说明同步。

- [ ] **Step 4：确认通过**，全量与门禁全绿后提交。`git commit -m "feat: 状态面用 Postgres 时会话主存直接进库，多实例不再共享数据目录"`

### Task C5：停止生成

**Files:**
- Modify: `runtime/sessions.py`（`cancel`）、`api/app.py`、`api/schemas.py`（`CancelBody`）、`xuanpu-sdk-py/src/xuanpu_sdk/client.py`（`sessions.cancel`）、`xuanpu-console/src/xuanpu_console/static/app.js`（流式时显示「停止」）
- Test: `xuanpu/packages/xuanpu-server/tests/test_cancel.py`（新建）、`xuanpu-sdk-py/tests/test_sdk.py`、`xuanpu-console/tests/test_static.py`

- [ ] **Step 1：写失败的测试。** 用一个会卡住的模型步骤：`Step` 加可选字段 `wait: asyncio.Event | None = None`，`_ScriptedSession.stream` 在产出文本之后、`completed` 之前 `await step.wait.wait()`（只为测试；默认 None 不等）。

  ```python
  async def test_cancel_stops_the_running_turn(world: Any, step: Any) -> None:
      """在途的一轮被停下：以 turn_completed（end_reason=cancelled）收尾，会话还能继续用。"""
      await world.publish()
      session = await world.platform.sessions.create(world.member, package="support-desk")
      gate = asyncio.Event()
      world.models.script(step(text="写到一半", wait=gate), step(text="下一轮"))
      turn = await world.platform.sessions.send_message(world.member, session.session_id, "写长文")
      events: list[Any] = []

      async def read() -> None:
          async for event in turn.stream.events():
              events.append(event)

      reader = asyncio.ensure_future(read())
      await wait_until(lambda: any(e.msg.kind == "assistant_text" for e in events))
      cancelled = await world.platform.sessions.cancel(world.member, session.session_id)
      assert cancelled == [turn.submission_id]
      await asyncio.wait_for(reader, 10)
      assert events[-1].msg.kind == "turn_completed"
      assert events[-1].msg.data["end_reason"] == "cancelled"

      done = await read_turn_fixture(await world.platform.sessions.send_message(world.member, session.session_id, "再来"))
      assert done[-1].msg.data.get("end_reason") != "cancelled"
      kinds = [r.kind for r in await world.platform.store.query_audit("acme", AuditQuery(session_id=session.session_id, limit=1000))]
      assert "turn_cancel_requested" in kinds


  async def test_cancel_with_nothing_running(world: Any) -> None:
      """没有在途对话：返回空列表，不报错。"""
      await world.publish()
      session = await world.platform.sessions.create(world.member, package="support-desk")
      assert await world.platform.sessions.cancel(world.member, session.session_id) == []


  async def test_cancel_unknown_submission(world: Any) -> None:
      """指定的提交不在途：invalid_request。"""
      ...（按上面的写法建会话，调用 cancel(submission_id="sub_nope")，断言 InvalidRequestError）


  async def test_cancel_over_http(world: Any, step: Any) -> None:
      """POST /v1/sessions/{id}/cancel 与 SDK 的 sessions.cancel。"""
      ...（照 test_pending_and_replay.py 的 ASGITransport 写法；在另一个任务里读事件流，用 SDK 调 cancel，断言返回的列表与最后一条事件）
  ```

  `wait_until` 写成测试文件里的小帮助函数（轮询 + 超时）。上面两个 `...` 的用例要写成完整代码，不留省略。被卡住的模型步骤在取消时会收到 `CancelledError`——`_ScriptedSession.stream` 不要吞掉它。

- [ ] **Step 2：确认失败。**

- [ ] **Step 3：实现。**
  - `SessionManager.cancel(principal, session_id, submission_id: str | None = None) -> list[str]`：
    1. `record = await self.get(principal, session_id)`；
    2. `live = self._live.get(key)`：为 None 时查租约——`self._store.get_lease(...)`（控制面若没有读租约的方法，加一个，内存与 Postgres 两个实现都加，契约测试里加用例）；别的实例持有且未过期 → 抛 `SessionBusyError(owner)`；否则返回 `[]`；
    3. 目标 = `[submission_id]`（不在 `live.in_flight` 里抛 `InvalidRequestError`）或 `sorted(live.in_flight)`；
    4. 逐个 `await live.engine.submit(taifeng.CancelTurn(submission_id=sid))`；
    5. 有目标时记控制面审计 `turn_cancel_requested`，`{"submission_ids": targets}`；返回目标列表。
  - `POST /v1/sessions/{session_id}/cancel`，body `CancelBody(submission_id: str | None = None)`，响应 `{"cancelled": [...]}`。
  - SDK：`async def cancel(self, session_id: str, *, submission_id: str | None = None) -> list[str]`。
  - 控制台：流式输出时把「发送」按钮换成「停止」，点击调 `POST .../cancel`；`test_static.py` 若检查页面里出现的接口路径，补上。

- [ ] **Step 4：确认通过**，全量与门禁全绿后提交。`git commit -m "feat: 停止生成 —— 取消会话里在途的一轮，接口、客户端、控制台"`

### Task C6：stdio 连接器进沙盒

**Files:**
- Modify: `runtime/sandbox.py`（`SandboxProvider.open_connectors`）、`runtime/connectors.py`（`ConnectorSet` 接收执行器）、`runtime/assembler.py`（按需开连接器沙盒、随运行时关闭）、`config.py`（`SandboxConfig.connector_readable_roots`、`connector_image`）、`bootstrap.py`
- Modify: `tests/conftest.py`（假 MCP server 的调用日志写到 `$HOME`；`World.calls()` 从连接器工作目录读；本机沙盒给 `connector_readable_roots=(Path(sys.prefix), Path(sys.base_prefix))`）
- Test: `tests/test_connector_sandbox.py`（新建）

- [ ] **Step 1：改测试夹具，确认现有用例仍过。** 假 MCP server 里 `CALL_LOG` 改为 `os.path.join(os.environ["HOME"], "mcp-calls.jsonl")`，清单里去掉 `CALL_LOG` 环境变量；`World.calls()` 改为读 `data_dir / "connectors"` 下唯一的 `mcp-calls.jsonl`（`rglob`）。此时实现还没改（连接器仍在平台节点上，`HOME` 是清单没给的——所以先在 `ConnectorSet._environment` 之外不动，**这一步预期部分用例失败**，作为 Step 2 的失败基线）。

- [ ] **Step 2：写失败的测试。**

  ```python
  async def test_connector_runs_inside_its_sandbox(world: Any, step: Any, read_turn: Any) -> None:
      """连接器进程的 HOME 是它的工作目录；写工作目录之外被拒绝；宿主环境变量不进去。"""
      await world.publish()
      session = await world.platform.sessions.create(world.member, package="support-desk")
      world.models.script(step(calls=[("c1", "mcp__crm__search_customer", {"q": "x"})]), step(text="ok"))
      await read_turn(await world.platform.sessions.send_message(world.member, session.session_id, "查"))
      call = world.calls()[0]
      assert call["home"].startswith(str(world.data_dir / "connectors"))
      assert call["can_write_outside"] is False
      assert call["inherited"] == []


  async def test_no_connector_sandbox_without_stdio_connectors(...) -> None:
      """清单里只有 HTTP 连接器：不开连接器沙盒（data_dir/connectors 不存在）。"""
      ...（用 test_http_connector.py 里只含 HTTP 连接器的样例包，跑一轮，断言目录不存在）
  ```

  假 MCP server 的 `tools/call` 回包里加 `"home": os.environ.get("HOME")`、`"can_write_outside"`（尝试写 `os.path.join(os.path.dirname(os.environ["HOME"]), "escape.txt")`，捕获 `OSError` 记 False）。

- [ ] **Step 3：实现。**
  - `SandboxProvider` 协议加：

    ```python
    async def open_connectors(self, package: ResolvedPackage, workspace: Path) -> SandboxHandle:
        """为业务包的 stdio 连接器准备沙盒：只读业务包目录，只写 ``workspace``，允许出网。"""
    ```

  - `LocalSandboxProvider(connector_readable_roots: tuple[Path, ...] = ())`：`SandboxPolicy.workspace_only(workspace, readable_roots=(package.root, *roots), network=True)`。
  - `DockerSandboxProvider(..., connector_image: str | None = None)`：与 `open` 相同，但镜像用 `connector_image or image`、`network=True`、标签加 `"xuanpu.role": "connectors"`。
  - `SandboxHandle.process_env()` 已给出 `HOME`/`TMPDIR`/`WORKSPACE`；连接器环境 = `DEFAULT_BASE_ENV` + 这三项（取 `workspace_inside`）+ 清单变量。
  - `ConnectorSet.__init__` 加 `executor: taifeng.CommandExecutor | None`、`workspace_env: Mapping[str, str]`；`_start_stdio` 调 `McpStdioClient.spawn(list(spec.command), env=env, cwd=<业务包目录在沙盒里的路径>, executor=self._executor, request_timeout_seconds=...)`。`executor` 为 None 且遇到 stdio 连接器时报 `RuntimeUnavailableError`（不退回在节点上直接起）。
  - `RuntimeAssembler._assemble`：清单里有 stdio 连接器才 `open_connectors(package, self._scoped("connectors", package))`，句柄存进 `TenantRuntime.connector_sandbox`，`close()` 时在连接器之后关闭。
  - 配置：`SandboxConfig.connector_readable_roots: tuple[Path, ...] = ()`（须为绝对路径）、`connector_image: str | None = None`；`bootstrap._sandboxes` 传进去。
  - `connectors.py` 模块 docstring 去掉「进程跑在平台节点上」。

- [ ] **Step 4：确认通过**，全量与门禁全绿后提交。`git commit -m "feat: stdio 连接器进程在单独的连接器沙盒里运行（只读业务包、只写自己的工作目录、可出网）"`

### Task C7：远端 MCP 连接器

**Files:**
- Modify: `xuanpu-spec/src/xuanpu_spec/manifest.py`（`ConnectorSpec`）、`xuanpu-spec/src/xuanpu_spec/validate.py`（工具名前缀检查）、`xuanpu-spec/pyproject.toml`（版本号按现有规则递增）
- Create: `xuanpu-server/src/xuanpu_server/runtime/remote_mcp.py`
- Modify: `runtime/connectors.py`（`start` 分派到 `_start_remote`）
- Test: `xuanpu-spec/tests/test_manifest.py`、`xuanpu-server/tests/test_remote_mcp.py`（新建）

- [ ] **Step 1：规范的失败测试。**

  ```python
  def test_remote_connector_shape() -> None:
      spec = ConnectorSpec.model_validate({
          "name": "docs", "transport": "remote", "url": "https://mcp.example.com/mcp",
          "auth": {"type": "bearer", "secret": "docs-token"},
      })
      assert spec.tool_name("search") == "mcp__docs__search"
      assert spec.secret_names == ("docs-token",)


  @pytest.mark.parametrize("extra", [{"command": ["x"]}, {"openapi": "a.yaml"}, {"env": {"A": {"value": "1"}}}, {"baseUrl": "https://x"}])
  def test_remote_connector_rejects_other_transport_fields(extra: dict[str, Any]) -> None:
      with pytest.raises(ValidationError):
          ConnectorSpec.model_validate({"name": "d", "transport": "remote", "url": "https://x/mcp", **extra})


  def test_remote_connector_needs_an_http_url() -> None:
      for url in (None, "ftp://x", "mcp.example.com"):
          with pytest.raises(ValidationError):
              ConnectorSpec.model_validate({"name": "d", "transport": "remote", "url": url})
  ```

  `validate.py` 的工具名前缀检查：`remote` 与 `stdio` 一样用 `mcp__`。加一条包级校验用例：skill 的 `tool_names` 引用 `mcp__docs__search` 且连接器是 `remote` 时通过。

- [ ] **Step 2：实现规范。** `transport: Literal["stdio", "http", "remote"]`；新增 `url: str | None = None`；`_shape` 校验：`remote` 必须有 `url` 且以 `http://`/`https://` 开头，不得有 `command`/`env`/`openapi`/`baseUrl`；`auth` 允许用于 `http` 与 `remote`；`url` 只用于 `remote`。`tool_prefix`：`stdio`、`remote` → `mcp`，`http` → `api`。类 docstring 补第三种传输。`timeout_seconds` 的说明改为「`http` 与 `remote` 传输下单次调用的超时」。重新生成 JSON Schema 的测试若有快照，同步更新。

- [ ] **Step 3：运行时的失败测试。** 在 `test_remote_mcp.py` 写一个最小的 MCP Streamable HTTP 服务（FastAPI/Starlette 应用：`POST /mcp` 收 JSON-RPC，按 `initialize`/`notifications/initialized`/`tools/list`/`tools/call` 回 `application/json`；`GET /mcp` 回 405；记下每次请求的 `Authorization` 头），用 conftest 的 `serve` 夹具跑在本机端口上；样例业务包用 `remote` 连接器指向它，`auth` 用 bearer + 密钥 `docs-token`：

  ```python
  async def test_remote_mcp_tools_go_through_the_gate(world: Any, serve: Any, step: Any, read_turn: Any) -> None:
      """远端 MCP 的工具与 stdio 一样过闸：读操作放行并留过闸记录；凭据按名字注入。"""
      base = await serve(fake_remote_mcp)
      await publish_remote_package(world, url=f"{base}/mcp")
      await world.platform.put_secret(world.admin, "docs-token", "tok-123")
      session = await world.platform.sessions.create(world.member, package="docs-desk")
      world.models.script(step(calls=[("c1", "mcp__docs__search", {"q": "合同"})]), step(text="找到了"))
      events = await read_turn(await world.platform.sessions.send_message(world.member, session.session_id, "找合同"))
      assert events[-1].msg.kind == "turn_completed"
      assert fake_remote_mcp.state.auth_headers and set(fake_remote_mcp.state.auth_headers) == {"Bearer tok-123"}
      gate = [r for r in await world.platform.store.query_audit("acme", AuditQuery(source="gate", limit=100))]
      assert gate[-1].data["transport"] == "remote" and gate[-1].data["decision"] == "allowed"
      assert "tok-123" not in json.dumps([r.data for r in gate], ensure_ascii=False)
  ```

  再加一条：密钥未配置时会话起不来（`SecretMissingError`），与 HTTP 连接器一致。

- [ ] **Step 4：实现运行时。**
  - `remote_mcp.py`：

    ```python
    class VaultAuthTransport(httpx.AsyncBaseTransport):
        """每次请求发出时从保管库取凭据加到请求头；客户端对象里不长期持有明文（规则 2）。"""

        def __init__(self, inner: httpx.AsyncBaseTransport, *, auth: HttpAuth | None, tenant_id: str, vault: SecretVault) -> None: ...

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if self._auth is not None:
                value = await self._vault.get(self._tenant_id, self._auth.secret)
                request.headers[header_name] = f"Bearer {value}" if bearer else value
            return await self._inner.handle_async_request(request)

        async def aclose(self) -> None:
            await self._inner.aclose()
    ```

    `HttpAuth` 的头名与格式与 `http_api.py` 里 HTTP 连接器的注入方式保持一致（先读 `http_api.py`，抽出共用的「按 HttpAuth 生成头」函数，两处都用它）。
  - `ConnectorSet._start_remote(spec)`：组装时先 `await self._vault.get(...)` 确认密钥已配置（取值不保留）；`client = await asyncio.wait_for(taifeng.McpHttpClient.connect(spec.url, transport=VaultAuthTransport(httpx.AsyncHTTPTransport(), ...), request_timeout_seconds=min(spec.timeout_seconds, self._call_timeout)), timeout=self._startup_timeout)`；失败统一转 `RuntimeUnavailableError`（提示里只带异常类型）；列工具与白名单检查复用 `_start_stdio` 里的逻辑（抽成 `_list_mcp_tools(client, spec)` 两处共用）；客户端放进 `self._mcp_clients`（类型改为 `list[taifeng.McpClient]`）。
  - 闸门的 `transport` 字段写 `remote`；`build_tools` 里超时：`http` 与 `remote` 都取 `min(spec.timeout_seconds, call_timeout)`。

- [ ] **Step 5：确认通过**，全量与门禁全绿后提交。`git commit -m "feat: 远端 MCP 连接器（transport: remote），凭据在请求发出时现取"`

### Task C8：过闸审计补齐

**Files:**
- Modify: `runtime/sessions.py`（`resume`）、`runtime/events.py`（`SubmissionIndex` docstring）
- Test: `tests/test_platform_governance.py`

- [ ] **Step 1：写失败的测试。**

  ```python
  async def test_approved_call_is_attributed_to_the_resume_submission(world: Any, step: Any, read_turn: Any) -> None:
      """审批后执行的连接器调用，过闸记录带触发重跑的那次 resume 的提交标识。"""
      await world.publish()
      session = await world.platform.sessions.create(world.member, package="support-desk")
      world.models.script(step(calls=[("c1", "mcp__crm__update_ticket", {"id": "T-1"})]), step(text="好了"))
      await read_turn(await world.platform.sessions.send_message(world.member, session.session_id, "改"))
      request_id = (await world.platform.sessions.pending(world.member, session.session_id))[0]["request_id"]
      resumed = await world.platform.sessions.resume(world.member, session.session_id, {request_id: {"granted": True}})
      await read_turn(resumed)
      gate = await world.platform.store.query_audit("acme", AuditQuery(source="gate", limit=100))
      assert [r.data["submission_id"] for r in gate if r.data["decision"] == "allowed"] == [resumed.submission_id]


  async def test_rejected_call_leaves_a_gate_record(world: Any, step: Any, read_turn: Any) -> None:
      """人工拒绝：补一条 decision=rejected 的过闸记录，带工具名与 call_id，参数只有摘要。"""
      ...（同上到拿 request_id；resume 传 {"granted": False}；查 source=gate 的记录，断言恰有一条 decision == "rejected"、detail == "rejected_by_user"、tool == "update_ticket"、connector == "crm"、call_id == "c1"、有 arguments_digest、没有 args 原文）
  ```

  `...` 处写完整代码。

- [ ] **Step 2：确认**第一个用例可能已通过（内核已带 `submission_id`）——通过就保留作回归；第二个失败。

- [ ] **Step 3：实现。** `resume` 里在提交 `Resume` 之前取 `await self.pending(principal, session_id)`，对 `granted` 为假的请求：从 `detail` 取工具全名（先打印一条真实的 pending 项看字段，按实际字段取 `target`/`call_id`/`args`），按 `mcp__<连接器>__<工具>` / `api__…` 拆出连接器与工具，在运行时的 `tool_effects` 里查得到才记（只对连接器工具补）；调用 `self._audit.gate(tenant, "connector_call", {..., "decision": "rejected", "detail": "rejected_by_user", "arguments_digest": digest_arguments(args), "submission_id": <被拒那一轮的提交标识>}, session_id=...)`。字段集合与 `ConnectorGate._record` 一致（`transport` 从 `tool_prefix` 推：`mcp` 前缀查清单里该连接器的传输）。`SubmissionIndex` docstring 去掉「审批通过后内核重跑工具的那条路径不带 submission 标识」，改为线程标识只作兜底（内核自己发起的重跑不带）。

- [ ] **Step 4：确认通过**，全量与门禁全绿后提交。`git commit -m "feat: 人工拒绝的连接器调用补过闸记录；审批后执行的调用归到 resume 那一轮"`

### Task C9：运维收尾 —— openapi、停机时限、改配额、指标

**Files:**
- Modify: `api/app.py`、`api/schemas.py`、`platform.py`（`set_tenant_quota`）、`config.py`（`MetricsConfig`）、`xuanpu-cli/src/xuanpu_cli/main.py`（uvicorn 收尾时限）、`xuanpu-sdk-py/src/xuanpu_sdk/client.py`（`admin.set_quota`）
- Create: `xuanpu-server/src/xuanpu_server/metrics.py`
- Modify: `runtime/sessions.py`、`runtime/events.py`、`runtime/connectors.py`、`audit.py`（打点）、`xuanpu-server/pyproject.toml`（`prometheus_client>=0.20`）
- Test: `tests/test_http_api.py`、`tests/test_platform_governance.py`、`tests/test_metrics.py`（新建）、`xuanpu-cli/tests/test_serve.py`

- [ ] **Step 1：openapi。** 测试：`GET /openapi.json` → 404。实现：`FastAPI(..., openapi_url=None)`。
- [ ] **Step 2：停机收尾时限。** 先读 `main.py` 第 139–160 行的 `serve`；测试（`test_serve.py`）断言构造出的 `uvicorn.Config` 的 `timeout_graceful_shutdown == config.shutdown_drain_seconds + 5`（把构造 `uvicorn.Config` 抽成可单测的函数 `uvicorn_config(app, config)`）。
- [ ] **Step 3：改配额。** 测试：平台管理员 `PUT /v1/admin/tenants/acme/quota` `{"max_daily_tokens": 1000}` 后 `GET /v1/admin/tenants` 里生效；租户管理员调用 → 403；不存在的租户 → 404；审计有 `tenant_quota_changed`；SDK `admin.set_quota("acme", max_daily_tokens=1000)`。实现：`Platform.set_tenant_quota(principal, tenant_id, quota: QuotaLimits) -> Tenant`（`require_platform_admin`，`store.update_tenant`）；`schemas.QuotaBody` 复用新建租户时的配额字段。
- [ ] **Step 4：指标。** 测试：

  ```python
  async def test_metrics_need_their_own_token(world_with_metrics: Any) -> None:
      """配了抓取令牌才开；令牌不对 401；内容是 Prometheus 文本，有第一批指标。"""
      ...（ASGITransport；无令牌 401、错令牌 401、对令牌 200；跑一轮对话后 xuanpu_turns_total{outcome="completed"} 为 1，
          xuanpu_connector_calls_total 有 transport/decision 标签；返回体里不出现 tenant 字样）


  async def test_metrics_disabled_without_a_token(world: Any) -> None:
      """没配 metrics.token_hash：/metrics 是 404。"""
  ```

  实现：`metrics.py` 里 `PlatformMetrics`——构造时建自己的 `CollectorRegistry`，注册设计第 3.8 节表里的六个指标；提供 `turn_ended(outcome)`、`connector_call(transport, decision)`、`audit_write_failed()`，两个 gauge 用回调（`set_function`）从 `SessionManager`/`RuntimeAssembler` 取当前值；`render() -> bytes`（`generate_latest(registry)`）。`Platform` 构造参数加 `metrics_token_hash: str | None = None`，持有 `self.metrics`，把它传给会话、事件枢纽、闸门、审计写入口打点（`outcome` 由终结事件推：`turn_suspended` → suspended，`turn_completed` 且 `end_reason == "cancelled"` → cancelled，`turn_completed` → completed，`turn_failed` → failed）。`config.py`：`MetricsConfig(token_hash: str | None = Field(default=None, pattern=_SHA256_HEX))`，`ServerConfig.metrics`。`/metrics` 路由：未配 → 404；`Authorization: Bearer <令牌>` 用 `tenancy.verify_root_key` 同款的定长比较校验摘要；不对 → 401；对 → `Response(render(), media_type=CONTENT_TYPE_LATEST)`。命令行 `keygen` 若只能生成根凭据，加一个 `--kind metrics` 或说明复用根凭据的生成方式（先读 `keygen` 的实现再定，选改动最小的）。
- [ ] **Step 5：确认通过**，全量与门禁全绿后提交。`git commit -m "feat: 指标端点、修改租户配额、停机收尾时限；关掉 /openapi.json"`

### Task C10：文档、ADR、缺口清单、样例

**Files:**
- Create: `xuanpu/docs/decisions/0013-connector-sandbox-and-remote-mcp.md`
- Modify: `docs/decisions/README.md`、`docs/architecture/README.md`、`docs/architecture/http-api.md`、`docs/architecture/package-spec.md`、`docs/architecture/kernel-seam-backlog.md`、`docs/guides/operations.md`、`docs/guides/writing-packages.md`、`docs/guides/integrating.md`、`docs/guides/audit-and-usage.md`、`docs/guides/quickstart.md`（若涉及数据目录）、`examples/xuanpu.yaml`、`examples/xuanpu.trial.yaml`、`packages/*/README.md`、`README.md`
- Test: `packages/xuanpu-cli/tests/test_examples_and_docs.py`

- [ ] **Step 1：ADR 0013**（Accepted，2026-10-02）：stdio 连接器进单独的连接器沙盒（只读业务包、只写工作目录、可出网；为什么与脚本沙盒分开：出网需求不同）；新增 `remote` 传输（为什么不等 MCP 网关：ADR 0006 继续暂缓，远端 server 直连即可；凭据每次请求现取）。
- [ ] **Step 2：架构总览**：状态面（会话主存进库、对象存储、本地盘只剩缓存）；「已知限制」删掉已解决的六条（连接器不在沙盒、会话主存是本地文件、不能单独取消、配额不能改、没有指标、拒绝无过闸记录、`/openapi.json`），保留其余；加一条「业务包缓存不自动淘汰，可随时清空」。
- [ ] **Step 3：HTTP 接口**：`POST /v1/sessions/{id}/cancel`、`PUT /v1/admin/tenants/{id}/quota`、`GET /metrics`；去掉 `/openapi.json`。
- [ ] **Step 4：业务包规范**：`remote` 传输的字段表、工具名前缀、示例。
- [ ] **Step 5：运维手册**：组成图去掉「共享数据目录」、加对象存储；配置表加 `object_store`、`metrics`、`sandbox.connector_readable_roots`、`sandbox.connector_image`；「多实例」第 3 条改为「各实例的数据目录各自独立，只放缓存」；监控一节写 `/metrics` 与抓取令牌；租户管理加改配额；升级一节写「从 v1 升级：状态面为 postgres 时，镜像已在库里，切换后直接从库续接」。命令与输出要实际跑过（按 `docs-before-moving-on` 的要求：在本机起一个试用实例跑 `curl` 取 `/metrics` 与 `cancel`，把真实输出贴进文档）。
- [ ] **Step 6：编写业务包**：连接器沙盒的约束（工作目录、出网、镜像里要有运行时）、远端 MCP 写法。**对接手册**：停止生成。**审计与用量**：`decision=rejected` 的过闸记录、`turn_cancel_requested`。
- [ ] **Step 7：缺口清单**：删掉内核已落地的第 1、3、4、5、6、7、8、9、10、11、12、13、14 项；第 2 项收窄为「会话日志后端在实验层，平台不用（ADR 0011 决策 2）」；保留 15–17；编号不重排；文首的依据版本改为 `2026.10.1.10`。
- [ ] **Step 8：样例配置**：`examples/xuanpu.yaml`（生产形态）加 `object_store: {backend: s3, s3: {endpoint: https://oss-cn-hangzhou.aliyuncs.com, bucket: ..., region: cn-hangzhou, access_key_id: ${OSS_ACCESS_KEY_ID}, secret_access_key: ${OSS_ACCESS_KEY_SECRET}}}` 与 `metrics.token_hash: ${XUANPU_METRICS_TOKEN_HASH}`；试用配置保持 `local`。样例校验测试跟着过。
- [ ] **Step 9：全量测试与门禁全绿后提交。** `git commit -m "docs: P0 底座升级 —— ADR 0013、架构与手册同步、缺口清单收窄"`

---

## 收尾

- [ ] 三个仓各跑一遍门禁，贴出命令与关键输出。
- [ ] 更新记忆：`platform-v1-foundation.md`（新增 `feat/p0-base` 分支）、`kernel-seam-backlog-pointer.md`（清单剩下的项）。
- [ ] 向用户汇报：做了什么、设计里 ★ 的判断、仍待点头的两项操作（内核发版；三仓推送合入发布——现在多了 `feat/p0-base`）。
