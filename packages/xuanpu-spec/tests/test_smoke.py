"""工程骨架冒烟测试：版本来源正确。"""

from __future__ import annotations

from importlib.metadata import version

import xuanpu_spec


def test_version_from_metadata_matches_distribution() -> None:
    """``__version__`` 必须等于发行元数据版本（pyproject 是唯一来源）。"""
    assert xuanpu_spec.__version__ == version("xuanpu-spec")
