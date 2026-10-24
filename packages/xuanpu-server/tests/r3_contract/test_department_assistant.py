"""到人和部门复用普通助手与原生确认；模型只起草，不产生业务决定。"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError


async def test_assistant_handoff_draft_requires_native_scope_and_creates_nothing(handoff_http,r3_world,step):
    """自然语言可准备说明，实际目标与来源必须由当前员工另行选择确认。"""
    http=handoff_http
    r3_world.models.script(step(text=json.dumps({'package':'@collaboration','entry':'draft','message':None,
        'handoff_draft':{'title':'请同事核对','summary':'核对选定材料并提交结果'}})))
    body={'request_id':'handoff-draft-only','text':'帮我交给同事核对材料'}
    response=await http['alice'].post('/v1/assistant/route',json=body)
    assert response.status_code==200,response.text
    assert response.json()['handoff_draft']['title']=='请同事核对' and response.json()['open_collaboration']
    assert (await http['alice'].get('/v1/handoffs?view=sent')).json()['total']==0
    assert (await http['alice'].post('/v1/assistant/route',json=body)).json()==response.json()
    assert len(r3_world.models.requests)==1


@pytest.mark.parametrize('patch',[{'actor_id':'usr_other'},{'action':'approve'},{'target_id':'usr_bob'},{'policy_id':'fake'}, {'material_revision_ids':['secret']}])
def test_model_handoff_cannot_supply_authoritative_fields(patch):
    """员工、权限和组织决定都不能借模型输出混进确认卡。"""
    from xuanpu_server.assistant import Choice
    with pytest.raises(ValidationError):
        Choice.model_validate({'package':'@collaboration','entry':'draft','message':None,
            'handoff_draft':{'title':'建议','summary':'仅文字',**patch}})
