# 2026-10-21 R1生产部署与验收

R1办公交办已接入正式Linux执行环境并完成生产验收。服务版本`2026.10.21.3`，数据库迁移`0008→0010`；既有正式租户已启用`office-assistant@1.0.1`，其他业务包保持原状态。平台保持单实例。

## 发布物与源码

| 层 | 实际证据 |
| --- | --- |
| 服务源码 | `b7a3105`；部署后、服务重建后均逐文件比对，178份已安装Python及前端源码SHA-256与检出一致 |
| 服务镜像 | `registry.cn-shanghai.aliyuncs.com/krasuschen/xuanpu:2026.10.21.3.r1-production` |
| 注册表摘要 | `sha256:4a598dea1b21c4cce2072a8835575419b9f01590573b19766712ae3adf5d67ec` |
| 服务依赖 | server `2026.10.21.3`；console/SDK/CLI `2026.10.21.2`；spec `2026.10.2.7` |
| 执行依赖 | taifeng `2026.10.1.11`；sandbox `2026.10.21.1`；stores `2026.9.29.15` |
| 办公执行镜像 | Linux/amd64，固定宿主镜像ID `sha256:386b7f111293317e961435f3e9d842847a49b375ae7063697ccc3c7ba82cef8b`；本机构建与宿主加载后的RootFS层及Config逐项一致 |
| 办公包 | 正式租户和隔离验收租户digest均为`sha256:99a2abecb049807604815b1571853fad77d51b59ff3f4d5e7354bdb6fb4d70c1` |

部署方法见[Docker部署](../docker-deployment.md)，执行边界见[运行时架构](../../architecture/deployment.md)。服务镜像只带Docker客户端；执行daemon属于独立用户服务。

## 备份与原有数据

确认执行租约归零后停止平台和原试用侧车，备份PostgreSQL、全部data、配置、Compose、旧容器描述与执行服务配置，再迁移和启动。备份位于宿主`/data/xuanpu/backups/r1-20261021-121832`，只允许root访问；摘要已实际校验。

- PostgreSQL归档：`ce7433dc53ddff85aec08081bd8aab50b9613f92e439f7f59aa9c85cfe77fafa`。
- 旧服务镜像的完整linux/amd64离线归档`previous-image-verified.tar.gz`：`b2431f090dbfbf19812cb3c7df6b9039b498a4d176e2d471aa5acbfdc6682dd8`，实际11层和镜像配置已核验；宿主复核摘要一致。
- 全部data归档：`d9056ddcc01b97a36e1d72b5eb81208c4ce27f85d2629127e8b3cd5a7ca1248d`。
- 切换后、验收写入前，原有24组按租户分表数量完全相等；全部15份原有数据文件SHA-256不变。
- 后续验收在独立合成租户产生3个成功任务、2个取消任务（其中运行中取消在重建后补验）；正式租户另有1个已关闭的只读工单回归会话及办公包发布记录。这些是本次明确新增，不能用切换前后不变的结论覆盖它们。

未实施快照恢复或整机重启。若已有新的正式写入，不能盲目用旧快照覆盖；恢复方案见部署手册。

## 生产隔离实测

| 检查 | 结果 |
| --- | --- |
| 平台容器 | UID10001、只读根、capabilities全丢弃、默认seccomp、`docker-default` AppArmor、默认masked/readonly系统路径、no-new-privileges；无特权模式 |
| daemon | 独立UID10002、无登录shell、Docker rootless 29.6.2，systemd cgroup v2；只监听Unix socket |
| 平台挂载 | 仅配置、平台data、独立executor socket目录；没有宿主rootful Docker socket |
| 共享数据 | 仅cache/workspaces/input-snapshots，Linux原生idmapped bind；相同设备/inode/字节，底层仍归平台UID10001，无递归chown |
| 执行用户权限 | 实际拒绝读取平台配置、对象目录、rootful socket及通过平台进程proc/root访问配置 |
| 实际任务容器 | 输入只读、network仅loopback、512MiB、1CPU、256pids；配置、对象及任何Docker socket不在容器内 |
| 全部执行的总额度 | root管理的user-10002.slice，6GiB、2CPU、4096tasks，覆盖daemon及其任务scope |
| socket生命周期 | 挂载稳定目录；重启daemon且平台容器ID不变后，探针和新的真实模型任务均成功；tmpfiles重复执行两次后ACL仍有效 |

