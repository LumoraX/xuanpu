"""使用成熟解析器检查原材料结构，不宣称签章、票据或业务事实已核实。"""
from __future__ import annotations

import io

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from xuanpu_finance.errors import FinanceError


def check_document(raw: bytes,media: str) -> None:
    """固定文件有限结构校验，拒绝不可读加密PDF和格式不符或超大图片。"""
    if not raw or len(raw)>50*1024*1024:
        raise FinanceError('ARCHIVE_DOCUMENT_LIMIT',409,'原件为空或超过结构检测大小限制')
    try:
        if media=='application/pdf':
            reader=PdfReader(io.BytesIO(raw),strict=True)
            if reader.is_encrypted or not 0<len(reader.pages)<=2000:
                raise ValueError('PDF加密不可读、无页或超出页数限制')
            for page in reader.pages:
                if page.mediabox.width<=0 or page.mediabox.height<=0:
                    raise ValueError('PDF页结构无效')
        else:
            expected={'image/png':'PNG','image/jpeg':'JPEG'}[media]
            with Image.open(io.BytesIO(raw),formats=[expected]) as image:
                if image.format!=expected or not 0<image.width*image.height<=20_000_000:
                    raise ValueError('图片格式或像素规模不符')
                image.verify()
            with Image.open(io.BytesIO(raw),formats=[expected]) as image:
                image.load()
    except (PyPdfError,UnidentifiedImageError,Image.DecompressionBombError,OSError,ValueError,KeyError,RecursionError) as error:
        raise FinanceError('ARCHIVE_DOCUMENT_INVALID',409,'原件结构不可读、格式不符或超出检测限制') from error
