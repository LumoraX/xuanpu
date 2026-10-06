# 桌面客户端安装与构建

## 员工安装

本机测试版面向 Apple Silicon Mac，要求 macOS 12 或更高版本。打开 `玄圃_2026.10.6_aarch64.dmg`，将“玄圃”拖入 Applications，再从应用程序中打开。

客户端会自动连接预置的企业服务，首次使用正常登录即可，不需要填写地址、模型或应用密钥。桌面版与原浏览器的登录状态独立。

- 文件上传沿用系统选择文件窗口。
- 文件下载保存到 Mac 的“下载”文件夹，文件名带唯一标识，防止覆盖旧文件；菜单“工作站 → 打开下载文件夹”可以直接打开。
- “工作站 → 刷新”（⌘R）重新加载页面，“重新连接”重新检查服务。
- 服务无法连接时使用“重新连接”按钮；仍失败则联系企业管理员。

当前这份本机测试包连接 `http://127.0.0.1:8420/console/`，需要本机玄圃后台保持运行。退出桌面应用不会停止后台的提醒调度；退出后没有桌面系统通知，外部通知仍按后台配置发送。

## 管理员发布配置

修改 `packages/xuanpu-desktop/src-tauri/desktop.json` 后重新构建应用。不要让员工手工修改已经签名的应用内容。

| 字段 | 说明 |
| --- | --- |
| `service_url` | 完整工作站地址；企业部署必须使用 HTTPS，本机调试允许回环 HTTP |
| `auth_origins` | 登录供应商的精确来源白名单，包含协议、域名与端口，只能使用根路径 |

`service_url` 的来源必须与后台 `public_url` 一致，否则 OAuth 回调无法使用客户端发起登录时的 Cookie。飞书开发者后台的重定向地址仍是后台 `/v1/auth/feishu/callback`；桌面应用不使用新的深链回调。

该文件只放公开部署信息。数据库、模型、飞书 App Secret 继续保留在服务端。本机版采用 ad-hoc 签名，不包含 Developer ID 或 Apple 公证；公司跨设备分发前应使用企业开发者签名并完成公证，不能把本机验证当作正式分发验证。

## 构建

准备 Rust、Node.js 和 Xcode。构建位置是 `packages/xuanpu-desktop`：

```bash
npm ci --ignore-scripts
cargo test --manifest-path src-tauri/Cargo.toml
npm run build -- --bundles app,dmg
```

输出位于 `src-tauri/target/release/bundle/macos/玄圃.app` 和 `src-tauri/target/release/bundle/dmg/`。构建锁定文件 `package-lock.json`、`Cargo.lock` 需要跟随源码提交。

图标来自控制台现有“玄”标记，可用 `scripts/icon.swift` 生成源 PNG，再用 `tauri icon` 生成系统图标；图标资源已随源码提供，普通构建无需重新生成。

## 原生验收

用 `tests/e2e/run_console.py --serve-only` 启动独立平台与模拟身份供应商。复制应用包到临时验收目录，修改复制品的 `Contents/Resources/desktop.json` 指向临时平台和模拟登录来源，使用不同的 Bundle ID 并重新本地签名，再打开该复制品。不得把测试登录数据混入交付包，不复制真实浏览器 Cookie。

验收项目包括模拟 OAuth 登录、退出重开保持身份、上传下载字节一致、文件模块与对话、通知悬浮卡、断连提示与恢复。真实企业服务单独检查登录入口。需要真人扫码的真实飞书登录应由员工完成，不以模拟身份验收替代。
