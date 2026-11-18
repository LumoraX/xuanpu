# 本人知识与工作记忆

> 给员工与开启试点的平台管理员。设计见[本人知识与工作记忆](../architecture/knowledge-governance.md)。当前为源码实现，尚未生产部署。员工可在「功能 → 知识与记忆」里登记来源、检索、勾选依据提问、管理记忆；也可经 HTTP 接口、Python SDK（`XuanpuClient` 的 `search_knowledge`、`create_knowledge_context`、`ask_knowledge` 等）或命令行 `xuanpu knowledge` 使用。

## 页面操作

「功能 → 知识与记忆」：从我的文件登记来源（只列 UTF-8 文本类文件可登记成功）；输入词检索，勾选要作依据的片段后提问，回答下方列出引用的文件、版本与段落。依据失效时页面会先清掉旧回答，再提示重新检索。记忆列表里「本人陈述」表示未经材料核实；待确认的记忆可确认或不采纳，任何记忆都可删除。不用模型时，检索、来源与记忆管理照常可用。

## 开启

平台配置 `knowledge.enabled: true` 并在 `knowledge.tenants` 里列出企业；未开启的企业相关接口返回 403。

## 知识来源

- 把本人工作区里的一份文件的明确版本登记为来源：`POST /v1/me/knowledge/sources`，正文 `{"item_id": "...", "revision": 1}`。只支持 UTF-8 的 TXT、Markdown、CSV、JSON；扫描件、PDF、网页不支持。
- 检索：`POST /v1/me/knowledge/search`，正文 `{"query": "关键词", "limit": 10}`。只在你此刻能读到的来源里按字面找，结果给出来源、版本与位置。
- 原文件改了新版本，来源列表会提示「有更新」，检索仍按登记的版本；要用新版本请刷新：`POST /v1/me/knowledge/sources/{id}/refresh`。不需要的来源可删除：`POST /v1/me/knowledge/sources/{id}/delete`（都带 `expected_version`）。原文件删除或移入回收站后，来源自动失效。

## 依据材料提问

- 先检索，再从结果里选 1–10 段作依据建问答：`POST /v1/me/knowledge/contexts`，正文 `{"request_id": "...", "pins": [{"source_id": "...", "ordinal": 0}]}`。
- 提问：`POST /v1/me/knowledge/contexts/{id}/ask`，正文 `{"request_id": "...", "question": "..."}`。回答只依据你选的那几段，用 `[编号]` 标出依据，`citations` 给出对应的文件、版本与位置；找不到依据时会直说，`grounded` 为 `false`。材料里写的指令不会被执行。
- 查看依据正文与已有回答：`GET /v1/me/knowledge/contexts/{id}`；列出全部问答：`GET /v1/me/knowledge/contexts`。
- 依据的来源被删除、刷新到新版本，或原文件被移入回收站后，这个问答立即或在下次打开时失效，已有回答一并清掉，返回 `knowledge_source_revoked`。请重新检索、选依据，开一个新问答；恢复文件或重新授权也不会让旧问答复活。已经显示给你或送达模型的内容无法追回。
- 不再需要时可结束：`POST /v1/me/knowledge/contexts/{id}/close`（带 `expected_version`），回答正文会被清掉。

## 工作记忆

- 候选：`POST /v1/me/work-memory/proposals`，须引用你的一个知识来源的一段；确认后才生效（`/{id}/confirm`），也可以拒绝（`/{id}/reject`）。
- 你也可以直接记一条：`POST /v1/me/work-memory/assertions`，它会标明是你本人陈述、不是从材料核实的。
- 更正（`/{id}/correct`）会生成新修订，旧的不再生效；删除（`/{id}/delete`）后不再出现。依据的来源删除后，引用它的记忆也不再出现。
- 记忆只属于你本人，不会共享给同事，也不会因此给你任何新的权限。
