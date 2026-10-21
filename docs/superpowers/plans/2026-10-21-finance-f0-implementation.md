# 财务 F0 发票基座 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development for implementation and independent spec/quality review. Independent platform integration and finance service files may be delegated under superpowers:dispatching-parallel-agents; never edit the same files concurrently. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 交付独立运行、独立 PostgreSQL 与原件存储的发票 F0：受权导入、受控解析、判重、来源追溯、持久异常/HITL 补件和进度查询。

**Architecture:** 财务服务不被平台进程导入，也不依赖 taifeng。平台只提供通用请求签名、在线身份核对和受权文件桥；财务使用自己的数据库、对象凭据和持久 worker。接口只开放 F0 工具，付款、记账、税务查验和正式归档保持未实现／未启用。

**Tech Stack:** Python 3.12、FastAPI/Pydantic、asyncpg/PostgreSQL、S3 兼容对象存储、cryptography、PyJWT EdDSA、defusedxml、httpx、pytest。成熟库承担加密、签名及 XML 安全解析，不自行实现这些算法。

## 运行基线和隔离

批准设计：[总设计](../specs/2026-10-21-finance-capability-design.md)与[F0](../specs/2026-10-21-finance-f0-design.md)。
从 `354d51b` 创建 `feat/finance-f0`，检出 `/Volumes/Codes/lumorax/lumorax-platform/xuanpu-finance-f0`。
应用 worktree 工具因上层工作目录不是 Git 仓库而失败，改用 Git 原生检出；相邻 taifeng-* 路径继续有效。
不修改生产数据库、现有 Compose 或现有工作目录。所有测试数据、服务和凭据独立且标明测试用途。

实现文件以该检出根为准。下列 API 返回结构对应 `docs/superpowers/specs/finance-v1/pack/apis/finance.yaml`；业务包只发布已实现操作。
财务 Principal 只含已验签 tenant_id/user_id，公司与数据范围必须查财务持久授权表；平台身份在线复核负责实际员工停用，财务库负责公司角色。
HITL 补件复用 importInvoices：增加可选 source_exception_id 与 expected_exception_version，二者同时出现；
原异常与新原件在成功解析事务中关联并按规则关闭，补件失败不关闭原异常，也不把材料补齐等同财务审批。

## 工作包 1 通用可信身份与文件桥

**Files:** Create `packages/xuanpu-spec/src/xuanpu_spec/call_context.py`, `packages/xuanpu-spec/tests/test_call_context.py`, `packages/xuanpu-server/src/xuanpu_server/runtime/call_context.py`, `packages/xuanpu-server/src/xuanpu_server/api/file_bridge.py`, `packages/xuanpu-server/tests/test_call_context.py`, `packages/xuanpu-server/tests/workspace/test_api.py`; Modify `runtime/{connectors,http_api,assembler}.py`, `config.py`, `bootstrap.py`, `api/app.py`, `xuanpu_spec/manifest.py` and package metadata as needed.

- [x] 增加请求规范化的失败测试：body 键顺序相同摘要一致；修改 Idempotency-Key、方法、路径、query 或 body 摘要必须变化；拒绝非有限数值。公开 `request_fingerprint(method,path,query,body,idempotency_key,content_type)`，query 为键值对列表，JSON 排序且禁用 NaN。

```python
def test_idempotency_key_is_signed() -> None:
    first = request_fingerprint("POST", "/v1/imports", [], {"files": []}, "one", "application/json")
    second = request_fingerprint("POST", "/v1/imports", [], {"files": []}, "two", "application/json")
    assert first != second
```

- [x] 运行 `uv run pytest packages/xuanpu-spec/tests/test_call_context.py -q` 确认失败；实现纯标准库规范化函数，不向规范包引入平台或财务依赖。
- [x] 用真实 Ed25519 密钥测试平台 signer 输出：iss、aud、kid、sub、tenant_id、session_id、call_id、iat、exp、jti、request_sha256；使用 PyJWT EdDSA，有效期60秒、容差5秒。签名主体来自持久会话对照，不来自模型参数。
- [x] 连接器显式声明 `contextAudience`，平台配置按目标 origin 绑定允许的 audience 与租户签名密钥名。没有配置时拒绝请求该能力的包，不影响旧连接器；保留头 X-Xuanpu-Context 永远不允许模型或 OpenAPI 覆盖。
- [x] 在审批通过后的实际 HTTP 出口注入签名，绑定真实请求（含幂等键），凭据不进入模型／日志。测试恶意头、无员工会话、跨租户上下文、同一包不同员工及普通连接器兼容性。
- [x] 增加通用 `/v1/integration-gateway/identity` 与 `/v1/integration-gateway/files/{id}`：沿用 Caller/API key act-as 与内容权限；文件要求 revision、sha256，下载后再次核对并限量返回，禁止 URL／对象键。财务仅持有获准的平台服务 API key，不持有平台数据库或对象凭据。
- [x] 运行 `uv run pytest packages/xuanpu-server/tests/test_call_context.py packages/xuanpu-server/tests/workspace/test_api.py packages/xuanpu-server/tests/test_http_api.py packages/xuanpu-server/tests/test_http_connector.py -q`；实际检验停用员工与他人文件拒绝。

