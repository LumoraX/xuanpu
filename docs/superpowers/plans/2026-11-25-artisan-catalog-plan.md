# 玄圃标准办公匠人与共享技能库矩阵规划（Butler 可直接读取、派工与编组协作）

> 记录时间：2026-11-25（2026-11-27 对齐 [管家灵魂、架构与演进总规划](2026-11-26-butler-soul-and-evolution-plan.md)，补充 Skill vs Agent 分层准绳、共享文档技能库 `document-skills` 与断网通用分析匠人）  
> 配套前提：Butler 现已通过 `list_artisans` 读取每位匠人的完整能力画像（`package` / `entry` / `name` / `entry_name` / `description` / `entry_description` / `host` / `accepts_materials` / `connectors` / `capabilities.{shell,network}`），并通过 `read_personal(kind="dispatches")` + `check_dispatch` 随时读取本人历史派工状态、验收五项与交付成果摘录，再经受控缩权的 `act_personal` 将匠人产出的结论沉淀进本人知识图谱、待办、日程、提醒与工作台文件。

---

## 一、分工与分层原则：Butler vs Skills vs Artisans

### 1. 三层角色边界

| 角色 | 职责范围 | 执行边界 |
| --- | --- | --- |
| **管家（Butler）** | 员工唯一入口与幕僚长大脑；私人空间全域感知与自主办理（图谱、随手记、常规 Markdown 与简单 Mermaid、待办/日程/提醒/事项/跟进、轻量 1–3 张名片/截图受控直读）；按能力画像派工、编组与有界动态接力 | 零沙盒、零脚本、零外部连接器、不跨员工 |
| **共享技能库（Skill Library）** | 纯复用能力包（`lumorax-catalog/skills/*`，仅声明 `spec.skills.exports`，**不进入 `list_artisans`**），按格式/专业工序拆分为高内聚独立 Skill，由宿主匠人在运行时按需渐进披露加载（`search_skills → read_skill → call_skill`） | 继承宿主匠人的沙盒/连接器边界，零重复造轮子 |
| **匠人（Artisans / Agents）** | 独立业务包入口（`lumorax-catalog/artisans/*`），经 `CatalogArtisanResolver` 按员工授权暴露给管家 | 独立沙盒、独立凭据、按副作用走审批闸门与确定性验收；**联网采集与本地代码执行物理隔离** |

### 2. 判断「建独立 Skill 还是独立 Agent（匠人）」的统一尺子
- **做独立 `Skill`**：处于同一安全/网络/凭据边界内、可被多个不同匠人复用、需要独立格式规范/脚本/模板、且经常在同一沙盒里共享中间变量连续组合的能力（如 `pdf-parse`、`excel-analysis`、`pptx-compose`、`chart-render`、`diagram-compose`、`markdown-publish`）。**绝不按文件后缀拆成 5 个独立 Agent。**
- **做独立 `Agent（匠人）`**：安全/网络/凭据边界不同（断网沙盒 vs 联网调研 vs 企业财务/企微凭据）、需要按部门/员工单独授权计费、或需要独立验收/背靠背会诊的角色。

---

## 二、纯共享技能库矩阵（`lumorax-catalog/skills/*`，不进 `list_artisans`）

1. **`common-skills`（已就绪）**：通用基础工具与计算/整理技能。
2. **`web-skills`（已就绪，持续演进）**：DeepSeek Search & Fetch 联网搜索与静态正文抓取（脚本沙盒保持断网）；M4 按需扩展只读无头浏览器渲染，覆盖动态 SPA 网页采集。
3. **`document-skills`（M2 核心新增）**：面向所有需要处理非纯文本文件与富交付物的匠人（供 `doc-analyst`、`finance`、`briefing-compiler`、合同审阅等统一复用）：
   - `pdf-parse`：PDF 分页正文与表格抽取、扫描件识别分流、页数/体积/解压炸弹防护。
   - `excel-analysis`：XLSX / CSV 结构化读写、pandas 统计与透视分析、公式校验与带样式表格导出。
   - `docx-compose`：DOCX 标题/段落/表格/批注解析与排版级 Word 文档生成。
   - `pptx-compose`：PPTX 结构化读取与演示稿生成（含版式模板与防文字溢出检查）。
   - `chart-render`：高质量中文统计图表（PNG / SVG，内置中文字体与配色规范）。
   - `diagram-compose`：复杂 Mermaid / SVG / 思维导图的确定性语法校验与编译转图（输出高清 PNG/SVG 以嵌入 DOCX/PPTX/PDF）。
   - `markdown-publish`：将长篇 Markdown（含表格、公式、Mermaid 图、引用溯源）编译导出为排版级 PDF / DOCX 或独立可交互 HTML 报告（Artifact）。
   - `doc-diff`：两版合同、方案或报价单的结构化差异比对与风险点清单。

