"""xuanpu-spec —— xuanpu 业务包规范。

定义业务方提交给平台的业务包清单（skills、连接器声明、策略、评测用例）及其版本规则。
它是业务方与平台之间的对外契约，因此不依赖平台的任何其他组件。
"""

from __future__ import annotations

from importlib.metadata import version

from spec.archive import archive_digest, pack_package, unpack_package
from spec.call_context import request_fingerprint
from spec.errors import ArchiveError, ManifestError, SpecError
from spec.loader import (
    dump_manifest,
    load_manifest,
    manifest_json_schema,
    parse_manifest,
)
from spec.localization import display_name_for
from spec.manifest import (
    API_VERSION,
    MANIFEST_FILENAME,
    ConnectorSpec,
    EvalCase,
    PackageManifest,
    PackageSpec,
    PolicySpec,
    SkillLibraryUse,
    SkillRecallSpec,
    WebSearchSpec,
    version_key,
)
from spec.openapi import ApiDocument, OpenApiError, Operation, load_openapi, parse_openapi
from spec.validate import (
    ValidationReport,
    connector_tool_name,
    split_tool_name,
    validate_package,
)

__all__ = [
    "API_VERSION",
    "MANIFEST_FILENAME",
    "ApiDocument",
    "ArchiveError",
    "ConnectorSpec",
    "EvalCase",
    "ManifestError",
    "OpenApiError",
    "Operation",
    "PackageManifest",
    "PackageSpec",
    "PolicySpec",
    "SkillLibraryUse",
    "SkillRecallSpec",
    "SpecError",
    "ValidationReport",
    "WebSearchSpec",
    "__version__",
    "archive_digest",
    "connector_tool_name",
    "dump_manifest",
    "display_name_for",
    "load_manifest",
    "load_openapi",
    "manifest_json_schema",
    "pack_package",
    "parse_manifest",
    "request_fingerprint",
    "parse_openapi",
    "split_tool_name",
    "unpack_package",
    "validate_package",
    "version_key",
]

# 版本单一来源是 pyproject.toml：从发行元数据读取，避免两处手改漂移
__version__ = version("xuanpu")
