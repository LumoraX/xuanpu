"""两名明确员工的共享功能验收，凭据私有、结果分层，不自动同意或冒充登录。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx


def private_config(path: Path) -> dict[str, Any]:
    """读取权限600的员工会话，禁止在命令行传令牌。"""
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError('需要权限600的普通私有配置')
    config = json.loads(path.read_text())
    origin = urlsplit(config['base_url'])
    if origin.path or origin.username or origin.password or origin.query or origin.fragment:
        raise ValueError('平台地址必须是完整源')
    if origin.scheme != 'https' and not (config.get('synthetic') is True and origin.hostname in ('127.0.0.1', 'localhost')):
        raise ValueError('真实员工验收需要HTTPS')
    return config


async def call(client: httpx.AsyncClient, method: str, path: str, *, expected: int = 200, **kwargs: Any) -> Any:
    """失败只保存状态与错误码，异常不回显供应商数据或会话令牌。"""
    response = await client.request(method, path, **kwargs)
    if response.status_code != expected:
        raise RuntimeError(f'{method} {path.split("?")[0]} 返回{response.status_code}，预期{expected}')
    return response.json() if response.content else None


def change(prefix: str, action: str, version: int, **extra: Any) -> dict[str, Any]:
    """固定操作号和明确确认，重试同一验收种子不会改写已提交回执。"""
    return {'request_id': prefix + ':' + action, 'confirmed': True, 'expected_version': version, **extra}


async def exercise(config: dict[str, Any], *, model: bool = False) -> dict[str, Any]:
    """只使用两个真实已登录员工本人会话，合成身份必须独立标明。"""
    origin, prefix = config['base_url'], config['request_prefix']
    headers = {'Origin': origin, 'X-Requested-By': 'xuanpu'}
    cookie = 'xuanpu_session' if config.get('synthetic') else '__Host-xuanpu_session'
    async with httpx.AsyncClient(base_url=origin, cookies={cookie: config['alice_session']}, headers=headers,
            timeout=40, trust_env=False) as alice, httpx.AsyncClient(base_url=origin,
            cookies={cookie: config['bob_session']}, headers=headers, timeout=40, trust_env=False) as bob:
        identities = [await call(client, 'GET', '/v1/me') for client in (alice, bob)]
        if any(m.get('key_id') is not None or m['tenant_id'] != config['tenant_id'] or not m.get('user_id') for m in identities):
            raise ValueError('需要明确企业的员工浏览器会话，不接受管理员API key替代本人')
        if identities[0]['user_id'] == identities[1]['user_id']:
            raise ValueError('发起和读取必须是两名不同员工')
        report: dict[str, Any] = {'synthetic': bool(config.get('synthetic')), 'actors': [m['user_id'] for m in identities],
            'real_model': 'not_checked', 'production_release': 'not_proven', 'external_business_writes': 0}
        created = await call(alice, 'POST', '/v1/shared-matters', expected=201, json={
            'request_id': prefix + ':create', 'confirmed': True, 'title': '共享资料功能验收', 'content': '仅供明确到人的材料核对'})
        path = '/v1/shared-matters/' + created['id']
        data = ('验收材料编号：' + prefix + '\n金额：9876\n').encode()
        publication = await call(alice, 'POST', path + '/material-uploads', expected=202,
            params=change(prefix, 'upload', 1, name='功能验收.txt'), content=data, headers={'Content-Type': 'text/plain'})
        state = await call(alice, 'GET', path + '/publications/' + publication['id'])
        if state['status'] != 'ready':
            raise RuntimeError('发布尚未ready，不能声明字节验收通过')
        current = await call(alice, 'GET', path)
        granted = await call(alice, 'POST', path + '/members', expected=201, json=change(prefix, 'invite', current['version'],
            subject_kind='user', subject_id=identities[1]['user_id'], role='viewer', scope='packet',
            packet_title='明确选定的验收资料', include_content=True,
            material_revisions=[(await call(alice, 'GET', path + '/materials'))['items'][0]['revisions'][0]['id']]))
        visible = await call(bob, 'GET', path)
        materials = await call(bob, 'GET', path + '/materials')
        revision = materials['items'][0]['revisions'][0]
        download = await bob.get(path + '/materials/' + publication['material_id'] + '/content?revision=1')
        if download.status_code != 200 or hashlib.sha256(download.content).hexdigest() != hashlib.sha256(data).hexdigest():
            raise RuntimeError('实际共享字节校验不一致')
        context = await call(bob, 'POST', '/v1/shared-contexts', expected=201, json=change(prefix, 'context', visible['version'],
            shared_id=created['id'], kind='assistant_conversation', material_revision_ids=[revision['id']]))
        context_path = '/v1/shared-contexts/' + context['id']
        await call(bob, 'GET', context_path + '/read')
        if model:
            reply = await call(bob, 'POST', '/v1/assistant/route', json={'request_id': prefix + ':model',
                'shared_context_id': context['id'], 'text': '只回答所选材料里的金额数字。'})
            if '9876' not in str(reply.get('message')):
                raise RuntimeError('模型未核对材料金额，不将生命周期成功当语义验收')
            report['real_model'] = 'synthetic_model_response' if config.get('synthetic') else 'amount_checked'
        current = await call(alice, 'GET', path)
        await call(alice, 'POST', path + '/members/' + granted['id'] + '/revoke',
            json=change(prefix, 'revoke', current['version']))
        for endpoint in (path, path + '/materials/' + publication['material_id'] + '/content?revision=1'):
            denied = await bob.get(endpoint)
            if denied.status_code != 404:
                raise RuntimeError('撤权后的读取没有被拒绝')
        denied = await bob.get(context_path + '/read')
        if denied.status_code != 403 or denied.json()['error']['code'] != 'shared_access_revoked':
            raise RuntimeError('旧上下文未被阻断')
        metadata = await call(bob, 'GET', context_path)
        await call(bob, 'POST', context_path + '/close', json=change(prefix, 'close', metadata['version']))
        current = await call(alice, 'GET', path)
        await call(alice, 'PATCH', path, json=change(prefix, 'archive', current['version'], status='archived'))
        report.update(status='passed', shared_id=created['id'], context_id=context['id'], material_sha256=revision['sha256'],
            cleanup={'reader_grant': 'revoked', 'context': 'closed', 'matter': 'archived', 'history': 'retained'})
        return report


def main() -> None:
    """显式执行才创建合成或已授权试点记录，报告不包含凭据或材料正文。"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--model', action='store_true')
    args = parser.parse_args()
    if not args.execute:
        parser.error('需要明确--execute；脚本将创建并归档验收共享事项')
    report = asyncio.run(exercise(private_config(args.config), model=args.model))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    args.output.chmod(0o600)
    print(json.dumps({'status': report['status'], 'synthetic': report['synthetic']}))


if __name__ == '__main__':
    main()
