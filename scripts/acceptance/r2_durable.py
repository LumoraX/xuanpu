"""R2真实HTTP验收；只操作明确隔离的合成租户，原始结果与评估分开保存。"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from datetime import date, datetime, time as wall_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from r1_office import XLSX, Acceptance, constant_xlsx


class DurableAcceptance(Acceptance):
    """复用独立下载核验；不从请求参数切换身份或修改全局配置。"""

    def __init__(self,config: dict,output: Path,mode: str) -> None:
        """每次运行使用新目录和新稳定前缀，失败证据不会覆盖。"""
        super().__init__(config,output)
        self.config=config
        self.prefix='r2-'+uuid.uuid4().hex[:16]
        self.evidence.update(database='not_checked',backend='not_checked',external_notifications='not_checked',
            environment_claims='operator deployment evidence required',mode=mode,run=self.prefix,started_at=time.time(),deployment='not_checked',
            host_reboot='not_checked',enterprise_login='not_checked',fault_matrix='separate_pytest_evidence',
            schedule_cycles=[],cleanup=[])
        self.definitions=[]

    async def verify_identity(self) -> None:
        """生产也先验证实际服务端身份，配置标签本身不算隔离证明。"""
        if self.config.get('isolated_acceptance_tenant') is not True:
            raise ValueError('验收只允许显式合成租户')
        me=await self.json('GET','/v1/me')
        if me['tenant_id']!=self.config['tenant_id'] or me['user_id']!=self.config['act_as_user']:
            raise ValueError('实际验收身份与私有配置不符')
        if not me['tenant_id'].startswith('r2-acceptance-'):
            raise ValueError('租户必须使用r2-acceptance-独立前缀')
        self.evidence['identity']={k:me[k] for k in ('tenant_id','user_id','role')}
        self.evidence['options']=await self.json('GET','/v1/task-definitions/options')
        if not self.evidence['options']['enabled']:raise ValueError('验收租户尚未开启R2')

    async def accept(self,label: str,data: bytes,mime: str,goal: str) -> dict:
        """同一验收的网络重放保持原请求，不与其他轮次混淆。"""
        return await super().accept(self.prefix+'-'+label,data,mime,goal)

    async def schedule_cycles(self) -> None:
        """两次未来时刻由真实调度扫描受理，不直接调用fire_time或调整时钟。"""
        source=await self.json('POST','/v1/workspace/files',params={'name':self.prefix+'-合成调度材料.csv',
            'request_id':self.prefix+'-schedule-source'},content='部门,金额\nA,12.50\nB,3.00\n'.encode(),headers={'Content-Type':'text/csv'})
        zone=ZoneInfo('Asia/Shanghai')
        for cycle in (1,2):
            now=datetime.now(zone)
            body={'request_id':self.prefix+'-schedule-'+str(cycle),'confirmed':False,'title':'合成调度验收'+str(cycle),
                'template':{'kind':'office','text':'按部门汇总金额，执行表格汇总脚本，交付CSV、XLSX、报告和行证据。',
                    'input_files':[{'item_id':source['id'],'revision':1}]},'trigger':{'kind':'time','repeat':'once',
                    'at':(now+timedelta(seconds=20)).replace(microsecond=0).isoformat()},'timezone':'Asia/Shanghai',
                'valid_until':(now+timedelta(minutes=15)).replace(microsecond=0).isoformat(),
                'max_executions':1,'max_attempts':3,'max_tokens':300000,'input_policy':'fixed_revision',
                'misfire_policy':'coalesce_latest','overlap_policy':'skip'}
            draft=await self.json('POST','/v1/task-definitions/preview',json=body)
            assert draft['request']['confirmed'] is False
            body['confirmed']=True
            record=await self.json('POST','/v1/task-definitions',json=body);self.definitions.append(record['definition_id'])
            replay=await self.json('POST','/v1/task-definitions',json=body)
            assert replay['definition_id']==record['definition_id']
            async with asyncio.timeout(300):
                while True:
                    occurrences=(await self.json('GET','/v1/task-definitions/'+record['definition_id']+'/occurrences'))['items']
                    if occurrences and occurrences[0]['task_id']:break
                    await asyncio.sleep(1)
            assert len(occurrences)==1 and occurrences[0]['status']=='accepted'
            task=await self.wait({'task_id':occurrences[0]['task_id']})
            assert task['status']=='succeeded' and len(task['artifacts'])==4
            self.evidence['cases'].append({'label':'actual-schedule-cycle-'+str(cycle),'task_id':task['task_id']})
            files=await self.download(task)
            assert json.loads(files['evidence'])['totals'][0]['amount']=='15.50'
            self.evidence['schedule_cycles'].append({'cycle':cycle,'definition_id':record['definition_id'],
                'occurrence':occurrences[0],'task_id':task['task_id'],'verified_total':'15.50','observed_at':time.time()})

    async def meeting(self) -> dict:
        """生命周期和原文引用之外，逐项独立验证全部明确时间及未知时间。"""
        task=await super().meeting()
        assessment=self.evidence['cases'][-1]['assessment']
        assessment['date_semantics']='failed'
        due=date.fromisoformat(assessment['explicit_deadline']);event_day=due+timedelta(days=1)
        zone=ZoneInfo('Asia/Shanghai')
        own=[entry['candidate'] for entry in task['action_candidates'] if entry['candidate']['owner']=='self']
        todo=next(c for c in own if c['kind']=='todo' and '方案' in c['title'])
        event=next(c for c in own if c['kind']=='event')
        assert todo['due_at']==datetime.combine(due,wall_time(17),zone).timestamp()
        assert event['start_at']==datetime.combine(event_day,wall_time(10),zone).timestamp()
        assert event['end_at']==datetime.combine(event_day,wall_time(11),zone).timestamp()
        assert event['remind_at']==datetime.combine(event_day,wall_time(9,50),zone).timestamp()
        assert any(c['kind']=='todo' and c.get('due_at') is None and '归档' in c['title'] for c in own)
        assessment['date_semantics']='passed'
        return task

    async def capability(self) -> None:
        """无附件任务只允许私有配置明确给定的合成只读能力。"""
        reference=self.config['capability']
        body={'request_id':self.prefix+'-capability','text':self.config['capability_goal'],
              'capability':reference,'input_files':[],'timezone':'Asia/Shanghai'}
        task=await self.json('POST','/v1/work-tasks/from-capability',json=body)
        self.evidence['cases'].append({'label':'no-attachment-read-only','task_id':task['task_id']})
        task=await self.wait(task)
        self.evidence['cases'][-1]['final_record']=task
        assert task['status']=='succeeded' and task['execution_kind']=='capability'
        for expected in self.config['expected_summary_contains']:
            assert expected in task['result_summary']
        self.evidence['cases'][-1]['assessment']={'lifecycle':True,'configured_summary_facts':True,'external_write_expected':0}

    async def cleanup(self) -> None:
        """补读本轮所有稳定请求，即使受理响应丢失也逐项尝试收尾。"""
        self.evidence['cleanup_errors']=[]
        definitions=await self.discover('/v1/task-definitions')
        tasks=await self.discover('/v1/work-tasks')
        for record in definitions:
            if not record['confirmed']['request_id'].startswith(self.prefix) or record['cancelled']:continue
            await self.cleanup_one('definition',record['definition_id'],record['state_version'])
        for record in tasks:
            if not record['request_id'].startswith((self.prefix,'r1-task-'+self.prefix)):continue
            if record['status'] not in {'succeeded','failed','cancelled','interrupted','outcome_unknown'}:
                await self.cleanup_one('task',record['task_id'],record['state_version'])
        self.evidence['cleanup_status']='partial' if self.evidence['cleanup_errors'] else 'complete'
        if self.evidence['cleanup_errors']:self.evidence['result']='failed_cleanup'

    async def discover(self,path: str) -> list[dict]:
        """本人稳定分页，不因一个列表故障而放弃另一类资源的清理。"""
        records=[];cursor=None
        try:
            for _ in range(100):
                body=await self.json('GET',path,params={'limit':100,**({'after_id':cursor} if cursor else {})})
                batch=body['items'];records.extend(batch)
                if len(batch)<100:return records
                cursor=batch[-1]['definition_id' if path.endswith('task-definitions') else 'task_id']
            raise ValueError('cleanup_pagination_limit')
        except Exception as error:
            self.evidence['cleanup_errors'].append({'scope':path,'error_type':type(error).__name__})
        return records

    async def cleanup_one(self,kind: str,identifier: str,version: int) -> None:
        """单项错误留证并继续，不能把未核实收尾报告为全部成功。"""
        try:
            path=('/v1/task-definitions/' if kind=='definition' else '/v1/work-tasks/')+identifier+'/cancel'
            options={'json':{'request_id':self.prefix+'-cleanup-'+identifier,
                'expected_version':version,'stop_running':True}} if kind=='definition' else {}
            await self.json('POST',path,**options)
            self.evidence['cleanup'].append({'kind':kind,'id':identifier,'stop_requested':True})
        except Exception as error:
            self.evidence['cleanup_errors'].append({'kind':kind,'id':identifier,'error_type':type(error).__name__})

    def save(self) -> None:
        """失败原因只保存异常类型；任何凭据都不进入证据。"""
        (self.output/'r2-http.json').write_text(json.dumps(self.evidence,ensure_ascii=False,indent=2)+'\n')


async def main() -> None:
    """运行前须已有隔离租户；不创建账号，不修改正式租户或全局模型。"""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials',type=Path,required=True);parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--mode',choices=['staging','production'],required=True)
    parser.add_argument('--case',choices=['all','office','schedule','capability'],default='all');args=parser.parse_args()
    args.evidence.mkdir(parents=True,exist_ok=False)
    config=await asyncio.to_thread(lambda:json.loads(args.credentials.read_text()))
    acceptance=DurableAcceptance(config,args.evidence,args.mode)
    failure=False
    try:
        await acceptance.verify_identity()
        if args.case in {'all','office'}:
            await acceptance.table('table.csv','部门,金额\nA,10.20\nA,2.30\nB,3.00\n'.encode(),'text/csv')
            await acceptance.table('table.xlsx',constant_xlsx(),XLSX)
            await acceptance.meeting()
        if args.case in {'all','schedule'}:await acceptance.schedule_cycles()
        if args.case in {'all','capability'}:await acceptance.capability()
        acceptance.evidence['result']='passed'
    except Exception as error:
        failure=True
        acceptance.evidence.update(result='failed',error_type=type(error).__name__)
        raise
    finally:
        acceptance.evidence['business_result']=acceptance.evidence.get('result','not_checked')
        try:await acceptance.cleanup()
        finally:acceptance.save();await acceptance.http.aclose()
        if not failure and acceptance.evidence.get('cleanup_status')!='complete':
            raise RuntimeError('验收收尾不完整，证据已保存')


if __name__=='__main__':asyncio.run(main())
