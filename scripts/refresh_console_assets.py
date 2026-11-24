"""更新控制台资源地址指纹，升级时不复用旧脚本或样式缓存。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path


def main() -> None:
    """只按资源实际字节重写本地入口，重复执行不改变内容。"""
    directory = Path(__file__).resolve().parents[1] / 'web/static'

    def fingerprint(match: re.Match[str]) -> str:
        """保留文件名并替换旧查询指纹，不修改导航链接。"""
        name = match[2]
        digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()[:16]
        return match[1] + name + '?v=' + digest + '"'

    # 功能页与极简入口（R7 起入口也带小院脚本）都按实际字节刷新
    for page in ('index.html', 'entry.html'):
        path = directory / page
        path.write_text(re.sub(r'((?:src|href)=")([^"?]+\.(?:js|css))(?:\?[^\"]*)?"', fingerprint, path.read_text()))


if __name__ == '__main__':
    main()
