"""读取 skill 目录：支持平铺与按分类嵌套目录，只解析校验与索引所需的少量字段。

规范包不依赖 taifeng，所以这里不用内核的 skill 加载器；完整的 skill 校验在平台加载业务包
时由内核完成。这里支持两类布局：

- 平铺：``skills/<skill_id>/SKILL.md``
- 分类：``skills/<分类>/<skill_id>/SKILL.md``（或多级分类 ``skills/<大类>/<小类>/<skill_id>/SKILL.md``）

一旦某个目录包含 ``SKILL.md`` 即视为一个完整的 skill，不再继续向下扫描其 ``references/`` 或
``scripts/`` 子目录。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import yaml

from spec.localization import DisplayNameValue, valid_locale

if TYPE_CHECKING:
    from pathlib import Path

SKILL_FILENAME = "SKILL.md"
_FENCE = "---"
_SKIPPED_DIRS = frozenset({"__pycache__", "node_modules", ".git", ".venv"})


@dataclass(frozen=True)
class SkillSummary:
    """一个 skill 的概要。

    Attributes:
        skill_id: skill 标识（frontmatter 里的 ``name``）。
        directory: skill 所在末级目录名。
        rel_path: 相对 skill 根目录的 POSIX 路径（如 ``geo/location`` 或 ``triage``）。
        category: 所属分类（优先 frontmatter 里的 ``category``，否则由目录相对父路径派生）。
        tags: frontmatter 里声明的检索标签。
        description: frontmatter 里的简要说明。
        skill_type: ``atomic`` 或 ``composite``。
        entry: 是否声明为入口。
        child_skills: 声明调用的子 skill 标识。
        tool_names: 声明使用的工具。
        display_name: 给员工看的名字，可按语言提供多个版本；None 表示用 skill_id。
        problem: 读取或解析失败的原因；None 表示正常。
    """

    skill_id: str
    directory: str
    entry: bool = False
    tool_names: tuple[str, ...] = ()
    display_name: DisplayNameValue | None = None
    problem: str | None = None
    rel_path: str = ""
    category: str | None = None
    tags: tuple[str, ...] = ()
    description: str = ""
    skill_type: str = "atomic"
    child_skills: tuple[str, ...] = ()

    @property
    def location(self) -> str:
        """用于报错与索引的稳定相对路径。"""
        return self.rel_path or self.directory


def parse_frontmatter(text: str) -> dict[str, Any]:
    """取出 SKILL.md 开头两行 ``---`` 之间的 YAML。

    Raises:
        ValueError: 没有 frontmatter、没有闭合，或内容不是映射。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        raise ValueError("文件开头没有 frontmatter（应以 --- 开始）")
    for index in range(1, len(lines)):
        if lines[index].strip() == _FENCE:
            try:
                data = yaml.safe_load("\n".join(lines[1:index]))
            except yaml.YAMLError as exc:
                raise ValueError(f"frontmatter 不是合法的 YAML：{exc}") from exc
            if not isinstance(data, dict):
                raise ValueError("frontmatter 必须是映射")
            return data
    raise ValueError("frontmatter 没有闭合（缺少结尾的 ---）")


def _valid_category(value: object) -> bool:
    """分类名：非空单行相对路径风格（如 ``geo`` 或 ``geo/maps``），不含 ``..``。"""
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped or len(stripped) > 128 or set(stripped) & {"\n", "\r", "\\"}:
        return False
    if stripped.startswith("/") or stripped.endswith("/"):
        return False
    return all(part and part not in {".", ".."} for part in stripped.split("/"))


def _parse_tags(raw: object) -> tuple[str, ...] | None:
    """解析检索标签；缺省返回空元组，类型非法返回 None。"""
    if raw is None:
        return ()
    if not isinstance(raw, list | tuple):
        return None
    result: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip() or len(item) > 64:
            return None
        result.append(item.strip())
    return tuple(result)


