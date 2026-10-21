"""只在办公沙盒中提取完整正文；不执行文档、不做OCR或猜测缺失页。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'office/scripts'))
from documents import check_archive, input_path  # noqa: E402


def extract(name: str) -> str:
    """返回准确引用所需的完整字符串，资源超限或扫描PDF明确失败。"""
    path = input_path(name)
    if path.suffix in {'.txt', '.md'}:
        result = path.read_bytes().decode('utf-8-sig')
    elif path.suffix == '.docx':
        from docx import Document

        check_archive(path)
        parts = []
        for block in Document(path).iter_inner_content():
            if hasattr(block, 'text'):
                parts.append(block.text)
            else:
                parts.extend('\t'.join(cell.text for cell in row.cells) for row in block.rows)
        result = '\n'.join(parts)
    elif path.suffix == '.pdf':
        from pypdf import PdfReader

        reader = PdfReader(path, strict=True)
        if reader.is_encrypted or not 0 < len(reader.pages) <= 200:
            raise ValueError('PDF加密或页数超限，请提供可提取正文的文本正本')
        pages = []
        for page in reader.pages:
            text = page.extract_text()
            if not text or not text.strip():
                raise ValueError('PDF存在无可提取正文页，需要OCR；请提供文本正本')
            pages.append(text)
        result = '\n'.join(pages)
    else:
        raise ValueError('会议材料仅支持文本、DOCX和正文PDF')
    if not result.strip() or '\x00' in result or len(result) > 200000:
        raise ValueError('正文为空、不安全或超过20万字符，不能用节选冒充全文')
    return result


def main() -> None:
    """固定输入数组到完整正文映射，错误保持非零退出码。"""
    names = json.loads(sys.argv[1])
    if not isinstance(names, list) or not 1 <= len(names) <= 8 or len(set(names)) != len(names):
        raise ValueError('输入数量无效')
    print(json.dumps({name:extract(name) for name in names}, ensure_ascii=False))


if __name__ == '__main__':
    main()
