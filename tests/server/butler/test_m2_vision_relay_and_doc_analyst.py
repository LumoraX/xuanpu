"""SOUL M2：轻量图片视觉直读、Mermaid 轻量校验、断网 doc-analyst 包验证与有界动态多步接力测试。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import taifeng

from server.engine.butler.multimodal import (
    collect_inline_images,
    sniff_image_media_type,
    validate_mermaid_blocks,
)
from server.engine.tracing.models import TraceOwner
from spec import pack_package, validate_package

# 1x1 透明 PNG 真实字节（含标准 PNG 魔数）
_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0bIDATx\x9cc`\x00\x00\x00\x02\x00\x01"
    b"\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_image_magic_byte_sniffing_and_mermaid_validation() -> None:
    """魔数识别严格校验真实图片字节，伪造后缀返回 None；Mermaid 校验准确捕获未加引号括号与不支持图类型。"""
    assert sniff_image_media_type(_PNG_1X1) == "image/png"
    assert sniff_image_media_type(b"\xff\xd8\xff\xe0" + b"\x00" * 16) == "image/jpeg"
    assert sniff_image_media_type(b"RIFF\x00\x00\x00\x00WEBP1234") == "image/webp"
    assert sniff_image_media_type(b"not a real png file at all") is None

    bad_md = """# 架构
```mermaid
gantt
    title Unsupported
```
```mermaid
flowchart TD
    A[节点(未加引号)] --> B[正常]
```
"""
    warnings = validate_mermaid_blocks(bad_md)
    assert len(warnings) == 2
    assert any("不在受支持清单" in w for w in warnings)
    assert any("未加引号" in w for w in warnings)

    good_md = '```mermaid\nflowchart LR\n  A["节点(合规)"] --> B["结束"]\n```'
    assert validate_mermaid_blocks(good_md) == []


async def test_butler_vision_direct_read_attaches_valid_images_up_to_limit(
    butler: dict[str, Any], step: Any,
) -> None:
    """上传真实 PNG 图片（含伪装文本文件混入）时，管家仅通过魔数校验直读真实图片（≤3 张）。"""
    platform = butler["world"].platform
    alice = butler["alice"]

    img1 = await platform.workspace.upload(alice, "card1.png", _PNG_1X1, "image/png", "up_img_1")
    fake = await platform.workspace.upload(alice, "fake.png", b"plain text pretending png", "image/png", "up_img_fake")

    from server.domains.work_tasks.models import InputFile

    refs = [
        InputFile(item_id=img1["id"], revision=1),
        InputFile(item_id=fake["id"], revision=1),
    ]
    atts = await collect_inline_images(platform, alice, refs)
    assert len(atts) == 1
    assert atts[0]["kind"] == "image" and atts[0]["media_type"] == "image/png"

    # 端到端发给管家并验证能正常走完多模态输入提交流程
    butler["world"].models.script(step(text="已识别名片信息：张三，产品总监。"))
    resp = await butler["http"].post(
        "/v1/assistant/messages",
        json={
            "request_id": "vis_1",
            "text": "帮我识别这张名片",
            "input_files": [{"item_id": img1["id"], "revision": 1}],
        },
    )
    assert resp.status_code == 200, resp.text
    assert "张三" in resp.json()["message"]


async def test_doc_analyst_package_validates_packs_loads_and_runs_csv_script(
    tmp_path: Path,
) -> None:
    """断网 doc-analyst 匠人包（含 8 项 document-skills 与分析脚本）通过规范校验、打包、内核加载与 CSV 计算。"""
    pkg_dir = Path(__file__).resolve().parents[3] / "examples/doc-analyst"
    report = validate_package(pkg_dir)
    assert report.ok, report.errors

    archive = pack_package(pkg_dir)
    assert len(archive) > 500

    registry = await taifeng.FilesystemSkillRegistry.load(pkg_dir / "skills")
    assert registry.get("analyze") is not None
    for skill_id in ("pdf-parse", "excel-analysis", "docx-compose", "pptx-compose",
                     "chart-render", "diagram-compose", "markdown-publish", "doc-diff"):
        assert registry.get(skill_id) is not None

    # 直接执行离线分析脚本验证确定性计算
    sample_csv = tmp_path / "sales.csv"
    sample_csv.write_text("region,amount\n华东,100\n华南,200\n华北,300\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(pkg_dir / "skills/analyze/scripts/analyze_csv.py"), str(sample_csv)],
        capture_output=True, text=True, check=True,
    )
    out = json.loads(proc.stdout)
    assert out["row_count"] == 3
    assert out["numeric_stats"]["amount"]["sum"] == 600.0
    assert out["numeric_stats"]["amount"]["mean"] == 200.0


async def test_bounded_dynamic_relay_from_wake_turn_enforces_max_hops(
    butler: dict[str, Any], step: Any,
) -> None:
    """唤醒复核轮在 passed=True 时允许动态接力续派下一匠人并冻结前步污点产物，且受 max_dynamic_relay_hops 约束。"""
    world = butler["world"]
    platform = world.platform
    # 把跳数上限设为 2 以便验证第 2 跳放行、第 3 跳被拦截
    platform.butler_config = platform.butler_config.model_copy(update={"max_dynamic_relay_hops": 2})

    # 1. 第 1 跳：首轮派给 support-desk
    world.models.script(
        step(calls=[("d1", "dispatch_artisan", {
            "package": "support-desk", "entry": "triage", "instruction": "先做联网初查"
        })]),
        step(text="已派出第 1 跳调研。"),
        # 第 1 跳匠人回复
        step(text="初查结果：发现 3 组关键数据。"),
        # 唤醒复核轮：同一轮连续发起第 2 跳（放行）与第 3 跳（超 max_dynamic_relay_hops=2 被拒）
        step(calls=[
            ("d2", "dispatch_artisan", {
                "package": "support-desk", "entry": "triage", "instruction": "基于前步数据做深度统计"
            }),
            ("d3", "dispatch_artisan", {
                "package": "support-desk", "entry": "triage", "instruction": "再加第 3 跳"
            }),
        ]),
        step(text="已将前步调研结果动态接力给下一位匠人分析。"),
        step(text="深度统计已完成。"),
    )
    resp = await butler["http"].post(
        "/v1/assistant/messages",
        json={"request_id": "dyn_relay_1", "text": "先查资料再做分析"},
    )
    assert resp.status_code == 200, resp.text
    trace_id = resp.json()["trace_id"]

    # 等待第 1 跳完成并驱动唤醒复核轮执行
    import asyncio

    owner = TraceOwner(tenant_id="acme", user_id="usr_alice")
    async with asyncio.timeout(15):
        while True:
            await platform.butler.wakeup.run_once()
            dispatches = await platform.dispatches.list_for_trace(owner, trace_id)
            if len(dispatches) >= 2:
                break
            await asyncio.sleep(0.02)

    # 断言：总共只允许建 2 跳（第 3 跳被上限拦截），且冻结了 dynamic_relay_hop_1 过程产物并带污点提示
    assert len(dispatches) == 2
    assert any("动态多步接力已达最大跳数上限" in str(r) for r in world.models.requests)
    artifacts = await platform.trace_artifacts.list_for_trace(butler["alice"], trace_id)
    assert any(a.name.startswith("dynamic_relay_hop_1_") and a.contains_external_input for a in artifacts)
