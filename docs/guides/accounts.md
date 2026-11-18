# 玄圃账号：开通、设口令与登录

> 给平台管理员与租户管理员。设计见[账号体系](../architecture/accounts.md)，接口细节见 [HTTP 接口](../architecture/http-api.md#玄圃账号)。

玄圃自己有账号。不接飞书、企业微信也能用：管理员开通账号，员工通过设置链接设好口令，之后用用户名与口令登录。接了飞书的租户照旧可以用飞书登录，管理员也可以给飞书来的员工补发设置链接，作为备用的登录方式。

## 前提

服务配置里必须有 `public_url`：登录 cookie 与设置链接都依赖它。本地用 HTTP 试用时这样配（下文的输出就是用这份配置在本机跑出来的）：

```yaml
public_url: http://127.0.0.1:8420
identity:
  cookie_secure: false     # 本地 HTTP；正式部署走 HTTPS，保持默认 true
  default_tenant: acme     # 登录时省略 tenant 就用它
```

确认口令登录已可用：

```bash
curl -s 'http://127.0.0.1:8420/v1/auth/methods?tenant=acme'
# {"tenant":"acme","password":true,"feishu":false}
```

## 开通第一名管理员

平台管理员用根凭据建租户，签发一把临时的租户管理员 API key（见[快速上手](quickstart.md#3-开通租户)），再用它开通管理员账号：

```bash
curl -s -X POST http://127.0.0.1:8420/v1/users \
     -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
     -d '{"username":"Admin.Wang","display_name":"王管理","role":"tenant_admin"}'
# {"user":{"user_id":"usr_6efec6eaa26b352bb5a38ff8","display_name":"王管理","role":"tenant_admin","status":"active",…},
#  "setup_link":{"url":"http://127.0.0.1:8420/console/#password_setup=HPV2…","user_id":"usr_6efec6eaa26b352bb5a38ff8",
#                "username":"admin.wang","purpose":"setup","expires_at":1795531345.2266161}}
```

用户名统一转成小写（`admin.wang`）。`setup_link.url` 只返回这一次，7 天内有效，经企业自己的渠道交给本人。本人在浏览器里打开它，输入两次新口令（至少 10 个字符）就设好了，同时自动登录。

管理员能登录之后吊销这把临时 key。在已登录的控制台里操作，或者带上管理员的登录 cookie：

```bash
curl -s -b cookies.txt -X DELETE http://127.0.0.1:8420/v1/keys/f0a73e0fc157 -H 'X-Requested-By: xuanpu'
# {"revoked":true}
```

## 开通员工、重置口令

管理员在控制台「员工」页填用户名、显示名、角色，点「开通账号」；页面上显示设置链接，转交给员工即可。也可以调接口，`POST /v1/users` 与上面相同。

每行员工都有「设置链接」按钮，对应 `POST /v1/users/{user_id}/password-link`：

- 员工还没有用户名（比如飞书来的）：页面先让你指定用户名，签发的是开通链接；
- 员工已经设过口令：签发的是重置链接，24 小时有效。员工设好新口令后，他已有的全部登录都会退出；
- 每名员工同时只有一条有效链接，新签发的会让旧链接失效；已停用的员工不能签发。

## 员工登录与改口令

控制台登录栏输入用户名与口令。用 `?tenant=<租户>` 打开页面可以指定租户，省略时用 `identity.default_tenant`。用接口登录要带 `X-Requested-By: xuanpu`：

```bash
curl -s -c cookies.txt -X POST http://127.0.0.1:8420/v1/auth/password/login \
     -H 'X-Requested-By: xuanpu' -H 'Content-Type: application/json' \
     -d '{"tenant":"acme","username":"ADMIN.WANG","password":"a long admin passphrase"}'
# {"tenant_id":"acme","user_id":"usr_6efec6eaa26b352bb5a38ff8"}
```

无论是用户名不存在、口令错，还是账号被锁、已停用，回复都一样：

```text
HTTP/1.1 401 Unauthorized
{"error":{"code":"unauthenticated","message":"用户名或口令不正确"}}
```

同一账号连续输错 5 次会锁 15 分钟，锁定期间输对口令也进不去。管理员可以在审计里按 `login_failed` 查看原因（`bad_password`、`locked_now`、`locked`、`user_disabled`）。

缺少 `X-Requested-By` 头的登录请求会被拒，防止别的网站替员工登录：

```text
{"error":{"code":"csrf_rejected","message":"用登录 cookie 发起的写请求必须带 X-Requested-By: xuanpu 头"}}
```

已登录的员工点「改口令」，输入当前口令与新口令；其他设备上的登录随之退出。接口是 `POST /v1/me/password`，只接受浏览器登录，API key 不能调用。

## 排障

| 现象 | 原因与处理 |
| --- | --- |
| `GET /v1/auth/methods` 里 `password` 为 `false`，开通返回 400 | 没有配置 `public_url` |
| 设置链接打开后提示无效或已过期 | 已过期、已用过、被新链接替换，或员工已停用；管理员重新签发 |
| 一直「用户名或口令不正确」 | 查 `login_failed` 审计的 `reason`；锁定的等 15 分钟，忘记口令的让管理员签发重置链接 |
| 登录返回 503 | 同一时刻登录请求过多，口令计算在排队；稍后重试 |
