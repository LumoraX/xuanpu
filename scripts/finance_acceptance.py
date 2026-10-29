"""仅面向显式本机独立测试库的财务端到端验收入口。"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from acceptance.finance_runner import run_acceptance


def main() -> None:
    """解析固定测试集和新证据目录；输入或数据库错误均脱敏输出。"""
    parser=argparse.ArgumentParser(description='财务本机隔离验收，不运行正式供应商动作')
    parser.add_argument('--dsn-file',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--suite',choices=['quick','full'],default='quick')
    parser.add_argument('--timeout',type=int,default=1800)
    args=parser.parse_args()
    try:
        report=asyncio.run(run_acceptance(root=Path(__file__).resolve().parents[1],
            dsn_file=args.dsn_file,output=args.output.resolve(),suite=args.suite,timeout=args.timeout))
    except (Exception,KeyboardInterrupt):
        print(json.dumps({'status':'failed','code':'ACCEPTANCE_INPUT_OR_RESOURCE_INVALID',
            'production_status':'not_verified'}))
        raise SystemExit(2) from None
    print(json.dumps(report,ensure_ascii=False))
    raise SystemExit(0 if report['status'] in {'passed','partial'} else 1)


if __name__=='__main__':
    main()
