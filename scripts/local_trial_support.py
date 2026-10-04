"""本机部署的私有配置、输入校验与资源归属检查。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from xuanpu_server.secrets import generate_master_key
from xuanpu_server.tenancy import generate_root_key

if TYPE_CHECKING:
    from pathlib import Path


def private_dir(path: Path) -> None:
    """拒绝符号链接并限制运行目录权限。"""
    if any(part.is_symlink() for part in (path, *path.parents)):
        # macOS 的 /tmp、/var 是系统链接，调用者应先规范化父目录。
        raise RuntimeError('运行目录不能含符号链接')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise RuntimeError('运行目录不属于当前用户')
    path.chmod(0o700)


def write_private(path: Path, value: object) -> None:
    """原子替换私有 JSON，创建时就限制权限。"""
    if path.is_symlink():
        raise RuntimeError('私有文件不能是符号链接')
    temporary = path.with_name(path.name + '.new')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_private(path: Path) -> dict:
    """秘密输入必须属于当前用户且不给组和其他用户权限。"""
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise RuntimeError('输入与状态文件须属于当前用户且权限为 0600')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('输入必须是 JSON 对象')
    return value


def validate_input(data: dict) -> dict:
    """校验配置而不在错误中带回秘密，根网关地址自动补 /v1。"""
    result = json.loads(json.dumps(data))
    for section, fields in {'model': ('base_url', 'api_key', 'name'),
                            'feishu': ('app_id', 'app_secret')}.items():
        if not isinstance(result.get(section), dict):
            raise ValueError('缺少 model 或 feishu 配置段')
        if any(not isinstance(result[section].get(field), str) or not result[section][field].strip()
               for field in fields):
            raise ValueError('模型和飞书必要字段不能为空')
    model, feishu = result['model'], result['feishu']
    url = urlsplit(model['base_url'])
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('模型地址须是无凭据、查询串或片段的 HTTP 地址')
    model['base_url'] = urlunsplit((url.scheme, url.netloc, '/v1' if url.path in ('', '/') else url.path.rstrip('/'), '', ''))
    model.setdefault('protocol', 'codex_responses')
    if model['protocol'] not in ('chat', 'responses', 'codex_responses'):
        raise ValueError('不支持的模型协议')
    for field in ('encrypt_key', 'verification_token'):
        if not isinstance(feishu.get(field, ''), str):
            raise ValueError('事件密钥必须是字符串')
        feishu.setdefault(field, '')
    return result


def feishu_payload(feishu: dict) -> dict:
    """空事件密钥映射为空引用，登录仅依赖 App Secret。"""
    return {'app_id': feishu['app_id'], 'app_secret_ref': 'feishu-app-secret',
            'encrypt_key_ref': 'feishu-encrypt-key' if feishu.get('encrypt_key') else None,
            'verification_token_ref': 'feishu-verification-token' if feishu.get('verification_token') else None}


def require_free_port(port: int) -> None:
    """启动之前拒绝任何已有监听者，不接管未知服务。"""
    try:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', port))
    except OSError as error:
        raise RuntimeError(f'端口 {port} 已占用；请停止原服务或指定其他端口') from error


def prepare(directory: Path, data: dict, port: int, ticket_port: int, pg_port: int) -> dict:
    """首次生成配置；停机后允许更新输入，绝不轮换已有主密钥。"""
    private_dir(directory)
    data = validate_input(data)
    if len({port, ticket_port, pg_port}) != 3 or any(not 0 < value < 65536 for value in (port, ticket_port, pg_port)):
        raise ValueError('三个端口须各不相同且在 1–65535 范围')
    fingerprint = hashlib.sha256(json.dumps([data, port, ticket_port, pg_port], sort_keys=True).encode()).hexdigest()
    credentials = directory / 'credentials.json'
    if credentials.exists():
        saved = read_private(credentials)
        if not (directory / 'config.json').is_file():
            raise RuntimeError('已有配置不完整；请恢复 config.json，不得重新生成主密钥')
        config = read_private(directory / 'config.json')
        if saved['input_fingerprint'] != fingerprint:
            if saved.get('postgres_port', pg_port) != pg_port:
                raise RuntimeError('已有数据库端口不可隐式更改；请沿用首次启动的 --postgres-port')
            model = data['model']
            config['models'].update(base_url=model['base_url'], api_key=model['api_key'],
                                    protocol=model['protocol'], tiers={tier: {'model': model['name']}
                                    for tier in ('fast', 'balanced', 'deep')})
            config['listen']['port'] = port
            config['public_url'] = f'http://127.0.0.1:{port}'
            saved.update(input_fingerprint=fingerprint, bootstrap_complete=False)
            write_private(directory / 'config.json', config)
            write_private(credentials, saved)
        return saved
    if (directory / 'config.json').exists():
        raise RuntimeError('已有配置缺少凭据文件；请恢复凭据，不得重新生成主密钥')
    root, digest = generate_root_key()
    saved = {'root': root, 'postgres_password': secrets.token_hex(24), 'tickets_token': secrets.token_hex(24),
             'postgres_port': pg_port, 'input_fingerprint': fingerprint, 'owner': hashlib.sha256(str(directory).encode()).hexdigest()[:16]}
    model = data['model']
    config = {'data_dir': str(directory / 'data'), 'root_key_hash': digest,
              'master_keys': {'k1': base64.b64encode(generate_master_key()).decode()}, 'primary_master_key': 'k1',
              'listen': {'host': '127.0.0.1', 'port': port}, 'public_url': f'http://127.0.0.1:{port}',
              'models': {'base_url': model['base_url'], 'api_key': model['api_key'], 'protocol': model['protocol'],
                         'tiers': {tier: {'model': model['name']} for tier in ('fast', 'balanced', 'deep')}},
              'control_plane': {'backend': 'postgres', 'postgres': {
                  'dsn': f'postgresql://xuanpu:{saved["postgres_password"]}@127.0.0.1:{pg_port}/xuanpu'}},
              'state': {'backend': 'local'}, 'identity': {'cookie_secure': False, 'default_tenant': 'acme',
                  'feishu_open_base': 'https://open.feishu.cn', 'feishu_accounts_base': 'https://accounts.feishu.cn'}}
    write_private(directory / 'config.json', config)
    write_private(credentials, saved)
    return saved
