# 能力登记与安装预检

管理员可以登记交付说明、保存“待发布”安装（R4-A），把能力分配给员工（R4-B），再经隔离评测把通过证据的版本发布给固定的员工名单（R4-C）。员工不会因为管理员完成配置而自动获得能力：既被分配、又在某次生效发布的名单里，才出现在“我的能力”与对话里。

## 管理员操作

1. 先按现有业务包流程上传精确版本的归档。交付方另提供delivery.json，声明包摘要、入口、材料类型、成果、工具权限和本地配置槽，不在文件里填写Secret或员工资料。
   如果这个业务包已经有员工在用（还没登记为能力），先看排空预览：`GET /v1/admin/capability-drain?package=<包名>`（SDK `capability_drain_preview`）。包的任一入口登记为能力后整包纳管，预览里数出的进行中会话（`unbound_sessions`、`unbound_task_sessions`）续跑会被拒。先让员工结束或等它们完成再登记；预览不统计定时安排，`truncated` 为 true 时数量不全。
2. 登录原生工作站，进入“能力配置”，选择“交付说明文件”，核对标题及版本，再点“确认登记说明”。登记只保存不可变说明。
3. 在说明卡点“配置并预检”。填写声明的公开参数，秘密只从已配置凭据名称选择，企业连接只从已有启用连接选择。没有可用凭据时不能输入秘密值替代。
4. 点“检查安装条件”。预检会启动声明的工具服务并列工具，但不会调用业务工具或模型。核对输入范围、预算、出网、工具和连接范围、审批等级，以及与此前安装权限的差异。
5. 条件齐全后点“确认保存安装配置”，结果显示“待发布”。刷新会保留配置状态。不要把待发布当成员工已可运行。
6. “停止此安装”仍需核对并确认；保留旧说明、配置与历史，不删除工作记录。A不提供发布为可运行的按钮。

网络中断可重试同一次提交，操作号不变。预检后输入控件锁定，修改配置须重新打开“配置并预检”建立新请求；409须刷新后重新核对，不覆盖别人提交的配置。预检默认15分钟过期。

员工从“功能→我的能力→查看我的能力”读取本人目录，当前明确显示尚未分配。管理员登记目录不会展示给普通员工；私人“个人能力”仍是本人工作方法。

## 分配给员工（R4-B）

- 直接到人是默认，不需要部门或岗位：选择员工及其当前代次（`user_epoch`，员工停用或重建后旧分配不再生效），列出明确的能力修订号，可设到期时间。也可以按岗位或部门分配，须引用真实存在的结构及当前版本；员工有对应的真实任职才生效。
- 分配只授权使用这个能力，不授予读文件、使用企业连接或审批的权限。撤销须带当前版本，撤销后员工的下一次调用即被拒，正在进行的会话或任务也不能再继续；已撤销的分配不能恢复，需要时重新分配。
- 已登记为能力的业务包不能再用“激活版本”或“发布并激活”上线，须走能力发布流程（R4-C）；不激活的上传仍可用。纳管之前已经开始的会话或任务没有能力依据，纳管后不能继续，请先让它们收尾。
- 原生分配界面尚未提供，目前经HTTP接口、SDK或命令行操作。

## 评测与发布（R4-C）

1. 评测：提交与交付描述声明摘要一致的评测套件（`xuanpu-eval-suite/v1`，最多50个用例），指定要评的安装。评测在后台用正常会话路径逐个用例运行，按实际回复判定；声明了企业连接或写入工具的能力本版本不能在隔离环境评测，会直接显示“外部实现未验证”。
2. 预览：选定通过的评测与员工名单（最多100人，按员工当前代次），预览会列出每人当前与目标版本、需要停止的重叠发布，并给出确认摘要。评测报告7天内有效，安装配置改过须重新评测。
3. 发布：带上预览摘要与要停止的重叠发布明确确认。期间状态有变会要求重新预览。首次发布后安装变为可运行。
4. 停止与撤销：正常停止只拒绝新的使用，进行中的工作可以收尾；安全撤销后进行中的工作也会被拒。回退就是用旧的仍有效的配置与评测再发布一次。

## SDK和命令行

SDK新增my_capabilities、list_capabilities、register_capability、preview_capability_install、install_capability、change_capability_installation，分配用的assign_capability、list_capability_assignments、revoke_capability_assignment，以及评测与发布用的evaluate_capability、get_capability_evaluation、preview_capability_release、release_capability、list_capability_releases、change_capability_release。方法接受与HTTP相同的正文，不自动补request_id或confirmed；没有员工身份的机器凭据不能调用my_capabilities。

命令为`xuanpu capabilities --credentials <私有JSON> list|mine|register|install-preview|install|state|assign|assignments|revoke|evaluate|evaluation|release-preview|release|releases|retire-release|revoke-release`（编号作参数，正文经--body文件；分配、评测与发布子命令本次未补充实际运行示例）。凭据文件仅当前用户可读，字段为base_url、api_key及可选act_as_user，禁止把密钥放命令参数或写入交付描述。写入正文使用--body文件，重复提交原文件保留操作号。错误不回显正文或凭据。

下面命令在本机回环合成夹具实际执行过。目录和登录入口只用于开发验收，不能接生产；运行夹具会创建合成包、员工和临时密钥，无需真实模型或员工资料：

```sh
uv run python scripts/acceptance/r4_browser_fixture.py --directory /tmp/xuanpu-r4a-browser-20261025 --port 8844
```

另一个终端可执行已验证的查询：

```sh
uv run xuanpu capabilities --credentials /tmp/xuanpu-r4a-browser-20261025/cli-credentials.json list
uv run xuanpu capabilities --credentials /tmp/xuanpu-r4a-browser-20261025/cli-credentials.json register --body /tmp/xuanpu-r4a-browser-20261025/register.json
```

register.json需由调用者准备`{"request_id":"固定操作号","delivery":{...完整交付描述...}}`；夹具生成delivery.json，但不自动确认登记。install-preview正文包含request_id、revision及需要的public_config/secret_refs/connection_refs；install正文包含request_id、preview_id、expected_version、confirmed=true。state正文包含request_id、expected_version和draining或disabled。三项命令均以服务端返回的精确编号及版本为准；实际执行证据见[验收记录](releases/2026-10-25-r4a-acceptance.md)。

## 运维准备

capability_delivery.enabled默认false，tenants默认空，须同时开启并明确列出租户才接受新登记、预检和安装。关闭后查询和停止仍可用。完整配置与迁移边界见[部署运维](operations.md#r4-a-能力目录与安装)。当前源码新增0020，尚未部署正式服务器；升级须停写备份、迁移并整体切换兼容reader，不能混跑旧R3进程。
