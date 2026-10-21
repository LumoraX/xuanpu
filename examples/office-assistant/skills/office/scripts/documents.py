"""办公业务进程内共享的有界归档与固定输入路径规则。"""

from __future__ import annotations

import os
import re
import zipfile
from pathlib import Path

from defusedxml.ElementTree import fromstring


def input_path(name: str) -> Path:
    """只读取平台约定的输入相对名，不接受用户目录或任意路径。"""
    if not re.fullmatch(r'input_[1-8]\.(txt|md|csv|docx|pdf|xlsx)', name):
        raise ValueError('输入名称不合法')
    root = Path(os.environ['WORKSPACE']).resolve() / 'inputs'
    path = (root / name).resolve()
    if path.parent != root or not path.is_file() or not 0 < path.stat().st_size <= 20*1024*1024:
        raise ValueError('输入不存在或超限')
    return path


def check_archive(path: Path) -> None:
    """在第三方解析器之前限制展开量，并用防实体解析器预检XML。"""
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 1000 or len({entry.filename for entry in entries}) != len(entries):
            raise ValueError('办公归档条目超限或重复')
        if sum(entry.file_size for entry in entries) > 64*1024*1024:
            raise ValueError('办公归档展开总量超限')
        for entry in entries:
            lower = entry.filename.lower()
            if (entry.file_size > 16*1024*1024 or entry.file_size > max(entry.compress_size, 1)*100
                or entry.flag_bits & 1 or 'vbaproject' in lower or '/externallinks/' in lower
                or entry.filename.startswith('/') or '..' in entry.filename.split('/')):
                raise ValueError('拒绝宏、外部链接或不安全压缩内容，请提供值快照')
            if lower.endswith(('.xml', '.rels')):
                fromstring(archive.read(entry))
