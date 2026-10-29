"""内部记录离线预检不要求外部支付能力，亦不启动运行资源。"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from xuanpu_finance.config import FinanceConfig
from xuanpu_finance.readiness.offline import preflight

from .test_f6_preflight import configured
from .test_f9_profile import internal_settings

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def internal_config(tmp_path: Path) -> FinanceConfig:
    """复用F6安全资源，替换为合法隔离公司的内部制度。"""
    base=configured(tmp_path)
    data=base.model_dump();definition=internal_settings()
    for name in ['profile','companies','grants','expense_policies']:
        data[name]=definition[name]
    data['gateway']['tenant_token_files']={'t':next(iter(base.gateway.tenant_token_files.values()))}
    return FinanceConfig.model_validate(data)


async def test_internal_preflight_passes_config_checks_without_external_capabilities(tmp_path: Path,monkeypatch: pytest.MonkeyPatch) -> None:
    """无DB/网络/建目录，成功也只报告正式未验，全部checks通过。"""
    config=internal_config(tmp_path)
    def forbidden(*args: object,**kwargs: object) -> None:
        """运行资源被调用即失败，不能以假成功掩盖副作用。"""
        raise AssertionError('offline preflight touched runtime')
    monkeypatch.setattr('asyncpg.create_pool',forbidden)
    monkeypatch.setattr('httpx.AsyncClient',forbidden)
    monkeypatch.setattr(type(tmp_path),'mkdir',forbidden)
    result=await preflight(config)
    assert result['status']=='not_verified' and result['production_status']=='not_verified'
    assert all(item['status']=='passed' for item in result['checks']),result
    assert not {'settlement_channels','controlled_accounts','funding_capabilities'} & {c['code'] for c in result['checks']}
    for secret in ['postgresql:',str(tmp_path),'service-secret','gateway-secret']:
        assert secret not in json.dumps(result)


async def test_internal_preflight_digest_tracks_nonsecret_configuration(tmp_path: Path) -> None:
    """非秘密配置变化绑定摘要，秘密字节与固定路径不进入摘要材料。"""
    config=internal_config(tmp_path)
    first=await preflight(config)
    config.companies[0].name='另一隔离公司'
    second=await preflight(config)
    assert first['configuration_digest']!=second['configuration_digest']
    config.service_token_file.write_text('other-service-token')
    assert (await preflight(config))['configuration_digest']==second['configuration_digest']
    config.service_token_file.chmod(0o644)
    assert (await preflight(config))['status']=='blocked'
