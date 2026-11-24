"""办公镜像内实际提取DOCX与PDF，不在平台进程加载办公依赖。"""

from __future__ import annotations

import io
import json
import zipfile


def docx_fixture():
    """标准库生成最小正文正本，避免测试依赖宿主办公库。"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        archive.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        )
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            "<w:p><w:r><w:t>本人负责整理需求。</w:t></w:r></w:p><w:sectPr/></w:body></w:document>",
        )
    return output.getvalue()


def pdf_fixture(*, blank=False):
    """独立构造含准确xref的文本或空页PDF，不借用被测解析器生成。"""
    stream = b"q Q" if blank else b"BT /F1 12 Tf 10 50 Td (MEETING MINUTES) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, value in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + value + b"\nendobj\n")
    position = len(output)
    output.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets:
        output.extend(f"{offset:010} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{position}\n%%EOF\n".encode())
    return bytes(output)


async def test_docx_text_is_extracted_inside_office_image(office_worker):
    """实际python-docx依赖在断网只读沙盒中提取完整正文。"""
    result = await office_worker(
        "meeting-minutes/scripts/extract.py", {"input_1.docx": docx_fixture()}, ['["input_1.docx"]']
    )
    assert result.code == 0, result.stderr
    assert json.loads(result.stdout)["input_1.docx"] == "本人负责整理需求。"


async def test_pdf_text_is_extracted_inside_office_image(office_worker):
    """实际pypdf提取文本，结果用于固定原文引用。"""
    result = await office_worker(
        "meeting-minutes/scripts/extract.py", {"input_1.pdf": pdf_fixture()}, ['["input_1.pdf"]']
    )
    assert result.code == 0, result.stderr
    assert json.loads(result.stdout)["input_1.pdf"].strip() == "MEETING MINUTES"


async def test_pdf_without_text_is_rejected_without_fake_ocr(office_worker):
    """空页或扫描件没有文字层时拒绝，不产生虚构正文。"""
    result = await office_worker(
        "meeting-minutes/scripts/extract.py",
        {"input_1.pdf": pdf_fixture(blank=True)},
        ['["input_1.pdf"]'],
    )
    assert result.code != 0
    assert "OCR" in result.stderr or "文字" in result.stderr


async def test_exact_unicode_quote_offsets_are_computed_inside_office_image(office_worker):
    """由固定正本计算唯一原文区间，拒绝重复或不存在的引文。"""
    text='会议🙂。\n我负责归档资料。\n未定日期。'
    result=await office_worker('meeting-minutes/scripts/cite.py',{'input_1.txt':text.encode()},['input_1.txt','我负责归档资料。'])
    assert result.code==0,result.stderr
    assert json.loads(result.stdout)=={'input_name':'input_1.txt','start':5,'end':13,'quote':'我负责归档资料。'}
    repeated=await office_worker('meeting-minutes/scripts/cite.py',{'input_1.txt':'重复。重复。'.encode()},['input_1.txt','重复。'])
    assert repeated.code!=0
    overlap=await office_worker('meeting-minutes/scripts/cite.py',{'input_1.txt':'哈哈哈'.encode()},['input_1.txt','哈哈'])
    assert overlap.code!=0


async def test_meeting_timestamp_uses_real_timezone_and_rejects_dst_gap(office_worker):
    """给定墙钟时间准确换算UTC秒，DST缺口与歧义不猜测。"""
    result=await office_worker('meeting-minutes/scripts/timestamp.py',{},['2026-10-24T17:00:00','Asia/Shanghai'])
    assert result.code==0,result.stderr
    from datetime import UTC, datetime
    assert datetime.fromtimestamp(json.loads(result.stdout)['timestamp'],UTC).isoformat()=='2026-10-24T09:00:00+00:00'
    for wall in ('2026-03-29T02:30:00','2026-10-25T02:30:00'):
        ambiguous=await office_worker('meeting-minutes/scripts/timestamp.py',{},[wall,'Europe/Paris'])
        assert ambiguous.code!=0
