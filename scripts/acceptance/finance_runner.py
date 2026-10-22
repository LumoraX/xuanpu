"""固定财务隔离验收的输入门禁、真实JUnit汇总和可追溯执行。"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import signal
import stat
import subprocess
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

import asyncpg
from defusedxml import ElementTree  # type: ignore[import-untyped]
from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from pathlib import Path

QUICK_MODULES = [
    'packages/xuanpu-finance/tests/test_f8_materials.py',
    'packages/xuanpu-finance/tests/test_f8_api.py',
    'packages/xuanpu-finance/tests/test_f8_contract.py',
    'packages/xuanpu-finance/tests/test_f8_harness.py',
    'packages/xuanpu-finance/tests/test_f7_process.py',
    'packages/xuanpu-finance/tests/test_f7_records.py',
    'packages/xuanpu-finance/tests/test_f7_api.py',
    'packages/xuanpu-finance/tests/test_f7_contract.py',
    'packages/xuanpu-server/tests/identity/test_finance_f7_gateway_e2e.py',
    'packages/xuanpu-console/tests/test_finance_behavior.py',
    'packages/xuanpu-finance/tests/test_worker.py',
    'packages/xuanpu-finance/tests/test_expense_domain.py',
    'packages/xuanpu-finance/tests/test_f2_http_e2e.py',
    'packages/xuanpu-finance/tests/test_f3_http_e2e.py',
    'packages/xuanpu-finance/tests/test_f5_http_e2e.py',
    'packages/xuanpu-finance/tests/test_f5_archive_restore.py',
    'packages/xuanpu-finance/tests/test_f2_lifecycle_api.py',
    'packages/xuanpu-finance/tests/test_f4_lifecycle.py',
    'packages/xuanpu-finance/tests/test_f6_credential_isolation.py',
    'packages/xuanpu-finance/tests/test_f6_readiness.py',
    'packages/xuanpu-server/tests/identity/test_finance_f5_gateway_e2e.py',
]


def validate_dsn(dsn: str) -> None:
    """仅接受本机明确测试库，禁止查询参数改变真实连接目的地。"""
    try:
        parts=urlsplit(dsn)
        valid=(parts.scheme in {'postgresql','postgres'} and
            parts.hostname in {'127.0.0.1','localhost','::1'} and
            parts.username and parts.password and parts.port is not None and
            re.fullmatch(r'/finance_test[a-zA-Z0-9_]*',parts.path) and
            not parts.query and not parts.fragment)
    except ValueError:
        valid=False
    if not valid:
        raise ValueError('TEST_DSN_REQUIRED')


def summarize(path: Path, *, returncode: int, expected_modules: list[str], suite: str) -> dict[str, Any]:
    """统计实际case并校对固定模块，suite自报总数不作为通过事实。"""
    result: dict[str,Any]={'status':'failed','production_status':'not_verified',
        'tests':0,'failures':0,'errors':0,'skipped':0,'missing_modules':list(expected_modules)}
    try:
        tree=ElementTree.parse(path)
        cases=list(tree.iter('testcase'))
    except (OSError,ValueError,ElementTree.ParseError,DefusedXmlException):
        result['diagnostic']='JUNIT_INVALID'
        return result
    classes={case.get('classname','') for case in cases}
    result.update(tests=len(cases),failures=sum(c.find('failure') is not None for c in cases),
        errors=sum(c.find('error') is not None for c in cases),
        skipped=sum(c.find('skipped') is not None for c in cases),
        missing_modules=[m for m in expected_modules if m.removesuffix('.py').replace('/','.') not in classes])
    finance_skips=[c for c in cases if c.find('skipped') is not None and
        ('xuanpu-finance' in c.get('classname','') or 'test_finance' in c.get('classname',''))]
    result['skipped_cases']=[{'class':c.get('classname',''),'name':c.get('name','')}
        for c in cases if c.find('skipped') is not None]
    blocked=(returncode!=0 or not cases or result['missing_modules'] or result['failures'] or
        result['errors'] or finance_skips or (suite=='quick' and result['skipped']))
    if not blocked:
        result['status']='partial' if result['skipped'] else 'passed'
    return result


async def check_database(dsn: str) -> None:
    """实际只读连接核对数据库身份，不迁移或创建任何资源。"""
    validate_dsn(dsn)
    conn=await asyncpg.connect(dsn,timeout=10,server_settings={'default_transaction_read_only':'on'})
    try:
        actual=await conn.fetchval('SELECT current_database()')
        if actual!=unquote(urlsplit(dsn).path[1:]) or not actual.startswith('finance_test'):
            raise ValueError('TEST_DATABASE_REQUIRED')
    finally:
        await conn.close()


def source_state(root: Path) -> dict[str,str]:
    """绑定实际HEAD和已跟踪/未跟踪源码字节，不写入文件正文。"""
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    digest=hashlib.sha256(subprocess.check_output(['git','diff','HEAD','--binary'],cwd=root))
    names=subprocess.check_output(['git','ls-files','--others','--exclude-standard','-z'],cwd=root).split(b'\0')
    for raw in sorted(n for n in names if n):
        p=root/os.fsdecode(raw)
        if p.is_file():
            digest.update(raw);digest.update(p.read_bytes())
    return {'head':head,'worktree_sha256':digest.hexdigest()}


def redact(raw: str, dsn: str) -> str:
    """清除连接正文和密码，异常及JUnit同样不能泄漏测试凭据。"""
    parts=urlsplit(dsn)
    values={dsn,parts.password or '',unquote(parts.password or '')}
    for value in sorted((v for v in values if v),key=len,reverse=True):
        raw=raw.replace(value,'[redacted]').replace(html.escape(value),'[redacted]')
    return raw


def child_environment(dsn: str) -> dict[str,str]:
    """清掉隐式库地址及测试选择，平台fixture只能创建自己的隔离库。"""
    blocked={'XUANPU_TEST_PG_DSN','FINANCE_TEST_DSN','PYTEST_PLUGINS',
             'PYTEST_ADDOPTS','PYTEST_DISABLE_PLUGIN_AUTOLOAD'}
    env={k:v for k,v in os.environ.items() if k not in blocked and not k.startswith('PG')}
    return {**env,'FINANCE_TEST_DSN':dsn,'PYTEST_ADDOPTS':''}


def signal_group(pid: int, sig: signal.Signals) -> None:
    """仅向本入口以新session创建的固定组发送终止信号。"""
    try:
        os.killpg(pid,sig)
    except ProcessLookupError:
        # 整个组已退出是目标状态，无需再次发送。
        return


async def stop_process(process: asyncio.subprocess.Process,
                       communication: asyncio.Task[tuple[bytes,bytes]]) -> None:
    """即使组长已退出也终止持管道后代，以通信结束判断退出。"""
    signal_group(process.pid,signal.SIGTERM)
    try:
        await asyncio.wait_for(asyncio.shield(communication),2)
    except TimeoutError:
        signal_group(process.pid,signal.SIGKILL)
        await asyncio.wait_for(process.wait(),2)


async def run_acceptance(*, root: Path, dsn_file: Path, output: Path, suite: str, timeout: int) -> dict[str,Any]:
    """执行固定pytest入口，实际结果及失败均以证据形式保全。"""
    if suite not in {'quick','full'} or not 1<=timeout<=7200:
        raise ValueError('ACCEPTANCE_INPUT_INVALID')
    info=dsn_file.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size>65536:
        raise ValueError('DSN_FILE_PRIVATE_REQUIRED')
    dsn=await asyncio.to_thread(dsn_file.read_text)
    dsn=dsn.strip()
    await check_database(dsn)
    identity=await asyncio.to_thread(source_state,root)
    output.mkdir(mode=0o700,parents=False,exist_ok=False)
    modules=QUICK_MODULES if suite=='quick' else ['packages']
    args=[sys.executable,'-m','pytest',*modules,'-q','-ra','--junitxml='+str(output/'junit.xml')]
    env=child_environment(dsn)
    started=datetime.now(UTC).isoformat();diagnostic=None;raw=b'';code=-1
    process=await asyncio.create_subprocess_exec(*args,cwd=root,env=env,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT,start_new_session=True)
    communication=asyncio.create_task(process.communicate())
    try:
        raw,_=await asyncio.wait_for(asyncio.shield(communication),timeout)
        code=process.returncode if process.returncode is not None else -1
    except (TimeoutError,asyncio.CancelledError) as error:
        diagnostic='ACCEPTANCE_TIMEOUT' if isinstance(error,TimeoutError) else 'ACCEPTANCE_CANCELLED'
        try:
            await stop_process(process,communication)
        except (OSError,TimeoutError):
            diagnostic='ACCEPTANCE_TERMINATION_FAILED'
        try:
            raw,_=await asyncio.wait_for(asyncio.shield(communication),3)
        except TimeoutError:
            communication.cancel()
            await asyncio.gather(communication,return_exceptions=True)
            diagnostic='ACCEPTANCE_DRAIN_TIMEOUT'
    finally:
        (output/'pytest.log').write_text(redact(raw.decode('utf-8',errors='replace'),dsn))
        junit=output/'junit.xml'
        if junit.exists():
            junit.write_text(redact(junit.read_text(),dsn))
        report=summarize(junit,returncode=code,expected_modules=QUICK_MODULES,suite=suite)
        report.update(suite=suite,source=identity,modules=modules,started_at=started,
            finished_at=datetime.now(UTC).isoformat(),returncode=code,diagnostic=diagnostic,
            environment='isolated_loopback_finance_test',external_suppliers='not_verified',
            cleanup_required=diagnostic is not None)
        report['evidence_sha256']={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [output/'pytest.log',junit] if p.exists()}
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report
