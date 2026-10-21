# R1生产执行接线与部署

承接已批准R1的部署步骤。本次授权含执行环境接线、备份、部署和真实验收，不扩展R2自动续跑。

## 执行边界

复用现成Docker rootless及systemd用户服务，不自建远端执行协议。使用UID10002独立无登录用户，平台UID10001不变，
仅将cache、workspaces、input-snapshots绑定到与平台一致的`/var/lib/xuanpu`路径。
执行用户不能穿越生产部署根目录；配置、对象、数据库、备份不共享给执行服务。
平台只连接独立rootless Unix socket，不连接宿主rootful Docker socket，不开放Docker TCP。
rootless容器内root映射到宿主执行用户，通过内核idmapped mounts保持产物与平台UID兼容。单任务资源限制和执行服务总额度均实测。
平台Compose覆盖去掉为bubblewrap放开的seccomp、AppArmor及systempaths选项，恢复Docker默认防护。

相较特权DinD，此方案不授予SYS_ADMIN；相较独立远程虚拟机，不新增当前R1尚未支持的文件传输协议。
rootless不提供容器AppArmor，仍依靠user namespace、默认seccomp、无capability、只读根、受限挂载和资源限制。
正式平台的配置密钥和对象存储保持原路径及权威数据，已有包不卸载。

## 实施与验收

- [x] 建立隔离检出，记录生产基线、原有记录数量与活动执行。
- [x] 验证Docker客户端、Compose覆盖、rootless安装和资源/路径检查；先红后绿。
- [x] 安装独立用户服务和三个绑定目录；加载办公镜像，校验rootless/cgroup及秘密不可读。
- [x] 完整pytest、Ruff、mypy与本机候选构建通过，修复复核问题。
- [ ] 停止受理并等待执行收尾，备份数据库、全部data、配置、Compose、镜像及执行配置；核验摘要。
- [ ] 发布候选镜像，迁移0008→0010，部署单实例与原试用侧车，核对HTTPS及既有数据数量。
- [ ] 使用独立验收租户和合成材料，真实模型完成CSV、XLSX及会议→本人确认→待办/日程/站内提醒。
- [ ] 实际容器核对只读输入、限制、取消清理、关页补读、身份隔离和下载摘要；重建后补读持久结果。
- [ ] 回收验收身份及key，保留成果回执与脱敏发布记录，合入本地主线。

部署失败先停止新受理并结算在途工作；按备份恢复旧配置和Compose，再启动旧镜像。
新增表及已产生的正式成果不盲目删除。只有确认无新增正式写入时才使用全库/文件快照回滚。
rootless安装与数据迁移互相独立，前者失败不重启既有生产平台。

参考：[Docker rootless](https://docs.docker.com/engine/security/rootless/)、
[systemd和cgroup要求](https://docs.docker.com/engine/security/rootless/tips/)。

共享目录使用Linux原生idmapped mounts（内核及util-linux需支持）：保持同一设备/inode和内容，挂载视图将平台UID/GID10001映射为执行UID/GID10002，新产物在底层仍属平台10001。不递归chown生产数据，不使用FUSE或放宽输入权限。安装器实际校验inode及映射属主，不支持时拒绝启动。
