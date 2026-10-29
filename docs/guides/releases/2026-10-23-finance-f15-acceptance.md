# 财务 F15 发票报销 Skill 验收进展（未完成）

更新于2026-10-24。已完成重复查询/委派优化、实测问题修复及当前源码的离线门禁和本地制品；**两次完整真实模型用户流程尚未通过，F15未完成，不作完成提交**。所有变更保留在隔离检出`xuanpu-finance-f0`、分支`feat/finance-f15`，基线`eb15fa49f83af07dc18da05a2e0fcdf0e3875546`。不支付、独立财务PostgreSQL/AES原件和真人审批保持；未推送、合并或部署。

## 当前实际结果与缺口

final7在驳回后补充时，仅更新了费用行reason而未更新单据顶层reason，原始调用、当前服务返回与独立PG只读导出一致。已以TDD补清单据`draft.reason`、行reason与审批`parameters.reason`的区别，以及本会话唯一真实ID直接定位后仍取当前授权GET/版本。[失败因果](evidence/finance-f15-2026-10-23/reason-revision-failure.json)、[PG导出](evidence/finance-f15-2026-10-23/final7/actual-pg-reason-failure.json)和[规格复审](evidence/finance-f15-2026-10-23/reason-revision-spec.json)保留。

final8随后实际通过补充/重提、三条human决定（负责人驳回、负责人批准、财务批准）、128.50金额与原文、approved、付款和入账均0、正式invoice/settlement/compliance表均0、所有真实版本和公开历史，以及平台源字节移除后独立原件同SHA回读。该次在新会话回查失败：模型已从两页真实历史读到旧轮驳回意见，却只答复后两条批准意见。另有[root独立PG只读核对](evidence/finance-f15-2026-10-23/final8/actual-pg-approval-audit.json)。完整原始结果为[final8/result.json](evidence/finance-f15-2026-10-23/final8/result.json)，[独立失败判定](evidence/finance-f15-2026-10-23/history-reply-failure.json)不覆盖其失败状态。

最后只做离线窄修：原/历史审批意见的最终答复须包含用户所问各轮真实人工决定，不能只列当前workflow；cancelled步骤不算人工决定，不硬编码测试数量或理由。q仅匹配当前单号/单据事由字面值，费用类别或旧事由线索默认本人无q查询，避免空查回退。自然用户提示、观察器和业务门槛未改。[设计](evidence/finance-f15-2026-10-23/history-reply-design-review.json)、[规格](evidence/finance-f15-2026-10-23/history-reply-spec.json)、[质量](evidence/finance-f15-2026-10-23/history-reply-quality.json)仅批准离线修复；**最终新源码没有完成真实模型复验**。final8的本人代批付款及无权同事挑战09/10未执行，不能宣称本轮完整越权/泄漏验收通过。

## 已测效率与原预算

下表只比较同一申请人前三阶段（澄清、起草、修改预检），各次整条均失败，不能视作完整成功：

| 尝试 | 前三阶段调用 | input+output tokens | 整条失败点 |
| --- | --- | --- | --- |
| final6 | 35 | 205306 | HarnessFailure:SESSION_TERMINAL:05-owner-resubmit |
| final7 | 10 | 83000 | model_revision_draft |
| final8 | 10 | 84738 | fresh_query_semantic_facts |

final7/8前三阶段调用均从35降为10，减少71.43%；token从205306降为83000/84738，减少约59.57%/58.73%。缺事实阶段不查业务，普通内部流程无默认子Skill委派，修改不重扫未变目录或收件。[逐阶段计量与原始SHA](evidence/finance-f15-2026-10-23/partial-efficiency-comparison.json)保留，cached input仍包含在input中。该指标来自对应旧冻结候选；最后历史答复修复仍未实测，不把它的离线测试算作实际效果。final8从澄清到补充重提的同一申请人业务会话为15次/154106tokens，已在原20万内走过重提；但model183仍重复一次本人摘要定位，唯一已知ID省查询规则未稳定兑现，不能称所有重复查询已清除。

全轮含探针和所有失败累计208次，其中206次有完整usage，56/59两次断流usage未知并保留null；已知input 1207876、output 30074，不能据此推算未知总量或货币费用。[逐条账本](evidence/finance-f15-2026-10-23/model-budget.json)与原始轨迹核对一致。用户原240次、单业务会话200000tokens和累计第5次未知停止等限制均未扩大或重置；400次/600000只是未执行提案。

已保留32次未消费。按当前完整流程与final8在回查前已消耗43次的实测，剩余量不足重跑全新完整流程，因此主动停止进一步真实模型消费；ledger并非触发STREAM_LIMIT，不把主动停止称为硬上限触发。没有拆业务会话绕过20万限制，也没有换模型、手填业务对象或放宽断言。仍缺最终新冻结下两次全新完整通过及09/10负向复验，本地提交继续待完成。

仅供后续明确授权的[新预算提案](evidence/finance-f15-2026-10-23/post-efficiency-budget-proposal-review.json)是累计调用320次，单业务会话仍200000 tokens，其它限制和原账本保持；当前未应用，执行授权为false。依据final8实际43次至回查，补09/10及避免空查后两次约92–94次，累计约300–302，320提供18–20次余量；这是工程估计，不能保证语义或网关成功。原用户“先别扩额”不被视为批准。

