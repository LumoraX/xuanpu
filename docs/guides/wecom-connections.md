# 企业微信只读接入

R3-A支持企业内部自建应用、企业微信客户端网页授权、本人基础资料读取和管理员通讯录预览。应用权限与员工本人同意分别管理；玄圃是组织和业务规则中心，通讯录同步属于后续R3-B。

## 企业管理员准备应用

1. 在[企业微信管理后台](https://work.weixin.qq.com/)的应用管理中创建自建应用，名称可用“玄圃只读接入”。可见范围先选测试员工，不要直接开放全公司。
2. 应用详情记录AgentId，查看Secret；企业信息页记录企业ID（CorpID）。不使用通讯录写Secret或第三方服务商suite凭据。
3. 按应用后台要求配置网页授权可信域名、服务器可信IP。玄圃需有正式HTTPS域名及public_url，回调为`<public_url>/v1/auth/wecom/callback`。只填写实际可达域名/IP，不用默认值冒充已核定。
4. 准备范围内和范围外两名真实测试成员。code身份识别成功不代表有权读取成员，必须验证实际资料读取和范围外拒绝。测试账号无需向玄圃提供企微密码。
5. Secret只写本机权限600的私有文件，或写入玄圃既有密钥保管库；安装中填写secret_ref。不要发送聊天、提交Git或填进业务包。仓库本机模板为被忽略的`.codex/private/r3a-wecom.json`；本模板不是已经生效的生产配置。

核定依据见[供应商契约](../superpowers/specs/2026-10-22-r3a-wecom-contract.md)。当前只支持企业微信客户端网页授权；未提供桌面扫码入口。姓名、头像等字段取决于应用类型和供应商权限，缺失不是代码可以补造的资料。

## 平台安装与身份

管理员使用`POST /v1/admin/connections`登记name/corp_id/agent_id/secret_ref/operations/request_id；创建后disabled。`PATCH /v1/admin/connections/{id}`需expected_version、request_id、status及真实capability_evidence引用。核定secret及真实权限后再启用，不把测试fixture证据用于正式安装。

管理员在`POST /v1/admin/connections/{id}/accounts`明确指定已有user_id与企微userid；不按姓名或邮箱猜测。另一种方式是已登录员工在本人连接页点击“绑定我的企业微信”，在企微客户端完成授权。已有usr及本地姓名保留，首次未知成员不自动新建管理员或员工。

预配员工可在企微客户端打开`/v1/auth/wecom/start?tenant=<本租户>&connection=<已核定安装>`。平台只保存300秒随机state及浏览器绑定，code一次性换身份，不保存成员票据。绑定过程若换了玄圃登录身份，必须重新开始。

## 员工使用

员工进入“功能 → 企业连接”，或在对话中请求管理本人企微连接。查看状态不产生同意。先看到范围及1小时到期说明，再由本人明确同意；读取本人资料与管理员预览通讯录分别确认。模型、机器key、act-as和SDK/CLI不能代替本人创建同意。

本人资料仅展示供应商实际提供的姓名、岗位、部门和成员账号。通讯录只代表应用当前可见范围，不保证全企业完整，不据此批量停用缺失员工。手机、邮箱、头像、原始供应商响应及token不返回。

随时点“撤销这项授权”。撤销后拒绝新派发并丢弃迟到结果；已发出的请求可能已到供应商，已经看过的内容无法追回。关闭整个连接功能后，已有同意仍可查看和撤销。

## 后台任务

已发布的`examples/wecom-readonly`需管理员用不可变归档摘要核定package-bindings，固定HTTP连接器/operation/GET endpoint及server_bound_parameters，不能同时配置静态auth。参数userid/access_token由平台填入，不展示给模型。

R2任务保存安装版本、consent_id与epoch。撤销、到期、停用后冷恢复显示`connection_reauthorization_required`；新同意不会自动恢复原任务。本人确认新同意后，`POST /v1/work-tasks/{task_id}/connection-authorization`用expected_version/request_id显式继续；保留预算、attempt及原恢复窗口，原工具批准需按当前任务重新确认。安排生成的任务须在原安排重新确认，不能改写多次发生共用的执行授权。

SDK提供list_connections/read_connection/revoke_connection_consent及connection_confirmation_url；CLI见[命令行说明](../../packages/xuanpu-cli/README.md)。真实企业身份和资料验收未完成时，不能将代码/模拟通过称为真实接入完成。
