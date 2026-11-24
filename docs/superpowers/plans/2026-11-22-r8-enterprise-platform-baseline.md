# R8 企业级服务端平台基础能力实施计划（Implementation Plan）

**Goal:** 将玄圃（`xuanpu`）升级为边界清晰、可长期演进的**企业级服务端托管 Agent 平台**：先解耦平台底座与业务生态（Phase 0），再补齐企业服务端最核心的三大基础能力——**细粒度 RBAC 权限（Phase 1.1）**、**在线模型与三级 Token 配额中心（Phase 1.2）**、**三位一体能力市场：Agent + Plugin + Skill（Phase 2）**，并持续加固**管家—匠人核心运行模型（Phase 3）**。

**Architecture:** 坚持**服务端托管执行**（非端侧本地执行）与六条架构红线：`taifeng`（已升级至 `v2026.11.20.0`）保持零租户内核；`xuanpu` 保持零行业硬编码平台底座；业务扩展严守进程外隔离（Skill + OpenAPI HTTP + MCP + 沙盒脚本）；所有配置与运行状态持久化到控制面存储与对象存储。

**Tech Stack:** Python 3.12+ / FastAPI / Pydantic v2 / asyncpg（控制面迁移自 `0033` 起）/ `xuanpu-spec` / 原生 JS 控制台（无构建，带指纹校验）+ Node `vm` 行为测试。

---

## Global Constraints（全阶段硬约束）

- 所有 Python 模块顶部必须 `from __future__ import annotations`，模块/类/函数写中文 docstring；文件 ≤ 800 行、函数 ≤ 80 行。
- 配置一律经构造函数注入，**禁止 `os.getenv`**；异步操作不写阻塞 IO。
- 所有读写必须携带 `Principal` 租户边界；新控制面表必须同时支持内存后端与 Postgres 迁移并跑双契约测试。
- 租户密钥与模型 API Key 一律经 `SecretVault` 加密保存，绝不回显明文、绝不进审计与日志。
- 任何新增 HTTP 路由必须同步写入 `docs/architecture/http-api.md`；任何新增 `ServerConfig` 字段必须同步写入 `examples/xuanpu.yaml` 与 `docs/guides/operations.md`；任何控制台静态资源改动必须运行 `python3 scripts/refresh_console_assets.py`。
- 每阶段合入门禁：
  - `uv run ruff check --select F,S108,I,TC packages`
  - `uv run mypy packages/*/src`
  - `uv run pytest`

---

## Phase 0：平台架构与生态边界调整（正本清源）

### Task 0.1：官方能力生态目录与 `xuanpu/examples/` 分离

**目标：** 将正式通用技能库与标准匠人从 `xuanpu/examples/` 迁出至同级独立生态目录 `lumorax-catalog/`（与 `xuanpu`、`taifeng`、`xuanpu-finance-f0` 平级），`xuanpu/examples/` 仅留最小教学样例、mock 替身与部署配置样板。

**Files:**
- Create: `../lumorax-catalog/README.md`
- Move/Copy:
  - `xuanpu/examples/common-skills/` → `../lumorax-catalog/skills/common-skills/`
  - `xuanpu/examples/travel-planner/` → `../lumorax-catalog/artisans/travel-planner/`
  - `xuanpu/examples/driver-dispatch/` → `../lumorax-catalog/artisans/driver-dispatch/`
  - `xuanpu/examples/expense-helper/` → `../lumorax-catalog/artisans/expense-helper/`
  - `xuanpu/examples/order-desk/` → `../lumorax-catalog/artisans/order-desk/`
- Modify: `server/marketplace/service.py`（支持扫描 `catalog_dirs` 下的二级分组目录 `skills/`、`plugins/`、`artisans/`）
- Modify: `scripts/acceptance/r7_courtyard_server.py`（`catalog_dirs` 指向 `../lumorax-catalog` 与保留样例目录）
- Modify: `examples/README.md`、`docs/architecture/package-spec.md`
- Test: `tests/server/test_marketplace.py`、`tests/cli/test_examples_and_docs.py`

**Steps:**
- [x] Step 1: 扩展 `marketplace/service.py` 的 `_scan_dirs_sync`，递归支持一层分类子目录（`skills/`、`plugins/`、`artisans/`）而不重复扫描包内子目录。
- [x] Step 2: 在 `../lumorax-catalog/` 建立 `skills/`、`plugins/`、`artisans/` 目录并迁移官方技能库与匠人；更新相关测试路径与 `r7_courtyard_server.py`。
- [x] Step 3: 跑 `pytest tests/server/test_marketplace.py tests/server/test_skill_assembly.py tests/cli/test_examples_and_docs.py`。

