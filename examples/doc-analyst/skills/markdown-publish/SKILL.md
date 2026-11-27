---
name: markdown-publish
display_name: 富文本报告发布
description: 输出包含摘要表、公式、图示与安全交互式 HTML 预览的成品报告。
version: 1.0.0
type: atomic
scripts:
  - name: render_html_report
    language: python
    path: scripts/render_html_report.py
    timeout_seconds: 30
    description: 将 Markdown 报告渲染为纯转义、零外链的独立 HTML 成品报告。
---

# 富文本报告发布

输出包含摘要表、公式、图示与安全独立 HTML 预览的成品报告；交付 HTML 版本时调用 `render_html_report` 脚本确保零 XSS 与零外链。
