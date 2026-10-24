"""原生协作命令固定本人档案和原请求文件，不从命令行覆盖员工身份。"""
from __future__ import annotations

import json

import pytest
from xuanpu_cli.main import build_parser


@pytest.mark.parametrize('arguments',[
    ['list','--view','received'],['accept','handoff','--body','accept.json'],
    ['sign-off','handoff','--body','sign.json'],['approve','approval','--body','approve.json'],
    ['result-upload','handoff','--body','upload.json','--file','result.txt'],
    ['policies','--admin'],['approval-list'],['read-notification','note','--body','read.json']])
def test_collaboration_parser_has_no_employee_override(arguments):
    """到人目标在服务端确认文件中，调用身份固定在私有凭据档案。"""
    args=build_parser().parse_args(['collaboration','--credentials','employee.json',*arguments])
    assert args.credentials=='employee.json' and args.operation==arguments[0]
    with pytest.raises(SystemExit):
        build_parser().parse_args(['collaboration','--credentials','employee.json','--user','victim','list'])


async def test_collaboration_cli_preserves_body_and_requires_private_files(tmp_path,monkeypatch):
    """同一文件重试逐字保持原确认；公开可读的业务正文也拒绝发送。"""
    from xuanpu_cli import collaboration_commands
    sent=[]
    class Client:
        """只记录契约，实际权限由服务双后端测试覆盖。"""
        def __init__(self,**credentials):
            """接收固定凭据。"""
        async def __aenter__(self):
            """打开当前客户端。"""
            return self
        async def __aexit__(self,*args):
            """关闭当前客户端。"""
        async def handoff_action(self,identifier,action,body):
            """保留原确认及签收版本。"""
            sent.append(body);return {'id':identifier}
    monkeypatch.setattr(collaboration_commands,'XuanpuClient',Client)
    credentials=tmp_path/'employee.json';credentials.write_text(json.dumps({'base_url':'https://example.test','api_key':'synthetic'}));credentials.chmod(0o600)
    body=tmp_path/'body.json';body.write_text(json.dumps({'request_id':'same','confirmed':False,'expected_version':2,'delivery_version':1}));body.chmod(0o600)
    args=build_parser().parse_args(['collaboration','--credentials',str(credentials),'sign-off','handoff','--body',str(body)])
    await collaboration_commands.run_collaboration(args);await collaboration_commands.run_collaboration(args)
    assert sent[0]==sent[1] and sent[0]['confirmed'] is False
    body.chmod(0o644)
    with pytest.raises(ValueError,match='私有'):
        await collaboration_commands.run_collaboration(args)
