# 对话与功能兜底 Implementation Plan

> 执行方式：沿用已授权的本地工作树，逐项实现、验证后合并本地主线。

**Goal:** 默认对话，同时提供不依赖模型的提醒管理和通知查看。

**Architecture:** 现有原生提醒HTTP接口保持不变；一个提醒工作区按入口挂载，对话和功能页共用组件及数据。页面上下文代际隔离迟到请求。

**Tech Stack:** 原生HTML/CSS/JavaScript、Node受控脚本、Playwright、pytest。

## 任务1：回归覆盖

- [ ] `packages/xuanpu-console/tests/reminder_behavior.cjs`：新建表单无模型POST、失败重试同request_id、输入拒绝后可修正、切页后旧提交不干扰新表单。
- [ ] `tests/e2e/console.cjs`：会话默认无新建表单；功能入口手动CRUD，拦截assistant/route期间调用数为0；刷新及对话查看同记录；窄屏与退出清理。
- [ ] 先运行`uv run pytest packages/xuanpu-console/tests/test_reminder_behavior.py -q`确认新建表单缺失，再实现。

## 任务2：界面实现

- [ ] `static/index.html/style.css`：增加功能页及提醒/通知卡片，提醒工作区保留单一DOM实例；新建按钮只在功能模块展示。
- [ ] `static/app.js`：切页作废待返回的识别结果、清除提醒上下文，不停止后台调度。
- [ ] `static/reminders.js`：`showReminderManager(surface="conversation")`挂载工作区；`editReminder(item=null)`支持手动POST及现有PUT。创建正文含`request_id:crypto.randomUUID()`；不确定失败冻结正文重试，400/422解除冻结。
- [ ] `static/identity.js`：真实个人登录才展示功能入口，退出隐藏并清空。

## 任务3：交付验证

- [ ] 同步AGENTS产品约定、架构与使用手册，console升2026.10.5.7、更新uv.lock。
- [ ] 全量pytest、`uv run ruff check --select F,S108,I,TC packages scripts tests/e2e`、`uv run mypy packages/*/src`、`uv lock --check`、真实浏览器验收和console构建。
- [ ] 提交并fast-forward本地主线；刷新本地安装，核对8420资源哈希与旧会话历史，记录证据。不推送远端或发布云端。
