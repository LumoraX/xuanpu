"""14MB+ 超长文档渐进式读写、Markdown AST、业务自动归类插件与 PPTX/JSON 受理验收测试。"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from server.api.app import create_app
from server.api.auth import caller
from server.core.tenancy import Principal
from server.domains.identity.models import UserRecord
from server.domains.knowledge.service import chunk as kb_chunk
from server.domains.work_tasks.formats import validate_content
from server.domains.workspace.document_io import (
    inspect_document_outline,
    read_text_window,
    search_document,
)
from server.domains.workspace.patch import replace_line_range
from server.domains.workspace.taxonomy import classify_document_sample
from server.engine.butler.personal_actions import execute_personal_action, undo_executed_action
from server.engine.butler.personal_reads import read_extended_personal
from server.engine.butler.turn import ButlerTurn
from spec import validate_package


def _make_14mb_markdown() -> str:
    """构造一份真实超过 14MB 的多章节 Markdown 长文（含 Frontmatter、表格、代码块与深埋锚点）。"""
    header = (
        "---\n"
        "title: 2026全球云计算与AI算力行业白皮书\n"
        "author: 玄圃研究院\n"
        "date: 2026-11-28\n"
        "---\n\n"
        "# 2026 全球云计算与 AI 算力行业白皮书\n\n"
        "| 年份 | 市场规模(亿元) | 复合增长率 |\n"
        "| --- | --- | --- |\n"
        "| 2025 | 4800 | 28.5% |\n"
        "| 2026 | 6200 | 29.2% |\n\n"
        "```python\n"
        "# 代码块内的注释绝不应被误识别为 Markdown 章节标题\n"
        "def calc():\n"
        "    return 6200\n"
        "```\n\n"
    )
    # 每行约 150 字节（50 个汉字），20 章 × 4900 行 ≈ 14.5 MiB（> 15 MB）
    filler_line = "本段记录产业链渗透率、政策导向、数据中心能耗指标与核心结论综述分析，确保超长文本体量达到真实生产级别。\n"
    chapter_body = filler_line * 4900
    parts = [header]
    for ch in range(1, 21):
        parts.append(f"## 第{ch}章 专题深度分析\n\n")
        if ch == 16:
            parts.append("核心审计锚点：2026-11-28 核定年度资本开支上限为 18.6亿元，违约预警阈值 3.5%。\n")
        parts.append(chapter_body)
    return "".join(parts)


def test_14mb_markdown_progressive_outline_search_window_and_fast_diff() -> None:
    """实测 14MB+ Markdown 的骨架扫描、正则定位、滑动窗口精读与局部快速 Diff 性能与精度。"""
    doc = _make_14mb_markdown()
    size_bytes = len(doc.encode("utf-8"))
    assert size_bytes >= 14 * 1024 * 1024, f"实际体积仅 {size_bytes} 字节"

    t0 = time.monotonic()
    outline = inspect_document_outline(doc, filename="industry_whitepaper.md", mime_type="text/markdown")
    t_outline = time.monotonic() - t0
    assert t_outline < 5.0, f"14MB 骨架扫描耗时过长：{t_outline:.2f}s"
    assert outline["format"] == "markdown"
    assert outline["frontmatter"]["author"] == "玄圃研究院"
    # 1 个 H1 + 20 个 H2 = 21 个标题，代码块内的 `# 注释` 绝不混入
    assert outline["heading_count"] == 21
    assert outline["table_count"] == 1
    assert outline["code_blocks"].get("python") == 1

    # 精确定位深埋在第 16 章（约 11MB 处）的锚点
    found = search_document(doc, "18.6亿元", context_lines=1)
    assert found["total_matches"] == 1
    hit = found["matches"][0]
    assert "第16章 专题深度分析" in (hit["section"] or "")
    target_line = hit["line_no"]

    # 滑动窗口精读目标行上下 5 行（绝不拉回整份 14MB）
    win = read_text_window(doc, start_line=target_line - 2, end_line=target_line + 3, with_line_numbers=True)
    assert len(win["content"]) < 2000
    assert "18.6亿元" in win["content"]
    assert "第16章 专题深度分析" in (win["active_heading"] or "")
    assert win["has_more"] is True and win["next_start_line"] == target_line + 4

    # 在 14MB 文档中间做单行区间替换，验证快速前后缀裁剪 Diff 毫秒级完成且行号正确
    t1 = time.monotonic()
    patched = replace_line_range(
        doc,
        start_line=target_line,
        end_line=target_line,
        replacement="核心审计锚点：2026-11-28 核定年度资本开支上限为 19.2亿元，违约预警阈值 3.0%。",
        filename="industry_whitepaper.md",
    )
    t_diff = time.monotonic() - t1
    assert t_diff < 2.0, f"14MB 局部 Diff 耗时过长：{t_diff:.2f}s"
    assert "19.2亿元" in patched.diff and "18.6亿元" in patched.diff
    assert f"@@ -{target_line - 3}," in patched.diff or f"@@ -{target_line}" in patched.diff

    # 对 14MB 文档执行采样业务归类：毫秒级命中「行业研报」并标记「超长文档」
    cls = classify_document_sample(filename="industry_whitepaper.md", mime_type="text/markdown", text=doc)
    assert cls.category_id == "research_intel"
    assert "超长文档" in cls.suggested_tags
    assert "18.6亿元" in cls.extracted_amounts or any("亿" in a for a in cls.extracted_amounts) or cls.confidence >= 0.6


async def test_butler_progressive_long_article_authoring_and_classify_undo(world: Any) -> None:
    """验证管家通过 save -> append x2 -> outline -> search -> line-range patch -> classify -> undo 完整闭环。"""
    platform = world.platform
    user_id = "usr_long_author"
    await platform.identity.store.create_user(
        UserRecord(
            user_id=user_id, tenant_id="acme", role="tenant_member", status="active",
            display_name="Author", created_at=1000.0,
        )
    )
    who = Principal(role="tenant_member", tenant_id="acme", user_id=user_id, key_id=None)

    # 先创建一个活跃事项以便自动归类匹配
    matter = await platform.workspace.create(
        who, "matter", {"title": "星辰云年度采购合同", "description": "供应商年度合作协议"}, "m_tax_1",
    )

    turn = ButlerTurn(
        request_id="req_long_1",
        trace_id="trc_long_1",
        who=who,
        timezone="Asia/Shanghai",
    )

    # 1. 首章落盘
    r1 = await execute_personal_action(
        platform,
        turn,
        {
            "action": "save_note_file",
            "name": "星辰云年度采购合同草案.md",
            "content": "# 星辰云年度采购合同\n\n## 第一章 双方信息\n\n甲方：玄圃科技\n乙方：星辰云服务有限公司",
        },
        now=1000.0,
    )
    file_id = r1["file"]["id"]

    # 2. 分批增量追加第二章与第三章（模拟多轮长文写作）
    r2 = await execute_personal_action(
        platform,
        turn,
        {
            "action": "append_personal_file",
            "item_id": file_id,
            "content": "## 第二章 合同金额与付款\n\n合同总金额：¥1,200,000（120万元），分三期结算。",
        },
        now=1001.0,
    )
    assert r2["appended_chars"] > 20
    r3 = await execute_personal_action(
        platform,
        turn,
        {
            "action": "append_personal_file",
            "item_id": file_id,
            "content": "## 第三章 违约责任与争议解决\n\n任何一方违约应承担违约责任，争议提交仲裁委员会。",
        },
        now=1002.0,
    )
    assert r3["file"]["data"]["current_revision"] == 3

    # 3. 管家渐进式读取：先看骨架 -> 再检索 -> 再窗口读取
    outline = await read_extended_personal(platform, turn, "file_outline", {"item_id": file_id}, now=1003.0)
    assert outline is not None and outline["heading_count"] == 4
    searched = await read_extended_personal(
        platform, turn, "file_search", {"item_id": file_id, "query": "120万元"}, now=1003.0,
    )
    assert searched is not None and searched["total_matches"] == 1
    hit_line = searched["matches"][0]["line_no"]

    # 4. 按精确行号区间修订金额条款，随后验证撤回恢复
    patched = await execute_personal_action(
        platform,
        turn,
        {
            "action": "patch_personal_file",
            "item_id": file_id,
            "start_line": hit_line,
            "end_line": hit_line,
            "content": "合同总金额：¥1,350,000（135万元），分三期结算。",
        },
        now=1004.0,
    )
    assert "135万元" in patched["diff"]

    # 5. 自动业务归类并归档到「01-合同法务」文件夹 + 自动绑定活跃事项 + 打标签
    cls_res = await execute_personal_action(
        platform, turn, {"action": "classify_file", "item_id": file_id, "apply": True}, now=1005.0,
    )
    assert cls_res["classification"]["category_id"] == "contract_legal"
    assert cls_res["classification"]["folder_name"] == "01-合同法务"
    assert cls_res["file"]["data"]["matter_id"] == matter["id"]
    assert "合同法务" in cls_res["file"]["data"]["tags"]
    assert cls_res["folder_id"] is not None

    # 6. 一键撤回自动归类，确认恢复原未归档状态
    classify_action = turn.executed_actions[-1]
    undone = await undo_executed_action(platform, who, classify_action)
    assert undone["undone"] == 1
    restored = await platform.workspace.get(who, file_id)
    assert restored["data"]["parent_id"] is None


async def test_doc_taxonomy_plugin_http_rules_batch_and_summary(world: Any) -> None:
    """验证 L2 doc-taxonomy 插件自定义规则持久化、批量未归档文件归类与统计看板。"""
    platform = world.platform
    user_id = "usr_tax_http"
    await platform.identity.store.create_user(
        UserRecord(
            user_id=user_id, tenant_id="acme", role="tenant_member", status="active",
            display_name="TaxUser", created_at=1000.0,
        )
    )
    who = Principal(role="tenant_member", tenant_id="acme", user_id=user_id, key_id=None)

    await platform.workspace.upload(
        who, "2026Q4利润表对账.md",
        "# Q4 利润表与毛利率\n净利润增长 18%，应收账款回收顺利。".encode(),
        "text/markdown", "u_fin_1",
    )
    await platform.workspace.upload(
        who, "凤凰计划芯片流片评审.md",
        "# 凤凰计划流片验收\n记录核心参数与良率。".encode(),
        "text/markdown", "u_custom_1",
    )

    app = create_app(platform)
    app.dependency_overrides[caller] = lambda: who
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        # 1. 新增自定义业务归类规则
        rule_resp = await http.put(
            "/v1/plugins/ext/doc-taxonomy/rules",
            json={
                "rule_id": "phoenix_chip",
                "category_id": "custom_chip",
                "display_name": "芯片研发专项",
                "folder_name": "00-芯片研发专项",
                "keywords": ["凤凰计划", "流片"],
                "tags": ["芯片专项", "绝密研发"],
            },
        )
        assert rule_resp.status_code == 200, rule_resp.text

        # 2. 批量一键归类工作台所有未归档文件
        batch = await http.post("/v1/plugins/ext/doc-taxonomy/batch-classify", json={"apply": True})
        assert batch.status_code == 200, batch.text
        items = {it["filename"]: it for it in batch.json()["items"]}
        assert items["2026Q4利润表对账.md"]["category_id"] == "finance_report"
        assert items["凤凰计划芯片流片评审.md"]["category_id"] == "custom_chip"
        assert items["凤凰计划芯片流片评审.md"]["folder_name"] == "00-芯片研发专项"

        # 3. 统计看板准确反映分类分布与 0 未归档
        summary = await http.get("/v1/plugins/ext/doc-taxonomy/summary")
        assert summary.status_code == 200
        body = summary.json()
        assert body["classified_count"] == 2 and body["unfiled_count"] == 0
        assert body["by_category"]["财务报表"] == 1 and body["by_category"]["芯片研发专项"] == 1


def test_knowledge_markdown_heading_aware_chunking_and_work_task_pptx_json(tmp_path: Path) -> None:
    """验证知识库 Markdown 分块保留章节面包屑，以及 work_tasks 原生支持 PPTX 与 JSON 输入。"""
    md = (
        "# 年度风控手册\n\n"
        "## 第二章 违约处置\n\n"
        + ("本条规定供应商延迟交付超过十五日时的违约金计算方式。\n" * 60)
    )
    chunks = kb_chunk("src_1", md)
    assert len(chunks) >= 2
    assert any("[章节：年度风控手册 > 第二章 违约处置]" in c.text for c in chunks)

    # JSON 合法输入通过 validate_content
    validate_content(b'{"revenue": 1200}', "application/json")

    # 真实 PPTX 文件（由 doc-analyst build_pptx.py 生成）通过 validate_content
    repo_root = Path(__file__).resolve().parents[3]
    pkg_dir = repo_root / "examples" / "doc-analyst"
    assert validate_package(pkg_dir).ok
    spec_path = tmp_path / "deck.json"
    pptx_path = tmp_path / "deck.pptx"
    spec_path.write_text(
        json.dumps({"slides": [{"title": "测试页", "bullets": ["条目一"]}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    subprocess.run(
        [sys.executable, str(pkg_dir / "skills/pptx-compose/scripts/build_pptx.py"), "build", str(spec_path), str(pptx_path)],
        check=True,
        capture_output=True,
    )
    validate_content(
        pptx_path.read_bytes(),
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )


def test_doc_analyst_large_doc_tool_streaming_pipeline(tmp_path: Path) -> None:
    """验证 doc-analyst large_doc_tool.py 的 outline / window / search / split / assemble 全链路。"""
    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "examples" / "doc-analyst" / "skills" / "markdown-publish" / "scripts" / "large_doc_tool.py"

    big_md = tmp_path / "big_book.md"
    big_md.write_text(
        "---\ntitle: 超长架构书\n---\n\n# 超长架构书\n\n"
        "## 第一章 背景\n\n" + ("背景描述句子。\n" * 200)
        + "\n## 第二章 核心算法\n\n| 指标 | 值 |\n| --- | --- |\n| QPS | 50000 |\n\n关键结论：延迟低于 10ms。\n"
        + ("算法细节。\n" * 200),
        encoding="utf-8",
    )

    out_info = json.loads(subprocess.check_output([sys.executable, str(script), "outline", str(big_md)], text=True))
    assert out_info["frontmatter"]["title"] == "超长架构书"
    assert out_info["heading_count"] == 3 and out_info["table_count"] == 1
    assert out_info["tables"][0]["sample_rows"][0] == {"指标": "QPS", "值": "50000"}

    srch = json.loads(subprocess.check_output([sys.executable, str(script), "search", str(big_md), "10ms"], text=True))
    assert srch["total_matches"] == 1 and "第二章 核心算法" in srch["matches"][0]["section"]

    hit_line = srch["matches"][0]["line_no"]
    win = json.loads(
        subprocess.check_output(
            [sys.executable, str(script), "window", str(big_md), str(hit_line), str(hit_line + 1)], text=True,
        )
    )
    assert "10ms" in win["content"]

    parts_dir = tmp_path / "parts"
    spl = json.loads(
        subprocess.check_output(
            [sys.executable, str(script), "split", str(big_md), str(parts_dir), "--max-chars", "1500"], text=True,
        )
    )
    assert spl["total_parts"] >= 2 and (parts_dir / "manifest.json").is_file()

    assembled = tmp_path / "assembled.md"
    asm = json.loads(
        subprocess.check_output(
            [sys.executable, str(script), "assemble", str(parts_dir), str(assembled), "--title", "汇总成品书"],
            text=True,
        )
    )
    assert asm["chapters"] == spl["total_parts"]
    assert "## 目录" in assembled.read_text(encoding="utf-8")
