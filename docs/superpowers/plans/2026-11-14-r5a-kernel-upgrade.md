# R5-A 内核升级与稳定层缺口实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans。

**Goal:** 平台运行在 taifeng 2026.11.14.x，且只经稳定层拿到执行身份。

**Architecture:** 见 [R5-A 设计](../specs/2026-11-14-r5a-kernel-upgrade-design.md)。先提需求、再升级、最后才开采集。

**Tech Stack:** uv、taifeng、taifeng-sandbox、taifeng-stores。

---

## A0：内核侧需求与提升

**Files**：Modify `docs/architecture/kernel-seam-backlog.md`（新增三项）；在 taifeng 仓按其流程提交（能力契约 → 实现 → `tests/public_api_snapshot.txt` → architecture 同步）。

- [x] 在 backlog 记缺口（第 31 项）：执行身份与 `execution_updated` 数据键只在实验层。瀑布计算不提升，平台自算（`S/tracing/waterfall.py`）。
- [x] taifeng 仓内完成提升并发版（遵守其 worktree 约定与真实回归红线，基础层变更须更新 `docs/real-llm-ledger`）。此步在 taifeng 仓执行，需用户确认发版。（2026-11-15 完成：用户确认后推送 main `21047ec` 与 tag `v2026.11.14.1`，Publish to PyPI 工作流 run 43691057213 通过，PyPI 已有 wheel 与 sdist）
- [x] 记录新版本号，作为 A1 的目标版本。（2026.11.14.1，见「进度」与「实际差异」）

## A1：平台升级

**Files**：Modify `packages/xuanpu-server/pyproject.toml`（版本下限）、`uv.lock`、`docker/` 下构建三个包 wheel 的脚本；Create `T/kernel_contract/test_event_compat.py`；按需 Modify `S/runtime/{events,assembler,sessions}.py` 的适配点。

- [x] 读内核两版之间的变更记录，列出稳定层不兼容项与弃用项，写进本文件末尾的「实际差异」一节。
- [x] 先写契约测试：既有事件消费者对新增字段不报错；`execution` 缺省为空的旧事件仍可回放。先看到失败或确认无关，再改。（`T/tracing/test_event_compat.py`，2 passed；放在 tracing 下而非计划写的 `T/kernel_contract/`，消费者就是采集器）
- [x] `uv lock --upgrade-package taifeng` 后跑三仓全量：（2026-11-15，PyPI taifeng 2026.11.14.1，taifeng-sandbox 按 CI 钉的 `af3e545` 锁定：`uv lock --check` 通过；xuanpu 全量 3771 passed / 60 failed / 49 errors / 445 skipped，失败集合与 main 同环境逐条一致（办公容器的 taifeng-sandbox 找不到 docker 可执行文件）；ruff All checks passed!；mypy 559 个源文件无错误。taifeng-sandbox、taifeng-stores 自身全量沿用「升级验证」一节结果）
  - `cd ../taifeng-stores && uv run pytest -q`
  - `cd ../taifeng-sandbox && uv run pytest -q`
  - `uv run pytest -q && uv run ruff check --select F,S108,I,TC packages && uv run mypy packages/*/src && uv lock --check`
- [x] 新失败逐个查根因；属于内核缺口的记 backlog，不 monkeypatch。（见「实际差异」表，均为内核修复带来的正确新行为）
- [ ] 真实模型验收脚本各跑一次（工单查询、审批写入、办公任务），结果记入 `docs/guides/releases/`。（部分：工单查询经 R5 真实模型验收覆盖，见 [R5 真实模型验收](../../guides/releases/2026-11-16-r5-real-model-acceptance.md)；审批写入与办公任务未跑，后者需要办公容器）
- [x] 提交 `chore: 内核升级到 2026.11.14.1 并补事件兼容契约`。（适配代码随 `fab795b`；本提交刷新 `uv.lock`、删除缺口第 31 项）

## 进度

