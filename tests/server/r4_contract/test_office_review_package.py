"""可复制办公样板（R4-E0）：确定性构建、外部交付描述、经真实接口登记、样板脚本的精确汇总。

样板脚本在沙盒里运行、不被平台进程导入；这里只为核对数字，按路径加载它的纯计算部分
（CSV 读取与 Decimal 汇总；XLSX 输出需办公容器，不在本测试内）。
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from server.domains.capabilities.delivery import Delivery
from server.engine.packages import archive_digest
from spec import pack_package

SAMPLE = Path(__file__).resolve().parents[3] / 'examples' / 'office-review'
SCRIPTS = SAMPLE / 'package' / 'skills' / 'review' / 'scripts'
LIB = SCRIPTS.parent / 'lib'


def _scripts(monkeypatch: pytest.MonkeyPatch, workspace: Path):
    """加载样板脚本模块（输入目录指向临时工作区）。"""
    monkeypatch.setenv('WORKSPACE', str(workspace))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.syspath_prepend(str(LIB))
    for name in ('documents', 'rows', 'summarize'):
        sys.modules.pop(name, None)
    return importlib.import_module('rows'), importlib.import_module('summarize')


def _input(workspace: Path, sample: str) -> None:
    """把样例 CSV 放进工作区的固定输入名。"""
    (workspace / 'inputs').mkdir(parents=True, exist_ok=True)
    (workspace / 'inputs' / 'input_1.csv').write_bytes((SAMPLE / 'evals' / 'inputs' / sample).read_bytes())


def test_build_is_deterministic_and_delivery_is_external() -> None:
    """重复打包字节相同；外部交付描述记的是这个归档的摘要；描述本身不在归档里。"""
    first = pack_package(SAMPLE / 'package')
    assert pack_package(SAMPLE / 'package') == first
    delivery = Delivery.model_validate(json.loads((SAMPLE / 'delivery.json').read_text()))
    assert delivery.package.digest == archive_digest(first)
    assert b'delivery.json' not in first and b'suite.json' not in first
    assert delivery.outputs.contract == 'office_files' and delivery.outputs.output_kind == 'table_summary'
    assert delivery.inputs.mime_types == ('text/csv',)
    assert delivery.permissions.network is False and delivery.permissions.tools == ()


async def test_sample_registers_through_the_product_api(r4_http, r4_world) -> None:
    """发布样板归档后，用外部交付描述经真实接口登记成功（包与描述的权限、摘要、入口一一对应）。"""
    await r4_world.platform.packages.publish(r4_world.admin, pack_package(SAMPLE / 'package'))
    delivery = json.loads((SAMPLE / 'delivery.json').read_text())
    response = await r4_http['admin'].post('/v1/capabilities', json={'request_id': 'office-review',
                                                                     'delivery': delivery})
    assert response.status_code == 201, response.text
    assert response.json()['revision'] == 1


def test_basic_sample_totals_are_exact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """甲 170.00、乙 80.00、总计 250.00、有效行 3：与样板预期完全一致。"""
    rows, summarize = _scripts(monkeypatch, tmp_path)
    _input(tmp_path, 'basic.csv')
    evidence = summarize.compute(list(rows.records(['input_1.csv'], '项目', '金额', '')))
    expected = json.loads((SAMPLE / 'evals' / 'expected' / 'basic.json').read_text())
    assert {k: evidence[k] for k in expected} == expected


def test_invalid_rows_are_listed_not_zeroed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """缺金额与非法数字列出文件与行号，整体拒绝，不静默归零。"""
    rows, _ = _scripts(monkeypatch, tmp_path)
    _input(tmp_path, 'invalid.csv')
    with pytest.raises(ValueError, match='无效原始行') as caught:
        list(rows.records(['input_1.csv'], '项目', '金额', ''))
    listed = json.loads(str(caught.value).removeprefix('无效原始行：'))
    assert sorted(entry['row'] for entry in listed) == [3, 4]


def test_formula_prefixes_are_neutralised_in_exports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """自由文本以 = + - @ 开头时导出前加单引号，不会被表格软件当公式执行。"""
    _, summarize = _scripts(monkeypatch, tmp_path)
    assert summarize.safe_csv('=1+1') == "'=1+1" and summarize.safe_csv('甲') == '甲'


def _documents(monkeypatch: pytest.MonkeyPatch, workspace: Path):
    """加载样板的输入路径规则模块（输入目录指向临时工作区）。"""
    monkeypatch.setenv('WORKSPACE', str(workspace))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.syspath_prepend(str(LIB))
    sys.modules.pop('documents', None)
    return importlib.import_module('documents')


def _handoff_paths() -> list[str]:
    """平台交办消息里「固定输入」给模型的路径（与 runner 同一函数生成，不手抄格式）。"""
    from types import SimpleNamespace

    from server.domains.work_tasks.runner import TaskRunner

    entry = SimpleNamespace(relative_name='input_1.csv', sha256='0' * 64, mime_type='text/csv')
    task = SimpleNamespace(execution_kind='office', inputs=(entry,), timezone='Asia/Shanghai',
                           accepted_at=0.0, text='汇总')
    message = TaskRunner._message(task)  # type: ignore[arg-type]
    line = next(item for item in message.splitlines() if item.startswith('固定输入：'))
    return [ref['path'] for ref in json.loads(line.removeprefix('固定输入：'))]


def test_handoff_path_and_bare_name_resolve_to_same_input(monkeypatch: pytest.MonkeyPatch,
                                                          tmp_path: Path) -> None:
    """缺陷 1：交办消息给的是 inputs/input_1.csv，脚本须与 input_1.csv 按同一个文件处理。"""
    documents = _documents(monkeypatch, tmp_path)
    _input(tmp_path, 'basic.csv')
    expected = (tmp_path / 'inputs' / 'input_1.csv').resolve()
    assert _handoff_paths() == ['inputs/input_1.csv']
    for name in ['input_1.csv', *_handoff_paths(), './inputs/input_1.csv']:
        assert documents.input_path(name) == expected, name


@pytest.mark.parametrize('name', ['../inputs/input_1.csv', 'inputs/../input_1.csv', '/inputs/input_1.csv',
                                  'outputs/input_1.csv', 'inputs/inputs/input_1.csv', 'inputs\\input_1.csv',
                                  'input_9.csv', 'inputs/notes.csv', ''])
def test_input_path_still_rejects_escapes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str) -> None:
    """规范化只认工作区 inputs/ 下的固定输入名；跳出 inputs、其他目录或任意文件名一律拒绝。"""
    documents = _documents(monkeypatch, tmp_path)
    _input(tmp_path, 'basic.csv')
    (tmp_path / 'outputs').mkdir()
    (tmp_path / 'outputs' / 'input_1.csv').write_text('项目,金额\n')
    with pytest.raises(ValueError, match='输入名称不合法'):
        documents.input_path(name)


@pytest.mark.parametrize('raw', ['["inputs/input_1.csv"]', '["input_1.csv"]', "['inputs/input_1.csv']",
                                 'inputs/input_1.csv', 'input_1.csv'])
def test_inputs_argument_forms_are_normalised(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str) -> None:
    """run_script 把参数逐个 str() 成 argv：JSON 数组、模型直接传数组（Python 表示）或单个名称都按同一列表处理。"""
    documents = _documents(monkeypatch, tmp_path)
    _input(tmp_path, 'basic.csv')
    assert documents.input_names(raw) == ['input_1.csv']


def test_inputs_argument_rejects_duplicates_and_garbage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """同一文件两种写法算重复；非字符串元素、空列表、超过 8 个一律拒绝。"""
    documents = _documents(monkeypatch, tmp_path)
    for raw in ['["input_1.csv", "inputs/input_1.csv"]', '[]', '[1]', '{"a": 1}',
                json.dumps([f'input_{i}.csv' for i in range(1, 10)])]:
        with pytest.raises(ValueError, match='输入'):
            documents.input_names(raw)


def _review_skill():
    """按内核实际加载方式读样板入口 skill。"""
    import asyncio

    import taifeng

    registry = asyncio.run(taifeng.FilesystemSkillRegistry.load(SAMPLE / 'package' / 'skills'))
    skill = registry.snapshot().get('review')
    assert skill is not None
    return skill


def test_entry_skill_cannot_call_itself() -> None:
    """缺陷 2：入口 review 对模型只暴露 run_script，不暴露 call_skill（否则模型会自调用被 cycle_detected 拒绝）。"""
    skill = _review_skill()
    assert 'call_skill' not in skill.visible_tool_names()
    assert 'run_script' in skill.visible_tool_names()
    assert not skill.child_skills
    assert {script.name for script in skill.scripts} == {'inspect_headers', 'summarize'}


def test_skill_instructions_match_handoff_paths() -> None:
    """SKILL.md 说明与交办消息的路径写法一致，并写明直接 run_script、不调用 skill。"""
    text = (SAMPLE / 'package' / 'skills' / 'review' / 'SKILL.md').read_text()
    assert _handoff_paths()[0] in text
    assert 'skill_id' in text and 'run_script' in text and 'call_skill' in text
    for script in _review_skill().scripts:
        assert 'inputs/input_1.csv' in script.args_schema['properties']['inputs']['description']
