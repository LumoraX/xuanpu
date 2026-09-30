# xuanpu-spec

xuanpu 业务包规范：业务方交给平台的业务包长什么样——清单 `xuanpu.yaml`、skill 目录、OpenAPI 文档、归档格式——以及校验它的代码。

这是业务方与平台之间的对外契约。本包不依赖平台的其他组件，也不依赖 taifeng，只依赖 `pydantic` 与 `pyyaml`：可以单独装进业务方的流水线里做校验和打包。

规范的说明见 [`docs/architecture/package-spec.md`](../../docs/architecture/package-spec.md)，怎么写一个业务包见[编写业务包](../../docs/guides/writing-packages.md)。

## 校验与打包

```python
from pathlib import Path

from xuanpu_spec import pack_package, validate_package

report = validate_package(Path("./support-desk"))
for warning in report.warnings:
    print("提醒：", warning)
if not report.ok:
    raise SystemExit("\n".join(report.errors))

archive = pack_package(Path("./support-desk"))     # bytes，可直接发给平台的发布接口
```

命令行里对应 `xuanpu validate` 与 `xuanpu pack`。

## 接口

| 函数 | 说明 |
| --- | --- |
| `validate_package(目录)` | 校验整个业务包目录，返回 `ValidationReport`（`ok`、`manifest`、`errors`、`warnings`）。不抛异常 |
| `load_manifest(路径)` / `parse_manifest(文本)` | 只解析清单，返回 `PackageManifest`；不合法抛 `ManifestError`（`problems` 里是逐条的问题） |
| `dump_manifest(manifest)` | 清单 → YAML 文本 |
| `manifest_json_schema()` | 清单的 JSON Schema |
| `pack_package(目录)` | 打成归档。确定性：内容相同则归档逐字节相同 |
| `unpack_package(数据, 目标目录)` | 解包。把归档当作不可信输入：路径逃逸、符号链接、重复条目、超限、损坏都抛 `ArchiveError` |
| `archive_digest(数据)` | 归档的指纹，`sha256:…` |
| `load_openapi(路径)` / `parse_openapi(文本)` | 解析 OpenAPI 3.x 文档，返回 `ApiDocument`（`operations` 是 operationId → `Operation`）；不支持的写法抛 `OpenApiError` |
| `split_tool_name(工具名)` | 把 `mcp__crm__search` 或 `api__erp__createOrder` 拆成 `(前缀, 连接器名, 工具名)` |
| `version_key(版本号)` | 语义化版本的排序键 |

数据模型（`PackageManifest`、`PackageSpec`、`ConnectorSpec`、`PolicySpec`、`EvalCase` 等）在 `xuanpu_spec.manifest` 里，都是不可变的 pydantic 模型，多余的字段一律拒绝。

异常都是 `SpecError` 的子类：`ManifestError`、`ArchiveError`、`OpenApiError`。

## 版本

规范版本是清单里的 `apiVersion`，当前为 `xuanpu/v1`。不兼容的变更会升这个版本号；同一版本内只做向后兼容的增补。
