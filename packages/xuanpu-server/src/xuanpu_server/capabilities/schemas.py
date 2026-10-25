"""能力HTTP输入只接受显式模板和CAS，不接受客户端执行身份。"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, StrictInt, field_validator

from .delivery import Delivery, Identifier, Name, StrictModel


class RegisterInput(StrictModel):
    """登记只读交付说明，不包含激活选项。"""
    request_id: Identifier
    delivery: Delivery


class PreviewInput(StrictModel):
    """配置键必须在模板内声明，secret_refs只接受保管库名称。"""
    request_id: Identifier
    revision: StrictInt = Field(ge=1)
    public_config: dict[Identifier, JsonValue] = Field(default_factory=dict, max_length=30)
    secret_refs: dict[Name, Name] = Field(default_factory=dict, max_length=30)
    connection_refs: dict[Name, Identifier] = Field(default_factory=dict, max_length=20)


class InstallInput(StrictModel):
    """明确确认精确预检和先前安装版本。"""
    request_id: Identifier
    preview_id: Identifier
    expected_version: StrictInt = Field(ge=0)
    confirmed: Literal[True]

    @field_validator('confirmed', mode='before')
    @classmethod
    def explicit_true(cls, value: object) -> object:
        """数字1不能被Literal布尔转换当成本人确认。"""
        if value is not True:
            raise ValueError('必须明确确认')
        return value


class StateInput(StrictModel):
    """ready保留为受门禁动作，A阶段永不放行。"""
    request_id: Identifier
    expected_version: StrictInt = Field(ge=1)
    status: Literal['ready', 'draining', 'disabled']
