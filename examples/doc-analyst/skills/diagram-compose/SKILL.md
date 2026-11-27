---
name: diagram-compose
display_name: 架构与流程图绘制
description: 编写严格语法合规的 Mermaid/SVG 架构图与时序图，含括号标签一律加双引号。
version: 1.0.0
type: atomic
scripts:
  - name: check_mermaid
    language: python
    path: scripts/check_mermaid.py
    timeout_seconds: 30
    description: 静态校验 Mermaid 图表类型头、节点双引号与禁用 HTML 标签，输出 JSON 报告。
---

# 架构与流程图绘制

编写严格语法合规的 Mermaid/SVG 架构图与时序图，含括号标签一律加双引号，交付前调用 `check_mermaid` 脚本完成自检。
