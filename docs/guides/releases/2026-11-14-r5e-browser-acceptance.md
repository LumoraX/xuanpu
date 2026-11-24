# R5-E 极简入口浏览器验收（2026-11-14）

环境：macOS arm64，Google Chrome 157.0.8103.79（无头），Node v24.22.3，Python 3.12；本机 loopback、内存平台，模型工厂一律拒绝（模型屏蔽），两名测试员工；不连任何外部服务，不用真实身份或模型。

命令：

```bash
.venv/bin/python scripts/acceptance/r5_minimal_entry_browser.py \
  --chrome "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --out <临时目录>
```

结果（原始证据见 [2026-11-14-r5e-browser-evidence.json](2026-11-14-r5e-browser-evidence.json)）：

| 检查 | 结果 |
| --- | --- |
| 员工打开 `/console/` 后进入 `entry.html`，首屏四样（概览、对话、提示、输入） | 通过（甲、乙） |
| 概览四块齐（今天、等我处理、进行中、未读通知），模型屏蔽下照常读出 | 通过；模型调用尝试 0 次 |
| 提示在模型屏蔽下渲染 | 通过 |
| 「更多」：`index.html?classic=1` 停在功能页，已登录、功能入口在 | 通过 |
| 两员工隔离：甲的提醒标题只出现在甲的页面 | 通过 |
| 375px（经 DevTools 协议模拟移动设备）：`scrollWidth` 375、发送按钮右缘 359 | 通过，无横向溢出 |
| 默认页加载：6 个脚本共 29489 字节，样式 3791 字节（未压缩）。E1 补齐后为 8 个脚本共 51492 字节（字节数按文件实测，浏览器未重跑） | 记录 |

未覆盖：

- 「各模块在对话内完成一次查看与一次确认」需要管家给出草稿与列表，模型屏蔽时管家不可用；由 Node 行为测试 `tests/web/entry_behavior.cjs` 覆盖确认卡、列表卡（带版本号）、进展卡与刷新恢复，真实浏览器加真实模型的这一项待有模型凭据的环境补跑。
- 首次打开 `/console/` 时先加载旧页（含其全部脚本）再跳到新页；直接打开 `/console/entry.html` 不加载旧页脚本。上表的脚本数与体积是新页本身。
