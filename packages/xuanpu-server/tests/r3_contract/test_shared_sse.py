"""排队中的事件也必须在实际发送前复核来源。"""
from __future__ import annotations

from types import SimpleNamespace

from xuanpu_server.api.sse import stream_turn
from xuanpu_server.errors import SharedAccessRevokedError


async def test_queued_sse_content_is_not_emitted_after_revocation():
    """已经生成但尚未外发的文本不能在撤权后继续发送。"""
    closed = False
    async def events():
        """模拟先前已排队的内核内容。"""
        nonlocal closed
        try:
            yield SimpleNamespace(msg=SimpleNamespace(kind='turn_text', data={'text': '不应外发'}),
                submission_id='sub', seq=1, timestamp=None)
        finally:
            closed = True
    stream = SimpleNamespace(events=events, overflowed=False)
    turn = SimpleNamespace(session_id='ses', submission_id='sub', stream=stream)
    checks = 0
    async def check():
        """首帧可用，随后来源已撤销。"""
        nonlocal checks
        checks += 1
        if checks > 1:
            raise SharedAccessRevokedError('共享资料权限已失效')
    frames = [part async for part in stream_turn(turn, check_sources=check)]
    wire = b''.join(frames).decode()
    assert '不应外发' not in wire
    assert 'shared_access_revoked' in wire
    assert closed, '提前结束外发也必须关闭事件迭代器'
