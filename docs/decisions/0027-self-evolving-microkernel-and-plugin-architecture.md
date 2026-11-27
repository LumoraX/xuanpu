# ADR 0027：全栈自进化微内核（L0–L3）与前后端统一插件架构

- 状态：Proposed（2026-11-27，待审批）
- 关联：演进并统合 [ADR 0004](0004-planes-and-effect-gates.md)（四平面与效应闸门）、[ADR 0025](0025-artisan-forge-and-versioned-releases.md)（三层不可变发布节点与造匠边界）、[ADR 0026](0026-local-transcript-and-cross-node-diagnosis.md)（跨节点 Transcript 自排查）
- 完整架构规范：[全栈自进化微内核与插件化架构设计](../superpowers/specs/2026-11-27-self-evolving-microkernel-architecture.md)

## 背景

玄圃已完成事件驱动唤醒、跨轮任务板、跨节点脱敏 Transcript 与后端造匠（`ArtisanForge`）骨架，但要同时达成以下两个长期目标，现有单体装配与静态平铺前端架构存在结构性瓶颈：

1. **能力对标头部 Agent 产品**：需要具备常驻分屏富交互工作台（Mermaid SVG、KaTeX、代码高亮、行级 Diff、交互式沙盒 Artifact）、真实 Office/PDF/Chart 生产、受控无头浏览器与 IM 主动触达能力；若继续硬编码进 `server/platform.py` 与 `web/static/entry.js`，系统会迅速膨胀失控。
2. **前后端全组件乃至内核自身的可自进化**：希望做到不仅业务匠人可自进化，前端组件、后端业务域乃至平台内核本身都能在安全门禁下持续自进化迭代，且避免“改坏自己后无法恢复”的根本风险。

## 决策

1. **确立 L0–L3 四层自举微内核拓扑（裁判与选手物理分离）。**
   - **L0 不可变监护根（Supervisor & Immutable Evaluator，~500 行）**：独立于 `xuanpu-server` 进程之外、以只读权限隔离运行，**永不自改**，负责门禁裁决（静态检查 + 单测 + 契约兼容 + Live-LLM 黄金集）、影子验活、蓝绿切流与 30 秒健康熔断自动回滚。
   - **L3 极简微内核（Python Core + Web Shell，可自进化）**：只保留不可下沉的底层原语（认证隔离、审计脱敏、配额密钥、事件总线、Trace/Transcript、沙盒与 5 级 Hook、管家编排核、存储与对象、PluginHost、ReleaseLedger）；内核自进化**严禁原地热改自身**，必须经隔离 Git Worktree 构造 → L0 门禁与影子实例验证 → 人工审批 → L0 蓝绿切流完成。
   - **L2 前后端组件插件层**：
     - 后端抽象 `BackendPlugin` 统一契约，支持 `in_process_verified`（受审核心插件）与 `isolated_worker`（独立进程/容器高危插件，如真实 Office 沙盒与受控无头浏览器）两级隔离，支持 **Clean Unmounting（干净卸载）** 与无 DDL 风险的 Plugin Collection 存储；
     - 前端抽象 `web-shell`（4 个标准挂载槽：`header-pill`、`message-renderer`、`aux-tab`、`artifact-viewer`），采用 **Level A 声明式 UI Schema** 与 **Level B 沙盒 iframe（`sandbox="allow-scripts"`，禁用 `allow-same-origin`）+ 白名单 `postMessage` Bridge** 双级安全模型，插件永不接触主站 Cookie、CSRF 与主页 DOM。
   - **L1 全栈业务匠人包（前后端同包版本化）**：扩展 `xuanpu.yaml` 可选 `ui:` 段，使一份不可变 `package@semver` 同时打包后端 `skills/`、前端 `ui/` 与评测 `evals/`，通过 `ArtisanForge` 同步生成、扫描、审核、原子发版与原子回退。
2. **数据库与协议变更强制执行 Expand-and-Contract（只扩不删）。**
   - L0 门禁自动拦截破坏性缩容迁移（如 `DROP TABLE`/`DROP COLUMN`），确保任意蓝绿回滚节点均可无损读写数据。
3. **真实大模型（Live LLM）黄金评测集纳入 L0 不可变裁判。**
   - 将端到端真实模型场景验收固化到 L0 只读目录，防止自进化过程出现“确定性 Mock 测试全绿但真实体验退化”的盲区。

## 不采用

- **让在线运行的 `xuanpu-server` 或 Web 主壳原地热覆写自身源码**：一旦引入语法、导入或死循环缺陷，平台与管家同时失联，彻底丧失自救与回退能力。
- **让前端插件直接在主站同源 DOM 中执行任意 JS**：一次外部网页/文档的 Prompt 注入即可演变为同源 XSS，窃取租户会话或伪造审批确认。
- **由 AI 自进化直接修改 L0 评测集与蓝绿监护策略**：违背“裁判不可被选手篡改”的安全底线。

## 影响

- 后端：新增 `server/core/plugins/`（`BackendPlugin` 契约、生命周期与 `Clean Unmounting`），逐步瘦身 `server/platform.py`；将 Office/PDF 沙盒、无头浏览器与 IM 通道以独立 L2 插件形式接入。
- 前端：重构极简入口为 `web-shell` + 右侧常驻分屏工作台 + 4 槽位插件挂载与 `postMessage` Bridge；离线打包 Mermaid SVG、KaTeX、语法高亮与行级 Diff 插件。
- 规范与造匠：扩展 `spec/manifest.py` 与 `server/engine/forge/` 支持全栈 `ui/` 插件声明、静态安全扫描与原子回退。
- 运维与发布：将 `scripts/release/l3_blue_green_release.py` 升级为 L0 常驻只读蓝绿监护器，衔接跨节点 Transcript 自诊断与内核候选补丁流水线。
