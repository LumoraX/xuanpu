# F15 报销业务会话效率优化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. 用户要求完成后才本地提交，覆盖通用技能的中途提交建议。

**Goal:** 在原240次/单会话20万上限内减少重复查询与委派，并完成两次完整真实业务验收。

**Architecture:** 仅调整7个现有Skills中相关4个指令的分工、按需读取和小批量历史分页，保持API声明、全包可调用能力并集、平台/财务权限和所有现有验收门槛；Task1b明确将入口工具从1项调整为原子Skill已有13项。跨线程传参仅为线索，当前读取与human决定不变。

**Tech Stack:** Markdown Skills、Python/pytest、现有Gateway模型工厂/公开HTTP、独立PostgreSQL和AES原件。

## Task 1: 真实基线与最小Skill修复

Files: examples/finance/skills/{finance-assistant,expense-reimbursement,invoice-intake,finance-review}/SKILL.md；设计pack对应4文件；packages/xuanpu-finance/tests/test_f15_skill_contract.py。

- [x] 依据final6真实raw保留三阶段35次/205306tokens的精确计量、委派与重复API清单，独立设计审查。
- [x] 新增契约测试：缺事实入口直接追问；普通报销单次expense委派；本人提交预检不交审批Skill；修订保留当前真实单据未改字段；小批历史5/长原文1、游标与截断边界。先运行`uv run pytest packages/xuanpu-finance/tests/test_f15_skill_contract.py -q`取得实际RED并保存日志/XML。
- [x] 最小实现对应Skill规则、保持自包含原文/首尾空格换行和不扩权，两份逐字一致；实际运行同一回归GREEN，必要Ruff/工程约束。
- [x] 离线压力场景与真实RED分别记录；独立规格审查后质量审查，问题修复复审。

## Task 1b: 普通报销入口直接编排

Files: examples/finance/skills/finance-assistant/SKILL.md、设计pack对应文件、test_f15_skill_contract.py；设计见2026-10-24-finance-f15-direct-entry-design.md。

- [x] 核准7个实质业务回合固定派发/回收成本及13既有工具集合，显式记录root1→13但全包可达并集不变；阶段1规格/质量关闭后才开始。
- [x] 先对发布root精确13工具与无多余ordinary委派写RED，完整保留旧阶段1测试结果；仅更新与新职责明确冲突的契约，不删除原文、鉴权、当前版本与人工门槛。
- [x] 最小root两副本GREEN：ordinary internal直接处理，独立caps+companies只读可同轮、当前GET/update/preview严格按依赖，专业general仍child/self-contained原文；预算/原运行器自然提示不改。
- [x] 实际包注册器验证13工具为原子Skill并集子集、总API并集与77/24/7/shellfalse不变，无execute/original；独立规格再质量后新冻。

## Task 2: 原预算真实完整复验

- [x] 保留旧740冻结和失败记录，不改240/20万/其它上限、不重置143/141+2未知，原配置指纹一致。
- [ ] 新冻并fresh final7/final8各完成完整流程；与原同三阶段比较真实calls/input/output并统计重复工具，目标22次/15万内且实测低于原35/205306；全流程同一owner业务会话不拆开绕限。
- [ ] 所有草稿/预检/三个人工决定、原文、所有版本、重新会话回查、独立原件移除后读回、无支付/入账、越权负向门槛不改。缺用量仍硬停，未知5停止，原240总上限内有限网络恢复按已授权范围保留快照和计数。
- [ ] 独立核原始输出/模型身份/已知与未知用量/真实PG业务事实及效率改善，不把离线压力测试当真实模型结论。

## Task 3: 最终门禁与本地交付

- [x] 独占PG依次运行全量和quick：`FINANCE_TEST_PG_CONTAINER=xuanpu-finance-f15-pg uv run python scripts/finance_acceptance.py --dsn-file /tmp/xuanpu-finance-f15-suite-dsn --output docs/guides/releases/evidence/finance-f15-2026-10-23/full-final --suite full --timeout 1800`，再同入口quick且输出docs/guides/releases/evidence/finance-f15-2026-10-23/quick-final；实际命令/结果落证。
- [x] Ruff F/S108/I/TC packages/scripts、strict mypy packages/*/src、锁、工程约束、最后文档测试；同步当前指南/缺口/验收报告和仍缺公司制度/人员。
- [x] 更新业务ZIP及受影响源码分发制品、两本地镜像源字节核对，9制品SHA与新冻结对应。
- [ ] 最终独立规格/质量、秘密扫描、当前文档/原始空白检查、仅清理owned PG/卷和临时DSN/状态，原检出干净。
- [ ] 所有必要门槛通过后仅本地中文提交，报告真实commit/测试/效率数据与未覆盖层；不推送合并部署。

## 最后状态：离线完成，真实完整门槛未完成

final7/8前三阶段分别10次/83000与10次/84738，但整条均失败。final7事由层级问题已修并在final8实际通过；final8历史已完整读到但根答复漏旧驳回，保留真实失败/09和10未跑。最后generic历史意见及q字面条件窄修145项离线与独立规格/质量通过，未再消费模型，最终源尚未真实复验。

累计208/240调用（206完整usage、2未知）且原单会话20万保持；余32保留，按当前实测不足fresh完整流程。两次完整通过0/2，不作完成提交、不推送合并部署。全量/quick/指定静态/制品/秘密与资源清理见F15验收进展；未关闭真实门槛、最终完成审查及本地提交，不把旧结果或合成身份替代正式资料。
