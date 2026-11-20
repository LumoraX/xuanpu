"""办公业务进程内共享的有界归档与固定输入路径规则。"""

from __future__ import annotations

import ast
import json
import os
import re
import zipfile
from pathlib import Path

from defusedxml.ElementTree import fromstring

INPUT_NAME = re.compile(r'input_[1-8]\.(txt|md|csv|docx|pdf|xlsx)')


def canonical_name(name: str) -> str:
    """把交办消息里的 inputs/input_1.csv 与 input_1.csv 规范成同一个固定输入名。

    只去掉一次可选的 ./ 与 inputs/ 前缀；余下必须恰好是固定输入名（不含任何目录分隔符），
    因此 ../、绝对路径、其他目录或嵌套目录都不会被接受。
    """
    text = name.removeprefix('./')
    text = text.removeprefix('inputs/')
    if not INPUT_NAME.fullmatch(text):
        raise ValueError(f'输入名称不合法：{name!r}，请使用交办消息里的 inputs/input_N.扩展名')
    return text


def input_names(raw: str) -> list[str]:
    """解析脚本的 inputs 参数，返回规范化后的固定输入名列表。

    run_script 把每个参数 str() 后作为 argv 传入，所以同一意图可能是 JSON 数组文本、
    模型直接传数组时的 Python 列表表示，或单个名称；三种都按同一列表处理，元素仍逐个校验。
    """
    text = raw.strip()
    if text.startswith('['):
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            try:
                value = ast.literal_eval(text)
            except (ValueError, SyntaxError) as exc:
                raise ValueError('输入文件列表不合法：应为 JSON 数组，例如 ["inputs/input_1.csv"]') from exc
    else:
        value = [text]
    if not isinstance(value, list) or not 1 <= len(value) <= 8 or not all(isinstance(v, str) for v in value):
        raise ValueError('输入文件列表不合法：应为 1 至 8 个文件名组成的数组')
    names = [canonical_name(item) for item in value]
    if len(set(names)) != len(names):
        raise ValueError('输入文件列表不合法：同一输入重复出现')
    return names


def input_path(name: str) -> Path:
    """只读取工作区 inputs/ 下平台约定的固定输入，不接受用户目录或任意路径。"""
    root = Path(os.environ['WORKSPACE']).resolve() / 'inputs'
    path = (root / canonical_name(name)).resolve()
    if path.parent != root or not path.is_file() or not 0 < path.stat().st_size <= 20*1024*1024:
        raise ValueError(f'输入不存在或超限：{name}')
    return path


def run_main(main: object) -> None:
    """脚本入口：业务校验错误只在 stderr 末行给出一句原因并以 2 退出，不让模型去读调用栈。"""
    import sys

    try:
        main()  # type: ignore[operator]
    except ValueError as exc:
        print('错误：' + str(exc), file=sys.stderr)
        raise SystemExit(2) from None


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
