---
name: excel-analysis
display_name: Excel 工作簿分析与生成
description: 读取或生成带动态 Excel 公式的 .xlsx 工作簿，严禁硬编码计算结果。
version: 1.0.0
type: atomic
scripts:
  - name: excel_tool
    language: python
    path: scripts/excel_tool.py
    timeout_seconds: 30
    description: 支持 build 生成含动态公式的 .xlsx 与 summarize 解析工作表/公式摘要。
---

# Excel 工作簿分析与生成

生成 `.xlsx` 时数值汇总一律写入 Excel 公式（如 `=SUM(B2:B4)`），并通过 `excel_tool summarize` 做交付前复核。
