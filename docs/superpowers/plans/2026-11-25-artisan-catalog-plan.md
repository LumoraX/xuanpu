# 玄圃标准办公匠人矩阵规划（Butler 可直接读取、派工与编组协作）

> 记录时间：2026-11-25
> 配套前提：Butler 现已通过 `list_artisans` 读取每位匠人的完整能力画像（`package` / `entry` / `name` / `entry_name` / `description` / `entry_description` / `host` / `accepts_materials` / `connectors` / `capabilities.{shell,network}`），并通过 `read_personal(kind="dispatches")` + `check_dispatch` 随时读取本人历史派工状态、验收五项与交付成果摘录，再经 `act_personal` 将匠人产出的结论沉淀进本人知识图谱、待办、日程、提醒与 Markdown 文档。

---

## 一、分工原则：管家（Butler）vs 匠人（Artisans）

| 角色 | 职责范围 | 执行边界 |
| --- | --- | --- |
| **管家（Butler）** | 员工唯一入口；私人空间全域感知与自主办理（知识图谱、随手记、纯文本纪要整理、待办/日程/提醒/事项/跟进、Markdown 归档）；按能力画像挑选匠人派工或编组（并行/接力/会诊）并把匠人成果二次整理落库 | 无沙盒、无脚本、无外部连接器、不跨员工 |
| **匠人（Artisans）** | 专业化、带沙盒脚本或外部连接器的企业/文档/多模态/联网能力，作为独立业务包（`xuanpu/v1`）发布到租户目录，经 `CatalogArtisanResolver` / 能力纳管按员工授权暴露给管家 | 独立沙盒、独立凭据、按副作用走效应闸门与确定性验收 |

---

## 二、标准匠人矩阵（6 大核心匠人）

### 1. 文档解析与表格整理匠人（`office-assistant` 升级 / `doc-organizer`）
- **状态**：已有基础样板 [examples/office-assistant](../../../examples/office-assistant/xuanpu.yaml)，需扩展多格式解析脚本。
- **承载与策略**：`host: task`（`accepts_materials: true`，`sandbox.shell: true`，`sandbox.network: false`，`approval: standard`）。
- **入口规划**：
  - `office`（已有）：纯文本/会议纪要/CSV 行动项与可追溯表格汇总。
  - `parse-doc`（规划新增）：在断网脚本沙盒内解析员工附带的 **PDF / DOCX / XLSX / PPTX**（带页数/体积/解压炸弹上限），输出结构化 Markdown 摘要 + 关键表数据 + 人物/公司/待办候选 JSON。
  - `compare-docs`（规划新增）：两版合同/方案/报价单差异比对与风险点清单。
- **Butler 联动**：员工在小院上传 PDF/Excel → Butler 调 `list_artisans` 看到 `accepts_materials=true` 与 `capabilities.shell=true` → `dispatch_artisan` 附带 `materials` → 完成后 Butler 唤醒复核并可按员工要求调用 `act_personal(capture_bundle / save_note_file)` 自动入图谱与工作台。

### 2. 图片与多模态识别匠人（`vision-extractor`）
- **状态**：规划新建业务包。
- **承载与策略**：`host: task`（`accepts_materials: true`，`sandbox.network: false`；或通过受控 OCR/多模态连接器承载）。
- **入口规划**：
  - `business-card`：名片照片 / 微信名片截图 → 输出严格结构化的 `orgs` + `persons` + `contacts` + `address` 文本摘要。
  - `screenshot-notes`：微信/飞书聊天长截图、会议白板照、PPT 拍照 → 提炼讨论结论、承诺、行动待办与相关人物。
  - `receipt-itinerary`：发票、行程单、酒店预订单截图 → 提取金额/日期/地点/航班酒店号，供 Butler 直接建日程提醒或接力派给财务匠人。
- **Butler 联动**：员工贴图上传 → Butler 派给 `vision-extractor` → 拿回结构化结果后直接 `act_personal(capture_bundle)` 建档/建待办，或用 `dispatch_group(mode="relay")` 接力派给 `finance` 报销。

