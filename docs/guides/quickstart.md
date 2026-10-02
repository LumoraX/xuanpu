# 快速上手

用仓库里的样例业务包 `support-desk`，在一台机器上走通：启动平台 → 开通租户 → 发布业务包 → 对话 → 审批 → 查审计。全程不需要 Postgres 和 Docker。

样例里的助手会做三件事：调外部接口查工单、在沙盒里跑脚本算处理时限、改工单优先级（这一步要人批准）。

文中的输出是示意：平台返回的字段就是这些，但模型怎么措辞、先调哪个工具、用多少 token，取决于你接的模型。

## 准备

| 需要 | 说明 |
| --- | --- |
| Python 3.12+ 与 [uv](https://docs.astral.sh/uv/) | |
| macOS，或装了 `bubblewrap` 的 Linux | 脚本在操作系统级的沙盒里跑。Linux 还要允许创建用户命名空间 |
| 一个兼容 OpenAI Chat 接口的模型地址 | 要支持流式输出与工具调用。LiteLLM Proxy、各家的兼容端点都可以 |
| 三个仓库检出在同一个父目录下 | `xuanpu`、`taifeng-sandbox`、`taifeng-stores`。后两个还没发布到 PyPI，开发期从同级目录取源码；内核 `taifeng` 从 PyPI 安装 |

```bash
cd xuanpu
uv sync --all-packages --all-extras
```

## 1. 生成凭据，启动平台

```bash
uv run xuanpu keygen root      # 打印根凭据和它的摘要
uv run xuanpu keygen master    # 打印一把主密钥
```

根凭据是平台管理员的身份，只显示这一次；平台只保存它的摘要。主密钥用来加密租户存进来的密钥。

把它们和模型地址放进环境变量。试用配置 [`examples/xuanpu.trial.yaml`](../../examples/xuanpu.trial.yaml) 会引用这些变量：

```bash
export ROOT_KEY='xproot_…'                 # 根凭据
export XUANPU_ROOT_KEY_HASH='…'            # 根凭据的摘要
export XUANPU_MASTER_KEY='…'               # 主密钥
export MODEL_GATEWAY_URL='https://…/v1'    # 模型地址，以 /v1 结尾
export MODEL_GATEWAY_KEY='sk-…'
export MODEL_NAME='…'                      # 模型名；试用时三个档位都用它

uv run xuanpu serve --config examples/xuanpu.trial.yaml
```

另开一个终端确认它起来了：

```bash
curl localhost:8420/healthz
# {"status":"ok","version":"2026.9.30.1","instance":"inst_9c7a5cea5506bdae0d89e8da"}
```

试用配置把控制面放在内存里：停掉进程，租户、凭据、业务包登记、会话、审计都会丢。数据目录 `/tmp/xuanpu-trial` 里的东西留在盘上：业务包归档（`objects/`，本机对象存储）、会话主存（`threads/`）、解包缓存与沙盒工作区；要从头再来，停掉进程后把这个目录一起删掉。各目录是什么见[数据目录](operations.md#数据目录)，正式部署见[部署与运维](operations.md)。

## 2. 启动样例工单接口

样例业务包要调一个「工单系统」。仓库里带了一个只用标准库写的替身，监听 `127.0.0.1:8431`，凭据是 `demo-token`：

```bash
python3 examples/ticket-api/server.py
```

## 3. 开通租户

平台管理员用根凭据建租户，再给它签发一把租户管理员的 API key：

```bash
curl -X POST localhost:8420/v1/admin/tenants \
     -H "Authorization: Bearer $ROOT_KEY" -H 'Content-Type: application/json' \
     -d '{"tenant_id": "acme", "name": "Acme", "quota": {"max_daily_tokens": 5000000}}'

curl -X POST localhost:8420/v1/admin/tenants/acme/keys \
     -H "Authorization: Bearer $ROOT_KEY" -H 'Content-Type: application/json' \
     -d '{"name": "admin", "role": "tenant_admin"}'
# {"key":"xp_be139be29730_…","key_id":"be139be29730","tenant_id":"acme","name":"admin","role":"tenant_admin",…}
```

响应里的 `key` 只出现这一次：

```bash
export KEY='xp_be139be29730_…'
```

从这里开始都用租户的 `KEY`。根凭据不属于任何租户，不能用来发布业务包或对话。

## 4. 配密钥，发布业务包

业务包里不写凭据，只写密钥的名字。样例包的连接器引用了名为 `tickets-token` 的密钥，先把取值存进平台：

```bash
curl -X PUT localhost:8420/v1/secrets/tickets-token \
     -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
     -d '{"value": "demo-token"}'
```

校验、打包、发布并设为生效版本：

```bash
uv run xuanpu validate examples/support-desk
# support-desk 1.0.0：校验通过

uv run xuanpu pack examples/support-desk -o support-desk.zip
# 已写入 support-desk.zip（3254 字节，sha256:71887b9e…）

curl -X POST 'localhost:8420/v1/packages?activate=true' \
     -H "Authorization: Bearer $KEY" --data-binary @support-desk.zip
# {"name":"support-desk","version":"1.0.0","digest":"sha256:71887b9e…","active":true,…}
```

## 5. 跑评测

业务包清单里带了两个评测用例。每个用例开一个会话、发一句话、按期望检查回复：

```bash
uv run xuanpu eval --key $KEY --package support-desk
# support-desk 1.0.0：2 个用例，0 个未通过，共 912 token
#   [通过] greet  completed  456 token  86 ms
#   [通过] refund-window  completed  456 token  84 ms
```

回复由模型生成，换一个模型可能有用例通不过——这正是评测的用处：把期望改到你的模型稳定满足为止，再发布。

## 6. 对话

建一个会话。`metadata` 是你自己的关联标识，平台原样保存并写进审计：

```bash
curl -X POST localhost:8420/v1/sessions \
     -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
     -d '{"package": "support-desk", "metadata": {"customer": "C-42"}}'
# {"session_id":"ses_1197ee27deb1aa819440d23c","package":"support-desk","version":"1.0.0","entry":"triage","status":"active",…}

export SID='ses_1197ee27deb1aa819440d23c'
```

发消息。响应是事件流（Server-Sent Events），`-N` 让 curl 不缓冲：

```bash
curl -N -X POST localhost:8420/v1/sessions/$SID/messages \
     -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
     -d '{"text": "查一下工单 T-1001"}'
```

事件流里能看到模型依次调了外部接口、跑了沙盒脚本、给出回复（下面省略了部分字段与事件）：

```
event: turn
data: {"session_id": "ses_1197…", "submission_id": "sub_2046614c1be2"}

event: tool_call_started
data: {…, "data": {"call_id": "call_1", "name": "api__tickets__getTicket", "arguments": "{\"id\": \"T-1001\"}"}}

event: tool_call_completed
data: {…, "data": {"call_id": "call_1", "name": "api__tickets__getTicket",
       "output": "HTTP 200\n{\"id\": \"T-1001\", \"title\": \"无法登录\", \"priority\": \"normal\", \"status\": \"open\"}", "is_error": false}}

event: tool_call_started
data: {…, "data": {"call_id": "call_2", "name": "run_script", "arguments": "{\"skill_id\": \"triage\", \"script_name\": \"sla\", \"args\": {\"priority\": \"normal\"}}"}}

event: tool_call_completed
data: {…, "data": {"call_id": "call_2", "name": "run_script", "output": "处理时限：24 小时\n", "is_error": false}}

event: assistant_text
data: {…, "data": {"delta": "工单 T-1001（无法登录）当前优先"}}

event: assistant_text
data: {…, "data": {"delta": "级 normal，处理时限 24 小时。"}}

event: turn_completed
data: {…, "data": {"iterations": 3, "usage": {"input_tokens": 1260, "output_tokens": 108, "total_tokens": 1368, …}, …}}

event: end
data: {"submission_id": "sub_2046614c1be2", "overflowed": false}
```

## 7. 审批

让它改工单：

```bash
curl -N -X POST localhost:8420/v1/sessions/$SID/messages \
     -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
     -d '{"text": "把它调成高优先级"}'
```

样例包的审批强度是 `standard`，清单里把 `updateTicket` 标成了写操作，所以这一轮不会执行修改，而是停在审批点：

```
event: turn_suspended
data: {…, "data": {"pending": [{"request_id": "req_7fcbe0950662111f", "reason": "permission",
       "detail": {"scope": "tool_use", "target": "api__tickets__updateTicket",
                  "reason": "调用连接器 tickets 的 updateTicket（write）",
                  "args": {"id": "T-1001", "body": {"priority": "high"}},
                  "arguments_digest": "sha256:ab129ec1…", …}, …}], …}}
```

此时工单还没被改。把批准送回去，对话从停下的地方继续：

```bash
curl -N -X POST localhost:8420/v1/sessions/$SID/resume \
     -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
     -d '{"resolutions": {"req_7fcbe0950662111f": {"granted": true}}}'
# …
# event: assistant_text
# data: {…, "data": {"delta": "已把工单 T-1001 调为高优"}}
```

到工单接口那边看，优先级确实改了：

```bash
curl localhost:8431/tickets/T-1001 -H 'Authorization: Bearer demo-token'
# {"id": "T-1001", "title": "无法登录", "priority": "high", "status": "open"}
```

想拒绝就送 `{"granted": false, "reason": "…"}`：修改不会执行，模型会收到「被拒绝」的结果并如实告诉用户。

## 8. 查审计与用量

每次连接器调用都有一条过闸记录：

```bash
curl "localhost:8420/v1/audit?source=gate" -H "Authorization: Bearer $KEY"
```

```json
{"audit_id": 51, "source": "gate", "kind": "connector_call", "actor": "engine",
 "session_id": "ses_1197ee27deb1aa819440d23c",
 "data": {"connector": "tickets", "transport": "http", "tool": "updateTicket", "effect": "write",
          "decision": "allowed", "is_error": false, "call_id": "call_3",
          "arguments_digest": "sha256:ab129ec1…", "duration_ms": 1}}
```

谁批准的在控制面的记录里：

```bash
curl "localhost:8420/v1/audit?kind=suspension_resolved_by" -H "Authorization: Bearer $KEY"
# {…, "actor": "key:be139be29730", "data": {"decisions": {"req_7fcbe0950662111f": true}, …}}
```

当天的用量：

```bash
curl "localhost:8420/v1/usage?since=2026-09-30&until=2026-09-30" -H "Authorization: Bearer $KEY"
# {"usage":[{"day":"2026-09-30","input_tokens":2940,"output_tokens":252,"total_tokens":3192,"turns":5}]}
```

## 9. 用控制台再走一遍

浏览器打开 <http://127.0.0.1:8420/console/>，填入 `KEY`，可以在页面上对话、审批、发布业务包、跑评测、查审计和用量。流式输出时「发送」按钮变成「停止」，点它停下正在生成的这一轮，会话接着可用（接口的用法见[应用接入](integrating.md#停止生成)）。

## 接下来

- 把自己的业务场景写成业务包：[编写业务包](writing-packages.md)
- 在自己的系统里调用平台：[应用接入](integrating.md)
- 换成可用于生产的配置：[部署与运维](operations.md)
