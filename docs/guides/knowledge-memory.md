# 本人知识与工作记忆

> 给员工与开启试点的平台管理员。设计见[本人知识与工作记忆](../architecture/knowledge-governance.md)。当前为源码实现，尚未生产部署；原生界面尚未提供，目前经 HTTP 接口使用。

## 开启

平台配置 `knowledge.enabled: true` 并在 `knowledge.tenants` 里列出企业；未开启的企业相关接口返回 403。

## 知识来源

- 把本人工作区里的一份文件的明确版本登记为来源：`POST /v1/me/knowledge/sources`，正文 `{"item_id": "...", "revision": 1}`。只支持 UTF-8 的 TXT、Markdown、CSV、JSON；扫描件、PDF、网页不支持。
- 检索：`POST /v1/me/knowledge/search`，正文 `{"query": "关键词", "limit": 10}`。只在你此刻能读到的来源里按字面找，结果给出来源、版本与位置。
- 原文件改了新版本，来源列表会提示「有更新」，检索仍按登记的版本；要用新版本请刷新：`POST /v1/me/knowledge/sources/{id}/refresh`。不需要的来源可删除：`POST /v1/me/knowledge/sources/{id}/delete`（都带 `expected_version`）。原文件删除或移入回收站后，来源自动失效。

## 工作记忆

- 候选：`POST /v1/me/work-memory/proposals`，须引用你的一个知识来源的一段；确认后才生效（`/{id}/confirm`），也可以拒绝（`/{id}/reject`）。
- 你也可以直接记一条：`POST /v1/me/work-memory/assertions`，它会标明是你本人陈述、不是从材料核实的。
- 更正（`/{id}/correct`）会生成新修订，旧的不再生效；删除（`/{id}/delete`）后不再出现。依据的来源删除后，引用它的记忆也不再出现。
- 记忆只属于你本人，不会共享给同事，也不会因此给你任何新的权限。
