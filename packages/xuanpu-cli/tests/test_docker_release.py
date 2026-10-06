"""验证镜像发布失败不会改变部署版本或携带私有配置。"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize('push_fails', [True, False])
def test_release_version_is_written_only_after_push(tmp_path: Path, push_fails: bool) -> None:
    """构建器隔离输入且成功发布后才替换 Compose 版本文件。"""
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
    (directory / '.env').write_text('VERSION=previous\nIMAGE=registry/example\n')
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
    env = dict(os.environ, PATH=f'{binaries}:{os.environ["PATH"]}',
               PUSH_FAILS='yes' if push_fails else 'no', VERSION='release-test', IMAGE='registry/example')
    result = subprocess.run(['bash', str(directory / 'deploy.sh')], env=env,
                            capture_output=True, text=True, check=False)
    assert (result.returncode != 0) == push_fails, result.stderr
    expected = 'previous' if push_fails else 'release-test'
    assert f'VERSION={expected}\n' in (directory / '.env').read_text()
