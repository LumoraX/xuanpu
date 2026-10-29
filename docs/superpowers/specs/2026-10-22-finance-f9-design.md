# 财务 F9 内部使用与上线准备设计

用户已授权持续完成F9，明确不做支付，且暂未准备试用公司/人员/部署环境；本轮完成可填写配置、上线预检及隔离试运行，不部署生产。复用当前独立检出，基线16875fb，分支feat/finance-f9。

## F0–F8缺口与边界

F8已完成收件点选/恢复、内部报销/HITL/历史原件的实际浏览器闭环。仍有两个直接使用缺口：旧报销列表返回完整申报原文，因响应上限页面只能一页一条；部署仍是通用财务配置，缺少专用的内部记录运行边界。真实制度/人员、SSO部署、独立生产凭据与IAM、异机灾备、生产负载和真实模型语义继续未验。支付、ERP、税务/官方验真、法定档案和外部审批不作为本轮依赖。

选用在现有进程外服务内增加有界摘要查询和显式profile，保持原DB/幂等/HITL。仅写手册仍需操作约定；直接部署则缺实际环境资料。F9不新增数据库或审批权威，不改taifeng或平台业务层。

## 1. 有界报销摘要检索

新增listExpenseClaimSummaries，GET /v1/finance/expense-claim-summaries。必需legal_entity_id，可选view=mine|visible（默认mine）、status（原6种状态）、q（最长128、trim后搜索）、cursor和limit（默认20，1..50）。空白q归一为空；q按字面量不区分大小写匹配单号或事由，%/_不当SQL通配符；不搜索申报原文或附件正文。

权限复用当前报销可见规则，SQL始终绑定tenant/company/current user；mine额外限定owner，即reviewer看“本人草稿”也不混入他人。按(created_at,id)倒序续读，游标绑定tenant/user/company/view/status/归一q/方向。count/items在同一repeatable_read事务内，count独立于分页。当前角色撤权即时失效。

响应{items,total,next_cursor}，items仅{id,legal_entity_id,owner_id,version,status,total,reason,created_at,record_mode}；不含invoice_information、lines、evidence、raw content、workflow或对象地址。reason按现有领域上限，最多50项，连JSON控制字符转义也须验证不超过平台1MiB。旧listExpenseClaims和完整详情/历史契约不变，不新建表。

页面默认本人记录、20条摘要，提供状态筛选和单号/事由搜索、全部可看记录；原本人草稿使用view=mine+status=draft。筛选变更清空游标与旧页，迟到响应不得覆盖新筛选/公司/身份；未知写请求与现有materialForm/revision保护保持一致。详情再读取完整事实，不用摘要猜审批、原件或验真。

## 2. 内部记录专用profile

FinanceConfig新增profile=general|internal_records，默认general以兼容原通用阶段与回归。internal_records显式要求：公司非空、每公司有enabled/internal_record费用制度、至少一个collector且非该公司审批步骤人、现有公司/岗位/制度验证全生效；synthetic和execution_simulation都false；不允许funding/settlement/compliance policies/providers、business_documents和payee_accounts。拒绝样例REPLACE_标识和.invalid网关，防止把未填写模板当可运行配置。

运行时在独占实例锁取得后、迁移/配置写入和任何网关/供应商客户端创建前，执行只读持久库兼容检查：空库允许；已有库不得有非internal_record/非expense_claim单据、正式invoice、结算/合规操作、非supporting_material的finance_operation（含尚无invoice的accepted/running发票导入）、非expense_claim或非允许action的action_plan、enabled资金/结算/合规制度或provider、simulation模式。检查覆盖实例全部tenant/company；缺列/未知旧结构失败关闭，不自动重写、清理或迁移通用业务历史。另有迁移需求须显式制定迁移方案。

internal_records只装配原件收件worker和本地通知worker；不构造供应商注册表/HTTP客户端、结算或合规worker，不注册callback。HTTP仅注册内部白名单操作：capabilities、授权公司、费用分类/成本中心、本人收件import/list/get、get/cancelOperation、普通报销create/summary/list/get/update/history/material、reviewQueue、preview/getPlan/execute、站内提醒读写、只读overview。通用支付、应付、资金、查验/红字/档案等路径无路由，不能借参数开启。

共用preview/getPlan/execute的profile守卫只允许expense_claim和submit_claim/approve/reject/withdraw；execute按实际不可变计划校验action/target再进入既有人工来源/版本/权限/CAS。不能用旧通用action_plan或payable穿过共用执行入口。profile不替代mode：read_only仍拒写，disabled仍按原停用，draining继续既有取消/只读约定。原件及实际执行依旧只human。

新增getFinanceCapabilities，GET /v1/finance/capabilities，仍须可信平台身份认证；只返回profile、mode、service_version、internal_records_only，不表示个人权限或生产可用。页面先读能力，internal_records隐藏更多通用财务入口和通用接入检查，不因界面隐藏替代服务端边界。Skill先读能力，在internal_records只准备记录草稿/预检及元数据查询，决定和原件仍留真人。

## 3. 上线前配置与检查

新增examples/finance-internal-records/config.example.yaml，含明确REPLACE_公司/员工、只读mode、production环境、internal_records profile、两步审批、独立DSN/服务令牌/平台网关令牌/签名公钥/AES文件路径、无任何供应商配置。模板缺值必阻断，不生成生产密钥、不套用样例账号。

--preflight在internal_records下使用专用只读离线检查，复用F6固定凭据权限/格式/隔离和TLS检查，核公司制度/角色/profile；不要求支付/ERP/WORM能力、不访问数据库/网关、不创建目录/迁移/worker。配置齐备时status仍not_verified且production_status=not_verified，checks各项passed；失败status=blocked/退出2，输出不含路径/DSN/令牌/正文。报告摘要绑定非秘密配置，不能充当上线许可或人工计划批准。general行为保持不变。

指南覆盖填写、凭据分别准备、只读启动验证、公司/人员授权核对、备份/停用/回退、转enabled须操作员确认实际部署配置，以及实际PG/原件/平台身份分别负责的边界。仅文档列人工上线步骤，不自动改生产开关、发布或部署。没有正式公司/人员资料时保持模板blocked并明确待配置。

## 4. 验证与交付

业务包finance 0.10.0-f9（76项API/7 Skills），finance发行2026.10.22.6，console2026.10.22.5。默认general发布契约及白名单profile实际路由均验证；新增摘要真实PG/签名HTTP覆盖字面检索、分页、撤权/跨租户、超长原文不膨胀响应、只读/停用；profile覆盖配置拒绝、旧库拒绝且无写、共用执行越界、没有外部worker/HTTP/回调。

隔离浏览器沿用F8真实TCP/员工cookie/CSRF/签名出口，严格拒绝全部模型工厂，后台被拒尝试如实计数。测试配置选择internal_records，验证摘要检索和本人范围、真实提交→退回补充→两级审批、历史及原件SHA，外部操作0。离线预检分别验证未填模板blocked和明确测试配置checks通过/not_verified；真实CLI重启验证profile保持和审批记录可恢复。测试环境、假公司/账号/票据、重建fixture均与生产事实分开。

完成前运行全量pytest、固定quick、ruff F/S108/I/TC packages/scripts、mypy packages/*/src、文档/契约/源码约束，构建wheel/sdist/业务ZIP/镜像并逐字节核对。独立规格/质量审查、证据和清理完成后中文本地提交；不推送/合并/部署。正式环境依赖保留not_verified，不用阶段完成冒称上线。
