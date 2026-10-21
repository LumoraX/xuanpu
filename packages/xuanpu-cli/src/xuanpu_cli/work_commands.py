"""本人工作命令使用私有凭据档案，输出安全摘要而非内部执行记录。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse
    from collections.abc import Awaitable, Callable

from xuanpu_sdk import WorkTask, XuanpuClient, XuanpuError, XuanpuTransportError

SUMMARY_FIELDS={'task_id','definition_id','title','enabled','version','state_version','status',
    'execution_stage','attempt_no','next_run_at','block_reason','event_id','event_kind','reason_code',
    'occurrence_id','state','action_id','available_actions'}


def add_work_commands(commands: Any) -> None:
    """所有身份参数只取档案，不提供tenant/user/key命令行覆盖。"""
    for group,operations in {
        'work-tasks':('list','get','events','create','capability','cancel','decide','reconcile'),
        'task-definitions':('list','get','create','update','pause','resume','cancel','occurrences'),
    }.items():
        parent=commands.add_parser(group,help='本人交办工作' if group=='work-tasks' else '本人执行安排')
        parent.add_argument('--credentials',required=True,help='私有JSON凭据档案')
        subs=parent.add_subparsers(dest='operation',required=True)
        for operation in operations:
            command=subs.add_parser(operation)
            if operation not in {'list','create','capability'}:command.add_argument('identifier')
            if operation in {'create','capability','decide','reconcile','update','pause','resume'} or (group=='task-definitions' and operation=='cancel'):
                command.add_argument('--body',required=True,help='明确确认且含固定request_id的JSON文件')
            if operation=='reconcile':command.add_argument('--action',required=True)
            if operation in {'list','events','occurrences'}:
                command.add_argument('--limit',type=int,default=100)
                command.add_argument('--after-id',type=int if operation=='events' else str)
            command.set_defaults(handler=cmd_work)


def read_object(path: str) -> dict[str,Any]:
    """私有文件只读，解析错误不回显内容或凭据。"""
    value=json.loads(Path(path).read_text())
    if not isinstance(value,dict):raise ValueError('需要JSON对象')
    return value


def summary(value: Any) -> Any:
    """不打印密钥、内部路径、原始参数及目标正文。"""
    from xuanpu_sdk.models import TaskDefinition

    if isinstance(value,(WorkTask,TaskDefinition)):value=value.record
    if isinstance(value,list):return [summary(item) for item in value]
    if isinstance(value,dict):return {key:item for key,item in value.items() if key in SUMMARY_FIELDS}
    raise ValueError('返回记录无效')


async def run_work(args: argparse.Namespace) -> Any:
    """所有操作调用同一公开SDK，网络重试复用文件中的原请求标识。"""
    credentials=await asyncio.to_thread(read_object,args.credentials)
    if set(credentials)-{'base_url','api_key','act_as_user'}:raise ValueError('凭据档案字段无效')
    request=await asyncio.to_thread(read_object,args.body) if getattr(args,'body',None) else {}
    if {'tenant_id','user_id','host_path','api_key','act_as_user'} & request.keys():raise ValueError('禁止覆盖身份')
    async with XuanpuClient(**credentials) as client:
        identifier=getattr(args,'identifier','')
        limit=getattr(args,'limit',100)
        cursor=getattr(args,'after_id',None)
        calls: dict[str,dict[str,Callable[[],Awaitable[Any]]]]={
            'work-tasks':{
                'list':lambda:client.list_work_tasks(limit=limit,after_id=cursor),
                'get':lambda:client.get_work_task(identifier),
                'events':lambda:client.work_task_events(identifier,after_id=cursor or 0,limit=limit),
                'create':lambda:client.create_work_task(**request),
                'capability':lambda:client.create_capability_task(request),
                'cancel':lambda:client.cancel_work_task(identifier),
                'decide':lambda:client.decide_work_task(identifier,request),
                'reconcile':lambda:client.reconcile_task_effect(identifier,args.action,**request),
            },
            'task-definitions':{
                'list':lambda:client.list_task_definitions(limit=limit,after_id=cursor),
                'get':lambda:client.get_task_definition(identifier),
                'create':lambda:client.create_task_definition(request),
                'update':lambda:client.update_task_definition(identifier,request),
                'pause':lambda:client.pause_task_definition(identifier,**request),
                'resume':lambda:client.resume_task_definition(identifier,**request),
                'cancel':lambda:client.cancel_task_definition(identifier,**request),
                'occurrences':lambda:client.list_occurrences(identifier,after_id=cursor,limit=limit),
            },
        }
        return summary(await calls[args.command][args.operation]())


def cmd_work(args: argparse.Namespace) -> int:
    """错误仅显示类别；服务端和供应商正文不能泄露档案密钥。"""
    try:
        result=asyncio.run(run_work(args))
    except (OSError,ValueError,TypeError,KeyError,XuanpuError,XuanpuTransportError):
        print('操作未完成，请检查私有凭据、请求文件和服务端状态；重试使用原request_id。',file=sys.stderr)
        return 1
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0
