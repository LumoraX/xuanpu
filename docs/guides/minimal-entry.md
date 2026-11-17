# 极简入口（R5-E）

给平台管理员：怎么为某个企业开启员工的极简入口，以及员工看到什么。设计见[极简入口架构](../architecture/minimal-entry.md)。已随 2026-11-15 生产发布部署，所有租户默认关闭（见[R5 生产发布记录](releases/2026-11-15-r5-production.md)）。

## 开启

极简入口依赖管家，管家依赖执行追踪，三者的租户名单要一致：

```yaml
execution_trace:
  enabled: true
  tenants: [acme]
butler:
  enabled: true
  tenants: [acme]
minimal_entry:
  enabled: true
  tenants: [acme]
```

`minimal_entry` 名单里的租户不在 `butler` 名单里时，服务启动时配置校验失败，报「minimal_entry 租户须同时在 butler 中开启」。

## 员工看到什么

- 打开 `/console/` 自动进入 `/console/entry.html`：上方是概览（今天、等我处理、进行中、未读通知），中间是对话，下面是提示和输入框，页脚是「更多 · 通知 · 退出」。
- 直接说需求。草稿、列表、提问与进展都以卡片出现在对话里，确认与取消仍由本人点击。
- 业务跟进、执行安排、企业连接、共享事项、工作交接与审批都在对话卡片里查看和处理；需要你选择或确认的（授权同意、审批决定、交接给谁和哪些资料）由你本人在卡片里点击。需要选文件、上传或下载核验成果，以及企业微信绑定（会跳到企业微信官方授权页），仍在「更多」完成。
- 「更多」就是原来的功能页（`/console/index.html?classic=1`），模型停用时也能用。

## 关闭或回退

把租户从 `minimal_entry.tenants` 去掉并重启，员工打开 `/console/` 回到原页面；不影响数据，概览接口 `GET /v1/dashboard` 仍可用。

## 本节的验证状态

上述行为由 `packages/xuanpu-console/tests/entry_behavior.cjs`（Node 行为测试）与 `packages/xuanpu-server/tests/dashboard` 覆盖。真实浏览器验收脚本是 `scripts/acceptance/r5_minimal_entry_browser.py`，本环境未执行，见 R5-E 计划。
