"""读取 skill 目录：只解析校验清单所需的少量字段。

规范包不依赖 taifeng，所以这里不用内核的 skill 加载器；完整的 skill 校验在平台加载业务包
时由内核完成。这里只回答两个问题：包里有哪些 skill，其中哪些声明了可作入口。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from pathlib import Path

SKILL_FILENAME = "SKILL.md"
_FENCE = "---"


@dataclass(frozen=True)
class SkillSummary:
    """一个 skill 的概要。

    Attributes:
        skill_id: skill 标识（frontmatter 里的 ``name``）。
        directory: skill 所在目录名。
        entry: 是否声明为入口。
        tool_names: 声明使用的工具。
        problem: 读取或解析失败的原因；None 表示正常。
    """

    skill_id: str
    directory: str
    entry: bool = False
    tool_names: tuple[str, ...] = ()
    problem: str | None = None


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


def _summarize(directory: Path) -> SkillSummary:
    """读取一个 skill 目录。"""
    try:
        text = (directory / SKILL_FILENAME).read_text(encoding="utf-8")
        front = parse_frontmatter(text)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return SkillSummary(skill_id=directory.name, directory=directory.name, problem=str(exc))
    name = front.get("name")
    if not isinstance(name, str) or not name:
        return SkillSummary(
            skill_id=directory.name, directory=directory.name, problem="frontmatter 缺少 name"
        )
    tools = front.get("tool_names") or ()
    tool_names = tuple(str(item) for item in tools) if isinstance(tools, list | tuple) else ()
    return SkillSummary(
        skill_id=name,
        directory=directory.name,
        entry=front.get("entry") is True,
        tool_names=tool_names,
    )


def scan_skills(skills_dir: Path) -> list[SkillSummary]:
    """扫描 skill 目录下的每个子目录，按目录名排序。没有 SKILL.md 的子目录跳过。"""
    if not skills_dir.is_dir():
        return []
    return [
        _summarize(child)
        for child in sorted(skills_dir.iterdir())
        if child.is_dir() and (child / SKILL_FILENAME).is_file()
    ]


__all__ = ["SKILL_FILENAME", "SkillSummary", "parse_frontmatter", "scan_skills"]
