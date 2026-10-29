# 玄圃财务 F0 发票基座详细设计

状态：用户于 2026-10-21 批准按本设计推进；F0 已开始实施。当前代码与验收边界以[现状架构](../../architecture/finance.md)及[验收记录](../../guides/releases/2026-10-21-finance-f0-acceptance.md)为准。
本子项目交付可通过对话和 API 使用的发票原件基座，不执行付款、ERP 记账、用途确认或正式归档。

## 1 验收目标

员工选择本人文件版本交办后，系统保存原始字节和来源、受控解析、按公司隔离归集、判重并输出可核查字段。
员工离开页面后已受理的财务导入继续，重新进入可查每个文件的结果和异常。
任何解析成功只能标记 parsed／not_checked，不能声称官方查验通过或符合财政部标准。
F0 API 暴露 importInvoices、getOperation、cancelOperation、listInvoices、getInvoice、getInvoiceLines、getInvoiceFields、
getInvoiceEvidence、listExceptions、listLegalEntities、listIntegrations 十一个 operation；其余契约操作留在设计包，发布 F0 包时移除未实现白名单及子 Skill。

## 2 运行与文件边界

新增包 `packages/xuanpu-finance`；命名空间 `xuanpu_finance`；独立进程监听内部端口，不能从平台进程导入业务服务。
F0 即使用独立 PostgreSQL 实例／数据库／账号及独立对象存储资源／桶／凭据，不能先把财务表塞进平台库再延后拆分。
数据库与原件各自具备备份、恢复和迁移配置，平台侧只通过 API 接入。已成功收件的原件必须在财务存储有受校验副本，
不依赖平台文件的后续保留；财务服务不持有平台数据库或对象存储凭据。
用构造注入配置。启动命令拟为 `python -m xuanpu_finance`，仅在完成实现及真实运行后写入使用手册。
F0 使用一个财务 worker 持有者，数据库锁拒绝第二个实例启动；不承诺水平多实例和无人值守进程接管。
接收任务先写数据库，worker 再认领；无外部副作用的导入可按同项恢复，不重复建票。重启对照对象和每项回执修复遗留状态。

文件桥是玄圃进程内通用接口，不含发票规则。它用可信服务身份＋授权上下文读取固定员工文件版本，
校验文件未撤回、权限有效、revision 与 SHA-256 一致，再以内部流传输至财务服务。
文件桥不接受任意 URL，不签发包含裸对象 key 的模型参数，不把租户服务密钥传到文件处理环境。
XML 解析在隔离 worker 执行，禁用网络、DTD 与外部实体；一次只处理一个受大小约束的文件。

## 3 模块与拟新增文件

以下路径为实现工作包的目标，均不是本轮已存在的能力。

| 路径 | 单一职责 |
| --- | --- |
| packages/xuanpu-finance/pyproject.toml | 独立版本与运行依赖；加入 uv workspace source 映射 |
| packages/xuanpu-finance/src/xuanpu_finance/config.py | 注入数据库、对象存储、信任密钥及限额 |
| packages/xuanpu-finance/src/xuanpu_finance/auth.py | 验证服务身份与平台签名上下文，构造不可由请求 body 覆盖的 principal |
| packages/xuanpu-finance/src/xuanpu_finance/models.py | 收件、票据、明细、标准映射和错误类型 |
| packages/xuanpu-finance/src/xuanpu_finance/invoices/identity.py | 按票种版本化身份算法 |
| packages/xuanpu-finance/src/xuanpu_finance/invoices/ingest.py | 逐项收件、解析、判重和异常 |
| packages/xuanpu-finance/src/xuanpu_finance/invoices/parsers.py | 成熟 XML 解析器边界和受控格式映射注册 |
| packages/xuanpu-finance/src/xuanpu_finance/invoices/provenance.py | 字节哈希、字段路径、映射版本和证据关联 |
| packages/xuanpu-finance/src/xuanpu_finance/storage/postgres.py | 有界事务、公司隔离和原子幂等；达到文件警戒长度按聚合拆分 |
| packages/xuanpu-finance/src/xuanpu_finance/storage/migrations/0001_finance.sql | F0 表、索引、约束、事件及导入工作队列 |
| packages/xuanpu-finance/src/xuanpu_finance/operations.py | 导入任务、逐项结果、重启核对与取消边界 |
| packages/xuanpu-finance/src/xuanpu_finance/api.py | F0 严格请求／响应、身份与错误映射 |
| packages/xuanpu-finance/tests/ | 模型、真实 PG、对象存储、身份、API、worker 故障测试 |
| packages/xuanpu-server/src/xuanpu_server/runtime/call_context.py | 通用签名上下文，固定 trusted session／principal；不 import 财务 |
| packages/xuanpu-server/src/xuanpu_server/api/file_bridge.py | 通用受权文件版本读取，不解释发票字段 |
| examples/finance/ | 验收后才新增的 F0 可执行包；只注册已实现操作 |

通用上下文接线涉及现有 `runtime/http_api.py`、`runtime/assembler.py` 与会话主体传递；
不能把可信上下文暴露为普通 OpenAPI 参数。文件桥接线复用 workspace 的下载与版本检查，不绕过原内容权限。
平台规范若需新增签名能力声明，按兼容性规则扩展，不把 legal_entity、发票或 ERP 概念放入规范包。

## 4 数据事务与文件提交

