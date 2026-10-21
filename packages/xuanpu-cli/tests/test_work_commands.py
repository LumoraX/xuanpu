"""本人任务命令只从私有档案取身份，不开放命令行越权字段。"""
from __future__ import annotations

import pytest
from xuanpu_cli.main import build_parser


@pytest.mark.parametrize('arguments',[
    ['work-tasks','list'],['work-tasks','get','task'],['work-tasks','events','task'],
    ['work-tasks','create','--body','input.json'],['work-tasks','capability','--body','input.json'],
    ['work-tasks','cancel','task'],['work-tasks','decide','task','--body','input.json'],
    ['work-tasks','reconcile','task','--action','action','--body','input.json'],
    ['task-definitions','list'],['task-definitions','get','definition'],
    ['task-definitions','create','--body','input.json'],['task-definitions','update','definition','--body','input.json'],
    ['task-definitions','pause','definition','--body','input.json'],['task-definitions','resume','definition','--body','input.json'],
    ['task-definitions','cancel','definition','--body','input.json'],['task-definitions','occurrences','definition'],
])
def test_work_cli_has_explicit_operations_and_private_credentials(arguments):
    """操作可解析，身份来源仅为私有凭据文件。"""
    args=build_parser().parse_args([arguments[0],'--credentials','private.json',*arguments[1:]])
    assert args.credentials=='private.json' and args.operation==arguments[1]
    with pytest.raises(SystemExit):
        build_parser().parse_args([arguments[0],'--credentials','private.json','--user','victim','list'])
