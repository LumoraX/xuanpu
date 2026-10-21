# Docker 镜像发布与服务器部署

适用：一台 Linux 服务器上的单实例部署。下文的 `<服务器>` 是部署者本机 `~/.ssh/config` 里的主机别名，`<部署目录>` 是服务器上放 Compose 项目的目录。服务器地址、域名、镜像仓库、代理网络名都是部署环境信息，只写在本机配置与 `docker/.env` 里，不写进仓库。正式公网地址需要企业域名、DNS 指向服务器、HTTPS 证书以及飞书后台登记相同回调；平台与数据库可先在服务器内部部署验证。

## 本机构建

```bash
cd docker   # 在 xuanpu 仓库根目录下
cp .env.example .env   # 首次：填 IMAGE（镜像仓库，不含 tag）与 PROXY_NETWORK（反向代理所在的外部网络）
./deploy.sh
```

构建依赖 uv、Docker Buildx，以及相邻的 taifeng-sandbox/taifeng-stores 源码。默认目标 linux/amd64。镜像仓库取环境变量 `IMAGE`，没有则取 `docker/.env` 里的 `IMAGE`；两处都没有时脚本报「需要 IMAGE」并退出，不会推到任何默认仓库。使用 Docker 已登录的仓库身份。推送成功后只改写 `.env` 的 IMAGE 与 VERSION，`PROXY_NETWORK` 等其他项原样保留。`OUTPUT_MODE=load ./deploy.sh` 仅构建到本机，不更新发布版本。镜像中包含控制台，服务器不需要另启前端服务。

## 服务器布局

- `compose.yml`、`.env`：部署描述，以及镜像仓库、镜像版本与代理网络名（模板见 `docker/.env.example`）。Compose 缺 `PROXY_NETWORK` 时拒绝启动。.env 不存业务秘密。
- `config/config.json`：平台 JSON（600、UID 10001），保留企业配置、加密主密钥和模型配置；容器中路径 /run/xuanpu/config.json。
- `secrets/postgres_password`：独立 Postgres 的初始化密码（600）。该值同时写进 JSON 的 PG DSN（主机名必须使用专属别名 xuanpu-database，不能使用共享代理网络中可能冲突的 postgres），初始化后不能只改密码文件。
- `secrets/tickets_token`：试用工单令牌（600、UID 10001），仅 trial profile 使用。
- `volumes/data`：玄圃文件、会话与工作区（UID/GID 10001）。
- `volumes/postgres`：专属 Postgres 数据，权限由数据库容器维护。
- `backups`：停写时产生的数据库与文件快照（700）。

## 首次安装沙盒许可

Ubuntu 24.04 在宿主机加载仅供玄圃容器使用的 profile；不修改全局 userns 限制：

```bash
install -m 644 <部署目录>/xuanpu-platform.apparmor /etc/apparmor.d/xuanpu-platform
apparmor_parser -r /etc/apparmor.d/xuanpu-platform
```

首次同步时将 docker/xuanpu-platform.apparmor 一起上传。重启后由 AppArmor 服务重新加载。

## 更新已有部署

先使用平台现有 JSON 和备份，不用重新初始化企业。平台停机之后备份 PG 和 data，所有导出文件必须保持私有：

```bash
ssh <服务器>
cd <部署目录>
umask 077
docker compose --profile trial stop trial-tickets platform
backup_dir="backups/$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$backup_dir"
docker compose exec -T postgres pg_dump -U xuanpu -d xuanpu -Fc --no-owner --no-acl > "$backup_dir/postgres.dump" && \
  tar -czf "$backup_dir/data.tar.gz" -C volumes data && \
  cp config/config.json "$backup_dir/config.json" && \
  cp .env "$backup_dir/image.env" && \
  cp compose.yml "$backup_dir/compose.yml"
```

本机发布成功后上传 compose.yml 和 .env；不要上传本机私有 JSON 覆盖服务器配置。服务器执行：

```bash
cd <部署目录>
# 用 && 保证拉取或迁移失败时不启动新平台。
# 已装试用业务包则保留 trial；正式系统可不启用该 profile。
docker compose --profile trial pull && \
  docker compose --profile operations run --rm migrate && \
  docker compose --profile trial up -d --wait
curl --fail http://127.0.0.1:8420/healthz
docker compose ps
```

Compose 更新必须包含 trial-tickets，否则只重建平台后侧车可能留在旧网络命名空间。不要手工仅替换平台容器。

## 公网入口

以 docker/nginx.conf.example 创建独立虚拟主机，域名证书准备好后再启用 HTTPS。服务器 JSON 的 `public_url` 改为域名的 HTTPS 源，`identity.cookie_secure` 改为 true。配置改变后重建平台及侧车；飞书企业后台登记 `https://企业域名/v1/auth/feishu/callback`，事件地址为 `/v1/identity/feishu/events/acme`。域名 Cookie 与本机 Cookie 分离，用户需要在新入口重新登录。

通知采用 SSE，代理需关闭 buffering，上传限制至少 20 MiB。统一 Nginx 先执行 nginx -t，成功才 reload。数据库端口始终无需公开。

