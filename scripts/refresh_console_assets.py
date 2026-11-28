"""更新前端静态资源地址指纹（/assets/<文件>?v=<摘要>），升级时不复用旧脚本或样式缓存。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

ASSET_PREFIX = "/assets/"


def main() -> None:
    """按资源实际字节重写 HTML 中的静态资源地址指纹，重复执行幂等。"""
    directory = Path(__file__).resolve().parents[1] / "web/static"

    def fingerprint(match: re.Match[str]) -> str:
        """统一输出 /assets/<文件名>?v=<摘要>，不修改导航链接。"""
        name = match[2].removeprefix(ASSET_PREFIX)
        digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()[:16]
        return f'{match[1]}{ASSET_PREFIX}{name}?v={digest}"'

    # 完整工作台与小院极简入口都按实际字节刷新
    for page in ("index.html", "entry.html"):
        path = directory / page
        path.write_text(
            re.sub(
                r'((?:src|href)=")((?:/assets/)?[^"?]+\.(?:js|css))(?:\?[^"]*)?"',
                fingerprint,
                path.read_text(encoding="utf-8"),
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
