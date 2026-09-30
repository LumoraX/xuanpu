"""冒烟测试：版本来源正确，规范包不依赖平台组件与内核。"""

from __future__ import annotations

from importlib.metadata import requires, version

import xuanpu_spec


def test_version_from_metadata_matches_distribution() -> None:
    """``__version__`` 必须等于发行元数据版本（pyproject 是唯一来源）。"""
    assert xuanpu_spec.__version__ == version("xuanpu-spec")


def test_spec_has_no_platform_or_kernel_dependency() -> None:
    """红线 6：规范是对外契约，不依赖平台其他组件，也不依赖 taifeng。"""
    declared = [
        item.split()[0].split(">")[0].split("=")[0] for item in requires("xuanpu-spec") or []
    ]
    assert sorted(declared) == ["pydantic", "pyyaml"]
