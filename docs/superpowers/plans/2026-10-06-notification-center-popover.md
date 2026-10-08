# 通知中心浮层实现计划

**目标：** 通知入口位于顶栏右侧，通知中心从入口下方浮起，开关不改变当前页面布局、滚动、输入或业务草稿。

**授权与选择：** 用户已确认可随时收起的浮层交互，并持续授权直接完成仓库修改及本地验收。采用与入口关联的非模态浮层；相较独立页面无需离开对话，相较整高抽屉占用更少空间。无需新增组件库。

**设计：** 顶栏按钮移入用户操作区；通知中心使用独立固定定位面板，宽度最多400px并约束在视口内，长列表内部滚动。打开时清除遮挡的提示卡、聚焦面板；关闭按钮和Esc将焦点还给入口，外部点击/移出焦点关闭且不抢外部焦点。再次点击切换关闭。窗口缩放/页面滚动重新约束定位，不调用scrollIntoView。顶栏、功能页、提示卡均复用同一打开流程；身份退出关闭并清除状态，迟到请求不重新打开。

## 执行与验收

- [x] 复现：旧版通知中心作为header之后的普通section，展开会把会话整体下推；提示卡入口还主动滚动页面。
- [x] `tests/e2e/notification-center.cjs`、`console.cjs`：补入真实浏览器回归，比较打开前后主内容坐标和滚动值、按钮锚定与窄屏边界；验证关闭方式、焦点、未发送输入保留、功能入口和现有提示卡入口。旧版应首先报布局位移失败。
- [x] `static/index.html`、`style.css`：入口移至用户区；通知中心增加非模态dialog语义与标题，固定浮层和内部滚动。
- [x] `static/notification-center.js`：集中位置计算、开关、三入口、关闭和焦点逻辑；`reminders.js`与`notification-alerts.js`保留数据及实时提示，删除重复开关和主动页面滚动。
- [x] 同步脚本加载与测试DOM替身；执行控制台测试、完整浏览器旅程，人工检查桌面/375px深色截图；独立设计与代码审查。
- [x] 更新架构、手册及控制台版本，运行全量pytest、Ruff、mypy，构建、提交并合入本地main；核对已运行服务静态资源并在真实登录页面验证。

业务数据、标读接口、SSE和提示卡去重维持既有契约；关闭面板不等于标为已读。真实员工环境只查看/开关页面，业务变更测试在隔离服务中运行。

设计独立审查补充并采纳：中心展开期间继续推进通知水位和更新列表，但不叠加提示卡；Esc只关闭最上层通知浮层；关闭按钮与Esc使用preventScroll返回实际发起入口，提示卡被移除时返回顶栏通知按钮。

## 验证记录

- 修改前真实浏览器失败：打开通知使工作区纵坐标从113变为290；修改后同一回归通过。
- `uv run pytest packages/xuanpu-console/tests -q`：6 passed；中心可见时新通知继续更新列表与水位、不叠卡、关闭后不补弹已有通知。
- 完整真实浏览器控制台/个人工作站旅程通过；新增覆盖页面/滚动不变、开关、外部点击、Tab离开、Esc焦点返回、后台文件窗口保留、已滚动功能入口、长列表内部滚动、桌面与375px深色。证据 `/tmp/xuanpu-notification-center-browser.log` 及同名前缀目录截图。
- 独立设计与实现审查无阻塞问题；`uv run ruff check --select F,S108,I,TC packages scripts tests/e2e`：All checks passed；`uv run mypy packages/*/src`：Success: no issues found in 116 source files。
- `uv build --package xuanpu-console --out-dir /tmp/xuanpu-notification-center-dist` 成功生成2026.10.6.1源码包和wheel，核对wheel包含新脚本且内容与源码一致。
- `uv run pytest`：1395 passed, 1 skipped in 101.17s；所有源码门禁通过。
- 已将实现 `6618984` 合入本地main，控制台升级2026.10.6.1；运行中HTML/CSS及三个通知脚本与main字节一致。真实员工登录页刷新后验证右上浮层、再次点击收起及工作区不移动；截图 `/tmp/xuanpu-notification-center-local.jpg`。本轮没有重启服务、修改数据库或发送测试飞书消息，也未进行远端推送/云端发布。
