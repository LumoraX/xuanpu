# 个人人物与关系使用手册

> 面向使用个人工作台的员工与开发者。设计见[纯个人空间「人物与关系」](../architecture/personal-people.md)。

## 控制台操作（「功能 → 人物与关系」）

1. **建档、编辑与搜索**：点击「新建人物」或卡片上的「编辑名片」填写/修改姓名、别名、公司、部门、职位、常驻城市、出生年份/月日（支持只填年份或只填月日，可选公历/农历）、多条联系方式、标签、备注与 `键=值` 扩展信息；支持按关键词或生日月份筛选。
2. **头像与照片**：在人物详情页上传或清除 JPEG / PNG / WebP 头像（最大 5 MiB）；名片原图或合影可先上传到「我的文件」，在新建/编辑表单里直接从个人文件勾选挂接。
3. **关系与项目联动**：在人物详情页点「添加关系」，可选择关联其他人物或个人「事项（项目）」并填关系类型、角色与开始日期；打开个人工作台的「事项详情」时会自动展示「关联人物」区并可一键跳回人物名片；人物详情页同步展示 1–2 跳关联网络。
4. **重复合并与回收站**：详情页支持「合并到其他档案」将重复名片软合并（保留原名作别名、合并联系方式/关系/笔记与头像），合并后页面顶部提供「撤销合并」一键还原；误删人物可在回收站恢复。
5. **批量文件导入（`.vcf` / `.csv`）**：在小院「书架资料 → 通讯录」点击「导入文件」或在控制台点击「导入 .vcf / .csv」，上传 Google Contacts / Apple iCloud / Outlook 等导出的通讯录文件，系统会先解析并与现有通讯录查重（含手机号格式归一化与文件内去重），可逐条或一键设置「合并更新 / 新建 / 跳过」后原子写入。

## 对话录入与查询

- **随手记名片与关系**：直接告诉助手（例如“刚和星辰科技的王建国聊了，86年生，李四介绍的，负责项目 A 供应链”），助手会自动查重并弹出待确认卡片；若命中疑似已有人物，可在确认卡下拉框直接切换为「更新已有」或「作为新人物建档」，确认后一次性保存名片、关系与原始摘录。
- **跨项目查人脉**：可直接询问“项目 A 涉及哪些人”“谁和王建国熟”“下个月有谁过生日”，助手通过只读工具检索本人人物档案与 2 跳关系图作答。

## HTTP 接口一览

| 方法与路径 | 说明 |
| :--- | :--- |
| `GET /v1/people` | 筛选本人人物（`q`、`tag`、`organization`、`birth_month`、`matter_id`、`trashed`） |
| `POST /v1/people` | 新建人物（需 `request_id`） |
| `GET/PUT/DELETE /v1/people/{id}` | 读取详情 / 版本 CAS 更新 / 移入回收站 |
| `POST /v1/people/{id}/restore` | 从回收站恢复 |
| `POST /v1/people/{id}/merge` · `/unmerge` | 可逆软合并与撤销合并 |
| `GET /v1/people/graph` | 读取本人全景关系图（`limit` 1–500、`include_isolated`） |
| `GET /v1/people/graph/{kind}/{id}` | 以人物/公司/事项/记录为中心展开 1–3 跳图谱与溯源记录 |
| `GET /v1/people/{id}/graph` | 展开 1–2 跳关系图（`depth`、`include_matters`） |
| `PUT/GET/DELETE /v1/people/{id}/avatar` | 上传 / 受权读取 / 删除头像 |
| `POST/PUT/DELETE /v1/people/relations[/{id}]` | 维护人↔人、人↔事项关系边 |
| `GET/POST/DELETE /v1/people/notes[/{id}]` | 维护原始互动记录 |
| `POST /v1/people/bundle` | 单事务原子提交多个人物、关系与笔记 |
| `POST /v1/people/import/preview` | 上传 `.vcf` / `.csv` 文件生成只读查重预览（≤ 5 MiB、≤ 1,000 人） |
| `POST /v1/people/import` | 幂等原子提交已确认的导入批次（新建 / 合并更新 / 跳过） |
| `GET /v1/people/google/status` | 查询本人 Google 通讯录关联状态（未配置时不发外网请求） |
| `POST /v1/people/google/connect/start` | 发起本人浏览器 Google OAuth2 + PKCE 授权跳转 |
| `GET /v1/people/google/callback` | 处理 Google 一次性 OAuth 回调并密文托管令牌 |
| `POST /v1/people/google/preview` | 只读拉取 Google 通讯录并与本地查重生成预览 |
| `POST /v1/people/google/sync` | 提交预览同步或执行一键增量同步（自动持久化 `syncToken` 与映射） |
| `POST /v1/people/google/unlink` | 撤销令牌并解除关联（保留已导入本地联系人） |
| `GET /v1/workspace/items/{matter_id}/people` | 按本人事项反查关联人物 |
