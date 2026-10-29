# 财务 F0 分层验收记录

日期：2026-10-21。起点 `354d51b`；隔离分支 `feat/finance-f0`，检出 `xuanpu-finance-f0`。本记录只覆盖开发验收，未改平台原工作区或生产环境。

## 当前交付

独立 `xuanpu-finance 2026.10.21.1` 服务、`xuanpu-spec 2026.10.21.1` 通用签名规范、`xuanpu-server 2026.10.21.4` 签名出口/身份文件桥、`examples/finance 0.1.0-f0.1` 两层 Skill 与11个 F0 API。
F0 保全原件、导入判重、追溯字段、持久进度与补件；报销业务审批、支付、税务查验、ERP记账和正式归档不在当前实现内。

## 验证层级

| 层级 | 证据及边界 |
| --- | --- |
| 源码/工作区 | 独立分支，平台业务进程不导入财务包；迁移与授权在财务库 |
| API契约 | 11个实际 HTTP 响应通过发布 OpenAPI 的严格 JSON Schema；202先持久受理 |
| 数据库 | 独立 postgres:16-alpine 测试实例，随机schema；并发唯一、跨租户/公司/员工、版本、幂等、持久防重放、keyset分页、租约恢复实测 |
| 平台联调 | 实际员工上传→Session/Gate/签名HTTP→财务ASGI验签→真实身份/固定版本文件桥→worker/独立PG/加密原件；模型边界受控替代，不算真实模型验收 |
| HITL恢复 | 真实财务 CLI 进程停止并重启，原异常仍可查询和补件；旧/新证据关联，失败不关闭原异常 |
| 故障恢复 | original.received与invoice.parsed事务故障注入；对象已成功而回执失败后删除平台源仍能核对恢复；取消保留完成项 |
| 本地对象 | AES-GCM密文、摘要、不可覆盖、源文件删除后独立副本保留实测；不等于WORM认证 |
| S3 | boto3 + moto条件写入与密文/恢复适配测试；真实云桶未验收 |
| Skill | 当前稳定内核加载两层Skill并真实read_skill读取附属文件；未测试真实模型对全部场景的语义完成率 |
| 镜像/Compose | 白名单上下文构建成功；独立财务PG/API/原件卷实际启动，health返回ok/read_only；仅本机烟测，不算部署生产 |
| 真实模型/并发模型 | unverified；本轮不使用真实模型完成报销对话，也不把受控模型算真实模型 |
| 官方格式/标准 | unverified；没有正式标准包与真实脱敏票据。production禁synthetic，未知XML隔离，PDF等持久待补原件 |
| 正式通道/生产/UI | 未部署；税务/支付/ERP/归档unconfigured。没有新增财务管理UI；现有文件入口和API可接入 |
| 运维备份/恢复 | 财务独立卷、密钥文件与迁移配置已提供；生产备份、异机恢复和密钥托管演练尚未验收 |

## 审阅及修正

1. 平台 HTTP 头大小写重复可能导致签名幂等键与真实发送值不一致：加载器与构造器均拒绝重复/凭据冲突，改为签名并发送同一最终Request。
2. 启动时进程持有锁与配置事务锁同号自阻塞：独立锁号，真实启动/第二实例拒绝回归。
3. 持锁数据库连接丢失后旧进程仍健康：使用专用连接终止监听，立即停worker、disabled、健康503。
4. 请求体在解析后才限长：改为框架前纯ASGI有界读取，分块无认证/有服务凭据均超限413。
5. 对象写成功但数据库回执失败后依赖平台重下：增加写前prepared责任索引、恢复核对副本，故障后源删除测试。
6. 未知XML与PDF等待原件状态混淆、全失败status不匹配、offset分页插入重项、字段超长违反契约、日期别名绕过判重：分别增加失败测试并修正。
7. XML解析改为成熟anyio可取消子进程限时执行；真实30秒任务在10秒deadline终止，PID已不存在，持久PARSE_TIMEOUT；没有声称操作系统网络沙箱。Compose限制内存和进程数。
8. 查询签名按key稳定排序，保留同名值顺序，避免摘要相同而实际标量选择不同；测试数据库门禁连接后核对真实库名，拒绝DSN密码中含test字样的诱导。
9. 全量门禁发现通用路由/签名配置/清单新字段总手册漏写，已更新 http-api.md、operations.md、package-spec.md 和默认示例配置；文档专项131项通过。

## 执行记录

- 起点全量：`uv run pytest -q` → **1843 passed, 3 skipped**（188.33s），只作基线。
- 业务包：`uv run xuanpu validate examples/finance` → `finance 0.1.0-f0.1：校验通过`。
- CLI配置：`uv run xuanpu-finance --config docker/finance/config.example.yaml --check-config` → 结构校验通过（不连接外部资源）。
- 镜像：`uv run python docker/finance/build_image.py` → 构建成功。最终镜像ID和最终门禁结果在本记录末尾补录。
- 独立Compose：`docker compose -p finance-f0-smoke -f docker/finance/compose.yml up -d` → 专属PG/API启动，`GET http://127.0.0.1:8443/health` → `{"status":"ok","mode":"read_only"}`。烟测后清理临时容器、卷和凭据。

完整测试使用明确独立 FINANCE_TEST_DSN（不将可复用地址/凭据作为生产配置提交）。未提供时的skip不能作为数据库通过证据。

最终镜像：`sha256:31e797c3662d379db41ae69c5f5007e0c04d0503c41730995fc9af41132eec29`（arm64）。最终Compose重新创建API后实测同一镜像ID、UID/GID `10001:10001`、内存 `536870912` 字节、pids_limit `64`，健康结果仍ok/read_only。独立烟测容器、卷、网络与临时密钥已清理，镜像保留。没有部署或修改生产。

修复后的财务专项：`FINANCE_TEST_DSN=<独立测试库> uv run pytest packages/xuanpu-finance/tests -q -x --tb=short` → **96 passed in 18.25s**。
第一轮整仓：**3 failed, 1967 passed, 3 skipped in 211.37s**，失败仅为上述三个总手册覆盖项；修正文档后专项 **131 passed in 0.39s**，并重新运行整仓门禁。


## 最终门禁

| 实际命令 | 输出 |
| --- | --- |
| `FINANCE_TEST_DSN=<独立测试库> uv run pytest -q -ra` | **1970 passed, 3 skipped in 219.25s** |
| `uv run ruff check --select F,S108,I,TC packages` | **All checks passed!** |
| `uv run mypy packages/*/src` | **Success: no issues found in 192 source files** |
| `uv lock --check` | **Resolved 106 packages**，退出0 |
| `git diff --check` | 退出0 |
| `uv run ruff check --select C901 packages/xuanpu-finance/src` | **All checks passed!** |

新增财务源码/测试无超过800行文件或超过80行函数。跳过的三项分别为既有reminders真实PG行锁用例、workspace直接数据库约束及数据库身份行锁用例；不是本次财务测试跳过。最终独立复审当前范围无未关闭P1/P2。初轮/最终测试输出保存在[证据目录](evidence/finance-f0-2026-10-21/)。

证据文本只规范化了行末空白，保留初轮失败及最终通过信息。测试库最后清理了早期失败验证遗留的3个本任务随机schema；最终不保留测试业务记录。专属PG容器在验收结束后停止，未接触平台/生产数据库。源码在 `feat/finance-f0` 独立分支保留，未推送、合并或生产部署。
