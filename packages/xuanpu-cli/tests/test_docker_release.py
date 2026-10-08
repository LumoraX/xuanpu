"""验证镜像发布失败不会改变部署版本或携带私有配置，部署环境信息只来自 .env。"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _prepare_checkout(tmp_path: Path, env_file: str) -> tuple[Path, Path]:
    """搭一个只含发布脚本与假构建输入的仓库副本，并放好假的 uv、docker。"""
    checkout = tmp_path / 'checkout'
    directory = checkout / 'docker'
    directory.mkdir(parents=True)
    script = ROOT / 'docker/deploy.sh'
    assert script.is_file(), '需要实现发布脚本'
    shutil.copy(script, directory / 'deploy.sh')
    for name in ['Dockerfile', 'trial_tickets.py', 'ticket-api.py']:
        (directory / name).write_text('构建输入')
    (checkout / 'examples/ticket-api').mkdir(parents=True)
    (checkout / 'examples/ticket-api/server.py').write_text('样例输入')
    (checkout / 'config.json').write_text('must-not-enter-image')
    (directory / '.env').write_text(env_file)
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    uv = binaries / 'uv'
    uv.write_text('''#!/bin/bash
set -eu
while [[ $# -gt 0 ]]; do
 case "$1" in
 --out-dir) mkdir -p "$2"; touch "$2/test.whl"; exit;;
 --output-file) echo 'locked-runtime-dependency' > "$2"; exit;;
 esac
 shift
done
''')
    docker = binaries / 'docker'
    docker.write_text('''#!/bin/bash
set -eu
echo "$*" >> "$DOCKER_CALLS"
if [[ "$1" == buildx && "$2" == build ]]; then
 context="${!#}"
 if find "$context" -name config.json | read -r; then exit 90; fi
 test -f "$context/requirements.txt"
 test -f "$context/wheels/test.whl"
fi
if [[ "$1" == push && "$PUSH_FAILS" == yes ]]; then exit 19; fi
''')
    uv.chmod(0o755)
    docker.chmod(0o755)
    return directory, binaries


def _run_deploy(tmp_path: Path, env_file: str, *, push_fails: bool = False,
                image: str | None = 'registry/example') -> tuple[subprocess.CompletedProcess[str], Path]:
    """在隔离副本里跑发布脚本，返回结果与部署目录。"""
    directory, binaries = _prepare_checkout(tmp_path, env_file)
    env = {key: value for key, value in os.environ.items() if key != 'IMAGE'}
    env.update(PATH=f'{binaries}:{os.environ["PATH"]}', PUSH_FAILS='yes' if push_fails else 'no',
               VERSION='release-test', DOCKER_CALLS=str(tmp_path / 'docker-calls.log'))
    if image is not None:
        env['IMAGE'] = image
    result = subprocess.run(['bash', str(directory / 'deploy.sh')], env=env,
                            capture_output=True, text=True, check=False)
    return result, directory


@pytest.mark.parametrize('push_fails', [True, False])
def test_release_version_is_written_only_after_push(tmp_path: Path, push_fails: bool) -> None:
    """构建器隔离输入且成功发布后才替换 Compose 版本文件；其余部署配置项原样保留。"""
    result, directory = _run_deploy(
        tmp_path, 'VERSION=previous\nIMAGE=registry/example\nPROXY_NETWORK=proxy-test\n', push_fails=push_fails)
    assert (result.returncode != 0) == push_fails, result.stderr
    expected = 'previous' if push_fails else 'release-test'
    env_file = (directory / '.env').read_text()
    assert f'VERSION={expected}\n' in env_file
    assert 'PROXY_NETWORK=proxy-test\n' in env_file
    assert env_file.count('IMAGE=') == 1


def test_image_falls_back_to_env_file(tmp_path: Path) -> None:
    """环境变量没给 IMAGE 时，从 docker/.env 读镜像仓库。"""
    result, directory = _run_deploy(tmp_path, 'IMAGE=registry/from-env-file\n', image=None)
    assert result.returncode == 0, result.stderr
    assert 'IMAGE=registry/from-env-file\n' in (directory / '.env').read_text()
    assert 'registry/from-env-file:release-test' in (tmp_path / 'docker-calls.log').read_text()


def test_missing_image_stops_before_build(tmp_path: Path) -> None:
    """环境变量与 docker/.env 都没有 IMAGE 时直接报错，不带任何默认仓库去构建。"""
    result, _ = _run_deploy(tmp_path, 'PROXY_NETWORK=proxy-test\n', image=None)
    assert result.returncode == 2
    assert '需要 IMAGE' in result.stderr
    assert not (tmp_path / 'docker-calls.log').exists()


def test_compose_requires_proxy_network_from_env() -> None:
    """Compose 的外部代理网络名必须由 .env 给出，不在仓库里写默认值。"""
    compose = (ROOT / 'docker/compose.yml').read_text()
    assert 'name: ${PROXY_NETWORK:?' in compose
    example = (ROOT / 'docker/.env.example').read_text()
    for key in ['IMAGE=', 'VERSION=', 'PROXY_NETWORK=']:
        assert key in example
