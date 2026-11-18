"""构建样板：确定性打包 package/，把归档摘要与评测套件摘要写进外部 delivery.json。

交付描述不进入它引用的归档（避免摘要自引用）；重复构建字节相同。用法：
    uv run python examples/office-review/build.py [--check]
--check 只核对 delivery.json 是否与当前包和套件一致，不改文件。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from xuanpu_server.capabilities.delivery import fingerprint
from xuanpu_server.capabilities.models import EvaluationSuite
from xuanpu_server.packages import archive_digest
from xuanpu_spec import pack_package

ROOT = Path(__file__).resolve().parent


def digests() -> tuple[str, str]:
    """包归档摘要（sha256:...）与评测套件摘要。"""
    archive = pack_package(ROOT / 'package')
    suite = EvaluationSuite.model_validate(json.loads((ROOT / 'evals' / 'suite.json').read_text()))
    return archive_digest(archive), fingerprint(suite.model_dump(mode='json'))


def main() -> None:
    """写入或核对 delivery.json。"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    package, suite = digests()
    path = ROOT / 'delivery.json'
    delivery = json.loads(path.read_text())
    if args.check:
        ok = delivery['package']['digest'] == package and delivery['evaluation_suite_sha256'] == suite
        print('delivery.json 一致' if ok else 'delivery.json 与包或套件不一致，请重新构建')
        sys.exit(0 if ok else 1)
    delivery['package']['digest'] = package
    delivery['evaluation_suite_sha256'] = suite
    path.write_text(json.dumps(delivery, ensure_ascii=False, indent=2) + '\n')
    print('已写入', path.name, package)


if __name__ == '__main__':
    main()
