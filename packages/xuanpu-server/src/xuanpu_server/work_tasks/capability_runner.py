"""只读能力采用真实轮次和工具回执完成，不伪造办公文件。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from xuanpu_server.work_tasks.models import WorkTaskRecord


@dataclass
class CapabilityEvidence:
    """从平台实际消费的内核事件取证，不采信模型自述的业务结果。"""

    texts: list[str] = field(default_factory=list)
    tools: list[dict[str, str]] = field(default_factory=list)
    submission_id: str | None = None
    completed: bool = False
    unsafe_resume: bool = False

    def observe(self, kind: str, data: dict[str, Any], submission_id: str | None) -> None:
        """保留有限摘要和调用引用；未知或错误结果不标记完成。"""
        if kind == 'assistant_text' and isinstance(data.get('delta'), str):
            if sum(len(text) for text in self.texts) < 16000:
                self.texts.append(data['delta'][:16000])
        if kind in {'tool_call_started', 'tool_call_completed'}:
            name = data.get('tool_name') or data.get('name') or data.get('tool', '')
            if isinstance(name, str) and name and not name.startswith(('api__', 'mcp__')):
                self.unsafe_resume = True
        if kind == 'tool_call_completed' and len(self.tools) < 256:
            self.tools.append({'call_id': str(data.get('call_id', '')),
                'result_digest': hashlib.sha256(str(data.get('output', data)).encode()).hexdigest()})
        if kind == 'turn_completed' and data.get('is_root') is True:
            self.submission_id, self.completed = submission_id, True

    def result(self, task: WorkTaskRecord) -> dict[str, Any]:
        """只有实际根完成且无未确认写入，才能形成能力完成回执。"""
        approved = {entry.get('related_call_id') for entry in task.pending} if task.pending_decision else set()
        receipts = [entry for entry in task.result_receipt.get('read_receipts', []) if
            entry.get('submission_id') == self.submission_id and entry.get('session_id') == task.session_id
            and (entry.get('attempt_no') == task.attempt_no or entry.get('call_id') in approved)]
        if task.unverified_write:
            return {'status': 'outcome_unknown', 'error_code': 'capability_receipt_unverified'}
        if not self.completed or not self.submission_id or not receipts:
            return {'status': 'failed', 'error_code': 'capability_receipt_missing'}
        return {'status': 'succeeded', 'outputs_verified': True, 'decisions_complete': True,
            'result_summary': ''.join(self.texts)[:16000], 'result_receipt': {
                'verified': True, 'session_id': task.session_id, 'submission_id': self.submission_id,
                'tool_receipts': receipts, 'policy': 'capability_receipt_v1'}}
