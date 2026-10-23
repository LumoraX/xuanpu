# 玄圃财务 F0–F13

当前包契约finance `0.14.0-f13`、77项API与7个Skill；当前优先发票申报和报销内部流转记录，不做支付。用法见[F13指南](../../docs/guides/finance-f13.md)，本轮验证与制品见[F13验收记录](../../docs/guides/releases/2026-10-23-finance-f13-acceptance.md)。

独立进程接收电子票原件、加密保留原字节、持久逐项操作和异常。提供归集、证据、轻量报销记录与真实内部人工审批；F2提供应付及通用付款/记账接口、专用人工批准和隔离闭环；真实支付、税务查验、ERP、归档未配置。原件哈希一致只说明内容一致，不表示官方验真。

生产尚未配置官方解析 profile：XML 保留原件并隔离未知 profile；PDF、OFD 和图片保留原件并形成待补原件异常。仅显式 `synthetic=True` 的测试使用 `urn:xuanpu:finance:synthetic:v1`；测试票仍为 `not_checked` / `unverified`。

所有配置与密钥由构造函数注入。数据库必须使用独立财务 PostgreSQL；公司和 collector/reviewer 授权属于本服务，不跨库外键。平台管理员没有默认业务访问权。

测试使用独立 Docker PostgreSQL，禁止指向生产。完整配置及启动由独立服务入口提供。

## 领域边界

`FinanceStore(pool)` 在本服务独立库维护公司和 collector/reviewer 授权。普通用户读取本人记录，reviewer 可读取已授权公司的记录，并可取消该公司内导入操作、为其他收集人的材料缺失异常补原件。`configure(companies, grants)` 在独占启动时事务替换本实例授权，撤掉的角色立即失去后续访问权，历史公司和业务记录保留。没有默认授权。

导入幂等键绑定租户、用户和完整请求（包括补件异常、版本）；同键异内容返回冲突。补件是材料缺失闭环，不是业务审批。仅在新票、明细、来源链接和领域事件同事务提交后关闭源异常；补件失败保留原异常。主票以租户、公司、销方税号、票号、开票日期唯一；内容冲突形成隔离异常，不覆盖已有票。

原件适配提供 `EncryptedLocalObjects(root, key)` 和 `EncryptedS3Objects(client, bucket, key, prefix="finance")`。`key` 为注入的 32 字节 AES-256 原始密钥；AES-GCM 随机 nonce 并绑定对象键。相同对象键只允许完全相同原字节重放，异内容拒绝。S3 使用条件创建，不经先查后写覆盖；两者都不等于具有法定保管保证的 WORM 归档。密钥生命周期和备份由部署方负责。

单文件最多 20 MiB，整个导入批次最多 100 MiB。未知 XML profile 保留加密原字节并进入 `quarantined`；PDF、OFD 和图片保留原字节并形成 `awaiting_original` 材料缺失异常；哈希不符、安全 XML 拒绝、存储或下载故障分别持久记录错误责任。超限原件不写对象、不创建成功回执。字段金额为十进制字符串，测试 profile 保留每行税率和每个字段原值、路径、来源回执。

worker 使用 PostgreSQL `FOR UPDATE SKIP LOCKED` 认领、60 秒租约与每 20 秒续租；每个写入事务检查租约令牌。原件写入前先事务持久固定对象责任键及摘要、大小、类型和文件名；原件写成功而回执事务失败时，重启优先读取责任键对应副本、校验摘要并补建回执，不再依赖平台源文件。尚未写成的预登记项仅允许重取与已持久责任摘要一致的内容。进程重启也可恢复 `received` 检查点，不重复创建已提交票；硬停后等待旧租约过期。用户取消保留已完成项。数据库事务故障向调用方传播，不能被转换为伪成功；原件存储故障形成显式异常。

## 合成测试 XML

仅测试显式启用 `synthetic=True` 时解析以下 profile；不能将该样本、原件哈希一致或解析成功当作官方标准/验真证据。

```xml
<Invoice xmlns="urn:xuanpu:finance:synthetic:v1">
  <Number>SYN-001</Number><IssueDate>2026-10-21</IssueDate>
  <SellerName>测试销方</SellerName><SellerTaxId>SELLER</SellerTaxId>
  <BuyerName>测试购方</BuyerName><BuyerTaxId>BUYER</BuyerTaxId>
  <Currency>CNY</Currency><Amount>100.00</Amount><TaxAmount>6.00</TaxAmount><Total>106.00</Total>
  <Lines><Line><Name>测试项目</Name><Quantity>1</Quantity><UnitPrice>100.00</UnitPrice>
    <Amount>100.00</Amount><TaxRate>0.06</TaxRate><TaxAmount>6.00</TaxAmount>
  </Line></Lines>
</Invoice>
```

