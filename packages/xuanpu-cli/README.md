# xuanpu-cli

`xuanpu` 命令行。业务方用它校验、打包业务包和跑评测；平台管理员用它生成凭据、迁移表结构、启动服务。

装了本包就同时装上了平台服务、Python 客户端和控制台。

## 命令

| 命令 | 谁用 | 用途 |
| --- | --- | --- |
| `xuanpu validate <目录>` | 业务方 | 校验业务包 |
| `xuanpu pack <目录> -o <文件>` | 业务方 | 校验并打包业务包 |
| `xuanpu schema` | 业务方 | 打印业务包清单的 JSON Schema |
| `xuanpu eval --package <名>` | 业务方 | 在平台上运行业务包的评测用例 |
| `xuanpu keygen root\|master\|metrics` | 平台管理员 | 生成根凭据、主密钥或指标抓取令牌 |
| `xuanpu migrate --config <文件>` | 平台管理员 | 迁移控制面的表结构 |
| `xuanpu serve --config <文件>` | 平台管理员 | 启动平台服务 |

退出码：`0` 成功；`1` 失败（校验不通过、评测有用例未通过、配置不合法、表结构迁移失败等）；`2` 用法错误。

### `validate`

```bash
xuanpu validate ./support-desk
# support-desk 1.0.0：校验通过
```

检查清单本身是否合法，以及清单与包内容是否对得上（入口存在、skill 引用的连接器工具有效、OpenAPI 文档可解析）。「提醒」打到标准输出，不影响退出码；「错误」打到标准错误，有任何一条就以 `1` 退出。

本地校验通过不等于一定能发布：发布时平台还会让内核实际加载一遍 skill。

### `pack`

```bash
xuanpu pack ./support-desk -o support-desk.zip
# support-desk 1.0.0：校验通过
# 已写入 support-desk.zip（3254 字节，sha256:71887b9e…）
```

`-o` / `--output` 是输出文件，必填。先校验，通过了才打包。打包是确定性的：内容相同，归档逐字节相同，打印出来的 SHA-256 就是发布后的版本指纹。

### `schema`

```bash
xuanpu schema > xuanpu.schema.json
```

把 JSON Schema 接进编辑器，写 `xuanpu.yaml` 时就有补全和即时校验。

### `eval`

```bash
xuanpu eval --key $KEY --package support-desk
# support-desk 1.0.0：2 个用例，0 个未通过，共 912 token
#   [通过] greet  completed  456 token  86 ms
#   [通过] refund-window  completed  456 token  84 ms
```

| 参数 | 说明 |
| --- | --- |
| `--package` | 业务包名，必填 |
| `--version` | 版本；不给用生效版本。用它在切换生效版本之前先测新版本 |
| `--case` | 只跑这个用例，可以重复给多个 |
| `--url` | 平台地址，默认 `http://127.0.0.1:8420` |
| `--key` | 租户管理员的 API key；不给则读环境变量 `XUANPU_API_KEY` |

没通过的用例会逐条列出原因。有用例未通过时退出码是 `1`，可以直接放进发布流水线。评测会真的调用模型与连接器，计入用量。

### `keygen`

```bash
xuanpu keygen root      # 根凭据（只显示一次）与它的摘要；摘要写进配置的 root_key_hash
xuanpu keygen master    # 一把主密钥（base64）；写进配置的 master_keys
xuanpu keygen metrics   # 指标抓取令牌（只显示一次，交给 Prometheus）与它的摘要；摘要写进配置的 metrics.token_hash
```

```bash
xuanpu keygen metrics
# 指标抓取令牌（只显示这一次，交给 Prometheus）：
#   <令牌>
# 写进配置文件的 metrics.token_hash：
#   <摘要>
```

### `migrate`

```bash
xuanpu migrate --config xuanpu.yaml
# 已执行 0001_control_plane
# 已执行 0002_tenant_concurrent_turns
# 已执行 0003_session_metadata
# 已执行 0004_identity
```

把控制面的表结构迁到最新。可以重复运行，已是最新时什么都不做。每次升级平台版本前先跑它。控制面用内存存储时没有表可迁。

### `serve`

```bash
xuanpu serve --config xuanpu.yaml
```

