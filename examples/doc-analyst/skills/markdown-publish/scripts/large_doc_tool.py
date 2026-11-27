"""断网沙盒内的 14MB+ 超长文档与 Markdown AST 流式处理工具（零第三方依赖，O(1) 流式内存）。

吸收 Claude VM / Antigravity 大文档处理范式，支持 5 种确定性命令行动作：
- ``outline <file.md>``：流式扫描提取 YAML Frontmatter、章节 TOC（精确起止行号与字节/字数）、Markdown 表格转 JSON、代码块与清单；
- ``window <file.md> <start_line> <end_line>``：O(1) 内存精确切片读取任意行窗口（支持 14MB+ / 数十万行文档）；
- ``search <file.md> <pattern> [--regex] [--max 25]``：流式正则/关键词定位，附带行号与所属 Markdown 章节路径；
- ``split <file.md> <out_dir> [--max-chars 30000]``：沿 Markdown 章节边界将超大文档无损切分为分章文件与 ``manifest.json``（用于 Map-Reduce 并发分析）；
- ``assemble <chapters_dir> <output.md> [--title 标题]``：将多轮/并行写出的几十个章节草稿确定性拼装为数十万字/14MB+ 成品 Markdown，并自动生成目录 TOC 与字数审计报告。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})\s*([A-Za-z0-9_+-]*)")


def cmd_outline(path: Path, *, max_headings: int = 300, max_tables: int = 80) -> dict[str, Any]:
    """流式扫描超大 Markdown/TXT 文件提取骨架、TOC 行范围与结构化表格（不整文件驻留大对象）。"""
    total_bytes = path.stat().st_size
    frontmatter: dict[str, str] = {}
    headings: list[dict[str, Any]] = []
    heading_stack: list[tuple[int, str]] = []
    tables: list[dict[str, Any]] = []
    code_blocks: dict[str, int] = {}

    in_fm = False
    fm_done = False
    in_fence = False
    fence_char = ""
    fence_len = 0
    table_start = -1
    table_headers: list[str] = []
    table_rows_data: list[dict[str, str]] = []
    total_lines = 0
    total_chars = 0
    byte_offset = 0

    def _flush_table(end_line: int) -> None:
        nonlocal table_start, table_headers, table_rows_data
        if table_start > 0 and table_headers and len(tables) < max_tables:
            tables.append({
                "start_line": table_start,
                "end_line": end_line,
                "headers": table_headers[:24],
                "row_count": len(table_rows_data),
                "sample_rows": table_rows_data[:5],
            })
        table_start = -1
        table_headers = []
        table_rows_data = []

    with path.open("r", encoding="utf-8-sig", errors="replace") as fh:
        for line_no, raw in enumerate(fh, start=1):
            total_lines = line_no
            line_bytes = len(raw.encode("utf-8"))
            total_chars += len(raw)
            stripped = raw.strip()

            # Frontmatter 处理
            if line_no == 1 and stripped == "---":
                in_fm = True
                byte_offset += line_bytes
                continue
            if in_fm and not fm_done:
                if stripped in ("---", "...") or line_no > 80:
                    in_fm = False
                    fm_done = True
                elif ":" in raw and not raw.lstrip().startswith("#"):
                    k, v = raw.split(":", 1)
                    if k.strip():
                        frontmatter[k.strip()] = v.strip().strip('"').strip("'")[:300]
                byte_offset += line_bytes
                continue

            fm_m = _FENCE_RE.match(stripped)
            if fm_m:
                _flush_table(line_no - 1)
                tok = fm_m.group(1)
                if not in_fence:
                    in_fence = True
                    fence_char = tok[0]
                    fence_len = len(tok)
                    lang = (fm_m.group(2) or "text").lower()
                    code_blocks[lang] = code_blocks.get(lang, 0) + 1
                elif tok[0] == fence_char and len(tok) >= fence_len:
                    in_fence = False
                byte_offset += line_bytes
                continue
            if in_fence:
                byte_offset += line_bytes
                continue

            if stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 2:
                inner = stripped[1:-1]
                if not all(ch in "-:| " for ch in inner):
                    cells = [c.strip() for c in inner.split("|")]
                    if table_start < 0:
                        table_start = line_no
                        table_headers = cells
                        table_rows_data = []
                    else:
                        row_dict = {
                            table_headers[i] if i < len(table_headers) else f"col_{i}": cells[i]
                            for i in range(len(cells))
                        }
                        if len(table_rows_data) < 50:
                            table_rows_data.append(row_dict)
                byte_offset += line_bytes
                continue
            if table_start > 0:
                _flush_table(line_no - 1)

            hm = _HEADING_RE.match(stripped)
            if hm:
                lvl = len(hm.group(1))
                title = hm.group(2).strip()
                while heading_stack and heading_stack[-1][0] >= lvl:
                    heading_stack.pop()
                heading_stack.append((lvl, title))
                if len(headings) < max_headings:
                    headings.append({
                        "level": lvl,
                        "title": title[:160],
                        "path": " > ".join(t for _, t in heading_stack)[:240],
                        "start_line": line_no,
                        "end_line": 0,
                        "byte_offset": byte_offset,
                    })
            byte_offset += line_bytes

    if table_start > 0:
        _flush_table(total_lines)

    for i, h in enumerate(headings):
        end_line = total_lines
        for j in range(i + 1, len(headings)):
            if headings[j]["level"] <= h["level"]:
                end_line = headings[j]["start_line"] - 1
                break
        h["end_line"] = max(h["start_line"], end_line)

    return {
        "file": str(path),
        "total_bytes": total_bytes,
        "total_chars": total_chars,
        "total_lines": total_lines,
        "frontmatter": frontmatter,
        "heading_count": len(headings),
        "headings": headings,
        "table_count": len(tables),
        "tables": tables,
        "code_blocks": code_blocks,
    }


def cmd_window(path: Path, start_line: int, end_line: int, *, max_chars: int = 32_000) -> dict[str, Any]:
    """O(1) 内存流式读取指定 1-based 行区间，避免将 14MB+ 文件整份读入内存。"""
    s = max(1, start_line)
    e = max(s, end_line)
    collected: list[str] = []
    used = 0
    actual_end = s - 1
    total_lines = 0
    capped = False

    with path.open("r", encoding="utf-8-sig", errors="replace") as fh:
        for line_no, raw in enumerate(fh, start=1):
            total_lines = line_no
            if line_no < s:
                continue
            if line_no > e:
                continue
            line = raw.rstrip("\r\n")
            if collected and used + len(line) + 1 > max_chars:
                capped = True
                break
            collected.append(line)
            used += len(line) + 1
            actual_end = line_no

    return {
        "file": str(path),
        "start_line": s,
        "end_line": actual_end,
        "total_lines_scanned": total_lines,
        "capped": capped,
        "content": "\n".join(collected),
    }


def cmd_search(
    path: Path, pattern: str, *, regex: bool = False, max_matches: int = 25,
) -> dict[str, Any]:
    """O(1) 内存流式检索超大文档，返回命中行号与所属 Markdown 章节面包屑。"""
    comp = re.compile(pattern if regex else re.escape(pattern), re.IGNORECASE)
    matches: list[dict[str, Any]] = []
    total_matches = 0
    heading_stack: list[tuple[int, str]] = []
    in_fence = False

    with path.open("r", encoding="utf-8-sig", errors="replace") as fh:
        for line_no, raw in enumerate(fh, start=1):
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
            if comp.search(raw):
                total_matches += 1
                if len(matches) < max_matches:
                    matches.append({
                        "line_no": line_no,
                        "section": " > ".join(t for _, t in heading_stack)[:200] if heading_stack else None,
                        "line": raw.rstrip("\r\n")[:360],
                    })

    return {
        "file": str(path),
        "pattern": pattern,
        "total_matches": total_matches,
        "truncated": total_matches > len(matches),
        "matches": matches,
    }


def cmd_split(path: Path, out_dir: Path, *, max_chars: int = 30_000) -> dict[str, Any]:
    """沿 Markdown 章节边界将超大文档无损切分为多份有序分块文件 + manifest.json（Map 阶段输入）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks_meta: list[dict[str, Any]] = []
    buf: list[str] = []
    buf_chars = 0
    chunk_start = 1
    current_title = "引文与导语"
    in_fence = False

    def _write_chunk(end_line: int) -> None:
        nonlocal buf, buf_chars
        if not buf:
            return
        idx = len(chunks_meta) + 1
        fname = f"part_{idx:04d}.md"
        text = "".join(buf)
        (out_dir / fname).write_text(text, encoding="utf-8")
        chunks_meta.append({
            "index": idx,
            "file": fname,
            "title": current_title[:120],
            "start_line": chunk_start,
            "end_line": end_line,
            "chars": len(text),
        })
        buf = []
        buf_chars = 0

    last_line = 0
    with path.open("r", encoding="utf-8-sig", errors="replace") as fh:
        for line_no, raw in enumerate(fh, start=1):
            last_line = line_no
            s = raw.strip()
            if _FENCE_RE.match(s):
                in_fence = not in_fence
            is_split_heading = (not in_fence) and bool(_HEADING_RE.match(s)) and (len(s) - len(s.lstrip("#")) <= 2)
            if buf and (is_split_heading and buf_chars >= max_chars // 3 or buf_chars + len(raw) > max_chars):
                _write_chunk(line_no - 1)
                chunk_start = line_no
            if is_split_heading:
                hm = _HEADING_RE.match(s)
                if hm:
                    current_title = hm.group(2).strip()
            buf.append(raw)
            buf_chars += len(raw)

    _write_chunk(last_line)
    manifest = {
        "source": str(path),
        "total_parts": len(chunks_meta),
        "total_chars": sum(c["chars"] for c in chunks_meta),
        "parts": chunks_meta,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def cmd_assemble(chapters_dir: Path, output_path: Path, *, title: str | None = None) -> dict[str, Any]:
    """将分章草稿目录确定性拼装为超长成品 Markdown（支持数十万字/14MB+），自动生成目录与体量审计。"""
    files = sorted(p for p in chapters_dir.glob("*.md") if p.is_file())
    if not files:
        raise ValueError(f"目录 {chapters_dir} 下未找到任何 .md 章节文件")

    toc_entries: list[str] = []
    sections_meta: list[dict[str, Any]] = []
    bodies: list[str] = []

    for idx, f in enumerate(files, start=1):
        text = f.read_text(encoding="utf-8-sig").strip()
        if not text:
            continue
        first_heading = f.stem
        for ln in text.splitlines():
            hm = _HEADING_RE.match(ln.strip())
            if hm:
                first_heading = hm.group(2).strip()
                break
        toc_entries.append(f"{idx}. {first_heading}")
        sections_meta.append({"index": idx, "file": f.name, "title": first_heading, "chars": len(text)})
        bodies.append(text)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    total_chars = 0
    with output_path.open("w", encoding="utf-8") as out:
        if title:
            hdr = f"# {title}\n\n"
            out.write(hdr)
            total_chars += len(hdr)
        toc_block = "## 目录\n\n" + "\n".join(toc_entries) + "\n\n---\n\n"
        out.write(toc_block)
        total_chars += len(toc_block)
        for i, body in enumerate(bodies):
            if i > 0:
                out.write("\n\n")
                total_chars += 2
            out.write(body)
            total_chars += len(body)
        out.write("\n")
        total_chars += 1

    return {
        "output": str(output_path),
        "chapters": len(sections_meta),
        "total_chars": total_chars,
        "total_bytes": output_path.stat().st_size,
        "sections": sections_meta,
    }


def main(argv: list[str] | None = None) -> int:
    """命令行入口：提供 outline / window / search / split / assemble 五项大文档流式能力。"""
    parser = argparse.ArgumentParser(description="断网沙盒 14MB+ 超长文档与 Markdown AST 流式处理工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_out = sub.add_parser("outline")
    p_out.add_argument("input", type=Path)

    p_win = sub.add_parser("window")
    p_win.add_argument("input", type=Path)
    p_win.add_argument("start_line", type=int)
    p_win.add_argument("end_line", type=int)
    p_win.add_argument("--max-chars", type=int, default=32_000)

    p_src = sub.add_parser("search")
    p_src.add_argument("input", type=Path)
    p_src.add_argument("pattern", type=str)
    p_src.add_argument("--regex", action="store_true")
    p_src.add_argument("--max", type=int, default=25)

    p_spl = sub.add_parser("split")
    p_spl.add_argument("input", type=Path)
    p_spl.add_argument("out_dir", type=Path)
    p_spl.add_argument("--max-chars", type=int, default=30_000)

    p_asm = sub.add_parser("assemble")
    p_asm.add_argument("chapters_dir", type=Path)
    p_asm.add_argument("output", type=Path)
    p_asm.add_argument("--title", type=str, default=None)

    args = parser.parse_args(argv)
    if args.cmd == "outline":
        res: Any = cmd_outline(args.input)
    elif args.cmd == "window":
        res = cmd_window(args.input, args.start_line, args.end_line, max_chars=args.max_chars)
    elif args.cmd == "search":
        res = cmd_search(args.input, args.pattern, regex=args.regex, max_matches=args.max)
    elif args.cmd == "split":
        res = cmd_split(args.input, args.out_dir, max_chars=args.max_chars)
    else:
        res = cmd_assemble(args.chapters_dir, args.output, title=args.title)

    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
