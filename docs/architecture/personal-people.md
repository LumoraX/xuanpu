# 纯个人空间「人物与关系（People）」

玄圃原生个人空间领域（`server/domains/people/`，迁移 `0033_personal_people`）。用于员工本人记录联系人名片、头像、出生信息、人际关系、项目（事项）参与角色与原始互动摘录。

## 隔离与权限边界

- 全部数据按 `(tenant_id, user_id)` 强隔离；机器 API key 无个人人物空间，管理员身份不授予读取其他员工私人人物记录的权限。
- 一期与企业组织架构（`organization`）和共享事项（`shared_matters`）保持解耦：不自动同步、不跨员工共享。
- 项目承接直接复用本人工作台「事项（`matter`）」，关系边支持指向本人有效事项；回收事项时不阻止回收，但禁止新建指向已回收事项的关系。
- 审计只记操作类型、实体类型、实体 ID 与版本号（`people.person.*`、`people.relation.*`、`people.note.*`、`people.avatar.*`），严禁将姓名、手机号、生日、头像路径或正文写入平台审计表。

## 数据模型与存储

控制面装配同一契约的 `MemoryPeopleStore` 与 `PostgresPeopleStore`（均通过 `ControlPlaneStore.people_store` 暴露），锁序沿用租户共享锁 → 员工排他锁。

1. **`xp_personal_person`（人物档案）**
   - 核心名片字段由 Pydantic 严格模型（`PersonProfile`）校验：`name`、`aliases`（≤20）、`gender`、`birth`（支持不完整公历/农历的 `year`/`month`/`day`/`calendar`）、`organization`、`department`、`title`、`city`、`contacts`（多条 phone/wechat/email/feishu/qq/other）、`tags`（≤30）、`summary`、`attachment_file_ids`（引用本人有效工作台文件，≤10）以及有界扁平扩展 `extra`（≤30 键）。
   - 头像字节存于现有 `ObjectStore`（键形如 `<tenant>/people-avatars/<user>/<person_id>/<token>`），仅接受魔数校验通过的 JPEG / PNG / WebP（≤5 MiB，禁止 SVG）；数据库只保存不可对外暴露路径的元数据。
   - 支持可逆软合并（`merged_into_id` + `merge_snapshot`）与回收站（`trashed_at` + `trash_batch`）。
2. **`xp_personal_relation`（关系边）**
   - 支持人↔人（`target_kind='person'`）与人↔本人事项（`target_kind='matter'`）；内置对称关系词表（如同事、前同事、朋友等）按字典序规范化端点，防止重复双向建边。
   - 带 `role`、`note`、可选起止时间 `valid_from`/`valid_to` 及证据引用 `source_note_id`。
   - 关系图查询（`GET /v1/people/{id}/graph`）做有界 1–2 跳 BFS（至多 50 节点、100 条边），包含共享同一事项的共事人物。
3. **`xp_personal_note`（原始互动记录）**
   - 保存名片原文、会面摘录、对话留痕，关联 `person_ids`、可选 `matter_id` 与名片原图/合影等 `attachment_file_ids`。
4. **`xp_personal_people_request`（幂等回执）**
   - 所有创建、合并、撤销合并及复合包提交均要求显式 `request_id`，同键同参返回原结果、同键异参返回 409。

## 双入口与管家集成

- **管家只读查询**：`ButlerTools.read_personal` 新增 `people_search`、`person_detail`（含 2 跳关系图）、`matter_people`。
- **管家复合确认卡**：`@people/record` 生成 `people_draft`（一次可含最多 5 个人物新建/更新、10 条关系与 1 条原始记录，并自动附带重名/同联系方式 `similar_candidates`）；经 `/v1/assistant/proposals/{request_id}/confirm` 由员工确认后调用 `PeopleService.apply_bundle` 原子落库。
- **手动兜底**：控制台「功能 → 人物与关系」（`web/static/people.js`）不依赖模型即可直接搜索、按生日月份筛选、建档、上传头像、维护关系与恢复回收站。

## 配额上限

- 每人最多 5,000 个活动人物、20,000 条活动关系、20,000 条活动笔记；头像单张最多 5 MiB。

## 后续演进预留

- **向量语义召回**：二期切至 `pgvector/pgvector:pg16` 后可新增独立派生表对 `person.summary` 与 `note.content` 建向量索引，与精确/关系图做混合召回，不改动一期表结构。
- **混合与共享**：未来如需团队共享名片或项目人脉，采用显式发布/授权投影机制，不默认暴露个人私密记录。
