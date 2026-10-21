"""在隔离业务进程中读取表头，不用猜测二进制工作簿的列名。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rows import input_path, raw_rows  # noqa: E402


def main() -> None:
    """按工作表返回首个非空行的真实列名，保持输入和归档资源限制。"""
    names = json.loads(sys.argv[1])
    if not isinstance(names,list) or not 1 <= len(names) <= 8 or len(set(names)) != len(names):
        raise ValueError('输入文件列表不合法')
    headers = []
    seen = set()
    for name in names:
        for sheet,row,values in raw_rows(input_path(name)):
            key = name,sheet
            if key in seen or not any(value is not None and str(value).strip() for value in values):
                continue
            columns = [str(value).strip() if value is not None else '' for value in values]
            headers.append({'file':name,'sheet':sheet,'row':row,'columns':columns})
            seen.add(key)
    if not headers:
        raise ValueError('没有可读取的表头')
    print(json.dumps({'headers':headers},ensure_ascii=False))


if __name__=='__main__':
    main()
