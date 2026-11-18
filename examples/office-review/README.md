# 办公材料汇总与复核（R4-E 样板）

可复制交付的首个能力样板：员工本人提供的 UTF-8 CSV 按指定分组列与金额列做 Decimal 精确汇总，列出异常行，生成 `summary.csv`、`summary.xlsx`、`report.md`、`evidence.json` 与 `outputs/manifest.json`（table_summary 四类成果）。

| 路径 | 是什么 |
| --- | --- |
| `package/` | 业务包（`xuanpu/v1`），只把这个目录交给打包；脚本在办公沙盒里运行，不被平台进程导入 |
| `delivery.json` | 外部交付描述（`xuanpu-delivery/v1`）：引用包的归档摘要、输入输出契约、权限、本地配置槽与评测套件摘要；不进入它引用的归档 |
| `evals/suite.json` | 评测套件（`xuanpu-eval-suite/v1`），摘要写在交付描述里 |
| `evals/inputs/`、`evals/expected/` | 确定性样例：`basic.csv` 预期甲 170.00、乙 80.00、总计 250.00、有效行 3；`invalid.csv` 的缺金额与非法数字须作为异常行列出 |
| `build.py` | 确定性打包并把归档摘要与套件摘要写进 `delivery.json`；`--check` 只核对 |

改了 `package/` 或 `evals/suite.json` 后运行：

```bash
uv run python examples/office-review/build.py
```

## 复制到另一家企业

同一份归档与 `delivery.json` 可以原样交给另一家企业：对方管理员发布归档（`POST /v1/packages`），用同一描述登记（`POST /v1/capabilities`），再按本企业实际填写 `report_title`、`group_column`、`amount_column` 做预检与安装，之后按本企业员工名单分配、评测、发布。两家的包摘要相同，安装配置与配置摘要各自独立；任何一家的能力、安装、发布、分配标识在另一家都是 404（`packages/xuanpu-server/tests/r4_contract/test_office_replication.py`，内存与 PG）。复制只带模板，不带对方的运行数据、评测结果或密钥。

样板只做精确汇总与复核草稿：不出网、不声明外部写工具；材料里的指令只是数据；生成文件不等于业务已签收，是否进入交接由员工本人决定。重复行保留计入并在报告里提示。
