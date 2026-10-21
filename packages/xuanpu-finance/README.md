# 玄圃财务 F0

独立进程接收电子票原件、加密保留原字节、持久逐项操作和异常。仅提供接收、证据和隔离；付款、税务查验、ERP、归档未配置。原件哈希一致只说明内容一致，不表示官方验真。

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
