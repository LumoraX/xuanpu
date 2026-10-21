# R1 办公交付验收（2026-10-21）

本次针对普通员工的办公交办：固定本人材料、后台处理、下载真实成果，以及本人选择后的原生待办、日程和站内提醒。
生产部署未执行；临时验收使用回环地址、合成材料、独立PostgreSQL schema和Linux Docker，不连接生产数据或外部通知。

## 验收层次

| 层 | 本次结果 |
| --- | --- |
| 源码 | R1实现与复核修复已落地；最终提交及门禁见下文记录 |
| 后端/前端/SDK | 全量pytest、Ruff、mypy与锁文件核验；界面行为另以浏览器验证 |
| 数据库 | 实际PostgreSQL双后端契约；迁移0009→0010、新库及重复迁移测试 |
| 执行 | 实际Linux Docker、只读输入、真实办公依赖及脚本、进程停止和安全导出 |
| 真实模型 | gpt-5.6-luna，经现有私有gateway配置；原始产物与语义核对分开保留 |
| 浏览器 | 桌面及375px；关闭原tab后新tab补读、部分确认、文件与原生记录 |
| 候选镜像 | 本地构建，不推送镜像仓库，不改生产Compose版本 |
| 生产部署及正式员工 | 未执行、未验证；不表示正式上线 |

原始模型报告、合成验收文件与截图仅保存于本机`docs/guides/releases/evidence/r1/`，不进入公开源码或发布包。下文原始证据链接用于本地审阅；可发布汇总见[门禁与镜像记录](2026-10-21-r1-verification.json)。

## 场景与证据

原始1.0.0办公包中，CSV成功，XLSX因模型重新追问已给出的列名而没有真实成果，平台正确判失败。
该失败及授权历史保留在初轮记录（本机私有路径：`evidence/r1/real-office-v100-initial.json`）。补入XLSX表头检查脚本及完整目标传递后，
普通办公包1.0.1的CSV、XLSX分别得到A=12.50、B=3.00、总计15.50；下载后重新校验摘要、来源行号和独立XML解析XLSX。

会议材料同时包含明确本人承诺、明确日程/站内提醒、不确定日期、讨论日期与其他同事任务。模型先保存纪要与行动项JSON，
本人编辑第一条标题、仅确认前两项，权威记录为待办1、日程1、站内提醒1；第三项未创建，未替其他同事分派。
引用区间逐字符核对原文，不确定日期保持null；讨论日期未被当成交付截止日期。

初轮1.0.1原始记录见real-office.json（本机私有路径：`evidence/r1/real-office.json`），本人确认后的HTTP权威记录及独立判断见
meeting-confirmed.json（本机私有路径：`evidence/r1/meeting-confirmed.json`），用量见owner-usage.json（本机私有路径：`evidence/r1/owner-usage.json`）。
中间复验记录保存到`evidence/r1/final-source/`；最终冻结源码另存`evidence/r1/release-source/`，其中source_sha256逐文件固定实际启动的源码，避免覆盖初轮证据。

![部分确认后的桌面结果](evidence/r1/release-source/browser/confirmed-desktop.png)

![375像素结果](evidence/r1/release-source/browser/confirmed-mobile.png)

## 故障矩阵

| 故障/边界 | 检查结果与证据入口 |
| --- | --- |
| 双击与同请求改内容 | 同请求只建一次；改内容409，见work_tasks/test_api.py及test_store.py |
| HTTP响应断线 | ASGI发送202时注入ConnectionResetError，独立执行继续并保存2份真实成果，见test_disconnect.py |
| 页面关闭/刷新 | 原tab运行时关闭；新tab读取持久任务与成果；375px刷新后保持权威记录，无横向溢出 |
| 身份切换/内容隔离 | 浏览器JS行为测试拒绝迟到响应；真实HTTP他人与管理员读取任务/成果404 |
| 员工停用/关闭 | 停用先等任务与容器收尾，内容403；关闭与决定认领互斥，见test_disconnect.py、test_registry.py |
| 模型超时 | 供应商边界注入TimeoutError，任务失败、无成果、输入和执行资源清理；不冒充真实供应商超时 |
| 预算耗尽 | 实际日用量达到配额后拒绝执行，无成果，见test_disconnect.py |
| 命令超时 | 实际Docker公共工具缩短时限，超时返回错误且进程不存在，见test_tools.py |
| 输出越界/链接/交换 | 平台manifest规则及适配层真实导出边界测试；先校验全部再上传 |
| 对象存储/索引提交失败 | 第二份真实对象put或最终索引提交注入故障；恢复已有上传索引，不新增文件、不重放执行，见test_artifact_recovery.py |
| 源版本/决定版本冲突 | 旧来源/旧CAS拒绝；省略与显式清空时间不能共享决定标识，见test_meeting_actions.py |
| 提醒配额/时间无效 | 100条真实活动提醒导致整批回滚；释放额度后同决定成功一次；过去时间/倒置日程均拒绝，见test_native_faults.py |
| 写结果未知 | 先持久待核实标记再执行；无确定回执的取消为outcome_unknown，见test_registry.py及R0回执测试 |
| 批准时并发满 | 返回409，保留waiting_user且不消费决定；额度释放后同请求恢复一次 |
| 装配清理失败 | 即使尚未移交句柄仍保留资源责任及输入，终态重启也清理；不能因缺句柄标记退出 |
| 重启后的本人决定 | 等待工具批准的任务保留，旧租约释放；明确拒绝后才继续处理，实际命令没有启动，见test_restart.py |
| 活动会话额度 | 最终执行收尾后活动会话计数0，挂起才保留，见test_runner.py |
| 实际强杀重启 | 独立宿主SIGKILL后已知Docker容器仍在；新进程清除容器/输入、保留原文件、结算interrupted，模型调用0，见test_restart.py |
| 模型禁用 | 真实HTTP仍可查询、下载、修改既有本人待办及取消新交办，见release-source/meeting-confirmed.json；与对话采样分开 |

