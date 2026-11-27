---
name: doc-diff
display_name: 文档版本比对
description: 对两版合同或方案文本生成结构化条款变更对照表与 Unified Diff。
version: 1.0.0
type: atomic
scripts:
  - name: diff_texts
    language: python
    path: scripts/diff_texts.py
    timeout_seconds: 30
    description: 比较旧版与新版 UTF-8 文本文件，输出 Unified Diff 与新增/删除行统计 JSON。
---

# 文档版本比对

对两版合同或方案文本生成结构化条款变更对照表与 Unified Diff。优先调用 `diff_texts` 脚本确定性生成差异，禁止人工凭印象比对。
