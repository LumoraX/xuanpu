"""连接事务内的幂等、审计、当前状态与最小公共视图。"""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import ConflictError, NotFoundError

MAX_RECORDS=10000

if TYPE_CHECKING:
    from xuanpu_server.connections.store import ConnectionState
    from xuanpu_server.tenancy import Principal


def get(state: ConnectionState,key: str,kind: str) -> dict[str,Any]:
    """按领域类型查找，不泄露其他对象。"""
    value=state.records.get(key)
    if value is None or value.get('kind')!=kind:
        raise NotFoundError('连接记录不存在')
    return value


def replay(state: ConnectionState,key: str,payload: Any) -> dict[str,Any] | None:
    """相同请求返回原回执，载荷不同明确冲突。"""
    previous=state.records.get(key)
    if previous is None:
        return None
    if previous['fingerprint']!=fingerprint(payload):
        raise ConflictError('请求标识已用于不同内容')
    return dict(previous['result'])


def fingerprint(payload: Any) -> str:
    """只保存规范JSON摘要。"""
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def remember(state: ConnectionState,key: str,payload: Any,result: dict[str,Any],*,security: bool=False) -> None:
    """与领域变更同事务保存稳定结果。"""
    if len(state.records)>=MAX_RECORDS and not security:
        raise ConflictError('连接元数据已达租户上限，请归档历史回执')
    state.records[key]={'kind':'receipt','fingerprint':fingerprint(payload),'result':result}


def audit(state: ConnectionState,who: Principal,kind: str,at: float,**data: Any) -> None:
    """仅记录状态标识与epoch，不含凭据、资料或原始URL。"""
    state.audits.append({'kind':'connection_'+kind,'at':at,'actor':who.actor,
                         'user_id':who.user_id,'data':data})


def public_install(record: dict[str,Any]) -> dict[str,Any]:
    """员工只得到操作范围和状态，应用密钥引用也不输出。"""
    return {k:record[k] for k in ('id','name','provider','status','operations','version','capability_status')}


def public_consent(record: dict[str,Any],now: float) -> dict[str,Any]:
    """到期由当前时钟判定，不依赖后台清理。"""
    result={k:record[k] for k in ('consent_id','installation_id','operations','purpose','expires_at','status','version','epoch')}
    if result['status']=='active' and result['expires_at']<=now:
        result['status']='expired'
    return result