---

### Task 0.2：单机持久化运行基线（告别纯内存丢状态）

**目标：** 提供开箱即用的本地持久化运行模式，已安装匠人/技能库、租户密钥（如 `amap-key`）、账号与会话在进程重启后全部保留。

**Files:**
- Create: `server/controlplane/sqlite_store.py`（或采用现成 Postgres Docker Compose 启动脚本；遵循红线 4，优先提供一键本地 Postgres + LocalObjectStore 编排 `docker/docker-compose.local.yml` 与 `scripts/dev_server.py`）
- Create: `examples/xuanpu.local.yaml`（持久化本地标准配置：`control_plane: postgres`、`state: local`、`object_store: local`、`marketplace.catalog_dirs`）
- Modify: `docs/guides/quickstart.md`、`docs/guides/operations.md`
- Test: `tests/cli/test_serve.py`

**Steps:**
- [ ] Step 1: 补充一键本地持久化启动配置（Postgres 控制面 + `<data_dir>/objects` 对象存储 + `marketplace.catalog_dirs`）。
- [ ] Step 2: 验收：启动 → 在「能力市场」安装 `travel-planner` → 在「密钥」写入 `amap-key` → 重启服务 → 验证匠人与 `amap-key` 依然生效。

---

## Phase 1：企业服务端最基础治理能力

### Task 1.1：企业级 RBAC 权限体系（`Role × Permission × Scope`）

**目标：** 将租户内写死的 2 档角色（`tenant_admin` / `tenant_member`）升级为细粒度 RBAC，支撑安全分权（审计员、能力管理员、密钥管理员、部门管理员、自定义角色）。

**Files:**
- Create: `server/controlplane/migrations/0033_rbac.sql`
  - 表 `xp_rbac_roles (tenant_id, role_id, name, description, permissions_json, builtin, created_at, updated_at)`
  - 表 `xp_rbac_bindings (tenant_id, binding_id, role_id, subject_type, subject_id, scope_unit_id, created_at, revoked_at)`（`subject_type` ∈ `user | position | unit`）
- Create: `server/rbac/`
  - `models.py`：`Permission` 枚举常量、内置 5 角色定义、`RoleRecord`、`RoleBindingRecord`、`EffectivePermissions`
  - `store.py`：`RbacStore` 协议 + `InMemoryRbacStore` + `PostgresRbacStore`
  - `service.py`：`RbacService`（角色 CRUD、绑定/撤销、结合 `organization.permissions.descendants` 计算用户有效权限与部门作用域、审计记录）
  - `guard.py`：`require_permission(platform, who, perm, *, unit_id=None)` 统一鉴权守卫（`tenant_admin` 与 `platform_admin` 保持全权限向后兼容）
- Create: `server/api/rbac.py`
  - `GET /v1/rbac/permissions`（返回平台全部权限点与中文说明）
  - `GET /v1/rbac/roles`、`POST /v1/rbac/roles`、`PUT /v1/rbac/roles/{role_id}`、`DELETE /v1/rbac/roles/{role_id}`
  - `GET /v1/rbac/bindings`、`POST /v1/rbac/bindings`、`DELETE /v1/rbac/bindings/{binding_id}`
  - `GET /v1/rbac/effective/{user_id}`（预览某员工最终有效权限及作用部门范围）
- Modify: `server/platform.py`、`api/app.py`、`api/marketplace.py`、`api/audit_usage.py`、`api/capabilities.py`（关键路由逐步切换至 `require_permission`）
- Create: `web/static/rbac.js`
- Modify: `web/static/index.html`、`app.js`、`identity.js`（新增「权限角色」页签，并按当前用户有效权限动态显示对应管理页签，不再只看 `isAdmin()`）
- Test:
  - `tests/server/test_rbac.py`（内置角色、自定义角色、部门子树作用域越权 403、审计员只读边界、绑定撤销立即生效）
  - `tests/web/rbac_behavior.cjs`

**内置权限点与默认角色矩阵:**

| 内置角色 | 持有权限点 | 作用范围 |
| --- | --- | --- |
| `tenant_admin` | `*`（全部租户权限） | 全租户 |
| `capability_admin` | `marketplace:*`, `package:*`, `capability:*`, `usage:read` | 全租户 |
| `secret_admin` | `secret:*`, `connection:*` | 全租户 |
| `auditor` | `audit:read`, `usage:read`, `trace:read`, `marketplace:read` | 全租户（只读） |
| `dept_admin` | `capability:assign`, `quota:dept_manage`, `usage:read`, `org:read` | 绑定的部门子树 |

