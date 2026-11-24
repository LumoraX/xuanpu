"""员工视图白名单及阶段投影，执行秘密永不靠客户端自行隐藏。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .models import TERMINAL

if TYPE_CHECKING:
    from .models import WorkTaskRecord

PUBLIC_FIELDS = {
    'task_id','request_id','matter_id','text','timezone','inputs','package','package_version',
    'package_digest','recovery_policy_version','definition_id','definition_version','execution_kind',
    'entry','completion_policy','output_kind','status','state_version','accepted_at','updated_at',
    'finished_at','error_code','artifacts','outputs_verified','decisions_complete','contains_external_input',
    'cancel_requested','resources_pending','resources_recovered','unverified_write','proposal_ids',
    'action_candidates','action_results','result_summary',
}
RECEIPT_FIELDS = {'call_id','tool','tool_name','transport','effect','verified','receipt_id','state',
                  'operation_id','result_digest','result_sha256','status'}


def execution_fields(task: WorkTaskRecord, dispatch: dict[str, Any] | None) -> dict[str, Any]:
    """仅投影调度事实，重算和继续保存成果分别解释。"""
    dispatch = dispatch or {}
    terminal = task.status in TERMINAL
    phase = 'settled' if terminal else dispatch.get('checkpoint',{}).get('phase','accepted')
    recovering = not terminal and task.attempt_no > 1
    mode = ('await_decision' if task.status=='waiting_user' else 'save_artifacts' if task.frozen_staging else 'recompute') if recovering else None
    return {'execution_stage':phase,'attempt_no':task.attempt_no,
        'next_run_at':dispatch.get('due_at') if not terminal and dispatch.get('state') in {'ready','retry_wait'} else None,
        'block_reason':None if terminal else dispatch.get('block_reason'), 'recovering':recovering,
        'recovery_mode':mode, 'source_occurrence':None if task.definition_id is None else {
            'occurrence_id':task.request_id,'definition_id':task.definition_id,'definition_version':task.definition_version}}


def serialize(task: WorkTaskRecord, dispatch: dict[str, Any] | None,
              effects: list[dict[str, Any]], adapters: set[str]) -> dict[str, Any]:
    """字段白名单阻止新增内部模型字段意外成为员工响应。"""
    result=task.model_dump(mode='json',include=PUBLIC_FIELDS)
    result.update(execution_fields(task,dispatch))
    result['pending']=[{'request_id':entry['request_id'],'detail':{'scope':entry.get('detail',{}).get('scope','tool_use')}}
                       for entry in task.pending if isinstance(entry.get('request_id'),str)]
    result['result_receipt']={group:[{key:value for key,value in entry.items() if key in RECEIPT_FIELDS}
        for entry in task.result_receipt.get(group,[]) if isinstance(entry,dict)]
        for group in ('read_receipts','tool_receipts') if group in task.result_receipt}
    result['error_message']='本次处理未完成，请核对材料、权限和执行状态。' if task.error_code else None
    result['effects']=[{'action_id':e['logical_action_id'],'state':e['state'],
        'can_reconcile':task.status=='outcome_unknown' and e['state']=='dispatch_intent' and e['tool'] in adapters} for e in effects]
    actions=[] if task.status in TERMINAL else ['cancel']
    if task.status=='waiting_user':actions.append('decide')
    if any(e['can_reconcile'] for e in result['effects']):actions.append('reconcile')
    if task.status=='failed' and not effects and not task.unverified_write:actions.append('create_followup')
    result['available_actions']=actions
    if task.recovery_policy_version==2:
        result['execution']={'state':'settled' if task.status in TERMINAL else (dispatch or {}).get('state','blocked'),
            'phase':result['execution_stage'],'attempt_no':task.attempt_no,'block_reason':result['block_reason']}
    return result
