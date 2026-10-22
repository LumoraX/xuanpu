# 财务 F7 内部流转记录验收

F7内部记录范围开发与本地验收完成。用户2026-10-22明确“这里不做支付，仅作发票报销相关流程的流转记录”，本阶段据此替代此前首家外部供应商接入目标，无供应商资料依赖。

## 已交付范围

- 发票申报原文随报销版本原样保存，仅internal_record制度接受；拒绝空白、NUL及非法UTF-8，申报仍未查验，不创建正式发票、不取得支付或记账资格。
- 普通报销真实附件上传、固定文件版本独立加密收件、回执关联和指定版本原件读取。每块重验当前权限、岗位代数、关联、大小及实际SHA；旧PDF awaiting_original仍能核对原件，原状态不改变。
- 本人HITL提交→逐级真人审批→驳回附理由→本人修订→重新提交→批准；已有撤回继续支持。完整原版本和实际决定可按权限逐页查看，当前附件可替换/清空，旧附件仍随历史保留。
- 原生人工页无需模型；列表及完整历史每页1条，避免合法长文本超过平台1MiB响应上限。未知上传请求复用原幂等键；同表单在途编辑、费用增行及资金依据增删保留原operation恢复链，切页/身份变化拒绝旧响应。
- 两个新API为listExpenseClaimHistory与仅human的getExpenseMaterial；总72项API、7个Skill，包finance 0.8.0-f7；服务发行2026.10.22.4，console发行2026.10.22.3。独立进程、PG及原件持久归属不变。

## 已完成的分层验证

| 层面 | 已核实事实 | 实际边界 |
| --- | --- | --- |
| 后端/签名API/权限 | F7输入、历史、原件、跨公司/租户/类型、撤权代数、非法状态等22项通过 | 测试员工与隔离PG，不代表公司生产配置 |
| 真cookie/CSRF人工入口 | 平台真实上传→独立收件→驳回补充→重提→两级批准→历史恢复；模型入口被设为失败；外部执行记录为0 | 平台/财务真实ASGI认证与DB，尚未做本轮浏览器验收 |
| 实际CLI运行 | 真CLI三次启动，跨进程恢复待审；移除平台源字节后仍可读取独立副本并批准，第三次启动保留原文与决定 | 平台网络边界为回环测试目录；非生产身份/供应商 |
| 备份/恢复 | 本轮维护及三启动组12 passed；成熟PG工具绑定实际本轮测试容器 | 本地配套恢复，不等于异机灾备/RTO/RPO |
| 页面行为 | console 15 passed，16组F7记录行为及6类财务脚本通过，含在途编辑/结构修改/迟到epoch/revision/同幂等恢复 | 原生JS行为验证；不声称已跑真实模型或本轮真实浏览器 |
| 静态门禁 | ruff F/S108/I/TC通过，mypy 296个源文件通过；变更19个Python文件函数≤80、文件≤800、docstring/future检查通过 | 不等于生产负载或真实模型语义验收 |
| 安装制品 | finance wheel/sdist 127个源码文件、console wheel/sdist 22个源码/静态文件逐字节一致；业务包ZIP确定性打包与20个契约/Skill参考文件一致 | 本地构建，未推送或发布到正式平台 |
| Docker镜像 | xuanpu-finance:2026.10.22.4，UID10001，127个Python源码摘要与本轮源码一致 | 已构建并运行无网络源码核对；本轮未做Compose部署验证 |
| 生产/外部系统 | 未部署；不启用支付、ERP、税务、法定档案及企业微信/飞书审批 | 正式标准/签名/WORM、真实模型、生产凭据/负载/异机容灾继续未验 |

最终全量通过：实际2532例，2529 passed、3 skipped、0 failures、0 errors，440.76秒；finance用例没有跳过。验收入口按真实JUnit把整体标记为partial，以保留这3个非财务变体跳过事实，production_status仍为not_verified。三个跳过分别是内存实现不验证真实PG行锁、内存实现不验证数据库约束，以及已由共享通知锁覆盖的数据库身份行锁变体。

