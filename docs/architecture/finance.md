# 财务 F0–F7 当前架构

财务作为进程外 HTTP 服务运行。玄圃负责企业员工身份、会话和工具入口；独立财务服务持有公司授权、导入操作、发票、异常、原件回执及事件。平台进程不加载财务业务代码。当前覆盖F0原件、F1报销/人工审批、F2应付/付款/记账、F3查验/红字/档案、F4提醒/恢复、F5专用资金及F6只读接入准备。当前F7按用户要求聚焦发票申报与内部报销流转记录，契约finance 0.8.0-f7、72项API、7个Skill；实际制品与门禁以[F7验收记录](../guides/releases/2026-10-22-finance-f7-acceptance.md)为准。真实供应商、正式制度、官方标准和正式归档尚未验收。

```mermaid
flowchart LR
  U[员工发票与说明] --> S[财务 Skill]
  S --> G[玄圃确认与签名出口]
  G --> A[独立财务 API]
  A --> D[(财务 PostgreSQL)]
  A --> W[持久 worker]
  W --> O[(独立加密原件存储)]
  W --> B[平台身份与文件桥]
  D --> H[持久异常待办]
  H --> U
  A --> HA[单据与原计划专用人工审批]
  HA --> A
  A --> SW[独立付款与记账 worker]
  SW --> P[可替换 HTTP 付款与记账系统]
  P --> CB[独立验签 Inbox]
  CB --> SW
  SW --> D
```

## 数据归属与独立性

财务包、数据库实例/数据库/账号、原件目录或桶、加密密钥、备份及迁移独立于平台。没有跨库外键，没有读取平台数据库或对象存储的凭据。平台只通过受限 HTTP 文件桥提供受权员工的固定文件版本，财务接收后拥有自己的原字节副本。删除平台附件不删除财务副本。企业身份停用仍由平台在线目录核对，平台无法访问时拒绝新业务；独立存储不意味着身份授权离线放行。

财务公司及 collector/reviewer/approver 权限由操作员配置，在财务库持久保存。重启时配置授权集合为准撤销旧授权，不删除历史材料。collector 仅本人公司内记录；reviewer 可见授权公司全部记录，也可补件和取消。平台管理员没有默认财务正文访问权。approver只看本人和实际分派单据/关联证据；内部approval已可用，外部审批及真实ERP/payment/tax/archive仍 unconfigured，未来适配器不能反向拥有核心业务状态。

## 可信执行与受控输入

连接器显式声明 contextAudience，平台操作员按目标 origin 允许 audience。平台出口从持久会话恢复实际员工身份，使用租户保险库中的 Ed25519 私钥签发最长60秒 JWT；签名绑定方法、解码后的路径、query、JSON、幂等键和 Content-Type。财务固定 issuer/audience/kid 公钥目录验签，持久保存 issuer+jti 的主体、摘要与幂等键，复核在线员工和财务公司授权。

文件桥仅接受 file_id、revision、sha256，复用员工文件权限。机器 API key 必须获准 act-as 具体员工；root/machine 原始身份不能替代员工。财务请求体在 FastAPI 解析前限制1MiB，单原件20MiB、单批100MiB/100项。跨租户与无权对象读取失败关闭。

## 持久进度与 HITL

导入先事务持久 accepted 再返回202，逐项状态独立保存。worker 使用数据库租约、心跳和 fencing；进程额外持有独占 advisory lock。持锁连接失效立即禁用旧实例、停止认领并返回不健康；数据库错误不会冒充业务成功。

PDF/OFD/图片保留原件并生成 awaiting_original；未知 XML profile 隔离为 quarantined。解析在可取消的独立子进程限时执行；defusedxml 拒绝外部实体/DTD，不提供操作系统级网络沙箱。本阶段只实现明确命名的合成 XML profile，production 配置禁止 synthetic。字段保留原值、路径和来源回执；主票唯一约束处理重复来源。所有查验字段保持 not_checked/unverified，未配置官方查验。

