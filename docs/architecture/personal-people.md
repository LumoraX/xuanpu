# 纯个人空间「个人知识图谱与关系（People & Graph）」

玄圃原生个人空间领域（`server/domains/people/`，迁移 `0033_personal_people` + `0034_personal_knowledge_graph`）。以「原始记录（随手记、会议纪要、名片、通话）」为事实源头，沉淀员工本人的公司/组织、人物名片、项目事项、待办/日程/提醒及跨实体关系网络。

## 隔离与权限边界

- 全部数据按 `(tenant_id, user_id)` 强隔离；机器 API key 无个人空间权限，管理员身份不授予读取其他员工私人记录的权限。
- 与企业后台正式组织架构（`organization`）和共享事项（`shared_matters`）保持解耦：不自动同步、不跨员工共享。
- 项目承接与会议行动项直接复用本人工作台「事项（`matter`）/ 待办（`todo`）/ 日程（`event`）」与「提醒（`reminders`）」；回收事项时不阻止回收，但禁止新建指向已回收事项的关系。
- 审计只记操作类型、实体类型、实体 ID 与版本号（`people.org.*`、`people.person.*`、`people.relation.*`、`people.note.*`、`people.avatar.*`），严禁将姓名、公司名、手机号、生日、头像路径或正文写入平台审计表。

## 数据模型与存储

控制面装配同一契约的 `MemoryPeopleStore` 与 `PostgresPeopleStore`（均通过 `ControlPlaneStore.people_store` 暴露），锁序沿用租户共享锁 → 员工排他锁。

1. **`xp_personal_org`（公司/组织轻档案，迁移 0034）**
   - 纯个人视角的公司/组织档案（`OrgProfile`）：`name`、`aliases`（简称/曾用名）、`org_type`、`industry`、`city`、`address`、`website`、`tags`、`summary`、`extra`，支持软删除与版本乐观锁。
2. **`xp_personal_person`（人物档案）**
   - 核心名片字段由 Pydantic 严格模型（`PersonProfile`）校验：`name`、`aliases`（≤20）、`gender`、`birth`（支持不完整公历/农历的 `year`/`month`/`day`/`calendar`）、`organization`、`department`、`title`、`city`、`contacts`（多条 phone/wechat/email/feishu/qq/other）、`tags`（≤30）、`summary`、`attachment_file_ids`（引用本人有效工作台文件，≤10）以及有界扁平扩展 `extra`（≤30 键）。
   - 头像字节存于现有 `ObjectStore`（键形如 `<tenant>/people-avatars/<user>/<person_id>/<token>`），仅接受魔数校验通过的 JPEG / PNG / WebP（≤5 MiB，禁止 SVG）；数据库只保存不可对外暴露路径的元数据。
   - 支持可逆软合并（`merged_into_id` + `merge_snapshot`）与回收站（`trashed_at` + `trash_batch`）。
3. **`xp_personal_relation`（跨实体关系边，迁移 0034 泛化）**
   - `from_kind` 与 `target_kind` 均覆盖 `person` / `org` / `matter`，原生表达公司母子（`subsidiary_of`）、人任职公司（`works_at`）、人际关系（`friend`/`colleague`/`introduced_by`）、人参与项目（`matter_role`）与公司合作项目（`org_matter_role`）。
   - 带 `role`、`note`、可选起止时间 `valid_from`/`valid_to` 及溯源引用 `source_note_id`。
   - 跨实体图谱查询（`GET /v1/people/graph/{entity_kind}/{entity_id}?depth=1..3`）支持以任意 `person`/`org`/`matter`/`record` 为中心做 1–3 跳 BFS 穿透并附带溯源原始记录；中心为 `matter` 时自动聚合关联待办、日程与活跃提醒全景。
   - 全景关系图查询（`GET /v1/people/graph?limit=1..500&include_isolated=false`）按连接度与活跃度有界返回本人全部活动人物/公司/事项节点及关系边，供前端小院「书架资料 → 关系图」（`web/static/people-graph.js`）做零依赖原生 SVG 力导向全景、1–3 跳聚焦穿透、类型筛选与同公司虚线聚合展示。
