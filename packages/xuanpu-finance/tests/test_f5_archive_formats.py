"""成熟PDF/图片检测实际结构，收件保全不能替代归档可用性。"""
from __future__ import annotations

import io

import pytest
from PIL import Image
from pypdf import PdfWriter
from xuanpu_finance.compliance.archive_checks import check_parse
from xuanpu_finance.errors import FinanceError


def actual_pdf(*,encrypted: bool=False) -> bytes:
    """生成可实际读取的一页PDF，另提供加密分支验证可用性拒绝。"""
    writer=PdfWriter();writer.add_blank_page(width=200,height=100)
    if encrypted:
        writer.encrypt('test-only-password')
    output=io.BytesIO();writer.write(output);return output.getvalue()


def actual_image(format: str='PNG') -> bytes:
    """成熟图像库生成实际像素与文件结构，不用文件头冒充图片。"""
    output=io.BytesIO();Image.new('RGB',(16,12),'white').save(output,format=format);return output.getvalue()


@pytest.mark.parametrize('raw,media',[(actual_pdf(),'application/pdf'),(actual_image(),'image/png'),(actual_image('JPEG'),'image/jpeg')])
def test_archive_parses_actual_pdf_and_image(raw: bytes,media: str) -> None:
    """真实格式结构可读取，归档检测结果只覆盖结构与字节，不证明官方签名。"""
    check_parse(raw,media,'funding_material')


@pytest.mark.parametrize('raw,media',[(b'%PDF-1.4 fake','application/pdf'),(actual_pdf(encrypted=True),'application/pdf'),(actual_image()[:40],'image/png'),(actual_image('JPEG'),'image/png')])
def test_archive_rejects_broken_encrypted_or_mislabeled_material(raw: bytes,media: str) -> None:
    """固定字节即使已收到，损坏、加密不可读或声明格式不符仍不得封存为可用。"""
    with pytest.raises(FinanceError):
        check_parse(raw,media,'funding_material')