## 工作包 2 独立财务领域、存储与受控解析

**Files:** Create `packages/xuanpu-finance/{pyproject.toml,README.md}`, `src/xuanpu_finance/{__init__,models,errors,objects,operations}.py`, `src/xuanpu_finance/invoices/{__init__,parser,worker}.py`, `src/xuanpu_finance/storage/{__init__,store,imports,queries,worker,originals,common,schema}.py`, `tests/{conftest,test_parser,test_storage,test_worker}.py`。以下短路径均在此新包。

- [x] 先用 parser 测试固定预期：合法 synthetic XML 保留原值／路径／行金额；DTD、XXE、超深、超大、未知 namespace 拒绝；多税率按行处理；金额必须有限、合法且关系一致。

```python
def test_external_entities_never_parse() -> None:
    with pytest.raises(FinanceError):
        parse_invoice(b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>', synthetic=True)
```

- [x] 实现 `FinancePrincipal(tenant_id,user_id)`、严格 FileRef/InvoiceImport 模型和稳定 FinanceError(code,status,message)。合成 parser 仅允许明确 test/development 配置，生产未知格式保存隔离异常；未取得真实标准包与票据时不得声称 production parser 验收通过。
- [x] 在独立真实 PG 实例上写失败测试：同键异内容409；并发同票仅一主票；主票与首事件事务原子；跨公司／员工查询拒绝；查看分页 total 独立于 limit；禁止平台管理员自动获得正文权限。
- [x] 实现 `FinanceStore(pool)` 的 migrate、seed_company、grant、check_access、accept_import/get_operation/cancel_operation、list/get invoices、lines/fields/evidence、list_exceptions/list_companies/list_integrations。所有查询显式 tenant 与主体范围，PK/FK 都在财务库内。
- [x] 对象适配器只接受财务独立根／桶及凭据，异步限量读写，原件加密保存并核对摘要；普通对象保存不冒充 WORM。存储写入和数据库责任索引先行，故障保留可追溯对象并恢复。
- [x] 实现 `FinanceWorker(store,objects,gateway,synthetic=False)`，gateway 接口为 async active(who)->bool、download(who,file)->(bytes,mime,name)；worker.run_once 持久认领一个批次，每项独立 checkpoint，处理结果明确 partial。已提交项不重放；第二 worker 实例通过独占数据库持有锁拒绝。
- [x] PDF／OFD／图片创建 awaiting_original 及持久异常；源异常补件只能由有权原提交者或财务角色处理，需版本和新原件，解析成功同事务关联关闭，失败保留待办。模拟重启后重新读出待办并继续补件。
- [x] 运行 `uv run pytest packages/xuanpu-finance/tests/test_parser.py packages/xuanpu-finance/tests/test_storage.py packages/xuanpu-finance/tests/test_worker.py -q`。对数据库用真实连接，不以 mock SQL 或内存锁代替。

## 工作包 3 独立服务身份、API 与生命周期

**Files:** Create `src/xuanpu_finance/{auth,gateway,api,config,__main__}.py`, `tests/{test_auth,test_api,test_lifecycle}.py`；Modify root `pyproject.toml`/`uv.lock`（仅集成者维护）。

- [x] 先测试无服务凭据、伪造／过期签名、请求改内容、同 jti 换 key、失效员工均拒绝；合法 issuer+jti 重放仅允许同摘要／同主体／同幂等键。重放表持久放财务库，不用进程 dict 代替。
- [x] 使用 PyJWT 与受控公钥目录验签，固定 EdDSA/iss/aud，检查请求摘要与最长有效期。服务凭据恒时比较；源身份 online 检查失败不接收新业务。财务授权从自身 grants 表读取。
- [x] 独立 FastAPI 应用实现11个 F0 operation，对 JSON 请求严格拒绝身份、状态、路径等额外字段；金额字符串不转 float。响应错误有code/message/trace_id/retryable，敏感原件不进入错误正文。
- [x] POST imports 先持久写 accepted 再返回202，后台任务由服务持有，关闭HTTP不取消；GET操作和补件状态均读库。cancel带版本和幂等键；取消保留已完成项。
- [x] 配置文件显式注入财务 DSN、财务对象根或 S3、加密密钥文件、公钥、服务凭据、平台网关地址与授权 API key、公司与角色初始化。禁止从业务模块 os.getenv；生产拒绝 synthetic。模块 enabled/draining/read_only/disabled 按许可阻断新写，不删除历史。
- [x] 启动获得财务数据库独占锁，初始schema迁移幂等，worker持有强引用；停机停止认领并结算／保留在途。读取原件副本不再回平台取原字节；测试平台源文件删除不影响财务副本。
- [x] 运行 `uv run pytest packages/xuanpu-finance/tests/test_auth.py packages/xuanpu-finance/tests/test_api.py packages/xuanpu-finance/tests/test_lifecycle.py -q`；使用ASGI真实请求和实际PG/对象适配器。

