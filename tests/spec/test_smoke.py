"""冒烟测试：版本来源正确，规范模块不依赖平台服务与内核。"""

from __future__ import annotations

import ast
from importlib.metadata import version
from pathlib import Path

import spec


def test_version_from_metadata_matches_distribution() -> None:
    """``__version__`` 必须等于平台发行元数据版本（pyproject 是唯一来源）。"""
    assert spec.__version__ == version("xuanpu")


def test_spec_has_no_platform_or_kernel_dependency() -> None:
    """红线 6：规范是对外契约，不依赖平台其他模块，也不依赖 taifeng。"""
    spec_dir = Path(spec.__file__).resolve().parent
    forbidden = {"server", "web", "cli", "sdk", "finance", "taifeng", "taifeng_sandbox", "taifeng_stores"}
    for path in spec_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden, (path, alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden, (path, node.module)
