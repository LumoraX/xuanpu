# 玄圃 Docker 服务器部署实施计划

> **For agentic workers:** 使用 superpowers:executing-plans 逐项实施并验收；本次方案与发布已获用户授权。

**Goal:** 将当前玄圃及现有数据迁移到 <服务器> 的 <部署目录>，以镜像发布和 Compose 部署。

**Architecture:** 本机将锁定的运行依赖和工作区 wheel 放入临时构建上下文，构建 linux/amd64 镜像并发布阿里云镜像仓库。服务器使用独立 Postgres 与持久目录，单实例平台通过现有 Nginx 提供 HTTPS；JSON 配置、凭据和业务数据不进入镜像。平台中的 bubblewrap 沙盒保持业务脚本隔离，不挂载 Docker socket。

**Tech Stack:** Python 3.12、Docker Buildx、Docker Compose、Postgres 16、Nginx、bubblewrap。

---

### 1. 构建与部署入口
- [x] 新建 `docker/deploy.sh`：严格失败退出，发布成功之后原子写入 VERSION，发布失败保留原版本；准备运行 wheel 和锁定 requirements，使用独立临时上下文。
- [x] 新建 `docker/Dockerfile`：运行依赖与 bubblewrap，非 root 用户 10001，CLI 默认启动；业务配置挂载到 /run/xuanpu。
- [x] 新建 `docker/compose.yml`：仅平台连代理网络；PG 无宿主端口，密码从私有文件读；迁移为一次性服务；平台及会话/文件持久存储。
- [x] 新建 `packages/xuanpu-cli/tests/test_docker_release.py`：模拟外部构建器验证 push 失败时保留版本、成功时版本落盘，并检查构建上下文只含构建输入。
- [x] 执行 `bash -n docker/deploy.sh` 与 Compose 校验；构建及 push amd64 镜像。

### 2. 数据迁移与上线
- [x] 确认服务器没有玄圃目录或旧实例；准备 <部署目录> 的私有 config、secrets、backups 与 volumes。
- [x] 准备数据库并等待健康；停止本地平台和工单进程，保留本地数据库以导出一致快照；导出数据库及 data 目录并保留本机备份。
- [x] 配置仅更改监听地址、数据目录、数据库地址及对外 URL；保留加密主密钥、身份映射、租户、已有配置。
- [x] 恢复到服务器 Postgres，比较表计数与文件校验；执行迁移，启动平台。
- [x] 使用独立试用工单 sidecar 继续支持当前示例包的 loopback 连接器；明确其内存示例数据非正式工单数据库。
- [ ] 收到域名后检查 DNS 与证书、添加独立 Nginx 虚拟主机，先 nginx -t 再 reload；域名未提供则先只绑定服务器 loopback 并保留该项待完成。

### 3. 验收及交付
- [x] 验证健康、未认证拒绝、已有管理员身份、文件与会话持久性；验证真实 Linux 沙盒脚本执行和不能读取 /run/xuanpu；不向真实员工发送测试飞书通知。
- [x] 执行平台重启并验证数据保持；检查定时提醒工作线程及站内通知调度，区分飞书登录需要用户实际扫码的验收。
- [x] 执行全量 pytest、门禁 Ruff 与 mypy；使用 requesting-code-review 进行独立部署审查，解决实质问题。
- [x] 更新 `docs/guides/docker-deployment.md` 与 `docs/architecture/deployment.md`，记录实际版本、备份和回滚命令；合并 main。
- [ ] 对外 URL 确定后更新并构建桌面安装包；未完成项不得宣称完成。
