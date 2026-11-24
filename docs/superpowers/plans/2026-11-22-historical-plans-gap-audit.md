# 历史规划未实现与未闭环项全景整理（P0–P1 / R0–R7 / Finance F0–F15）

> 核查日期：2026-11-22。依据：`docs/superpowers/plans/` 全部历史计划、`docs/architecture/README.md`、`docs/architecture/kernel-seam-backlog.md` 与 `docs/guides/releases/` 实际验收记录。
> 严格区分**「A. 已规划但未实现（缺代码）」**、**「B. 源码已实现但未生产发布/未真人验收」**与**「C. 内核对接口遗留与 R8 承接项」**，避免把“未部署”误当“未开发”，也避免把“暂缓验收”忘在角落。

---

## A. 已规划但尚未实现的功能与底座缺口（缺代码）

| 编号 | 来源阶段 | 缺口项 | 当前现状 | 建议归口 |
| --- | --- | --- | --- | --- |
| A1 | 总体架构 | **`xuanpu-sdk-js`（JS/TS 客户端 SDK）** | 架构总览明确标记「未开始」 | 后续开放生态期 |
| A2 | P0 / 运维 | **租户删除接口、主密钥轮换批量重加密、审计表自动清理与归档** | 无接口与批处理脚本；轮换主密钥需逐条重写 | R8 平台运维后台 |
| A3 | P0 / 状态面 | **长期记忆向量后端配置化接入** | `StateBackends.memory_store` 槽位与 `taifeng-stores` 向量后端已存在，但 `ServerConfig` 缺少嵌入服务配置项 | R8 Phase 3.2（与企业共享知识库一并接入） |
| A4 | R0 / 高可用 | **多实例水平扩展与 `responses` 协议外部存储** | `execution_mode=single_instance` 唯一支持；咨询锁不含状态 fencing；外部 Postgres 会话主存目前仅支持 `chat` 协议 | R8 Phase 3.3 |
| A5 | R0 / 安全 | **出网范围限制、HTTP/stdio 成功响应脱敏与 JSON 参数按键脱敏** | 外部系统回显凭据可能原样进模型与审计；连接器凭据不含 OAuth2 换取令牌与远端 MCP OAuth/私有 CA | R8 Phase 3.2（与出站 DLP 一并收口） |
| A6 | R4-C | **带企业连接/写入/外部工具能力的评测环境策略** | 这类能力发起评测目前一律 `blocked`（`external_implementation_not_verified`），导致无法经正式能力流发布 | R8 Phase 2.2（市场与审核发布合流） |
| A7 | R4-F | **能力运行结果验收批次原生管理页** | 后端接口与 SDK 已就绪，但控制台无原生页面（建批次需手工样本任务 ID） | R8 Phase 2.2 |
| A8 | R4-D | **知识删除后台清理 Worker** | 删除知识来源在请求内同步删文本块与回答缓存，无异步清理队列 | R8 Phase 3.2 |
| A9 | ADR 0006 | **统一 MCP 网关** | 明确暂缓，当前由 `transport: remote` 直连替代 | R8 Phase 2.1（通过独立 `kind: plugin` 连接器市场承接） |

---

## B. 源码已实现，但尚未生产部署或尚未完成真实环境验收

