"""实际解析办公Compose，验证平台只接独立rootless执行接口。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_office_compose_keeps_default_confinement_and_private_executor() -> None:
    """通过真实Compose合并验证：不能继承bubblewrap的额外放行或连接rootful接口。"""
    result = subprocess.run([
        'docker', 'compose', '-f', str(ROOT/'docker/compose.yml'),
        '-f', str(ROOT/'docker/compose.office.yml'), 'config', '--format', 'json',
    ], env={**os.environ, 'IMAGE':'registry/example', 'VERSION':'test',
            'PROXY_NETWORK':'test-network', 'EXECUTOR_SOCKET_DIR':'/run/xuanpu-executor'},
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    platform = json.loads(result.stdout)['services']['platform']
    assert platform['security_opt'] == ['no-new-privileges:true']
    assert platform['cap_drop'] == ['ALL'] and platform['read_only']
    binds = {mount['target']:mount['source'] for mount in platform['volumes']}
    assert binds['/run/xuanpu-executor'] == '/run/xuanpu-executor'
    assert '/var/run/docker.sock' not in binds.values()
    assert len(platform['volumes']) == 3


def test_service_image_includes_pinned_official_cli_without_engine() -> None:
    """只复制已固定摘要的CLI，不将dockerd或Docker宿主管理权限引入平台。"""
    dockerfile = (ROOT/'docker/Dockerfile').read_text()
    assert 'FROM docker:29-cli@sha256:' in dockerfile
    assert 'COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker' in dockerfile
    assert 'COPY --from=docker-cli /usr/local/bin/dockerd' not in dockerfile
