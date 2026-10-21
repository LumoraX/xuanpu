"""运行依赖采集不读取部署配置。"""
import runpy
from pathlib import Path


def test_inventory_exposes_versions_and_public_seams() -> None:
    """真实安装分布与公开协议可核对。"""
    script = Path(__file__).resolve().parents[3] / "scripts/runtime_inventory.py"
    result = runpy.run_path(str(script))["collect"]()
    assert result["packages"]["taifeng"]["version"]
    assert result["public_seams"]["CommandExecutor"]
    assert result["public_seams"]["WorkspaceFS"]
    assert "api_key" not in result
    assert "environment" not in result