| 阶段 | 源码状态 | 未闭环的具体事项 | 依据文档 |
| --- | --- | --- | --- |
| **P1 飞书登录** | ✅ 已实现并过本地/模拟验收 | 真实飞书自建应用接入、公网 HTTP 事件回调（离职事件）尚未实机验收 | `docs/guides/feishu-login.md`、`docs/architecture/README.md` |
| **R1 办公执行** | ✅ 已实现 | 本机 `local` 后端拒绝办公任务，生产需独立 rootless 容器配置（升级镜像不自动开启） | `docs/architecture/README.md` |
| **R3 到人协作** | ✅ 已实现并灰度部署 `acme`（0016–0019） | 真实双员工企微登录闭环与管理员通讯录授权/组织导入预览未执行（标记 `deferred_by_user`） | `docs/guides/releases/2026-10-24-r3-acceptance.md` |
| **R4 能力治理**（0028–0032） | ✅ 源码冻结并通过 CI（PR #9） | ① 未构建 R4 生产镜像、未执行生产库 `0028–0032` 迁移（G1 待授权）；② 真实浏览器 390px/Tab/身份切换未人工执行；③ 部分 PG SIGKILL 窗口（任务绑定、知识索引未标 ready、来源撤权清理、统计水位）未测；④ 真人业务暂缓 | `docs/guides/releases/2026-11-19-r4-candidate.md` |
| **R5 管家与极简入口** | ✅ 已随 `2026-11-15` 部署生产（默认关闭） | ① R5-A 办公容器真实模型验收未跑；② 真人试用签收未完成；③ 业务跟进后台语义判断偶发超时未优化 | `docs/guides/releases/2026-11-17-r5-remaining-acceptance.md` |
| **R6 管家常在线** | ✅ 源码与真实模型验收通过 | ① **尚未部署生产**；② 进程中途退出后由唤醒重跑未在真实模型下做真实进程终止验证 | `docs/guides/releases/2026-11-18-r6-always-online-acceptance.md` |
| **R7 小院（含四合院/故宫多尺度）** | ✅ 源码、模型与重放验收通过 | ① **尚未部署生产**；② 真实浏览器登录后全链路端到端未跑；③ 任务承载（写信）真实模型验收未跑（需办公容器） | `docs/guides/releases/2026-11-19-r7-courtyard-acceptance.md` |
| **Finance F0–F15（发票报销）** | ✅ 已合入主线，4000 项单测全过 | ① 未构建发布生产镜像、未连真实公司制度/员工/SSO；② 完整真实模型双旅程 0/2、负向挑战未执行；③ 上轮 F3 补件构造偶发失败未定位根因；④ 非阻塞 P3：持久任务 usage 局部缓存释放时机待优化 | `docs/guides/releases/2026-10-29-finance-main-merge.md` |
| **桌面端 & Docker 发布** | ✅ 基础可用 | `2026-10-06-macos-desktop.md` 留 1 项签名/分发收尾、`2026-10-07-docker-server-deployment.md` 留 2 项生产发布核对 | 对应计划文件 |

---

## C. `taifeng` 内核对接口遗留与本次升级后的新状态

截至 `external/taifeng` 升级至 **`v2026.11.20.0` (`6a17b82`)**：

| 编号 | 原缺口项 | 当前状态与后续动作 |
| --- | --- | --- |
| #16 | `EnginePool.create` 只接单 `skills_dir`，不支持多目录来源分层 | **`taifeng v2026.11.20.0` 已在实验层落地 `ctx.skills.add_dir`（ADR 0144）**；待晋升稳定层后，平台可将共享技能库与个人技能切换为多目录挂载，替代当前临时目录物化 |
| #2, #17 | `SessionJournalCore` 与白名单外 skill 授权/来源信任分层仍在实验层 | 维持现行处理（平台用 `MessageStore` + 平台审计表 + 静态白名单装配） |
| #15 | `CommandExecutor` / `WorkspaceFS` 调用不带会话/提交归属标识 | 维持按（租户、包、版本、用户）拆分引擎池 |
| #19 | `LocalWorkspaceFS` 边界契约差异 | 生产办公容器走 `taifeng-sandbox` `DaemonWorkspace`；本机不开放办公任务 |
| #20, #22, #23 | 取消受理竞态、中断用量估算、纯过期哨兵 | 平台保留纵深兜底（`unconfirmed`、`usage_estimated`、根中止检查） |
| #32 | `OtelTelemetrySink` 无法按事件附加 `xuanpu.trace_id` | 维持每租户一个 sink 并以 `taifeng.execution_id` 关联 |
| 扩展点 | 远端 MCP 会话过期（404）内核自动重连（ADR 0127） | 缺平台侧端到端回归用例覆盖 |

---

## D. 与 R8 规划的衔接结论（一句话版）

1. **老账不丢，但不要被“历史未部署的验收项”拖住平台演进**：R3/R4/R5/R6/R7 与 Finance 的代码底座绝大多数已经合入主线，未闭环的主要是**真实员工试点与生产发版窗口（G1）**，可在 R8 底座稳定后做一次**统一候选版本打包与生产迁移（0028–0034+）**，不必为每个小版本重复停写发版。
2. **真正缺代码且决定企业上线成败的项**，全部汇总进 [R8 企业级服务端平台实施计划](2026-11-22-r8-enterprise-platform-baseline.md)：
   - **已完成（Phase 0.1）**：官方能力生态迁出至独立 `../lumorax-catalog/` + 能力市场支持分组目录扫描 + 单机持久化模板 `examples/xuanpu.local.yaml`；
   - **待实施（按优先级）**：`Phase 1.1 RBAC` → `Phase 1.2 在线模型与三级 Token 配额` → `Phase 2 三位一体能力市场（补齐 Plugin + 审核发布 + 远端源）` → `Phase 3 OIDC / 企业共享知识库 ACL / 出站 DLP / 多实例高可用`。