- 2026-11-14：taifeng worktree `.claude/worktrees/stable-execution-identity`、分支 `feat/stable-execution-identity`，未提交。内容：`ExecutionIdentity` 进稳定层、实验层保留带弃用告警的同名入口、快照与公共 API 测试、新契约测试 `tests/test_execution_identity_stable.py`、ADR 0143、public-api / capability 文档与能力矩阵。
- 门禁（2026-11-14，Linux x86_64，Python 3.12，依赖按 `.[dev,litellm,telemetry-otel]` 实装）：
  - `ruff check --select F,S108,I,TC src tests examples scripts`：All checks passed!
  - `PYTHONPATH=src pytest tests/ -q`：4253 passed, 17 skipped in 151.06s
  - `python scripts/verify_examples.py`：通过 40/40
  - `mypy src/`：2 个错误（`llm/providers/openai/responses.py:244`、`llm/providers/codex/accumulator.py:604`，`ProviderStateEnvelope.payload` 的 Literal 键字典），main 在同一 mypy 2.4.1 下完全相同，与本改动无关；内核 dev 依赖未锁 mypy 版本。
- 2026-11-14：用户确认发版。本地 main 合并为 `21047ec`、附注 tag `v2026.11.14.1`；发布工作流的全部检查在本地复跑通过（`uv lock --check`、发布冒烟 5 passed、wheel/sdist 构建与干净安装 PASS），Python 3.13 全量 4253 passed、17 skipped。推送被会话环境拒绝，需用户在本机执行 `git push origin main && git push origin v2026.11.14.1`。

## 实际差异

稳定层 2026.10.1.11 → 2026.11.14.1 只增不减：新增 `EphemeralAtomicBatchMessageStore`、`EphemeralMessageStore`、`EphemeralStoreClosedError`、`ExecutionIdentity`、`InputOrigin`、`InputTaint`、`origin_of`、`summarize_taint`、`taint_from_extras`，无删除、无改名。内核 ADR 0117–0129 修了缺口清单第 18–29 项中的多项，平台行为因此可见变化的有三处，测试已按新的正确行为改断言、安全性断言保持不变：

| 测试 | 旧行为 | 新行为 |
| --- | --- | --- |
| `test_cancel.py::test_cancel_right_after_sending` | 提交后立刻取消要等受理，结束于 `turn_completed{end_reason=cancelled}` | 内核 ADR 0117 起提交即可取消，排队中停下结束于 `turn_failed{kind=cancelled}`；平台同样记 cancelled |
| `test_connector_sandbox.py` 两例 | 连接器启动失败的提示是 `RuntimeError`；启动超时后内核不收回进程 | 提示是 `McpToolError`；内核 ADR 0123 起自己收回进程，平台强杀兜底保留 |
| `test_remote_mcp.py` 四例 | 畸形响应交给模型的是异常类型；SSE 噪声日志为「invalid SSE JSON …内容不记」 | 交给模型的是内核固定分类文本（`[-32000] …`）；日志为「invalid frame type=… length=…」，仍无明文 |

## 升级验证（2026-11-14，Linux x86_64，Python 3.12，PG 16.16 + pgvector，sandbox `af3e545`，`UV_PYTHON_INSTALL_DIR` 已设）

- xuanpu 候选（新内核 + R5-B0/B1）：`pytest -q` 3883 passed / 9 failed / 31 skipped / 165 errors；同环境 main（旧内核）3801 passed / 5 failed / 165 errors。逐条比对，候选多出的 4 例即上表与路由文档一例，修正后单独复跑全部通过；其余失败与错误两边相同，来自环境（Docker 守护进程不可用、以 root 运行使权限用例不成立）。
- `ruff check --select F,S108,I,TC packages`：All checks passed!；`mypy packages/*/src`：Success: no issues found in 503 source files。
- taifeng-sandbox（`af3e545`）：新旧内核同为 281 passed、3 failed（以 root 运行时 bwrap 只读挂载用例不成立）、38 skipped。taifeng-stores（`086f212`）：新旧内核同为 179 passed、40 skipped。
- 待做：PyPI 出现 2026.11.14.1 后执行 `uv lock --upgrade-package taifeng`，跑 `uv lock --check`，删除缺口清单第 31 项，再提交。`xuanpu-server` 的版本下限已改为 `taifeng>=2026.11.14.1`。
