# 对话能力按需显示 Implementation Plan

> 使用现有隔离分支实施，用户已授权自主完成；独立审查后合本地主线并更新本地页面。

**Goal:** 日常界面保持对话，提醒操作仅在当前任务需要时出现。

**Architecture:** 仅调整console的展现生命周期；原提醒HTTP/SSE和持久调度保持不变。顶栏通用通知入口与对话内能力清单各有独立显隐状态。

**Tech Stack:** 原生HTML/CSS/JS、现有Python测试与Playwright验收。

- [x] `index.html/style.css`：移除aside中的提醒模块和新建入口；把管理清单放到chat-pane，默认hidden；顶栏通知按钮+独立可关闭通知面板。
- [x] `reminders.js/app.js`：start只启动后台读取；显式`showReminderManager`在manage路由调用，close清除并阻止旧请求重开；新会话调用`dismissReminderContext`；通知按钮未读提示、安全文本、显式开关。去掉空白手工新建表单入口，只保留已有提醒编辑。
- [x] 行为测试先复现默认面板自动打开，再覆盖草稿/后台读取不展开、管理显式打开/关闭、通知未读与退出清理、旧异步结果不重开。
- [x] `tests/e2e`从自然语言创建测试数据，经“查看我的提醒”进入修改取消；验证通知入口与窄屏、新对话收起，保留身份/XSS回归。
- [ ] 更新活文档及console版本，独立规格/代码审查，全量pytest、Ruff、mypy、锁与构建；合main、更新本地静态资源，验证新资源和旧数据保留。

## 实施验收

- 规格及最终代码独立审查通过；修复取消请求晚返回污染新对话的问题。
- 动态行为测试先复现登录时自动展开，再覆盖显式管理/通知入口、跨对话草稿与取消响应隔离；console专项6 passed。
- 浏览器验收通过：默认隐藏/无手工新建，自然语言创建与确认、到期未读提示、显式打开通知/刷新恢复/标读、对话清单编辑取消、继续普通业务与新对话收起，375px无溢出。日志`/tmp/xuanpu-conversation-final-browser.log`，测试服务已结束。
- Ruff通过，mypy 105源文件通过，锁与diff检查通过；console 2026.10.5.6 wheel及sdist构建成功（`/tmp/xuanpu-conversation-final-dist`）。
- 本次产品代码只改控制台静态资源；平台按请求读取资源文件，主线更新和环境同步后直接验证正在运行服务返回的新资源，无需为界面更新中断后台提醒。

- 最终全量 `uv run pytest -q`：1294 passed、1 skipped，84.98秒；唯一跳过是Postgres专属用例的内存参数，真实数据库分支通过。日志`/tmp/xuanpu-conversation-final-tests.log`。
