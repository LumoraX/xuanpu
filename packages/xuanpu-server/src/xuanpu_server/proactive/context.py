"""有界读取当前员工的关联业务证据和已有承诺来源。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_server.controlplane.models import AuditQuery
from xuanpu_server.proactive.chunks import parse_ref
from xuanpu_server.workspace.models import fingerprint

if TYPE_CHECKING:
    from xuanpu_server.controlplane.models import SessionRecord
    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.workspace.store import WorkspaceStore


def session_stamp(sessions: list[SessionRecord]) -> str:
    """会话新轮次会更新活动时间，模型提交前用于复核输入。"""
    return fingerprint([(session.session_id, session.updated_at, session.status) for session in sessions])


async def business_evidence(control: ControlPlaneStore, tenant: str, user: str,
                            sessions: list[SessionRecord], cursors: dict[str, int]) -> list[dict[str, Any]]:
    """只读最多五个本人会话，每会话最多五十条增量审计。"""
    result: list[dict[str, Any]] = []
    for session in sessions[:25]:
        if session.user_id != user or session.tenant_id != tenant:
            raise ValueError('业务会话归属无效')
        for kind in ('message_sent', 'turn_text'):
            cursor_key = session.session_id + ':' + kind
            rows = await control.query_audit(tenant, AuditQuery(session_id=session.session_id,
                user_id=user, kind=kind, after_id=cursors.get(cursor_key, 0), limit=50))
            for row in rows:
                if not isinstance(row.data.get('text'), str):
                    raise ValueError('业务文本记录格式错误')
                result.append({'ref': f'business:{session.session_id}:{row.audit_id}', 'kind': 'message',
                    'role': 'user' if row.kind == 'message_sent' and (row.actor == 'user:' + user or row.actor.endswith('/user:' + user)) else 'assistant',
                    'actor': row.actor,
                    'text': row.data['text'], 'at': row.at, 'session_id': session.session_id,
                    'cursor_key': cursor_key, 'audit_id': row.audit_id})
    return result


async def restore_evidence(store: WorkspaceStore, tenant: str, user: str,
                           followups: list[dict[str, Any]], messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """周期复查重新读取待跟进承诺的原消息，不能只有增量而丢失证据。"""
    present = {message.get('ref', f"message:{message['id']}") for message in messages}
    result = list(messages)
    active = sorted((row for row in followups if row['status'] not in ('resolved', 'dismissed', 'ignored')),
        key=lambda row: (row.get('next_review_at') or float('inf'), row['id']))
    for followup in active[:20]:
        for ref in [*followup['evidence_refs'], *followup.get('context_refs', [])]:
            if ref.startswith('message:') and ref not in present:
                base, _ = parse_ref(ref)
                message_id = int(base.split(':')[1])
                rows = await store.messages(tenant, user, message_id - 1, 1)
                if rows and rows[0]['id'] == message_id:
                    result.append({**rows[0], 'ref': ref})
                    present.add(ref)
    return result


async def restore_business(control: ControlPlaneStore, tenant: str, user: str,
                           followups: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """复查本人业务会话承诺时恢复原审计，不依赖尚未消费的增量。"""
    result = list(current)
    present = {record['ref'] for record in current}
    active = sorted((row for row in followups if row['status'] not in ('resolved', 'dismissed', 'ignored')),
        key=lambda row: (row.get('next_review_at') or float('inf'), row['id']))
    for followup in active[:20]:
        for ref in [*followup['evidence_refs'], *followup.get('context_refs', [])]:
            if not ref.startswith('business:') or ref in present:
                continue
            base, _ = parse_ref(ref)
            _, session_id, audit_id = base.split(':')
            session = await control.get_session(tenant, session_id)
            if session is None or session.user_id != user:
                continue
            rows = await control.query_audit(tenant, AuditQuery(session_id=session_id,
                user_id=user, after_id=int(audit_id) - 1, limit=1))
            if not rows or rows[0].audit_id != int(audit_id):
                continue
            row = rows[0]
            if row.kind not in ('message_sent', 'turn_text') or not isinstance(row.data.get('text'), str):
                raise ValueError('业务跟进证据类型错误')
            result.append({'ref': ref, 'restored': True, 'kind': 'message', 'role': 'user' if row.kind == 'message_sent' and (row.actor == 'user:' + user or row.actor.endswith('/user:' + user)) else 'assistant',
                    'actor': row.actor,
                'text': row.data['text'], 'at': row.at, 'session_id': session_id,
                'audit_id': row.audit_id, 'cursor_key': session_id + ':' + row.kind})
            present.add(ref)
    return result