def _summarize(directory: Path, skills_root: Path) -> SkillSummary:
    """读取一个 skill 目录。"""
    rel = directory.relative_to(skills_root)
    rel_path = rel.as_posix()
    derived_category = rel.parent.as_posix() if len(rel.parts) > 1 else None
    try:
        text = (directory / SKILL_FILENAME).read_text(encoding="utf-8")
        front = parse_frontmatter(text)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return SkillSummary(
            skill_id=directory.name, directory=directory.name, rel_path=rel_path,
            category=derived_category, problem=str(exc),
        )
    name = front.get("name")
    if not isinstance(name, str) or not name:
        return SkillSummary(
            skill_id=directory.name, directory=directory.name, rel_path=rel_path,
            category=derived_category, problem="frontmatter 缺少 name",
        )
    raw_category = front.get("category")
    if raw_category is not None and not _valid_category(raw_category):
        return SkillSummary(
            skill_id=name, directory=directory.name, rel_path=rel_path,
            problem="category 须是非空相对分类名（如 geo 或 geo/maps）",
        )
    category = raw_category.strip() if isinstance(raw_category, str) else derived_category
    tags = _parse_tags(front.get("tags"))
    if tags is None:
        return SkillSummary(
            skill_id=name, directory=directory.name, rel_path=rel_path,
            category=category, problem="tags 须是非空字符串数组",
        )
    display = front.get("display_name")
    if display is not None and not _valid_display(display):
        return SkillSummary(
            skill_id=name, directory=directory.name, rel_path=rel_path,
            category=category, problem="display_name 须是 1–40 字的单行文字",
        )
    tools = front.get("tool_names") or ()
    tool_names = tuple(str(item) for item in tools) if isinstance(tools, list | tuple) else ()
    children = front.get("child_skills") or ()
    child_skills = tuple(str(item) for item in children) if isinstance(children, list | tuple) else ()
    raw_desc = front.get("description")
    description = raw_desc if isinstance(raw_desc, str) else ""
    raw_type = front.get("type")
    skill_type = raw_type if isinstance(raw_type, str) else "atomic"
    return SkillSummary(
        skill_id=name,
        directory=directory.name,
        rel_path=rel_path,
        category=category,
        tags=tags,
        description=description,
        skill_type=skill_type,
        entry=front.get("entry") is True,
        child_skills=child_skills,
        tool_names=tool_names,
        display_name=display,
    )


def _valid_display(value: object) -> bool:
    """显示名：字符串或非空语言映射，值为 1–40 字单行。"""
    if isinstance(value, str):
        return 1 <= len(value.strip()) <= 40 and not set(value) & {"\n", "\r"}
    if not isinstance(value, dict) or not value:
        return False
    return all(
        isinstance(locale, str)
        and valid_locale(locale)
        and isinstance(name, str)
        and 1 <= len(name.strip()) <= 40
        and not set(name) & {"\n", "\r"}
        for locale, name in value.items()
    )


def _walk_skill_dirs(current: Path, found: list[Path]) -> None:
    """递归发现包含 SKILL.md 的目录；命中后不再向下遍历该 skill 内部子目录。"""
    for child in sorted(current.iterdir()):
        if not child.is_dir() or child.name.startswith(".") or child.name in _SKIPPED_DIRS:
            continue
        if (child / SKILL_FILENAME).is_file():
            found.append(child)
            continue
        _walk_skill_dirs(child, found)


def scan_skills(skills_dir: Path) -> list[SkillSummary]:
    """扫描 skill 目录（支持平铺与按分类嵌套），按相对路径排序。"""
    if not skills_dir.is_dir():
        return []
    found: list[Path] = []
    _walk_skill_dirs(skills_dir, found)
    return [_summarize(directory, skills_dir) for directory in found]


__all__ = ["SKILL_FILENAME", "SkillSummary", "parse_frontmatter", "scan_skills"]
