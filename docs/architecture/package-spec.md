# 业务包规范（活文档）

> 规范版本 `xuanpu/v1`。权威定义在 `packages/xuanpu-spec/src/xuanpu_spec/manifest.py`；
> `xuanpu schema` 可以导出 JSON Schema。

业务包是一个目录，根下有清单 `xuanpu.yaml`：

```
support-desk/
├── xuanpu.yaml
└── skills/
    ├── triage/
    │   ├── SKILL.md
    │   └── scripts/stamp.sh
    └── faq/
        └── SKILL.md
```

## 清单

```yaml
apiVersion: xuanpu/v1
kind: Package
metadata:
  name: support-desk          # 小写字母、数字、连字符
  version: 1.2.0              # 语义化版本
  description: 客服工单助手
spec:
  skills:
    dir: skills               # 默认 skills
    entries: [triage]         # 可以作为会话入口的 skill，至少一个
  model:
    tier: balanced            # fast | balanced | deep，默认 balanced
  policy:
    approval: standard        # strict | standard | relaxed，默认 standard
    budget:
      maxSessionTokens: 200000
      maxIterations: 40
    sandbox:
      network: false          # 脚本是否需要出网，默认否
      shell: false            # 是否开放沙盒内的后台命令工具，默认否
  connectors:
    - name: crm
      command: ["uvx", "crm-mcp"]
      env:
        CRM_TOKEN: {secret: crm-token}   # 密钥引用，明文由平台注入
        CRM_REGION: {value: cn}          # 字面量
      effect: external        # 默认的副作用分类：read | write | external，默认 external
      tools:                  # 工具白名单；留空表示开放全部
        - {name: search_customer, effect: read}
        - {name: update_ticket}
  evals:
    - name: greet
      entry: triage
      input: 你好
      expect:
        contains: ["您好"]
        maxTokens: 2000
```

## 规则

- **多余的字段一律拒绝**：拼错的字段名在校验时报错。
- **只声明需求，不写实现细节**：模型只选档位；审批只选强度；凭据只写密钥名。skill 的 `model` 字段只在取值是档位名时生效。
- **连接器工具名**：模型看到的是 `mcp__<连接器名>__<工具名>`，skill 在 `tool_names` 里按这个名字引用。引用了清单里没有的连接器、或白名单之外的工具，校验不通过。
- **入口**：清单 `entries` 里的 skill 必须在 SKILL.md 里声明 `entry: true`。
- **版本**：发布后不可变，改动必须发新版本。不兼容的规范变更升 `apiVersion`。

## 归档

业务包以 zip 归档发布，清单在归档根下。

- 打包是确定性的：同样的内容总是得到逐字节相同的归档，归档的 SHA-256 是版本的指纹。
- 打包跳过 `.git`、`__pycache__`、`.venv`、`node_modules`、`.env`、`.DS_Store`；包里不允许符号链接。
- 解包把归档当作不可信输入：路径逃逸、符号链接、重复条目、体积超限、内容损坏都会被拒绝。
- 上限：归档 64 MiB，解包后 256 MiB，单文件 32 MiB，5000 个文件。

## 校验

`xuanpu validate <目录>` 做两层检查：清单本身是否合法；清单与包内容是否对得上（入口存在、工具引用有效）。发布时平台还会让内核实际加载一遍 skill。
