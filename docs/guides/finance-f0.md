# 财务 F0 开发与接入

当前专用资金能力与最新包契约入口见[F5指南](finance-f5.md)（finance `0.6.0-f5`、69项API、7个Skill）。本页保留本阶段操作说明、历史版本和验证证据；当前最终门禁、制品与运行状态见[F5验收记录](releases/2026-10-22-finance-f5-acceptance.md)。

F0基座用于原件归集、逐项进度、票面/字段/证据查询和持久补件。示例包的报销与内部审批见[F1指南](finance-f1.md)；通用付款/记账见[F2](finance-f2.md)，查验、红字和档案隔离闭环见[F3](finance-f3.md)。生产官方 XML 标准映射、真实模型使用、税务/支付/ERP/归档与生产环境验收分别记录在[验收记录](releases/2026-10-21-finance-f0-acceptance.md)。

F5支持材料经同一固定版本收件和责任索引保全，以supporting_material与material_received区分发票导入；返回source/not_checked，不制造发票或缺票异常，见[F5材料流程](finance-f5.md#支持材料与api示例)。

## 已执行的本地验证

在仓库根目录：

```sh
uv sync --all-packages --all-extras --python 3.12
uv run xuanpu validate examples/finance
uv run pytest packages/xuanpu-finance/tests/test_package.py --noconftest -q
```

校验输出 `finance 0.2.0-f1.1：校验通过`；内核加载finance-assistant及invoice-intake/expense-reimbursement/finance-review，并用真实 read_skill 工具读取 references/execution-rules.md。该项不代表真实模型已正确使用这些工具。

财务数据库测试需要明确设置 FINANCE_TEST_DSN，连接后核对真实数据库名必须以 finance_test 开头；每个用例使用随机 schema，结束后清理。该地址必须指向独立测试 PG，不能指向平台或生产库。没有此变量会显式 skip，不能据 skip 声称数据库验证通过。

```sh
uv run pytest packages/xuanpu-finance/tests -q
```

## 独立配置

配置结构见 [config.example.yaml](../../docker/finance/config.example.yaml)。服务 CLI 支持 `xuanpu-finance --config <文件> --check-config`；该命令只检查结构，不探测凭据、数据库或外部通道。实际启动去掉 --check-config。

数据库 DSN、服务 bearer、平台公钥、平台 act-as API key 和原件32字节二进制加密密钥均引用独立文件。私钥只放平台租户保险库；财务仅有公钥。不要把私钥、运行目录、DSN或原件提交仓库。密钥丢失无法解密原件，须按财务备份策略另行保全。

平台配置增加：

```yaml
call_context:
  issuer: xuanpu
  keyId: platform-ed25519-1
  privateKeySecret: platform-signing-key
  allowedAudiences:
    http://127.0.0.1:8443: [xuanpu-finance]
```

在对应租户保险库设置 platform-signing-key（Ed25519 PEM 私钥）和 finance-service-token（与财务 service_token_file 同值）；独立财务持有获准 act-as 的平台 API key，仍需逐次在线核对真实员工。示例服务器地址只适用本机开发，容器部署须将 examples/finance/apis/finance.yaml 的 servers、平台允许 origin 和实际财务 origin 一起改为部署地址。不能只改其中一处。

## 运行与验收顺序

1. 准备独立 PG、独立原件目录/桶与密钥，设置公司及员工角色；不要挂平台数据卷。
2. 配置签名和文件桥，再装载 examples/finance。保留 standard 工具确认策略。
3. 员工上传文件后传 file_id/revision/sha256。202 后查询 getOperation，确认逐项结果；合成材料仅在 development/test + synthetic=true 使用。
4. PDF 等材料产生异常后，重启服务，再查询 listExceptions，用原 ID/version 和新原件补件。原异常只在成功提交后关闭。
5. 数据库与原件应配套备份、演练恢复；暂停或移除功能入口不删除独立财务数据。当前未实现带备份审批的卸载清理器，不得手动 drop 数据冒充停用。

## 镜像与独立 Compose

[build_image.py](../../docker/finance/build_image.py) 只从白名单复制规范/财务源码和哈希锁定依赖到临时构建上下文；不发送平台配置与数据。调用方式：

```sh
uv run python docker/finance/build_image.py
```

[docker/finance/compose.yml](../../docker/finance/compose.yml) 是专属财务开发/预发布编排，使用财务 PG 和原件两个独立 volume，不改平台现有 Compose。启动前由操作员创建该目录下 runtime/config.yaml 与引用文件；db-password 与 database-dsn 中口令必须一致，DSN 主机为 finance-db。文件须可供镜像 UID10001读取。示例原件使用独立加密卷，S3 可按 config.py 的显式 endpoint/bucket/access_key_file/secret_key_file 切换。Compose 实际运行证据另见验收记录。

当前F4已补原生站内提醒、SQL权限分页、配置/运营投影和local配套恢复，见[F4指南](finance-f4.md)与[剩余缺口台账](finance-gap-register.md)。F4阶段为61项API、7个Skill；当前F5为69项API、7个Skill，专用资金见[F5指南](finance-f5.md)。

当前F6新增正式接入准备检查与可重复隔离验收，总70项API、7个Skill；见[F6指南](finance-f6.md)和[正式接入清单](finance-provider-acceptance.md)。原阶段正式能力未验边界继续保留。
