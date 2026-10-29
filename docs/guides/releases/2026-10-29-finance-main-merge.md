# 发票报销 Skill 本地主线集成验收（2026-10-29）

用户确认本轮以发票管理与报销 Skill 功能已实现为交付范围，并明确授权合并到本地 main。保持不支付、独立财务 PostgreSQL/AES 原件和真人审批；不推送、不部署，也不扩大真实模型预算。

## 集成内容

原 main 为 `60b6797f4127739359edf2be1adb6e4d1d77599c`，财务实现提交为 `f44a989a886cb535f045fd8fc57c099e635e0875`。财务分支原有 F0–F14 的16个提交及F15实现一起集成，主线19个R2–R4提交保留。本次未拉取远端或推送分支。

19个文件的冲突逐项整合：同时保留企业连接、组织协作、执行安排、能力目录与财务入口；平台同时注入 managed_connections、context_signer 和人工连接器；连接器在原审批通过后绑定可信调用上下文，两种 invoke 路径均保留。SSE同时保留共享来源外发检查、取消清理和 operation_terminal。版本保留主线 server/SDK/console 的2026.10.25.1，锁文件重新解析；34份控制台资源指纹与实际字节一致。两个主线验收脚本仅整理导入顺序以通过统一lint。

## 实际验证

- 原财务源742份文件与此前通过的3185项全量源码哈希一致，原报告0失败/错误、3项既有非财务skip；先核对证据再提交现有实现，未重复该旧组合全量。
- 合并后专项147 passed，覆盖console、SDK、配置、调用签名、嵌套恢复及待办重放，见[专项](evidence/finance-main-merge-2026-10-29/targeted.xml)。
- 对合并后的1117份冻结源码，只运行一轮完整`python -m pytest packages -q -ra`：4000项，3979 passed、21 skipped、0 failures、0 errors、无缺失模块。入口为`FINANCE_TEST_PG_CONTAINER=xuanpu-finance-merge-20261029 uv run python scripts/finance_acceptance.py --dsn-file /tmp/xuanpu-finance-merge-20261029/dsn --output docs/guides/releases/evidence/finance-main-merge-2026-10-29/full --suite full --timeout 1800`，见[全量报告](evidence/finance-main-merge-2026-10-29/full/report.json)。报告因既有skip如实为partial，production_status仍not_verified。
- `uv run ruff check --select F,S108,I,TC packages scripts`通过；`uv run mypy packages/*/src`为489 source files无问题。合并后文档链接另用`uv run pytest packages/xuanpu-cli/tests/test_examples_and_docs.py -q`核对，283 passed，结果归档到本次证据目录。
- [只读合并审查](evidence/finance-main-merge-2026-10-29/merge-review.json)Approved，无阻塞项。记录一项非阻塞P3：持久任务的usage局部缓存保留至hub释放，不影响计费或本次财务功能，后续单独处理。

源码尺寸复核发现自动合并后的sessions.py为807行；仅精简模块说明至800行，保留全部租约与恢复限制，执行部分AST完全一致。该说明性差异另留前后SHA，未改可执行代码，也未为此重复全量测试。

本轮财务业务网关模型调用新增0，累计仍218/240，历史未知用量56/59保留。完整真实模型双旅程仍0/2，09/10负向模型挑战未执行；这些记录不因代码合并变为通过。正式公司制度、真实员工、SSO、生产与官方财务标准的未验边界保持，见[公司资料缺项](../finance-company-readiness.md)及[上轮定点测试](2026-10-29-finance-execution-efficiency.md)。上轮F3补件构造偶发失败的原始证据继续保留，不宣称已经定位或修复。

21项skip与原main的2623 passed/21 skipped基线一致；被跳过测试文件逐字节与原main相同，包括仅适用于PG的故障场景在memory后端跳过、未显式启用的真实网关验收等，无财务skip；理由单独归档在skip-reasons.json。

本次测试只使用专属标签的本机临时财务数据库；秘密扫描通过，清理前无遗留客户端或咨询锁，已删除该容器、匿名卷及私有DSN，证据保留。未重建或发布生产镜像，未启动生产实例，未执行支付。
