# R4-G 分层发布、恢复与交付验收实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans；所有结果以实际运行证据填写。

**Goal:** 将A–F制成可验证发布，并证明升级保全旧记录、关闭新受理后可合法收尾、临时资源清理完整；真人暂缓单独记录。

**Architecture:** 复用当前单实例Compose、PG和独立rootless执行环境，三步验证“新兼容reader全关→隔离合成试点→明确授权的正式租户”。只读查询和运行来源检查与真实业务签收分别记录。路径/夹具见[总计划](2026-10-25-r4-delivery.md)。

**Tech Stack:** 当前Docker/PG/ObjectStore、现有部署脚本、pytest/原生网页、精确文件SHA与镜像digest。

---

## 分层验收矩阵

| 层 | 必需证据 | 不能替代什么 |
| --- | --- | --- |
| 源码/依赖 | 本轮全量pytest/Ruff/strict mypy/lock、当前taifeng稳定接口、每包版本与锁定依赖 | 不代替镜像和运行环境 |
| 实际PG | 受理、发布、构建、撤权、聚合提交窗口SIGKILL，重启重放不双事实 | memory契约通过不代替PG故障 |
| 原生网页 | 安装差异/分配/发布/知识删除/样板/统计，身份切换、390px、Tab、模型停用兜底 | HTTP200或资源匹配不代替实际操作 |
| 真实模型 | 确定输入、实际协议、引用/注入/错误情形及独立语义判断 | mock模型和任务succeeded不代替语义 |
| 归档/镜像 | wheel版本、归档SHA、镜像digest、实际安装文件与源代码白名单一致 | 镜像标签不证明部署内容 |
| 部署/迁移 | 停写备份、恢复可读、发布迁移、旧表原列和原对象哈希、unknown/预算/审批原记录保留 | healthz成功不证明数据不变 |
| 复制/回退 | 两隔离租户、版本未来切换、旧任务固定、关闭新受理仍合法收尾 | 隔离租户不是真人企业效果 |
| 真人业务 | 恢复试点后本人登录、真实材料、实际有权决定及独立签收 | 当前deferred_by_user，不伪造也不记pass |

## G0：候选冻结与隔离整体验收

**Files**：Create `scripts/acceptance/r4_release_acceptance.py`、`T/r4_contract/{test_r4_acceptance_matrix,test_r4_shutdown,test_r4_compatibility}.py`；Modify `docker/{compose.yml,compose.office.yml}`仅在实际需要配置时变更、`scripts/refresh_console_assets.py`、各包pyproject和lock；Create实施时的`docs/guides/releases/<实际日期>-r4-acceptance.md`及脱敏JSON。

- [ ] 核对main、schema ledger、安装依赖、平台/执行服务配置及占用；冻结候选commit，记录并行财务/本机试用分支未被合并。版本按实际发布日，不预填未来包版本。
- [ ] 独立运行各阶段目标集、全量pytest、Ruff F/S108/I/TC、strict mypy、lock、diff；新失败查根因，不以旧基线绿灯覆盖。不预填通过数。
- [ ] 真PG故障窗口至少覆盖：任务binding提交前后、发布提交前后、索引写完未标ready、来源撤权后清理前、聚合水位前后。各窗口用独立进程与实际终止，保留第一次失败证据。
- [ ] 用独立地址、命名空间、合成租户跑两次同delivery安装和完整样板，真实模型另有预算上限。脚本默认拒绝生产域名；明确操作者、数据种类和外部写次数。
- [ ] 实际原生双身份/移动宽度/模型停用验证，截图标synthetic。完整制品和原始协议进入私有证据目录，仓库只留脱敏结果，扫查秘密、Cookie和企业材料。
- [ ] 运行：`uv run pytest packages/xuanpu-server/tests/r4_contract/test_r4_acceptance_matrix.py packages/xuanpu-server/tests/r4_contract/test_r4_shutdown.py packages/xuanpu-server/tests/r4_contract/test_r4_compatibility.py -q`及总门禁；提交`test: 冻结R4候选与分层验收门禁`。

## G1：迁移与逐层部署

**Files**：Modify `docs/guides/{operations,docker-deployment}.md`；Create实施时私有运行记录，不将凭据/数据库备份纳入Git。

- [ ] 进入部署前重新核对用户当前授权和维护范围；本轮计划本身不执行部署、不扩大真实企微范围。仅授权范围内重建玄圃容器，不重启共享宿主或求本应用。
- [ ] 停止玄圃受理与写入，核对当前执行/租约/在途操作，再备份PG、对象、配置、密钥引用/密文、执行服务配置及旧镜像。实际验证PG可读/镜像各层SHA和文件清单，不只记录备份路径。
- [ ] 原样保全0019及以前旧表原列、原文件、R2预算/effect未知、R3交付/审批/签收。执行新增迁移，立即核对旧数据不变，再启动新兼容reader且R4新受理全关。
- [ ] 在同服务器独立合成租户开启R4，验证隔离、资源、索引清理、旧任务固定、版本回退及模型停用。正式租户只有明确名单才开启；真人验收仍暂缓则界面及报告标“技术灰度，真实业务未签收”。
- [ ] 对运行容器实际安装文件逐一与候选源码对齐，下载正式前端指纹资源比字节；浏览器重新打开确认不会因旧缓存漏新控件。
- [ ] 不回退到旧R3 reader读取新严格JSON格式；首选同兼容镜像关新受理。若必须恢复备份，先停新写入并迁回最新记录，禁止直接丢弃上线后新任务。
- [ ] 记录实际host/schema/image/digest/source/status至服务器release元数据，并保留旧文件；未知/暂缓项不改成pass。

## G2：前向回退、清理与最终交付

**Files**：Modify实施中的R4发布记录、架构/使用手册及[总路线](../specs/2026-10-20-platform-capabilities-design.md)阶段索引。

- [ ] 关闭所有R4新受理，验证已纳管包仍不能借旧active/session入口运行；原有绑定工作按B状态矩阵收尾，旧资料撤权与知识清理仍生效。
- [ ] 实际重启玄圃服务，核对发布/分配/绑定/索引清理/统计水位及旧账本。只有开机依赖或执行服务配置发生变更时才增加对应服务恢复验收；共享宿主重启另需明确维护授权，不当成默认动作。
- [ ] 撤销临时评测/机器凭据、来源授权与未来触发，暂停合成租户、关闭侧车/临时实例/本机夹具/隧道。账本和实际失败记录保留，机器key吊销后实际401。真实试点账号不随测试清理删除。
- [ ] 原始私有证据复制到工作区之外，验证摘要；公开记录区分source/image/runtime/browser/real_model/real_business。`deferred_by_user`持续保留到恢复真人验收，不因技术通过自动变为完成。
- [ ] 最终交付：精确commit/包版本/镜像digest、关闭与恢复操作、安装/分配手册、样板复制说明、知识撤权及删除说明、统计口径、已知限制和待项。
- [ ] 提交`docs: 记录R4技术交付与真实验收边界`；是否推送或部署按当时授权执行，不能仅因本机main已合入就宣称远端或生产已更新。

## 恢复真人验收时的入口

不要求补建部门。先重查陈翔/drYang的当前账号映射、普通成员权限和真实本人登录，再用明确试点材料检查R3共享/审批/独立签收及R4能力分配/样板成果。管理员通讯录导入如仍需验，使用实际有权管理员本人授权；不自动提升陈翔、不让机器代同意。用户未恢复此项之前，技术验收可完成，真实业务签收始终暂缓。
