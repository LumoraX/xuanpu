# R4-D 来源知识与工作记忆治理实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans；先落实B的绑定和撤权接口。

**Goal:** 员工检索明确选定且当前有权的材料，看到版本和依据；记忆能由本人确认、更正与删除，撤权后不再继续使用旧内容。

**Architecture:** 既有工作区和R3共享材料仍是权限与原件正本。知识索引与记忆只是有来源的派生记录；复用受保护只读模型会话和PG维护循环，不接通任意外部抓取或向量记忆槽位。路径/夹具见[总计划](2026-10-25-r4-delivery.md)。

**Tech Stack:** asyncpg、现有ObjectStore、标准UTF-8/CSV/JSON解析、参数化关键词查询、R3来源依赖与公开模型包装。

---

## 来源、查询和删除契约

`source_ref`为严格联合类型：`workspace_file(item_id,revision)`、`shared_body(matter_id,body_revision,access_grant_id)`、`shared_material(matter_id,material_id,revision,access_grant_id)`；身份、对象键及SHA由服务端核验填充。共享项必须来自当前用户的明确R3固定资料包/用途，不凭整个matter_id授予搜索权。

每个source保存固定SHA、提取器版本、ACL依据/epoch、build_id。构建键`tenant+owner+source_ref+revision+sha+extractor_version`唯一；同文本但不同权限来源不能合并为一个跨人chunk。查询先取当前可读来源集合再匹配与统计，不得先查topK再过滤造成隐私和召回偏差。正文摘要也属于受保护内容。

上限：200个来源/库、50MiB总文本、25000个chunk；每文件20MiB、一次8份；query≤500字符，最多10命中、每条≤2000字符。UTF-8 TXT/MD/CSV/JSON以实际解码和格式验证为准，拒绝二进制/不支持格式；不解析PDF/OCR/网页/音视频。关键词`%/_`按字面转义，不拼SQL；中文连续文本、空白及标点有确定用例。采用有界扫描的首期结果，不宣称向量语义召回。

拟接口：POST `/v1/me/knowledge/sources`登记明确来源；GET `/v1/me/knowledge/sources`；POST `/v1/me/knowledge/search`；POST `/v1/me/knowledge/contexts`创建固定命中集只读会话；POST `/v1/me/knowledge/sources/{id}/refresh|delete`；GET `/v1/me/work-memory`、POST `/v1/me/work-memory/proposals`、POST `/v1/me/work-memory/proposals/{id}/confirm|reject`、POST `/v1/me/work-memory/{id}/correct|delete`。所有mutation有操作号/CAS，删除先逻辑撤权再有界物理清理。

本轮知识仅用于受保护阅读与可核实引用；**不把检索正文或共享知识自动交给R2沙盒，不新增“有知识就能执行”的工具**。本人上传的办公文件仍按R1/R2明确选择执行；若未来要以共享知识作执行输入，必须单独设计R3用途授权与派生输出权限，不能在E样板绕过。

## D0：固定来源、索引构建与查询

**Files**：Create `S/knowledge/{models,config,store,memory,postgres,sources,extractors,chunks,search,builds,service}.py`、`S/api/knowledge.py`、`S/controlplane/migrations/0023_knowledge_governance.sql`、`T/r4_contract/{test_knowledge_sources,test_knowledge_builds,test_knowledge_search}.py`；Modify `S/platform.py`、`S/config.py`、`S/api/app.py`、`S/controlplane/schema.py`。

- [ ] 创建本人文件、明确共享packet、异租户同名材料、无权同文本四类seed；新增r4_ids的`source_input/secret_marker/knowledge_source_id`。证明不得从管理员视角爬取全库。
- [ ] 先写范围、格式、SHA改变、空间上限、中文检索、字面通配符、空结果、无权计数反例；memory/PG同契约，源SHA不一致标stale而不是重新取最新版。
- [ ] 构建持久意图/租约，在事务外提取，事务内验证来源epoch和空间预留后提交；进程故障重新领取同意图，不形成双chunk；清理失败不提前释放已占容量。
- [ ] 注册来源并不复制分享权；源新版本只标“有更新”，用户明确refresh产生新source修订并使原contexts失效，不改旧引用。
- [ ] Run：`uv run pytest packages/xuanpu-server/tests/r4_contract/test_knowledge_sources.py packages/xuanpu-server/tests/r4_contract/test_knowledge_builds.py packages/xuanpu-server/tests/r4_contract/test_knowledge_search.py -q`；提交`feat: 增加有权固定来源检索`。

