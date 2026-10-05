# 网页悬浮通知 Implementation Plan

**Goal:** 员工在网页内明显看到新通知，中心继续作为持久记录入口。

**Architecture:** 独立静态脚本管理悬浮卡片及显示去重；现有SSE与快照补读调用共同入口，不改变后端调度或飞书投递。

**Tech Stack:** 原生JavaScript/CSS、Node脚本、Playwright、pytest。

- [x] 先扩展`packages/xuanpu-console/tests/reminder_behavior.cjs`验证新通知弹卡、关闭/标读语义、去重、注销和请求乱序；运行控制台专项确认缺少提示的失败。
- [x] 新增`static/notification-alerts.js`，提供`resetNotificationAlerts()`和`observeNotificationAlerts(items,{snapshot})`。在`index.html`加入固定位置区域及脚本，在`style.css`实现卡片、暗色、窄屏与减少动画偏好。
- [x] `reminders.js`接入SSE即时弹卡、初次快照汇总、补查、标读刷新及注销清理；快照请求序号防旧结果覆盖已读。
- [x] 浏览器验证真实定时生成的站内通知弹卡、不抢焦点、按钮操作、历史汇总、窄屏暗色；现有无模型手动兜底保持可用。
- [x] 更新架构与本地手册，console升2026.10.5.8；全量pytest/Ruff/mypy/lock、构建、独立审查后合并本地主线，验证8420资源与历史保存。


## 验收证据

- 控制台专项6项通过；受控脚本先复现“新站内通知没有悬浮卡片”，再验证实时卡片、初次历史汇总、重复补查不重弹、关闭不标读、标读失败保留/成功移除、卡片上限和注销清空、旧快照不恢复未读。
- `uv run pytest`：1298 passed、1 skipped，77.97秒；日志`/tmp/xuanpu-popup-release-tests.log`。
- `uv run ruff check --select F,S108,I,TC packages scripts tests/e2e`：All checks passed；`uv run mypy packages/*/src`：105 source files无错误；`uv lock --check`和`git diff --check`通过。
- `tests/e2e/run_console.py`真实CLI与Chromium验收通过，使用模拟飞书/模型和隔离员工。真实3秒/5秒后台到期生成站内消息，网页弹卡、输入焦点不变、关闭不标读、刷新历史汇总、查看中心、卡片标读均通过；原有业务、功能兜底与身份回归通过。证据`/tmp/xuanpu-popup-release-browser/`、日志`/tmp/xuanpu-popup-release-browser.log`。没有触发真实飞书员工消息。
- 暗色及375px截图已检查，卡片在视口内且无横向溢出。首轮浏览器脚本错把现有创建接口200预期成201，按真实接口契约修正断言后完整重跑通过，无后端改动。
- `uv build --package xuanpu-console --out-dir /tmp/xuanpu-popup-release-dist`成功构建2026.10.5.8的wheel/sdist，六个静态资源与源码逐字节匹配。

- 独立审查发现的启动弱网历史重播、关闭汇总被迟到快照复活两项P2均已先红后绿修复：初次快照状态与显示水位独立，汇总展示标记在本次登录期间保留。新增真实脚本回归覆盖首次GET失败/SSE先到/迟到快照/关闭/新通知。最终复核无剩余P1/P2。
- 常驻独立读屏播报区域不受卡片显隐影响，退出清空；未进行真实读屏软件人工验收，不把DOM检查当成读屏实测。


## 本地部署

- 实现提交`3dc8a65`已fast-forward合并本地主线；`uv sync --all-packages --all-extras`安装console 2026.10.5.8。
- 8420实际返回的六个静态资源（含新增notification-alerts.js）均与主线文件哈希一致；路径指向主线`xuanpu/packages/xuanpu-console/src/xuanpu_console/static`。
- 主线控制台专项6 passed；platform和tickets均运行中；既有会话`ses_c004382ccd2a3c51ec2292eb`的14条历史保留。
- 静态资源更新，无需重启后台；未修改用户私有JSON、未创建真实员工测试通知、未推送远端或发布云端。已打开网页刷新后生效。