HITL 在 F0 体现为财务库内的持久异常，补件请求必须同时携带 source_exception_id、expected_exception_version 及新文件固定引用。成功解析后同事务链接旧/新证据并关闭原异常，失败保留原待办。服务/会话重建不会丢失待办。材料补齐和平台工具确认均不等于财务业务审批；F1在同一独立库持有真实顺序审批、人工决定与撤回/驳回，不把工具批准转换为业务决定。

## 运行模式与恢复

启动时选择 enabled、draining、read_only 或 disabled；配置改动需重启。draining 拒绝新导入而允许处理已受理项及取消；read_only 保留读接口、暂停导入worker；disabled 拒绝员工业务接口、保留数据。F2外部查单和验签回调在后三种模式仍运行，均禁止新发送。取消仅取消未完成项，已提交票据/原件不删除。

本地和 S3 适配器均以 AES-GCM 保存密文、固定 key 不允许换内容覆盖；这不是经过认证的 WORM。独立备份必须同时保全数据库、原件和加密密钥。对象/数据库间故障的恢复证据见验收记录，不能以 HTTP200 或测试模拟声明生产已验收。

## F1 轻量记录与人工入口

费用记录持久保存record_mode及每次版本快照；internal_record允许没有结构化发票，仍保留附件缺口与not_checked语义。配置未提供时禁用新报销；显式最小分类和真实审批人即可启用，制度版本不可覆盖。invoice_backed或任何有分票的记录必须完整分配，提交锁单据和排序票据，原子复核票/行金额税额累计。批准后额度仍reserved，未进入支付/核销。

平台通用人工HTTP网关只接受当前员工cookie+CSRF，拒绝API key、root和act-as，校验租户已安装包及显式工具列表。HumanContextSigner在最终请求中签origin=human与interactive关联；模型路径固定agent。财务executeFinanceAction要求人工来源、当前身份/公司授权与计划/目标/步骤/策略版本同时有效。模型故障时功能辅助页仍可操作。

预检不产生决定。单据/Workflow/Step/Decision/幂等Operation/额度/事件/Outbox同事务。中间审批只递增流程和步骤版本，最终批准/驳回/撤回改变单据版本；待办由财务库恢复。超时与策略变更阻断批准，允许有权申请人新计划撤回清理。策略实际启停或更改的generation进入计划和工作流，恢复旧版本不复活旧授权。通知Outbox由F4消费到独立财务站内inbox，不代表外部渠道已送达。

企业微信/飞书是未来可替换适配器，内部ID、原件和审批历史保持财务所有；单一流程权威和外部映射/回调验签须在接入时显式实现。当前只支持internal provider。[F1使用指南](../guides/finance-f1.md)描述启用步骤。

## F2 专用批准及外部状态

应付复用有document_kind区分的版本单据和顺序业务审批，旧报销API拒绝访问应付。供应商/合同/订单/验收和收款账户来自受控目录，版本和摘要冻结入计划。internal_record始终不能进入付款或记账。正式发票要求有效查验；隔离模拟显式使用finance_test库、独立原件前缀及持久simulation标记，既有F1单据不自动升级。

execute_payment、submit_posting、reverse_posting生成不可变计划及独立action_plan审批流程。实际分派审批人批准同一digest，原经办再人工确认；付款/记账岗位分离，申请人和经办不能自审。专用授权绑定金额、目标、策略/岗位/目录/渠道代数及有效期，停用再恢复旧配置不复活授权。

独立settlement worker在发送前核对所有实际批准人的在线状态与当前授权，在财务事务保存dispatching及固定请求；网络不持有数据库事务。固定HTTP渠道支持幂等和查单，缺任一能力即关闭。受理/在途/未知分别持久化；未知保留占额，自动退避查单及人工reconcile均只GET原请求号。恢复、停发及锁丢失不能再次POST。

付款与凭证各自计额，部分成功不核销整张分配；冲回引用原凭证、独立批准并保留历史。成功证据必须匹配所有固定范围，原字节及哈希与状态同事务保存；错误回执保全后为未知。回调使用独立HMAC、固定配置密钥和持久inbox，重复事件幂等、异内容冲突，合法回调只唤起查单。外部异常独立于F0导入异常表，统一查询但不能人工宣称付款成功。Outbox经F4进入独立站内提醒，外部通知适配器尚未接入。

