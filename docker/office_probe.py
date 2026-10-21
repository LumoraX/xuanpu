"""通过生产实际配置验证rootless资源限制、只读输入和安全成果导出。"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from taifeng import CommandSpec
from taifeng_sandbox import export_files
from taifeng_sandbox.docker import DockerEnvironment, DockerSandboxConfig, Mount
from xuanpu_server.config import load_config

PROGRAM = '''from pathlib import Path
import errno,json,socket
assert socket.if_nameindex()==[(1,'lo')]
for path in ['/run/xuanpu/config.json','/var/lib/xuanpu/objects','/var/run/docker.sock',
             '/run/xuanpu-executor/docker.sock']:
    assert not Path(path).exists(),path
assert Path('inputs/fixed.txt').read_text()=='fixed-input'
try:
    Path('inputs/fixed.txt').write_text('changed')
except OSError as error:
    assert error.errno==errno.EROFS
else:
    raise AssertionError('input writable')
try:
    Path('/root/escape').write_text('changed')
except OSError:
    pass
else:
    raise AssertionError('root writable')
memory=Path('/sys/fs/cgroup/memory.max').read_text().strip()
cpu=Path('/sys/fs/cgroup/cpu.max').read_text().strip().split()
pids=Path('/sys/fs/cgroup/pids.max').read_text().strip()
assert memory=='536870912' and pids=='256'
assert cpu[0]!='max' and int(cpu[0])/int(cpu[1])<=1
Path('outputs/proof.txt').write_text('office-isolated')
print(json.dumps({'input_read_only':True,'network':'loopback only','memory_max':memory,
 'cpu_max':cpu,'pids_max':pids,'secrets_and_sockets_absent':True}))
'''


def info(binary: str, env: dict[str, str]) -> dict:
    """读取明确执行端点并拒绝rootful或不兑现资源配额的守护进程。"""
    result = subprocess.check_output([binary, 'info', '--format', '{{json .}}'], env=env,
                                     text=True, timeout=20)
    value = json.loads(result)
    if ('name=rootless' not in value['SecurityOptions'] or value['CgroupVersion'] != '2'
        or value['CgroupDriver'] != 'systemd'):
        raise RuntimeError('办公执行必须使用带systemd/cgroup v2资源限制的独立rootless服务')
    return value


async def main() -> None:
    """真实执行和实际导出均成功才输出证据，所有资源在finally中收尾。"""
    config = load_config(Path('/run/xuanpu/config.json'), variables=os.environ)
    section = config.sandbox
    if section.backend != 'docker':
        raise RuntimeError('办公探针只接受显式Docker后端')
    daemon = await asyncio.to_thread(info, section.docker_binary, section.docker_env)
    with tempfile.TemporaryDirectory(dir=config.data_dir/'workspaces', prefix='office-probe-') as temp:
        workspace = Path(temp)
        for name in ('outputs','scratch','inputs'):
            (workspace/name).mkdir()
        with tempfile.TemporaryDirectory(dir=config.data_dir/'input-snapshots', prefix='office-probe-') as inputs:
            source = Path(inputs)
            (source/'fixed.txt').write_text('fixed-input')
            environment = await DockerEnvironment.create(DockerSandboxConfig(
                image=section.image, workspace_host_dir=workspace,
                mounts=(Mount(source,'/workspace/inputs'),), network=False, memory_mb=512, cpus=1,
                docker_env=section.docker_env, docker_binary=section.docker_binary))
            try:
                process = await environment.executor().start(CommandSpec(
                    command=shlex.join(['python3','-c',PROGRAM]), shell=False, cwd='/workspace',
                    env={'PATH':'/usr/local/bin:/usr/bin:/bin'}))
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
                if process.returncode != 0:
                    raise RuntimeError('办公隔离探针失败：'+stderr.decode())
            finally:
                await environment.close()
            files = await export_files(workspace,['outputs/proof.txt'])
            if files['outputs/proof.txt'].content != b'office-isolated':
                raise RuntimeError('明确绑定目录导出内容不一致')
            print(json.dumps({'daemon_version':daemon['ServerVersion'], 'rootless':True,
                'cgroup_driver':daemon['CgroupDriver'], 'isolation':json.loads(stdout),
                'export_sha256':files['outputs/proof.txt'].sha256},ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