## 源码、离线门禁与本地制品

业务包0.16.0-f15；77声明API、24内部操作、7 Skills、shell=false保持。根入口工具从1项调整为13项，全部来自原expense/review子Skill；整包模型可调用API并集仍68项，是工具分布变化，不能声称根名单未变。专业general流程和受限未知结果恢复保留，根入口没有execute/原件读取/支付执行工具。[实际注册/包核对](evidence/finance-f15-2026-10-23/skill-contract-validation.json)和[当前冻结清册](evidence/finance-f15-2026-10-23/frozen-source.json)对应740文件。final7与final8的原输入分别另存frozen-candidate5/6及各目录source-freeze，不能拿旧失败候选证明最终新源通过。

| 门禁 | 当前源码实际结果 | 证据 |
| --- | --- | --- |
| 全量 | 3070通过，3既有非财务差异跳过，0失败/错误 | [完整汇总](evidence/finance-f15-2026-10-23/full-final/report.json) |
| 财务quick | 620通过，0跳过，50固定模块 | [完整汇总](evidence/finance-f15-2026-10-23/quick-final/report.json) |
| 静态与工程约束 | Ruff F/S108/I/TC、strict mypy、offline lock及源码约束通过 | [命令与日志](evidence/finance-f15-2026-10-23/static-results.json) |
| 制品 | 9份wheel/sdist/业务ZIP与冻结输入逐字节核对 | [版本、SHA与字节清册](evidence/finance-f15-2026-10-23/artifacts.json) |

额外[默认Ruff探针](evidence/finance-f15-2026-10-23/history-reply-spec-default-ruff.log)报告测试中的9项E501长行和1项ASYNC240，保留为非阻断已知项；本轮只声明仓库指定的F/S108/I/TC及工程复杂度门禁通过，不宣称所有默认lint规则为绿。

全量3跳过与F14相同，财务与nested无跳过；旧受控中止及PG工具前置失败另存，当前重新执行，未复用旧计数。业务发布20文件与设计对应文件一致，额外4份引用保持基线。finance服务/镜像2026.10.23.5、console2026.10.23.1、server/SDK2026.10.23.1。两本地镜像只build/load，核UID10001及实际源字节；[平台镜像核对](evidence/finance-f15-2026-10-23/platform-artifacts.json)不代表完整Linux业务或部署。

早先baseline/baseline2空cursor、final1子Skill恢复、final2/3网络断流、final4委派原文遗漏、final5观察器漏正常201、final6会话token上限和final7/8真实语义失败全部保留。平台可信leaf恢复/审计终态、SDK operation_terminal与seq=None兼容及正式成功状态取证均有单独RED/GREEN，未改内核或业务授权来通过。最终根工具许可仍只是模型草稿/预检许可，实际决定由独立human身份执行。

## 正式资料与未覆盖层

[公司制度与人员缺项](../finance-company-readiness.md)单独列正式主体、费用制度/分类/归属/金额和步骤/期限/材料/留存，以及申请人/各级审批与财务、SSO映射、替岗撤权与职责分离，并列应提供岗位和关闭证据。独立凭据/IAM/AES/恢复、正式SSO/TLS/origin/网络由管理员提供；不编造实际姓名或制度数值。知识库检索失败不能证明公司没有制度。银行、支付或ERP供应商不是当前内部试用所需补项。

当前为合成公司/身份、真实TCP cookie/CSRF/EdDSA与独立PG/AES，平台身份/控制装配InMemory；不等于实际员工、SSO或浏览器点击。配置请求/客户端模型事件为gpt-5.6-luna，事件名由客户端构造，网关后端身份未独立核实。正式公司/制度/员工、生产负载/部署、官方票据标准/税务/法定归档、外部审批、实际S3/异机恢复及完整Linux业务仍not_verified或不在本轮范围。

仅清理精确owned PG/卷及私有测试DSN/状态，原检出和私有模型配置保持；脱敏证据、账本和本地制品保留。[资源审计](evidence/finance-f15-2026-10-23/test-resource-audit.json)、[清理](evidence/finance-f15-2026-10-23/cleanup.json)及[秘密扫描](evidence/finance-f15-2026-10-23/secret-scan.json)单列。原始失败日志/XML保留原内容；最终文档测试见同目录`docs-final.xml`。未生成通过的提交门禁或本地完成提交，F15仍待完整真实模型验收。


[最终交付缺口复核](evidence/finance-f15-2026-10-23/delivery-review.json)结论为IncompletePendingRealAcceptance；[测试后源码与用量核对](evidence/finance-f15-2026-10-23/post-test-source-audit.json)确认当前740文件无SHA漂移。[清理后新增交付文件的补充扫描](evidence/finance-f15-2026-10-23/secret-scan-post-cleanup.json)补足最终记录范围，不重建已销毁的测试凭据。


## 后续2026-10-29记录

当前累计调用已到218/240（216次usage完整、2次历史未知），原模型和上限未变。执行逻辑及局部回查的最新实测见[10月29日记录](2026-10-29-finance-execution-efficiency.md)。本页740文件/3070通过等是10月24日对应旧冻结的历史门禁，不能代替后续源码验证。完整真实双轮仍0/2，未作完成提交。
