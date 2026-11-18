"""在办公沙盒内精确汇总原始行，输出文件、证据和声明清单。"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
from collections import defaultdict
from decimal import Decimal, localcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rows import input_path, records  # noqa: E402


def decimal_text(value: Decimal) -> str:
    """保留所有有效小数，至少显示两位，不静默舍入。"""
    text = format(value, 'f')
    if '.' not in text:
        return text + '.00'
    return text + '0' * max(0, 2 - len(text.split('.')[1]))


def safe_csv(value: str) -> str:
    """自由文本加前缀防止电子表格把它作为公式执行。"""
    return "'" + value if value.startswith(('=', '+', '-', '@', '\t', '\r')) else value


def compute(rows: list[dict]) -> dict:
    """每条原始行均贡献一次，重复记录披露但不隐式删除。"""
    grouped = defaultdict(lambda: [Decimal('0'), 0])
    totals = defaultdict(lambda: [Decimal('0'), 0])
    seen = set()
    duplicates = 0
    evidence = []
    with localcontext() as context:
        context.prec = 50
        for row in rows:
            group, currency, amount = row['group'], row['currency'], row['amount']
            signature = group, currency, amount
            duplicates += int(signature in seen)
            seen.add(signature)
            grouped[(group, currency)][0] += amount
            grouped[(group, currency)][1] += 1
            totals[currency][0] += amount
            totals[currency][1] += 1
            evidence.append({**row, 'amount': decimal_text(amount)})
    return {'schema_version':1, 'record_count':len(rows), 'duplicate_rows':duplicates, 'rows':evidence,
        'groups':[{'group':group, 'currency':currency,'amount':decimal_text(amount),'records':count}
                  for (group,currency),(amount,count) in sorted(grouped.items())],
        'totals':[{'currency':currency,'amount':decimal_text(amount),'records':count}
                  for currency,(amount,count) in sorted(totals.items())]}


def save(evidence: dict, output: Path) -> None:
    """金额保存精确文本，自由文本强制字符串，报告与证据同源。"""
    from openpyxl import Workbook

    output.mkdir(parents=True, exist_ok=True)
    headings = ['类型','部门','币种','金额','记录数']
    data = [['分组',entry['group'],entry['currency'],entry['amount'],entry['records']] for entry in evidence['groups']]
    data.extend(['币种合计','',entry['currency'],entry['amount'],entry['records']] for entry in evidence['totals'])
    with (output / 'summary.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(headings)
        for row in data:
            writer.writerow([safe_csv(str(value)) if index in {0,1,2} else value for index,value in enumerate(row)])
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = '汇总'
    for row in [headings, *data]:
        sheet.append(row)
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = 's'
    workbook.save(output / 'summary.xlsx')
    workbook.close()
    lines = ['# 表格汇总', '', f"原始有效记录 {evidence['record_count']} 行；重复汇总字段组合 {evidence['duplicate_rows']} 行，全部保留计入。",
             '空行忽略，空金额或无效金额拒绝；币种未提供则标注未注明，不跨币种求和。',
             '金额使用Decimal精确计算；XLSX金额保存为十进制文本，未进行公式重算。', '', '|部门|币种|金额|记录数|','|---|---|---:|---:|']
    for entry in evidence['groups']:
        cells = [str(entry[key]).replace('|','\\|').replace('\n',' ') for key in ('group','currency','amount','records')]
        lines.append('|' + '|'.join(cells) + '|')
    for entry in evidence['totals']:
        lines.append(f"\n币种合计 {entry['currency']}：{entry['amount']}（{entry['records']} 行）。")
    (output / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    (output / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    artifacts = [('summary.csv','汇总.csv','summary_csv','text/csv'),
        ('summary.xlsx','汇总.xlsx','summary_xlsx','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
        ('report.md','汇总报告.md','report','text/markdown'),('evidence.json','来源行证据.json','evidence','application/json')]
    (output / 'manifest.json').write_text(json.dumps({'kind':'table_summary','artifacts':[
        {'path':'outputs/'+name, 'name':label,'role':role,'mime_type':mime} for name,label,role,mime in artifacts]}, ensure_ascii=False), encoding='utf-8')


def main() -> None:
    """验证显式列映射后生成全部成果，任意错误都不生成成功清单。"""
    names = json.loads(sys.argv[1])
    if not isinstance(names,list) or not 1 <= len(names) <= 8 or len(set(names)) != len(names):
        raise ValueError('输入文件列表不合法')
    output = Path(os.environ['WORKSPACE']) / 'outputs'
    if (output / 'manifest.json').exists():
        (output / 'manifest.json').unlink()
    evidence = compute(list(records(names, *sys.argv[2:5])))
    evidence['columns'] = {'group':sys.argv[2],'amount':sys.argv[3],'currency':sys.argv[4]}
    evidence['inputs'] = [{'name':name,'sha256':hashlib.sha256(input_path(name).read_bytes()).hexdigest()} for name in names]
    save(evidence, output)
    hashes = {'outputs/'+path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in output.iterdir() if path.is_file()}
    print(json.dumps({'record_count':evidence['record_count'], 'totals':evidence['totals'],
                      'outputs_sha256':hashes}, ensure_ascii=False))


if __name__ == '__main__':
    main()
