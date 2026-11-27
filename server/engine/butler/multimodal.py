"""管家轻量视觉直读（1–3 张图片魔数校验）与确定性 Mermaid 语法微检（SOUL M2）。

- 绝不信任文件名后缀或客户端声明的 MIME，通过文件头魔数严格校验 PNG / JPEG / WebP / GIF；
- 单轮最多直读 3 张、每张不超过 5 MiB，超过或非图片文件照常作为材料供匠人处理；
- 对管家生成的 Markdown 中的 ``mermaid`` 代码块做确定性图类型与括号引号检查，预防前端渲染报错。
"""

from __future__ import annotations

import base64
import hashlib
import re
from typing import TYPE_CHECKING, Any, Literal

import taifeng

if TYPE_CHECKING:
    from collections.abc import Sequence

    from server.core.tenancy import Principal
    from server.domains.work_tasks.models import InputFile
    from server.platform import Platform

ImageMediaType = Literal["image/png", "image/jpeg", "image/webp", "image/gif"]

MAX_INLINE_IMAGES = 3
MAX_INLINE_IMAGE_BYTES = 5 * 1024 * 1024

_SUPPORTED_MERMAID_TYPES = (
    "flowchart",
    "graph",
    "sequencediagram",
    "statediagram",
    "statediagram-v2",
    "classdiagram",
    "erdiagram",
    "xychart-beta",
    "pie",
    "mindmap",
)
_MERMAID_FENCE_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_UNQUOTED_BRACKET_PAREN_RE = re.compile(r'\[[^"\]]*[()（）][^"\]]*\]')
_HTML_TAG_RE = re.compile(r"</?(?:br|div|span|script|img|a)\b", re.IGNORECASE)


def sniff_image_media_type(raw: bytes) -> ImageMediaType | None:
    """按文件头魔数识别受支持的图片格式；非图片或伪造后缀返回 None。"""
    if len(raw) < 12:
        return None
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


async def collect_inline_images(
    platform: Platform,
    who: Principal,
    input_files: Sequence[InputFile],
    *,
    max_images: int = MAX_INLINE_IMAGES,
    max_item_bytes: int = MAX_INLINE_IMAGE_BYTES,
) -> list[dict[str, Any]]:
    """从本轮附件中提取最多 3 张经魔数校验的轻量图片，构造成 ``taifeng.ImageAttachmentV1``。"""
    attachments: list[dict[str, Any]] = []
    for ref in input_files:
        if len(attachments) >= max_images:
            break
        try:
            raw, _mime, _name = await platform.workspace.download(who, ref.item_id, ref.revision)
        except Exception:
            continue
        if not raw or len(raw) > max_item_bytes:
            continue
        media_type = sniff_image_media_type(raw)
        if media_type is None:
            continue
        att = taifeng.ImageAttachmentV1(
            media_type=media_type,
            size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            content=base64.b64encode(raw).decode("ascii"),
            detail="auto",
        )
        attachments.append(att.model_dump())
    return attachments


def validate_mermaid_blocks(markdown_text: str) -> list[str]:
    """对 Markdown 中的 Mermaid 代码块做确定性轻量校验，返回可读告警列表（空列表表示通过）。"""
    warnings: list[str] = []
    for idx, match in enumerate(_MERMAID_FENCE_RE.finditer(markdown_text), start=1):
        body = match.group(1).strip()
        if not body:
            warnings.append(f"第 {idx} 个 Mermaid 图块为空")
            continue
        first_line = next(
            (ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("%%")),
            "",
        )
        head = first_line.split()[0].lower() if first_line else ""
        if head not in _SUPPORTED_MERMAID_TYPES:
            warnings.append(f"第 {idx} 个 Mermaid 图块类型 {head!r} 不在受支持清单内")
        if _UNQUOTED_BRACKET_PAREN_RE.search(body):
            warnings.append(
                f"第 {idx} 个 Mermaid 图块存在含括号的未加引号节点标签，建议改写为 id[\"标签(说明)\"]"
            )
        if _HTML_TAG_RE.search(body):
            warnings.append(f"第 {idx} 个 Mermaid 图块包含原始 HTML 标签，可能导致渲染异常或被清洗")
    return warnings
