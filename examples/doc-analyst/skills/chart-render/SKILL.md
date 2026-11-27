---
name: chart-render
display_name: 数据图表可视化
description: 在断网沙盒中输出高清矢量 SVG 图表及 PNG 图片（供管家视觉直读自检）。
version: 1.0.0
type: atomic
scripts:
  - name: render_chart
    language: python
    path: scripts/render_chart.py
    timeout_seconds: 30
    description: 根据 JSON 数据规范生成 SVG 柱状/折线图与可选 PNG 预览图。
---

# 数据图表可视化

使用 `render_chart` 输出清晰的 SVG 与 PNG 图表，确保坐标刻度、单位与数据标签完整无遮挡。
