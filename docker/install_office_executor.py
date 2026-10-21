"""以root安装独立rootless执行服务；不重启生产平台或宿主Docker。"""

from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import subprocess
from pathlib import Path

ACCOUNT = 'xuanpu-executor'
UID = 10002
PLATFORM_UID = 10001
HOME_DIR = Path('/var/lib/xuanpu-executor')
SHARED = Path('/var/lib/xuanpu')
DIRECTORIES = ('cache', 'workspaces', 'input-snapshots')


def run(*args: str) -> None:
    """明确参数调用系统工具，失败立即停止，不拼接命令或静默降级。"""
    subprocess.run(args, check=True)


def as_user(*args: str) -> None:
    """通过独立用户已启动的systemd总线执行，不带生产凭据。"""
    run('runuser', '-u', ACCOUNT, '--', 'env', f'XDG_RUNTIME_DIR=/run/user/{UID}',
        f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{UID}/bus', *args)


def identity() -> None:
    """仅复用同名同UID账户；UID冲突时拒绝，不改动现有用户。"""
    try:
        record = pwd.getpwuid(UID)
    except KeyError:
        try:
            group = grp.getgrgid(UID)
        except KeyError:
            run('groupadd', '--gid', str(UID), ACCOUNT)
        else:
            if group.gr_name != ACCOUNT:
                raise RuntimeError('执行GID已被其他账户占用')
        run('useradd', '--system', '--uid', str(UID), '--gid', str(UID), '--home-dir',
            str(HOME_DIR), '--create-home', '--shell', '/usr/sbin/nologin', ACCOUNT)
    else:
        if record.pw_name != ACCOUNT or Path(record.pw_dir) != HOME_DIR or record.pw_gid != UID:
            raise RuntimeError('执行UID或家目录已被其他账户占用')
    if HOME_DIR.is_symlink() or HOME_DIR.stat().st_uid != UID:
        raise RuntimeError('执行家目录不是本人目录')
    HOME_DIR.chmod(0o700)
    for filename, option in (('/etc/subuid', '--add-subuids'), ('/etc/subgid', '--add-subgids')):
        records = [line.split(':') for line in Path(filename).read_text().splitlines() if line]
        own = [row for row in records if row[0] == ACCOUNT]
        if own:
            if not any(int(row[2]) >= 65536 for row in own):
                raise RuntimeError('已有subordinate ID范围不足，需人工核对')
        else:
            end = max((int(row[1])+int(row[2]) for row in records), default=100000)
            start = ((end+65535)//65536)*65536
            run('usermod', option, f'{start}-{start+65535}', ACCOUNT)


def share_directories(deploy: Path) -> None:
    """只绑定三个执行目录到统一路径；目标有无关内容时拒绝覆盖。"""
    SHARED.mkdir(mode=0o755, exist_ok=True)
    for name in DIRECTORIES:
        source = deploy/'volumes/data'/name
        existed = source.exists()
        source.mkdir(mode=0o700, exist_ok=True)
        if not existed:
            os.chown(source, PLATFORM_UID, PLATFORM_UID)
        if source.is_symlink() or source.stat().st_uid != PLATFORM_UID:
            raise RuntimeError(f'执行目录属主或类型异常：{source}')
        destination = SHARED/name
        destination.mkdir(mode=0o700, exist_ok=True)
        same = os.path.samefile(source, destination)
        if not same and any(destination.iterdir()):
            raise RuntimeError(f'绑定目标有其他内容：{destination}')
        unit = subprocess.check_output(['systemd-escape', '--path', '--suffix=mount',
                                        str(destination)], text=True).strip()
        content = (f'[Unit]\nDescription=Xuanpu office {name} bind\nBefore=user@{UID}.service\n'
                   f'[Mount]\nWhat={source}\nWhere={destination}\nType=none\n'
                   f'Options=bind,nosuid,nodev,X-mount.idmap=b:{PLATFORM_UID}:{UID}:1\n'
                   '[Install]\nWantedBy=multi-user.target\n')
        Path('/etc/systemd/system', unit).write_text(content)
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', '--now', unit)
        if destination.stat().st_uid != UID:
            run('systemctl', 'restart', unit)
        if not os.path.samefile(source, destination) or destination.stat().st_uid != UID:
            raise RuntimeError('执行目录inode或身份映射不一致')


def service() -> None:
    """使用官方用户服务，启用开机恢复、总资源额度与挂载前置检查。"""
    # 容器scope是docker.service的兄弟，聚合额度必须在共同祖先user slice上。
    directory = Path(f'/etc/systemd/system/user-{UID}.slice.d')
    directory.mkdir(parents=True, exist_ok=True)
    (directory/'office.conf').write_text('[Slice]\nCPUQuota=200%\nMemoryMax=6G\nTasksMax=4096\n')
    # 只共享稳定目录；daemon重建socket inode后，平台无需重建即可重新连接。
    Path('/etc/tmpfiles.d/xuanpu-executor.conf').write_text(
        f'd /run/xuanpu-executor 0700 {UID} {UID} -\n'
        f'a+ /run/xuanpu-executor - - - - u:{PLATFORM_UID}:r-x,m::r-x,'
        f'd:u:{PLATFORM_UID}:rwx,d:m::rwx\n')
    run('systemd-tmpfiles', '--create', '/etc/tmpfiles.d/xuanpu-executor.conf')
    run('setfacl', '-m', f'u:{PLATFORM_UID}:rx,d:u:{PLATFORM_UID}:rwx',
        '/run/xuanpu-executor')
    run('systemctl', 'daemon-reload')
    run('loginctl', 'enable-linger', ACCOUNT)
    run('systemctl', 'start', f'user@{UID}.service')
    config = {'log-driver':'json-file', 'log-opts':{'max-size':'10m', 'max-file':'3'}}
    config['hosts'] = [f'unix:///run/user/{UID}/docker.sock',
                       'unix:///run/xuanpu-executor/docker.sock']
    content = '[Service]\nCPUQuota=200%\nMemoryMax=6G\nTasksMax=4096\nRestart=on-failure\n'
    content += 'Environment=DOCKERD_ROOTLESS_ROOTLESSKIT_NET=slirp4netns\n'
    content += ''.join(f'ExecStartPre=/usr/bin/mountpoint -q {SHARED/name}\n' for name in DIRECTORIES)
    # 用户家目录可由该用户改动；所有写入都降权，root不跟随其中的链接或chown。
    writer = '''import json,sys
from pathlib import Path
home=Path(sys.argv[1]);config=json.loads(sys.argv[2])
directory=home/'.config/docker';directory.mkdir(parents=True,exist_ok=True)
path=directory/'daemon.json'
previous={key:value for key,value in config.items() if key!='hosts'}
if path.exists() and json.loads(path.read_text()) not in (config,previous):
    raise RuntimeError('已有独立daemon配置不同，拒绝覆盖')
path.write_text(json.dumps(config)+'\\n');path.chmod(0o600)
override=home/'.config/systemd/user/docker.service.d';override.mkdir(parents=True,exist_ok=True)
(override/'office.conf').write_text(sys.argv[3])
'''
    as_user('python3', '-c', writer, str(HOME_DIR), json.dumps(config), content)
    as_user('dockerd-rootless-setuptool.sh', 'install', '--force')
    as_user('docker', 'context', 'update', 'rootless', '--docker',
            f'host=unix:///run/user/{UID}/docker.sock')
    as_user('systemctl', '--user', 'daemon-reload')
    as_user('systemctl', '--user', 'restart', 'docker')


def main() -> None:
    """校验前提后安装；目标部署目录须由root独占，防止执行用户读取生产秘密。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deploy-dir', type=Path, required=True)
    deploy = parser.parse_args().deploy_dir.resolve(strict=True)
    if os.geteuid() != 0 or deploy.stat().st_uid != 0 or deploy.stat().st_mode & 0o077:
        raise RuntimeError('需要root及root独占的部署根目录')
    if any(character in str(deploy) for character in ('\n', ' ', '%')):
        raise RuntimeError('部署目录含不支持的systemd路径字符')
    for program in ('newuidmap', 'newgidmap', 'slirp4netns', 'setfacl', 'dockerd-rootless-setuptool.sh'):
        run('/usr/bin/which', program)
    identity()
    share_directories(deploy)
    service()
    print('独立rootless执行服务已安装；尚未修改生产平台配置')


if __name__ == '__main__':
    main()
