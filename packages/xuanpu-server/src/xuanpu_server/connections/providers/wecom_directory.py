"""可见目录的有界只读采集，逐页保留摘要与明确错误。"""
from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


def digest(value: Any) -> str:
    """来源版本排除采集时间，数据或错误改变则摘要改变。"""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def project_department(raw: Any) -> dict[str, Any]:
    """只保留部门稳定标识、名称和上级，嵌套私密内容不能透传。"""
    from xuanpu_server.connections.providers.wecom import WeComError
    if not isinstance(raw, dict) or type(raw.get('id')) is not int:
        raise WeComError()
    result: dict[str, Any] = {'id': raw['id']}
    if isinstance(raw.get('name'), str) and len(raw['name']) <= 256:
        result['name'] = raw['name']
    if type(raw.get('parentid')) is int:
        result['parentid'] = raw['parentid']
    return result


def project_members(raw: Any) -> list[dict[str, Any]]:
    """限制单页规模，姓名相同的不同userid仍保持两条身份。"""
    from xuanpu_server.connections.providers.wecom import WeComError
    if not isinstance(raw, list) or len(raw) > 1000:
        raise WeComError()
    result = []
    for user in raw:
        if not isinstance(user, dict) or not isinstance(user.get('userid'), str) or not 1 <= len(user['userid'].encode()) <= 64:
            raise WeComError()
        member: dict[str, Any] = {'userid': user['userid']}
        if isinstance(user.get('name'), str) and len(user['name']) <= 256:
            member['name'] = user['name']
        groups = user.get('department')
        if isinstance(groups, list) and len(groups) <= 1000 and all(type(v) is int for v in groups):
            member['department'] = groups
        result.append(member)
    return result


async def capture(get: Callable[[str, dict[str, str]], Awaitable[dict[str, Any]]], token: str) -> dict[str, Any]:
    """外部采集完全在业务事务外；某页失败明确标记，绝不推导删除。"""
    from xuanpu_server.connections.providers.wecom import WeComError
    started = time.time()
    pages: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    async def page(path: str, parameters: dict[str, str]) -> dict[str, Any] | None:
        """每页仍经过broker当前授权检查；认证失败交给broker刷新一次。"""
        scope = {k: v for k, v in parameters.items() if k != 'access_token'}
        try:
            value = await get(path, parameters)
        except WeComError as exc:
            if exc.provider_code in (40014, 42001):
                raise
            errors.append({'path': path, 'scope': scope, 'code': exc.provider_code})
            pages.append({'path': path, 'scope': scope, 'sha256': None, 'error_code': exc.provider_code})
            return None
        pages.append({'path': path, 'scope': scope, 'sha256': digest(value), 'error_code': None})
        return value

    raw = await page('/cgi-bin/department/list', {'access_token': token})
    departments = [] if raw is None else raw.get('department')
    if not isinstance(departments, list) or len(departments) > 100:
        raise WeComError()
    observed = []
    for department in departments:
        item = project_department(department)
        members = await page('/cgi-bin/user/simplelist', {'access_token': token,
                              'department_id': str(item['id']), 'fetch_child': '0'})
        item['members'] = [] if members is None else project_members(members.get('userlist'))
        item['members_observed'] = members is not None
        observed.append(item)
    result = {'departments': observed, 'scope': 'application_visible', 'complete': False,
              'completeness_reason': '企业应用可见范围不等于全企业；缺失成员不授权停用',
              'pages': pages, 'errors': errors}
    return {**result, 'source_revision': digest(result), 'capture_started_at': started,
            'capture_finished_at': time.time()}
