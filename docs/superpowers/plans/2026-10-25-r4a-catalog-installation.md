# R4-A 能力目录与安装契约实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans，逐工作包执行并保存反例结果。

2026-10-25完成：A0–A2本地技术门禁通过，交付边界和实际结果见[验收记录](../../guides/releases/2026-10-25-r4a-acceptance.md)。本段不含生产部署。

**Goal:** 把已有业务包整理成有安装条件、精确权限和版本依据的能力，形成不含秘密的交付描述及本地安装预检。

**Architecture:** 保留`PackageRegistry`为包归档唯一正本，新增`capabilities/`记录能力修订与安装状态；目录和管理功能不创建任务或授予数据权。开工约束、路径缩写和夹具定义见[R4总计划](2026-10-25-r4-delivery.md)。

**Tech Stack:** Pydantic、asyncpg、既有ObjectStore/EncryptedVault、FastAPI、原生JS；不新增包管理器。

---

## 契约先冻结

`delivery.json`的字段：`format=xuanpu-delivery/v1`、`capability_key`、`title`、`summary`、`scenario_key`、`package={name,version,digest,entry}`、`inputs`、`outputs`、`permissions`、`requirements`、`evaluation_suite_sha256`。描述文件自身以规范化JSON计算SHA256；不读取秘密值，不含租户和员工ID。输入/输出首期仅允许已有`office_files`、`capability_receipt`及`read_only_answer`契约标识，不接受Python表达式或可执行验证器。

`permissions`包含脚本/出网开关、工具名、参数schema摘要、effect、连接安装引用类型、模型层级和预算上限。归档digest不等于远端工具集合固定：MCP在预检与每次新装配时取工具清单，固定完整名称+schema+effect摘要，实际权限只取批准集合交集；新增工具或schema增长使安装stale，拒绝新运行。远端实现没有可验证部署版本时标`implementation_unverified`，不能宣称旧评测证明同实现。

安装配置只允许公开配置键和秘密引用；未知键422，回显只显示“已配置/缺失”，不显示秘密值。预检固定包、交付描述、配置修订、服务器策略和工具集合摘要，15分钟有效。应用时再次核对当前版本；对象/服务不可用为`unavailable`，不能写成`ready`。

同租户`package+entry`永久映射一个capability_key；同包多entry可登记不同能力。登记和staged预检不改变旧包权限，C首次发布才原子设置包级managed标记；之后未登记入口及旧显式version直调均拒绝。首次纳管先排空无绑定的在途任务/会话并暂停该包旧后台安排，不能给旧工作猜授权。终态与unknown台账只读核实不阻塞纳管，也不能因此新发动作。

拟新增接口：

| 接口 | 作用与返回 |
| --- | --- |
| POST `/v1/capabilities` | 管理员登记不可变修订；body含request_id、delivery；201返回capability_id/revision/digest |
| GET `/v1/capabilities` | 管理列表与安装状态；员工不能用此接口枚举未分配包 |
| POST `/v1/capabilities/{id}/install-previews` | body含request_id、revision、public_config、secret_refs；返回id/digest/requirements/diff，不写正式配置 |
| POST `/v1/capabilities/{id}/installations` | body含request_id、preview_id、expected_version、confirmed=true；保存staged配置，A阶段不激活执行 |
| POST `/v1/installations/{id}/state` | CAS变更ready/draining/disabled；ready需B/C发布门禁，A阶段无有效release则拒绝 |
| GET `/v1/me/capabilities` | B完成前返回明确空的可运行集合；无员工机器403；不是所有租户active包的映射 |

## A0：契约、兼容和公共夹具

**Files**：Create `S/capabilities/{models,delivery,config,contracts}.py`、`T/r4_contract/{conftest,seed,client_helpers,test_delivery_contract}.py`；Modify `S/config.py`。

- [x] 写严格模型：拒绝额外字段、空工具白名单的远端能力、不在entry清单的入口、与归档不符的digest、非法secret引用与未声明配置键；对旧`xuanpu/v1`包解析不变。
- [x] 创建总计划中的合成身份/包/时钟夹具，memory/PG共同参数化；同时seed一个未纳管旧包，验证关闭R4时既有兼容行为。
- [x] 先运行下方反例，确认因尚无交付契约失败，再最小实现独立交付描述解析和规范化摘要。
- [x] 配置默认关闭、空名单拒绝；测试R2既有空名单语义没有被顺手改变。
- [x] 契约/夹具反例分别留证；随A0–A2完整实现检查点提交，避免提交尚未装配的平台状态。

新增测试的最小反例（夹具字段由本任务seed定义）：