---

## 三、标准匠人矩阵（6 大核心 Agent）

### 1. 通用文档与数据分析匠人（`doc-analyst` / `general-workbench`，Super Agent 断网生产力底座）
- **状态**：M2 核心新建/由 [examples/office-assistant](../../../examples/office-assistant/xuanpu.yaml) 升级沉淀至 `lumorax-catalog/artisans/doc-analyst`。
- **承载与策略**：`host: task`（`accepts_materials: true`，`sandbox.shell: true`，**`sandbox.network: false` 完全断网**，`approval: standard`），依赖 `document-skills` + `common-skills`。
- **核心能力（单沙盒一站式闭环）**：
  - 承担玄圃里的 **Code Interpreter（Python 统计建模与数据清洗）+ 多格式文档解析 + 图表/图示渲染 + 富交付物制作（XLSX / DOCX / PPTX / PDF / PNG / 交互式 HTML 报告）**。
  - 支持跨格式复合任务一次派工完结（例如：同沙盒内先后按需加载 `pdf-parse` + `excel-analysis` → 内存跑 Python 比对分析 → `chart-render` 画图 → `pptx-compose` / `markdown-publish` 出稿），产物自动回写本人工作台。
- **Butler 联动**：员工上传 PDF/Excel/Word/PPT 或提出深度数据分析与成稿需求 → Butler 派给 `doc-analyst` 附带 `materials` → 完成后 Butler 唤醒验收、受控沉淀核心结论，并推送产物预览卡片。

### 2. 联网调研与出行规划匠人（`web-research` / `travel-planner`）
- **状态**：`web-research`、`travel-planner`、`driver-dispatch` 已落地于 `lumorax-catalog/artisans/`，复用 `web-skills`。
- **承载与策略**：`host: session`（通过受控搜索/地图/天气 HTTP 连接器访问外部，**脚本沙盒保持断网**，`approval: standard`）。
- **物理隔离原则**：与 `doc-analyst` 保持为**两个独立 Agent**，确保“联网采集”与“本地文件任意代码执行”物理分离。
- **入口能力**：
  - `web-research/deep-research`：公司工商与动态、人物公开履历、行业/竞品/政策调研（带来源链接与 `as_of` 日期戳，唤醒轮严守外源事实分级与防二阶注入缩权）。
  - `travel-planner/trip-plan`：结合客户端 `location`、实时天气与路线接口规划出行与出发窗口。

### 3. 批量图像与扫描件识别匠人（`vision-extractor`，重型视觉管道）
- **定位澄清（轻重分层）**：
  - **轻量高频（1–3 张名片照片、微信/飞书聊天长截图、白板照、行程单）** → 优先走管家侧**受控视觉直读**（1–3 秒同轮完成识别并落库，不走重型异步派工）；
  - **重型批量（多页扫描 PDF、批量发票/合同扫描件、大规模表格 OCR）** → 交由 `vision-extractor`（或并入 `doc-analyst` 的受控 OCR 管道）异步处理。
- **承载与策略**：`host: task`（`accepts_materials: true`，`sandbox.network: false` 或受控只读 OCR 连接器）。

### 4. 企业信息只读采集匠人（`wecom-readonly` / `feishu-readonly` / `mail-readonly`）
- **状态**：已有 [examples/wecom-readonly](../../../examples/wecom-readonly/xuanpu.yaml) 与 [examples/shared-reader](../../../examples/shared-reader/xuanpu.yaml)。
- **承载与策略**：`host: session`（本人显式授权只读连接器，所有操作为 `read`，严禁自动代发任何外部消息）。
- **入口规划**：按时间窗口拉取本人未读企业微信/飞书消息、邮件或共享事项变更，归纳为「需要回复 / 新增待办 / 关键进展」摘要，经 Butler 统一整理入图谱或纳入晨报。

