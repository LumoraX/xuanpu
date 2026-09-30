# xuanpu-console

xuanpu 控制台：给业务方和租户管理员用的网页界面。纯静态页面（一个 HTML、一个脚本、一个样式表，没有构建步骤、没有第三方依赖），直接调用平台的 HTTP 接口。

平台服务启动时自动挂载在 `/console/`（`xuanpu-cli` 已经带上了本包）。不想对外提供的话，在反向代理上屏蔽这个路径。

## 使用

打开 `http://<平台地址>/console/`，填 API key，点「连接」。平台地址留空表示当前站点。

| 页签 | 能做什么 | 需要的角色 |
| --- | --- | --- |
| 会话 | 新建会话、流式对话、处理审批（批准 / 拒绝）、关闭会话、查看已关闭的会话 | 对话与审批：租户成员。重新打开一个会话时要从审计里加载历史，这一步需要租户管理员 |
| 业务包 | 上传归档发布、切换生效版本、对任意版本跑评测并看报告 | 租户管理员 |
| 审计 | 按会话、事件类型、来源筛选，逐页查看 | 租户管理员 |
| 用量 | 按日期范围查看每天的轮数与 token | 租户管理员 |

控制台没有的功能：租户管理（平台管理员的事，用 HTTP 接口）、API key 与密钥管理。

## 凭据存在哪

- API key 存在浏览器的 `sessionStorage` 里：关掉标签页就没了，不会留在磁盘上。
- 平台地址存在 `localStorage` 里，下次打开还在。

控制台把 API key 直接放在浏览器里，所以它是给内部人员用的管理界面，不是给终端用户的聊天窗口。面向终端用户的界面请自己做，经你的后端转发（见[应用接入](../../docs/guides/integrating.md)）。

## 作为参照实现

`static/app.js` 里有一份不依赖任何库、用 `fetch` 读事件流的实现（`streamTurn`），可以在写自己的前端时参照。

## 挂到别的 ASGI 应用上

```python
from xuanpu_console import static_dir
from xuanpu_server.api import create_app

app = create_app(platform, console_dir=static_dir())
```