**Steps:**
- [ ] Step 1: 编写 `0033_rbac.sql`、`rbac/models.py`、`rbac/store.py`（内存 + Postgres）。
- [ ] Step 2: 实现 `rbac/service.py` 与 `rbac/guard.py`，确保 `tenant_admin` 既有行为 100% 兼容。
- [ ] Step 3: 暴露 `api/rbac.py`，并将市场/密钥/审计/能力接口接入 `require_permission`。
- [ ] Step 4: 实现控制台 `rbac.js`（角色权限勾选、成员绑定、有效权限预览）与动态导航显隐。
- [ ] Step 5: 补齐测试、更新 `http-api.md` 并跑全量门禁。

---

### Task 1.2：在线模型路由与三级 Token 配额/成本中心

**目标：** 管理员在控制台在线配置多模型供应商、档位路由（`fast / balanced / deep` + 主备降级）与单价表，无需重启；配额从单租户日上限升级为「租户 → 部门 → 员工/匠人」三级管控与 RPM/TPM 限流。

**Files:**
- Create: `server/controlplane/migrations/0034_model_routing_and_quotas.sql`
  - 表 `xp_model_providers (tenant_id, provider_id, name, protocol, base_url, api_key_secret, status, created_at, updated_at)`
  - 表 `xp_model_tier_bindings (tenant_id, tier, primary_provider_id, primary_model, fallback_provider_id, fallback_model, timeout_seconds, input_price_per_m, output_price_per_m, updated_at)`
  - 表 `xp_scoped_quotas (tenant_id, scope_type, scope_id, max_daily_tokens, max_monthly_tokens, max_rpm, max_tpm, warn_ratio, updated_at)`（`scope_type` ∈ `tenant | unit | user | package`）
  - 扩展 `xp_usage` 维度汇总（按日 × 部门 × 员工 × 匠人 × 模型统计 token 与费用）
- Create: `server/models_config/`
  - `models.py`、`store.py`、`service.py`（供应商管理、档位绑定、在线连通性探测 `test_provider`、密钥仅存 `SecretVault` 引用）
- Modify: `server/runtime/models.py`
  - `GatewayModelClientFactory.create(tenant_id, tier)` 优先查租户在线配置（带短 TTL 缓存 + 变更失效），未配置时回退部署级 YAML 默认值；支持主供应商异常时降级至 Fallback。
- Modify: `server/quota.py`
  - `QuotaGuard.check_new_turn(tenant_id, *, user_id=None, unit_ids=(), package=None)` 逐级校验：租户日/月限 → 部门月限 → 员工/匠人日限 → 滑动窗口 RPM/TPM。
- Create: `server/api/models_config.py`
  - `GET/POST/PUT/DELETE /v1/models/providers`、`POST /v1/models/providers/{id}/test`
  - `GET/PUT /v1/models/tiers`
  - `GET/PUT /v1/quotas`、`GET /v1/usage/breakdown`（按部门/员工/匠人/模型统计 Token 与估算费用）
- Create: `web/static/models.js`
- Modify: `web/static/index.html`、`app.js`、`identity.js`（新增「模型与配额」页签，增强「用量」成本视图）
- Test:
  - `tests/server/test_models_config.py`（在线切换模型热生效、Fallback 降级、连通探测）
  - `tests/server/test_scoped_quotas.py`（租户/部门/员工三级拦截、RPM 限流、费用汇总）
  - `tests/web/models_behavior.cjs`

**Steps:**
- [ ] Step 1: 编写 `0034_model_routing_and_quotas.sql` 与 `models_config/`（内存 + Postgres 双后端）。
- [ ] Step 2: 改造 `GatewayModelClientFactory` 接入租户动态路由与 Fallback；扩展 `QuotaGuard` 支持三级配额与 RPM/TPM。
- [ ] Step 3: 暴露 `api/models_config.py`（受 `model:manage` / `quota:manage` RBAC 保护）。
- [ ] Step 4: 实现控制台 `models.js`（配置供应商、绑定 `fast/balanced/deep`、测试连通、配置部门/员工配额与查看费用明细）。
- [ ] Step 5: 补齐测试、更新 `http-api.md` 并跑全量门禁。

---

## Phase 2：统一能力市场（Agent + Plugin + Skill 三位一体）

### Task 2.1：补齐独立「插件/连接器市场（Plugin）」资产类型

**目标：** 将外部连接器（OpenAPI HTTP / MCP Server）从具体技能库/匠人包中解耦为独立的一等资产 `kind: plugin`，形成完整的 **匠人市场（Agents）+ 插件市场（Plugins）+ Skill 市场（Skills）**。

