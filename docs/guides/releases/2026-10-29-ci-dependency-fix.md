# 主线CI依赖版本修复（2026-10-29）

初次主线推送bd37b9f的[CI](https://github.com/LumoraX/xuanpu/actions/runs/40827037809)失败：测试矩阵停在uv.lock检查，静态任务缺少SandboxOperationOutcomeUnknownError、DockerEnvironment.remove和export_files。CI未进入全量测试，不能将本地全量结果当成GitHub通过。

根因是工作流浮动检出taifeng-sandbox远端main（abfed410fcfb90a6c5e318541297049d653890cf，版本2026.9.29.15），而本地完整验收依赖36322dd68fbaf51c7a8a87a77a1ec38b80d109bf（版本2026.10.21.1）。四个既有修复提供清理重试、启动取消回收、固定目录有界成果导出及超时结果未知语义；未在本次重写适配器或内核。

将已验证的依赖提交发布到taifeng-sandbox的ci/xuanpu-runtime-20261029专用分支，不改变该仓库远端main。xuanpu测试及lint两个job都固定该sandbox SHA和stores的086f212be2867f311f6ad59673b8f521d8ea85fe，避免依赖浮动与锁文件漂移。主线运行时代码、taifeng内核和uv.lock均未更改。

CI新增独立finance_postgres服务、finance_test_ci数据库及对应Docker容器ID，平台仍使用原pgvector服务和test库。财务测试不再因缺少FINANCE_TEST_DSN而整体跳过；备份恢复直接使用专属财务容器。全量继续执行冻结依赖，超时设45分钟以覆盖故障租约等待，不缩减测试或业务断言。没有增加部署步骤、正式供应商动作或真实模型配置。

本地核查：干净目录使用远端旧依赖时uv lock --check失败；替换固定sandbox提交后通过。sandbox全量291 passed、17项平台条件skip、1项xfail；Ruff通过、mypy22 source files通过。xuanpu原合并源全量3979 passed/21条件skip，运行时代码未变，本次未重复旧组合全量；冻结依赖的Ruff与mypy489 source files均通过。最终Linux/Python3.12与3.13结果以修复提交对应的GitHub Actions为准。

不部署、不支付，财务独立存储与真人审批保持。本轮不运行财务业务网关模型，原218/240计数不扩大。


## Linux完整运行暴露的第二批问题

[980d169的实际CI](https://github.com/LumoraX/xuanpu/actions/runs/40834481674)静态与锁检查通过，但Python3.13全量为180 failed、3785 passed、21 skipped，财务用例无失败；Python3.12超时取消。失败集中于本机沙盒缺少Python导出的F_ADD_SEALS常量，以及rootful容器默认UID 0在cap-drop ALL后不能访问宿主用户的私有目录，造成会话启动、工具读取与办公任务级联失败。没有删除这些用例或放宽原断言。

Sandbox依赖提交15aefcb21ecb6498ae66b59436d2a5142e3fc127使用Linux UAPI固定封口常量兼容缺失符号，继续实际内核封口、失败关闭fd并传播异常；Linux专项从6 failed/2 passed到8 passed，并验证不可写入、扩缩容和追加封口。宿主全量291 passed/25平台条件skip/1既有xfail，Ruff及mac/Linux目标mypy通过。

平台增加显式sandbox.user数字身份配置，默认None维持镜像及rootless语义；Linux rootful测试使用宿主UID/GID。受控Linux私有卷验证：默认UID和其他UID均被拒绝，匹配1001:1001可实际读写，0700目录与0600输入权限不变；专属卷和容器已清理。新增配置传递与权限保持回归先3 failed，再85 passed相关回归。CI增加先行Linux沙盒预检，防止同类启动问题等待整套运行才发现；全量仍保留全部用例并输出耗时和超时栈诊断。


## 次版本解释器别名与夹具遗漏

[8c7c117预检](https://github.com/LumoraX/xuanpu/actions/runs/40840743581)为4 failed/21 passed，及时阻止全量：本机解释器ENOENT，以及独立office_worker直接构造DockerSandboxConfig时未传UID。后者与已修复的私有目录问题相同，补充该夹具的显式身份即可，不改权限。

解释器缺失已在Linux实际复现：uv按3.13次版本创建的venv先链接cpython-3.13-linux-…别名，再指向cpython-3.13.18-…版本目录。原fixture仅授权sys.prefix及sys.base_prefix解析后的目录，别名在空根沙盒中缺失。只在测试确实使用uv管理解释器时，加入setup-uv显式指定的UV_PYTHON_INSTALL_DIR专用只读根；系统Python不挂载无关目录，全盘、顶层目录、用户家及临时根拒绝。保留原venv命令及依赖，不将其改成基底Python，不扩大生产沙盒策略。真实Linux验证中原命令成功且sys.prefix仍为venv，专用目录之外的秘密文件不可见。新增回归先3 failed，根目录/别名与相关预检一起复验；sandbox源码仍15aefcb，无新增依赖修改。
