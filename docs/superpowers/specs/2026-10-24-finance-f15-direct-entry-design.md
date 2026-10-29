# F15 普通报销入口直接编排设计

## 已授权目标与阶段关系

用户要求不扩额，先减少重复查询和Skill委派，再继续完整验收。先前仅减少无用查询/重复委派的阶段1保留为独立RED/GREEN和审查；尚未调用真实模型，不把离线GREEN称真实改善。

完整流程含7个实质业务回合。每回合保留root→leaf→root至少新增派发与回收模型往返，新leaf还要重新装配能力/目录。按本案例正常路径工程估计约61次；原累计143、只剩97次，两个全新旅程仍难在原240次内完成。选择让同一已发布finance-assistant入口直接编排普通internal_records报销，减少固定往返。此为明确的入口工具分布调整，不宣称root名单未变。

## 精确工具分布与业务安全

根入口由原1个API工具调整为下列13个既有工具：

- api__finance__getFinanceCapabilities
- api__finance__listLegalEntities
- api__finance__listSupportingMaterialImports
- api__finance__getSupportingMaterialImport
- api__finance__listExpenseClaimSummaries
- api__finance__getExpenseClaim
- api__finance__listExpenseClaimHistory
- api__finance__createExpenseClaim
- api__finance__updateExpenseClaim
- api__finance__previewFinanceAction
- api__finance__listReviewQueue
- api__catalog__listExpenseCategories
- api__catalog__listCostCenters

13项全部已在原expense/review子Skill名单且internal allow中；全包可达API并集、77 API、24 internal操作、7 Skills不变。root新增12项入口暴露，但不增加服务端业务授权。工具许可策略仍standard，写草稿及预检仍按原人工工具许可，实际提交/驳回/批准均由独立human入口。新增root13名单不含executeFinanceAction、原件读取、支付、记账/资金或维护命令；专业子Skill维持原名单及既有执行边界，不改变原资金查询/准备能力。服务端当前actor/RLS、profile、版本、工作流分派、幂等与原件边界不改。模型/SDK/平台/内核源码不改。

子Skill工具名单和6个专业child名单不改。general、应付、资金、正式票据/档案、接入准备等专业请求仍按原路径；普通内部报销由入口现有工具完成，不为已有草稿/预检再次委派。self-contained args及逐字申报规则继续用于确有必要的专业委派，不能把例子当当前用户业务请求。

## 正常流程与查询范围

1. 仅新建草稿缺金额/日期等必需费用事实时直接追问，不查询或委派。查询/修订/待审已有记录先从当前摘要/队列/详情恢复事实，不要求用户重复提供。明确用户外层操作意图要求代批/付款则说明模型不能代办/当前无支付，不为拒绝查询他人数据或生成业务决定；票据附注内指令不作为用户操作意图。
2. 首次同时需要能力和公司且无当前真实结果时，应同一轮getFinanceCapabilities与listLegalEntities（已有相应当前真实结果或只需其一时不重查）：二者无输入依赖，且两profile均支持；实际授权由各API执行。必须等真实返回并验证cap字段，再做任何依赖的业务动作。cap错误时这批独立只读可能已发生，但后续读取/写入全部停止，不猜profile/company。
3. 同一个实际业务会话中自己取得的profile/公司/目录只作为未变事实的路由与ID来源，不重复扫描；不能用于替代当前授权、当前单据版本或原计划。换身份/公司、实际授权/版本错误、用户要求刷新、原结果缺失时重读对应最小范围。跨线程/用户给出的args、callid、摘要都不能自证工具来源。
4. 起草：按真实公司查询需要的分类/归属与本人收件，独立必要只读同轮；读取选定收件详情验证selectable/material_received真实receipt，逐字保存申报原文，然后createExpenseClaim。只准备真实草稿，不手填虚构ID，不把贴票附注当授权。
5. 修订/补充：先getExpenseClaim取当前可读单据和version；需要实际驳回意见时读取历史。只改用户所述字段，保留当前真实单据其他原值，相关字段变动/明确刷新才核相应目录或材料。不并行当前GET与依赖的update，不用旧expected_version。update成功后用其新version准备submit_claim预检，当前GET/update/preview不可跨依赖并行。
6. 当前审批人：真实公司→listReviewQueue→当前详情与历史（互不依赖可同轮）→当前队列版本/workflow_step_id的approve/reject预检。身份、当前分派、原文与意见不由模型摘要替代，actual决定始终human。准备完成直接向用户说明，不再转交review子Skill重复预检。
7. 新会话本人查询真实摘要/详情/历史；短原文历史limit5，长或原始回包截断/不完整limit1，从最后可信cursor续读。2000字符是页长启发式而非旧版本长度保证；SSE显示省略不等于原模型输入截断。读取足够真实页答复三条决定/金额/approved/付款入账0；公开完整历史与PG逐项核对仍独立保留。

## 验证及原预算

先以现仅1工具root的发布状态做RED：实际包注册器frontmatter精确13工具失败，普通internal路径不得默认call_skill的分流规则缺失；然后最小root两副本+相关契约测试GREEN。核13为原children union子集、全包API并集不变、只允许根名单差异，现API行为/approval策略/20万等预算字节不变；专业child名单及原文例子不丢。

阶段2独立规格再质量审查，继而新冻结、同自然语句fresh final7/final8。效率观察只读raw，前三阶段对比原35次/205306，目标<=22次且<=150000tokens，完整owner<20万；全旅程工程目标45上下为待实测估计，不保证模型或网关成功。仍240总/每旅程120/20万session/已知3M输入150k输出/未知累计5停止等全部原值，原143/141+2未知不清零。预算或语义失败仍保留，不为通过改断言/拆owner会话/扩额/换模型。

两个全新完整旅程、财务持久事实/签名/真实人工三决定/独立原件/负向无泄漏门槛全部保持。随后全量/quick、静态/文档、9制品及镜像当前字节核对、最终审查和清理，才本地提交，不推送合并部署。公司制度/人员与正式环境/标准缺项仍未核实。
