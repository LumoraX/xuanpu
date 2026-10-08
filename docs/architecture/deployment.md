# 单机 Docker 部署

玄圃平台以单个服务容器承载控制台、HTTP API、会话调度、提醒定时工作线程及外部通知投递。网页对话与通知使用 SSE；不依赖网页在线，也不依赖飞书长连接来计时。Postgres 保存企业身份、权限、业务记录、提醒和审计。文件正本与会话线程分别持久化到 `volumes/data/objects` 和 `volumes/data/threads`。

当前 `codex_responses` 模型协议使用本地会话状态，必须保持一个平台副本。将来扩容前需要完成该协议与 Postgres 状态面的适配、对象存储替换及完整迁移验证；不能直接复制平台容器。

`docker/deploy.sh` 在本机构建 linux/amd64 镜像并推送到 `docker/.env`（或环境变量）指定的镜像仓库，只有推送成功才更新 `docker/.env` 的 IMAGE 与 VERSION。镜像仓库、代理网络名、服务器地址等部署环境信息只放在不进仓库的 `docker/.env` 与部署者本机配置里，仓库只带 `docker/.env.example` 模板。临时构建上下文只包含七个运行 wheel、锁定的公开依赖、Dockerfile 和试用工单入口；真实 JSON、凭据和业务文件均不发送到构建器。安装包保留各自版本，部署记录额外保留不可变镜像 digest。

服务器 `<部署目录>` 使用独立 Compose 项目和 Postgres。数据库不暴露宿主端口，平台仅绑定服务器回环 8420，并通过 `.env` 中 `PROXY_NETWORK` 指定的已有代理网络提供 HTTPS。Nginx 禁止响应缓冲和写请求重试，以支持 SSE 和避免重复业务。

平台以 UID/GID 10001 运行，根文件系统只读、能力全部移除；JSON 配置只读挂载在 `/run/xuanpu/config.json`，不放在业务沙盒默认可读的 `/etc` 或 `/opt`。Docker socket 不挂载。业务脚本仍由 bubblewrap 创建独立 user/pid/ipc namespace，并限制可读业务包与可写工作区。为允许非特权 namespace，平台使用 seccomp unconfined、专属 AppArmor userns 许可 profile 及 systempaths unconfined；这放开容器默认系统调用过滤及 /proc 掩蔽，必须结合非 root、无能力、bubblewrap 和容器资源上限使用，不应将其误解为独立虚拟机。平台总内存 4 GiB、CPU 2、进程 512；PG 1 GiB、CPU 1。每个业务运行时暂不单独划分 cgroup。

`trial-tickets` 是现有 support-desk 样例包的兼容侧车，显式启用 trial profile 才启动；共享平台网络空间以保留已有 loopback 8431 连接器。Compose 更新平台时一并更新侧车。其样例工单数据仍为内存数据，重建会重置；正式企业工单需接入真实系统，不属于玄圃业务正本存储。

从本地迁移必须先停平台及定时工作线程，再导出 PG 与整个 data 目录。加密主密钥、租户及用户标识保留；只改变监听地址、数据路径、PG 连接及对外 URL。恢复前后比较表计数与文件 SHA256；新实例健康后才能切换入口。切换后保留本地快照，但不得同时运行两个提醒调度实例。

Ubuntu 24.04 的全局非特权 user namespace 限制保持开启。仅对玄圃使用 `docker/xuanpu-platform.apparmor` 专属 profile 的 userns 许可；恢复后由 AppArmor 服务加载。Docker 的 /proc 掩蔽会阻止子 PID namespace 挂载 proc，因此仅本非 root 平台设置 systempaths unconfined。部署验收必须执行真实沙盒探针，不能只检查 bwrap 文件存在。参考 [Ubuntu userns 限制说明](https://ubuntu.com/blog/ubuntu-23-10-restricted-unprivileged-user-namespaces) 与 [Moby 的 rootless 嵌套容器说明](https://github.com/moby/buildkit/blob/master/docs/rootless.md)。
