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
from xuanpu_server.capabilities.delivery import Delivery
from xuanpu_server.packages import archive_digest
from xuanpu_spec import pack_package

SAMPLE = Path(__file__).resolve().parents[4] / 'examples' / 'office-review'
SCRIPTS = SAMPLE / 'package' / 'skills' / 'review' / 'scripts'


def _scripts(monkeypatch: pytest.MonkeyPatch, workspace: Path):
    """加载样板脚本模块（输入目录指向临时工作区）。"""
    monkeypatch.setenv('WORKSPACE', str(workspace))
    monkeypatch.syspath_prepend(str(SCRIPTS))
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
