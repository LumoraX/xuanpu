"""财务最小身份与严格写入模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=128)]


@dataclass(frozen=True)
class FinancePrincipal:
    """通过独立信任边界验证后的租户、用户身份。"""

    tenant_id: str
    user_id: str
    origin: Literal["agent", "human"] = "agent"


class FileRef(BaseModel):
    """绑定平台文件不可变修订及预期原件摘要。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    file_id: Identifier
    revision: Annotated[int, Field(ge=1)]
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class SupportingMaterialImport(BaseModel):
    """支持材料只接收固定文件版本，禁止借此处置发票异常或声明已核实。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    legal_entity_id: Identifier
    files: Annotated[list[FileRef], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def unique_files(self) -> Self:
        """拒绝同一次材料请求重复引用固定版本。"""
        if len({(item.file_id, item.revision, item.sha256) for item in self.files}) != len(self.files):
            raise ValueError("文件引用不能重复")
        return self


class InvoiceImport(BaseModel):
    """严格接收导入请求；补件必须绑定原异常版本。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    legal_entity_id: Identifier
    files: Annotated[list[FileRef], Field(min_length=1, max_length=100)]
    source_exception_id: Identifier | None = None
    expected_exception_version: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def validate_supplement(self) -> Self:
        """禁止无版本补件和同一请求内重复原件引用。"""
        if (self.source_exception_id is None) != (self.expected_exception_version is None):
            raise ValueError("补件异常与预期版本必须同时提供")
        if len({(item.file_id, item.revision, item.sha256) for item in self.files}) != len(
            self.files
        ):
            raise ValueError("文件引用不能重复")
        return self