领域测试需显式提供指向独立测试库且库名含 `finance_test` 的 `FINANCE_TEST_DSN`；每项测试创建随机 schema 后清理。未提供测试库时，PG 项明确跳过，不能据此声称 PG 验证完成。集成测试用 moto S3 模拟服务验证客户端条件创建和密文；尚未在实际云 S3/OSS 服务验收。

公司、发票和异常列表采用 `received_at/id` 稳定键集游标；明细和字段事务提交后不可变，采用持久位置续读。游标绑定租户、用户、查询种类、公司和筛选，畸形或跨查询游标返回 400；`total` 始终为当前全部授权匹配记录数。新增记录不会改变已读位置。

XML 映射在 AnyIO 可取消子进程执行，10 秒硬性业务时限超出后终止解析子进程，并将项持久隔离为 `PARSE_TIMEOUT`。这属于解析进程隔离与取消能力，不构成操作系统/网络沙箱保证。DTD、外部实体、深度和元素总量限制仍由 defusedxml 与显式资源门禁共同执行；票号/税号64、行名1000、字段名256、来源路径1000和字段原值2000字符均按发布契约拒绝越界，不截断存库。

F1配置、API与人工页面见[使用指南](../../docs/guides/finance-f1.md)。expense_policies为空保留F0，内部记录须显式启用并配置实际审批人；业务数据完全保存在独立财务存储。未来企业微信/飞书接入尚未实现。

F2执行与回执、独立模拟环境及渠道协议见[使用指南](../../docs/guides/finance-f2.md)。外部动作必须专用批准后由原经办人工确认；internal_record不取得外部执行资格。

F3查验、红字、独立目标专用HITL和档案HTTP隔离闭环见[使用指南](../../docs/guides/finance-f3.md)。真实供应商和正式profile尚未验收，正式能力关闭；simulation逐版本锁不证明真实WORM。

F4原生提醒、SQL权限分页、运营概览与local配套备份恢复见[使用指南](../../docs/guides/finance-f4.md)，完整范围见[缺口台账](../../docs/guides/finance-gap-register.md)。维护CLI不覆盖旧库，不启动业务worker；正式渠道和真实WORM未验收。


F5新增严格`funding_case`：无票预付、员工已付款后的本人偿还、供应商授权第三方收款，以及不再付款的预付核销/内部退款核对。`funding_policies`缺省为空，启用须五场景显式审批链及invoice_backed基础制度；真实供应商/正式制度未选，仍仅显式simulation，正式默认关闭。支持材料用F0固定版本责任链独立加密保存，purpose为supporting_material，终态material_received、source为not_checked，不伪造invoice或银行成功。核销/退回共同受原确定已付限额，unknown不算已付；最终批准的收尾事实不可普通回退。模型仅准备与查询，人工执行、授权撤销仍走真实员工网关。详见[F6指南](../../docs/guides/finance-f6.md)。

F6新增只读接入准备检查和仓库固定隔离验收入口；配置通过不构成正式验收或执行许可。正式接入逐项证据见[供应商清单](../../docs/guides/finance-provider-acceptance.md)。

`profile=internal_records`提供显式内部记录部署：必填公司/制度/实际岗位，启动迁移前只读核对已有结构与全租户业务兼容性，不装配外部客户端/worker/回调。专用离线预检成功仍not_verified，模板待填写即blocked。`getFinanceCapabilities`读取实例模式；`listExpenseClaimSummaries`只投影九字段，默认本人20条、最大50，按单号/事由字面检索，详情另读原文。独立存储和真人决定不变。

F10增加内部试用只读检查与恢复后人工续办验收，当前77项API/7 Skills；使用与未验边界见[F10指南](../../docs/guides/finance-f10.md)。

F11增加production/internal正式启动门禁及停机只读原件verify，不新增API/模型执行权限；当前正式公司、人员、环境和真实模型仍未验。

F12补齐内部材料中途撤权的事务检查和单项隔离，真实强制退出恢复与并发真人确认另有隔离证据；不新增API、模型执行或支付权限，正式环境与真实模型仍未验。

F13补齐独立原件目录的符号链接与特殊文件拒绝、并发不变性和故障恢复后真人续办；不增加API、模型执行或支付权限，正式环境与真实模型仍未验。

本地适配支持macOS/Linux的POSIX目录描述符与O_NOFOLLOW，缺少必需能力时失败关闭。配置根目录祖先须受信，根/分片由服务账号和受信管理员管理；已打开目录改名后可能在旧fd成功，但不跟随新链接，也不证明配置路径仍可达。遇OBJECT_PATH_UNSAFE等原件异常，由受信管理员核对独立目录并恢复合法文件，再查原operation、按当前资格补充或续办。模型不能读取原件正文、修改目录、维护或代审批。恶意本机管理员、硬链接、挂载更换、掉电、写中临时文件自动修复、Windows与生产灾备不在本轮承诺内。
