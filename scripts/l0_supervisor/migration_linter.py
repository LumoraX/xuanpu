"""L0 数据库迁移 Expand-and-Contract 静态检查器（ADR 0027）。

确保 L3 内核自进化与蓝绿切流始终向后兼容：
- 严禁在自动蓝绿迁移中出现破坏性缩容 DDL（DROP TABLE / DROP COLUMN / ALTER COLUMN TYPE / RENAME）；
- 严禁新增无默认值的 NOT NULL 列，防止旧槽位代码写入失败导致回滚崩溃。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

_COMMENT_LINE_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/")
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE), "禁止在蓝绿迁移中 DROP TABLE（违反 Expand-and-Contract）"),
    (re.compile(r"\bDROP\s+COLUMN\b", re.IGNORECASE), "禁止在蓝绿迁移中 DROP COLUMN（旧槽位仍在读取该列）"),
    (
        re.compile(r"\bALTER\s+COLUMN\s+\S+\s+(?:SET\s+DATA\s+)?TYPE\b", re.IGNORECASE),
        "禁止直接修改列类型 ALTER COLUMN ... TYPE，请改用新增兼容列",
    ),
    (
        re.compile(r"\bRENAME\s+(?:COLUMN|TO)\b", re.IGNORECASE),
        "禁止直接 RENAME 表或列，请改用新增兼容列过渡",
    ),
)
_ADD_NOT_NULL_RE = re.compile(
    r"\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(\S+)\s+[^;]*?\bNOT\s+NULL\b([^;]*)",
    re.IGNORECASE,
)


def _strip_sql_comments(sql: str) -> str:
    """移除 SQL 单行与块注释，避免注释文本误报。"""
    no_block = _BLOCK_COMMENT_RE.sub(" ", sql)
    return _COMMENT_LINE_RE.sub(" ", no_block)


def lint_migration_sql(name: str, sql: str) -> list[str]:
    """对单份候选 SQL 迁移脚本执行 Expand-and-Contract 安全检查，返回违规列表。"""
    cleaned = _strip_sql_comments(sql)
    errors: list[str] = []
    for pattern, reason in _RULES:
        if pattern.search(cleaned):
            errors.append(f"{name}：{reason}")
    for match in _ADD_NOT_NULL_RE.finditer(cleaned):
        stmt = match.group(0)
        if not re.search(r"\bDEFAULT\b", stmt, re.IGNORECASE):
            col = match.group(1)
            errors.append(f"{name}：新增非空列 {col} 必须提供 DEFAULT 默认值，以确保旧版本代码兼容写入")
    return errors


def lint_migration_files(files: list[Path]) -> list[str]:
    """批量检查一组 SQL 迁移文件。"""
    errors: list[str] = []
    for path in files:
        if path.suffix.lower() != ".sql" or not path.is_file():
            continue
        errors.extend(lint_migration_sql(path.name, path.read_text(encoding="utf-8")))
    return errors


def _collect_sql_targets(raw_paths: list[str]) -> list[Path]:
    """将命令行传入的文件或目录路径展开为有序 SQL 迁移文件列表。"""
    targets: list[Path] = []
    for raw in raw_paths:
        p = Path(raw)
        if p.is_dir():
            targets.extend(sorted(p.glob("*.sql")))
        elif p.is_file():
            targets.append(p)
    return targets


def main(argv: list[str] | None = None) -> int:
    """命令行入口：对指定迁移文件或目录执行 Expand-and-Contract 静态检查并输出 JSON 报告。"""
    parser = argparse.ArgumentParser(description="L0 SQL 迁移 Expand-and-Contract 静态检查器")
    parser.add_argument("paths", nargs="+", help="SQL 文件或包含 *.sql 迁移的目录路径")
    args = parser.parse_args(argv)
    targets = _collect_sql_targets(args.paths)
    errors = lint_migration_files(targets)
    print(json.dumps({
        "ok": not errors,
        "checked_files": [p.name for p in targets if p.suffix.lower() == ".sql"],
        "errors": errors,
    }, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
