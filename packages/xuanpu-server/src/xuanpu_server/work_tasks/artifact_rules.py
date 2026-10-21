"""候选清单只能描述文件，文件标识、来源与核验回执由服务器生成。"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import Field, ValidationError

from xuanpu_server.errors import InvalidRequestError

from .models import FrozenModel


class ArtifactCandidate(FrozenModel):
    """明确成果角色和安全相对路径，不接受身份、对象键或成功声明。"""

    path: str = Field(pattern=r'^outputs/[a-zA-Z0-9_./-]{1,160}$')
    name: str = Field(min_length=1, max_length=255, pattern=r'\S')
    role: Literal['minutes', 'actions', 'summary_csv', 'summary_xlsx', 'report', 'evidence']
    mime_type: str = Field(min_length=1, max_length=127)


class OutputManifest(FrozenModel):
    """预定义办公成果要求，任何必需文件缺失都不能通过核验。"""

    kind: Literal['meeting_minutes', 'table_summary']
    artifacts: tuple[ArtifactCandidate, ...] = Field(min_length=1, max_length=8)


MIME_BY_ROLE = {'minutes': 'text/markdown', 'actions': 'application/json',
    'summary_csv': 'text/csv', 'summary_xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'report': 'text/markdown', 'evidence': 'application/json'}
REQUIRED = {'meeting_minutes': {'minutes', 'actions'},
            'table_summary': {'summary_csv', 'summary_xlsx', 'report', 'evidence'}}


def json_value(content: bytes) -> Any:
    """拒绝重复键、非有限数字和非法编码，避免清单有多重解释。"""
    def pairs(entries: list[tuple[str, Any]]) -> dict[str, Any]:
        """JSON对象不得重复字段。"""
        result: dict[str, Any] = {}
        for key, value in entries:
            if key in result:
                raise ValueError('JSON字段重复')
            result[key] = value
        return result

    def nonfinite(value: str) -> None:
        """非标准JSON数字不能进入权威数据。"""
        raise ValueError('非有限数字：' + value)

    try:
        return json.loads(content, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InvalidRequestError('成果JSON无效') from exc


def manifest_from(content: bytes) -> OutputManifest:
    """先做严格JSON验证，再检查角色齐全、路径唯一及格式匹配。"""
    try:
        parsed = json_value(content)
        # JSON数组仅在此边界转为不可变元组，其他类型仍严格验证。
        if isinstance(parsed, dict) and isinstance(parsed.get('artifacts'), list):
            parsed['artifacts'] = tuple(parsed['artifacts'])
        manifest = OutputManifest.model_validate(parsed)
    except ValidationError as exc:
        raise InvalidRequestError('成果清单字段无效') from exc
    roles = [entry.role for entry in manifest.artifacts]
    paths = [entry.path for entry in manifest.artifacts]
    if len(set(roles)) != len(roles) or set(roles) != REQUIRED[manifest.kind] or len(set(paths)) != len(paths):
        raise InvalidRequestError('成果清单角色缺失、重复或包含额外文件')
    for entry in manifest.artifacts:
        if (entry.mime_type != MIME_BY_ROLE[entry.role] or '..' in entry.path.split('/')
            or any(part == '' for part in entry.path.split('/'))):
            raise InvalidRequestError('成果路径或格式声明无效')
    return manifest
