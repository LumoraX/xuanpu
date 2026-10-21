"""实际办公镜像执行普通业务脚本，验证金额、来源行和拒绝路径。"""

from __future__ import annotations

import json

import pytest

SCRIPT = 'table-summary/scripts/summarize.py'
ARGS = ['["input_1.csv"]', '部门', '金额', '币种']


async def test_decimal_totals_and_row_evidence(office_worker):
    """三条真实输入得出12.50、3.00和15.50，保留原始行号。"""
    result = await office_worker(SCRIPT, {'input_1.csv': '部门,金额\nA,10.20\nA,2.30\nB,3.00\n'.encode()},
                                [*ARGS[:-1], ''])
    assert result.code == 0, result.stderr
    evidence = json.loads((result.workspace / 'outputs/evidence.json').read_text())
    assert {entry['group']:entry['amount'] for entry in evidence['groups']} == {'A':'12.50','B':'3.00'}
    assert evidence['totals'] == [{'currency':'未注明','amount':'15.50','records':3}]
    assert [row['row'] for row in evidence['rows']] == [2,3,4]
    assert evidence['record_count'] == 3 and evidence['duplicate_rows'] == 0
    for name in ('summary.csv', 'summary.xlsx', 'report.md', 'manifest.json'):
        assert (result.workspace / 'outputs' / name).stat().st_size > 0


@pytest.mark.parametrize('value', ['', 'bad', 'NaN', 'Infinity', '1e10'])
async def test_invalid_amounts_have_row_location(office_worker, value):
    """无效金额不默认为零、不静默丢弃，并列出错误行。"""
    result = await office_worker(SCRIPT, {'input_1.csv': f'部门,金额\nA,{value}\n'.encode()},
                                [*ARGS[:-1], ''])
    assert result.code != 0
    assert 'input_1.csv' in result.stderr and '2' in result.stderr
    assert not (result.workspace / 'outputs/manifest.json').exists()


async def test_duplicate_rows_and_currencies_are_explicit(office_worker):
    """重复行仍计入，各币种分别合计，没有无币种依据的总金额。"""
    result = await office_worker(SCRIPT, {'input_1.csv':
        '部门,金额,币种\nA,1.10,CNY\nA,1.10,CNY\nA,2.00,USD\n'.encode()}, ARGS)
    assert result.code == 0, result.stderr
    evidence = json.loads((result.workspace / 'outputs/evidence.json').read_text())
    assert evidence['duplicate_rows'] == 1
    assert evidence['totals'] == [{'currency':'CNY','amount':'2.20','records':2},
                                  {'currency':'USD','amount':'2.00','records':1}]


async def test_xlsx_formula_is_rejected(office_worker):
    """真实XLSX公式不能借缓存值冒充重算。"""
    import io
    import zipfile

    output = io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        archive.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        archive.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>部门</t></is></c><c r="B1" t="inlineStr"><is><t>金额</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>A</t></is></c><c r="B2"><f>1+2</f><v>3</v></c></row></sheetData></worksheet>')
    result = await office_worker(SCRIPT, {'input_1.xlsx':output.getvalue()},
                                ['["input_1.xlsx"]','部门','金额',''])
    assert result.code != 0 and '公式' in result.stderr


async def test_inspect_real_xlsx_headers_without_guessing(office_worker):
    """XLSX为二进制也能在真实脚本中读取列名，无须用户重复已经明确的映射。"""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[4] / 'scripts/acceptance/r1_office.py'
    spec = importlib.util.spec_from_file_location('r1_accept_fixture',path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = await office_worker('table-summary/scripts/inspect_headers.py',
        {'input_1.xlsx':module.constant_xlsx()}, ['["input_1.xlsx"]'])
    assert result.code == 0, result.stderr
    assert json.loads(result.stdout)['headers'][0]['columns'] == ['部门','金额']
