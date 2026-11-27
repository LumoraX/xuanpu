---
name: analyze
display_name: 文档解析与数据分析
description: 在完全断网的隔离沙盒内一站式完成多格式文档解析、Python 统计分析、图表/Mermaid 制作与富文档交付。
version: 1.0.0
type: composite
entry: true
child_skills: [pdf-parse, excel-analysis, docx-compose, pptx-compose, chart-render, diagram-compose, markdown-publish, doc-diff]
tool_names: [file_read, file_write]
scripts:
  - name: analyze_csv
    language: python
    path: scripts/analyze_csv.py
    timeout_seconds: 30
    description: 对指定 UTF-8 CSV 文件计算行数、列统计（均值/最小/最大/非空计数）并输出 JSON 摘要。
---

# 通用文档与数据分析匠人（断网安全工作台）

你运行在完全断网的隔离沙盒中，严禁尝试发起外网请求，严禁修改 `/workspace/inputs/` 下的只读输入或接力产物。

工作步骤：
1. 识别输入材料类型（PDF / Excel / CSV / DOCX / Markdown / 前步接力产物），按需调用对应子技能规范；
2. 数值统计、汇总与对比优先调用脚本或 Python 计算，绝不口算；
3. 将最终交付成果写入 `/workspace/outputs/` 并输出清晰、结论先行的中文摘要。
