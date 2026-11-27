---
name: pptx-compose
display_name: PPT 演示文稿制作
description: 生成结构化多页 .pptx 汇报材料并抽取逐页文本核对页数与核心论点。
version: 1.0.0
type: atomic
scripts:
  - name: build_pptx
    language: python
    path: scripts/build_pptx.py
    timeout_seconds: 30
    description: 根据 JSON 提纲生成 .pptx 演示文稿并支持 inspect 逐页文本核对。
---

# PPT 演示文稿制作

使用 `build_pptx build` 产出清晰简洁的多页 `.pptx`，每页控制在 3–5 条要点，交付前运行 `build_pptx inspect` 核对。
