"""清单的解析与导出。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from xuanpu_spec.errors import ManifestError
from xuanpu_spec.manifest import API_VERSION, PackageManifest

if TYPE_CHECKING:
    from pathlib import Path

# 清单是人写的小文件；超过这个大小多半是传错了文件
MAX_MANIFEST_BYTES = 256 * 1024


def _problems(error: ValidationError) -> tuple[str, ...]:
    """把校验错误整理成「字段路径：原因」。"""
    lines: list[str] = []
    for item in error.errors():
        path = ".".join(str(part) for part in item["loc"]) or "<根>"
        lines.append(f"{path}：{item['msg']}")
    return tuple(lines)


def parse_manifest(text: str) -> PackageManifest:
    """从 YAML 文本解析清单。

    Raises:
        ManifestError: 不是合法 YAML、不是映射、版本不受支持或字段不合法。
    """
    if len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ManifestError(f"清单超过 {MAX_MANIFEST_BYTES} 字节")
    try:
        # safe_load：清单是不可信输入，不允许构造任意对象
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"清单不是合法的 YAML：{exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("清单的顶层必须是映射")
    declared = data.get("apiVersion")
    if declared != API_VERSION:
        raise ManifestError(f"不支持的 apiVersion：{declared!r}（本规范版本为 {API_VERSION!r}）")
    try:
        return PackageManifest.model_validate(data)
    except ValidationError as exc:
        raise ManifestError("清单字段不合法", _problems(exc)) from exc


def load_manifest(path: Path) -> PackageManifest:
    """从文件读取清单。

    Raises:
        ManifestError: 文件不存在、读不了或内容不合法。
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ManifestError(f"读不到清单 {path}：{exc.strerror or exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestError(f"清单 {path} 不是 UTF-8 编码") from exc
    return parse_manifest(text)


def dump_manifest(manifest: PackageManifest) -> str:
    """把清单写回 YAML（字段用对外的驼峰名，省略取默认值的字段）。"""
    data = manifest.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    # apiVersion / kind 即便等于默认值也要写出来：它们是识别清单的依据
    data = {"apiVersion": manifest.api_version, "kind": manifest.kind, **data}
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def manifest_json_schema() -> dict[str, Any]:
    """清单的 JSON Schema，供编辑器补全与外部工具校验。"""
    return PackageManifest.model_json_schema(by_alias=True)


__all__ = [
    "MAX_MANIFEST_BYTES",
    "dump_manifest",
    "load_manifest",
    "manifest_json_schema",
    "parse_manifest",
]
