# 企业连接架构

## 企业只读连接（R3-A）

| 接口 | 权限及内容 |
| --- | --- |
| GET `/v1/me/connections` | 当前员工安装元数据和本人的consents，机器无员工拒绝 |
| GET/POST `/v1/admin/connections` | 租户管理员列安装/创建disabled安装；POST参数name/corp_id/agent_id/secret_ref/operations/request_id |
| PATCH `/v1/admin/connections/{id}` | expected_version/request_id/status/capability_evidence；企业和应用标识不可原位换绑 |
| POST `/v1/admin/connections/{id}/accounts` | user_id/userid明确映射已有员工，不授角色 |
| POST `/v1/admin/connections/{id}/package-bindings` | 已发布归档package_digest/connector_name/operation_id/operation/endpoint/GET/effect=read/server_bound_parameters/request_id；不可变 |
| POST `/v1/me/connections/{id}/binding/start` | 当前本人浏览器；返回authorization_url并写HttpOnly尝试Cookie |
| GET `/v1/auth/wecom/start` | tenant/connection/return_to，固定客户端授权页；未预配员工不自动创建 |
| GET `/v1/auth/wecom/callback` | 一次性state/code、同浏览器Cookie，成功签发原有平台登录会话 |
| POST `/v1/me/connections/{id}/consents` | 本人浏览器确认request_id/operations/expires_at/purpose；默认上限24小时，机器和act-as不能创建 |
| GET `/v1/me/connections/{id}/consents/{consent_id}` | 当前本人，其他员工404 |
| POST `/v1/me/connections/{id}/consents/{consent_id}/revoke` | 当前本人，request_id/expected_version，旧epoch失效 |
| POST `/v1/me/connections/{id}/read` | operation为self_profile.read或管理员directory_preview.read；parameters必须空；白名单投影+source/read_at/connection_ref |
| POST `/v1/work-tasks/{id}/connection-authorization` | 本人浏览器，request_id/expected_version；显式继续原任务，次数预算和恢复窗口不重置 |

应用token、员工外部身份与个人consent分离；内部凭据独立加密存储，不进业务包、对话、脚本或密钥列表。关闭新读取后保留当前本人查询、撤销和既有任务取消。401/403/404/409按认证/权限/资源不可见/CAS状态变化区分；供应商HTTP200仍核定errcode。更多边界见[供应商契约](../superpowers/specs/2026-10-22-r3a-wecom-contract.md)。

持久化由同一控制面池的租户短事务提供；供应商HTTP、token更新和模型调用均在事务外。密文采用独立表并复用既有加密保管库。安装与委托默认关闭，现有R2任务的connection_refs默认空，旧记录可读。

每次供应商派发及结果交付前核对当前员工角色、代员工key和固定授权引用；目录的每批读取也遵守同一检查。OAuth完成在租户锁内复核安装版本，并原子提交外部身份与登录会话。在途撤权的后台任务清理执行资源并进入需要重新授权的阻塞状态，清除旧工具批准；未知写入及用户取消仍按原规则优先收尾。
