# ADR 0025：管家造匠、三层修改边界与 L1/L2/L3 不可变发布节点

- 状态：Proposed（2026-11-27，待实施）
- 关联：细化并补充 [ADR 0017](0017-butler-artisan-orchestration.md) 决策 4（「平台进程不执行模型即兴编排脚本」不变，明确管家可在隔离沙盒内编写与测试匠人包）与决策 9（明确造匠草稿 → 管家审核 → R4 发布的完整链路）；复用 [能力目录与安装契约](../architecture/capability-delivery.md)（R4）
- 规划：[运行时、造匠与可观测底座规划](../superpowers/plans/2026-11-27-butler-runtime-forge-and-observability-plan.md)

## 背景

随着玄圃向自进化企业 Agent 平台演进，业务方与员工需要管家能够按需**创造或迭代匠人**（编写 `xuanpu.yaml`、`skills/*/SKILL.md`、沙盒脚本代码与评测集，并在沙盒内自测）。同时必须回答三个核心安全与稳定性问题：

1. 如何防止模型裸改 `xuanpu.yaml` 或启停配置时破坏结构、漏掉字段、绕过安全不变量？
2. 如何在管家编辑业务包甚至演进构造代码后，保证线上存量匠人与玄圃自身始终可运行、不中断、随时可回退？
3. 如何做到新匠人免重启生效，又避免“直接热覆写线上文件夹”导致的半成品炸服与执行中撕裂？

## 决策

1. **明确边界：平台主进程不跑即兴脚本，但在隔离沙盒内支持「管家造匠（Artisan Forge）」。**
   - `xuanpu-server` 主进程仍绝不加载或执行模型生成的任意脚本，管家主引擎继续保持零沙盒、无业务包代码。
   - 新增平台内置隔离造匠工作区（`ArtisanForge`，运行于独立沙盒目录与 `taifeng-sandbox` `DaemonWorkspace`），用于新建或从指定不可变版本 `checkout` 基线迭代标准 `xuanpu/v1` 业务包。
2. **三层修改边界（核心铁律：控制面只走强类型代码 Tool，严禁 LLM 裸改 `xuanpu.yaml`）。**
   - **L-Control（控制面与清单）**：修改 `xuanpu.yaml`、扩充/移除匠人入口、禁用/启用匠人、配置连接器工具白名单、预算与沙盒策略、管理评测声明与版本号，一律收口为**强类型确定性代码 Tool**（如 `forge_add_entry`、`forge_remove_entry`、`forge_set_policy`、`forge_set_connector_tools`、`forge_upsert_eval`、`forge_bump_version`、`artisan_disable`、`artisan_rollback`）。由平台经 `spec/manifest.py` 反序列化 → 校验不变量（拒绝未知字段、禁止空通配连接器、强制版本递增）→ 规范化回写 → 留审计与语义 Diff。
   - **L-Content（技能文档、代码脚本与私域文档）**：仅对 `skills/*/SKILL.md`、`skills/*/scripts/*`、`skills/*/references/*` 开放受控文件编辑，直接复用 `taifeng` 稳定层的 `make_apply_patch_tool`（全量 dry-run 原子写）+ `make_file_read_tool` / `glob` / `grep`；通过 `taifeng` `HookRegistry` 在 `PreToolUseHook` 层按路径白名单**硬拒绝直接改 `xuanpu.yaml`**，在 `PostToolUseHook` 层自动生成 Unified Diff 并触发 `scan_skills` / `py_compile` / `ruff` 校验。私域个人文件同步补齐 `patch_personal_file`（唯一命中校验 + 版本 CAS + Diff + `/undo`）。
   - **L-Release（审核、发版与回退）**：`forge_validate` → `forge_test` → `forge_review` → `forge_publish` 走纯状态机工具。
3. **三级自动门禁 + 管家审核后发版生效（热插拔而非热覆写）。**
   - 造匠草稿必须依次通过：L1 静态契约与安全校验（`validate_package` + 脚本静态检查）→ L2 断网沙盒脚本单元测试 → L3 独立行为评测（复用 R4-C `capability_evaluation` 的 `xuanpu-eval-suite/v1`，按平台实际观测判定，不采信模型自评）→ **管家审核（核对 Diff、权限变更与 L1/L2/L3 证据）**。
   - 审核通过后冻结为不可变 `package@semver` + `sha256` 归档，进入 `PackageRegistry` / R4 预检、分配与发布流程，通过原子切换版本指针实现免重启生效；绝不直接修改正在服务线上会话的目录。
   - 旧版本 `retire`（在途平滑收尾）或 `revoke`（经 `capability_guard` 立即熔断）后，在途清零即彻底卸载对应 `EnginePool` 与连接器进程（Clean Unmounting），不留僵尸状态。
4. **三层独立版本化与不可变发布节点（L1 匠人 / L2 构造器 / L3 内核均可安全回退）。**
   - **L1 业务匠人层**：不可变归档 + R4 发布节点；
   - **L2 构造工具层（Forge Builder & Spec）**：修改 `xuanpu.yaml` 的构造代码与模板自身实行独立版本管理（`forge_version` + `apiVersion`），每个匠人制品记录 `built_by: {forge_version, spec_version}` 血缘；L2 升级必须通过**双向契约回归测试**（无损兼容存量已发布包），L2 回退不影响已发布的自包含不可变 L1 匠人；
   - **L3 平台与内核层（`taifeng` + `xuanpu-server`）**：不轻易动，只在隔离 Git Worktree 开发并通过全量 CI 分片 + 临时端口影子实例 `/healthz` 验活后由外部切流发布。
   - 每一次生效切换均为追加式不可变发布节点（`R1 → R2 → R3`）；回退即追加新节点 `R4 → 指向旧节点 R2`，审计链单调递增。L2/L3 涉及存储或 Schema 变更严格遵守 **Expand-and-Contract（只扩不删）**，确保旧版本代码可读新版本数据；已发出的外部副作用由闸门与 `EffectReconciliation` 兜底。
5. **统一 5 级工具安全评估链。**
   - 基于 `taifeng` `HookRegistry` + `PermissionPolicy` 固化 `PreToolUseHook → Deny → Ask/Force-Ask → Allow/Grant → PostToolUseHook` 评估顺序，`PreToolUseHook` 永远最先执行且不可被模型或宽松预设绕过。

## 不采用

- **允许 LLM 用通用文件工具直接编辑 `xuanpu.yaml`**：极易破坏 YAML 结构、误删兄弟配置或绕过连接器白名单与版本不变量。
- **监听可变目录直接热重载线上匠人**：半成品写入会直接打挂线上服务，导致同一会话前后两轮执行撕裂且无法多实例一致与审计复现。
- **在运行中的 `xuanpu-server` 进程内热重载平台自身代码**：一次语法或导入错误即可令管家与平台同时宕机，失去自救能力。

## 影响

- 规范与工具：基于 `spec/manifest.py` 沉淀 L-Control 强类型修改原语，并赋予独立 `forge_version` 与双向兼容测试集；新增 `server/engine/forge/` 沙盒工作流与 `Artifact` 分层。
- 接力与工作区：`relay` 与有界动态多步接力通过不可变 `ObjectStore` `artifact_refs` + `InputSnapshotService` 只读挂载传递文件成果，并强制继承 `InputTaint`。
- 纯 Web 端：Aux Pane「产物与代码」标签统一承接私域文件 Diff 与 Forge 造匠文件树、Diff 及 L1/L2/L3 审核报告。