F2阶段提供38项工具及6个Skill，模型可准备/查询，人工网关执行；独立辅助页支持应付、脱敏账户、专用待办、付款/入账/冲回和结果核对。协议及配置见[F2指南](../guides/finance-f2.md)。

## F3 税务与归档独立闭环

F3使用自己的typed target计划/流程/实际决定/grant/operation/receipt/inbox与查验、红字、调整、档案、审计及补包表，不依赖伪报销父单据。FinanceStore统一API按f3plan_/f3op_派发。票面content版本不变，ComplianceState独立版本；F1/F2/F3共用实时金额与正交状态投影、同一排序蓝票锁和授权代数。

ComplianceQueueAdapter复用SettlementWorker心跳/发送门禁/只读恢复，第三后台随实例锁丢失一同取消。preflight取证网络暂不可用保留未发送任务；任何dispatching之后未知只查原请求。税务当前源以provider/config/generation隔离权威revision，旧来源仅留证，当前策略立即收紧TTL，原passed观察时刻不被pending刷新。

ArchivePipeline先物化独立加密固定ZIP，随后事务重检再首次上传。通用档案HTTP要求全部内容/manifest/seal的实际字节、具体版本和保留锁逐项读回；seal包含真实sealed_at，保留期限按受控起算规则核对。成功恢复和只读审计共用全量验证，不信succeeded/readback_verified字符串。正式profile当前拒绝，仅显式simulation协议验证。

档案源闭包包括关联蓝红票实际原件/字段/明细/查验、费用与支付凭证、实际F3税务人工决定、调整义务及人工补件。失败原始回执按raw evidence字节保留。归档/审计自身未来回执不进入本包源。历史封存事实、清册、检测与决定不可变；实际事件sequence及关联单据变化产生补包缺口，补包绑定父hash及来源事件，审计失败只追加健康记录。

F3阶段发布58操作/7Skill。模型可准备和查询，执行、红票关联和调整人工补件只走真实human；直接财务辅助页仍独立于模型。完整配置、固定HTTP协议、数据所有权与真实能力边界见[F3指南](../guides/finance-f3.md)。F4已将相关统一查询收敛到SQL授权过滤、独立count与同快照稳定分页，生产规模查询计划仍须实测。

## F4内部收敛与恢复

F4阶段包61操作/7Skill。独立财务库保存原生通知和读状态，旧Outbox消费到inbox_recorded，当前审批/期限/未知/档案健康按真实源版本去重；当前授权及来源过滤列表与计数，已读不批准、消息不执行。通知任务不依赖模型，实例锁丢失取消四任务。未接外部通知，财务提醒不与平台通用通知正本混存。

F3及统一F2/F3查询在SQL做授权count与limit+1分页；同一repeatable_read快照。listIntegrations反映真实持久配置阶段，reviewer概览只公开公司聚合，最早活动时间排除终态，通知统计排除已失效来源。

维护CLI用成熟pg_dump/pg_restore与同一独占锁，配套local密文/清册HMAC/责任索引AAD认证；维护丢锁禁止发布，恢复只新空库和新对象根，不启动worker或覆写当前配置。S3版本备份、外部档案灾备及生产异机指标未验收。[F4指南](../guides/finance-f4.md)与[缺口台账](../guides/finance-gap-register.md)界定该阶段范围，F5专用资金见下节。

## F5专用资金与原件责任

`document_kind=funding_case`在独立财务库复用版本单据、顺序审批、行动计划与F2队列。五种严格场景为无票预付、员工已付款后的本人偿还、供应商授权第三方收款、已付预付核销、真实材料退款核对；后两者不能再次付款。委托保持原发票销方，不处理客户代收款。核销实际消耗发票分配；退款为内部人工事实，不发起银行退款或宣称到账。父子累计预留/完成不超过确定已付，unknown不算已付；最后批准的收尾事实不可普通撤回。

