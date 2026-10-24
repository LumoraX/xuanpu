"""共享命令的原生确认、私有身份档案及固定成果导出边界。"""
from __future__ import annotations

import json

import pytest
from xuanpu_cli.main import build_parser


@pytest.mark.parametrize('arguments', [
    ['list'], ['get', 'matter'], ['members', 'matter'], ['materials', 'matter'],
    ['create', '--body', 'body.json'], ['change', 'matter', '--body', 'body.json'],
    ['invite', 'matter', '--body', 'body.json'], ['revoke', 'matter', '--member', 'member', '--body', 'body.json'],
    ['publish', 'matter', '--body', 'body.json'], ['publish-text', 'matter', '--body', 'body.json'],
    ['withdraw', 'matter', '--material', 'material', '--body', 'body.json'],
    ['download', 'matter', '--material', 'material', '--revision', '1', '--output', 'output.txt'],
    ['context-create', '--body', 'body.json'], ['context-read', 'source'],
    ['context-close', 'source', '--body', 'body.json'], ['notifications'],
])
def test_shared_commands_keep_explicit_profile_and_body(arguments):
    """不提供调用身份覆盖，写入正文必须来自可重用文件。"""
    args = build_parser().parse_args(['shared', '--credentials', 'private.json', *arguments])
    assert args.credentials == 'private.json' and args.operation == arguments[0]
    with pytest.raises(SystemExit):
        build_parser().parse_args(['shared', '--credentials', 'private.json', '--user', 'victim', 'list'])


async def test_shared_cli_preserves_confirmation_and_download_is_private(tmp_path, monkeypatch):
    """命令不替用户补确认；已核验的导出不覆盖文件且只允许本人读取。"""
    from xuanpu_cli import shared_commands
    calls = []
    class Client:
        """只观察CLI到公开SDK边界，真实HTTP另由集成测试覆盖。"""
        def __init__(self, **credentials):
            """不保留或回显凭据。"""
        async def __aenter__(self):
            """模拟客户端生命周期。"""
            return self
        async def __aexit__(self, *args):
            """本地替身没有连接资源。"""
        async def create_shared_matter(self, body):
            """必须原样接收未确认的输入。"""
            calls.append(dict(body)); return {'id': 'receipt'}
        async def download_shared_material(self, *args, **kwargs):
            """SDK已验证SHA后返回的固定字节。"""
            return b'fixed-result'
    monkeypatch.setattr(shared_commands, 'XuanpuClient', Client)
    credentials = tmp_path / 'credentials.json'; credentials.write_text(json.dumps({'base_url': 'https://example.test', 'api_key': 'synthetic'})); credentials.chmod(0o600)
    body = tmp_path / 'body.json'; body.write_text(json.dumps({'request_id': 'same', 'confirmed': False, 'title': '先核对'}))
    args = build_parser().parse_args(['shared', '--credentials', str(credentials), 'create', '--body', str(body)])
    await shared_commands.run_shared(args)
    await shared_commands.run_shared(args)
    assert calls[0] == calls[1] and calls[0]['confirmed'] is False
    output = tmp_path / 'result.txt'
    args = build_parser().parse_args(['shared', '--credentials', str(credentials), 'download', 'matter',
        '--material', 'material', '--revision', '1', '--output', str(output)])
    await shared_commands.run_shared(args)
    assert output.read_bytes() == b'fixed-result' and output.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        await shared_commands.run_shared(args)
    credentials.chmod(0o644)
    with pytest.raises(ValueError):
        await shared_commands.run_shared(args)
