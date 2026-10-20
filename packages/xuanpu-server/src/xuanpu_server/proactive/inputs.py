"""员工上下文分页、分段与提交游标，未送入模型的内容不算已读。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_server.proactive.chunks import cursor_progress, parse_ref, slice_text
from xuanpu_server.proactive.context import (
    business_evidence,
    restore_business,
    restore_evidence,
    session_stamp,
)
from xuanpu_server.proactive.semantic import bounded_context

if TYPE_CHECKING:
    from xuanpu_server.proactive.service import ProactiveService


def chunk_messages(messages: list[dict[str, Any]], offsets: dict[str, int]) -> list[dict[str, Any]]:
    """增量读下一个片段，已有承诺按其稳定引用恢复原片段。"""
    result = []
    for message in messages:
        base = f"message:{message['id']}"
        ref, offset = parse_ref(message['ref']) if 'ref' in message else (base, offsets.get(base, 0))
        result.append({**message, **slice_text(ref, message['text'], offset, limit=1000)})
    return result


def chunk_business(records: list[dict[str, Any]], offsets: dict[str, int]) -> list[dict[str, Any]]:
    """业务会话使用审计身份和字符偏移，不以整条游标跳过长文本尾部。"""
    result = []
    for record in records:
        base, original_offset = parse_ref(record['ref'])
        offset = original_offset if record.get('restored') else offsets.get(base, 0)
        result.append({**record, **slice_text(base, record['text'], offset, limit=1000)})
    return result


async def prepare_context(service: ProactiveService, tenant: str, user: str,
                          claim: dict[str, Any]) -> tuple[str, dict[str, dict[str, Any]]]:
    """续扫与关联来源同时读取，所有来源会话都参与提交前版本复核。"""
    page = await service.control.scan_user_sessions(tenant, user, claim['session_cursor'], 5)
    sessions = list(page)
    active = sorted((row for row in claim['followups'] if row['status'] == 'awaiting_user'),
        key=lambda row: (row.get('next_review_at') or 0, row['id']))[:20]
    for followup in active:
        for ref in followup['evidence_refs']:
            if ref.startswith('business:'):
                source = await service.control.get_session(tenant, ref.split(':')[1])
                if source and source.user_id == user and source.session_id not in {row.session_id for row in sessions}:
                    sessions.append(source)
    claim['session_ids'] = [row.session_id for row in sessions]
    claim['business_stamp'] = session_stamp(sessions)
    messages = await service.workspace.store.messages(tenant, user, claim['message_cursor'], 100)
    messages = [row for row in messages if row['id'] < claim['next_message_id']]
    claim['message_lengths'] = {row['id']: len(row['text']) for row in messages}
    messages = await restore_evidence(service.workspace.store, tenant, user, claim['followups'], messages)
    business = await business_evidence(service.control, tenant, user, sessions, claim['business_cursors'])
    claim['business_rows'] = [{key: row[key] for key in ('ref', 'cursor_key', 'audit_id')}
        | {'total_length': len(row['text'])} for row in business]
    business = await restore_business(service.control, tenant, user, claim['followups'], business)
    messages = chunk_messages(messages, claim['partial_offsets'])
    business = chunk_business(business, claim['partial_offsets'])
    items = sorted((row for row in claim['items'] if row['id'] > claim['item_cursor']), key=lambda row: row['id'])[:25]
    claim['item_page_ids'] = [row['id'] for row in items]
    wanted = {row['item_id'] for row in active if row.get('item_id')}
    items.extend(row for row in claim['items'] if row['id'] in wanted and row['id'] not in claim['item_page_ids'])
    payload, evidence = bounded_context(items, messages, claim['followups'], claim['settings'], service.now(), business)
    claim['next_session_cursor'] = page[-1].session_id if len(page) == 5 else None
    if any(row['ref'] not in evidence or evidence[row['ref']].get('text_truncated') for row in business if not row.get('restored')):
        claim['next_session_cursor'] = claim['session_cursor']
    return payload, evidence


async def context_current(service: ProactiveService, tenant: str, user: str, claim: dict[str, Any]) -> bool:
    """关联来源会话即使不在当前分页，也必须维持同一活动版本和归属。"""
    sessions = [await service.control.get_session(tenant, sid) for sid in claim['session_ids']]
    return bool(session_stamp([row for row in sessions if row and row.user_id == user]) == claim['business_stamp'])


def commit_cursors(scan: dict[str, Any], claim: dict[str, Any], evidence: dict[str, dict[str, Any]]) -> None:
    """仅连续完整消费推进记录游标，未完成片段单独保存偏移。"""
    scan.setdefault('message_cursor', claim['message_cursor'])
    offsets = cursor_progress(list(evidence.values()), claim['partial_offsets'])
    for identifier, length in claim['message_lengths'].items():
        if offsets.get(f'message:{identifier}', -1) < length:
            break
        scan['message_cursor'] = identifier
    cursors = scan.setdefault('business_cursors', {})
    blocked = set()
    for row in claim['business_rows']:
        key = row['cursor_key']
        if key in blocked:
            continue
        if offsets.get(row['ref'], -1) < row['total_length']:
            blocked.add(key)
            continue
        cursors[key] = max(cursors.get(key, 0), row['audit_id'])
    included = {entry['id'] for entry in evidence.values() if entry.get('kind') == 'item'}
    consumed = []
    for identifier in claim['item_page_ids']:
        if identifier not in included:
            break
        consumed.append(identifier)
    if consumed:
        scan['item_cursor'] = consumed[-1]
    if len(consumed) == len(claim['item_page_ids']) and len(consumed) < 25:
        scan['item_cursor'] = ''
    # 仅保留尚未整条消费的偏移；已恢复的历史证据不增加无界游标状态。
    lengths = {f'message:{key}': value for key, value in claim['message_lengths'].items()}
    lengths.update({row['ref']: row['total_length'] for row in claim['business_rows']})
    scan['partial_offsets'] = {ref: offset for ref, offset in offsets.items()
        if ref in lengths and offset < lengths[ref]}