```python
async def test_search_does_not_reveal_other_users_source(r4_http, r4_ids):
    """正文、命中数和引用都不泄露无权来源。"""
    response = await r4_http['bob'].post('/v1/me/knowledge/search', json={
        'query': r4_ids['secret_marker'], 'limit': 10})
    assert response.status_code == 200
    assert response.json()['hits'] == []
    assert response.json()['total'] == 0
```

## D1：受保护问答与撤权/删除传播

**Files**：Create `S/knowledge/{contexts,dependencies,revocations,cleanup,read_tools,views}.py`、`T/r4_contract/{test_knowledge_context,test_knowledge_revocation,test_knowledge_faults}.py`、`scripts/acceptance/r4_knowledge_worker.py`；Modify `S/runtime/{source_guard,source_lifecycle}.py`、`S/shared_matters/{context_guard,source_dependencies}.py`、`S/api/history.py`及`S/assistant.py`的新增独立知识路由接缝。

- [ ] 提取通用来源复核接口，仅抽离R3已有检查的共性；R3只读模式保持原白名单。新增知识只读工具只读服务端固定context，不带脚本、写文件、连接器或子任务。
- [ ] 每次模型发送前/流块外发/历史读取/引用下载复核来源；来源撤销或仓储故障拒绝，不让复用引擎、历史摘要和缓存回填旧正文。已发送到设备/模型的数据不宣称追回。
- [ ] 删除先增加来源epoch、阻塞检索和旧context；清理worker删除chunk、提取文本及回答缓存，保留不含正文的删除回执。`cleanup_pending`重启续做，不能按清理失败自动重新授权。
- [ ] 测试对象读到一半撤权、模型stream中撤权、搜索响应迟到、删除后冷解释器恢复、结果被发布成新材料时仍带来源依赖；禁止通过转存取消原权限。
- [ ] Run：`uv run pytest packages/xuanpu-server/tests/r4_contract/test_knowledge_context.py packages/xuanpu-server/tests/r4_contract/test_knowledge_revocation.py packages/xuanpu-server/tests/r4_contract/test_knowledge_faults.py -q`；实际SIGKILL分支单列，不能以memory状态模拟PG恢复。
- [ ] 提交`feat: 保护知识问答来源与删除传播`。

## D2：本人工作记忆与原生入口

**Files**：Create `S/knowledge/{work_memory,memory_proposals}.py`、`S/api/work_memory.py`、`C/{knowledge,work-memory}.js`、`SDK/knowledge.py`、`CLI/knowledge_commands.py`、`T/r4_contract/{test_work_memory,test_knowledge_views}.py`、`Q/{r4_knowledge_behavior.cjs,test_r4_knowledge_behavior.py}`；Modify `C/{index.html,workspace.js,workspace-chat.js}`、`SDK/client.py`、`CLI/main.py`。

- [ ] 个人偏好继续由现有助手偏好管理；新记忆类型限定`confirmed_fact`，必须显示原文、来源引用、有效期及“本人确认”。模型只提建议，不能静默确认为永久事实。
- [ ] 用户手工输入事实标`user_assertion`而非“已核实”；模型提出的事实必须携带当前有权来源。纠正生成新修订，旧记忆标superseded；删除/过期增加epoch并失效所有依赖的contexts/摘要缓存，沿D1清理路径处理。
- [ ] 不自动把私人记忆共享给同事/岗位，不以记忆授予能力或审批权。受保护记忆只进入受保护只读回答，不能在普通助理路由提示中混入全文。
- [ ] 默认对话可请求查找材料或提出记忆候选；确认卡和「功能 → 知识与记忆」使用同一服务。无模型仍可检索、看来源、确认/拒绝、更正、删除及查看清理状态。
- [ ] 执行浏览器双身份、390px、Tab、撤权首帧清屏；实际模型测试只读引用、材料内注入指令、无依据拒答，原始协议与独立判定分列。
- [ ] Run：`uv run pytest packages/xuanpu-server/tests/r4_contract/test_work_memory.py packages/xuanpu-server/tests/r4_contract/test_knowledge_views.py -q`和全门禁；实施时新增`docs/architecture/knowledge-governance.md`、`docs/guides/knowledge-memory.md`；提交`feat: 提供本人知识和工作记忆治理`。

本段不接入员工聊天全历史、不复制财务原件、不启用长期记忆自动学习；真材料和真人检索验收继续暂缓，不能将合成来源正确引用写成企业知识全面可用。
