"""通过独立验收实例的真实HTTP接口验证办公成果；凭据只从私有文件加载。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import time
import zipfile
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx

TERMINAL = {'succeeded','failed','cancelled','interrupted','outcome_unknown'}
XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def fixture_entry(archive: zipfile.ZipFile, name: str, content: str) -> None:
    """fixture归档时间固定，重复运行不会改变同内容输入的摘要。"""
    archive.writestr(zipfile.ZipInfo(name, date_time=(2020,1,1,0,0,0)), content)


def constant_xlsx() -> bytes:
    """用标准库生成独立的三行常量fixture，不导入业务包或平台办公解析器。"""
    output = io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        fixture_entry(archive,'[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        fixture_entry(archive,'_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        fixture_entry(archive,'xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="原始记录" sheetId="1" r:id="rId1"/></sheets></workbook>')
        fixture_entry(archive,'xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        rows = ['<row r="1"><c r="A1" t="inlineStr"><is><t>部门</t></is></c><c r="B1" t="inlineStr"><is><t>金额</t></is></c></row>']
        for row,(group,amount) in enumerate([('A','10.20'),('A','2.30'),('B','3.00')],start=2):
            rows.append(f'<row r="{row}"><c r="A{row}" t="inlineStr"><is><t>{group}</t></is></c><c r="B{row}" t="n"><v>{amount}</v></c></row>')
        fixture_entry(archive,'xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+''.join(rows)+'</sheetData></worksheet>')
    return output.getvalue()


class Acceptance:
    """受理和补读都通过HTTP，下载后独立解析与核对摘要。"""

    def __init__(self, config: dict, output: Path) -> None:
        """认证配置不会写入证据，只保留合成材料、回执与评估。"""
        self.http = httpx.AsyncClient(base_url=config['url'],headers={
            'Authorization':'Bearer '+config['api_key'],'X-Xuanpu-Act-As':config['act_as_user']},timeout=120)
        self.output = output
        self.evidence = {'model':config['model'],'database':'isolated PostgreSQL schema',
                         'backend':'Linux Docker office image','external_notifications':0,'cases':[],
                         'source_sha256':config.get('source_sha256',{})}

    async def json(self, method: str, path: str, **kwargs) -> dict:
        """保留服务端错误，不把非成功HTTP响应解释为业务通过。"""
        response = await self.http.request(method,path,**kwargs)
        response.raise_for_status()
        return response.json()

    async def accept(self, label: str, data: bytes, mime: str, goal: str) -> dict:
        """上传真实正本，固定服务端版本与请求标识并受理。"""
        item = await self.json('POST','/v1/workspace/files',params={'name':label,'request_id':'r1-file-'+label},
                               content=data,headers={'Content-Type':mime})
        request = {'request_id':'r1-task-'+label,'text':goal,'timezone':'Asia/Shanghai',
                   'input_files':[{'item_id':item['id'],'revision':item['data']['current_revision']}]}
        task = await self.json('POST','/v1/work-tasks',json=request)
        repeated = await self.json('POST','/v1/work-tasks',json=request)
        assert repeated['task_id'] == task['task_id']
        self.evidence['cases'].append({'label':label,'request':request,'input_sha256':hashlib.sha256(data).hexdigest(),
                                       'task_id':task['task_id'],'accepted_status':task['status']})
        return task

    async def wait(self, task: dict) -> dict:
        """网页连接不持有执行；每次都从持久接口补读状态。"""
        deadline = time.monotonic()+300
        while time.monotonic()<deadline:
            task = await self.json('GET','/v1/work-tasks/'+task['task_id'])
            if task['status'] in TERMINAL:
                return task
            if task['status']=='waiting_user':
                if task['action_candidates']:
                    return task
                # 仅测试包本地沙盒处理可以自动验收；从不批准外部业务服务。
                decisions = {}
                for pending in task['pending']:
                    scope = pending.get('detail',{}).get('scope')
                    assert scope in {'shell_exec','script_exec','file_read','file_write'}, pending
                    decisions[pending['request_id']] = {'granted':True}
                assert decisions, task
                await self.json('POST','/v1/work-tasks/'+task['task_id']+'/decisions',json={
                    'kind':'tool_approval','request_id':'r1-approval-'+str(task['state_version']),
                    'state_version':task['state_version'],'decisions':decisions})
            await asyncio.sleep(0.5)
        raise TimeoutError('真实模型任务未在验收期限内结束')

    async def download(self, task: dict) -> dict[str, bytes]:
        """每个回执下载实际文件并重新计算摘要，版本保持固定。"""
        files = {}
        folder = self.output / task['task_id'];folder.mkdir(parents=True,exist_ok=True)
        for receipt in task['artifacts']:
            response = await self.http.get('/v1/workspace/files/'+receipt['item_id']+'/content',params={'revision':receipt['revision']})
            response.raise_for_status();content=response.content
            assert len(content)==receipt['size'] and hashlib.sha256(content).hexdigest()==receipt['sha256']
            files[receipt['role']]=content
            (folder/receipt['name']).write_bytes(content)
        self.evidence['cases'][-1]['final_record']=task
        return files

    async def table(self, label: str, data: bytes, mime: str) -> None:
        """CSV与XLSX分别走真实模型和实际脚本，独立校验15.50及来源行。"""
        task = await self.wait(await self.accept(label,data,mime,
            '请按部门列对金额列精确汇总。没有币种列，标注未注明；不要去重。必须调用table-summary的summarize脚本，输出CSV、XLSX、报告和行证据。'))
        assert task['status']=='succeeded',task
        files=await self.download(task)
        evidence=json.loads(files['evidence'])
        assert {entry['group']:Decimal(entry['amount']) for entry in evidence['groups']}=={'A':Decimal('12.50'),'B':Decimal('3.00')}
        assert Decimal(evidence['totals'][0]['amount'])==Decimal('15.50')
        assert [row['row'] for row in evidence['rows']]==[2,3,4]
        with zipfile.ZipFile(io.BytesIO(files['summary_xlsx'])) as archive:
            sheet=ElementTree.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            text=' '.join(sheet.itertext())
            assert '12.50' in text and '3.00' in text and '15.50' in text
        assert b'15.50' in files['summary_csv'] and b'15.50' in files['report']
        self.evidence['cases'][-1]['assessment']={'totals':True,'rows':True,'download_sha256':True,'xlsx_independent_xml':True}
        print(json.dumps({'case':label,'status':task['status'],'total':'15.50'}),flush=True)

    async def meeting(self) -> dict:
        """真实模型生成本人候选，留下等待卡供浏览器编辑和部分确认。"""
        today=datetime.now(ZoneInfo('Asia/Shanghai')).date()
        due=today+timedelta(days=2);event=today+timedelta(days=3);discussed=today+timedelta(days=1)
        material=(f'会议日期：{today}。时区：Asia/Shanghai。\n'
            f'已经决定：我（当前账号本人）负责整理需求方案，截止时间为{due} 17:00。\n'
            f'已经决定：我在{event} 10:00到11:00安排个人方案准备会议，并在当天09:50用站内信提醒我。\n'
            '我还负责归档会议资料，但截止时间没有确定，需要核实，不要猜测。\n'
            f'讨论时有人提及{discussed}，但没有决定它是交付截止日期。\n'
            '其他同事李四负责复核，这不是我负责的工作，不要替他创建事项。\n')
        task=await self.wait(await self.accept('meeting-v101.txt',material.encode(),'text/plain',
            '请整理真实纪要及带准确原文字符区间的行动项JSON。整理方案用todo，明确的个人准备会议用event并提出站内提醒；不明确日期保持未知，其他同事任务不分派。只提出候选，稍后由我编辑并部分确认。'))
        assert task['status']=='waiting_user' and task['outputs_verified'],task
        await self.download(task)
        kinds={entry['candidate']['kind'] for entry in task['action_candidates'] if entry['candidate']['owner']=='self'}
        assert {'todo','event'}<=kinds,task['action_candidates']
        self.evidence['cases'][-1]['assessment']={'files_saved':True,'awaiting_user':True,'native_records_created':False,
            'discussion_date':str(discussed),'explicit_deadline':str(due),'requires_browser_confirmation':True}
        print(json.dumps({'case':'meeting','task_id':task['task_id'],'status':task['status'],'candidates':len(task['action_candidates'])}),flush=True)
        return task

    def save(self) -> None:
        """原始模型产物与独立评估分开保留，不含凭据。"""
        self.output.mkdir(parents=True,exist_ok=True)
        (self.output/'real-office.json').write_text(json.dumps(self.evidence,ensure_ascii=False,indent=2)+'\n')


async def main() -> None:
    """参数必须指向已隔离的验收实例；脚本不创建生产部署。"""
    parser=argparse.ArgumentParser();parser.add_argument('--credentials',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True);args=parser.parse_args()
    acceptance=Acceptance(json.loads(args.credentials.read_text()),args.evidence)
    try:
        await acceptance.table('table-v101.csv','部门,金额\nA,10.20\nA,2.30\nB,3.00\n'.encode(),'text/csv')
        await acceptance.table('table-v101.xlsx',constant_xlsx(),XLSX)
        task=await acceptance.meeting()
        (args.evidence/'meeting-waiting.json').write_text(json.dumps(task,ensure_ascii=False,indent=2)+'\n')
    finally:
        acceptance.save();await acceptance.http.aclose()


if __name__=='__main__':
    asyncio.run(main())
