# 接入飞书登录

P1 让员工用飞书自建应用登录 xuanpu，首次登录自动创建普通成员。平台按员工隔离会话、记审计与用量，并通过离职事件和通讯录对账停用员工。先完成[快速上手](quickstart.md)，准备一个租户及其管理员 API key。

本文配置与命令于 2026-10-04 用真实 `uv run xuanpu serve`、本地模拟飞书验证；**尚未用真实飞书应用验收**，最后的核对清单仍须完成。HTTP 字段与错误以[接口文档](../architecture/http-api.md)为准。

## 1. 创建应用并准备平台地址

在[飞书开发者后台](https://open.feishu.cn/app)创建企业自建应用，从「凭证与基础信息」取得 App ID、App Secret。P1 不保存员工的飞书访问令牌，不申请 `offline_access`；文档、日历等资源授权属于 P6。

平台配置增加以下内容，再启动或重启服务。`public_url` 只写协议、主机、端口，不带子路径；控制台与接口应同源。

```yaml
public_url: https://xuanpu.example.com
identity:
  cookie_secure: true
  default_tenant: acme
  session_ttl_seconds: 604800
  reconcile_interval_seconds: 86400
  login_landing_path: /console/
```

正式接入采用 HTTPS，保持 `cookie_secure: true`。本地 HTTP 模拟时可用 `false`；两个飞书基地址仅在测试或接其他兼容服务时修改，正式飞书使用默认值。完整配置见[身份配置](operations.md#public_url-与-identity)。

以下命令使用 `curl` 与 `jq`。先设置 `BASE` 为平台地址、`KEY` 为租户管理员 key、`TENANT=acme`，不要使用根凭据调用租户内接口。这里不展示真实 key；命令中的 `demo-*` 是本地模拟值，正式环境应替换。

```bash
curl -fsS "$BASE/v1/identity/feishu" \
  -H "Authorization: Bearer $KEY" | jq '{configured, callback_url, event_url}'
```

响应给出应填写到飞书后台的两个地址：

| 地址 | 用途 |
| --- | --- |
| `<public_url>/v1/auth/feishu/callback` | 在应用「安全设置」的重定向 URL 配置中登记，须与平台返回值一致 |
| `<public_url>/v1/identity/feishu/events/acme` | 「开发配置 → 事件与回调 → 事件配置」的请求地址，最后一段是租户标识 |

事件订阅选择「将事件发送至开发者服务器」。飞书说明要求公网 IPv4 可访问地址；每个应用仅一个事件请求地址。**先完成下文平台密钥与应用配置，再保存事件地址触发验证**，否则平台还不知道如何验签解密。[官方配置步骤](https://open.feishu.cn/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-/choose-a-subscription-mode/send-notifications-to-developers-server)

## 2. 权限、可用范围与通讯录范围

| 用途 | 最小配置 |
| --- | --- |
| 登录基本信息 | `authen/v1/user_info` 调用本身无需额外权限；姓名、头像、open_id、union_id、tenant_key 没有单独字段权限要求。邮箱、手机号、user_id 等另需字段权限，P1 登录不用它们 |
| 批量通讯录查询、订阅 `contact.user.deleted_v3` | 开通「获取通讯录基本信息」`contact:contact.base:readonly` |
| 对账判断暂停、离职、退出状态 | 另开「获取用户受雇信息」`contact:user.employee:readonly`，以取得 `status`；仅开基本信息权限不足以保证返回状态 |

以上依据分别是[获取用户信息](https://open.feishu.cn/document/server-docs/authentication-management/login-state-management/get)、[批量获取用户信息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/contact-v3/user/batch)、[员工离职事件](https://open.feishu.cn/document/server-docs/contact-v3/user/events/deleted)。飞书用户信息中的邮箱与手机号由管理员导入，官方不建议直接作为登录凭证；平台用 `union_id` 建外部身份映射。

两个范围要分别核对：

- **应用可用范围**决定哪些成员能使用应用。开发者在「应用发布 → 版本管理与发布 → 创建版本」里设置并提交审核；企业管理员也能在管理后台的「工作台 → 应用管理」直接调整。只加入应使用平台的成员。[官方说明](https://open.feishu.cn/document/home/introduction-to-scope-and-authorization/availability)
- **通讯录权限范围**决定应用身份能查询哪些成员。在「开发配置 → 权限管理」已开通通讯录权限的「可访问的数据范围」中配置，覆盖要对账的员工；默认可与应用可用范围一致，但不是同一个概念。开发者修改后需创建版本并经管理员审核生效。[官方说明](https://open.feishu.cn/document/ukTMukTMukTM/uETNz4SM1MjLxUzM/v3/guides/scope_authority)

通讯录范围外的用户可能不返回；**查不到不能证明离职**。平台只设 `unverified_since`，不会自动停用。返回员工但缺少 `status` 也不推断离职，应检查字段权限。离职事件订阅同样在发布审核后核对已生效。

## 3. 设置加密并写入平台

在「开发配置 → 事件与回调 → 加密策略」取得或设置 Encrypt Key、Verification Token。飞书本身允许明文事件；**xuanpu 要求 Encrypt Key，拒收明文事件与明文 URL 验证**。三个密钥经保管库写入，身份配置只保存引用名称。

下面的模拟值已实际运行；正式操作不要把密钥写进共享终端历史或版本库。

```bash
curl -fsS -X PUT "$BASE/v1/secrets/app-secret" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"value":"demo-secret"}'
# {"name":"app-secret"}
curl -fsS -X PUT "$BASE/v1/secrets/encrypt-key" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"value":"demo-encrypt"}'
# {"name":"encrypt-key"}
curl -fsS -X PUT "$BASE/v1/secrets/verification-token" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"value":"demo-token"}'
# {"name":"verification-token"}

curl -fsS -X PUT "$BASE/v1/identity/feishu" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"app_id":"demo-app","app_secret_ref":"app-secret","encrypt_key_ref":"encrypt-key","verification_token_ref":"verification-token","session_ttl_seconds":28800}' \
  | jq '{configured, session_ttl_seconds, feishu_tenant_key}'
# {"configured":true,"session_ttl_seconds":28800,"feishu_tenant_key":null}
```

`session_ttl_seconds` 是该租户的覆盖值，上例 8 小时；省略或传 `null` 恢复平台默认。此接口整体替换配置，但保留已经绑定的飞书企业，不能拿它改企业绑定。三个引用必须已存在。

现在保存飞书事件请求地址并订阅 `contact.user.deleted_v3`。官方说明：配置 Encrypt Key 后 URL 验证体为加密内容，解密后取 `challenge`，一秒内原样返回 `{"challenge": ...}`。[官方说明](https://open.feishu.cn/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-/choose-a-subscription-mode/send-notifications-to-developers-server)

**实现依据的区别**：平台兼容「加密但没有签名头」的 URL 验证，仅在成功解密、`type=url_verification`、Verification Token 相符时返回 challenge。这个分支依据官方 SDK [`lark-oapi` 的 `EventDispatcherHandler.do`](https://github.com/larksuite/oapi-sdk-python/blob/main/lark_oapi/event/dispatcher_handler.py)（先处理 URL 验证再验签）以及 [OpenClaw #58905](https://github.com/openclaw/openclaw/issues/58905) 的报文观察；**不是飞书官方说明原文，也不是本项目真实飞书实测结论**。普通事件必须验签，并核对 token、App ID、已绑定企业。未绑定企业时普通事件忽略，不用事件建立企业绑定。

## 4. 登录与首位管理员

让首位管理员本人打开 `<BASE>/console/?tenant=acme`，点击「用飞书登录」。也可直接访问 `/v1/auth/feishu/login?tenant=acme&return_to=/console/`。员工首次登录为 `tenant_member`；首次成功登录同时绑定飞书 `tenant_key`，以后其他企业的登录会被拒绝。

持租户管理员 key 的操作员核对姓名与用户 ID，设置 `ADMIN_USER` 为刚登录那个人的 `user_id`，再执行：

```bash
curl -fsS "$BASE/v1/users?status=active&limit=100" \
  -H "Authorization: Bearer $KEY"
curl -fsS -X PUT "$BASE/v1/users/$ADMIN_USER/role" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"role":"tenant_admin"}' | jq '{user_id,role}'
# {"user_id":"usr_a1149107687e03686f47fc03","role":"tenant_admin"}
```

列用户的完整响应保留 `display_name` 与 `user_id`，先核对两者，不能只靠列表顺序。`next_after` 不为空时继续带 `after=<next_after>` 翻页。末行是本地验收实际产生的 ID，**不要照抄**。标识格式为 `usr_` 加 24 位十六进制。让员工刷新控制台，管理员会看到「用户」页；后续可在页面上管理角色与状态。最后一名启用管理员不能被管理员接口降级或停用，但离职事件及对账可以停用他；兜底方法见[员工停用与离职](operations.md#员工停用与离职)。

浏览器登录使用 HttpOnly cookie，不向脚本暴露令牌。写请求带 `X-Requested-By: xuanpu`，若携带 `Origin` 则必须同源。Authorization 头优先于 cookie，所以切换到飞书登录时控制台会清除保存的 key。退出清当前登录会话与页面数据。

## 5. 对账与按员工接入

```bash
curl -fsS -X POST "$BASE/v1/identity/feishu/reconcile" \
  -H "Authorization: Bearer $KEY"
# {"checked":2,"disabled":0,"unverified":0}
```

这是两个模拟在职员工的实际结果。手工触发忽略每日周期，仍需认领租约；冲突返回 409。自动对账与登录清理共用每五分钟维护循环，成功后按配置周期再运行；扫描失败释放租约，不推进完成时间，下个维护周期重试。已停用的保持停用，查不到的人由管理员确认。[运维说明](operations.md#通讯录对账运维)

后台应用需要代表员工时，由管理员签发有 `act_as_users` 能力的 key，再附带 `X-Xuanpu-Act-As`。生效角色取 key 和员工中较低者；普通成员只看本人会话。Python SDK 用 `XuanpuClient(..., act_as_user=user_id)` 固定一个客户端的员工身份，不能因此提权。示例见[应用接入](integrating.md#代表员工调用)与 [SDK](../../packages/xuanpu-sdk-py/README.md)。

## 6. 本地验证边界与真实应用核对清单

本轮本地验证用仓库的 `tests/e2e/run_console.py --serve-only`：启动真实 CLI、内存控制面、本地状态面和模拟飞书；执行了本文配置、列用户、首位管理员、对账命令。另验证 SDK 真实网络请求、无签名加密 URL 验证返回 200、签名模拟离职事件停用员工，服务已经回收。没有调用真实模型，按用户用量返回 `{"usage":[]}`；带模型用量的 SDK 契约由自动测试验证。

真实 CLI 回调访问日志实测为（端口是本地临时端口）：

```text
INFO:     127.0.0.1:56609 - "GET /v1/auth/feishu/callback?… HTTP/1.1" 302 Found
```

反向代理仍需自己隐藏此路径查询串，平台无法代它处理日志。

下面五项**都尚未在真实飞书自建应用验证**；保存结果时记录应用版本、权限、范围与时间，不保存授权码、访问令牌、cookie 或密钥：

- [ ] 不额外申请登录字段权限时，实际返回姓名、头像、open_id、union_id、tenant_key 的情况。
- [ ] 不在应用可用范围的员工，错误出现在飞书授权页、换令牌还是获取用户信息阶段，是否为 20010。
- [ ] 员工离职后批量查询是否仍返回该员工；缩小通讯录范围时如何返回。
- [ ] 真实 URL 验证与离职事件报文结构、签名、加密、union_id 和状态字段是否与实现一致。
- [ ] 启用 Encrypt Key 后，实际 URL 验证是否确实不带签名头；普通离职事件仍必须带有效签名。

上述实机验收不等同于本地模拟通过；生产部署、真实飞书接通与云端发布需另行确认。