`importSupportingMaterials`复用F0收件、固定文件版本、责任索引及AES-GCM独立副本，以`purpose=supporting_material`持久化；逐项`material_received`与source证据`not_checked`不等于材料验真，也不制造发票或缺票异常。同一原凭据hash与稳定自然scope共享额度，供应商自然键仅kind/source/id，完整版本摘要进入不可变正文，预付合同保留原版本绑定。

`FundingPolicy`必须显式配置五种场景非空链，依赖启用的invoice_backed基础制度与当前实际岗位。独立版本/digest/generation及角色epoch进入计划、业务批准与发送前复核；撤权再恢复不复活批准、单据/计划/支付/票据分派读权。员工仅绑定本人受控employee_account，委托固定原指定supplier_account。getFundingOptions展示五链和当前可见已核实脱敏账户，listPayeeAccounts按场景筛选；渠道token不返回。getFundingMaterial仅向当前有权owner/reviewer/epoch有效分派人按块读取本单据关联独立原件；服务整体hash核对，人工页合并分块后再次校验再下载，模型不持有base64读取工具。日期授权按Asia/Shanghai自然日含末日判断。

人工动作沿真实cookie/CSRF/origin签名网关；模型可起草、分配、预检和查询，不可executeFinanceAction或revokeFundingAuthority。外部通道须显式supported_funding_scenarios，缺省空不扩权；空能力描述符保持旧hash。发送前再次重审资金制度/原件/账户/授权/实际批准人，沿F2首次一次POST和unknown原号GET，重启或撤权不重发。

F3归档闭包扩展funding_case父子、实际材料、authority/decisions/reservation与付款/记账原回执；新源事实追加事件并要求补包。成熟pypdf/Pillow在可终止进程（10秒期限）进行PDF/PNG/JPEG有限结构检查，限制50MiB/2000页/2000万像素；不证明官方签章或法定四性。F4提醒保留资金目标，funding_due仅确定已付、到票逾期未结的预付，完整收尾后退出。SQL授权count/分页和独立dump/新库恢复覆盖新事实，恢复不启动worker。

专用场景的配置、严格字段、API示例及停发续办见[F5指南](../guides/finance-f5.md)。正式制度/供应商未选，仍仅simulation与可替换HTTP；真实税务票样/WORM/外部报销/异机DR/真实模型与生产并发未验。

## F6接入准备与验收事实

`--preflight`在启动前只读显式固定凭据，检查秘密格式/权限/分离、公司制度与渠道前提，不连接外部资源或迁移。`getFinanceReadiness`仅当前同公司reviewer可读，从独立PG的read-only repeatable_read事务读取实际当前配置、授权代数及账户/渠道启停；配置摘要绑定作用域与版本。运行态独立附带模式、锁和后台任务故障。报告不持久化为批准，不新增业务表或平台库外键；逐单预检/HITL/原经办执行不受检查结果替代。

仓库固定验收入口验证loopback实际finance_test库，使用真实pytest/JUnit生成模块覆盖/失败/跳过/源码和证据摘要。临时两库/独立非超级角色双向CONNECT拒绝是隔离机制演练，不代表正式凭据或原件IAM已验。正式制度批准、供应商、官方票样/签章、WORM、异机DR、模型与生产负载始终not_verified，关闭条件见[正式接入清单](../guides/finance-provider-acceptance.md)。

## F7 内部记录使用闭环

ExpenseDraft可选invoice_information仅在internal_record原样保存，作为未查验的用户申报，不能写正式发票或取得执行资格。listExpenseClaimHistory按当前普通报销读权分页读取finance_expense_history与finance_decision不可变原文；不把最新workflow反推旧意见。getExpenseMaterial仅接受human，校验当前岗位代数及指定版本实际证据关联，再从独立加密存储验证完整字节摘要返回固定分块；原件下载不是验真，awaiting_original已保全材料仍保持原状态。

F7复用既有本人确认、顺序审批、驳回补充重提、撤回与持久待办，审批通过即内部流程完成；不引入支付或独立结案状态。API及人工页共用规则，Skill仅准备草稿/计划及查询历史，原始附件读取与业务决定保留人工入口。F7不需要供应商配置。
