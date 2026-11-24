"""知识命令（R4-D/F）：凭据须仅本人可读；依据格式本地核对；命令按操作调用对应接口。"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from cli.main import build_parser


def _credentials(tmp_path, mode=0o600):
    """写一份凭据文件。"""
    path = tmp_path / 'credentials.json'
    path.write_text(json.dumps({'base_url': 'https://office.test', 'api_key': 'private-key'}))
    path.chmod(mode)
    return str(path)


def test_credentials_must_be_private_and_pins_well_formed(tmp_path):
    """凭据他人可读即拒绝；依据不是「来源ID:块序号」即拒绝。"""
    from cli.knowledge_commands import _pins, run_knowledge
    parser = build_parser()
    args = parser.parse_args(['knowledge', '--credentials', _credentials(tmp_path, 0o644), 'sources'])
    with pytest.raises(ValueError):
        asyncio.run(run_knowledge(args))
    assert _pins(['ksr_1:0', 'ksr:2:3']) == [{'source_id': 'ksr_1', 'ordinal': 0}, {'source_id': 'ksr:2', 'ordinal': 3}]
    for bad in ('ksr_1', 'ksr_1:x', ':1'):
        with pytest.raises(ValueError):
            _pins([bad])


def test_pin_and_ask_reach_the_right_endpoints(tmp_path, monkeypatch, capsys):
    """pin 与 ask 走问答接口，请求体就是命令参数。"""
    from cli import knowledge_commands
    seen = []

    def receive(request):
        seen.append(request)
        return httpx.Response(200, json={'ok': True})

    original = knowledge_commands.XuanpuClient

    def client(**kwargs):
        return original(**kwargs, transport=httpx.MockTransport(receive))

    monkeypatch.setattr(knowledge_commands, 'XuanpuClient', client)
    credentials = _credentials(tmp_path)
    parser = build_parser()
    for argv in (['pin', '--request-id', 'c1', '--source', 'ksr_1:0'],
                 ['ask', 'kcx_1', '--request-id', 'q1', '--question', '口径？']):
        args = parser.parse_args(['knowledge', '--credentials', credentials, *argv])
        assert args.handler(args) == 0
    assert [(r.method, r.url.path) for r in seen] == [
        ('POST', '/v1/me/knowledge/contexts'), ('POST', '/v1/me/knowledge/contexts/kcx_1/ask')]
    assert json.loads(seen[0].content) == {'request_id': 'c1', 'pins': [{'source_id': 'ksr_1', 'ordinal': 0}]}
    assert '"ok": true' in capsys.readouterr().out