### 5. 企业业务办理匠人（`finance` / `support-desk` / `crm-ops`）
- **状态**：已有 [examples/finance](../../../examples/finance/xuanpu.yaml)、[examples/support-desk](../../../examples/support-desk/xuanpu.yaml) 与 `lumorax-catalog/artisans/expense-helper` / `order-desk`。
- **承载与策略**：`approval: standard` 或 `strict`（只读查询放行；提交单据、金额付款、改优先级、对外发送等写操作一律走平台审批闸门，由员工在小院确认）；按需依赖 `document-skills` 复用发票/对账单解析与导出能力。

### 6. 周期复盘与简报匠人（`briefing-compiler`）
- **状态**：M3 规划，与 `task_triggers` + `proactive` + Attention Budget 联动，依赖 `document-skills`（复用 `markdown-publish` / `docx-compose`）。
- **承载与策略**：`host: task`（`accepts_materials: true`，`sandbox.shell: true`，`sandbox.network: false`）。
- **入口规划**：
  - `weekly-review`：汇总本周原始记录、事项进度、待办完成率与新拓展人脉，生成可交付周报（Markdown / DOCX）。
  - `meeting-prep`：会前基于参会人图谱、历史纪要与未决问题生成高信噪比会前准备包。

---

## 四、多匠人编组与有界动态接力剧本

1. **有界动态多步接力（M2 新增，≤3 跳预算受控）— 联网调研 + 本地数据分析 → 成品 PPT/PDF**：
   - `web-research` 联网采集目标公司与行业动态 → Butler 验收去噪并做外源安全隔离 → 自动续派断网的 `doc-analyst` 结合员工上传的 Excel 报价单跑 Python 对比、画图并生成 PPTX / HTML 报告 → Butler 综合收口交付产物卡片。
2. **接力（`relay`）— 票据/合同材料 → 结构化提取 → 业务办理**：
   - `doc-analyst`（或 `vision-extractor`）提取批量发票/合同要素 → 接力给 `finance` 核对并起草报销/付款单（写操作等员工确认）。
3. **并行（`parallel` + 编组综合收口轮）— 重要商务拜访一键准备**：
   - 并行派出 `web-research`（查对方公司与高管动态）+ `travel-planner`（算路线天气与出发时间）+ `shared-reader`（查团队共享资料），同时 Butler 整合本人图谱历史纪要，经编组终态「管家综合润色轮」自动生成《拜访准备一页纸》文档与 3 行摘要。
4. **会诊（`consult`）— 高价值方案/合同复核**：
   - 两个视角不同的审查匠人背靠背独立审阅同一份合同，Butler 按 `rate_divergence` 综合或将高分歧项交员工裁决。

---

## 五、验收与演进检查点（与总规划 M1–M4 对齐）

- [x] **底座与共享技能库范式打通**：`list_artisans` 透出完整能力画像；`web-skills` 纯共享技能库 + `web-research` / `travel-planner` 渐进披露装配已端到端验证；`act_personal(action="act_followup")` 已打通。
- [ ] **M1（P0）**：内核有界并发 + SOUL 分层注入 + 真撤回/唤醒轮缩权 + 同会话 FIFO/强制插入合并 + 流式规划分离 + 前端轻量 Markdown/Mermaid 预览。
- [ ] **M2（P1）**：落地纯共享技能库 `document-skills` 与断网通用匠人 `doc-analyst`；落地管家轻量图片/名片受控直读；打通有界动态多步接力（≤3 跳）与编组终态综合收口轮。
- [ ] **M3（P1–P2）**：落地 `briefing-compiler` + `task_triggers`/`proactive` 统一单人格触发与 Attention Budget；接入企业只读源同步。
- [ ] **M4（P2–P3）**：`pgvector` 混合召回、长期主题静默追踪、`web-skills` 只读无头浏览器渲染与高隔离 Browser Use 探索。