rootless不提供容器AppArmor。本次依靠user namespace、默认seccomp、无capability、只读根、受限挂载和cgroups控制任务；平台容器自身恢复了默认AppArmor等防护。未启用RootlessKit额外PID namespace：该选项与本机user-systemd的PID映射不兼容；每个业务容器仍使用Docker默认独立PID namespace。

## 真实模型与业务结果

所有办公案例通过正式HTTPS访问生产PostgreSQL中的独立验收租户，模型为`gpt-5.6-luna`，输入仅合成材料。原始模型产物、HTTP回执、下载文件和独立评估分别保留于操作者私有证据目录，未作为公开源码提交；[脱敏证据索引](2026-10-21-r1-production-evidence.json)记录摘要和检查结论。

| 案例 | 独立核验 |
| --- | --- |
| CSV、XLSX分别交办 | 两个任务均成功；A=12.50、B=3.00、总计15.50，来源行2/3/4；重新下载核对SHA-256，标准库独立解析XLSX XML |
| 纪要与行动项 | 3条本人候选，所有引用字符区间与固定输入正本逐字一致；明确截止/日程/提醒时间正确，不明截止保留null，讨论日期未冒充期限，未向其他同事分派 |
| 网页部分确认 | 关闭网页、重新打开仍可补读等待卡；将首条改为“R1生产验收｜已编辑方案”，只确认前两条，实际创建1待办+1日程+1站内提醒，第三条不创建 |
| 幂等 | 相同受理请求返回同一task；原网页确认request_id及相同payload重放，state_version和记录数量不变 |
| 访问边界 | 另一成员及租户管理员以各自员工身份读取本人任务/成果均404；机器身份读取员工任务403；匿名/无效key均401；有效cookie可读，伪造Origin写入403 |
| 取消与清理 | 新受理后、创建会话前立即取消，最终cancelled且resources_pending=false；另在实际沙盒中观察运行中进程后kill，退出码-9并验证容器删除；再补验实际running且已有沙盒容器的work-task，HTTP取消后cancelled、resources_pending=false，容器归零；临时补验身份再次回收 |
| 服务重建 | 实际重新创建平台及原试用侧车；三个成功任务和取消任务补读正确，10份成果下载摘要一致，本人待办/日程/提醒保留，重建后隔离探针再次成功 |
| 既有业务回归 | 正式租户真实模型只读查询样例工单T-1001，getTicket与sla脚本均成功，得出24小时；没有调用写工具，验收会话已关闭 |

浏览器通过仅绑定本机回环地址的验收代理查看和确认**生产后端真实记录**；代理固定使用合成验收成员的有效cookie，不开放管理/密钥接口。正式HTTPS接口独立直连完成认证、CSRF和内容权限检查。本次没有重新进行真实飞书扫码登录，因此页面“飞书”标签不构成飞书登录验收证据。代理已关闭。

生产没有关闭全局模型配置。本次确认走原生事务接口，不新增模型生成；“模型禁用后原生能力”沿用此前[R1本机验收](2026-10-21-r1-acceptance.md)的独立证据，不将其记作本次生产禁用模型试验。

R1只保证任务由服务端持有和持久补读；进程重启后的自动续跑仍属于R2，未以重建后的成功补读冒称自动续跑。

## 收尾与运行状态

验收未来提醒已取消、两名合成成员已停用、临时key已吊销，所有合成登录会话归零。最后一名合成管理员按平台保护规则保留，整个验收租户已暂停，无法继续登录使用；未绕过最后管理员规则。验收会话活动数为0，executor容器无残留。

平台与数据库健康，HTTPS返回服务版本`2026.10.21.3`；服务重建后的运行日志中Traceback、cycle_failed、cleanup_failed、ERROR均为0。未验证飞书新登录、外部消息发送、整机重启或真实企业材料质量；本次没有发送外部通知。

源码、镜像、生产进程、真实模型和浏览器分别留有证据；完整`uv run pytest`为1842通过、3跳过；`uv run ruff check --select F,S108,I,TC docker packages`通过；`uv run mypy packages/*/src`为164个源码文件无错误；`uv lock --check`通过。合入并行财务文档后，文档检查122项通过，独立只读复核通过。具体门禁结果见脱敏证据索引。