启动平台服务，直到收到终止信号。控制台挂在 `/console/`。停机过程见[部署与运维](../../docs/guides/operations.md#单实例运行与停机)。

## 配置文件与环境变量

`migrate` 与 `serve` 读同一份配置文件，各项含义见[部署与运维](../../docs/guides/operations.md#配置)。

配置里的 `${NAME}` 用进程的环境变量展开，引用了没设置的变量会报错。命令行是平台里唯一读环境变量的地方：

| 环境变量 | 用在哪 |
| --- | --- |
| 配置文件里 `${…}` 引用到的任何变量 | `migrate`、`serve` |
| `XUANPU_API_KEY` | `eval`，没给 `--key` 时 |

## 本人后台工作和执行安排

`xuanpu work-tasks`、`xuanpu task-definitions`使用组参数`--credentials`读取私有JSON，字段为`base_url`、`api_key`、`act_as_user`。该文件含凭据，应仅本人可读；命令行没有覆盖租户或员工身份的参数。默认输出本人摘要，不能依靠管理员身份查看其他员工私有任务。

work-tasks提供list/get/events/create/capability/cancel/decide/reconcile；task-definitions提供list/get/create/update/pause/resume/cancel/occurrences。写操作的`--body`指向严格请求JSON，保留原request_id和版本；网络结果不确定时重发同一文件，不生成新标识。分页使用`--limit`及`--after`，按服务端游标继续补读。安排创建/修改须confirmed=true；模型草稿不能充当确认。回执核实只接受已登记的权威查询，不重试原写操作。

两个组的`--help`及各子命令`--help`可直接查看当前参数，无需模型或网络。CLI仅在原生HTTP接口上封装操作，关闭R2后读取、暂停、取消和已有成果仍使用兼容reader。

## 本人的企业只读连接

`xuanpu connections` 使用 `--credentials` 指向权限600的私有JSON档案，字段仅`base_url`、`api_key`、`act_as_user`。不提供tenant/user/key命令行覆盖：`list`列本人连接；`read <identifier>`读本人基础资料；`revoke <identifier> <consent_id> --request-id <稳定请求> --expected-version <当前版本>`撤销；`confirm`只显示浏览器确认地址，不创建或扩大同意。个人同意必须由员工本人登录浏览器确认，API key及act-as不能代替。命令已通过帮助和行为测试；真实企业接入按服务端验收记录判断。


## 组织与职责

`xuanpu organization --credentials <权限600的私有JSON档案>`使用固定调用身份。支持me、directory、list/get/create/change（集合为units、positions、memberships、roles）、authority、preview-authority、apply-authority、preview-import、get-import、apply-import。写操作用`--body`保留request_id、expected_version及明确selected_changes；分页用`--limit`和`--cursor`。目标user_id不是调用身份覆盖，服务端仍按组织范围授权。

## `xuanpu shared`（R3 候选）

同一员工的共享事项、材料及来源阅读。新增能力尚未生产发布，CLI 不会自动确认或指定他人身份。
`--credentials` 为权限600的 JSON，字段仅 `base_url`、`api_key`、可选 `act_as_user`（密钥须已有相应委托权）；不要把凭据放进命令参数或输出。

已实际检查帮助：

```bash
xuanpu shared --help
xuanpu shared --credentials private.json download --help
```

子命令 `list/get/create/change/members/invite/revoke/transfer/events` 管理当前可见事项；
`materials/publish/publish-text/publication/upload/download/withdraw` 管理固定共享副本；
`context-list/context-create/context-get/context-read/context-close/context-ask` 管理本人独立来源阅读；
`notifications/read-notification` 读取来源通知。通知不向第三方发送。

写操作的 `--body` 文件保存明确的 `request_id`、`confirmed=true`、`expected_version` 及该动作字段；网络重试重用原文件，409后重新核对。
创建不要求 `expected_version`；context-ask使用 `request_id/text`，不接受另传身份、租户、来源正文或主机路径。
`--limit/--cursor/--search` 为列表参数；notifications用 `--after-id`。
撤销成员用 `--member`；publication用 `--publication`；withdraw/download用 `--material`；
下载还需明确 `--revision`、新路径 `--output`，经20MiB和SHA验证后创建权限600文件，拒绝覆盖已有文件。
上传用 `--file`，确认文件中包括名称、事项版本及操作号，实际字节有界读取；publish指明本人文件的固定revision，不追随最新版。

SDK经合成租户的正式ASGI/认证/双后端测试验证，真实员工使用及生产发布另行记录。

### 到人交接和组织审批候选

`xuanpu collaboration --credentials private-employee.json list --view received` 查询本人收到的工作；`--view sent|assigned|department_queue` 分别为发出、承办及可选部门队列。无需先配置部门。

命令使用权限600的固定员工凭据文件（base_url、api_key、可选act_as_user）；有权API key代表员工时，服务端仍检查当前员工及委托权限。写入的`--body`同样须为权限600的JSON文件，明确原request_id、confirmed及版本；断线重试使用同一文件，冲突后必须重新核对。不得通过body覆盖tenant、actor或调用身份。

| 命令 | 用途 |
| --- | --- |
| list/get/submit | 本人交接列表、当前详情、原生确认提交 |
| accept/start/return/request-information/provide-information | 接收、开始、退回、请求补充及明确新输入 |
| transfer/cancel/request-cancel/confirm-cancel | 明确转交、未接受取消及接受后双方取消确认 |
| result-upload/result-text/result-publication | `--file`上传本次新成果、固定本人助手消息发布、补读发布状态；上传body含name、mime_type、shared_version及expected_version |
| policies/policy-create/policy-change | 可选规则名；`--admin`完整规则、创建及追加修订需要企业级管理资格 |
| approval-list/approval-get/approval-submit | 本人参与的组织审批及固定目标申请 |
| approve/reject/approval-return/approval-withdraw | 当前步骤决定或申请人撤销，不授予个人工具权限 |
| deliver/sign-off/dispute/reopen | 固定成果交付、精确delivery_version签收、争议及重新办理 |
| notifications/read-notification | 当前本人站内动态及幂等标读 |

上传不提升整事项编辑权。组织批准、承办交付和发起人签收分别记录，不能用后台任务成功或模型文字代替。当前为未部署候选；完整功能与生产验收进度以发布证据为准。

## 能力目录与安装（R4-A）

`xuanpu capabilities --credentials <私有JSON> list|mine|register|install-preview|install|state`使用公开SDK。写入传`--body`文件；install-preview/install/state还需精确记录编号。凭据文件权限须仅当前用户可读，含base_url/api_key及可选act_as_user；拒绝正文覆盖身份。保留原操作号重试，confirmed必须由调用者明确提供。当前不激活或分配运行；[手册和实际本地命令](../../docs/guides/capability-delivery.md)。
