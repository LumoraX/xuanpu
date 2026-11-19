"""办公样板成果的端到端用例（R4-E0/E1）：在办公沙盒容器里实际运行 ``examples/office-review``。

两层核对，都要办公容器（``office_image`` 夹具）；取不到办公镜像或起不了容器时，与 ``work_tasks`` 下的
办公容器用例以同样的原因失败，不会被当成通过：

- 脚本层：样板包只读挂进断网容器，直接跑它的汇总脚本，核对数字、四类成果与清单摘要、
  公式前缀文本在 XLSX 里仍是字符串、异常行按文件与行号列出；
- 平台层：管理员经产品接口发布同一归档、登记、预检、安装、评测、分配、发布，员工本人上传 CSV
  经能力入口交办，成果经平台冻结、校验并保存到本人空间；两家合成企业各跑各的，互相读不到。

平台层内存与 PG 各跑一遍。XLSX 用标准库按 OOXML 结构读，不要求测试进程装表格库。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from .client_helpers import employee_client
from .test_office_replication import SAMPLE, _install

SHEET_NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
ROLES = {'summary_csv', 'summary_xlsx', 'report', 'evidence'}
ARGS = ['["input_1.csv"]', '项目', '金额', '']
SCRIPT_ARGS = {'inputs': ARGS[0], 'group_column': '项目', 'amount_column': '金额', 'currency_column': ''}
DOCKER_ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(Path.home())}


def _sample(name: str) -> bytes:
    """样板自带的确定性输入。"""
    return (SAMPLE / 'evals' / 'inputs' / name).read_bytes()


def _expected() -> dict:
    """样板声明的 basic.csv 预期：甲 170.00、乙 80.00、总计 250.00、有效行 3。"""
    return json.loads((SAMPLE / 'evals' / 'expected' / 'basic.json').read_text())


def _cells(content: bytes) -> tuple[list[list[str]], int]:
    """按 OOXML 结构读首个工作表：返回逐行的非空字符串单元格文本，以及公式单元格个数。

    只认字符串类型（共享字符串或内联字符串）的单元格；数值、公式结果等其他类型不计入文本，
    所以「文本出现在返回值里」就等于「它是作为字符串保存的」。内容来自本测试自己跑出的成果。
    """
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        sheet = ElementTree.fromstring(archive.read('xl/worksheets/sheet1.xml'))  # noqa: S314
        shared: list[str] = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            table = ElementTree.fromstring(archive.read('xl/sharedStrings.xml'))  # noqa: S314
            shared = [''.join(node.text or '' for node in item.iter(SHEET_NS + 't'))
                      for item in table.iter(SHEET_NS + 'si')]
    rows = []
    for row in sheet.iter(SHEET_NS + 'row'):
        texts = []
        for cell in row.iter(SHEET_NS + 'c'):
            kind = cell.get('t')
            if kind == 's':
                texts.append(shared[int(cell.findtext(SHEET_NS + 'v') or '0')])
            elif kind == 'inlineStr':
                texts.append(''.join(node.text or '' for node in cell.iter(SHEET_NS + 't')))
        rows.append([text for text in texts if text])
    return rows, len(list(sheet.iter(SHEET_NS + 'f')))


def _summary(evidence: dict) -> dict:
    """证据里与样板预期对应的部分。"""
    return {key: evidence[key] for key in _expected()}


@pytest.fixture
async def review_script(office_image, docker_user, tmp_path):
    """样板包与输入只读挂进断网的办公容器，经真实沙盒执行器跑包里的脚本。"""
    import shlex

    import taifeng
    from taifeng_sandbox.docker import DockerEnvironment, DockerSandboxConfig, Mount

    inputs, workspace = tmp_path / 'inputs', tmp_path / 'work'
    inputs.mkdir()
    (workspace / 'inputs').mkdir(parents=True)
    environment = await DockerEnvironment.create(DockerSandboxConfig(
        image=office_image, user=docker_user, workspace_host_dir=workspace, memory_mb=512, cpus=1,
        network=False, docker_env=DOCKER_ENV,
        mounts=(Mount(SAMPLE / 'package', '/package'), Mount(inputs, '/workspace/inputs')),
        labels={'xuanpu.test': '1', 'xuanpu.office': '1'}))

    async def run(script: str, content: bytes, args: list[str]):
        """输入由宿主固定为 input_1.csv；脚本只能读它，成果写进容器工作区。"""
        (inputs / 'input_1.csv').write_bytes(content)
        process = await environment.executor().start(taifeng.CommandSpec(
            command=shlex.join(['python3', '-u', '-I', '/package/skills/review/scripts/' + script, *args]),
            shell=False, cwd='/workspace', env={'PATH': DOCKER_ENV['PATH'], 'WORKSPACE': '/workspace'}))
        stdout, stderr = await process.communicate()
        return SimpleNamespace(code=await process.wait(), stdout=stdout.decode(), stderr=stderr.decode(),
                               outputs=workspace / 'outputs')

    try:
        yield run
    finally:
        await environment.close()


async def test_basic_sample_writes_four_results_matching_their_digests(review_script):
    """basic.csv：甲 170.00、乙 80.00、总计 250.00、有效行 3；四类成果与清单齐全，脚本回执摘要与文件一致。"""
    result = await review_script('summarize.py', _sample('basic.csv'), ARGS)
    assert result.code == 0, result.stderr
    evidence = json.loads((result.outputs / 'evidence.json').read_text())
    assert _summary(evidence) == _expected()
    assert [row['row'] for row in evidence['rows']] == [2, 3, 4]
    assert evidence['inputs'] == [{'name': 'input_1.csv', 'sha256': hashlib.sha256(_sample('basic.csv')).hexdigest()}]
    files = {'outputs/' + path.name: path.read_bytes() for path in result.outputs.iterdir()}
    assert set(files) == {'outputs/' + name for name in (
        'summary.csv', 'summary.xlsx', 'report.md', 'evidence.json', 'manifest.json')}
    receipt = json.loads(result.stdout)
    assert receipt['outputs_sha256'] == {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    assert receipt['record_count'] == 3 and receipt['totals'] == _expected()['totals']
    manifest = json.loads(files['outputs/manifest.json'])
    assert manifest['kind'] == 'table_summary'
    assert {entry['role'] for entry in manifest['artifacts']} == ROLES
    assert all(files[entry['path']] for entry in manifest['artifacts'])
    assert '甲,未注明,170.00,2' in files['outputs/summary.csv'].decode('utf-8-sig')
    assert '币种合计 未注明：250.00（3 行）' in files['outputs/report.md'].decode()
    rows, formulas = _cells(files['outputs/summary.xlsx'])
    assert formulas == 0 and ['分组', '甲', '未注明', '170.00'] in rows and ['币种合计', '未注明', '250.00'] in rows


async def test_formula_prefixed_text_is_stored_as_text(review_script):
    """用户文本以 = + - @ 开头：XLSX 里是字符串单元格、没有任何公式；CSV 加单引号；证据保留原值。"""
    names = ['=1+1', '+SUM(A1)', '-2+3', '@cmd']
    source = '项目,金额\n' + ''.join(f'{name},{index}0.00\n' for index, name in enumerate(names, start=1))
    result = await review_script('summarize.py', source.encode(), ARGS)
    assert result.code == 0, result.stderr
    evidence = json.loads((result.outputs / 'evidence.json').read_text())
    assert sorted(entry['group'] for entry in evidence['groups']) == sorted(names)
    assert evidence['totals'] == [{'currency': '未注明', 'amount': '100.00', 'records': 4}]
    rows, formulas = _cells((result.outputs / 'summary.xlsx').read_bytes())
    assert formulas == 0, 'XLSX 里不能有公式单元格'
    stored = {row[1]: row[3] for row in rows if row and row[0] == '分组'}
    assert stored == {'=1+1': '10.00', '+SUM(A1)': '20.00', '-2+3': '30.00', '@cmd': '40.00'}
    exported = (result.outputs / 'summary.csv').read_text(encoding='utf-8-sig')
    for name in names:
        assert f"分组,'{name}," in exported, exported


async def test_invalid_sample_fails_listing_file_and_rows(review_script):
    """invalid.csv：脚本非零退出，按文件与行号列出缺金额与非法数字，不产生任何成功清单或归零的汇总。"""
    result = await review_script('summarize.py', _sample('invalid.csv'), ARGS)
    assert result.code != 0
    listed = json.loads(result.stderr.strip().splitlines()[-1].split('无效原始行：', 1)[1])
    assert [(entry['file'], entry['row']) for entry in listed] == [('input_1.csv', 3), ('input_1.csv', 4)]
    assert all(entry['error'] for entry in listed)
    assert not (result.outputs / 'manifest.json').exists() and not (result.outputs / 'evidence.json').exists()


@asynccontextmanager
async def open_stage(identity_stores, world_opener, sandboxes):
    """装配真实平台：双后端、持久执行、两家合成企业各有管理员与一名员工；沙盒供给方由调用方给。"""
    from xuanpu_server.capabilities.config import CapabilityDeliveryConfig
    from xuanpu_server.identity.config import IdentityConfig
    from xuanpu_server.identity.service import IdentitySettings
    from xuanpu_server.platform import ROOT_PRINCIPAL
    from xuanpu_server.work_tasks.config import DurableExecutionConfig

    control, identity = identity_stores
    world = await world_opener(
        store=control, identity_store=identity, sandboxes=sandboxes,
        identity_settings=IdentitySettings.from_config('https://office.test', IdentityConfig()),
        durable_execution=DurableExecutionConfig(enabled=True, verified_text_bounds=True))
    platform, clients = world.platform, {}
    try:
        platform.capabilities.config = CapabilityDeliveryConfig(enabled=True, tenants=('acme', 'beta'))
        await platform.create_tenant(ROOT_PRINCIPAL, tenant_id='beta', name='第二合成企业')
        for tenant, employee in (('acme', 'usr_alice'), ('beta', 'usr_bob')):
            clients[tenant] = SimpleNamespace(
                tenant=tenant, employee_id=employee,
                admin=await employee_client(platform, tenant, 'usr_admin', 'tenant_admin'),
                employee=await employee_client(platform, tenant, employee, 'tenant_member'))
        yield SimpleNamespace(world=world, platform=platform, models=world.models, **clients)
    finally:
        for company in clients.values():
            await company.admin.aclose()
            await company.employee.aclose()
        await platform.close()


@pytest.fixture
async def stage(identity_stores, world_opener, office_image, docker_user):
    """办公镜像上的平台：任务在断网办公容器里执行，输入只读挂载。"""
    from xuanpu_server.runtime.sandbox import DockerSandboxProvider

    provider = DockerSandboxProvider(user=docker_user, image=office_image, memory_mb=512,
                                     docker_env=DOCKER_ENV, labels={'xuanpu.test': 'r4-office-review'})
    async with open_stage(identity_stores, world_opener, provider) as opened:
        yield opened


async def _released(stage, company, title: str, step) -> dict:
    """本企业管理员安装同一归档，评测通过后只发布给本企业这名员工；返回安装记录。"""
    record = await _install(company.admin, title)
    cap, installation = record['capability'], record['installation']['id']
    stage.models.script(step(text='可以对你本人提供的 CSV 做精确汇总并列出异常行。'),
                        step(text='请先告诉我分组列和金额列的名称。'))
    started = await company.admin.post('/v1/capability-evaluations', json={
        'request_id': 'evaluate', 'capability_id': cap, 'installation_id': installation,
        'suite': json.loads((SAMPLE / 'evals' / 'suite.json').read_text())})
    assert started.status_code == 202, started.text
    async with asyncio.timeout(120):
        while (evaluation := (await company.admin.get(
                f"/v1/capability-evaluations/{started.json()['id']}")).json())['status'] == 'running':
            await asyncio.sleep(0.1)
    assert evaluation['status'] == 'passed', evaluation
    user = await stage.platform.identity.store.get_user(company.tenant, company.employee_id)
    target = {'user_id': company.employee_id, 'user_epoch': int(user.lifecycle_epoch)}
    assigned = await company.admin.post(f'/v1/capabilities/{cap}/assignments', json={
        'request_id': 'assign', 'allowed_revisions': [1],
        'selector': {'kind': 'user', 'id': target['user_id'], 'user_epoch': target['user_epoch']}})
    assert assigned.status_code == 201, assigned.text
    body = {'installation_id': installation, 'evaluation_id': evaluation['id'], 'targets': [target]}
    preview = await company.admin.post(f'/v1/capabilities/{cap}/release-previews', json=body)
    assert preview.status_code == 200, preview.text
    released = await company.admin.post(f'/v1/capabilities/{cap}/releases', json={
        **body, 'request_id': 'release', 'preview_digest': preview.json()['digest'], 'supersedes': [],
        'confirmed': True})
    assert released.status_code == 201, released.text
    return record


async def _run(stage, company, sample: str, step) -> dict:
    """员工本人上传样例 CSV，经能力入口交办；等后台执行结束后返回本人可见的任务记录。"""
    uploaded = await company.employee.post(
        '/v1/workspace/files', params={'name': sample, 'request_id': 'upload-' + sample},
        content=_sample(sample), headers={'Content-Type': 'text/csv'})
    assert uploaded.status_code == 200, uploaded.text
    stage.models.script(step(calls=[('summarize', 'run_script', {
        'skill_id': 'review', 'script_name': 'summarize', 'args': SCRIPT_ARGS})]),
        step(text='汇总脚本已结束，成果进入校验与保存。'))
    accepted = await company.employee.post('/v1/work-tasks/from-capability', json={
        'request_id': 'run-' + sample, 'text': '按项目汇总金额并列出异常行',
        'capability': {'package': 'office-review', 'entry': 'review'},
        'input_files': [{'item_id': uploaded.json()['id'], 'revision': 1}]})
    assert accepted.status_code == 202, accepted.text
    assert (accepted.json()['execution_kind'], accepted.json()['completion_policy']) == ('office', 'office_files')
    await asyncio.wait_for(stage.platform.task_registry.join(), 180)
    current = await company.employee.get('/v1/work-tasks/' + accepted.json()['task_id'])
    assert current.status_code == 200, current.text
    return current.json()


def _tool_outputs(stage) -> str:
    """模型实际看到的全部工具输出（脚本的退出码与标准错误）。"""
    return '\n'.join(str(message.content) for request in stage.models.requests
                     for message in (request.messages or []) if message.role == 'tool')


async def _saved(company, task: dict) -> dict[str, bytes]:
    """按任务成果回执从本人空间下载；逐份核对回执的大小、摘要与实际字节一致。"""
    assert task['status'] == 'succeeded' and task['outputs_verified'], task
    assert {entry['role'] for entry in task['artifacts']} == ROLES and len(task['artifacts']) == 4
    contents = {}
    for entry in task['artifacts']:
        response = await company.employee.get(f"/v1/workspace/files/{entry['item_id']}/content")
        assert response.status_code == 200, response.text
        assert hashlib.sha256(response.content).hexdigest() == entry['sha256']
        assert len(response.content) == entry['size']
        contents[entry['role']] = response.content
    return contents


async def test_employee_task_saves_four_verified_results(stage, step):
    """员工经能力入口交办 basic.csv：任务成功，四类成果保存到本人空间，数字、来源摘要与回执全部对得上。"""
    await _released(stage, stage.acme, '甲公司月度汇总', step)
    task = await _run(stage, stage.acme, 'basic.csv', step)
    assert task['status'] == 'succeeded', (task, _tool_outputs(stage)[-2000:])
    assert (task['package'], task['package_version'], task['entry'], task['output_kind']) == (
        'office-review', '1.0.0', 'review', 'table_summary')
    saved = await _saved(stage.acme, task)
    evidence = json.loads(saved['evidence'])
    assert _summary(evidence) == _expected()
    assert evidence['inputs'] == [{'name': 'input_1.csv', 'sha256': hashlib.sha256(_sample('basic.csv')).hexdigest()}]
    assert task['inputs'][0]['sha256'] == evidence['inputs'][0]['sha256']
    assert '乙,未注明,80.00,1' in saved['summary_csv'].decode('utf-8-sig')
    assert '币种合计 未注明：250.00（3 行）' in saved['report'].decode()
    rows, formulas = _cells(saved['summary_xlsx'])
    assert formulas == 0 and ['币种合计', '未注明', '250.00'] in rows


async def test_invalid_sample_fails_the_task_without_results(stage, step):
    """invalid.csv：脚本把异常行（文件、行号）交还模型，任务失败，没有成果、没有归零的汇总进入本人空间。"""
    from xuanpu_server.tenancy import Principal

    await _released(stage, stage.acme, '甲公司月度汇总', step)
    task = await _run(stage, stage.acme, 'invalid.csv', step)
    assert task['status'] == 'failed' and task['error_code'] == 'source_or_output_invalid', task
    assert task['artifacts'] == [] and not task['outputs_verified']
    shown = _tool_outputs(stage)
    assert '无效原始行' in shown and 'input_1.csv' in shown, shown[-2000:]
    assert sorted({int(row) for row in re.findall(r'row\W{1,6}(\d+)', shown)}) == [3, 4], shown[-2000:]
    alice = Principal(role='tenant_member', tenant_id='acme', user_id='usr_alice', key_id=None)
    assert len(await stage.platform.workspace.list(alice, kind='file')) == 1, '本人空间只有上传的原件'


async def test_two_companies_each_get_their_own_250(stage, step):
    """两家合成企业各自安装同一归档、各自员工运行：各得 250.00；对方的任务与成果标识一律 404。"""
    records, tasks = {}, {}
    for company, title in ((stage.acme, '甲公司月度汇总'), (stage.beta, '乙公司费用复核')):
        records[company.tenant] = await _released(stage, company, title, step)
        tasks[company.tenant] = await _run(stage, company, 'basic.csv', step)
        saved = await _saved(company, tasks[company.tenant])
        assert json.loads(saved['evidence'])['totals'] == [{'currency': '未注明', 'amount': '250.00', 'records': 3}]
    assert records['acme']['published']['digest'] == records['beta']['published']['digest']
    assert tasks['acme']['package_digest'] == tasks['beta']['package_digest']
    assert tasks['acme']['task_id'] != tasks['beta']['task_id']
    for owner, other in ((stage.acme, stage.beta), (stage.beta, stage.acme)):
        task = tasks[owner.tenant]
        for client in (other.employee, other.admin):
            assert (await client.get('/v1/work-tasks/' + task['task_id'])).status_code == 404
            assert (await client.get(f"/v1/work-tasks/{task['task_id']}/events")).status_code == 404
            for entry in [*task['artifacts'], *task['inputs']]:
                assert (await client.get(f"/v1/workspace/files/{entry['item_id']}/content")).status_code == 404
                assert (await client.get(f"/v1/workspace/items/{entry['item_id']}")).status_code == 404
        listed = (await other.employee.get('/v1/work-tasks')).json()['items']
        assert [item['task_id'] for item in listed] == [tasks[other.tenant]['task_id']]