## 工作包 4 F0 业务包、独立部署与操作说明

**Files:** Create `examples/finance/{xuanpu.yaml,apis/finance.yaml,skills/finance-assistant/SKILL.md,skills/invoice-intake/SKILL.md}`, `docker/finance/{Dockerfile,compose.yml,config.example.yaml}`, `docs/architecture/finance.md`, `docs/guides/finance-f0.md`；Modify `docs/README.md`。

- [x] 从设计包提取且只开放11个 F0 operation，删除未来阶段schema与工具依赖；补件参数同步契约。Skill使用本人固定文件版本，202继续查操作，partial列逐项结果，not_checked不称查验通过。
- [x] 独立 Compose 只包含财务API、财务PG和财务对象存储，独立network/volume/secret；不改平台现有生产Compose。配置不带默认生产口令、不挂平台数据卷或宿主Docker socket。
- [x] 文件与身份桥API只通过明确网关域名访问；部署使用secret文件，不把测试JWT、数据库连接或密钥提交源码。
- [x] 实际运行 `uv run xuanpu validate examples/finance`；以当前内核加载Skill并实际读取附属文件。指南记录实际跑通的启动、合成材料验收与恢复命令，并明确生产格式／官方查验／支付／ERP／归档未验证。

## 工作包 5 交叉审阅与验收

**Files:** Add finance tests for signature/key replay、真实PG并发／故障、存储复制／加密、HITL重启补件、启停、filebridge权限；Create `docs/guides/releases/2026-10-21-finance-f0-acceptance.md`。

- [x] 先按批准设计独立审阅差异与F0范围，再做代码质量／安全审阅；修正阻断并复审，原始失败结果保留。
- [x] 用独立PG及独立对象存储端到端：受权平台文件→签名API→批量导入→一票多来源→PDF待补→重启→补XML→异常关闭→跨身份拒绝→源文件删除后财务副本可读。
- [x] 新增服务仅使用合成测试资料验收；如官方标准包／真实脱敏票据或真实模型凭据不可用，明确 unverified，不绕过条件且不把受控模型算真实模型。
- [x] 运行完整门禁 `uv run pytest`、`uv run ruff check --select F,S108,I,TC packages`、`uv run mypy packages/*/src`、`uv lock --check`、`git diff --check`，记录实际输出；镜像构建与独立本地运行另记，不据测试声称生产部署。
- [x] 汇总当前提交、业务包版本、服务边界和剩余真实输入；保留独立分支供审阅，不推送／合并／生产部署，除非用户另行授权。

## 当前进度

- [x] 用户批准设计并确认 HITL、独立存储。
- [x] 已创建隔离检出并完成代码／契约基线核对。
- [x] 工作包1：通用签名与身份/固定文件桥，含重复头安全修复。
- [x] 工作包2：独立财务领域/PG/加密对象、worker、HITL与故障恢复。
- [x] 工作包3：11个签名API、独立配置/生命周期、实际进程重启补件。
- [x] 工作包4：可加载F0包、白名单镜像构建、专属Compose和现状文档。
- [x] 工作包5：最终全量门禁、独立复审及验收记录收尾。

实施物理拆分以源码为准：storage/{store,imports,queries,worker,originals,common,schema}.py，invoices/{parser,worker}.py；没有为了匹配草案文件名合并大模块。正式格式/真实模型/云S3与生产运维验收仍为unverified，不属于已通过的本机合成材料验证。


最终验收：整仓 **1970 passed, 3 skipped in 219.25s**；财务专项 **96 passed**；门禁ruff通过、strict mypy **192 source files**通过、uv lock与diff检查通过。独立审阅当前无未关闭P1/P2。最终镜像与专属Compose已实测，临时实例/密钥/测试schema已清理。三项跳过为原有非财务PG用例，官方票据映射、真实模型、云S3、正式业务通道和生产运维仍按unverified/未实现记录，不据本机合成测试宣称生产验收。完整证据见[分层验收](../../guides/releases/2026-10-21-finance-f0-acceptance.md)。