**Files:**
- Modify: `spec/manifest.py`、`validate.py`
  - 新增 `PackageManifest.is_plugin`（仅声明 `connectors` 与 `exports` 工具声明，无需入口/子技能）；
  - 允许 `SkillLibraryUse` 或新增 `PluginUse`（`spec.plugins.uses`）声明依赖某独立插件包（例如 `common-skills` 引用 `@official/amap-plugin`）。
- Modify: `server/runtime/skill_assembly.py`、`runtime/assembler.py`
  - 装配运行时自动解析 `plugins.uses`，将依赖插件包的连接器安全挂载到当前匠人沙盒/出口代理。
- Modify: `server/marketplace/models.py`、`service.py`、`api/marketplace.py`
  - `MarketplaceKind = Literal["artisan", "plugin", "skill_library"]`；
  - 依赖拓扑解析支持 `artisan → skill_library → plugin` 三层自动级联安装。
- Modify: `web/static/marketplace.js`、`index.html`、`style.css`
  - 市场分栏升级为：`全部` / `匠人市场` / `插件市场` / `Skill 市场`，并在插件卡片提供「连通性检查」与密钥配置引导。
- Create: `../lumorax-catalog/plugins/amap-plugin/`（将高德 OpenAPI 连接器独立为插件包示例）
- Test: `tests/spec/test_manifest.py`、`tests/server/test_marketplace.py`、`tests/web/marketplace_behavior.cjs`

---

### Task 2.2：市场对接企业审核发布流 + 远端 HTTP 制品库源

**Files:**
- Create: `server/marketplace/sources.py`（抽象 `CatalogSource`：`DirectoryCatalogSource` + `HttpRegistrySource`，校验 `sha256` 指纹）
- Modify: `server/marketplace/service.py`、`api/marketplace.py`
  - 安装支持两种模式：`mode="direct"`（开发/快速启用）与 `mode="governed"`（经 `capabilities` 登记 → 评测 → 按部门/RBAC 发布上线）。
- Modify: `server/marketplace/config.py`、`examples/xuanpu.yaml`、`docs/guides/operations.md`（增加 `registry_urls` 配置项）
- Test: `tests/server/test_marketplace_registry.py`

---

## Phase 3：管家—匠人核心模型加固与企业护城河

### Task 3.1：标准企业 SSO（OIDC）与平台级多租户管理页
- 新增 `identity/oidc.py` + `GET /v1/auth/oidc/start` & `callback`（复用既有 `IdentityService` 会话签发与离职/停用机制）。
- 控制台新增平台管理员视图（仅 `platform_admin` 凭据可见）：租户列表、新建/停用租户、全局配额与健康总览。

### Task 3.2：企业共享知识库（带 RBAC ACL）与出站 DLP 脱敏
- 在 `knowledge/` 下扩展企业/部门级共享知识库（基于 `rbac` + `organization` 过滤可见来源，引入向量+全文混合检索）。
- 在 `runtime/models.py` 出口层新增可配置 `DlpGuard`（规则表：正则/关键词匹配手机号、身份证、银行卡、自定义机密标签，支持 `mask` 自动打码或 `block` 阻断并记审计）。

### Task 3.3：管家—匠人运行态与多实例高可用收口
- 补齐 `taifeng-stores` / `xuanpu-server` 对 `responses` 协议外部 Postgres 主存的支持，解除 R0 单进程实例锁限制。
- 待 `taifeng` 将 `taifeng.plugin`（ADR 0144/0145）晋升稳定层后，将 `xuanpu-server` 宿主侧的连接器工具桥接、共享技能目录挂载（`ctx.skills.add_dir`）、多路遥测（`FanoutSink`）与外部存储后端（`provide_backends`）统一迁移至内核插件契约。
- 持续保持展示层（小院 / 四合院 / 故宫）与底层派工/追踪模型的严格解耦。

---

## 执行与里程碑总表

| 阶段 | 核心产出 | 完成后达到的平台形态 |
| --- | --- | --- |
| **Phase 0** | 独立生态仓 + 持久化默认运行 | 平台代码与业务能力彻底分离，配置与已装能力重启不丢 |
| **Phase 1** | RBAC 权限体系 + 在线模型与三级 Token 配额 | 企业可安全分权、在线切模型、按部门/员工控算力与算账 |
| **Phase 2** | 三位一体能力市场（Agent/Plugin/Skill）+ 远端源与审核发布 | 管理员全在线逛市场、一键装依赖、配密钥、评测灰度上线 |
| **Phase 3** | OIDC + 共享知识库 ACL + 出站 DLP + 多实例高可用 | 具备中大型企业正式生产交付与高可用运行能力 |
