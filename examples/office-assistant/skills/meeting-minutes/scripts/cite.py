"""仅从固定正本计算唯一原文引文区间，拒绝猜测或歧义匹配。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent))
from extract import extract  # noqa: E402


def main() -> None:
    """引文逐字存在且只出现一次才返回Unicode字符索引。"""
    name,quote=sys.argv[1:]
    if not quote or len(quote)>4000:raise ValueError('引文为空或超限')
    text=extract(name)
    start=text.find(quote)
    if start<0 or text.find(quote,start+1)>=0:raise ValueError('引文不存在或不唯一，请用更完整的原文')
    print(json.dumps({'input_name':name,'start':start,'end':start+len(quote),'quote':quote},ensure_ascii=False))


if __name__=='__main__':main()
