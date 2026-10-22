"""企微只读验收：先核定供应商，再复用员工已原生确认的委托；不自动同意。"""
from __future__ import annotations

import argparse
import asyncio
import json
import stat
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from xuanpu_server.connections.providers.wecom import WeComClient, WeComError


def private_config(path: Path) -> dict[str,Any]:
    """私有配置不得被组或其他用户读取，解析错误不回显文件。"""
    if path.is_symlink() or stat.S_IMODE(path.stat().st_mode)&0o077:
        raise ValueError('配置必须是权限600的普通私有文件')
    config=json.loads(path.read_text())
    if not isinstance(config,dict):
        raise ValueError('需要JSON对象')
    return config


async def provider_checks(config: dict[str,Any]) -> dict[str,Any]:
    """实际企微GET只验证接口、范围和字段，不把app token当个人授权。"""
    client=WeComClient()
    try:
        token,_=await client.token(config['corp_id'],config['corp_secret'])
        result=await client.profile(token,config['visible_test_userid'])
        outside: Any='not_checked'
        try:
            await client.profile(token,config['outside_scope_test_userid'])
        except WeComError as exc:
            outside={'status':'provider_rejected','provider_code':exc.provider_code,
                     'scope_restriction':'requires_operator_verification'}
        else:
            outside='unexpectedly_readable'
        return {'token':'passed','visible_profile':'passed',
            'fields':sorted(result['data']),'missing_fields':result['missing_fields'],
            'outside_scope':outside,'employee_consent':'not_proven_by_app_token'}
    finally:
        await client.close()


async def platform_checks(config: dict[str,Any],cid: str) -> dict[str,Any]:
    """真实员工已在浏览器同意后，仅补读和读取；不创建consent或伪造Cookie。"""
    token=config.get('visible_browser_session')
    if not token:
        return {'status':'not_checked','reason':'需要测试员工真实登录后的私有会话及已原生确认的委托'}
    origin=config['base_url'].rstrip('/')
    parsed=urlsplit(origin)
    if parsed.scheme!='https' or parsed.path:
        raise ValueError('真实平台验收必须使用正式HTTPS源')
    async with httpx.AsyncClient(base_url=origin,timeout=30,trust_env=False,follow_redirects=False,
            cookies={'__Host-xuanpu_session':token},headers={'Origin':origin,'X-Requested-By':'xuanpu'}) as client:
        me=await client.get('/v1/me')
        me.raise_for_status()
        if (me.json().get('key_id') is not None or not me.json().get('user_id')
            or me.json().get('tenant_id')!=config['tenant_id']):
            raise ValueError('需要真实员工浏览器会话')
        response=await client.post(f'/v1/me/connections/{quote(cid,safe="")}/read',json={'operation':'self_profile.read','parameters':{}})
        if response.status_code!=200:
            return {'status':'failed','http_status':response.status_code}
        result=response.json()
        if result.get('data',{}).get('userid','').lower()!=config['visible_test_userid'].lower():
            raise ValueError('实际平台成员与指定测试成员不符')
        return {'status':'passed','fields':sorted(result['data']),
            'source':result['source'],'missing_fields':result['missing_fields'],
            'revoke_and_cold_recovery':'not_checked','native_oauth':'requires_separate_browser_evidence'}


async def run(args: argparse.Namespace) -> int:
    """证据只记录字段名和状态，不写姓名、账号、Secret、token或Cookie。"""
    args.evidence.mkdir(parents=True,exist_ok=False)
    report: dict[str,Any]={'environment':args.environment,'status':'partial',
        'real_supplier':'not_checked','real_platform':'not_checked','external_write_calls':0,
        'native_oauth':'not_checked','revocation_and_scope_shrink':'not_checked'}
    try:
        config=await asyncio.to_thread(private_config,args.credentials)
        needed=('corp_id','agent_id','corp_secret','visible_test_userid','outside_scope_test_userid')
        missing=[k for k in needed if not isinstance(config.get(k),str) or not config[k].strip() or any(
            marker in config[k] for marker in ('填写','测试成员','仅在本文件'))]
        if missing:
            report.update(status='blocked',missing_fields=missing)
        elif config.get('trusted_ip_verified') is not True or config.get('trusted_domain_verified') is not True:
            report.update(status='blocked',reason='请先核定可信IP和可信域名')
        else:
            report['real_supplier']=await provider_checks(config)
            if report['real_supplier']['outside_scope']=='unexpectedly_readable':
                report.update(status='failed',reason='指定范围外成员实际可读，须先核对应用范围')
            if args.connection:
                report['real_platform']=await platform_checks(config,args.connection)
    except (OSError,ValueError,KeyError,WeComError,httpx.HTTPError) as exc:
        report.update(status='failed',error_type=type(exc).__name__)
    await asyncio.to_thread((args.evidence/'result.json').write_text,json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':report['status'],'evidence':str(args.evidence/'result.json')},ensure_ascii=False))
    # 真实原生登录、撤销、范围缩小和冷恢复必须另签，本脚本不自动给出全部通过。
    return 0 if report['status']=='partial' else 2


def main() -> int:
    """显式环境、独立证据目录及私有路径，不接受Secret命令行参数。"""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--environment',choices=('staging','production'),required=True)
    parser.add_argument('--connection',default='',help='已核定的玄圃安装ID')
    return asyncio.run(run(parser.parse_args()))


if __name__=='__main__':
    raise SystemExit(main())
