"""只采集安装版本和公开接口，不读取配置或环境变量。"""
from __future__ import annotations

import json
import platform
from importlib import metadata, util
from typing import Any

import taifeng


def collect() -> dict[str, Any]:
    """返回当前解释器实际加载的依赖及稳定执行接口。"""
    packages = {}
    for name in ("taifeng", "taifeng-sandbox", "taifeng-stores", "xuanpu-server"):
        spec = util.find_spec(name.replace("-", "_"))
        packages[name] = {"version": metadata.version(name),
                          "origin": None if spec is None else spec.origin}
    seams = ("CommandExecutor", "WorkspaceFS", "LocalWorkspaceFS", "EnginePool",
             "make_shell_exec_tool", "make_file_read_tool", "make_file_write_tool")
    public = set(taifeng.__all__)
    return {"python": platform.python_version(), "packages": packages,
            "public_seams": {name: name in public for name in seams}}


if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False, indent=2))
