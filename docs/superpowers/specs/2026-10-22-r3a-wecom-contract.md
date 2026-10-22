# R3-A 企业微信只读供应商契约

核对日期：2026-10-22。范围：企业内部自建应用。玄圃拥有稳定员工身份、同意记录、业务规则和审计；企微只是可替换的登录与只读来源。

本轮已直接读取下列官方页面正文，保留标题、更新日期及接口核定结果。通用接口文档可读不代表某个真实应用已获权限：CorpID、AgentID、Secret、可信IP/域名及两名测试员工仍需由企业管理员准备。实际企业后台与真实员工验收当前为 **unverified/not_checked**，安装默认disabled。

| 操作 | 已核定协议 | 来源与页面更新日期 | 真实应用能力 |
| --- | --- | --- | --- |
| 应用token | GET `/cgi-bin/gettoken`；corpid+corpsecret；按应用缓存，按expires_in判断到期 | [获取access_token](https://developer.work.weixin.qq.com/document/path/91039)，2024-03-26 | unverified |
| 客户端授权链接 | `open.weixin.qq.com/connect/oauth2/authorize`，snsapi_base，CorpID/AgentID及固定回调 | [构造网页授权链接](https://developer.work.weixin.qq.com/document/path/91022)，2024-07-30 | unverified |
| code识别成员 | GET `/cgi-bin/auth/getuserinfo`；code一次使用、5分钟；非员工可能只有openid | [获取访问用户身份](https://developer.work.weixin.qq.com/document/path/91023)，2026-04-22 | unverified |
| 本人基础资料 | GET `/cgi-bin/user/get`，平台绑定的userid；应用必须拥有成员查看权限 | [读取成员](https://developer.work.weixin.qq.com/document/path/90196)，2025-03-06 | unverified |
| 管理员部门预览 | GET `/cgi-bin/department/list`，只代表应用可见范围；限制100部门 | [获取部门列表](https://developer.work.weixin.qq.com/document/path/90208) | unverified |
| 部门成员预览 | GET `/cgi-bin/user/simplelist`，逐个可见部门、fetch_child=0；限制每部门1000成员 | [获取部门成员](https://developer.work.weixin.qq.com/document/path/90200) | unverified |
| 桌面扫码 | 本轮官方旧入口仅返回页面框架，未取得可核定正文 | 不根据第三方博客接入旧qrConnect | unsupported in this release |
| 消息发送、组织写入、敏感资料、会话存档、完整文档 | 不接入；userid/account不是个人refresh_token | 不注册操作或写API | unsupported |

## 身份与范围

`auth/getuserinfo`可以识别不在应用可见范围内的企业成员，不能把它当作可读资料授权。登录/绑定还要实际读取该成员基础资料并验证userid一致。互联、上下游等含`CorpId/userid`格式不在本期范围；不把外部联系人openid变成员工。

本期仅接受管理员预先映射已有usr，或已登录员工本人完成账号链接；不按姓名或邮箱猜测，不JIT建管理员。旧飞书配置模型保持飞书专用，企微配置放独立installation。供应商缺少姓名、岗位、部门时明确缺失；本地显示名不在企微登录时被覆盖。

## 数据、错误和凭据

白名单只有本人userid/name/position/department，以及管理员可见部门和基础成员列表。手机、邮箱、头像、user_ticket、user_doc_ticket、extattr、原始errmsg和token均不返回。正常token有效期常见7200秒，但代码读取真实expires_in，不硬编码2小时。40014/42001仅对已核定GET读取最多刷新重试一次；code交换不重试。

固定HTTPS主机、禁止重定向、禁用环境代理、响应大小1MiB、请求15秒。HTTP200仍检查errcode；未返回成功errcode视为失败。目录逐部门采集，无全企业完整性保证，complete=false，不能据缺失推断离职或执行停用。

数据库内部密文与普通`/v1/secrets`分表；复用现有AES-GCM及主密钥轮换，AAD绑定租户/安装/应用主体/种类/密钥修订。HTTP日志隐去认证URL，OAuth回调需反向代理同样关闭query日志；页面禁止缓存和referrer。未具备可信域名/IP、应用范围或实际字段时，不扩大权限来凑成功。

## 测试和真实验收边界

本机HTTP模拟、脚本模型、合成Cookie、PG双后端与受控重建仅是代码/协议证据。真实企微客户端授权、范围内员工成功、范围外拒绝、真实资料字段核对、应用范围缩小及令牌提前失效需另留真实证据。缺少这些条件不得把R3-A标为全部验收完成。
