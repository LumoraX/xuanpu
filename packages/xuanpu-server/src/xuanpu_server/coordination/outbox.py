"""持久事件使用语义去重和逐消费者逐事件确认。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class CoordinationEvent(BaseModel):
    """服务端生成的治理事件，内容不默认包含私有正文。"""
    model_config = ConfigDict(frozen=True, extra='forbid')
    id: str
    tenant_id: str
    kind: str
    entity_id: str
    entity_version: int = Field(ge=1)
    actor: str
    at: float
    provenance: str = 'xuanpu_server'
    data: dict[str, JsonValue] = Field(default_factory=dict)
