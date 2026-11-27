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
  - name: large_doc_tool
    language: python
    path: scripts/large_doc_tool.py
    timeout_seconds: 60
    description: 14MB+ 超长文档与 Markdown AST 流式处理（outline 提取大纲与表格 / window 行窗口精读 / search 正则定位 / split 按章节切分 / assemble 多章拼装成册）。
---

# 富文本报告发布与超长文档处理

- 面对大份 Markdown / 文本（100KB–20MB，数十万字）：**绝不整文件读入上下文**，先运行 `large_doc_tool outline` 拿到 Frontmatter、章节行范围与表格清单，再用 `search` / `window` 精读相关片段，或用 `split` 沿章节边界切分后逐段分析；
- 编写超长报告时，将各章草稿写入 `/workspace/outputs/chapters/01_xxx.md ...`，最后调用 `large_doc_tool assemble` 确定性拼装成品并自动生成目录与字数统计；
- 输出独立 HTML 预览版本时调用 `render_html_report` 确保零 XSS 与零外链。
