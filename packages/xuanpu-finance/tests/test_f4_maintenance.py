"""独立真实PG与加密原件的配套维护、损坏拒绝和恢复演练。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from xuanpu_finance.maintenance import MaintenanceError, MaintenanceOptions, backup, restore
from xuanpu_finance.models import FinancePrincipal
from xuanpu_finance.objects import EncryptedLocalObjects
from xuanpu_finance.storage import FinanceStore


@pytest_asyncio.fixture
async def maintenance_database(tmp_path: Path):
    """仅在显式finance_test服务创建本轮独占数据库并删除。"""
    dsn = os.environ.get('FINANCE_TEST_DSN')
    if not dsn:
        pytest.skip('须显式提供独立FINANCE_TEST_DSN')
    from urllib.parse import urlsplit, urlunsplit
    parts=urlsplit(dsn)
    assert parts.path.startswith('/finance_test')
    admin=await asyncpg.connect(dsn)
    names=['finance_test_backup_'+uuid4().hex,'finance_restore_'+uuid4().hex]
    dsns=[]
    for name in names:
        await admin.execute(f'CREATE DATABASE "{name}"')
        dsns.append(urlunsplit(parts._replace(path='/'+name)))
    source=await asyncpg.create_pool(dsns[0],min_size=1,max_size=3)
    store=FinanceStore(source)
    await store.migrate()
    await store.seed_company('t','e','隔离备份公司','BUYER')
    await store.grant('t','owner','e','collector')
    files=[]
    for i,dsn_value in enumerate(dsns):
        p=tmp_path/f'dsn-{i}';p.write_text(dsn_value);p.chmod(0o600);files.append(p)
    key=tmp_path/'key';key.write_bytes(b'k'*32)
    options=[MaintenanceOptions(files[0],tmp_path/'originals',key),MaintenanceOptions(files[1],tmp_path/'restored',key)]
    try:
        yield store,options,dsns
    finally:
        await source.close()
        for name in names:
            await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


def pg_tools(tmp_path: Path) -> tuple[Path,Path]:
    """测试通过本轮专用容器调用成熟PG工具，数据库凭据只用环境传递。"""
    container = os.environ.get('FINANCE_TEST_PG_CONTAINER')
    if container is None:
        native = [shutil.which(tool) for tool in ['pg_dump', 'pg_restore']]
        if not all(native):
            pytest.fail('请安装PG工具或显式设置本轮 FINANCE_TEST_PG_CONTAINER')
        return Path(str(native[0])), Path(str(native[1]))
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', container) is None:
        raise ValueError('测试PG容器名称无效')
    files=[]
    for tool in ['pg_dump','pg_restore']:
        path=tmp_path/tool
        path.write_text('#!/bin/sh\nexec docker exec -i -e PGDATABASE -e PGUSER -e PGPASSWORD '+container+' '+tool+' "$@"\n')
        path.chmod(0o700);files.append(path)
    return files[0],files[1]


async def source_original(store: FinanceStore, root: Path) -> bytes:
    """通过既有真实收件worker保存原件和责任索引，不直接伪造回执。"""
    from xuanpu_finance.invoices.worker import FinanceWorker

    from .test_parser import SAMPLE
    from .test_worker import Gateway, ref
    who=FinancePrincipal('t','owner')
    await store.accept_import(who,'e',[ref('source',SAMPLE)],'original')
    await FinanceWorker(store,EncryptedLocalObjects(root,b'k'*32),Gateway({'source':SAMPLE}),synthetic=True).run_once()
    return SAMPLE


async def test_backup_restore_actual_original_and_database(maintenance_database,tmp_path: Path) -> None:
    """真实dump/restore后，原件可用AAD解密，业务行与原摘要一致。"""
    store,options,dsns=maintenance_database
    raw=await source_original(store,options[0].object_root)
    dump,load=pg_tools(tmp_path)
    output=tmp_path/'backup'
    result=await backup(options[0],output,pg_dump=str(dump))
    assert result['authenticated_objects']>=1 and result['unmapped_objects']==[]
    report=await restore(options[1],output,pg_restore=str(load))
    assert report['database_restored'] and report['authenticated_objects']>=1
    conn=await asyncpg.connect(dsns[1])
    try:
        row=await conn.fetchrow('SELECT object_key,sha256 FROM finance_receipt LIMIT 1')
        assert await EncryptedLocalObjects(options[1].object_root,b'k'*32).get(row['object_key'])==raw
        assert row['sha256']==hashlib.sha256(raw).hexdigest()
    finally:
        await conn.close()
    assert not any(p.name=='key' for p in output.rglob('*'))


async def test_backup_rejects_active_instance_and_missing_original(maintenance_database,tmp_path: Path) -> None:
    """运行实例互斥及错误对象根不能产生完整备份。"""
    store,options,dsns=maintenance_database
    await source_original(store,options[0].object_root)
    dump,_=pg_tools(tmp_path)
    lock=await asyncpg.connect(dsns[0]);await lock.execute('SELECT pg_advisory_lock(617472002)')
    try:
        with pytest.raises(MaintenanceError,match='INSTANCE_ACTIVE'):
            await backup(options[0],tmp_path/'active',pg_dump=str(dump))
    finally:
        await lock.close()
    wrong=MaintenanceOptions(options[0].database_dsn_file,tmp_path/'wrong-root',options[0].key_file)
    with pytest.raises(MaintenanceError,match='OBJECT_MISSING'):
        await backup(wrong,tmp_path/'missing',pg_dump=str(dump))
    assert not (tmp_path/'missing').exists()


async def test_restore_rejects_tamper_key_and_nonempty_target(maintenance_database,tmp_path: Path) -> None:
    """恢复校验在写入前拒绝错密钥、清册篡改和已有对象根。"""
    store,options,_=maintenance_database
    await source_original(store,options[0].object_root)
    dump,load=pg_tools(tmp_path);output=tmp_path/'backup'
    await backup(options[0],output,pg_dump=str(dump))
    wrong=tmp_path/'wrong-key';wrong.write_bytes(b'x'*32)
    with pytest.raises(MaintenanceError,match='KEY_MISMATCH'):
        await restore(MaintenanceOptions(options[1].database_dsn_file,options[1].object_root,wrong),output,pg_restore=str(load))
    manifest=json.loads((output/'manifest.json').read_text())
    path=output/manifest['files'][0]['path'];path.write_bytes(path.read_bytes()+b'tampered')
    with pytest.raises(MaintenanceError,match='BACKUP_CORRUPT'):
        await restore(options[1],output,pg_restore=str(load))


async def test_corrupt_ciphertext_and_nonempty_restore_fail_closed(maintenance_database,tmp_path: Path) -> None:
    """头仍合法的损坏原件必须实际认证失败；既有目标表不能被覆盖。"""
    store,options,dsns=maintenance_database
    await source_original(store,options[0].object_root)
    dump,load=pg_tools(tmp_path);output=tmp_path/'backup'
    await backup(options[0],output,pg_dump=str(dump))
    conn=await asyncpg.connect(dsns[1]);await conn.execute('CREATE TABLE keep_me(id integer)');await conn.close()
    with pytest.raises(MaintenanceError,match='RESTORE_DATABASE_NOT_EMPTY'):
        await restore(options[1],output,pg_restore=str(load))
    cipher=next(options[0].object_root.rglob('*.enc'));raw=cipher.read_bytes()
    cipher.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
    with pytest.raises(MaintenanceError,match='OBJECT_AUTHENTICATION_FAILED'):
        await backup(options[0],tmp_path/'corrupt',pg_dump=str(dump))
    assert not (tmp_path/'corrupt').exists()


async def test_maintenance_lock_loss_terminates_pg_tool(maintenance_database,tmp_path: Path) -> None:
    """持锁连接被终止后，正在运行的工具被停止且不继续发布。"""
    import asyncio
    import sys

    from xuanpu_finance.maintenance import maintenance_session
    store,options,dsns=maintenance_database
    await source_original(store,options[0].object_root)
    async with maintenance_session(options[0]) as (guard,_,_):
        task=asyncio.create_task(guard.run([sys.executable,'-c','import time;time.sleep(60)'],dict(os.environ),stdout=asyncio.subprocess.DEVNULL))
        await asyncio.sleep(.1)
        other=await asyncpg.connect(dsns[0])
        await other.execute('SELECT pg_terminate_backend($1)',guard.connection.get_server_pid())
        await other.close()
        with pytest.raises(MaintenanceError,match='MAINTENANCE_LOCK_LOST'):
            await asyncio.wait_for(task,3)


async def test_manifest_rewrite_cannot_hide_modified_dump(maintenance_database,tmp_path: Path) -> None:
    """不能同时改文件和清册hash绕过备份完整性认证。"""
    store,options,_=maintenance_database
    await source_original(store,options[0].object_root)
    dump,load=pg_tools(tmp_path);output=tmp_path/'backup'
    await backup(options[0],output,pg_dump=str(dump))
    manifest=json.loads((output/'manifest.json').read_text())
    path=output/'database.dump';path.write_bytes(path.read_bytes()+b'tamper')
    for entry in manifest['files']:
        if entry['path']=='database.dump':
            entry.update(size=path.stat().st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    (output/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(MaintenanceError,match='BACKUP_CORRUPT'):
        await restore(options[1],output,pg_restore=str(load))


async def test_backup_never_copies_explicit_secrets_or_plain_files(maintenance_database,tmp_path: Path) -> None:
    """误把密钥放在对象目录时必须拒绝，未知普通文件不能混入密文备份。"""
    store,options,_=maintenance_database
    await source_original(store,options[0].object_root)
    secret=options[0].object_root/'private-key';secret.write_bytes(b'k'*32)
    dump,_=pg_tools(tmp_path)
    unsafe=MaintenanceOptions(options[0].database_dsn_file,options[0].object_root,secret)
    with pytest.raises(MaintenanceError,match='SENSITIVE_PATH_OVERLAP'):
        await backup(unsafe,tmp_path/'unsafe',pg_dump=str(dump))
    secret.unlink()
    (options[0].object_root/'notes.txt').write_text('private content')
    with pytest.raises(MaintenanceError,match='UNEXPECTED_OBJECT_FILE'):
        await backup(options[0],tmp_path/'plain',pg_dump=str(dump))
    assert not (tmp_path/'unsafe').exists() and not (tmp_path/'plain').exists()


async def test_provider_accepted_archive_requires_local_descriptor(maintenance_database,tmp_path: Path) -> None:
    """已真实封存的缓存进入受理待回执状态后，缺descriptor不能当prepared略过。"""
    from .test_f3_http_e2e import test_real_import_tax_hitl_archive_and_readonly_audit
    store,options,_=maintenance_database
    await test_real_import_tax_hitl_archive_and_readonly_audit(store,tmp_path)
    row=await store.pool.fetchrow("UPDATE finance_compliance_operation SET status='awaiting_provider' WHERE action='archive' RETURNING request_template")
    request=json.loads(row['request_template'])
    root=tmp_path/'objects';objects=EncryptedLocalObjects(root,b'k'*32)
    objects._path('f3/archive-staging/'+request['payload_hash']+'/descriptor.json').unlink()
    dump,_=pg_tools(tmp_path)
    with pytest.raises(MaintenanceError,match='OBJECT_MISSING'):
        await backup(MaintenanceOptions(options[0].database_dsn_file,root,options[0].key_file),tmp_path/'missing-descriptor',pg_dump=str(dump))
    assert not (tmp_path/'missing-descriptor').exists()


async def test_restore_preserves_hitl_and_unknown_requests(maintenance_database,tmp_path: Path) -> None:
    """恢复真实业务批准与F2/F3原未知请求，恢复不配置通道或启动执行器。"""
    from xuanpu_finance.invoices.worker import FinanceWorker

    from .test_expense_domain import execute
    from .test_f2_domain import action_plan, approved_payable
    from .test_f3_domain import configured
    from .test_parser import SAMPLE
    from .test_worker import Gateway, ref
    store,options,dsns=maintenance_database
    _,payer,_,approver,doc=await approved_payable(store)
    plan=await action_plan(store,payer,approver,doc,'execute_payment')
    payment=await execute(store,payer,plan)
    job=await store.claim_settlement();payment_request=await store.prepare_settlement_dispatch(job)
    await store.defer_settlement(job,'TIMEOUT',uncertain=True);await store.release_settlement(job)
    owner,_,_=await configured(store)
    await store.accept_import(owner,'e',[ref('retained',SAMPLE)],'retained')
    await FinanceWorker(store,EncryptedLocalObjects(options[0].object_root,b'k'*32),Gateway({'retained':SAMPLE}),synthetic=True).run_once()
    invoices=await store.list_invoices(owner,'e');id=next(x['id'] for x in invoices['items'] if x.get('number')=='SYN-001')
    tax=await store.verify_invoice(owner,id,1,'verify-retained')
    job=await store.claim_compliance();tax_request=await store.prepare_compliance_dispatch(job)
    await store.defer_compliance(job,'TIMEOUT',uncertain=True);await store.release_compliance(job)
    decisions=await store.pool.fetchval('SELECT count(*) FROM finance_decision')
    dump,load=pg_tools(tmp_path);output=tmp_path/'history-backup'
    await backup(options[0],output,pg_dump=str(dump));report=await restore(options[1],output,pg_restore=str(load))
    conn=await asyncpg.connect(dsns[1])
    try:
        assert await conn.fetchval('SELECT count(*) FROM finance_decision')==decisions>0
        for table,op,request in [('finance_settlement_operation',payment,payment_request),('finance_compliance_operation',tax,tax_request)]:
            restored=await conn.fetchrow(f'SELECT status,request FROM {table} WHERE id=$1',op['id'])
            assert restored['status']=='outcome_unknown' and json.loads(restored['request'])==request
    finally:
        await conn.close()
    assert report['workers_started'] is False and report['external_side_effects']==0


async def test_restore_actual_archive_cache_and_supplement(maintenance_database,tmp_path: Path) -> None:
    """完整归档及补包真实dump到新库后，所有缓存以原AAD解密且包字节一致。"""
    import io
    import zipfile

    from xuanpu_finance.maintenance_objects import archive_index

    from .test_f3_http_e2e import test_real_import_tax_hitl_archive_and_readonly_audit
    store,options,dsns=maintenance_database
    await test_real_import_tax_hitl_archive_and_readonly_audit(store,tmp_path)
    root=tmp_path/'objects'
    source=EncryptedLocalObjects(root,b'k'*32)
    indexed={}
    async with store.pool.acquire() as conn:
        await archive_index(conn,source,indexed)
    originals={key:await source.get(key) for key in indexed}
    bundles={key:raw for key,raw in originals.items() if key.endswith('/bundle.zip')}
    assert len(bundles)==2 and len(originals)>10
    for raw in bundles.values():
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            assert archive.testzip() is None and len(archive.namelist())>5
    rows=await store.pool.fetch("SELECT id,request,request_template,status FROM finance_compliance_operation ORDER BY id")
    dump,load=pg_tools(tmp_path)
    output=tmp_path/'archive-backup'
    report=await backup(MaintenanceOptions(options[0].database_dsn_file,root,options[0].key_file),output,pg_dump=str(dump))
    assert report['complete_object_validation'] and report['unmapped_objects']==[]
    restored=await restore(options[1],output,pg_restore=str(load))
    assert restored['complete_object_validation'] and not restored['workers_started'] and restored['external_side_effects']==0
    conn=await asyncpg.connect(dsns[1])
    try:
        assert await conn.fetch("SELECT id,request,request_template,status FROM finance_compliance_operation ORDER BY id")==rows
        target=EncryptedLocalObjects(options[1].object_root,b'k'*32)
        restored_index={}
        await archive_index(conn,target,restored_index)
        assert indexed==restored_index
        for key,raw in originals.items():
            assert await target.get(key)==raw
    finally:
        await conn.close()