## 回滚

保留升级前 .env、JSON、数据库 dump 及 data 快照。先停止平台和侧车，再恢复对应版本的数据库与 data，随后恢复旧镜像版本、执行该版本的迁移并启动。跨表结构升级不能只换旧镜像。禁止启动本地旧快照与服务器同时调度提醒。

实际沙盒检查：

```bash
cd <部署目录>
docker compose exec -T platform python - < sandbox_probe.py
```

成功应输出 sandbox-isolated；该探针真实执行脚本，验证能写工作区、不能读平台配置与全部对象目录、隔离网络仅有 loopback。

数据迁移核对、实际镜像 digest 与现场验收记录含服务器地址等部署细节，由部署者保存在本机私有目录（`~/.local/share/xuanpu/server-deploy/`），不进仓库；构建成功和健康检查不能替代真实登录、会话、文件及提醒测试。


## R1办公执行的部署边界

默认Compose仍用local/bubblewrap；R1办公启用独立Docker rootless服务，local后端明确拒绝办公任务。
平台不接宿主rootful Docker socket。使用现成Docker/systemd能力，不自建远程文件传输协议。

### 独立执行服务

Ubuntu24.04需官方docker-ce-rootless-extras、uidmap、slirp4netns及systemd用户总线。
将[安装器](../../docker/install_office_executor.py)同步到部署目录，以root执行：

```bash
python3 install_office_executor.py --deploy-dir <部署目录>
```

安装器创建无登录用户xuanpu-executor，UID/GID10002；平台仍为10001，冲突时拒绝。部署根必须root独占0700。
仅cache、workspaces、input-snapshots经systemd mount units绑定到宿主`/var/lib/xuanpu`同名子目录，
不绑定objects、配置、秘密或数据库。平台和daemon因此看到相同绝对路径及inode。
用户服务由linger开机恢复；不另给daemon增加PID namespace，以便兑现systemd容器资源控制；任务容器仍使用Docker默认独立PID namespace。

总额度在root管理的user-10002.slice：6GiB、2CPU、4096进程，覆盖daemon及全部容器scope。
任务单独限额仍生效。只限制docker.service无法覆盖兄弟容器scope，不能作为总额证明。
rootless不提供容器AppArmor；保留user namespace、默认seccomp、无capability、只读根和受限挂载。

专用Unix接口目录`/run/xuanpu-executor`由tmpfiles创建并只向平台10001授予命名ACL和默认ACL，API不开放TCP。
绑定整个专用目录使daemon重建socket后平台仍可重连；不绑定单个socket文件，也不暴露用户总线。
官方默认用户socket仅供该账户管理。用户家目录内配置由安装器降权写入，root不跟随其中的链接或chown。

### 接入和核对

独立daemon加载linux/amd64办公镜像后，以实际image ID固定sandbox.image。
服务镜像只增加固定官方摘要的Docker CLI，不含dockerd。保留既有JSON全部身份、模型和秘密，仅修改sandbox：

```json
{"sandbox":{"backend":"docker","image":"sha256:<办公镜像ID>","memory_mb":512,"cpus":1,
"docker_env":{"PATH":"/usr/local/bin:/usr/bin:/bin","DOCKER_HOST":"unix:///run/xuanpu-executor/docker.sock"}}}
```

同步[办公Compose覆盖](../../docker/compose.office.yml)，在.env设置：

```text
COMPOSE_FILE=compose.yml:compose.office.yml
EXECUTOR_SOCKET_DIR=/run/xuanpu-executor
```

覆盖文件恢复平台默认seccomp/AppArmor/systempaths，不再继承bubblewrap的额外放行。
按上文停写、备份、迁移和部署后运行[真实办公探针](../../docker/office_probe.py)：

```bash
docker compose exec -T platform python - < office_probe.py
```

探针拒绝rootful或不兑现systemd/cgroup v2资源限额的执行端，真实验证只读输入、断网、
配置/对象/socket不可见、512MiB/1CPU/256进程限制及容器停止后的安全导出。
另须保持平台容器不重建，重启执行daemon并验证新任务；平台重启只结算中断，不自动重放。
最后完成真实模型的会议、CSV和XLSX链路，核对本人确认、原生记录及下载摘要，不能用健康检查替代。

参考[Docker rootless](https://docs.docker.com/engine/security/rootless/)和
[systemd/cgroup要求](https://docs.docker.com/engine/security/rootless/tips/)。

共享目录使用Linux原生idmapped mounts（内核及util-linux需支持）：保持同一设备/inode和内容，挂载视图将平台UID/GID10001映射为执行UID/GID10002，新产物在底层仍属平台10001。不递归chown生产数据，不使用FUSE或放宽输入权限。安装器实际校验inode及映射属主，不支持时拒绝启动。

身份映射使用[Linux内核idmapped mounts](https://www.kernel.org/doc/html/latest/filesystems/idmappings.html)。`/run/xuanpu-executor`的命名ACL只允许10001平台访问API，不能改为所有人可写；tmpfiles在重启后恢复相同ACL。安装器会重启执行服务，必须先停写并等在途任务收尾，不在运行中重跑安装器。
