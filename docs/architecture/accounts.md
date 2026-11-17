# 账号体系（活文档）

> 为什么这么定见 [ADR 0019](../decisions/0019-native-accounts.md)；登录会话与 cookie 的通用规则见 [HTTP 接口 · 浏览器登录](http-api.md#浏览器登录)；怎么用见[玄圃账号](../guides/accounts.md)。

## 现状

玄圃账号就是平台用户（`UserRecord`，`usr_…`，按租户）。会话、审计、用量、组织、停用流水线都只认它。挂在账号上的登录方式：

| 登录方式 | 怎么来的 | 能否自动建账号 |
| --- | --- | --- |
| 用户名口令（本文） | 管理员开通或补发设置链接，员工自己设口令 | 否：账号由管理员开通 |
| 飞书 | `GET /v1/auth/feishu/login`（ADR 0010） | 是：飞书应用可用范围内首次登录建普通成员 |
| 企业微信 | 企业连接的 OAuth（[企业连接](connections.md)） | 否：只认已映射的员工 |

各种登录方式成功后签发的是同一种服务端登录会话；停用员工、退出、过期规则相同。尚未实现：二次验证、邮箱与手机号、Google / GitHub / 通用 OIDC 登录、给桌面端与命令行签发令牌。

## 数据

迁移 `0026_local_credential` 建两张表，都以 `(tenant_id, user_id)` 外键指向员工：

| 表 | 内容 | 约束 |
| --- | --- | --- |
| `local_credential` | 规整后的用户名、argon2id 摘要（为空表示待设）、开通与设口令时间、连续失败次数、锁定到期 | 每名员工一条；租户内用户名唯一 |
| `account_token` | 设置链接令牌的 SHA-256、用途 `setup`/`reset`、签发与到期时间、签发者 | 每名员工一条，新签发替换旧的；兑现即删 |

存储经 `IdentityStore.credentials`（`identity.credential_store.CredentialStore`），内存与 Postgres 两个实现对同一套契约测试负责。按令牌查链接与周期清理是仅有的两处不带租户的操作，与登录会话同理。过期链接由身份域每 5 分钟一次的清理删除。

## 规则

**用户名**：去首尾空白、转小写后比较；2–64 个字符，只含 ASCII 字母、数字与 `. _ -`，以字母或数字开头。只用 ASCII，避免同形字符冒充。开通后暂不支持改名。

**口令**：10–128 个字符，不能与用户名相同，不强制字符种类。摘要用 `argon2-cffi` 默认参数的 argon2id；登录时参数过时则重算写回。计算在线程池里进行，全进程同时最多 4 个，排队超过 10 秒返回 `503 runtime_unavailable`。

**设置链接**：`<public_url><identity.login_landing_path>#password_setup=<令牌>`，令牌 32 字节随机数。开通链接 7 天、重置链接 24 小时有效。员工已停用时不能签发，已签发的也兑现不了。兑现时先查链接、算摘要，再在一个原子操作里删链接并写口令，同一条链接并发兑现至多一个成功。兑现、改口令都会作废这名员工的全部登录会话，再签发一个新的。

**登录失败**：用户名不存在、没设口令、口令错、锁定中、员工或租户停用、租户不存在，都返回同一个 `401 unauthenticated`「用户名或口令不正确」。不存在的用户名照样算一次摘要；锁定期内不核对真口令。同一账号连续 5 次口令错锁 15 分钟，锁定后计数清零。

**防跨站**：口令登录、查看与兑现设置链接不需要凭据，但和 cookie 写请求一样，要求 `X-Requested-By: xuanpu`，带了 `Origin` 的必须等于 `public_url`；否则 `403 csrf_rejected`。没有配置 `public_url` 时这几个接口与开通接口都返回 `400`，`GET /v1/auth/methods` 的 `password` 为 `false`。

**第一名管理员**：平台管理员签发一把租户管理员 API key，用它调 `POST /v1/users` 开通一名 `tenant_admin`；管理员设好口令后吊销这把 key。整个过程没有专门的后门接口。

## 审计与指标

| 审计 `kind` | 何时 | `data` |
| --- | --- | --- |
| `user_created` | 开通账号 | `provider: password`、`username`、`display_name`、`role` |
| `username_assigned` | 给已有员工（如飞书来的）开通用户名 | `username` |
| `password_link_issued` | 签发设置链接 | `purpose`、`expires_at` |
| `password_set` | 兑现设置链接 | `purpose` |
| `password_changed` | 员工改口令 | 无 |
| `login_succeeded` | 登录成功（含兑现与改口令后的重新登录） | `provider: password` |
| `login_failed` | 认出了员工的失败（匿名失败不写） | `provider: password`、`reason`：`bad_password`、`locked_now`、`locked`、`user_disabled` |

指标 `xuanpu_password_logins_total{result}`：`succeeded`、`rejected`、`locked`（锁定期内被拒）。

## 控制台

- 登录栏：用户名口令表单与「用飞书登录」并列，只在同源页面显示；`?tenant=` 决定登录哪个租户，省略时用 `identity.default_tenant`。极简入口页提供「用户名登录」链接，回到旧页登录后按 `/v1/me` 的 `entry` 跳回。
- 打开设置链接：脚本先从地址栏抹掉令牌，再查看链接、显示用户名，输入两次新口令后设置并登录。
- 员工页（管理员）：开通账号表单，每行「设置链接」。没有用户名的员工会先提示输入用户名。链接只在管理员页面上显示一次。
- 已登录员工可以「改口令」。
