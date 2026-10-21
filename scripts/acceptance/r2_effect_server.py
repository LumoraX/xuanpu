"""隔离验收HTTP下游：真实持久去重、丢回执和权威只读查询。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlsplit


def initialize(path: Path) -> None:
    """只在操作者显式给定的合成数据库中记账。"""
    with sqlite3.connect(path) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE IF NOT EXISTS effects (stable_key TEXT PRIMARY KEY,digest TEXT NOT NULL,record TEXT NOT NULL)')
    path.chmod(0o600)


def apply(path: Path,key: str,payload: dict) -> dict:
    """同稳定键同内容只写一次；不同内容拒绝，原提交记录不覆盖。"""
    raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    digest=hashlib.sha256(raw.encode()).hexdigest()
    with sqlite3.connect(path,timeout=10) as conn:
        conn.execute('BEGIN IMMEDIATE')
        previous=conn.execute('SELECT digest,record FROM effects WHERE stable_key=?',(key,)).fetchone()
        if previous is not None:
            if previous[0]!=digest:raise ValueError('stable_key_conflict')
            return json.loads(previous[1])
        record={'stable_key':key,'state':'applied','result_digest':digest,'authority':'isolated-r2-counter'}
        conn.execute('INSERT INTO effects VALUES (?,?,?)',(key,digest,json.dumps(record)))
        return record


def query(path: Path,key: str | None) -> dict:
    """只读证明实际应用数，不用HTTP成功码代替业务事实。"""
    with sqlite3.connect(path) as conn:
        count=conn.execute('SELECT count(*) FROM effects').fetchone()[0]
        row=conn.execute('SELECT record FROM effects WHERE stable_key=?',(key,)).fetchone() if key else None
    return {'applied_count':count,'receipt':json.loads(row[0]) if row else None}


async def respond(writer: asyncio.StreamWriter,status: int,body: dict) -> None:
    """测试响应不包含任何平台凭据。"""
    raw=json.dumps(body).encode()
    writer.write(f'HTTP/1.1 {status} Result\r\nContent-Type: application/json\r\nContent-Length: {len(raw)}\r\nConnection: close\r\n\r\n'.encode()+raw)
    await writer.drain()


async def handle(reader: asyncio.StreamReader,writer: asyncio.StreamWriter,path: Path) -> None:
    """事务落盘后才执行受控断线；超时和输入大小均有上限。"""
    try:
        async with asyncio.timeout(30):
            header=await reader.readuntil(b'\r\n\r\n')
            lines=header.decode('ascii').split('\r\n');method,target,_=lines[0].split()
            headers=dict(line.lower().split(':',1) for line in lines[1:] if ':' in line)
            length=int(headers.get('content-length','0'))
            if not 0<=length<=65536:raise ValueError('body_too_large')
            body=json.loads(await reader.readexactly(length)) if length else {}
            route=urlsplit(target).path
            if method=='GET' and route=='/count':result=await asyncio.to_thread(query,path,None)
            elif method=='GET' and route.startswith('/effects/'):
                result=await asyncio.to_thread(query,path,unquote(route.removeprefix('/effects/')))
            elif method=='POST' and route=='/effects':
                key=body['stable_key'];payload=body['payload']
                if not isinstance(key,str) or not 1<=len(key)<=512 or not isinstance(payload,dict):raise ValueError('invalid_effect')
                result=await asyncio.to_thread(apply,path,key,payload)
                if headers.get('x-test-reply','').strip()=='drop':return
                if headers.get('x-test-reply','').strip()=='hold':await asyncio.sleep(20)
            else:
                await respond(writer,404,{'error':'not_found'});return
            await respond(writer,200,result)
    except (ValueError,KeyError,UnicodeError,asyncio.IncompleteReadError):
        await respond(writer,409,{'error':'invalid_or_conflicting_synthetic_request'})
    finally:
        writer.close();await writer.wait_closed()


async def main() -> None:
    """禁止公开监听，只接受明确回环或私网隔离地址。"""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True);parser.add_argument('--bind',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=0);args=parser.parse_args()
    address=ipaddress.ip_address(args.bind)
    if address.is_unspecified or not address.is_private:parser.error('只允许明确的回环或隔离私网地址')
    await asyncio.to_thread(initialize,args.db)
    server=await asyncio.start_server(lambda r,w:handle(r,w,args.db),args.bind,args.port,limit=70000)
    print(json.dumps({'bind':args.bind,'port':server.sockets[0].getsockname()[1]}),flush=True)
    async with server:await server.serve_forever()


if __name__=='__main__':asyncio.run(main())