```python
async def test_member_cannot_register_delivery(r4_http, r4_ids):
    """普通成员不能把描述文件当作发布许可。"""
    response = await r4_http['alice'].post('/v1/capabilities', json={
        'request_id': 'register-denied', 'delivery': r4_ids['delivery']})
    assert response.status_code == 403


async def test_registration_does_not_grant_execution(r4_http, r4_ids):
    """登记不可变修订不会自动进入员工可运行目录。"""
    response = await r4_http['admin'].post('/v1/capabilities', json={
        'request_id': 'register-one', 'delivery': r4_ids['delivery']})
    assert response.status_code == 201
    view = await r4_http['alice'].get('/v1/me/capabilities')
    assert view.json()['capabilities'] == []
```

Run：`uv run pytest packages/xuanpu-server/tests/r4_contract/test_delivery_contract.py -q`。预期目标反例转绿；不允许通过删除403或自动创建分配实现。

## A1：不可变登记、安装预检与事务

**Files**：Create `S/capabilities/{store,memory,postgres,service,preflight,tool_inventory,credential_bindings,receipts}.py`、`S/controlplane/migrations/0020_capability_delivery.sql`、`S/api/capabilities.py`、`T/r4_contract/{test_installation,test_install_races,test_tool_inventory,test_credential_revision}.py`；Modify `S/controlplane/{schema,store,memory,postgres}.py`、`S/secrets.py`、`S/platform.py`、`S/api/app.py`。

- [x] 固定登记唯一键`(tenant_id,capability_key,revision)`；重复同操作号回原登记，不同描述409；不同操作号不能覆盖旧修订。每个安装配置不可改写，只追加修订。
- [x] 预检验证归档存在/摘要/entry、沙盒能力、连接及secret引用存在、工具实际清单、预算、输入输出契约。管理员有配置权不等于可读取员工材料，预检不用员工材料。
- [x] 同名secret覆盖也必须失效旧安装依据：记录SecretRecord密文SHA作为内部变化指纹，不对秘密明文做哈希或公开密文指纹。新增vault `get_bound(tenant_id,name,expected_record_digest)`在一次读取后比对并解密同一记录，使用既有AESGCM实现；不能“先核版本再另读新值”。覆盖/删除/重新加密后旧运行时拒绝并重新预检，回收持有旧凭据的缓存。
- [x] 事务只做版本/epoch/幂等/审计提交；包解压、对象读和远端清单采集在事务外，提交前重查所有前提版本。预检超时或并发改变配置/工具清单时409重新预检。
- [x] memory在异常时完整回滚；PG记录带租户外键，测试跨租户preview/installation都404。核对新表编号未被并行分支占用后才新增SQL。
- [x] 写失败/重放/权限差异测试，运行`uv run pytest packages/xuanpu-server/tests/r4_contract/test_installation.py packages/xuanpu-server/tests/r4_contract/test_install_races.py packages/xuanpu-server/tests/r4_contract/test_tool_inventory.py -q`。期望一个安装事实、一次审计，没有秘密回显。
- [x] 另运行`uv run pytest packages/xuanpu-server/tests/r4_contract/test_credential_revision.py -q`：同引用换值、同秒两次写入、删除重建、出站取值窗口轮换均不能沿用旧安装；无凭据的办公样板不依赖此secret分支。
- [x] 安装预检与修订回执随A0–A2完整实现检查点提交。

## A2：原生管理与兼容边界

**Files**：Create `C/capabilities.js`、`SDK/capabilities.py`、`CLI/capabilities.py`、`T/r4_contract/test_catalog_views.py`、`Q/{r4_catalog_behavior.cjs,test_r4_catalog_behavior.py}`；Modify `C/{index.html,app.js,workspace.js}`、`Q/test_static.py`、`SDK/client.py`、`CLI/main.py`及`S/assistant.py`的目录服务接缝。

- [x] 员工只读入口放「功能 → 我的能力」，管理员折叠入口提供登记、差异、预检、配置；状态显示“尚未分配/未通过发布检查”，不展示假运行按钮。
- [x] 安装确认展示出网、工具、连接、数据范围和确认等级差异；秘密只显示配置状态。导出delivery不含tenant/employee、Secret、源材料或机器路径。
- [x] 断连重试沿用request_id，身份切换清空缓存，409重新核对。使用真实API响应做浏览器测试，不让前端默认把缺失计数或状态补成成功。
- [x] 拟新增命令`xuanpu capabilities list/register/install-preview/install`，SDK映射相同API；命令输出、文档仅在实现并实际运行后成为使用手册。
- [x] 运行`uv run pytest packages/xuanpu-server/tests/r4_contract/test_catalog_views.py -q`及总门禁；刷新内容指纹脚本，原生桌面和390px复核入口与Tab焦点。
- [x] 实施时新增`docs/architecture/capability-delivery.md`与`docs/guides/capability-delivery.md`，原生入口与使用手册随A0–A2完整实现检查点提交。

## 本段交付边界

目录和安装基础完成；分配与运行权限归B，发布为ready归C。不得把“可登记/已配置”显示成“业务能力已可使用”。本轮不运行任何真实员工/财务/外部写操作。
