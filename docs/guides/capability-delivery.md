# 能力登记与安装预检

当前是R4-A。管理员可以登记交付说明并保存“待发布”安装；人员分配、业务运行和评测发布尚待R4-B/C。员工不会因为管理员完成配置而自动获得能力。

## 管理员操作

1. 先按现有业务包流程上传精确版本的归档。交付方另提供delivery.json，声明包摘要、入口、材料类型、成果、工具权限和本地配置槽，不在文件里填写Secret或员工资料。
2. 登录原生工作站，进入“能力配置”，选择“交付说明文件”，核对标题及版本，再点“确认登记说明”。登记只保存不可变说明。
3. 在说明卡点“配置并预检”。填写声明的公开参数，秘密只从已配置凭据名称选择，企业连接只从已有启用连接选择。没有可用凭据时不能输入秘密值替代。
4. 点“检查安装条件”。预检会启动声明的工具服务并列工具，但不会调用业务工具或模型。核对输入范围、预算、出网、工具和连接范围、审批等级，以及与此前安装权限的差异。
5. 条件齐全后点“确认保存安装配置”，结果显示“待发布”。刷新会保留配置状态。不要把待发布当成员工已可运行。
6. “停止此安装”仍需核对并确认；保留旧说明、配置与历史，不删除工作记录。A不提供发布为可运行的按钮。

网络中断可重试同一次提交，操作号不变。预检后输入控件锁定，修改配置须重新打开“配置并预检”建立新请求；409须刷新后重新核对，不覆盖别人提交的配置。预检默认15分钟过期。

员工从“功能→我的能力→查看我的能力”读取本人目录，当前明确显示尚未分配。管理员登记目录不会展示给普通员工；私人“个人能力”仍是本人工作方法。

## SDK和命令行

SDK新增my_capabilities、list_capabilities、register_capability、preview_capability_install、install_capability和change_capability_installation。方法接受与HTTP相同的正文，不自动补request_id或confirmed；没有员工身份的机器凭据不能调用my_capabilities。

命令为`xuanpu capabilities --credentials <私有JSON> list|mine|register|install-preview|install|state`。凭据文件仅当前用户可读，字段为base_url、api_key及可选act_as_user，禁止把密钥放命令参数或写入交付描述。写入正文使用--body文件，重复提交原文件保留操作号。错误不回显正文或凭据。

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