1. 接口验证身份、目标公司与文件权限；短事务持久导入 Operation、幂等指纹和每项 item_id／revision／sha256；返回 202。
2. worker 用服务身份重新核实原授权，读取固定文件并流式核对大小和摘要。不可读进入该项 failed，保留错误而不跳过。
3. 对象存储在隔离前缀写入不可覆盖的 receipt_id，核对回读摘要；数据库写 SourceReceipt。无数据库引用的对象由待核对清单保留，不能直接删除可能仍在提交的对象。
4. 成熟 XML 解析器提取命名空间、根结构、明细和字段路径；选取受控 parser_profile。未知票种／schema 进入 quarantined，保存原始字节，禁止猜测字段。
5. 同公司业务身份唯一约束决定创建或链接既有 Invoice；字节不同但关键票面相同仍附来源；关键票面冲突进入 exception。
6. 同一事务提交 Invoice／Line／Field／Source、首事件和该项结果；不允许已有 Invoice 却缺首事件。
7. 每项独立处理，批次保存 succeeded／partial／failed 业务结果。Operation.status= succeeded 表示批次处理结束，必须同时返回 summary 和每项结果，
   不能把存在失败的 partial 批次说成全部导入成功。所有项失败时 Operation.status=failed，business_result=failed。

PDF／OFD／图片只收件、检测和提示待补原件；F0 不用 OCR 填补 XML 字段。后续票种适配若正式接受某类版式原件，需要独立格式、验签与标准验收。
合成测试 XML 必须有 synthetic parser_profile，测试数据库明确隔离；生产配置拒绝启用 synthetic profile。
取得官方标准包和真实脱敏样本后才注册 production profile，保存 namespace、schema digest、元素目录版本及样本验证报告。

## 5 状态与失败语义

来源收件与主票状态分离。解析完成后 verification=not_checked，ticket=normal 仅指原件描述的初始本地记录，
额外 `authority_status=unverified`；不能用初始 normal 表示税务确认有效。权威票据状态取得前一切税务写入关闭。
每项状态包含 received、awaiting_original、quarantined、parsed、duplicate_linked、failed；
parsed 和 duplicate_linked 才计入结构化成功数。缺 XML 入 awaiting_original／异常，来源保留。
对象存储失败不产生成功收件；数据库提交失败保留对象核对索引；worker 崩溃通过 item checkpoint 核对是否已完成，不重复解析已提交项。
Operation 未发送任何外部副作用时可取消未处理项；已保存票据不删除，最终明细说明部分完成和取消数。

## 6 API 的 F0 细则

请求／响应字段以 [OpenAPI](finance-v1/pack/apis/finance.yaml) 为准。
`file_id` 是玄圃 item_id 对应的公开 ID，revision 固定；请求不含 tenant_id、user_id、seller_tax_id、object_key 或状态覆盖字段。
目标公司必须显式指定，不能按发票买方名称自动选择公司。查票默认按本人数据范围；财务岗位可见公司内经授权单据，系统管理员无默认正文权。
`GET /invoices` 的 total 是权限与过滤后的独立总数，items 采用稳定 `(received_at,id)` 游标，limit 最大 100。
证据输出引用而非下载凭据，原件下载再经过文件桥授权；银行账号只展示掩码。
单次导入最多 100 个引用，单文件 20 MiB、合计 100 MiB。若经现有 R1 工作任务提交，以 R1 更小限额为准。

## 7 F0 逐项验收

| 用例 | 断言 |
| --- | --- |
| 正常 XML | 原字节可回读且摘要一致；金额和明细对应来源路径；version=1 与首事件同事务 |
| 相同字节／同身份重复 | 两个并发请求只产生一张票，均可追来源；更换幂等键也不重复建主记录 |
| 同键不同请求 | 409，不新增票／来源／操作；同键同内容返回原操作 |
| 多公司／租户／员工 | 任意读写、证据、操作与文件引用均受真实权限范围限制 |
| 伪造员工上下文 | 无签名、过期、错 audience、方法／请求体改变、模型伪造头均被拒绝 |
| XXE／DTD／过深／超大 | 没有网络访问或外部文件读取；受限失败，原件隔离，无静默截断 |
| 未知 schema | quarantined，不能由模型猜测字段进入 parsed |
| 图片／只有 PDF | 保留收件，awaiting_original；结构化成功数不增加 |
| 一批 3 项中 1 项失败 | summary 明确 partial；成功项可查，失败项有 code 和异常；不阻塞后续项 |
| 断开网页／进程重启 | 前者继续；后者核对已提交 item checkpoint，未提交项可恢复，来源与首事件不重复 |
| 对象写成功后 DB 失败 | 对象有核对索引；重试不漏原件，不制造孤儿成功态 |
| 模型不可用 | 原生 API 查询、导入及异常查看仍可用；财务统计不依赖模型计算 |

## 8 实施顺序与交付证据

F0 分为五个顺序工作包：①可信身份与文件桥；②独立服务与真实 PG 事务；③受控解析及来源保全；④API 和只开放 F0 的业务包；
⑤故障矩阵、真实模型及本地部署验收。每包先写有意义的失败回归，再实现并跑目标测试，最后跑仓库完整门禁。
书面设计批准后为每包补齐可执行实现计划，固定实际文件和测试代码；本文件不把拟议命令记作已运行。
F0 不依赖选择具体 ERP／银行，可以先开发；生产解析的外部输入是官方格式包、元素清单和真实脱敏票据样本。
F0 的缺原件、解析失败等人工补件进入持久异常待办；后续业务 HITL 按总设计执行，不将聊天挂起状态当作财务工作流正本。
这些输入未取得时可完成 synthetic adapter 测试，但 production parser 及真实样本／标准符合项必须标记 unverified。