实际门禁命令：

```text
FINANCE_TEST_PG_CONTAINER=xuanpu-finance-f7-pg uv run python scripts/finance_acceptance.py --dsn-file /tmp/xuanpu-finance-f7-dsn --output docs/guides/releases/evidence/finance-f7-2026-10-22/full-final --suite full --timeout 1800
uv run ruff check --select F,S108,I,TC packages
uv run mypy packages/*/src
uv run mypy scripts/finance_acceptance.py
uv lock --check --offline
```

full入口清除隐式PG地址与测试选择后实际执行当前解释器`python -m pytest packages -q -ra --junitxml=...`；仓库pytest的testpaths同为packages，完整运行全部测试。原始命令、退出0、模块覆盖、实际JUnit总数和SHA见[最终全量报告](evidence/finance-f7-2026-10-22/full-final/report.json)及[原始日志](evidence/finance-f7-2026-10-22/full-final/pytest.log)。quick固定17个模块实际107 passed、0 skipped、0 failures、0 errors，约71秒，入口返回passed；见[快速报告](evidence/finance-f7-2026-10-22/quick-final/report.json)。文档/示例检查185 passed。

## 审查与故障处理

规格审查及最终质量审查均Approved。审查发现并已修复NUL写JSONB失败、附件并集导致无法移除、长原文列表/历史超响应上限、材料确认在途编辑丢operation及结构编辑同类路径；均保留红绿回归证据。

首轮财务回归556 passed/7 failed，已定位全部7项为维护夹具写死旧F4容器名；改为显式FINANCE_TEST_PG_CONTAINER或本机成熟工具后实际备份恢复通过，没有修改产品业务约束绕过。首次CLI扩展用例启动超时；检查发现测试异常负责人配置成approver，不满足reviewer约束，修正测试配置后三启动通过。首轮全量2529 passed/3项非财务skip、0失败，最后页面竞态修复后重新进行最终全量；原始输出均保留，不混作最终结果。

## 证据与制品

证据位于[evidence/finance-f7-2026-10-22/](evidence/finance-f7-2026-10-22/)，包含backend/frontend红绿、维护/CLI/cookie、静态、构建、源约束、业务包校验及实际制品清册。镜像ID为`sha256:a13c6ed0e6171061c9bc0ec5f726130c6762b9b529e267d5d0f5c5d22e7e596d`。可安装业务包为`/tmp/xuanpu-finance-f7-build/finance-0.8.0-f7.zip`，SHA-256 `be346fb05d9ff378232196174512435f9bef59e48d8237d1bc3587b216decac5`；wheel/sdist路径与SHA见证据artifacts.json。

设计见[F7规格](../../superpowers/specs/2026-10-22-finance-f7-design.md)，执行见[实施计划](../../superpowers/plans/2026-10-22-finance-f7.md)，操作见[F7指南](../finance-f7.md)，范围之外的历史能力见[缺口台账](../finance-gap-register.md)。

## 清理与本地交付

本轮独占PostgreSQL容器`xuanpu-finance-f7-pg`及匿名卷已删除；移除前仅剩主测试库finance_test_f7、0临时schema、唯一测试角色finance_test，备份恢复临时库/角色均已清理。端口64844已关闭，临时DSN文件已移除，原检出xuanpu保持干净；见[清理记录](evidence/finance-f7-2026-10-22/cleanup.json)。保留构建目录和镜像供后续审阅。

本地交付分支为`feat/finance-f7`，工作目录`/Volumes/Codes/lumorax/lumorax-platform/xuanpu-finance-f0`；基线e844796，中文提交记录由该分支git log查询。未推送、合并或部署生产。验收过程中原始失败输出使用目录内精确文件级.gitattributes保留原字节，没有放宽源码空白规则或改写原始回溯。
