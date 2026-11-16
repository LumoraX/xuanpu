# R5 生产发布记录（2026-11-15）

## 版本

| 项 | 值 |
| --- | --- |
| 源码 | xuanpu `main` `735698d`（合并 R5） |
| 内核 | taifeng 2026.11.14.1（PyPI，Publish 工作流 run 43691057213） |
| 沙盒 / 存储 | taifeng-sandbox `af3e545`（2026.10.21.1）、taifeng-stores 2026.9.29.15 |
| 服务镜像 | `xuanpu:2026.11.15.1.r5-candidate`，linux/amd64 |
| 控制面 schema | 19 → 25（0020 capability_delivery，0021–0025 执行追踪、管家派工与唤醒、编组与会诊消息） |

## 发布方式

部署者本机到生产宿主的上行约 33 KB/s，整镜像无法在合理时间内送达；部署者本机也未登录服务镜像所在仓库。因此改为：本机按 `docker/deploy.sh` 同样步骤生成构建上下文（全部 wheel 与 `uv export --frozen` 的带哈希 requirements，1.3 MB，SHA-256 两端一致），在生产宿主上用仓库 `docker/Dockerfile` 构建同名镜像。镜像内核与 schema 版本经容器内核对。

之后按[Docker 部署](../docker-deployment.md)「更新已有部署」执行：停平台与试用侧车 → 私有备份（PG dump、data、config、.env、compose，均校验可读）→ 改 VERSION → `migrate` → `up --wait`。

注意：通过 `ssh … 'bash -s' <<EOF` 执行时，`docker compose run` / `exec` 会读走后续脚本的标准输入，导致后续命令不执行。本次因此中断两次（均在安全点：仅停机与已完成的迁移），之后改为逐条执行。经 ssh 交给脚本时，这些命令须加 `< /dev/null`。

## 验证

- 发布前：生产现有配置在新镜像下 `load_config` 通过；R5 新开关 `execution_trace`、`telemetry.otel`、`butler`、`minimal_entry` 均为关闭。
- 迁移输出：已执行 0020–0025；`xp_schema_migrations` 最大版本 25。
- 启动后：平台、侧车、PostgreSQL 均 healthy；`/healthz` 200（实例版本 2026.10.25.1）；HTTPS `/console/`、`/console/entry.html` 200；`/v1/me`、`/v1/dashboard`、`/v1/traces` 未登录 401；平台日志无错误；`office_probe.py` 隔离通过（rootless、输入只读、仅回环网络、内存/CPU/PID 上限、无秘密与套接字）。
- 源码门禁（本机，PyPI 内核）：`uv lock --check` 通过；ruff All checks passed；mypy 559 个源文件无错误；全量 3771 passed / 60 failed / 49 errors / 445 skipped，失败集合与 main 同环境一致（办公容器的 taifeng-sandbox 找不到 docker 可执行文件）。

## 未做

- 真实员工登录与对话、真实模型验收：未执行。R5 新功能未对任何租户开启，员工可见行为与 R3 版本一致。
- 开启任一 R5 功能前，须先完成 R5 计划中的真实模型验收与用户签收（[R5 交付计划](../../superpowers/plans/2026-11-14-r5-delivery.md) 总门槛）。

## 回退

停平台与侧车 → 恢复本次升级前备份的 PG 与 data → `.env` 的 VERSION 改回升级前版本 → 启动。schema 25 不能直接运行旧镜像。备份位置与宿主细节保存在部署目录的 DEPLOYMENT.md，不进仓库。
