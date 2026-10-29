"""CI明确声明uv运行时根，保留虚拟环境链接且不暴露其父目录。"""
from __future__ import annotations

from pathlib import Path

import pytest

from . import conftest


def test_managed_python_keeps_alias_installation_root(tmp_path, monkeypatch) -> None:
    """只有版本目录会丢失uv稳定别名，专用安装根同时保留两者。"""
    runtime = tmp_path / 'uv-python'
    version = runtime / 'cpython-3.13.18-linux-gnu'
    version.mkdir(parents=True)
    (runtime / 'cpython-3.13-linux-gnu').symlink_to(version.name)
    venv = tmp_path / '.venv'
    venv.mkdir()
    secret = tmp_path / 'secret-outside-runtime'
    secret.write_text('unrelated private data')
    monkeypatch.setattr(conftest.sys, 'prefix', str(venv))
    monkeypatch.setattr(conftest.sys, 'base_prefix', str(version))
    monkeypatch.setenv('UV_PYTHON_INSTALL_DIR', str(runtime))
    roots = conftest.connector_runtime_roots()
    assert set(roots) == {venv, version, runtime}
    assert not any(secret.is_relative_to(root) for root in roots)
    assert tmp_path not in roots


def test_system_python_does_not_mount_unused_uv_path(tmp_path, monkeypatch) -> None:
    """系统Python不使用uv下载目录，不挂载不存在或无关的路径。"""
    system = tmp_path / 'system'
    system.mkdir()
    monkeypatch.setattr(conftest.sys, 'prefix', str(system))
    monkeypatch.setattr(conftest.sys, 'base_prefix', str(system))
    monkeypatch.setenv('UV_PYTHON_INSTALL_DIR', str(tmp_path / 'not-installed'))
    assert conftest.connector_runtime_roots() == (system,)


def test_uv_runtime_cannot_authorize_entire_filesystem(monkeypatch) -> None:
    """错误的全盘安装根必须显式失败，不能扩大测试沙盒读权限。"""
    monkeypatch.setenv('UV_PYTHON_INSTALL_DIR', str(Path('/').resolve()))
    with pytest.raises(ValueError, match='专用'):
        conftest.connector_runtime_roots()
