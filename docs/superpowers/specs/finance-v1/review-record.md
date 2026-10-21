# 财务设计审阅与契约校验记录

日期：2026-10-21。初轮运行基线：xuanpu `be36853`；初轮只新增财务设计与契约，修改文档入口。
业务范围已确认，书面设计待用户审阅；独立审阅结论为可进入实施规划，不代表已实现财务服务。

## 1 来源保全

用户原规范副本为 [source-requirements-v1.md](source-requirements-v1.md)，字节数 14961。
SHA-256：`85ebe45f6dcaed72cbeb0cae98dd7bdfad22c22cf1da015a83d2050c202c9e8e`。
副本与附件逐字节相同；修订在总设计另列，不覆盖原文。

## 2 独立审阅

第一轮发现四个阻断，修正后第二轮 Approved：

| 问题 | 已写入的修正 | 验证 |
| --- | --- | --- |
| Skill 跨目录读取共用规则会被拒绝 | 每个 Skill 自带 references/execution-rules.md，不使用越界引用或符号链接 | 当前安装内核的 read_skill 实际读取 7 次成功 |
| 专用付款／税务计划无法进入审批 | action_plan 的工作流、getActionPlan、审核队列和决定；专用 grant，禁止递归批准 | 独立复审核对闭环和过期／失效规则 |
| 主数据与原分配无法查询 | catalog 接口提供费用分类、部门项目、业务引用、收款账户、分配详情 | 包工具引用与接口一致 |
| 签名未绑定幂等键 | 请求签名纳入 Idempotency-Key，重放表以 issuer+jti 唯一 | 设计协议和 A33 验收项核对；运行实现尚未验证 |

另已明确 F1／F2 正式动作与 F3 真实查验的发布依赖；尚无发票的预付场景使用专用证据与审批规则。

## 3 本轮实测

| 检查 | 实际结果 | 覆盖边界 |
| --- | --- | --- |
| 原生业务包校验 | finance 0.1.0-design.1：校验通过；0 errors／0 warnings | 清单、API、工具引用及入口一致 |
| OpenAPI 3.0.3 | openapi_spec_validator.validate_spec 两文件通过 | 业务 29、目录 5，共 34 operations，无重复 operationId |
| 交付阶段 | F0 11、F1 13、F2 7、F3 3 | action-previews 内动作另受动作参数矩阵阶段限制 |
| 内核加载 | 7 个 Skill 均可加载 | 不表示真实模型已使用成功 |
| 附属规则读取 | read_skill 7 次实际成功，规则副本摘要一致 | 遵守单 Skill 目录边界 |
| 请求 schema | 14 个正反例通过 | 合法导入与计划执行可解析；越权字段、浮点／NaN／科学计数／负报销金额、旧式任意对象键等被 schema 拒绝 |
| 重复 schema | 7 类共享 schema 在两文件中一致 | Id、Money、Version、Digest、Error、BusinessRef、Allocation |
| 本地文档链接 | 相应设计与包链接检查通过 | 不把链接存在当作运行读取的替代 |
| 全量 pytest | 1847 passed，3 skipped；178.99 秒 | 当前平台仓库回归；没有新增财务服务测试，也不代表真实通道验收 |
| Ruff | All checks passed | 仓库规定门禁规则 |
| strict mypy | Success: no issues found in 164 source files | 已有平台源码，无财务实现 |
| 依赖锁 | Resolved 103 packages | 本轮未改依赖 |

14 个请求 schema 反例覆盖的是结构，不代替数据权限、签名验真、跨字段动作约束、业务批准或数据库并发测试。
3 项 pytest 跳过为既有环境／后端条件，本轮没有将其改成通过，也没有把它们计入正式财务数据库证据。

## 4 已运行的仓库门禁与复核命令

从 xuanpu 仓库根运行：

```bash
uv run xuanpu validate docs/superpowers/specs/finance-v1/pack
uv run pytest
uv run ruff check --select F,S108,I,TC packages
uv run mypy packages/*/src
uv lock --check
```

OpenAPI 复核使用已安装验证器，不改变项目依赖：

```bash
uv run python -c 'from pathlib import Path; import yaml; from openapi_spec_validator import validate_spec; [validate_spec(yaml.safe_load(p.read_text())) for p in Path("docs/superpowers/specs/finance-v1/pack/apis").glob("*.yaml")]; print("OpenAPI 校验通过")'
```

## 5 未执行项目

财务业务服务、通用可信身份透传与文件桥仍为待开发设计；没有新增生产配置、数据库迁移或部署。
没有连接真实税务、支付、ERP 或归档通道；没有生成真实会计凭证、付款或不可变档案。
没有真实财务原件／官方标准包验证或财务真实模型验收；[验收矩阵](acceptance-matrix.md)中的运行项仍为未执行。
包里的评测用例仅声明，尚未对模型执行；关键验收依据是实际业务记录，不能只凭文字 contains 判断通过。

## 6 用户确认后的设计补充

用户明确采用 HITL，并要求财务存储完全独立。总设计、F0 和领域契约现明确：关键人工决定持久保存在财务工作流，
批准后后台续办；独立 PostgreSQL 实例／数据库／账号、对象存储资源／桶／凭据、迁移和备份；
成功收件后财务持有原件副本，模块启停保留在途核对责任，身份服务故障不阻断已发送操作的回执落库。
增加 A34—A38 运行验收项；它们尚未执行。本节补充不改变 34 个 API 契约及设计包的 standard 调用确认策略。
第 3 节测试数量属于初轮实际运行，不能直接当作本次补充后的重新运行结果。

补充后的独立复审 Approved；在线授权仅约束新用户命令及新外部副作用，已发送操作的真实回执例外范围已收紧。
本次实际重跑：`uv run pytest` 为 **1854 passed，3 skipped，180.83 秒**；
`uv run ruff check --select F,S108,I,TC packages` 为 All checks passed；
`uv run mypy packages/*/src` 为 Success: no issues found in 164 source files；
`uv run xuanpu validate docs/superpowers/specs/finance-v1/pack` 校验通过。
两份 OpenAPI 校验通过，23 个本地链接可解析，原规范副本摘要未变，`git diff --check` 无错误。
这些是设计／契约与既有平台检查，A34—A38 的财务服务运行验收仍未执行。