4. **`xp_personal_note`（通用原始记录，迁移 0034 升格）**
   - 支持 `record_kind`（`note` / `meeting` / `card` / `call` / `other`）、`title`、`location`、`occurred_at`、`end_at`、`content`、`summary`、`decisions[]`、`open_questions[]`，并双向记录关联的 `person_ids`、`org_ids`、`matter_ids`、`todo_ids`、`event_ids`、`reminder_ids` 与 `attachment_file_ids`（允许纯会议记录零人物独立存盘）。
5. **`xp_personal_people_request`（幂等回执）**
   - 所有创建、合并、撤销合并及复合包提交均要求显式 `request_id`，同键同参返回原结果、同键异参返回 409。

## 双入口与管家集成

- **管家五类诉求与只读查询**：`ButlerTools.read_personal` 覆盖 `orgs_search`、`people_search`、`matters`、`records_search`（按类型/地点/关键词）、`entity_graph`（1–3 跳穿透 + 项目全景）、`person_detail` 与 `matter_people`。
- **管家复合拆解确认卡**：`@people/record` 生成 `people_draft`（`record` + 最多 8 家公司 + 10 位人物 + 8 个事项 + 15 条待办 + 8 条日程 + 8 条提醒 + 30 条跨实体关系）；提案解析自动附带 `similar_orgs` / `similar_candidates` / `similar_matters` 并对更新已有公司/人物做安全增量合并；小院确认卡（`web/static/capture-card.js`）提供「确认保存全部」与「仅存原始记录」两个动作，经 `/v1/assistant/proposals/{request_id}/confirm` 调用 `apply_people_bundle` 幂等落库。
- **手动兜底与图谱浏览**：控制台「功能 → 人物与关系」（`web/static/people.js`）提供 **人物 / 公司 / 原始记录** 三视图切换，可直接搜索、建档、查看公司 2 跳图谱与原始记录拆解分支。
- **通讯录文件导入**：`server/domains/people/importer.py` 支持解析 Google Contacts、Apple iCloud、Outlook 等导出的 `.vcf`（vCard 2.1/3.0/4.0）与 `.csv`（含 UTF-8 / UTF-16 / GB18030）文件（单文件 ≤ 5 MiB，单批 ≤ 1,000 人）。采用「只读预览查重（`POST /v1/people/import/preview`）→ 逐条选择新建/增量合并/跳过 → 幂等原子提交（`POST /v1/people/import`）」两步流程；合并保留本地主姓名与已填字段并仅记录脱敏审计 `people.import.committed`。
- **Google 账号只读关联（默认关闭）**：`server/domains/people/google_*.py` 与迁移 `0035_personal_google_contacts` 提供基础能力：采用平台配置段 `google_contacts`（默认 `enabled=false`），OAuth2 + PKCE（`S256`）+ 一次性 `state` 与 `HttpOnly` Cookie 回调，使用 `ScopedCipher` 密文保存 `access_token` / `refresh_token`，按 `resourceName` 映射与 `syncToken` 支持预览合并与增量同步，解绑时尽力撤销令牌并保留本地联系人；审计仅记 `people.google.linked` / `synced` / `unlinked`。

## 配额上限

- 每人最多 5,000 个活动人物、20,000 条活动关系、20,000 条活动笔记；头像单张最多 5 MiB。

## 后续演进预留

- **向量语义召回**：二期切至 `pgvector/pgvector:pg16` 后可新增独立派生表对 `person.summary` 与 `note.content` 建向量索引，与精确/关系图做混合召回，不改动一期表结构。
- **混合与共享**：未来如需团队共享名片或项目人脉，采用显式发布/授权投影机制，不默认暴露个人私密记录。
