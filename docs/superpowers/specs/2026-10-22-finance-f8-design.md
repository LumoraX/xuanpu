# 财务 F8：实际使用、材料恢复与浏览器验收

## 范围与方案

用户授权连续拆计划并开发至完成，明确不做支付，沿用独立存储、Skill/API/HITL。F7基线b32a061已有普通报销、申报文本、真人审批、修订撤回、版本原件与历史查询。本轮审计确认：已受理材料操作刷新后无列表入口，普通附件仍需手填技术编号，F7尚未实际浏览器验收。正式发票查验、跨单票额/重复报销判定、支付/记账/税务/法定档案、外部企业微信/飞书审批继续不在本轮范围。

方案比较：只补按钮不能恢复页面丢失的操作；重建通用附件中心超出记录需求。采用两个受权只读API与附件点选/恢复，复用持久finance_operation/item/receipt及现有幂等；不新增业务库或另一套审批。

## 受权材料查询

新增listSupportingMaterialImports：GET `/v1/finance/supporting-material-imports`，参数legal_entity_id/cursor/limit，limit 1–100、默认20；只列当前tenant/company/owner且purpose=supporting_material的原操作。返回items（id/version/status/created_at/total和summary计数）、total、next_cursor。SQL授权过滤和count独立；按(created_at,id)降序，新收件优先，游标绑定tenant/user/company及方向，count/items在同一repeatable_read快照内。列表total为operation数，summary.total为该操作文件数；不返回文件正文、对象键、平台凭据或其他人的材料。即使reviewer也只列本人的“我的收件记录”。

新增getSupportingMaterialImport：GET `/v1/finance/supporting-material-imports/{id}`，同样当前owner、当前公司授权及purpose检查。返回operation（复用原getOperation结构）及materials数组（position/state/receipt_id可空/filename可空/size_bytes可空/media_type可空/sha256可空/verification_state固定not_checked、selectable为服务端计算布尔值）。只从实际收件责任读取元数据，只有单项state=material_received且receipt/item的tenant/company/operation/position/固定file_ref/摘要/对象责任键一致才可选；未取得完整真实回执不能选为附件。整体operation partial/取消不排除其中已完整保全的项；列表本身不替代实际字节核对；失败/取消/待补记录如实展示。原getOperation与现有幂等保持兼容。可供Skill查询准备草稿，原件字节仍仅human并绑定具体报销单据和版本读取。

## 实际使用与恢复

普通报销新建/编辑展示附件项，支持查看名称/状态、点选真实已收件附件、移除当前项；保留历史关联，移除不删除原件。旧附件只有回执时显示“已有附件n”，详情技术信息可展开，普通路径无须手填ID。旧的编号辅助输入保留在折叠技术信息中并与点选状态一致。表单内点选使用内嵌选择区，选择、取消、翻页不替换当前表单或丢未保存的事由/明细。新增“我的收件记录”入口，刷新、切页或关闭后可找回已受理原操作，查询结果并选择用于当前草稿，不把receipt当批准或发票验真。

上传与财务收件各自保留稳定请求标识；浏览器仅保存按tenant/user/company隔离的有限恢复线索（文件名/大小/类型/SHA、平台request_id、固定file_ref、财务幂等键和可用operation ID），不保存原字节、报销正文或令牌，不作为业务权威。每个tenant/user/company最多一个未决线索、序列化不超过8192字符，同一当前员工最多10个公司线索；达到上限或localStorage拒绝/写失败须明确报错并阻止新的上传/收件POST，不能默默失去恢复能力。平台POST前写入原request_id及固定文件元数据，财务POST前写入原file_ref及幂等键，表单编辑不能覆盖未决线索。刷新后本人主动恢复：未知平台上传需重选相同文件并核对摘要后用原request_id；已知file_ref可用原财务幂等键确认收件；已知operation只查询原结果。不得自动换键重发。坏的/不相符的线索明确阻断并允许用户放弃本地线索，放弃不删除平台文件或财务历史。跨员工/租户/公司不能读取或重放别人的线索。

实际接受成功以后以独立PG中的操作及回执为准；浏览器线索丢失仍可通过“我的收件记录”恢复；整页刷新不承诺恢复尚未保存的报销正文。材料修改要失效旧保存确认，同表单在途编辑保留原操作恢复链；导航/身份变化不得复活旧页面。

## 端到端与交付

真实隔离PG和真实平台员工cookie/CSRF/签名出口，实际console静态资源通过回环HTTP服务供浏览器操作；仅测试员工及有效测试PDF，模型运行时关闭。浏览器完成上传、受理后刷新、找回并关联附件、草稿/本人提交、审批人下载原件并驳回、申请人替换/补充重提、两级批准及历史原件查看。读独立PG核对实际单据/决定/回执数，外部执行为零。平台目录可用测试人员配置，不能声称实际企业身份/正式发票或生产验收。

独立重启/原平台源移除后的原件恢复继续回归。失败/未知平台上传及财务收件用故障注入验证原request_id/幂等键重试唯一；浏览器截图与日志、DB结果、包契约、构建源字节证据分别保留。

预期finance业务包0.9.0-f8，74项实际API，7个Skill；财务发行2026.10.22.5、console2026.10.22.4。原有接口兼容，所有文档同步。全量pytest、ruff F/S108/I/TC及mypy绿后本地中文提交；不推送或生产部署。F8完成限定本范围，真实模型语义/生产负载/正式政策与外部法定能力另验。

### 实际验收补充：模型入口与后台跟进

实际浏览器发现平台后台proactive会直接经ModelClientFactory创建语义采样，绕过runtimes.get。隔离夹具因此须在Platform构造时注入严格拒绝的模型工厂，覆盖create/session/runtime入口，分别记录blocked_attempts与实际派发model_calls；后台尝试可以非零，但全部被拒且实际模型调用为零。保留后台失败的真实记录，手动报销及审批仍需完整完成，不能删除后台或伪造零尝试。此变化加强验收装配，不改变产品业务流转。
