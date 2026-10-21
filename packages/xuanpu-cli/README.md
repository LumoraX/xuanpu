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
