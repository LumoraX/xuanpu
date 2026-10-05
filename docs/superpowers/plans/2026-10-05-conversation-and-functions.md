# 对话与功能兜底 Implementation Plan

> 执行方式：沿用已授权的本地工作树，逐项实现、验证后合并本地主线。

**Goal:** 默认对话，同时提供不依赖模型的提醒管理和通知查看。

**Architecture:** 现有原生提醒HTTP接口保持不变；一个提醒工作区按入口挂载，对话和功能页共用组件及数据。页面上下文代际隔离迟到请求。

**Tech Stack:** 原生HTML/CSS/JavaScript、Node受控脚本、Playwright、pytest。

## 任务1：回归覆盖

- [x] `packages/xuanpu-console/tests/reminder_behavior.cjs`：新建表单无模型POST、失败重试同request_id、输入拒绝后可修正、切页后旧提交不干扰新表单。
- [x] `tests/e2e/console.cjs`：会话默认无新建表单；功能入口手动CRUD，拦截assistant/route期间调用数为0；刷新及对话查看同记录；窄屏与退出清理。
- [x] 先运行`uv run pytest packages/xuanpu-console/tests/test_reminder_behavior.py -q`确认新建表单缺失，再实现。

## 任务2：界面实现

- [x] `static/index.html/style.css`：增加功能页及提醒/通知卡片，提醒工作区保留单一DOM实例；新建按钮只在功能模块展示。
- [x] `static/app.js`：切页作废待返回的识别结果、清除提醒上下文，不停止后台调度。
- [x] `static/reminders.js`：`showReminderManager(surface="conversation")`挂载工作区；`editReminder(item=null)`支持手动POST及现有PUT。创建正文含`request_id:crypto.randomUUID()`；不确定失败冻结正文重试，400/422解除冻结。
- [x] `static/identity.js`：真实个人登录才展示功能入口，退出隐藏并清空。

## 任务3：交付验证

- [x] 同步AGENTS产品约定、架构与使用手册，console升2026.10.5.7、更新uv.lock。
- [x] 全量pytest、`uv run ruff check --select F,S108,I,TC packages scripts tests/e2e`、`uv run mypy packages/*/src`、`uv lock --check`、真实浏览器验收和console构建。
- [x] 提交并fast-forward本地主线；刷新本地安装，核对8420资源哈希与旧会话历史，记录证据。不推送远端或发布云端。


## 实际验证证据

- 先确认手动表单缺失的红测试，再实现并通过控制台6项测试。切页的会话列表/历史/审批三个等待点、登录初始化入口提前开放也分别复现后修复；两个Node脚本必须输出结束标志，避免悬空Promise静默退出造成假通过。
- `uv run pytest`：1296 passed、1 skipped，85.04秒，日志`/tmp/xuanpu-functions-final-tests.log`。
- `uv run ruff check --select F,S108,I,TC packages scripts tests/e2e`：All checks passed；`uv run mypy packages/*/src`：105 source files无错误；`uv lock --check`、`git diff --check`通过。
- 真实CLI + Chromium + 模拟飞书/模型浏览器验收通过：默认会话，手动提醒CRUD在assistant接口被屏蔽时零调用，刷新持久，对话与模块同数据，切页隔离迟到响应，通知已读、退出清理、本人隔离、375px无溢出。证据`/tmp/xuanpu-functions-final-browser/result.json`及截图；日志`/tmp/xuanpu-functions-final-browser.log`。没有给真实员工发送测试消息。
- `uv build --package xuanpu-console --out-dir /tmp/xuanpu-functions-dist`：2026.10.5.7 wheel与sdist成功，wheel内五个静态资源逐字节匹配当前源码。
- 独立审查指出的两个P2时序问题均已修复并复核，无剩余P1/P2。代码只改变控制台入口与操作兜底，没有修改提醒数据库或调度。

## 本地部署

- 实现提交`e52d78b`已fast-forward合并本地main；`uv sync --all-packages --all-extras`将主线console更新至2026.10.5.7。未推送远端、未发布云端。
- 8420实际返回的index.html、app.js、reminders.js、identity.js、style.css逐一匹配主线资源哈希；安装路径指向`<xuanpu 仓库>/packages/xuanpu-console/src/xuanpu_console/static`。
- 主线控制台专项测试6 passed；platform与tickets均运行中。静态资源即时读取，本次没有重启后台或更改私有配置。
- 更新前后已有验收会话`ses_c004382ccd2a3c51ec2292eb`均保留14条历史。实际用户刷新控制台后使用新版；浏览器交互验收使用隔离模拟员工，未代替真实飞书登录重新授权。
- 产品约定已写入AGENTS：对话为主入口，功能模块可选但必要兜底始终保留；当前已提供提醒与通知，不把未来报销、签收等领域称为已完成。
