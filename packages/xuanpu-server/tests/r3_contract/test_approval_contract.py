"""组织审批冻结具体员工及固定成果，不能代替工具批准或成果签收。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_policy_allows_named_employee_without_unit_and_max_three_steps():
    """无部门可以预配明确员工顺序，空策略或四步不被误当成自动通过。"""
    from xuanpu_server.approvals.schemas import PolicyInput
    base = dict(request_id='policy', confirmed=True, name='到人核对', steps=[{'selector': 'user', 'user_id': 'usr_alice'}])
    assert PolicyInput.model_validate(base).steps[0].selector == 'user'
    for steps in ([], [{'selector': 'user', 'user_id': 'usr_alice'}] * 4):
        with pytest.raises(ValidationError):
            PolicyInput.model_validate({**base, 'steps': steps})


@pytest.mark.parametrize('step', [
    {'selector': 'user', 'user_id': 'usr_a', 'unit_id': 'unit'},
    {'selector': 'user'}, {'selector': 'unit_role', 'unit_id': 'unit'},
    {'selector': 'unit_role', 'unit_id': 'unit', 'role': 'org_manager'},
    {'selector': 'model', 'user_id': 'usr_a'},
])
def test_step_selector_cannot_mix_authority_sources(step):
    """明确员工与部门职责互斥，模型不能指定审批主体。"""
    from xuanpu_server.approvals.schemas import StepSelector
    with pytest.raises(ValidationError):
        StepSelector.model_validate(step)


def test_request_accepts_policy_reference_not_client_approvers():
    """申请只能引用预配规则和当前目标，不信任申请人提供的审批人集合。"""
    from xuanpu_server.approvals.schemas import SubmitApproval
    values = dict(request_id='approval', confirmed=True, policy_id='policy', target_type='handoff',
        target_id='handoff', target_version=3, result_refs=[{'material_id':'material','revision':1,'sha256':'a'*64}])
    assert SubmitApproval.model_validate(values).target_type == 'handoff'
    with pytest.raises(ValidationError):
        SubmitApproval.model_validate({**values, 'approvers': ['usr_a']})
