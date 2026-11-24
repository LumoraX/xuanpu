"""规范包的异常类型。"""

from __future__ import annotations


class SpecError(ValueError):
    """业务包不符合规范。"""


class ManifestError(SpecError):
    """清单文件缺失、无法解析或字段不合法。

    Attributes:
        problems: 逐条的问题描述，每条带字段路径。
    """

    def __init__(self, message: str, problems: tuple[str, ...] = ()) -> None:
        """记录问题明细。"""
        detail = "".join(f"\n  - {problem}" for problem in problems)
        super().__init__(f"{message}{detail}")
        self.problems = problems


class ArchiveError(SpecError):
    """业务包归档不合法（路径逃逸、体积超限、含不允许的文件类型等）。"""


__all__ = ["ArchiveError", "ManifestError", "SpecError"]
