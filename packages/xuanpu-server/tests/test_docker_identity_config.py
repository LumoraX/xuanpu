"""容器身份必须显式传递，私有宿主目录不因测试变成公开目录。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from taifeng_sandbox import SandboxUnavailableError
from taifeng_sandbox.docker import DockerEnvironment
from xuanpu_server.bootstrap import _sandboxes
from xuanpu_server.config import SandboxConfig
from xuanpu_server.errors import RuntimeUnavailableError
from xuanpu_server.runtime.sandbox import DockerSandboxProvider


def test_bootstrap_keeps_explicit_docker_user_and_default_image_user() -> None:
    """rootful部署可以选择UID，未配置时继续使用镜像身份以兼容rootless映射。"""
    chosen = _sandboxes(SimpleNamespace(sandbox=SandboxConfig(backend='docker', user='1001:1001')))
    assert chosen._user == '1001:1001'
    default = _sandboxes(SimpleNamespace(sandbox=SandboxConfig(backend='docker')))
    assert default._user is None


@pytest.mark.parametrize('user', ['', ' ', '-1', '1001:', '1001:bad', 'root', '1:2:3'])
def test_docker_identity_rejects_ambiguous_or_option_like_values(user: str) -> None:
    """只接受明确数字身份，不接收镜像用户名、负数或不完整组号。"""
    with pytest.raises(ValidationError):
        SandboxConfig(backend='docker', user=user)


def test_local_backend_rejects_docker_identity() -> None:
    """本机隔离不能静默忽略容器身份配置。"""
    with pytest.raises(ValidationError, match='user'):
        SandboxConfig(backend='local', user='1001')


@pytest.mark.parametrize('user', [None, '1001:1001'])
async def test_script_and_connector_keep_same_selected_identity(tmp_path, monkeypatch, user) -> None:
    """两种容器都消费同一个身份，且不改写宿主私有目录权限。"""
    seen = []

    async def create(config, *, name=None):
        """在启动边界捕获真实配置并制造受控启动失败。"""
        seen.append(config)
        raise SandboxUnavailableError('test boundary')

    async def remove(config, name):
        """模拟尚未创建实际容器时的幂等清理。"""

    monkeypatch.setattr(DockerEnvironment, 'create', create)
    monkeypatch.setattr(DockerEnvironment, 'remove', remove)
    package = SimpleNamespace(root=tmp_path / 'package',
        record=SimpleNamespace(tenant_id='t', name='p', version='1'),
        manifest=SimpleNamespace(spec=SimpleNamespace(policy=SimpleNamespace(
            sandbox=SimpleNamespace(network=False)))))
    package.root.mkdir(mode=0o700)
    provider = DockerSandboxProvider(image='python:3.12-slim', docker_env={}, user=user)
    for method, name in [(provider.open, 'scripts'), (provider.open_connectors, 'connectors')]:
        workspace = tmp_path / name
        workspace.mkdir(mode=0o700)
        with pytest.raises(RuntimeUnavailableError, match='test boundary'):
            await method(package, workspace)
        assert workspace.stat().st_mode & 0o777 == 0o700
    assert len(seen) == 2 and all(config.user == user for config in seen)
