# 单机 Docker 部署

玄圃平台以单个服务容器承载控制台、HTTP API、会话调度、提醒定时工作线程及外部通知投递。网页对话与通知使用 SSE；不依赖网页在线，也不依赖飞书长连接来计时。Postgres 保存企业身份、权限、业务记录、提醒和审计。文件正本与会话线程分别持久化到 `volumes/data/objects` 和 `volumes/data/threads`。

R0 所有协议均必须保持一个平台副本、一个服务进程，启动时先取得控制面单实例锁。`codex_responses` 模型协议使用本地会话状态。将来扩容前需要完成该协议与 Postgres 状态面的适配、对象存储替换及完整迁移验证；不能直接复制平台容器。

`docker/deploy.sh` 在本机构建 linux/amd64 镜像并推送到 `docker/.env`（或环境变量）指定的镜像仓库，只有推送成功才更新 `docker/.env` 的 IMAGE 与 VERSION。镜像仓库、代理网络名、服务器地址等部署环境信息只放在不进仓库的 `docker/.env` 与部署者本机配置里，仓库只带 `docker/.env.example` 模板。临时构建上下文只包含七个运行 wheel、锁定的公开依赖、Dockerfile 和试用工单入口；真实 JSON、凭据和业务文件均不发送到构建器。安装包保留各自版本，部署记录额外保留不可变镜像 digest。

服务器 `<部署目录>` 使用独立 Compose 项目和 Postgres。数据库不暴露宿主端口，平台仅绑定服务器回环 8420，并通过 `.env` 中 `PROXY_NETWORK` 指定的已有代理网络提供 HTTPS。Nginx 禁止响应缓冲和写请求重试，以支持 SSE 和避免重复业务。

默认local配置下，平台以 UID/GID 10001 运行，根文件系统只读、能力全部移除；JSON 配置只读挂载在 `/run/xuanpu/config.json`，不放在业务沙盒默认可读的 `/etc` 或 `/opt`。Docker socket 不挂载。业务脚本仍由 bubblewrap 创建独立 user/pid/ipc namespace，并限制可读业务包与可写工作区。为允许非特权 namespace，平台使用 seccomp unconfined、专属 AppArmor userns 许可 profile 及 systempaths unconfined；这放开容器默认系统调用过滤及 /proc 掩蔽，必须结合非 root、无能力、bubblewrap 和容器资源上限使用，不应将其误解为独立虚拟机。平台总内存 4 GiB、CPU 2、进程 512；PG 1 GiB、CPU 1。local业务运行时不单独划分 cgroup。

`trial-tickets` 是现有 support-desk 样例包的兼容侧车，显式启用 trial profile 才启动；共享平台网络空间以保留已有 loopback 8431 连接器。Compose 更新平台时一并更新侧车。其样例工单数据仍为内存数据，重建会重置；正式企业工单需接入真实系统，不属于玄圃业务正本存储。

从本地迁移必须先停平台及定时工作线程，再导出 PG 与整个 data 目录。加密主密钥、租户及用户标识保留；只改变监听地址、数据路径、PG 连接及对外 URL。恢复前后比较表计数与文件 SHA256；新实例健康后才能切换入口。切换后保留本地快照，但不得同时运行两个提醒调度实例。

Ubuntu 24.04 的全局非特权 user namespace 限制保持开启。仅对玄圃使用 `docker/xuanpu-platform.apparmor` 专属 profile 的 userns 许可；恢复后由 AppArmor 服务加载。Docker 的 /proc 掩蔽会阻止子 PID namespace 挂载 proc，因此仅本非 root 平台设置 systempaths unconfined。部署验收必须执行真实沙盒探针，不能只检查 bwrap 文件存在。参考 [Ubuntu userns 限制说明](https://ubuntu.com/blog/ubuntu-23-10-restricted-unprivileged-user-namespaces) 与 [Moby 的 rootless 嵌套容器说明](https://github.com/moby/buildkit/blob/master/docs/rootless.md)。

持有权使用独立 PostgreSQL 连接；断开后立即关闭接收闸门并清理工作，健康变 503，不能自动重连续跑。
启动新实例前须确认旧进程已退出。咨询锁没有为工具或状态面提供 fencing，已发出的外部动作可能完成。
迁移 0009 仅新增审批回执表；退回旧程序前必须停新写入并保留回执表，不通过删除回执解除未知操作。


## R1独立办公执行

办公部署复用DockerSandboxProvider稳定契约，服务镜像带固定官方摘要的CLI，独立rootless daemon
由无登录UID10002账户的systemd用户服务承载。平台仅绑定专用Unix接口目录；不挂宿主rootful socket，
不授予SYS_ADMIN，不开放Docker TCP。恢复平台Docker默认seccomp/AppArmor/systempaths。

数据面通过三个systemd绑定挂载保持相同inode及绝对路径：cache、workspaces、input-snapshots。
实际配置与对象继续留在root独占部署根，执行服务不共享这些目录。任务容器只得到自己的工作区、
只读包与只读输入，不获得daemon接口。rootless容器root映射到宿主UID10002，共享目录以内核idmapped mounts映射回平台10001，成果可由平台安全导出。
执行daemon与平台不同UID，任务容器保留Docker默认独立PID namespace。rootless不提供容器AppArmor，不能宣称与rootful所有防护相同。

聚合额度由root管理的user-10002.slice覆盖所有容器scope；daemon自身限额不视为总额。
稳定接口目录允许daemon重建socket后重新连接，临时执行进程丢失仍按R1中断处理，不自动重发。
安装、探针和回滚步骤见[办公执行部署](../guides/docker-deployment.md#r1办公执行的部署边界)。

共享目录使用Linux原生idmapped mounts（内核及util-linux需支持）：保持同一设备/inode和内容，挂载视图将平台UID/GID10001映射为执行UID/GID10002，新产物在底层仍属平台10001。不递归chown生产数据，不使用FUSE或放宽输入权限。安装器实际校验inode及映射属主，不支持时拒绝启动。
