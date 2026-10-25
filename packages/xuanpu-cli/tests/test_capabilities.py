"""能力命令保留显式确认、私有凭据和原操作号。"""
from __future__ import annotations

import asyncio
import json

import pytest
from xuanpu_cli.main import build_parser


def test_capabilities_cli_requires_private_credentials_and_explicit_body(tmp_path):
    """不能通过请求覆盖身份或自动创建确认正文。"""
    parser = build_parser()
    assert 'capabilities' in parser.format_help(), '尚未接入能力命令'
    from xuanpu_cli.capabilities import run_capabilities
    credentials = tmp_path / 'credentials.json'
    credentials.write_text(json.dumps({'base_url': 'https://office.test', 'api_key': 'private-key'}))
    body = tmp_path / 'body.json'
    body.write_text(json.dumps({'actor': 'someone-else', 'confirmed': True}))
    args = parser.parse_args(['capabilities', '--credentials', str(credentials), 'install', 'cap-test', '--body', str(body)])
    credentials.chmod(0o644)
    with pytest.raises(ValueError):
        asyncio.run(run_capabilities(args))
    credentials.chmod(0o600)
    with pytest.raises(ValueError):
        asyncio.run(run_capabilities(args))