这些故障证据包括受控模型、边界异常注入和实际后端；并不声称每种故障都在真实模型供应商上触发。
强杀重启只证明R1的中断结算和资源回收，不能标成R2自动续跑。

## 复现与发布边界

使用仓库门禁`uv run pytest`、`uv run ruff check --select F,S108,I,TC packages`、`uv run mypy packages/*/src`、
`uv lock --check`。适配包另跑全量pytest及自身lint/mypy。真实模型脚本为`scripts/acceptance/r1_office.py`，
只读取私有凭据文件；原始输入、回执和下载文件进入证据目录，凭据不进入仓库。

候选服务/控制台由`docker/deploy.sh`的OUTPUT_MODE=load构建，临时白名单上下文只含wheel、带hash依赖和明确构建文件。
办公镜像独立构建，依赖只在执行镜像，禁止运行时安装。具体镜像ID、依赖、摘要及最终门禁见[机器可读记录](2026-10-21-r1-verification.json)。服务镜像linux/amd64，办公执行镜像linux/arm64；本次分别在对应兼容执行环境验收，未声称双架构部署完成。

上线前仍须按[Docker部署](../docker-deployment.md)备份数据库、对象与配置，再独立部署并留生产回执。回滚先停受理、
结算在途工作，再切回旧镜像；新增表、来源及成果对象保留，不盲目降库或删除。镜像构建通过不代表生产可用。

当前生产Compose仍走local/bubblewrap且没有Docker CLI/执行节点接线，本次不把候选服务镜像验证当作生产办公部署完成。
生产启用前须完成[执行部署接线](../docker-deployment.md#r1办公执行的部署边界)与现场验收，不挂宿主Docker socket。

冻结复验还保留了一次会议引用拒绝案例：三条原文均唯一存在，第三条字符偏移由模型算错，
见`evidence/r1/anchor-rejected-source/real-office.json`及原始文件。修复后仅由服务端为唯一完整引用确定位置；
不存在或歧义引用仍拒绝。原始模型产物不覆盖，确认卡使用独立核对后的引用；相应双后端失败回归已覆盖。

最终冻结复验的权威记录（本机私有路径：`evidence/r1/release-source/meeting-confirmed.json`）独立解析原文日期和时区，核对实际待办截止、日程起止与提醒时间；同一决定重放后数量不变。
DOCX正文、文字PDF实际提取及无文字PDF拒绝另由办公镜像测试覆盖；不把这三项称为额外真实模型验收。

## 最终门禁

`uv run pytest`：**1834 passed，3 skipped**（169.16秒）；`uv run ruff check --select F,S108,I,TC packages`通过；`uv run mypy packages/*/src`通过（164份源码）；`uv lock --check`通过（103个包）。
适配包完整测试：**291 passed，17 skipped，1 xfailed**；自身Ruff和mypy通过。跳过含平台与权限条件，既有严格xfail对应macOS同用户进程参数隔离限制。
平台3项跳过属于memory后端不适用的数据库约束，实际PostgreSQL对应项已通过。原始模型失败案例、全部产物与语义核对单独保留。

提交收尾只清除了一个控制台脚本行尾空格及测试文件尾部空行；前端6项复验通过。最终候选镜像与检出178份源码摘要一致，真实模型启动源码其中177份完全一致，余下一份仅该行尾空格不同，原始摘要保留可核对。

后续正式Linux执行接线、部署和真实验收见[R1生产部署与验收](2026-10-21-r1-production.md)，不以本记录替代生产证据。
