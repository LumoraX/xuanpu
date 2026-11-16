# 桌面客户端安装与构建

## 员工安装

当前版本面向 Apple Silicon Mac，要求 macOS 12 或更高版本。打开 `玄圃_2026.11.16_aarch64.dmg`，将“玄圃”拖入 Applications，再从应用程序中打开。

应用包不内置任何服务器地址。首次打开时启动页显示「欢迎使用玄圃」并展开设置，填写要连接的玄圃服务器地址后「保存并连接」，再正常登录即可；不需要填写模型或应用密钥。服务器地址向企业管理员索取。桌面版与原浏览器的登录状态独立。

- 文件上传沿用系统选择文件窗口。
- 文件下载保存到 Mac 的“下载”文件夹，文件名带唯一标识，防止覆盖旧文件；菜单“工作站 → 打开下载文件夹”可以直接打开。
- “工作站 → 刷新”（⌘R）重新加载页面，“重新连接”重新检查服务。
- 服务无法连接时使用“重新连接”按钮；仍失败则联系企业管理员。

### 更换服务器与查看日志

- 首次打开会直接展开设置；之后可在启动页「服务器：…」一行旁点「设置」，或菜单「工作站 → 服务器设置…」（⌘,），填写服务器地址后「保存并连接」。可以只填域名，例如 `xuanpu.example.com`，客户端会补成 `https://xuanpu.example.com/console/`。必须是 HTTPS；本机调试可用 `http://127.0.0.1:8420`。
- 设置保存在 `~/.xuanpu/desktop.json`，只对这台 Mac 生效；「清除地址」删除该文件，启动页回到首次使用时的填写状态。
- 运行日志在 `~/.xuanpu/logs/desktop.log`，启动页「打开日志」或菜单「工作站 → 打开日志文件夹」可直接打开。连不上时把这份日志发给管理员，其中有具体失败原因（例如超时、域名解析、证书、连接被拒）。
- 换了服务器地址后需要在新地址重新登录；登录状态按服务器分开保存。

`~/.xuanpu/desktop.json` 示例：

```json
{
  "service_url": "https://xuanpu.example.com/console/"
}
```

填写的地址必须与后台 `public_url` 同源，否则登录回调无法带上客户端的 Cookie。可选的 `auth_origins` 覆盖登录来源白名单（每项只能是 HTTPS 根地址）；不写则沿用应用内置的飞书来源。退出桌面应用不会停止后台的提醒调度；退出后没有桌面系统通知，外部通知仍按后台配置发送。

该包为 ad-hoc 签名、未经 Apple 公证：从网络或聊天工具下载到其他 Mac 后，首次打开会被系统拦截，需要在「系统设置 → 隐私与安全性」中点「仍要打开」，或在 Finder 中右键「打开」。面向全员分发前应换用企业 Developer ID 签名并公证。

## 应用内置配置

`packages/xuanpu-desktop/src-tauri/desktop.json` 随应用包发布，只允许一个字段：

| 字段 | 说明 |
| --- | --- |
| `auth_origins` | 登录供应商的精确来源白名单，包含协议、域名与端口，只能使用根路径 |

应用包、仓库与构建配置里都不写服务器地址：该文件出现 `service_url` 或其他未知字段时客户端拒绝加载，`cargo test` 也会检查随包文件。服务器地址只由用户在本机填写，同一个安装包可以连接任何玄圃服务器。飞书开发者后台的重定向地址仍是后台 `/v1/auth/feishu/callback`；桌面应用不使用新的深链回调。

该文件只放公开的登录来源。数据库、模型、飞书 App Secret 继续保留在服务端。本机版采用 ad-hoc 签名，不包含 Developer ID 或 Apple 公证；公司跨设备分发前应使用企业开发者签名并完成公证，不能把本机验证当作正式分发验证。

## 构建

准备 Rust、Node.js 和 Xcode。构建位置是 `packages/xuanpu-desktop`：

```bash
npm ci --ignore-scripts
cargo test --manifest-path src-tauri/Cargo.toml
npm run build -- --bundles app,dmg
```

输出位于 `src-tauri/target/release/bundle/macos/玄圃.app` 和 `src-tauri/target/release/bundle/dmg/`。

`bundle_dmg.sh` 需要经 AppleScript 控制 Finder 排版窗口；在无图形会话或未授权自动化的终端里会失败（`failed to run bundle_dmg.sh`）。这时 `.app` 已经生成，可以直接用系统工具打包：

```bash
cd src-tauri/target/release/bundle
stage=$(mktemp -d); cp -R "macos/玄圃.app" "$stage/"; ln -s /Applications "$stage/Applications"
hdiutil create -volname 玄圃 -srcfolder "$stage" -fs HFS+ -format UDZO -ov "dmg/玄圃_<版本>_aarch64.dmg"
hdiutil verify "dmg/玄圃_<版本>_aarch64.dmg"
```构建锁定文件 `package-lock.json`、`Cargo.lock` 需要跟随源码提交。

图标来自控制台现有“玄”标记，可用 `scripts/icon.swift` 生成源 PNG，再用 `tauri icon` 生成系统图标；图标资源已随源码提供，普通构建无需重新生成。

## 原生验收

用 `tests/e2e/run_console.py --serve-only` 启动独立平台与模拟身份供应商。复制应用包到临时验收目录，修改复制品的 `Contents/Resources/desktop.json` 加入模拟登录来源，使用不同的 Bundle ID 并重新本地签名，再打开该复制品，在启动页填写临时平台地址。验收结束后用「清除地址」删除本机设置。不得把测试登录数据混入交付包，不复制真实浏览器 Cookie。

验收项目包括模拟 OAuth 登录、退出重开保持身份、上传下载字节一致、文件模块与对话、通知悬浮卡、断连提示与恢复。真实企业服务单独检查登录入口。需要真人扫码的真实飞书登录应由员工完成，不以模拟身份验收替代。
