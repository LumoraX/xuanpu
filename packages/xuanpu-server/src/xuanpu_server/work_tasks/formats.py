"""对受控办公格式做有界内容识别，复杂文档解析仍在沙盒进程中。"""

from __future__ import annotations

import io
import zipfile

from xuanpu_server.errors import InvalidRequestError

OOXML = {
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'word/document.xml',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xl/workbook.xml',
}


def validate_content(content: bytes, mime: str) -> None:
    """只接受实际格式匹配且在压缩资源上限内的办公输入。"""
    if not content:
        raise InvalidRequestError('文件内容为空')
    if mime in {'text/plain', 'text/markdown', 'text/csv'}:
        try:
            text = content.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise InvalidRequestError('文本须使用UTF-8编码') from exc
        if '\x00' in text:
            raise InvalidRequestError('文本含有二进制内容')
        return
    if mime == 'application/pdf':
        if not content.startswith(b'%PDF-'):
            raise InvalidRequestError('PDF内容与格式声明不符')
        return
    if mime not in OOXML:
        raise InvalidRequestError('不支持该办公格式')
    _validate_archive(content, mime)


def _validate_archive(content: bytes, mime: str) -> None:
    """办公归档仅识别并限制资源，正文解析留给隔离进程。"""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            names = {entry.filename for entry in entries}
            if OOXML[mime] not in names or '[Content_Types].xml' not in names:
                raise InvalidRequestError('办公文档内容与格式声明不符')
            if len(entries) > 1000 or sum(entry.file_size for entry in entries) > 64 * 1024 * 1024:
                raise InvalidRequestError('办公文档展开资源超限')
            for entry in entries:
                if (entry.file_size > 16 * 1024 * 1024 or entry.flag_bits & 1
                    or entry.file_size > max(entry.compress_size, 1) * 100
                    or entry.filename.startswith('/') or '..' in entry.filename.split('/')):
                    raise InvalidRequestError('办公文档压缩内容不安全')
                lower = entry.filename.lower()
                if 'vbaproject' in lower or '/externallinks/' in lower:
                    raise InvalidRequestError('请提供无宏、无外部链接的值快照')
    except (zipfile.BadZipFile, OSError) as exc:
        raise InvalidRequestError('无法读取办公文档归档') from exc
