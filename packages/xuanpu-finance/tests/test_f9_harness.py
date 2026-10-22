"""F9浏览器夹具明确选择内部记录配置，不改变产品身份和模型边界。"""
from __future__ import annotations

import json
import subprocess
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .test_f8_harness import browser


async def test_browser_fixture_uses_strict_internal_records_profile(tmp_path: Path) -> None:
    """真实装配测试人员和固定凭据，离线检查通过仍不能宣称生产可用。"""
    from xuanpu_finance.config import FinanceConfig
    from xuanpu_finance.readiness.offline import preflight

    async with AsyncExitStack() as resources:
        _, ident, people, _, blocker = await browser.bootstrap_people(
            tmp_path, 'http://127.0.0.1:1', resources)
        config = await browser.configure_finance(
            tmp_path, 'postgresql://sample:private-value@127.0.0.1:1/finance_test_browser',
            'http://127.0.0.1:1', 2, Ed25519PrivateKey.generate(), ident, people)
        assert config.profile == 'internal_records'
        FinanceConfig.model_validate(config.model_dump())
        report = await preflight(config)
        assert report['status'] == report['production_status'] == 'not_verified'
        assert all(item['status'] == 'passed' for item in report['checks'])
        assert not config.objects.root.exists()
        assert blocker.attempts == 0
        assert config.settlement_providers == config.compliance_providers == []


def test_unfilled_template_blocks_without_creating_resources(tmp_path: Path) -> None:
    """实际CLI检查未填写模板，只输出固定脱敏结果且不创建凭据或原件目录。"""
    root = Path(__file__).resolve().parents[3]
    source = root / 'examples/finance-internal-records/config.example.yaml'
    target = tmp_path / 'config.yaml'
    target.write_bytes(source.read_bytes())
    result = subprocess.run(
        [sys.executable, '-m', 'xuanpu_finance', '--config', str(target), '--preflight'],
        capture_output=True, text=True, timeout=15, cwd=tmp_path)
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report['status'] == 'blocked'
    assert report['production_status'] == 'not_verified'
    assert 'REPLACE_' not in result.stdout
    assert str(tmp_path) not in result.stdout
    assert result.stderr == ''
    assert list(tmp_path.iterdir()) == [target]
