# R4-E 首个可复制办公样板实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans，在C/D技术门禁后实施。

**Goal:** 将“办公材料汇总与交接复核”做成能安装、配置、分配、评测和回退的完整样板，并在两个隔离企业复制验证。

**Architecture:** 复用现有office-assistant的文档/表格脚本，能力包只声明Skill和进程外执行。准备、固定材料、办公任务、成果查看和交接分别使用现有领域服务，不新建业务工作流引擎。路径和夹具见[总计划](2026-10-25-r4-delivery.md)。

**Tech Stack:** xuanpu/v1包、独立delivery描述、现有沙盒、R1/R2任务、R3交接入口、pytest与原生网页。

---

## 样板输入输出与验收

首期输入：本人明确上传的UTF-8 CSV，补充说明写在任务目标文本；不混输TXT/Markdown附件。办公任务使用固定revision/SHA。样板输出为`summary.csv`、`summary.xlsx`、`review.md`、`evidence.json`四类，匹配现有table_summary的summary_csv/summary_xlsx/report/evidence角色，另写outputs/manifest.json。记录输入引用、行数、分组汇总、缺失值/异常行及生成依据。金额统一以字符串Decimal、固定两位小数计算，禁止模型算术作为唯一依据。

合成确定输入固定为列`项目,金额`、三行`甲,120.00`、`乙,80.00`、`甲,50.00`；预期甲170.00、乙80.00、总计250.00、有效行3。异常样本含缺金额、非法数字与重复行；必须明确标异常，不静默归零或去重。重复是否合并由样板配置确认，默认保留并提示。

知识流程说明通过D受保护阅读单独展示，不自动拼入R2提示或沙盒；不从共享知识拷贝正文当本人文件。首期办公计算输入只来自明确本人文件。成果生成后本人查看并决定是否进入R3交接；审批、交付和签收仍由实际有权人原生确认。样板不宣称“生成文件=业务已签收”。

样板配置限定`report_title`、`group_column`、`amount_column`、`currency_label`、`deadline_policy`及可选的本地审批policy引用；身份和部门不打包。复制到另一租户必须重新确认本地成员/规则、分配、模型与执行条件，源租户审批ID直接复制应404。

## E0：完整交付件和确定业务校验

**Files**：Create `examples/office-review/{delivery.json,README.md}`、`examples/office-review/package/xuanpu.yaml`、`examples/office-review/package/skills/review/SKILL.md`、`examples/office-review/package/skills/review/scripts/review_rows.py`、`examples/office-review/evals/{suite.json,inputs/basic.csv,inputs/invalid.csv,expected/basic.json}`、`T/r4_contract/{test_office_review_package,test_office_review_results}.py`；Modify `examples/README.md`。

- [x] 只将`examples/office-review/package/`交给现有pack_package；delivery.json和evals为同级外部交付件，不进入被引用ZIP。先构建包再把其digest写入外部delivery，测试重复构建ZIP逐字节相同、修改外部delivery不改变包SHA，避免自引用摘要。
- [x] 包/交付描述通过现有规范与A解析，固定B2的office_files/table_summary profile，不把模型选择当完成类型；默认关闭出网、不声明任何外部写工具。
- [x] 抽取复用现有表格读取与成果保存工具，不复制平台身份/路径逻辑到脚本。自定义脚本在既有沙盒运行，不被xuanpu-server import。
- [x] 编写确定输入的数值、行数、异常清单、CSV安全导出、UTF-8和SHA断言；表格单元格含公式前缀时输出保留原始值及安全显示策略，不能当公式执行。
- [x] SKILL明确只做草稿/准备和有证据的状态描述，不从材料指令扩大权限，不替他人创建组织决定。缺字段需要澄清，不擅自选列。
- [ ] 冻结安装条件、输入输出限制、预算、失败代码和处置：invalid_columns要求重新选择；source_stale重选版本；budget_exhausted显式续办；outcome_unknown先核实不自动重跑。
- [ ] Run：`uv run pytest packages/xuanpu-server/tests/r4_contract/test_office_review_package.py packages/xuanpu-server/tests/r4_contract/test_office_review_results.py -q`；提交`feat: 增加可复制办公汇总样板`。

## E1：两企业复制与员工操作旅程

**Files**：Create `scripts/acceptance/r4_repeatable_office.py`、`T/r4_contract/{test_office_replication,test_office_journey,test_office_rollback}.py`、`Q/{r4_office_journey_behavior.cjs,test_r4_office_journey_behavior.py}`；Modify `C/{capabilities.js,work-tasks.js,workspace-chat.js}`及`SDK/capabilities.py`；不新增重复的交接表。

