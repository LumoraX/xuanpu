"""更新控制台资源地址指纹，升级时不复用旧脚本或样式缓存。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path


def main() -> None:
    """只按资源实际字节重写本地入口，重复执行不改变内容。"""
    directory = Path(__file__).resolve().parents[1] / 'packages/xuanpu-console/src/xuanpu_console/static'
    index = directory / 'index.html'

    def fingerprint(match: re.Match[str]) -> str:
        """保留文件名并替换旧查询指纹，不修改导航链接。"""
        name = match[2]
        digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()[:16]
        return match[1] + name + '?v=' + digest + '"'

    index.write_text(re.sub(r'((?:src|href)=")([^"?]+\.(?:js|css))(?:\?[^\"]*)?"', fingerprint, index.read_text()))


if __name__ == '__main__':
    main()