### 3. 外部情报与出行规划匠人（`web-research` / `travel-planner`）
- **状态**：已有地图连接器样例 [examples/demo-map-api](../../../examples/demo-map-api)，待补齐通用联网调研包。
- **承载与策略**：`host: session`（`sandbox.network: true` 或配置受控搜索/地图/天气 HTTP 连接器，`approval: standard`）。
- **入口规划**：
  - `company-brief`：查目标公司公开工商、主营业务、近期新闻与地址补全（带来源链接），供 Butler 补全个人知识图谱里的公司档案。
  - `trip-plan`：结合客户端 `location`、实时天气与路线接口规划出行方案与建议出发时间，交回 Butler 后可一键设置出发提醒。

### 4. 企业信息只读采集匠人（`wecom-readonly` / `feishu-readonly` / `mail-readonly`）
- **状态**：已有 [examples/wecom-readonly](../../../examples/wecom-readonly/xuanpu.yaml) 与 [examples/shared-reader](../../../examples/shared-reader/xuanpu.yaml)。
- **承载与策略**：`host: session`（本人显式授权只读连接器，所有操作为 `read`，不自动代发任何外部消息）。
- **入口规划**：
  - `digest`：按时间窗口拉取本人未读企业微信/飞书消息、邮件或共享事项变更，归纳成「需要回复 / 新增待办 / 关键进展」三段摘要。
- **Butler 联动**：Butler 读回摘要后，自动通过 `act_personal` 更新本人待办/图谱或生成晨报。

### 5. 企业业务办理匠人（`finance` / `support-desk` / `crm-ops`）
- **状态**：已有 [examples/finance](../../../examples/finance/xuanpu.yaml) 与 [examples/support-desk](../../../examples/support-desk/xuanpu.yaml)。
- **承载与策略**：`approval: standard` 或 `strict`（只读查询自动放行；涉及提交单据、改优先级、付款/对外发送等写操作一律走平台审批闸门，由员工在小院「去盖章」确认）。
- **Butler 联动**：Butler 仅按连接器与描述选对匠人派工，绝不绕过审批或替员工拍板金额/对外动作。

### 6. 周期复盘与简报匠人（`briefing-compiler`）
- **状态**：规划新建，与 `task_triggers` 定时触发器联动。
- **承载与策略**：`host: task`（`accepts_materials: true`，`sandbox.shell: true`，`sandbox.network: false`）。
- **入口规划**：
  - `weekly-review`：汇总本周原始记录、事项进度、待办完成率与新拓展人脉，生成结构化周报与下周重点。
  - `meeting-prep`：会前基于参会人图谱、历史纪要与未决问题生成 1 分钟会前速览卡。

---

## 三、多匠人编组典型剧本（Butler `dispatch_group`）

1. **接力（`relay`）— 截图/文档 → 自动整理 → 业务执行**：
   - `vision-extractor/receipt-itinerary`（或 `office-assistant/parse-doc`）提取发票/合同要素 → 接力给 `finance/finance-assistant` 核对并起草报销单（写操作等员工盖章）。
2. **并行（`parallel`）— 见客户前一键准备**：
   - 并行派出 `web-research/company-brief`（查对方最新动态与路线）+ `shared-reader/read`（查团队共享资料），同时 Butler 用 `read_personal(entity_graph)` 查本人历史往来，统一汇总成会前简报。
3. **会诊（`consult`）— 高价值方案/合同复核**：
   - `office-assistant/compare-docs` 与业务审查匠人背靠背独立审阅同一份合同，Butler 按 `rate_divergence` 综合或将高分歧项交员工在圆桌裁决。

---

## 四、验收与演进检查点

- [x] **底座打通**：`list_artisans` 透出 `host` / `accepts_materials` / `connectors` / `capabilities.{shell,network}`；`read_personal(kind="dispatches")` 列出本人最近派工；`check_dispatch` 返回验收结果与交付成果摘录；`act_personal(action="act_followup")` 打通主动跟进办理。
- [ ] **P1**：在 `examples/office-assistant` 落地 `parse-doc`（PDF/DOCX/XLSX 提取），并在租户默认目录启用持久任务执行，让 Butler 能直接把上传的非纯文本附件派给它。
- [ ] **P2**：落地 `vision-extractor`（名片/截图/白板识别）与前端输入框贴图上传。
- [ ] **P3**：落地 `briefing-compiler` + `task_triggers` 定时晨报/周报与会前自动准备。
