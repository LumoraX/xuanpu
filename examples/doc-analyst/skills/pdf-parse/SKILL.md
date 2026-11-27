---
name: pdf-parse
display_name: PDF 解析与结构化抽取
description: 解析多页 PDF 文本、元信息与关键段落，输出结构化 JSON 摘要。
version: 1.0.0
type: atomic
scripts:
  - name: parse_pdf
    language: python
    path: scripts/parse_pdf.py
    timeout_seconds: 30
    description: 提取 PDF 页数、元信息与逐页文本内容，输出结构化 JSON。
---

# PDF 解析与结构化抽取

优先调用 `parse_pdf` 脚本确定性提取页数与逐页文本，禁止凭空推测未读页内容。
