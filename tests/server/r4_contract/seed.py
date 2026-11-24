"""R4隔离样本，不使用生产人员、秘密或业务材料。"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from spec import pack_package


def package_bytes(root: Path, version: str = '1.0.0') -> bytes:
    """构建无连接器的确定性测试包。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / 'xuanpu.yaml').write_text(f'''apiVersion: xuanpu/v1
kind: Package
metadata: {{name: office-review, version: {version}, description: 合成复核}}
spec:
  skills: {{entries: [review]}}
  policy:
    budget: {{maxSessionTokens: 10000, maxIterations: 10}}
    sandbox: {{network: false, shell: false}}
''')
    skill = root / 'skills/review'
    skill.mkdir(parents=True, exist_ok=True)
    (skill / 'SKILL.md').write_text('---\nname: review\ntype: composite\nversion: 1.0.0\ndescription: 合成复核\nentry: true\ntool_names: [file_read]\n---\n核对材料。\n')
    return pack_package(root)


SUITE = {'format': 'xuanpu-eval-suite/v1', 'cases': [
    {'name': 'review', 'entry': 'review', 'input': '核对这份材料', 'contains': ['核对完成']}]}


def suite_digest(suite: dict) -> str:
    """套件摘要（与平台计算口径一致：规范 JSON 的 SHA-256）。"""
    from server.domains.capabilities.delivery import fingerprint
    from server.domains.capabilities.models import EvaluationSuite

    return fingerprint(EvaluationSuite.model_validate(suite).model_dump(mode='json'))


def delivery(digest: str, version: str = '1.0.0') -> dict:
    """交付描述独立于归档，不包含其自身摘要或租户配置。"""
    return {
        'format': 'xuanpu-delivery/v1', 'capability_key': 'office-review',
        'title': '办公复核', 'summary': '固定材料复核', 'scenario_key': 'office-review',
        'package': {'name': 'office-review', 'version': version, 'digest': digest, 'entry': 'review'},
        'inputs': {'contract': 'office_files', 'mime_types': ['text/csv'], 'max_files': 8},
        'outputs': {'contract': 'office_files', 'output_kind': 'table_summary'},
        'permissions': {'shell': False, 'network': False, 'approval': 'standard',
                        'model_tier': 'balanced', 'max_tokens': 10000, 'max_iterations': 10,
                        'tools': [], 'connections': []},
        'requirements': {'public_config': {}, 'secret_refs': []},
        'evaluation_suite_sha256': suite_digest(SUITE),
    }
