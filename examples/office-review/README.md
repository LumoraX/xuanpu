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

样板只做精确汇总与复核草稿：不出网、不声明外部写工具；材料里的指令只是数据；生成文件不等于业务已签收，是否进入交接由员工本人决定。重复行保留计入并在报告里提示。
