"""到人交接的业务状态契约，模型输出、工具批准和组织决定不等于签收。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError


@pytest.mark.parametrize('state,action,next_state', [
    ('submitted', 'accept', 'accepted'), ('submitted', 'return', 'returned'),
    ('submitted', 'cancel', 'cancelled'), ('accepted', 'start', 'in_progress'),
    ('accepted', 'request_information', 'needs_information'),
    ('in_progress', 'request_information', 'needs_information'),
    ('needs_information', 'provide_information', 'submitted'),
    ('in_progress', 'deliver', 'delivered'), ('delivered', 'sign_off', 'signed_off'),
    ('delivered', 'dispute', 'disputed'), ('disputed', 'reopen', 'in_progress'),
    ('accepted', 'transfer', 'submitted'), ('in_progress', 'transfer', 'submitted'),
    ('needs_information', 'transfer', 'submitted'),
    ('accepted', 'request_cancel', 'accepted'), ('in_progress', 'request_cancel', 'in_progress'),
    ('needs_information', 'request_cancel', 'needs_information'),
    ('accepted', 'confirm_cancel', 'cancelled'), ('in_progress', 'confirm_cancel', 'cancelled'),
    ('needs_information', 'confirm_cancel', 'cancelled'),
])
def test_handoff_business_edges(state, action, next_state):
    """每个规定的业务边有独立结果，交付与签收不能合并。"""
    from xuanpu_server.handoffs.transitions import next_status
    assert next_status(state, action) == next_state


@pytest.mark.parametrize('state', ['signed_off', 'returned', 'cancelled'])
@pytest.mark.parametrize('action', ['accept', 'start', 'deliver', 'sign_off', 'transfer', 'reopen'])
def test_terminal_handoff_never_revives(state, action):
    """原单终态保留，重做必须创建新单，重复请求仅回放原回执。"""
    from xuanpu_server.errors import ConflictError
    from xuanpu_server.handoffs.transitions import next_status
    with pytest.raises(ConflictError):
        next_status(state, action)


def test_named_handoff_requires_explicit_target_and_confirmation():
    """员工目标不要求部门，身份覆盖字段与未确认提交都拒绝。"""
    from xuanpu_server.handoffs.schemas import SubmitHandoff
    values = dict(request_id='handoff', confirmed=True, expected_version=1, shared_id='shared',
        target_kind='user', target_id='usr_bob', title='材料核对', summary='只核对选定资料')
    assert SubmitHandoff.model_validate(values).target_kind == 'user'
    for patch in ({'confirmed': False}, {'user_id': 'usr_other'}, {'target_kind': 'department'}, {'target_id': ''}):
        with pytest.raises(ValidationError):
            SubmitHandoff.model_validate({**values, **patch})


def test_signoff_requires_exact_delivery_and_explicit_confirmation():
    """只说已完成或引用后台任务成功都不能构造签收请求。"""
    from xuanpu_server.handoffs.schemas import SignOffHandoff
    with pytest.raises(ValidationError):
        SignOffHandoff.model_validate({'request_id': 'sign', 'confirmed': True, 'expected_version': 1})
