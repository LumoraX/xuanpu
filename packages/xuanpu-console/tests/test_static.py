"""控制台包：静态文件齐全，页面引用的资源都存在。"""

from __future__ import annotations

import re

from xuanpu_console import static_dir


def test_static_files_present() -> None:
    """三个静态文件都在，页面引用的脚本与样式就是它们。"""
    directory = static_dir()
    assert (directory / "index.html").is_file()
    assert (directory / "app.js").is_file()
    assert (directory / "style.css").is_file()
    html = (directory / "index.html").read_text(encoding="utf-8")
    referenced = set(re.findall(r'(?:src|href)="([^"]+)"', html))
    assert referenced == {"app.js", "style.css"}


def test_console_only_talks_to_documented_endpoints() -> None:
    """页面调用的接口路径都在 HTTP 接口文档描述的范围内。"""
    script = (static_dir() / "app.js").read_text(encoding="utf-8")
    paths = set(re.findall(r'["`](/(?:v1/[a-z]+|healthz)[^"`?]*)', script))
    allowed_prefixes = (
        "/healthz", "/v1/sessions", "/v1/packages", "/v1/audit", "/v1/usage",
    )  # fmt: skip
    assert paths
    assert all(path.startswith(allowed_prefixes) for path in paths), paths


def test_console_can_stop_a_streaming_turn() -> None:
    """对话框有「停止」按钮，点了调停止生成的接口。"""
    html = (static_dir() / "index.html").read_text(encoding="utf-8")
    script = (static_dir() / "app.js").read_text(encoding="utf-8")
    assert re.search(r'<button[^>]*id="stop-btn"[^>]*>停止</button>', html)
    assert "/v1/sessions/${chat.streaming}/cancel`" in script
    assert '$("stop-btn")' in script
