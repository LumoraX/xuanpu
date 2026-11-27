"""超长文档与 Markdown 的渐进式解析、大纲索引、窗口分页读取与文件内检索引擎。

吸收 Claude / Antigravity 处理 14MB+ / 数十万字超长文档的核心机制：
1. 先看骨架（``inspect_document_outline``）：在 <2KB 内返回 YAML Frontmatter、章节 TOC（含精确起止行号与字数）、表格/代码块清单；
2. 定位跳转（``search_document``）：文件内关键词/正则检索，直接定位行号、所属章节路径与上下文窗口；
3. 滑动窗口精读（``read_text_window``）：严格限制单次返回行数与字符上限，附带 ``has_more`` / ``next_start_line`` / ``active_heading``，永不撑爆模型上下文。
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

DEFAULT_WINDOW_LINES = 250
MAX_WINDOW_LINES = 800
DEFAULT_WINDOW_CHARS = 16_000
MAX_WINDOW_CHARS = 46_000

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})\s*([A-Za-z0-9_+-]*)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_CHECKBOX_RE = re.compile(r"^\s*[-*+]\s+\[([ xX])\]\s+")


def _parse_frontmatter(lines: list[str]) -> tuple[dict[str, str], int]:
    """解析文档顶部可选 YAML Frontmatter（仅简单键值，零外部依赖），返回 (dict, 跳过行数)。"""
    if len(lines) < 3 or lines[0].strip() != "---":
        return {}, 0
    end_idx = -1
    for i in range(1, min(len(lines), 80)):
        if lines[i].strip() in ("---", "..."):
            end_idx = i
            break
    if end_idx < 0:
        return {}, 0
    fm: dict[str, str] = {}
    for raw in lines[1:end_idx]:
        if ":" not in raw or raw.lstrip().startswith("#"):
            continue
        k, v = raw.split(":", 1)
        key = k.strip()
        val = v.strip().strip('"').strip("'")
        if key and len(fm) < 40:
            fm[key] = val[:300]
    return fm, end_idx + 1


def _estimate_words(text: str) -> int:
    """中英混合字数估算：中文字符按 1 字计，英文/数字连续串按 1 词计。"""
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z0-9_]+", text))
    return cjk + latin


def _scan_markdown_structure(
    lines: list[str], *, max_headings: int = 200, max_tables: int = 50,
) -> dict[str, Any]:
    """一次线性扫描提取 Markdown 骨架：Frontmatter、章节树与精确行范围、表格、代码块与任务列表。"""
    frontmatter, start_idx = _parse_frontmatter(lines)
    headings: list[dict[str, Any]] = []
    heading_stack: list[tuple[int, str]] = []
    tables: list[dict[str, Any]] = []
    code_langs: dict[str, int] = {}
    links_count = 0
    tasks_done = 0
    tasks_open = 0

    in_fence = False
    fence_char = ""
    fence_len = 0
    table_start = -1
    table_headers: list[str] = []
    table_rows = 0

    def _flush_table(end_line_1based: int) -> None:
        nonlocal table_start, table_headers, table_rows
        if table_start > 0 and table_headers and len(tables) < max_tables:
            tables.append({
                "start_line": table_start,
                "end_line": end_line_1based,
                "headers": table_headers[:20],
                "row_count": max(0, table_rows - 1),
            })
        table_start = -1
        table_headers = []
        table_rows = 0

    # 记录每行累积字符数以 O(1) 计算每个章节的字数体量
    cum_chars: list[int] = [0]
    for ln in lines:
        cum_chars.append(cum_chars[-1] + len(ln) + 1)

    for idx in range(start_idx, len(lines)):
        raw = lines[idx]
        stripped = raw.strip()
        line_no = idx + 1

        fence_m = _FENCE_RE.match(stripped)
        if fence_m:
            _flush_table(line_no - 1)
            tok = fence_m.group(1)
            if not in_fence:
                in_fence = True
                fence_char = tok[0]
                fence_len = len(tok)
                lang = (fence_m.group(2) or "text").lower()
                code_langs[lang] = code_langs.get(lang, 0) + 1
            elif tok[0] == fence_char and len(tok) >= fence_len:
                in_fence = False
            continue
        if in_fence:
            continue

        # 表格检测
        if stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 2:
            inner = stripped[1:-1]
            if all(ch in "-:| " for ch in inner):
                continue
            cells = [c.strip() for c in inner.split("|")]
            if table_start < 0:
                table_start = line_no
                table_headers = cells
                table_rows = 1
            else:
                table_rows += 1
            continue
        if table_start > 0:
            _flush_table(line_no - 1)

        # 标题检测
        hm = _HEADING_RE.match(stripped)
        if hm:
            level = len(hm.group(1))
            title = hm.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            if len(headings) < max_headings:
                headings.append({
                    "level": level,
                    "title": title[:160],
                    "path": " > ".join(t for _, t in heading_stack)[:240],
                    "start_line": line_no,
                    "end_line": len(lines),
                    "char_count": 0,
                })
            continue

        cb = _CHECKBOX_RE.match(raw)
        if cb:
            if cb.group(1).lower() == "x":
                tasks_done += 1
            else:
                tasks_open += 1
        if "http" in raw and "](" in raw:
            links_count += len(_LINK_RE.findall(raw))

    if table_start > 0:
        _flush_table(len(lines))

    # 回填每个标题对应章节的精确 end_line 与 char_count（遇同级或更高级标题收口）
    total_lines = len(lines)
    for i, h in enumerate(headings):
        end_line = total_lines
        for j in range(i + 1, len(headings)):
            if headings[j]["level"] <= h["level"]:
                end_line = headings[j]["start_line"] - 1
                break
        h["end_line"] = max(h["start_line"], end_line)
        h["char_count"] = max(0, cum_chars[h["end_line"]] - cum_chars[h["start_line"] - 1])

    return {
        "frontmatter": frontmatter,
        "headings": headings,
        "heading_count": len(headings),
        "tables": tables,
        "table_count": len(tables),
        "code_blocks": code_langs,
        "tasks": {"done": tasks_done, "open": tasks_open},
        "links_count": links_count,
    }


def inspect_document_outline(
    text: str,
    *,
    filename: str,
    mime_type: str,
    max_headings: int = 200,
) -> dict[str, Any]:
    """提取任意 UTF-8 文档的结构骨架（支持 Markdown / JSON / CSV / TXT），便于超长文件渐进式精读。"""
    lines = text.splitlines()
    total_lines = len(lines)
    total_chars = len(text)
    total_bytes = len(text.encode("utf-8"))
    lower = filename.lower()

    info: dict[str, Any] = {
        "name": filename,
        "mime_type": mime_type,
        "total_bytes": total_bytes,
        "total_chars": total_chars,
        "total_lines": total_lines,
        "word_estimate": _estimate_words(text[:500_000]) if total_chars <= 500_000 else int(
            _estimate_words(text[:200_000]) * (total_chars / 200_000)
        ),
        "recommended_window_lines": DEFAULT_WINDOW_LINES,
    }

    if mime_type == "text/markdown" or lower.endswith((".md", ".markdown")):
        info["format"] = "markdown"
        info.update(_scan_markdown_structure(lines, max_headings=max_headings))
    elif mime_type == "application/json" or lower.endswith(".json"):
        info["format"] = "json"
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                info["json_root"] = "object"
                info["top_keys"] = list(parsed.keys())[:100]
                info["key_count"] = len(parsed)
            elif isinstance(parsed, list):
                info["json_root"] = "array"
                info["item_count"] = len(parsed)
                if parsed and isinstance(parsed[0], dict):
                    info["sample_item_keys"] = list(parsed[0].keys())[:40]
            else:
                info["json_root"] = type(parsed).__name__
        except ValueError:
            info["json_root"] = "invalid_json"
    elif mime_type == "text/csv" or lower.endswith(".csv"):
        info["format"] = "csv"
        reader = csv.reader(io.StringIO("\n".join(lines[:50])))
        rows = list(reader)
        info["headers"] = rows[0][:50] if rows else []
        info["data_rows_estimate"] = max(0, total_lines - 1)
    else:
        info["format"] = "text"
        # 对纯文本也尝试检测常见章节模式（如 第X章 / 1. / 一、）
        sections: list[dict[str, Any]] = []
        sec_re = re.compile(r"^(第[一二三四五六七八九十百千万0-9]+[章节篇部]|#{1,4}\s+\S|[一二三四五六七八九十]+、|\d+\.\s+\S)")
        for idx, ln in enumerate(lines, start=1):
            s = ln.strip()
            if s and len(s) <= 80 and sec_re.match(s):
                sections.append({"line": idx, "title": s})
                if len(sections) >= max_headings:
                    break
        info["sections"] = sections
    return info


def _active_heading_at(lines: list[str], line_1based: int) -> str | None:
    """定位指定行号当前所属的 Markdown 章节面包屑路径。"""
    stack: list[tuple[int, str]] = []
    in_fence = False
    limit = min(line_1based, len(lines))
    for i in range(limit):
        s = lines[i].strip()
        if _FENCE_RE.match(s):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        hm = _HEADING_RE.match(s)
        if hm:
            lvl = len(hm.group(1))
            title = hm.group(2).strip()
            while stack and stack[-1][0] >= lvl:
                stack.pop()
            stack.append((lvl, title))
    return " > ".join(t for _, t in stack)[:240] if stack else None


def read_text_window(
    text: str,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    char_offset: int | None = None,
    max_lines: int = DEFAULT_WINDOW_LINES,
    max_chars: int = DEFAULT_WINDOW_CHARS,
    with_line_numbers: bool = False,
) -> dict[str, Any]:
    """按行区间或字符偏移执行有界滑动窗口读取，适配几十万字 / 14MB 超长文件。"""
    clamped_lines = max(1, min(int(max_lines), MAX_WINDOW_LINES))
    clamped_chars = max(500, min(int(max_chars), MAX_WINDOW_CHARS))
    total_chars = len(text)

    # 字符偏移模式（适合单行超大 JSON/日志或精确续读）
    if char_offset is not None and start_line is None and end_line is None:
        start = max(0, min(int(char_offset), total_chars))
        end = min(total_chars, start + clamped_chars)
        chunk = text[start:end]
        return {
            "mode": "char_offset",
            "total_chars": total_chars,
            "char_offset": start,
            "next_char_offset": end if end < total_chars else None,
            "has_more": end < total_chars,
            "truncated": end < total_chars,
            "content": chunk,
        }

    lines = text.splitlines()
    total_lines = len(lines)
    if total_lines == 0:
        return {
            "mode": "lines",
            "total_lines": 0,
            "total_chars": 0,
            "start_line": 1,
            "end_line": 0,
            "has_more": False,
            "next_start_line": None,
            "truncated": False,
            "active_heading": None,
            "content": "",
        }

    s_line = max(1, int(start_line or 1))
    if s_line > total_lines:
        s_line = total_lines
    target_end = min(total_lines, int(end_line) if end_line is not None else s_line + clamped_lines - 1)
    if target_end < s_line:
        target_end = s_line
    target_end = min(target_end, s_line + clamped_lines - 1)

    selected: list[str] = []
    used_chars = 0
    actual_end = s_line - 1
    char_capped = False
    for line_no in range(s_line, target_end + 1):
        raw_line = lines[line_no - 1]
        rendered = f"{line_no}: {raw_line}" if with_line_numbers else raw_line
        if selected and used_chars + len(rendered) + 1 > clamped_chars:
            char_capped = True
            break
        if not selected and len(rendered) > clamped_chars:
            selected.append(rendered[:clamped_chars])
            used_chars = clamped_chars
            actual_end = line_no
            char_capped = True
            break
        selected.append(rendered)
        used_chars += len(rendered) + 1
        actual_end = line_no

    has_more = actual_end < total_lines or char_capped
    return {
        "mode": "lines",
        "total_lines": total_lines,
        "total_chars": total_chars,
        "start_line": s_line,
        "end_line": actual_end,
        "has_more": has_more,
        "next_start_line": (actual_end + 1) if actual_end < total_lines else None,
        "truncated": has_more,
        "active_heading": _active_heading_at(lines, s_line),
        "content": "\n".join(selected),
    }


def search_document(
    text: str,
    query: str,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
    context_lines: int = 2,
    max_matches: int = 25,
) -> dict[str, Any]:
    """在超长文档内做关键词或正则精准定位，返回行号、所属 Markdown 章节与前后文片段。"""
    q = query.strip()
    if not q:
        return {"query": query, "total_matches": 0, "truncated": False, "matches": []}
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(q if regex else re.escape(q), flags)
    lines = text.splitlines()
    ctx_n = max(0, min(int(context_lines), 5))
    cap = max(1, min(int(max_matches), 100))

    matches: list[dict[str, Any]] = []
    total_matches = 0
    heading_stack: list[tuple[int, str]] = []
    in_fence = False

    for idx, raw in enumerate(lines):
        s = raw.strip()
        if _FENCE_RE.match(s):
            in_fence = not in_fence
        elif not in_fence:
            hm = _HEADING_RE.match(s)
            if hm:
                lvl = len(hm.group(1))
                title = hm.group(2).strip()
                while heading_stack and heading_stack[-1][0] >= lvl:
                    heading_stack.pop()
                heading_stack.append((lvl, title))

        if pattern.search(raw):
            total_matches += 1
            if len(matches) < cap:
                before = lines[max(0, idx - ctx_n) : idx]
                after = lines[idx + 1 : min(len(lines), idx + 1 + ctx_n)]
                matches.append({
                    "line_no": idx + 1,
                    "section": " > ".join(t for _, t in heading_stack)[:200] if heading_stack else None,
                    "line": raw[:400],
                    "context_before": [ln[:240] for ln in before],
                    "context_after": [ln[:240] for ln in after],
                })

    return {
        "query": q,
        "regex": regex,
        "total_lines": len(lines),
        "total_matches": total_matches,
        "truncated": total_matches > len(matches),
        "matches": matches,
    }


def sample_large_text(text: str, *, max_total_chars: int = 18_000) -> str:
    """对超大文档执行「首部 + 中段 + 尾部」有界采样，用于快速业务归类而不加载全文进模型。"""
    if len(text) <= max_total_chars:
        return text
    head_n = int(max_total_chars * 0.55)
    mid_n = int(max_total_chars * 0.25)
    tail_n = max_total_chars - head_n - mid_n
    mid_start = max(head_n, (len(text) - mid_n) // 2)
    return (
        text[:head_n]
        + "\n\n... [中段采样] ...\n\n"
        + text[mid_start : mid_start + mid_n]
        + "\n\n... [尾部采样] ...\n\n"
        + text[-tail_n:]
    )
