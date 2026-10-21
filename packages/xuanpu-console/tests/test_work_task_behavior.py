"""运行真实控制台脚本，验证断线重试和身份代际，不以字符串匹配代替行为。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from xuanpu_console import static_dir


def test_work_task_behavior() -> None:
    """身份切换丢弃迟到结果，未知提交只能重发同一完整决定。"""
    node = shutil.which('node')
    assert node is not None, '行为验证需要Node.js'
    result = subprocess.run([node, str(Path(__file__).with_name('work_task_behavior.cjs')),str(static_dir())],
                            capture_output=True,text=True,timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
