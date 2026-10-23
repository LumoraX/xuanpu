"""组织命令复用私有身份档案和产品接口，不提供隐式全量应用。"""
from __future__ import annotations

import pytest
from xuanpu_cli.main import build_parser


@pytest.mark.parametrize('arguments', [
    ['me'], ['directory'], ['list', 'units'], ['get', 'units', 'unit'],
    ['create', 'units', '--body', 'input.json'],
    ['change', 'roles', 'role', '--body', 'input.json'],
    ['preview-import', '--body', 'input.json'],
    ['get-import', 'preview'], ['apply-import', 'preview', '--body', 'input.json'],
    ['authority'], ['preview-authority', '--body', 'input.json'],
    ['apply-authority', 'preview', '--body', 'input.json'],
])
def test_organization_cli_uses_explicit_private_profile(arguments):
    """身份不能由命令行补充或覆盖。"""
    args = build_parser().parse_args(['organization', '--credentials', 'private.json', *arguments])
    assert args.credentials == 'private.json' and args.operation == arguments[0]
    with pytest.raises(SystemExit):
        build_parser().parse_args(['organization', '--credentials', 'private.json', '--user', 'victim', 'me'])