- [ ] 在两个隔离合成租户安装相同归档和delivery，分别使用不同报告标题/本地配置/员工，独立预检、分配和发布。包digest相同、安装配置digest不同、评测各自匹配。
- [ ] 租户一Alice运行，租户二Bob运行；成果250.00及输入SHA都正确；任何跨租户安装/结果/报告/审批/来源ID均404。导出的复制描述只含模板，不含运行数据或秘密。
- [ ] 原生从对话提出需求→本人选文件/列→任务受理→关闭网页/服务进程重启→查看固定成果；重新打开不创建第二个任务，输入变更409要求核对。
- [ ] 在纯合成身份下完整跑R3接收/补充/审批/交付/签收，并留对账记录；真实员工闭环仍暂缓。本轮不由管理密钥模拟陈翔/drYang确认。
- [ ] 发布坏版本被评测阻止；正常升级后新任务用新版、旧任务原版；回退后再新建用旧版而已受理任务不变。模型服务断开时仍能查看、取消、下载和完成合法人工决定。
- [ ] 实际模型单独执行正常输入、缺字段、注入材料和无知识来源四组，每组保存原始报告和人工/确定性判定；确定计算错误即失败，不能以模型说“已完成”通过。
- [ ] Run：`uv run pytest packages/xuanpu-server/tests/r4_contract/test_office_replication.py packages/xuanpu-server/tests/r4_contract/test_office_journey.py packages/xuanpu-server/tests/r4_contract/test_office_rollback.py -q`及全量；实际脚本需指定隔离地址/租户，默认拒绝生产域名。
- [ ] 实施时新增`docs/guides/repeatable-office.md`；提交`feat: 验证办公样板跨企业复制与原生旅程`。

本段交付一个可复制样板，不承诺覆盖所有表格、复杂报销或企业所有岗位。真实部门资料与业务收益等待恢复真人试点后另验。

## 进度（2026-11-18，分支 `feat/r4d-knowledge`）

- E0：`examples/office-review/{package,delivery.json,evals,build.py,README.md}`；样板脚本复用 office-assistant 的表格读取与 Decimal 汇总（复制进样板包，导入路径改为同目录）；测试 `T/r4_contract/test_office_review_package.py`：确定性构建、外部描述摘要、经真实接口登记、basic 样例 250.00、invalid 样例按行号列出异常、公式前缀中和。未做：XLSX 公式单元格的显示策略专门用例（需办公容器）、`test_office_review_results`（四类成果在办公容器内生成与校验）、失败代码 invalid_columns/source_stale/budget_exhausted/outcome_unknown 的冻结与处置文档；评测套件只有对话类用例（R4-C 评测不支持带输入文件的确定性用例）。
- E1（分支 `feat/r4d1-knowledge-qa`）：两企业复制 `T/r4_contract/test_office_replication.py`（内存与 PG）：同一归档摘要、各自本地配置得到不同配置摘要、跨企业的预检/停用/发布与分配列表均 404（发布与分配列表对本企业不存在的能力改为 404）；复制说明写在样板 README。升级/回退旅程 `T/r4_contract/test_office_rollback.py`（升级窗口旧发布照常可用、升级后新会话用新版旧会话不变、回退后新会话回旧版）——这条旅程暴露并修正了两处问题：换配置把安装打回 staged 导致升级期间全员不能新受理；发布绑定安装当前配置导致无法回退。未做：两企业各自跑出 250.00 成果（需办公容器）、原生旅程、R3 交接、真实模型四组。

## 进度（2026-11-19，分支 `test/office-review-results`）

- E0 成果用例：新增 `T/r4_contract/test_office_review_results.py`，在办公沙盒镜像里实际运行样板（需要 Docker）。脚本层 3 项：`basic.csv` 得甲 170.00、乙 80.00、总计 250.00、有效行 3，`summary.csv`、`summary.xlsx`、`report.md`、`evidence.json` 与 `outputs/manifest.json` 齐全，脚本回执 `outputs_sha256` 与文件逐项一致，证据记有输入 SHA；分组文本以 `=` `+` `-` `@` 开头时 XLSX 里是字符串单元格、公式单元格数为 0，CSV 加单引号，证据保留原值；`invalid.csv` 非零退出、按文件与行号列出第 3、4 行、不生成清单。平台层 3 项（内存与 PG 各一遍）：管理员经产品接口发布、登记、预检、安装、评测、分配、发布，员工上传 CSV 经 `POST /v1/work-tasks/from-capability` 交办，任务成功并保存四类成果，回执的大小、摘要与下载字节一致；`invalid.csv` 的任务以 `source_or_output_invalid` 失败、没有成果、模型看到的脚本输出列有文件与行号。
- E1 两企业运行：同一归档在两家合成企业各自安装、评测、发布给各自员工，各自跑出 250.00；对方员工与管理员读取任务、任务事件、成果文件与输入文件标识一律 404，各自任务列表只有自己的任务。
- 这条用例暴露并修正一处缺陷：成果摘要回执原先只认名为 `table-summary` 的 skill（`TaskRunner._script_receipt`），按文件型能力登记的样板入口 skill 是 `review`，脚本跑完也会在成果校验处以「缺少真实汇总脚本回执」失败——样板此前无法经能力入口跑出成果。现改为认「该能力登记的入口 skill 自己的 `summarize`」（内置办公包的 `table-summary` 不变），并补了不需要容器的判定用例（`test_file_capability_tasks.py`，5 组）。契约写入 `docs/architecture/capability-delivery.md` 与 `docs/guides/capability-delivery.md`。
- 办公镜像夹具 `office_image` 上移到 `T/conftest.py`，`work_tasks`、`task_triggers`、`r4_contract` 共用；没有可用容器时新用例与既有办公容器用例以同样的原因失败。
- 仍未做：失败代码 invalid_columns/source_stale/budget_exhausted/outcome_unknown 的冻结与处置文档；跨企业的审批与知识来源标识 404、复制描述导出不含运行数据的专门断言；原生旅程、R3 交接、真实模型四组；评测套件仍只有对话类用例。

