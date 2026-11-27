---
name: docx-compose
display_name: Word 正式文档排版
description: 生成结构清晰的 .docx 正式报告（标题层级、正文段落、要点与表格）并做交付前核验。
version: 1.0.0
type: atomic
scripts:
  - name: build_docx
    language: python
    path: scripts/build_docx.py
    timeout_seconds: 30
    description: 根据 JSON 提纲生成 .docx 正式文档并支持 read 核验段落内容。
---

# Word 正式文档排版

使用 `build_docx build` 生成正式 `.docx` 并用 `build_docx read` 确认所有章节与表格完整落盘。
