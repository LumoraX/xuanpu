"""认证URL脱敏须兼容生产Uvicorn访问日志格式器。"""
from __future__ import annotations

import io
import logging

from uvicorn.logging import AccessFormatter
from xuanpu_server.connections.providers.wecom import _CredentialLogFilter


def test_callback_redaction_preserves_access_formatter(capsys):
    """实际格式器能保留HTTP状态，且无脱敏引起的日志异常或授权码。"""
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(AccessFormatter(
        '%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
    handler.addFilter(_CredentialLogFilter())
    record = logging.LogRecord('uvicorn.access', logging.INFO, '', 0,
        '%s - "%s %s HTTP/%s" %d',
        ('127.0.0.1:1234', 'GET', '/v1/auth/wecom/callback?code=private-code&state=opaque',
         '1.1', 401), None)

    handler.handle(record)

    assert capsys.readouterr().err == ''
    assert '401' in output.getvalue()
    assert 'GET' in output.getvalue()
    assert 'private-code' not in output.getvalue()
    assert 'opaque' not in output.getvalue()


def test_normal_access_log_remains_readable(capsys):
    """普通健康访问仍由原生格式器正常输出。"""
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(AccessFormatter('%(request_line)s %(status_code)s', use_colors=False))
    handler.addFilter(_CredentialLogFilter())
    handler.handle(logging.LogRecord('uvicorn.access', logging.INFO, '', 0,
        '%s - "%s %s HTTP/%s" %d',
        ('127.0.0.1:1234', 'GET', '/healthz', '1.1', 200), None))

    assert capsys.readouterr().err == ''
    assert 'GET /healthz HTTP/1.1' in output.getvalue()
    assert '200' in output.getvalue()
